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
    GET  /                    托管 index.html
"""

import os
import sys
import json
import threading
import webbrowser
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 保证导入模块：脚本自身路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from shopping_agent import ChatSession  # noqa: E402
from recommender import Recommender  # noqa: E402  会话工厂重建推荐器用
import ai_client  # noqa: E402  安全配置 & LLM 调用（key永远不回传前端）
from product_searcher import save_scrape_cache  # noqa: E402  Trae 浏览器桥接缓存写入
import config_store  # noqa: E402  配置库：网站登录态管理
import session_store  # noqa: E402  会话持久化（chat_sessions.json）

DEFAULT_PORT = 8765
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

# ---------- 多会话注册表 ----------
# 每个会话一个 ChatSession 实例（7 个会话态字段互相独立，随聊天持久化到 chat_sessions.json）；
# profile/orders/cart 各模块自带 JSON 持久化，属全局共享——所有会话实例共用同一组管理器，保持原单会话语义。
# ACTIVE_SESSION_ID：当前激活会话（老前端/不带 session_id 的请求都落到它）。
SESSION_LOCK = threading.Lock()          # 全局粗锁：沿用原单会话的并发模型
SESSIONS: Dict[str, ChatSession] = {}    # 内存中的会话实例（按需懒加载/恢复）
ACTIVE_SESSION_ID: Optional[str] = None  # 启动时从 chat_sessions.json 恢复

# 全局共享的管理器宿主：各会话实例只借用它的 profile/orders/cart（三模块自带 JSON 持久化）
_SHARED = ChatSession()


def _new_chat_session() -> ChatSession:
    """会话工厂：新 ChatSession 挂上全局共享的档案/订单/购物车管理器，并重建推荐器绑定"""
    cs = ChatSession()
    cs.profile = _SHARED.profile
    cs.orders = _SHARED.orders
    cs.cart = _SHARED.cart
    cs.recommender = Recommender(cs.profile, cs.searcher)
    return cs


def _ensure_default_session() -> str:
    """保证存在激活会话（须持有 SESSION_LOCK 调用）：无则新建「默认会话」"""
    global ACTIVE_SESSION_ID
    lst = session_store.list_sessions()
    if lst:
        ids = {s["id"] for s in lst}
        if ACTIVE_SESSION_ID not in ids:
            ACTIVE_SESSION_ID = lst[0]["id"]
            session_store.set_active_id(ACTIVE_SESSION_ID)
        return ACTIVE_SESSION_ID
    s = session_store.create_session("默认会话")
    ACTIVE_SESSION_ID = s["id"]
    return s["id"]


def _chat(session_id: str = "") -> Tuple[str, ChatSession]:
    """取指定/激活会话（须持有 SESSION_LOCK 调用）。
    内存没有则新建并 restore_state；会话不存在（已被删）回退默认会话。返回 (会话id, 实例)"""
    sid = (session_id or "").strip() or ACTIVE_SESSION_ID or ""
    if sid and session_store.get_session(sid) is None:
        sid = ""
    if not sid:
        sid = _ensure_default_session()
    cs = SESSIONS.get(sid)
    if cs is None:
        cs = _new_chat_session()
        meta = session_store.get_session(sid, with_state=True) or {}
        if meta.get("state"):
            cs.restore_state(meta["state"])
        SESSIONS[sid] = cs
    return sid, cs


def json_response(handler, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
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

    # ---------- 基础 ----------
    def log_message(self, format, *args):
        """安静模式：默认打印会刷屏"""
        return

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
        if path == "/api/health":
            with SESSION_LOCK:
                _sid, cs = _chat()
                snap = cs.snapshot()
            json_response(self, 200, {
                "ok": True, "name": "ShoppingAgent Web", "version": "1.0",
                "cart_count": snap.get("cart_count", 0),
                "data_source_blocked": snap.get("data_source_blocked", False),
            })
            return
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
        if path == "/api/cart":
            self._api_cart_get()
            return
        if path.startswith("/api/logistics/"):
            oid = unquote(path[len("/api/logistics/"):])
            self._api_logistics_get(oid)
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

        if path == "/api/chat":
            self._api_chat(body)
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
        if path == "/api/cart":
            self._api_cart_post(body)
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
    def _api_chat(self, body: dict):
        msg = str(body.get("message") or "").strip()
        if not msg:
            json_response(self, 400, {"ok": False, "error": "message不能为空"})
            return
        req_sid = str(body.get("session_id") or "").strip()
        with SESSION_LOCK:
            sid, cs = _chat(req_sid)
            # 自动标题：会话首条消息取用户输入前 20 字（仅当还是默认标题）
            meta = session_store.get_session(sid)
            if meta is not None and not meta.get("messages") and \
                    (meta.get("title") or "") in ("", "新会话", "默认会话"):
                session_store.rename_session(sid, msg[:20])
            reply = cs.chat(msg)
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
            # 持久化：用户消息、AI 回复、会话状态（重启不丢）
            session_store.append_message(sid, "user", msg)
            session_store.append_message(sid, "ai", reply)
            session_store.save_state(sid, cs.export_state())
        json_response(self, 200, {
            "ok": True,
            "session_id": sid,
            "reply": reply,
            "snapshot": snap,
            "pending_order": pending_preview,
        })

    # ---------- 多会话管理 ----------
    def _api_sessions_get(self):
        with SESSION_LOCK:
            active = ACTIVE_SESSION_ID
        json_response(self, 200, {"ok": True, "sessions": session_store.list_sessions(),
                                  "active_id": active})

    def _api_sessions_post(self, body: dict):
        global ACTIVE_SESSION_ID
        title = str(body.get("title") or "").strip() or "新会话"
        with SESSION_LOCK:
            s = session_store.create_session(title)
            ACTIVE_SESSION_ID = s["id"]
            SESSIONS[s["id"]] = _new_chat_session()   # 预建空实例
        json_response(self, 200, {"ok": True, "session": s, "active_id": s["id"],
                                  "sessions": session_store.list_sessions()})

    def _api_session_detail(self, sid: str):
        meta = session_store.get_session(sid)
        if meta is None:
            json_response(self, 404, {"ok": False, "error": "会话不存在"})
            return
        with SESSION_LOCK:
            meta["active_id"] = ACTIVE_SESSION_ID
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
        global ACTIVE_SESSION_ID
        with SESSION_LOCK:
            if session_store.get_session(sid) is None:
                json_response(self, 404, {"ok": False, "error": "会话不存在"})
                return
            ACTIVE_SESSION_ID = sid
            session_store.set_active_id(sid)
            _chat(sid)   # 确保该会话已加载并 restore_state
        json_response(self, 200, {"ok": True, "active_id": sid})

    def _api_session_delete(self, sid: str):
        global ACTIVE_SESSION_ID
        with SESSION_LOCK:
            if not session_store.delete_session(sid):
                json_response(self, 404, {"ok": False, "error": "会话不存在"})
                return
            SESSIONS.pop(sid, None)   # 释放内存实例
            # 删除的是激活会话时，session_store 已切到最近一个；这里同步并保证有可用会话
            ACTIVE_SESSION_ID = session_store.get_active_id()
            if not ACTIVE_SESSION_ID:
                ACTIVE_SESSION_ID = _ensure_default_session()
            else:
                _chat(ACTIVE_SESSION_ID)   # 预加载新激活会话
        json_response(self, 200, {"ok": True, "active_id": ACTIVE_SESSION_ID,
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
                        resp = f"⚠️  未识别为档案指令：{payload}"
                else:
                    if "档案录入 (" in cmd_resp and "第 " in cmd_resp:
                        cs._collecting_profile = True
                    resp = cmd_resp
            else:
                resp = "⚠️  无效请求"
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
                        r = {"ok": True, "message": f"✅ 已配置 {cfg.get('provider') or cfg.get('base_url')}，模型 {cfg.get('model')}", "config": cfg}
                    else:
                        r = {"ok": False, "message": "⚠️ 当前未配置API Key，请先填写并保存。", "config": cfg}
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
            _sid, cs = _chat()
            items = cs.cart.to_list()
            text = cs.cart.list_text()
            alerts = cs.cart.check_prices()  # 顺便触发监控检查
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
            "message": f"✅ 已缓存 {len(clean)} 条真实商品（关键词：{keyword}，30 分钟有效）",
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
        with SESSION_LOCK:
            sid, cs = _chat(req_sid)
            user_text = text or "（图片消息）"
            reply = cs.chat_with_images(clean_images, text)
            snap = cs.snapshot()
            # 持久化：图片本体不入库（体积大），只存文字与回复
            session_store.append_message(sid, "user", user_text)
            session_store.append_message(sid, "ai", reply)
            session_store.save_state(sid, cs.export_state())
        json_response(self, 200, {"ok": True, "session_id": sid, "reply": reply, "snapshot": snap})

    # ---------- 静态资源 ----------
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


def run(port: int = DEFAULT_PORT, open_browser: bool = True):
    addr = ("127.0.0.1", port)
    httpd = ThreadingHTTPServer(addr, ShoppingHandler)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print("  🛒 全自动个人购物AI助手 — 网页版")
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
        print("\n👋 服务已停止。")
        httpd.server_close()


# ---------- 启动准备（import 时执行，不启动服务）----------
# 从 chat_sessions.json 恢复激活会话；无会话则自动建「默认会话」，保证老前端/无参数请求兼容。
try:
    with SESSION_LOCK:
        _ensure_default_session()
except Exception as _e:
    print(f"⚠️ 会话存储初始化失败（不影响服务启动）：{_e}")


if __name__ == "__main__":
    port = DEFAULT_PORT
    open_browser = True
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)
        if a in ("--no-open", "-n", "no-open"):
            open_browser = False
    run(port=port, open_browser=open_browser)
