# -*- coding: utf-8 -*-
"""
消息站 / 操作记录中心（模块：message_center）

说明：
- 系统操作类信息（订单取消、售后申请、下单模拟、移入回收站、自动清理等）统一记录在此，
  不再挤占对话购物区（2026-09-16 开发者指示：对话购物用于探讨交互购买商品）。
- 持久化到 message_center.json；路径随当前账户上下文（accounts/<用户名>/，无上下文=项目根）。
- 模块级单例跨请求复用，账户上下文切换后惰性重载（与 shopping_list 同模式）。
"""

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根
DEFAULT_FILE = os.path.join(BASE_DIR, "message_center.json")
MAX_KEEP = 500  # 最多保留条数（防膨胀，超出丢最旧）

_LOCK = threading.RLock()  # 单例跨账户复用，_ensure 惰性切换期间须串行，防跨账户串写


class MessageCenter:
    """操作记录中心"""

    def __init__(self, file_path: str = DEFAULT_FILE):
        self.file_path = file_path
        self.items: List[Dict[str, Any]] = self._load()

    # ---------- 持久化 ----------
    def _file(self) -> str:
        from account_manager import current, data_dir
        u = current()
        return os.path.join(data_dir(), "message_center.json") if u else self.file_path

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

    # ---------- 记录 ----------
    def add(self, mtype: str, title: str, detail: str = "") -> Dict[str, Any]:
        """新增一条操作记录；返回该记录 dict"""
        with _LOCK:
            self._ensure()
            rec = {
                "id": str(uuid.uuid4())[:8],
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "type": mtype or "system",
                "title": (title or "").strip() or "（无标题操作）",
                "detail": detail or "",
                "read": False,
            }
            self.items.append(rec)
            if len(self.items) > MAX_KEEP:
                self.items = self.items[-MAX_KEEP:]
            self._save()
            return rec

    # ---------- 查询 / 管理 ----------
    def to_list(self, limit: int = 200) -> List[Dict[str, Any]]:
        """全部记录，最新在前"""
        with _LOCK:
            self._ensure()
            return list(reversed(self.items[-limit:] if limit and limit < len(self.items) else self.items))

    def unread_count(self) -> int:
        with _LOCK:
            self._ensure()
            return sum(1 for it in self.items if not it.get("read"))

    def count(self) -> int:
        with _LOCK:
            self._ensure()
            return len(self.items)

    def mark_all_read(self) -> int:
        with _LOCK:
            self._ensure()
            n = 0
            for it in self.items:
                if not it.get("read"):
                    it["read"] = True
                    n += 1
            if n:
                self._save()
            return n

    def clear(self) -> int:
        """清空全部记录（消息站是日志流水，直接删除，不进回收站）"""
        with _LOCK:
            self._ensure()
            n = len(self.items)
            self.items = []
            self._save()
            return n


# 模块级单例
message_center = MessageCenter()
