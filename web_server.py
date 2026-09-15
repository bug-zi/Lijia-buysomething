# -*- coding: utf-8 -*-
"""
网页版购物助手 — 后端服务
技术：Python 标准库 http.server（零第三方依赖，双击bat即跑）
服务地址：http://127.0.0.1:8765/
API:
    GET  /api/health
    POST /api/chat            body: {"message":"...", "session_id?":"..."}
    GET  /api/sessions        会话列表（多窗口会话管理）
    POST /api/sessions        body: {"title?":"..."}  新建并激活会话
    GET  /api/sessions/<id>   单会话详情（消息+元信息）
    POST /api/sessions/<id>/rename  body: {"title":"..."}
    POST /api/sessions/<id>/switch  激活该会话
    DELETE /api/sessions/<id> 删除会话
    GET  /api/profile
    POST /api/profile         body: {"action":"view|collect|update|clear|delete|cmd","payload":...}
    GET  /api/orders
    GET  /api/logistics/<oid>
    POST /api/aftersale       body: {"order_id":"...", "reason":"..."}
    POST /api/cancel          body: {"order_id":"...", "reason":"..."}
    GET  /api/login            网站登录态列表（配置库）
    POST /api/login            body: {"action":"check|login|clear","platform":"京东|淘宝/天猫"}
    GET  /api/usercenter       用户中心（账户信息 + 界面/行为偏好）
    POST /api/usercenter       body: {"nickname?":"...","avatar_color?":"...","gender?":"male|female","avatar?":"/resource/... 或空串清除","avatar_data?":"data:image/... 上传头像"}
    GET  /api/messages         消息站（操作记录列表 + 未读数）
    POST /api/messages         body: {"action":"read_all|clear"}
    GET  /api/trash            回收站列表（3 天过期倒计时）
    POST /api/trash            body: {"action":"restore|purge|clear","id?":"..."}
    GET  /                    托管 index.html
"""

import os
import sys
import json
import base64
import re
import time
import socket
import threading
import webbrowser
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 保证导入模块：脚本自身路径 + 业务模块目录 core/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
CORE_DIR = os.path.join(BASE_DIR, "core")
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

from shopping_agent import ChatSession  # noqa: E402
from recommender import Recommender  # noqa: E402  会话工厂重建推荐器用
import ai_client  # noqa: E402  安全配置 & LLM 调用（key永远不回传前端）
from product_searcher import save_scrape_cache  # noqa: E402  Trae 浏览器桥接缓存写入
import config_store  # noqa: E402  配置库：网站登录态管理
import session_store  # noqa: E402  会话持久化（chat_sessions.json，路径随账户上下文）
import user_center  # noqa: E402  用户中心：账户信息与偏好
from shopping_list import shopping_list  # noqa: E402  购物清单单例（购买前需求池；内部按上下文切换文件）
import account_manager  # noqa: E402  本地多账户：注册/登录/token 与数据目录上下文
from message_center import message_center  # noqa: E402  消息站：操作记录中心
from trash_bin import trash_bin, sweep_all, EXPIRE_DAYS  # noqa: E402  回收站：删除数据归档（3 天过期）

DEFAULT_PORT = 8765
INDEX_FILE = os.path.join(BASE_DIR, "index.html")
RESOURCE_DIR = os.path.join(BASE_DIR, "resource")

# ---------- 多会话注册表（按账户隔离） ----------
# SESSIONS/_SESSION_LOCKS 键 = "用户名:会话id"；每账户激活会话由其自己的 chat_sessions.json(active_id) 承载；
# _SHARED 为每账户的共享管理器宿主（profile/orders/cart 三模块自带 JSON 持久化，按账户目录隔离）。
SESSION_LOCK = threading.Lock()          # 元锁：只保护 SESSIONS/_SHARED 与锁表（毫秒级持有，绝不裹真实抓取）
SESSIONS: Dict[str, ChatSession] = {}    # 内存中的会话实例（键 "用户名:会话id"，按需懒加载/恢复）
_SESSION_LOCKS: Dict[str, threading.Lock] = {}  # 每会话锁：串行化同一会话 ChatSession 的长耗时操作（互不影响其他会话）
_BROWSER_LOCK = threading.Lock()         # 全局浏览器锁：真实抓取共享同一浏览器 profile，必须全局串行
_CANCEL_FLAGS: Dict[str, bool] = {}      # 取消发送登记：request_id → True（被原请求消费即删）
_CANCEL_EVENTS: Dict[str, threading.Event] = {}  # 取消事件：搜索链路协作式中止的检查信号源
_CANCEL_LOCK = threading.Lock()
_SHARED: Dict[str, ChatSession] = {}     # 每账户的共享管理器宿主（键=用户名，惰性创建）


def _new_chat_session(user: str) -> ChatSession:
    """会话工厂：挂上该账户共享的档案/订单/购物车管理器，并重建推荐器绑定"""
    shared = _shared_for(user)
    cs = ChatSession()
    cs.profile = shared.profile
    cs.orders = shared.orders
    cs.cart = shared.cart
    cs.recommender = Recommender(cs.profile, cs.searcher)
    return cs


def _shared_for(user: str) -> ChatSession:
    """每账户的共享管理器宿主（须持有 SESSION_LOCK 调用；构造期间临时切到该账户上下文）"""
    cs = _SHARED.get(user)
    if cs is None:
        account_manager.set_current(user)
        try:
            cs = ChatSession()
        finally:
            account_manager.set_current(None)
        _SHARED[user] = cs
    return cs


def _ensure_default_session() -> str:
    """保证当前账户存在激活会话（须持有 SESSION_LOCK 调用；账户上下文已由请求分发设置）"""
    lst = session_store.list_sessions()
    if lst:
        aid = session_store.get_active_id()
        if aid not in {s["id"] for s in lst}:
            aid = lst[0]["id"]
            session_store.set_active_id(aid)
        return aid
    s = session_store.create_session("默认会话")
    return s["id"]


def _chat(session_id: str = "") -> Tuple[str, ChatSession]:
    """取当前账户的指定/激活会话（须持有 SESSION_LOCK 调用）。
    内存没有则新建并 restore_state；会话不存在（已被删）回退默认会话。返回 (会话id, 实例)"""
    user = account_manager.current() or ""
    prefix = user + ":"
    sid = (session_id or "").strip() or session_store.get_active_id() or ""
    if sid and session_store.get_session(sid) is None:
        sid = ""
    if not sid:
        sid = _ensure_default_session()
    key = prefix + sid
    cs = SESSIONS.get(key)
    if cs is None:
        cs = _new_chat_session(user)
        meta = session_store.get_session(sid, with_state=True) or {}
        if meta.get("state"):
            cs.restore_state(meta["state"])
        SESSIONS[key] = cs
    return sid, cs


def _lock_for(sid: str) -> threading.Lock:
    """会话专属锁：按「账户:会话」隔离（须先用元锁注册）：同一会话串行，不同会话互不阻塞"""
    user = account_manager.current() or ""
    key = user + ":" + sid
    with SESSION_LOCK:
        lk = _SESSION_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _SESSION_LOCKS[key] = lk
        return lk


def _consume_cancel(req_id: str) -> bool:
    """取走在途聊天请求的取消标记（取后即清）"""
    with _CANCEL_LOCK:
        return bool(_CANCEL_FLAGS.pop(req_id, False))


def _register_cancel_event(req_id: str) -> threading.Event:
    """登记在途请求的取消事件（搜索链路各检查点轮询它实现协作式中止）。
    撤回若先于注册到达（竞态：取消请求比聊天请求先被处理），登记即置位。"""
    with _CANCEL_LOCK:
        ev = _CANCEL_EVENTS.get(req_id)
        if ev is None:
            ev = threading.Event()
            _CANCEL_EVENTS[req_id] = ev
        if _CANCEL_FLAGS.get(req_id):
            ev.set()
        return ev


def json_response(handler, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler, status: int, body_bytes: bytes):
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body_bytes)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body_bytes)


class ShoppingHandler(BaseHTTPRequestHandler):
    server_version = "ShoppingAgent/1.0"

    # ---------- 多账户鉴权 ----------
    def _auth_token(self) -> str:
        return (self.headers.get("X-Auth-Token") or "").strip()

    def _authenticate(self) -> Optional[dict]:
        tok = self._auth_token()
        return account_manager.ACCOUNTS.resolve(tok) if tok else None

    # ---------- 基础 ----------
    def log_message(self, format, *args):
        """安静模式：默认打印会刷屏"""
        return

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.end_headers()

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            return {"_error": str(e)}

    # ---------- 分发 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "" or path == "/":
            self._serve_index()
            return
        # ---------- 多账户鉴权（auth/health/静态资源公开，其余需 token） ----------
        user = self._authenticate()
        if path == "/api/health":
            self._api_health(user)
            return
        if path == "/api/auth/me":
            if user is None:
                json_response(self, 401, {"ok": False, "error": "未登录"})
                return
            json_response(self, 200, {"ok": True, "user": user})
            return
        is_public = path == "/index.html" or path.startswith("/resource/")
        if user is None and not is_public:
            json_response(self, 401, {"ok": False, "error": "未登录"})
            return
        account_manager.set_current(user["username"] if user else None)
        if path == "/api/sessions":
            self._api_sessions_get()
            return
        if path.startswith("/api/sessions/"):
            self._api_session_detail(unquote(path[len("/api/sessions/"):]))
            return
        if path == "/api/profile":
            self._api_profile_get()
            return
        if path == "/api/orders":
            self._api_orders_get()
            return
        if path == "/api/apikey":
            json_response(self, 200, {"ok": True, "config": ai_client.get_config()})
            return
        if path == "/api/login":
            json_response(self, 200, {"ok": True, "states": config_store.get_states()})
            return
        if path == "/api/usercenter":
            json_response(self, 200, {"ok": True, "user": user_center.load()})
            return
        if path == "/api/messages":
            json_response(self, 200, {"ok": True, "messages": message_center.to_list(),
                                      "unread": message_center.unread_count(),
                                      "count": message_center.count()})
            return
        if path == "/api/trash":
            json_response(self, 200, {"ok": True, "items": trash_bin.list_items(),
                                      "expire_days": EXPIRE_DAYS})
            return
        if path == "/api/cart":
            self._api_cart_get()
            return
        if path == "/api/shopping_list":
            json_response(self, 200, {
                "ok": True, "items": shopping_list.to_list(),
                "pending_count": shopping_list.pending_count(),
                "list_text": shopping_list.list_text(),
            })
            return
        if path.startswith("/api/logistics/"):
            oid = unquote(path[len("/api/logistics/"):])
            self._api_logistics_get(oid)
            return
        # 静态资源：resource/ 目录下的图片（壁纸等；basename 防目录穿越，后缀白名单）
        if path.startswith("/resource/"):
            self._serve_resource(unquote(path[len("/resource/"):]))
            return
        # 静态兜底：index.html
        if path == "/index.html":
            self._serve_index()
            return
        json_response(self, 404, {"ok": False, "error": "Not Found", "path": path})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        body = self._read_body()

        # ---------- 多账户鉴权（register/login/logout 公开或自带凭据，其余需 token） ----------
        if path == "/api/auth/register":
            self._api_auth_register(body)
            return
        if path == "/api/auth/login":
            self._api_auth_login(body)
            return
        if path == "/api/auth/logout":
            account_manager.ACCOUNTS.logout(self._auth_token())
            json_response(self, 200, {"ok": True})
            return
        user = self._authenticate()
        if user is None:
            json_response(self, 401, {"ok": False, "error": "未登录"})
            return
        account_manager.set_current(user["username"])

        if path == "/api/auth/delete_account":
            r = account_manager.ACCOUNTS.delete_account(user["username"], (body or {}).get("password", ""))
            if r.get("ok"):
                json_response(self, 200, {"ok": True})
            else:
                json_response(self, 400, r)
            return

        if path == "/api/chat":
            self._api_chat(body)
            return
        if path == "/api/chat_cancel":
            self._api_chat_cancel(body)
            return
        if path == "/api/sessions":
            self._api_sessions_post(body)
            return
        if path.startswith("/api/sessions/"):
            parts = path[len("/api/sessions/"):].split("/")
            if len(parts) == 2 and parts[1] == "rename":
                self._api_session_rename(unquote(parts[0]), body)
                return
            if len(parts) == 2 and parts[1] == "switch":
                self._api_session_switch(unquote(parts[0]))
                return
        if path == "/api/profile":
            self._api_profile_post(body)
            return
        if path == "/api/aftersale":
            self._api_aftersale(body)
            return
        if path == "/api/cancel":
            self._api_cancel(body)
            return
        if path == "/api/apikey":
            self._api_apikey_post(body)
            return
        if path == "/api/login":
            self._api_login_post(body)
            return
        if path == "/api/usercenter":
            self._api_usercenter_post(body)
            return
        if path == "/api/messages":
            self._api_messages_post(body)
            return
        if path == "/api/trash":
            self._api_trash_post(body)
            return
        if path == "/api/cart":
            self._api_cart_post(body)
            return
        if path == "/api/shopping_list":
            self._api_shopping_list_post(body)
            return
        if path == "/api/scrape":
            self._api_scrape_post(body)
            return
        if path == "/api/image":
            self._api_image_post(body)
            return
        # DELETE 风格：/api/cart/<i> 用 POST 也可，这里兼容 do_DELETE
        json_response(self, 404, {"ok": False, "error": "Not Found"})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        user = self._authenticate()
        if user is None:
            json_response(self, 401, {"ok": False, "error": "未登录"})
            return
        account_manager.set_current(user["username"])
        if path.startswith("/api/cart/"):
            idx = unquote(path[len("/api/cart/"):])
            with SESSION_LOCK:
                _sid, cs = _chat()
                resp = cs.cart.remove(int(idx) if idx.isdigit() else 0)
                items = cs.cart.to_list()
            json_response(self, 200, {"ok": True, "response": resp, "cart": items})
            return
        if path.startswith("/api/sessions/"):
            sid = unquote(path[len("/api/sessions/"):])
            if "/" not in sid:
                self._api_session_delete(sid)
                return
        json_response(self, 404, {"ok": False, "error": "Not Found"})

    # ---------- API 实现 ----------
    def _api_health(self, user: Optional[dict]):
        """探活：未登录只回基础信息；登录后附带该账户的购物车/清单计数"""
        payload = {"ok": True, "name": "ShoppingAgent Web", "version": "1.0"}
        if user:
            account_manager.set_current(user["username"])
            try:
                with SESSION_LOCK:
                    _sid, cs = _chat()
                    snap = cs.snapshot()
                payload.update({
                    "cart_count": snap.get("cart_count", 0),
                    "list_pending": shopping_list.pending_count(),
                    "data_source_blocked": snap.get("data_source_blocked", False),
                    "messages_unread": message_center.unread_count(),
                    "trash_count": trash_bin.count(),
                })
            finally:
                account_manager.set_current(None)
        json_response(self, 200, payload)

    def _api_auth_register(self, body: dict):
        first = account_manager.ACCOUNTS.is_empty()
        r = account_manager.ACCOUNTS.register(str(body.get("username") or ""),
                                              str(body.get("email") or ""),
                                              str(body.get("password") or ""))
        if not r.get("ok"):
            json_response(self, 200, {"ok": False, "error": r.get("error", "注册失败")})
            return
        if first:
            moved = account_manager.migrate_legacy_into(r["username"])
            print(f"[accounts] 首个账户 {r['username']} 注册，继承存量数据 {len(moved)} 个文件")
        json_response(self, 200, {"ok": True, "token": r["token"], "username": r["username"]})

    def _api_auth_login(self, body: dict):
        r = account_manager.ACCOUNTS.login(str(body.get("username") or ""),
                                           str(body.get("password") or ""))
        if not r.get("ok"):
            json_response(self, 200, {"ok": False, "error": r.get("error", "登录失败")})
            return
        json_response(self, 200, {"ok": True, "token": r["token"], "username": r["username"]})

    def _api_chat(self, body: dict):
        msg = str(body.get("message") or "").strip()
        if not msg:
            json_response(self, 400, {"ok": False, "error": "message不能为空"})
            return
        req_sid = str(body.get("session_id") or "").strip()
        req_id = str(body.get("request_id") or "").strip()   # 前端生成；用于「取消发送」登记
        cancel_ev = _register_cancel_event(req_id) if req_id else None
        with SESSION_LOCK:
            sid, cs = _chat(req_sid)
            # 自动标题：会话首条消息取用户输入前 20 字（仅当还是默认标题）
            meta = session_store.get_session(sid)
            if meta is not None and not meta.get("messages") and \
                    (meta.get("title") or "") in ("", "新会话", "默认会话"):
                session_store.rename_session(sid, msg[:20])
        # 真实抓取可能耗时数十秒：只持「会话锁+浏览器锁」，元锁已释放——
        # 搜索期间新建/删除会话、看清单等轻操作照常响应（问题疑惑区第5轮）
        with _lock_for(sid):
            prev_state = cs.export_state()   # 取消回滚基线（聊天前的会话态快照）
            cancelled = False
            with _BROWSER_LOCK:
                reply = cs.chat(msg, cancel_check=cancel_ev.is_set if cancel_ev is not None else None)
                # 澄清式问答的选项按钮（无则空数组，前端不渲染）
                chips = cs.pop_chips()
                # 澄清表单结构（维度追问表单化；无则 None，前端不渲染）
                clarify_form = cs.pop_clarify_form()
                # 额外返回状态快照，便于Web端其它Tab同步刷新
                snap = cs.snapshot()
                # 待确认预览（若处于pending）
                pending_preview = None
                if cs._pending_order is not None:
                    o = cs._pending_order
                    pending_preview = {
                        "order_id": o.order_id,
                        "product_name": o.product_name,
                        "platform": o.platform,
                        "seller": o.seller,
                        "price": o.price,
                        "final_price": o.final_price,
                        "receiver": o.receiver,
                        "phone": o.phone,
                        "address": o.address,
                        "rank": cs._pending_rank,
                    }
                cancelled = bool(req_id) and _consume_cancel(req_id)
                if req_id:
                    _CANCEL_EVENTS.pop(req_id, None)
            if cancelled:
                # 用户已取消：会话态整体回滚、消息不入对话历史（本轮响应前端也不会渲染）。
                # 浏览器抓取本身无法凭空打断，本次动作自然结束后结果即被丢弃。
                cs.restore_state(prev_state)
                cs._last_chips = []
                cs._last_clarify_form = None
            else:
                # 持久化：用户消息、AI 回复、会话状态（重启不丢；session_store 自带线程锁）
                session_store.append_message(sid, "user", msg)
                session_store.append_message(sid, "ai", reply)
                session_store.save_state(sid, cs.export_state())
        if cancelled:
            json_response(self, 200, {"ok": False, "cancelled": True, "session_id": sid})
            return
        json_response(self, 200, {
            "ok": True,
            "session_id": sid,
            "reply": reply,
            "chips": chips,
            "clarify_form": clarify_form,
            "snapshot": snap,
            "pending_order": pending_preview,
        })

    # ---------- 多会话管理 ----------
    def _api_sessions_get(self):
        with SESSION_LOCK:
            active = session_store.get_active_id()
        json_response(self, 200, {"ok": True, "sessions": session_store.list_sessions(),
                                  "active_id": active})

    def _api_sessions_post(self, body: dict):
        title = str(body.get("title") or "").strip() or "新会话"
        user = account_manager.current() or ""
        with SESSION_LOCK:
            s = session_store.create_session(title)
            SESSIONS[user + ":" + s["id"]] = _new_chat_session(user)   # 预建空实例
        json_response(self, 200, {"ok": True, "session": s, "active_id": s["id"],
                                  "sessions": session_store.list_sessions()})

    def _api_session_detail(self, sid: str):
        meta = session_store.get_session(sid)
        if meta is None:
            json_response(self, 404, {"ok": False, "error": "会话不存在"})
            return
        with SESSION_LOCK:
            meta["active_id"] = session_store.get_active_id()
        json_response(self, 200, dict(ok=True, **meta))

    def _api_session_rename(self, sid: str, body: dict):
        title = str(body.get("title") or "").strip()
        if not title:
            json_response(self, 400, {"ok": False, "error": "title不能为空"})
            return
        if not session_store.rename_session(sid, title):
            json_response(self, 404, {"ok": False, "error": "会话不存在"})
            return
        json_response(self, 200, {"ok": True, "sessions": session_store.list_sessions()})

    def _api_session_switch(self, sid: str):
        with SESSION_LOCK:
            if session_store.get_session(sid) is None:
                json_response(self, 404, {"ok": False, "error": "会话不存在"})
                return
            session_store.set_active_id(sid)
            _chat(sid)   # 确保该会话已加载并 restore_state
        json_response(self, 200, {"ok": True, "active_id": sid})

    def _api_session_delete(self, sid: str):
        user = account_manager.current() or ""
        with SESSION_LOCK:
            meta = session_store.get_session(sid, with_state=True)
            if meta is None:
                json_response(self, 404, {"ok": False, "error": "会话不存在"})
                return
            # 先归档进回收站（3 天内可还原），再执行删除
            n_msg = len(meta.get("messages") or [])
            trash_bin.add("session", f"会话「{meta.get('title') or '未命名'}」",
                          f"{n_msg} 条消息 · 创建于 {meta.get('created_at', '')}",
                          {"session": meta})
            if not session_store.delete_session(sid):
                json_response(self, 404, {"ok": False, "error": "会话不存在"})
                return
            SESSIONS.pop(user + ":" + sid, None)   # 释放内存实例
            _SESSION_LOCKS.pop(user + ":" + sid, None)  # 回收会话锁
            # 删除的是激活会话时，session_store 已切到最近一个；这里同步并保证有可用会话
            aid = session_store.get_active_id()
            if not aid:
                aid = _ensure_default_session()
            else:
                _chat(aid)   # 预加载新激活会话
        json_response(self, 200, {"ok": True, "active_id": aid,
                                  "sessions": session_store.list_sessions()})

    def _api_profile_get(self):
        with SESSION_LOCK:
            _sid, cs = _chat()
            data = cs.profile.get_all()
            view = cs.profile.view_profile()
        json_response(self, 200, {"ok": True, "profile": data, "view_table": view})

    def _api_profile_post(self, body: dict):
        action = str(body.get("action") or "cmd").lower()
        payload = body.get("payload") or ""
        with SESSION_LOCK:
            _sid, cs = _chat()
        # 兼容两种入参：cmd 模式直接透传字符串；update 模式接收dict
        if action == "update" and isinstance(payload, dict):
            with SESSION_LOCK:
                resp = cs.profile.update_batch(payload)
                data = cs.profile.get_all()
                view = cs.profile.view_profile()
            json_response(self, 200, {"ok": True, "response": resp, "profile": data, "view_table": view})
            return
        if action == "clear":
            with SESSION_LOCK:
                resp = cs.profile.clear_all()
                data = cs.profile.get_all()
                view = cs.profile.view_profile()
            json_response(self, 200, {"ok": True, "response": resp, "profile": data, "view_table": view})
            return
        if action == "delete" and isinstance(payload, str):
            with SESSION_LOCK:
                resp = cs.profile.delete_field(payload)
                data = cs.profile.get_all()
                view = cs.profile.view_profile()
            json_response(self, 200, {"ok": True, "response": resp, "profile": data, "view_table": view})
            return
        # cmd / view / collect 透传字符串
        with SESSION_LOCK:
            if action == "view":
                resp = cs.profile.view_profile()
            elif action == "collect":
                # 强制开启分批录入
                cs._collecting_profile = True
                resp = cs.profile.start_collect()
            elif isinstance(payload, str):
                # 档案命令字符串：查看档案 / 修改XX=YY / 清空XX / ...
                cmd_resp = cs.profile.handle_command(payload)
                if cmd_resp is None:
                    # 可能是建档收集过程
                    if cs._collecting_profile:
                        resp = cs.profile.continue_collect(payload)
                        if "已收集完成" in resp:
                            cs._collecting_profile = False
                    else:
                        resp = f"未识别为档案指令：{payload}"
                else:
                    if "档案录入 (" in cmd_resp and "第 " in cmd_resp:
                        cs._collecting_profile = True
                    resp = cmd_resp
            else:
                resp = "无效请求"
            data = cs.profile.get_all()
            view = cs.profile.view_profile()
        json_response(self, 200, {
            "ok": True,
            "response": resp,
            "profile": data,
            "view_table": view,
            "collecting": cs._collecting_profile,
        })

    def _api_orders_get(self):
        with SESSION_LOCK:
            _sid, cs = _chat()
            text = cs.orders.list_orders()
            items = []
            for oid, o in cs.orders.orders.items():
                items.append({
                    "order_id": o.order_id,
                    "create_time": o.create_time,
                    "status": o.status,
                    "product_id": o.product_id,
                    "product_name": o.product_name,
                    "platform": o.platform,
                    "seller": o.seller,
                    "final_price": o.final_price,
                    "receiver": o.receiver,
                    "phone": o.phone,
                    "address": o.address,
                    "carrier": o.carrier,
                    "track_no": o.track_no,
                    "events": [e.__dict__ for e in o.events],
                })
            items.sort(key=lambda x: x["create_time"], reverse=True)
        json_response(self, 200, {"ok": True, "orders": items, "list_text": text})

    def _api_logistics_get(self, oid: str):
        with SESSION_LOCK:
            _sid, cs = _chat()
            text = cs.orders.query_logistics(oid.strip())
        json_response(self, 200, {"ok": True, "detail": text})

    def _api_aftersale(self, body: dict):
        oid = str(body.get("order_id") or "").strip()
        reason = str(body.get("reason") or "未填写").strip()
        if not oid:
            json_response(self, 400, {"ok": False, "error": "order_id不能为空"})
            return
        with SESSION_LOCK:
            _sid, cs = _chat()
            text = cs.orders.apply_after_sale(oid, reason)
        json_response(self, 200, {"ok": True, "response": text})

    def _api_cancel(self, body: dict):
        oid = str(body.get("order_id") or "").strip()
        reason = str(body.get("reason") or "用户取消").strip()
        if not oid:
            json_response(self, 400, {"ok": False, "error": "order_id不能为空"})
            return
        with SESSION_LOCK:
            _sid, cs = _chat()
            text = cs.orders.cancel_order(oid, reason)
        json_response(self, 200, {"ok": True, "response": text})

    def _api_apikey_post(self, body: dict):
        """
        action:
          - "set"   接收用户输入的 key / base_url / model → 写入本地（安全保存，不回传明文）
          - "test"  用当前 key 做一次连通性测试（使用 ai_client 保存的配置优先）
          - "clear" 删除本地 key
        """
        action = str(body.get("action") or "set").strip().lower()
        with SESSION_LOCK:
            if action == "set":
                key = str(body.get("api_key") or "").strip()
                base = str(body.get("base_url") or "").strip()
                model = str(body.get("model") or "").strip()
                if not key:
                    json_response(self, 400, {"ok": False, "message": "API Key 不能为空。", "config": ai_client.get_config()})
                    return
                r = ai_client.probe_and_set_api_key(key, base, model, timeout=8)
            elif action == "test":
                # 用户可能已经在页面里填了新的key还没保存：支持"带着测试"；否则就是现有key再测一次
                key = str(body.get("api_key") or "").strip()
                base = str(body.get("base_url") or "").strip()
                model = str(body.get("model") or "").strip()
                if key:
                    r = ai_client.probe_and_set_api_key(key, base, model, timeout=8)
                else:
                    # 复用本地 key 做一次 hello 测试：用 mask_key 信息+当前成功率返回
                    cfg = ai_client.get_config()
                    if cfg.get("enabled"):
                        r = {"ok": True, "message": f"已配置 {cfg.get('provider') or cfg.get('base_url')}，模型 {cfg.get('model')}", "config": cfg}
                    else:
                        r = {"ok": False, "message": "当前未配置API Key，请先填写并保存。", "config": cfg}
            elif action == "clear":
                r = ai_client.clear_api_key()
            else:
                r = {"ok": False, "message": f"未知 action: {action}", "config": ai_client.get_config()}
                json_response(self, 400, r)
                return
        json_response(self, 200, r)

    def _api_login_post(self, body: dict):
        """配置库 · 网站登录态：check=同步检测(≤25s)；login=后台扫码(前端轮询)；clear=按域清理"""
        action = str(body.get("action") or "").strip().lower()
        platform = str(body.get("platform") or "").strip()
        if not platform:
            json_response(self, 400, {"ok": False, "message": "platform 不能为空",
                                      "states": config_store.get_states()})
            return
        if action == "check":
            r = config_store.check_login(platform)
        elif action == "login":
            r = config_store.start_manual_login(platform)
        elif action == "clear":
            r = config_store.clear_login(platform)
        else:
            json_response(self, 400, {"ok": False, "message": "未知 action，仅支持 check/login/clear"})
            return
        json_response(self, 200, dict(r, states=config_store.get_states()))

    # ---------- 虚拟购物车 ----------
    def _api_cart_get(self):
        with SESSION_LOCK:
            sid, cs = _chat()
        with _lock_for(sid):
            items = cs.cart.to_list()
            text = cs.cart.list_text()
            with _BROWSER_LOCK:
                alerts = cs.cart.check_prices()  # 顺便触发监控检查（走浏览器，须全局串行）
        json_response(self, 200, {
            "ok": True, "cart": items, "list_text": text,
            "alerts": alerts, "cart_count": len(items),
        })

    def _api_cart_post(self, body: dict):
        """
        action:
          - remove  {index}
          - monitor {index, on?}
          - note    {index, note}
          - clear
          - add     {product:{name,platform,url,current_price,pid?}, note?, monitor?}
        """
        action = str(body.get("action") or "").strip().lower()
        with SESSION_LOCK:
            _sid, cs = _chat()
            if action == "remove":
                idx = int(body.get("index") or 0)
                resp = cs.cart.remove(idx)
            elif action == "monitor":
                idx = int(body.get("index") or 0)
                # on=true 开启；false 关闭；未传则切换
                on = body.get("on")
                if on is None:
                    resp = cs.cart.toggle_monitor(idx)
                else:
                    cur = cs.cart.items[idx - 1].monitor if 1 <= idx <= len(cs.cart.items) else False
                    if bool(on) != cur:
                        cs.cart.toggle_monitor(idx)
                    resp = cs.cart.list_text()
            elif action == "note":
                idx = int(body.get("index") or 0)
                note = str(body.get("note") or "")
                resp = cs.cart.set_note(idx, note)
            elif action == "clear":
                resp = cs.cart.clear()
            elif action == "add":
                p = body.get("product") or {}
                resp = cs.cart.add(
                    str(p.get("name") or ""), str(p.get("platform") or ""),
                    str(p.get("url") or ""), float(p.get("current_price") or p.get("final_price") or 0),
                    note=str(body.get("note") or ""), monitor=bool(body.get("monitor") or False),
                    pid=str(p.get("pid") or ""),
                )
            else:
                json_response(self, 400, {"ok": False, "error": f"未知 action: {action}",
                                          "cart": cs.cart.to_list()})
                return
            items = cs.cart.to_list()
            text = cs.cart.list_text()
        json_response(self, 200, {"ok": True, "response": resp, "cart": items,
                                  "list_text": text, "cart_count": len(items)})

    def _api_shopping_list_post(self, body: dict):
        """
        action:
          - add         {content}
          - remove      {index}
          - toggle_done {index}
          - clear_done
          - clear
        """
        action = str(body.get("action") or "").strip().lower()
        with SESSION_LOCK:
            if action == "add":
                resp = shopping_list.add(str(body.get("content") or ""))
            elif action == "remove":
                resp = shopping_list.remove(int(body.get("index") or 0))
            elif action == "toggle_done":
                resp = shopping_list.toggle_done(int(body.get("index") or 0))
            elif action == "clear_done":
                resp = shopping_list.clear_done()
            elif action == "clear":
                resp = shopping_list.clear()
            else:
                resp = ""
            items = shopping_list.to_list()
            pending = shopping_list.pending_count()
        if not resp:
            json_response(self, 200, {"ok": False, "error": f"未知操作：{action}"})
            return
        json_response(self, 200, {"ok": True, "response": resp,
                                  "items": items, "pending_count": pending})

    # ---------- Trae 浏览器桥接：注入真实抓取缓存 ----------
    def _api_scrape_post(self, body: dict):
        """
        供 Trae agent 用浏览器抓到真实商品后注入缓存。
        body: {keyword: "连衣裙", products: [{...Product dict...}, ...]}
        写入 scrape_cache.json（30 分钟过期），下次 /api/chat 搜索该关键词即返回真实数据。
        """
        keyword = str(body.get("keyword") or "").strip()
        products = body.get("products") or []
        if not keyword or not isinstance(products, list):
            json_response(self, 400, {"ok": False, "error": "需要 keyword 与 products 列表"})
            return
        # 标注 data_source 并写入缓存
        clean = []
        for p in products:
            if not isinstance(p, dict):
                continue
            p["data_source"] = "真实"
            clean.append(p)
        try:
            save_scrape_cache(keyword, clean)
        except Exception as e:
            json_response(self, 500, {"ok": False, "error": f"写入缓存失败：{e}"})
            return
        json_response(self, 200, {
            "ok": True, "keyword": keyword, "count": len(clean),
            "message": f"已缓存 {len(clean)} 条真实商品（关键词：{keyword}，30 分钟有效）",
        })

    # ---------- 多模态图片分析 ----------
    def _api_image_post(self, body: dict):
        """
        接收前端上传的图片（base64 data URI 列表）+ 文字指令。
        body: {"images": ["data:image/jpeg;base64,..."], "text": "根据这张图找同款"}
        分发到场景A（单图找同款）或场景B（多图对比）。
        """
        images = body.get("images") or []
        text = str(body.get("text") or "").strip()
        if not isinstance(images, list) or not images:
            json_response(self, 400, {"ok": False, "error": "images 列表不能为空"})
            return
        # 限制最多 5 张，单张 < 4MB
        clean_images = []
        for img in images[:5]:
            if not isinstance(img, str) or not img.startswith("data:image"):
                continue
            # base64 部分长度粗判 < 4MB(约 5.3M base64 字符)
            if len(img) > 6_000_000:
                json_response(self, 400, {"ok": False, "error": "单张图片过大（>4MB），请压缩后重试"})
                return
            clean_images.append(img)
        if not clean_images:
            json_response(self, 400, {"ok": False, "error": "未提供有效的 data:image URI"})
            return
        req_sid = str(body.get("session_id") or "").strip()
        req_id = str(body.get("request_id") or "").strip()
        cancel_ev = _register_cancel_event(req_id) if req_id else None
        with SESSION_LOCK:
            sid, cs = _chat(req_sid)
        with _lock_for(sid):
            prev_state = cs.export_state()
            cancelled = False
            with _BROWSER_LOCK:
                user_text = text or "（图片消息）"
                reply = cs.chat_with_images(
                    clean_images, text,
                    cancel_check=cancel_ev.is_set if cancel_ev is not None else None)
                snap = cs.snapshot()
                cancelled = bool(req_id) and _consume_cancel(req_id)
                if req_id:
                    _CANCEL_EVENTS.pop(req_id, None)
            if cancelled:
                cs.restore_state(prev_state)
                cs._last_chips = []
                cs._last_clarify_form = None
            else:
                # 持久化：图片本体不入库（体积大），只存文字与回复（session_store 自带线程锁）
                session_store.append_message(sid, "user", user_text)
                session_store.append_message(sid, "ai", reply)
                session_store.save_state(sid, cs.export_state())
        if cancelled:
            json_response(self, 200, {"ok": False, "cancelled": True, "session_id": sid})
            return
        json_response(self, 200, {"ok": True, "session_id": sid, "reply": reply, "snapshot": snap})

    def _api_chat_cancel(self, body: dict):
        """取消在途聊天/图片请求：仅登记取消标记，即时返回（元数据级操作）。
        原请求跑完后后端自会丢弃结果、回滚会话态、消息不入历史——浏览器抓取本身无法凭空打断。"""
        rid = str(body.get("request_id") or "").strip()
        if not rid:
            json_response(self, 400, {"ok": False, "error": "request_id不能为空"})
            return
        with _CANCEL_LOCK:
            _CANCEL_FLAGS[rid] = True
            ev = _CANCEL_EVENTS.get(rid)
            if ev is not None:
                ev.set()
        json_response(self, 200, {"ok": True})

    # ---------- 用户中心（含头像上传：data URI 落盘 resource/，路径入库长期有效） ----------
    def _api_usercenter_post(self, body: dict):
        avatar_data = body.pop("avatar_data", None)
        if isinstance(avatar_data, str) and avatar_data.startswith("data:image"):
            # 限制约 2MB 原图（base64 后 ~2.7M 字符）
            if len(avatar_data) > 2_800_000:
                json_response(self, 400, {"ok": False, "error": "头像图片过大（>2MB），请换一张或压缩后重试"})
                return
            try:
                head, b64 = avatar_data.split(",", 1)
                raw = base64.b64decode(b64)
            except Exception:
                json_response(self, 400, {"ok": False, "error": "头像数据解析失败"})
                return
            if len(raw) < 64:
                json_response(self, 400, {"ok": False, "error": "头像数据无效"})
                return
            ext = "png"
            m = re.search(r"data:image/(png|jpeg|jpg|webp|gif)", head)
            if m:
                ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
            fname = "user_avatar." + ext
            try:
                for old in os.listdir(RESOURCE_DIR):  # 清理旧扩展名头像，避免堆积
                    if old.startswith("user_avatar.") and old != fname:
                        os.remove(os.path.join(RESOURCE_DIR, old))
                with open(os.path.join(RESOURCE_DIR, fname), "wb") as f:
                    f.write(raw)
            except OSError:
                json_response(self, 500, {"ok": False, "error": "头像保存失败"})
                return
            # ?v= 时间戳做缓存穿透：覆盖上传后浏览器立即取新图
            body["avatar"] = f"/resource/{fname}?v={int(time.time())}"
        json_response(self, 200, {"ok": True, "user": user_center.save(body)})

    # ---------- 消息站（操作记录） ----------
    def _api_messages_post(self, body: dict):
        action = str(body.get("action") or "").strip().lower()
        if action == "read_all":
            n = message_center.mark_all_read()
            json_response(self, 200, {"ok": True, "marked": n, "unread": message_center.unread_count()})
            return
        if action == "clear":
            n = message_center.clear()
            json_response(self, 200, {"ok": True, "cleared": n, "unread": 0, "count": 0})
            return
        json_response(self, 400, {"ok": False, "error": "未知 action，仅支持 read_all/clear"})

    # ---------- 回收站（还原由 web 层分发回各业务模块，保证内存实例与磁盘一致） ----------
    def _api_trash_post(self, body: dict):
        action = str(body.get("action") or "").strip().lower()
        tid = str(body.get("id") or "").strip()

        if action == "clear":
            n = trash_bin.clear()
            json_response(self, 200, {"ok": True, "cleared": n, "items": trash_bin.list_items(),
                                      "expire_days": EXPIRE_DAYS})
            return
        if action not in ("restore", "purge"):
            json_response(self, 400, {"ok": False, "error": "未知 action，仅支持 restore/purge/clear"})
            return
        item = trash_bin.find(tid)
        if item is None:
            json_response(self, 404, {"ok": False, "error": "回收站中找不到该条目"})
            return
        ttype = item.get("type")
        data = item.get("data") or {}

        if action == "purge":
            trash_bin.purge(tid)
            json_response(self, 200, {"ok": True, "action": "purge", "items": trash_bin.list_items(),
                                      "expire_days": EXPIRE_DAYS})
            return

        # ---------- restore：按类型分发 ----------
        msg = ""
        try:
            if ttype == "session":
                s = session_store.restore_session(data.get("session") or {})
                if s is None:
                    json_response(self, 400, {"ok": False, "error": "归档数据不完整，无法还原会话"})
                    return
                msg = f"会话「{s['title']}」已还原"
            elif ttype in ("cart_item", "cart_clear"):
                objs = [data["item"]] if ttype == "cart_item" else (data.get("items") or [])
                if not objs:
                    json_response(self, 400, {"ok": False, "error": "归档数据不完整，无法还原"})
                    return
                with SESSION_LOCK:
                    _sid, cs = _chat()
                    for d in objs:
                        cs.cart.add_item(d)
                msg = f"已还原 {len(objs)} 项到虚拟购物车"
            elif ttype in ("list_item", "list_clear_done", "list_clear"):
                objs = [data["item"]] if ttype == "list_item" else (data.get("items") or [])
                if not objs:
                    json_response(self, 400, {"ok": False, "error": "归档数据不完整，无法还原"})
                    return
                for d in objs:
                    shopping_list.add_item(d)
                msg = f"已还原 {len(objs)} 项到购物清单"
            elif ttype == "profile":
                prof = data.get("profile") or {}
                if not prof:
                    json_response(self, 400, {"ok": False, "error": "归档数据不完整，无法还原"})
                    return
                with SESSION_LOCK:
                    _sid, cs = _chat()
                    n = cs.profile.restore_merge(prof)
                msg = f"已还原档案 {n} 项（现有非空内容未被覆盖）"
            elif ttype == "apikey":
                cfg = data.get("config") or {}
                r = ai_client.restore_key_config(str(cfg.get("api_key") or ""),
                                                 str(cfg.get("base_url") or ""),
                                                 str(cfg.get("model") or ""),
                                                 str(cfg.get("provider") or ""))
                if not r.get("ok"):
                    json_response(self, 400, {"ok": False, "error": r.get("message", "还原失败")})
                    return
                msg = "API Key 配置已还原"
            else:
                json_response(self, 400, {"ok": False, "error": f"该类型（{ttype}）不支持还原"})
                return
        except Exception as e:
            json_response(self, 500, {"ok": False, "error": f"还原失败：{e}"})
            return
        trash_bin.remove(tid)
        try:
            message_center.add("trash", f"「{item.get('title')}」已从回收站还原", msg)
        except Exception:
            pass
        json_response(self, 200, {"ok": True, "action": "restore", "message": msg,
                                  "items": trash_bin.list_items(), "expire_days": EXPIRE_DAYS})

    # ---------- 静态资源 ----------
    _RESOURCE_TYPES = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }

    def _serve_resource(self, name: str):
        name = (name or "").split("?", 1)[0]  # 剥离 ?v= 缓存穿透参数
        fname = os.path.basename(name.strip())  # 只取文件名，防目录穿越
        ext = os.path.splitext(fname)[1].lower()
        fp = os.path.join(RESOURCE_DIR, fname)
        if ext not in self._RESOURCE_TYPES or not os.path.isfile(fp):
            json_response(self, 404, {"ok": False, "error": "Not Found", "path": fname})
            return
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except OSError:
            json_response(self, 500, {"ok": False, "error": "读取资源失败", "path": fname})
            return
        self.send_response(200)
        self.send_header("Content-Type", self._RESOURCE_TYPES[ext])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _serve_index(self):
        if not os.path.exists(INDEX_FILE):
            html_response(self, 404, "<h1>404 缺少 index.html</h1>".encode("utf-8"))
            return
        try:
            with open(INDEX_FILE, "rb") as f:
                data = f.read()
        except Exception as e:
            html_response(self, 500, f"<h1>500 读取失败</h1><p>{e}</p>".encode("utf-8"))
            return
        html_response(self, 200, data)


def _port_in_use(host: str, port: int) -> bool:
    s = socket.socket()
    try:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def run(port: int = DEFAULT_PORT, open_browser: bool = True):
    # 端口已有存活服务时拒绝启动：Windows 下 SO_REUSEADDR 允许双绑定，双进程
    # 会对同一份数据 JSON 各自读-改-写、互相覆盖（偏好/会话丢更新的根源）
    if _port_in_use("127.0.0.1", port):
        print("=" * 60)
        print(f"  启动中止：端口 {port} 已有服务在运行（多半是旧进程未退出）。")
        print("  双进程并存会互相覆盖数据，请先关闭旧的运行窗口；")
        print(f"  或执行 netstat -ano | findstr :{port} 查得 PID，")
        print(f"  再 taskkill /F /PID <PID> 结束旧进程后重新启动。")
        print("=" * 60)
        return
    # 启动巡检：所有账户回收站中超 3 天未处理的数据自动彻底删除（记入消息站）
    try:
        swept = sweep_all()
        if swept:
            print(f"[trash] 启动清理：回收站 {swept} 项过期数据已彻底删除")
    except Exception:
        pass
    addr = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(addr, ShoppingHandler)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print("  全自动个人购物AI助手 — 网页版")
    print(f"  本地访问地址：{url}")
    print("  关闭本窗口即可停止服务。")
    print("=" * 60)
    if open_browser:
        # 起服务后异步打开浏览器
        def _open():
            import time
            time.sleep(0.6)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
        httpd.server_close()


# ---------- 启动准备 ----------
# 多账户模式下每账户的默认会话在首个鉴权请求时按需惰性创建（_ensure_default_session），
# 导入期不再触碰任何数据文件。


if __name__ == "__main__":
    port = DEFAULT_PORT
    open_browser = True
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)
        if a in ("--no-open", "-n", "no-open"):
            open_browser = False
    run(port=port, open_browser=open_browser)
