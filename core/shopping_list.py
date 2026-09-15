# -*- coding: utf-8 -*-
"""
购物清单 / 购买前需求池模块

说明：
- 存放用户想买但还没开聊的需求（一句话原文，可模糊如「夏天房间太热想凉快点」）。
- 与虚拟购物车的边界：清单 = 推荐前的需求池；购物车 = 推荐后的商品收藏。
- 持久化到项目根 shopping_list.json；模块级单例跨会话共享（区别于每会话独立的 VirtualCart）。
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Dict, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根
LIST_FILE = os.path.join(BASE_DIR, "shopping_list.json")


@dataclass
class ListItem:
    """清单单条记录"""
    id: str          # uuid 前 8 位
    content: str     # 需求原文（自由文本）
    created_at: str  # "YYYY-MM-DD HH:MM:SS"
    done: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ShoppingList:
    """购物清单管理器（购买前需求池）"""

    def __init__(self, file_path: str = LIST_FILE):
        self.file_path = file_path
        self.items: List[ListItem] = self._load()

    def _file(self) -> str:
        """数据文件路径：有当前账户上下文时用 accounts/<用户名>/shopping_list.json，否则项目根"""
        from account_manager import current, data_dir
        u = current()
        return os.path.join(data_dir(), "shopping_list.json") if u else self.file_path

    def _ensure(self) -> None:
        """账户上下文切换后惰性重载（单例跨账户复用同一实例）"""
        f = self._file()
        if f != self.file_path:
            self.file_path = f
            self.items = self._load()

    def _load(self) -> List[ListItem]:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                items = []
                for d in data:
                    if "id" not in d or not d["id"]:
                        d["id"] = str(uuid.uuid4())[:8]
                    items.append(ListItem(**d))
                return items
            except Exception:
                return []
        return []

    def _save(self) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump([it.to_dict() for it in self.items], f, ensure_ascii=False, indent=2)

    def add(self, content: str) -> str:
        """添加需求；与现有待处理条目完全同文则不重复添加"""
        self._ensure()
        content = (content or "").strip()
        if not content:
            return "需求内容不能为空。用法：`记到清单：想买的东西或想解决的问题`"
        for i, it in enumerate(self.items, 1):
            if not it.done and it.content == content:
                return f"清单里已有这条待处理需求（第{i}项）：{content}"
        self.items.append(ListItem(id=str(uuid.uuid4())[:8], content=content,
                                   created_at=time.strftime("%Y-%m-%d %H:%M:%S")))
        self._save()
        return (f"已记到清单（第{len(self.items)}项）：{content}\n"
                "想开始选购时，到侧边栏「购物清单」页点该条的「去推荐」。")

    def remove(self, index: int) -> str:
        self._ensure()
        if 1 <= index <= len(self.items):
            it = self.items.pop(index - 1)
            self._save()
            return f"已从清单移除第{index}项：{it.content}"
        return f"清单没有第{index}项（当前共{len(self.items)}项）"

    def toggle_done(self, index: int) -> str:
        self._ensure()
        if 1 <= index <= len(self.items):
            it = self.items[index - 1]
            it.done = not it.done
            self._save()
            return (f"第{index}项「{it.content}」已标记完成。" if it.done
                    else f"第{index}项「{it.content}」已改回待处理。")
        return f"清单没有第{index}项"

    def clear_done(self) -> str:
        self._ensure()
        n = sum(1 for it in self.items if it.done)
        self.items = [it for it in self.items if not it.done]
        self._save()
        return f"已清空{n}项已完成条目。"

    def clear(self) -> str:
        self._ensure()
        n = len(self.items)
        self.items.clear()
        self._save()
        return f"已清空购物清单（共{n}项）。"

    def pending_count(self) -> int:
        self._ensure()
        return sum(1 for it in self.items if not it.done)

    def list_text(self) -> str:
        self._ensure()
        if not self.items:
            return ("购物清单为空。想到想买的随时记下来：\n"
                    "· 对话里说 `记到清单：xxx`\n"
                    "· 或在侧边栏「购物清单」页添加")
        lines = ["**我的购物清单**（想买/想解决的需求池；清单收藏的是需求，转成对话后商品收藏进购物车）",
                 "| # | 需求 | 状态 | 记录时间 |",
                 "|---|---|---|---|"]
        for i, it in enumerate(self.items, 1):
            state = "已完成" if it.done else "待处理"
            lines.append(f"| {i} | {it.content} | {state} | {it.created_at} |")
        lines.append("")
        lines.append("指令：`记到清单：xxx` / `完成第N项` / `从清单移除第N项`；编号即存储顺序。")
        return "\n".join(lines)

    def to_list(self) -> List[Dict[str, Any]]:
        self._ensure()
        return [it.to_dict() for it in self.items]


# 模块级单例：需求池跨会话共享
shopping_list = ShoppingList()


if __name__ == "__main__":
    # 冒烟：全部用临时文件，不碰真实 shopping_list.json
    tmp = LIST_FILE + ".smoke.tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    s = ShoppingList(file_path=tmp)
    assert s.add("").startswith("需求内容不能为空"), "空内容应被拒绝"
    assert s.add("夏天房间太热想凉快点").startswith("已记到清单"), "添加失败"
    assert s.add("夏天房间太热想凉快点").startswith("清单里已有"), "重复添加未去重"
    assert s.add("降噪耳机 预算500").startswith("已记到清单"), "第二条添加失败"
    assert s.pending_count() == 2, "待处理数应为2"
    assert s.toggle_done(1).startswith("第1项"), "标记完成失败"
    assert s.pending_count() == 1, "标记完成后待处理应为1"
    assert s.toggle_done(1).startswith("第1项"), "取消完成失败"
    assert s.remove(2).startswith("已从清单移除"), "移除失败"
    assert len(s.items) == 1, "移除后应剩1项"
    assert s.clear_done().startswith("已清空0项"), "无已完成时清空应处理0项"
    s.toggle_done(1)
    assert s.clear_done().startswith("已清空1项"), "清空已完成失败"
    assert os.path.exists(tmp), "未落盘"
    s2 = ShoppingList(file_path=tmp)
    assert s2.items == [], "重载后应与清空后一致"
    txt = s2.list_text()
    assert "购物清单为空" in txt, "空态文案缺失"
    s.add("冒烟-带数据空态")
    assert "待处理" in s.list_text() and "指令" in s.list_text(), "列表文案缺状态列或指令行"
    os.remove(tmp)
    print("shopping_list 冒烟通过：增/删/标记/清空/持久化/去重/空态")
