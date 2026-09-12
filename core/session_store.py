# -*- coding: utf-8 -*-
"""
会话持久化存储（模块：session_store）
- 管理 chat_sessions.json（存项目根目录，文件名固定）
- 结构：{"sessions": {id: {"id","title","created_at","updated_at","messages":[...],"state":{...}}}, "active_id": ...}
- 线程安全（threading.Lock）+ 原子写（临时文件 + os.replace），UTF-8
- 只提供本范围 API，不做额外抽象
"""

import copy
import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根（数据文件仍存根目录）
SESSIONS_FILE = os.path.join(BASE_DIR, "chat_sessions.json")

_LOCK = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _new_id() -> str:
    """会话 id：uuid4 短格式，稳定且适合 URL"""
    return uuid.uuid4().hex[:12]


def _empty() -> Dict[str, Any]:
    return {"sessions": {}, "active_id": None}


def _read() -> Dict[str, Any]:
    """读取整个存储（文件缺失/损坏时返回空结构；损坏文件备份为 .broken）"""
    if not os.path.exists(SESSIONS_FILE):
        return _empty()
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _empty()
        data.setdefault("sessions", {})
        data.setdefault("active_id", None)
        if not isinstance(data["sessions"], dict):
            data["sessions"] = {}
        return data
    except Exception:
        try:
            os.replace(SESSIONS_FILE, SESSIONS_FILE + ".broken")
        except Exception:
            pass
        return _empty()


def _write(data: Dict[str, Any]) -> None:
    """原子写：先写临时文件再 os.replace，避免写一半损坏"""
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSIONS_FILE)


def _summary(s: Dict[str, Any]) -> Dict[str, Any]:
    """会话摘要（列表用，不含消息体）"""
    return {
        "id": s.get("id") or "",
        "title": s.get("title") or "",
        "created_at": s.get("created_at") or "",
        "updated_at": s.get("updated_at") or "",
        "message_count": len(s.get("messages") or []),
    }


# ---------- 对外 API ----------

def list_sessions() -> List[Dict[str, Any]]:
    """全部会话摘要，按 updated_at 倒序"""
    with _LOCK:
        data = _read()
        items = [_summary(s) for s in data["sessions"].values()]
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return items


def create_session(title: str = "新会话") -> Dict[str, Any]:
    """新建会话并设为激活，返回摘要"""
    with _LOCK:
        data = _read()
        sid = _new_id()
        now = _now()
        data["sessions"][sid] = {
            "id": sid,
            "title": (title or "新会话").strip() or "新会话",
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "state": {},
        }
        data["active_id"] = sid
        _write(data)
        return _summary(data["sessions"][sid])


def get_session(sid: str, with_state: bool = False) -> Optional[Dict[str, Any]]:
    """取单个会话；with_state=True 时附带持久化的会话状态（返回副本，改它不影响存储）"""
    with _LOCK:
        data = _read()
        s = data["sessions"].get(sid)
        if not s:
            return None
        out = {
            "id": s.get("id"),
            "title": s.get("title") or "",
            "created_at": s.get("created_at") or "",
            "updated_at": s.get("updated_at") or "",
            "messages": [dict(m) for m in (s.get("messages") or [])],
        }
        if with_state:
            st = s.get("state")
            out["state"] = copy.deepcopy(st) if st else {}
        return out


def delete_session(sid: str) -> bool:
    """删除会话；若删的是激活会话自动切到最近更新的一个（无会话时 active_id=None）"""
    with _LOCK:
        data = _read()
        if sid not in data["sessions"]:
            return False
        del data["sessions"][sid]
        if data.get("active_id") == sid:
            rest = sorted(data["sessions"].values(),
                          key=lambda s: s.get("updated_at") or "", reverse=True)
            data["active_id"] = rest[0]["id"] if rest else None
        _write(data)
        return True


def rename_session(sid: str, title: str) -> bool:
    with _LOCK:
        data = _read()
        s = data["sessions"].get(sid)
        if not s:
            return False
        s["title"] = (title or "").strip() or s.get("title") or "新会话"
        s["updated_at"] = _now()
        _write(data)
        return True


def append_message(sid: str, role: str, content: str, ts: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """追加一条消息并 touch；返回该消息（会话不存在返回 None）"""
    with _LOCK:
        data = _read()
        s = data["sessions"].get(sid)
        if not s:
            return None
        msg = {"role": role, "content": content, "time": ts or _now()}
        s.setdefault("messages", []).append(msg)
        s["updated_at"] = msg["time"]
        _write(data)
        return dict(msg)


def save_state(sid: str, state: Dict[str, Any]) -> bool:
    """保存会话上下文状态（Agent 7 个会话态字段）并 touch"""
    with _LOCK:
        data = _read()
        s = data["sessions"].get(sid)
        if not s:
            return False
        s["state"] = state if isinstance(state, dict) else {}
        s["updated_at"] = _now()
        _write(data)
        return True


def touch(sid: str) -> bool:
    """仅刷新 updated_at（用于排序）"""
    with _LOCK:
        data = _read()
        s = data["sessions"].get(sid)
        if not s:
            return False
        s["updated_at"] = _now()
        _write(data)
        return True


def get_active_id() -> Optional[str]:
    with _LOCK:
        return _read().get("active_id")


def set_active_id(sid: str) -> bool:
    with _LOCK:
        data = _read()
        if sid not in data["sessions"]:
            return False
        data["active_id"] = sid
        _write(data)
        return True
