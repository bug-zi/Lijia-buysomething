# -*- coding: utf-8 -*-
"""
回收站（模块：trash_bin）

说明：
- 删除的业务数据先归档于此，不立即彻底删除；超过 3 天未处理自动彻底删除（2026-09-16 开发者指示）。
- 覆盖：会话 / 购物车条目 / 购物清单条目 / 个人档案 / API Key 配置。
  不覆盖：网站登录态清除（浏览器 Cookie 属登录凭据，清除即重扫，无法可靠快照还原）、
  注销账号（账户目录整体删除，回收站随目录一同消亡）。
- 还原动作由 web_server 分发回各业务模块（保证内存实例与磁盘一致）；本模块只做存储与过期清理。
- 持久化 trash.json；路径随当前账户上下文（accounts/<用户名>/，无上下文=项目根）。
"""

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根
DEFAULT_FILE = os.path.join(BASE_DIR, "trash.json")

EXPIRE_DAYS = 3
EXPIRE_SECONDS = EXPIRE_DAYS * 86400

_LOCK = threading.RLock()  # 单例跨账户复用，_ensure 惰性切换期间须串行，防跨账户串写

TYPE_LABELS = {
    "session": "会话",
    "cart_item": "购物车条目",
    "cart_clear": "虚拟购物车（清空）",
    "list_item": "购物清单条目",
    "list_clear_done": "购物清单已完成条目",
    "list_clear": "购物清单（清空）",
    "profile": "个人购物偏好档案",
    "apikey": "API Key 配置",
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_left(seconds: float) -> str:
    if seconds <= 0:
        return "已过期"
    h = int(seconds // 3600)
    if h >= 24:
        return f"约 {h // 24} 天"
    if h >= 1:
        return f"约 {h} 小时"
    return "不足 1 小时"


class TrashBin:
    """回收站存储"""

    def __init__(self, file_path: str = DEFAULT_FILE):
        self.file_path = file_path
        self.items: List[Dict[str, Any]] = self._load()

    # ---------- 持久化 ----------
    def _file(self) -> str:
        from account_manager import current, data_dir
        u = current()
        return os.path.join(data_dir(), "trash.json") if u else self.file_path

    def _ensure(self) -> None:
        f = self._file()
        if f != self.file_path:
            self.file_path = f
            self.items = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
            except Exception:
                return []
        return []

    def _save(self) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=2)

    # ---------- 归档 ----------
    def add(self, ttype: str, title: str, summary: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """归档一条被删数据；同时记入消息站。返回该回收条目。"""
        with _LOCK:
            self._ensure()
            rec = {
                "id": str(uuid.uuid4())[:8],
                "type": ttype if ttype in TYPE_LABELS else "system",
                "title": (title or "").strip() or "（未命名）",
                "summary": summary or "",
                "data": data or {},
                "deleted_at": _now(),
                "expire_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + EXPIRE_SECONDS)),
            }
            self.items.append(rec)
            self._save()
        try:
            from message_center import message_center
            message_center.add(
                "trash", f"「{rec['title']}」已移入回收站",
                f"类型：{TYPE_LABELS.get(rec['type'], rec['type'])}\n"
                f"{rec['summary']}\n"
                f"保留至 {rec['expire_at']}（超 {EXPIRE_DAYS} 天未处理将自动彻底删除）；"
                f"可随时到「回收站」还原。")
        except Exception:
            pass
        return rec

    # ---------- 查询 / 过期清理 ----------
    def _purge_expired_locked(self) -> List[Dict[str, Any]]:
        """清理已过期条目（须先 _ensure 且持有 _LOCK）；返回被清理条目"""
        now = time.time()
        keep, gone = [], []
        for it in self.items:
            try:
                expire_ts = time.mktime(time.strptime(it.get("expire_at") or "", "%Y-%m-%d %H:%M:%S"))
            except Exception:
                expire_ts = now + EXPIRE_SECONDS
            (gone if expire_ts <= now else keep).append(it)
        if gone:
            self.items = keep
            self._save()
        return gone

    def _notify_auto_purge(self, gone: List[Dict[str, Any]]) -> None:
        try:
            from message_center import message_center
            names = "、".join(f"「{g.get('title')}」" for g in gone[:5])
            more = f" 等 {len(gone)} 项" if len(gone) > 5 else ""
            message_center.add(
                "trash_auto", f"回收站自动清理 {len(gone)} 项",
                f"超过 {EXPIRE_DAYS} 天未处理，已自动彻底删除：{names}{more}。")
        except Exception:
            pass

    def list_items(self, notify: bool = True) -> List[Dict[str, Any]]:
        """当前回收站条目（读取时惰性清理过期项；有清理时记入消息站），最新在前。
        注意：方法名不可叫 items —— 与实例属性 self.items（数据列表）同名会互相遮蔽。"""
        with _LOCK:
            self._ensure()
            gone = self._purge_expired_locked()
        if gone:
            self._notify_auto_purge(gone)
        return list(reversed(self.items))

    def sweep(self) -> int:
        """清理过期条目（启动巡检用），返回清理数量"""
        with _LOCK:
            self._ensure()
            gone = self._purge_expired_locked()
        if gone:
            self._notify_auto_purge(gone)
        return len(gone)

    def count(self) -> int:
        with _LOCK:
            self._ensure()
            self._purge_expired_locked()
            return len(self.items)

    def find(self, tid: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            self._ensure()
            for it in self.items:
                if it.get("id") == tid:
                    return it
        return None

    # ---------- 处理 ----------
    def remove(self, tid: str) -> Optional[Dict[str, Any]]:
        """移出回收站（还原成功后调用；不记消息），返回被移出条目"""
        with _LOCK:
            self._ensure()
            it = self.find(tid)
            if it is None:
                return None
            self.items.remove(it)
            self._save()
            return it

    def purge(self, tid: str) -> Optional[Dict[str, Any]]:
        """彻底删除单条并记入消息站"""
        it = self.remove(tid)
        if it is None:
            return None
        try:
            from message_center import message_center
            message_center.add("trash_auto", f"「{it.get('title')}」已彻底删除",
                               f"你在回收站中手动彻底删除了该{TYPE_LABELS.get(it.get('type'), '数据')}，数据已不可恢复。")
        except Exception:
            pass
        return it

    def clear(self) -> int:
        """清空回收站并记入消息站"""
        with _LOCK:
            self._ensure()
            n = len(self.items)
            if n:
                self.items = []
                self._save()
        if n:
            try:
                from message_center import message_center
                message_center.add("trash_auto", f"回收站已清空（{n} 项）",
                                   f"你手动清空了回收站，{n} 项数据已彻底删除、不可恢复。")
            except Exception:
                pass
        return n


# 模块级单例
trash_bin = TrashBin()


def sweep_all() -> int:
    """服务启动时全账户清理过期回收站（遍历 accounts/*，直接切上下文复用单例逻辑）"""
    from account_manager import current, set_current, DATA_ROOT
    total = 0
    try:
        targets = []
        if os.path.isdir(DATA_ROOT):
            targets = [n for n in os.listdir(DATA_ROOT) if os.path.isdir(os.path.join(DATA_ROOT, n))]
        targets.append("")  # 项目根（无账户遗留数据）
        prev = current()
        try:
            for name in targets:
                set_current(name or None)
                try:
                    total += trash_bin.sweep()
                except Exception:
                    continue
        finally:
            set_current(prev)
    except Exception:
        pass
    return total
