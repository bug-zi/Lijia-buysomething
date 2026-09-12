# -*- coding: utf-8 -*-
"""
虚拟购物车 / 收藏模块（Agent 内部存储）

说明：
- 这是本 Agent 的虚拟购物车，不等于平台官方购物车。
- 持久化到 virtual_cart.json。
- 价格监控：对 monitor=True 的项重新抓价，低于记录价返回提醒。
- 历史最低价字段暂留空字符串（按用户决策：仅当前价对比，不做历史曲线）。
"""

import json
import os
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根（数据文件仍存根目录）
CART_FILE = os.path.join(BASE_DIR, "virtual_cart.json")


@dataclass
class CartItem:
    """购物车单条记录"""
    id: str                     # uuid 唯一标识
    name: str
    platform: str
    url: str
    current_price: float
    added_at: str              # 记录时间（ISO 字符串）
    history_low: str = ""      # 历史最低价（从详情页或第三方比价获取）
    note: str = ""             # 用户备注
    monitor: bool = False      # 价格监控开关
    pid: str = ""              # 商品ID（便于反查）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class VirtualCart:
    """虚拟购物车管理器"""

    def __init__(self, file_path: str = CART_FILE):
        self.file_path = file_path
        self.items: List[CartItem] = self._load()

    # ---------- 持久化 ----------
    def _load(self) -> List[CartItem]:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                items = []
                for d in data:
                    # 兼容旧数据：无 id 字段时自动生成
                    if "id" not in d or not d["id"]:
                        import uuid
                        d["id"] = str(uuid.uuid4())[:8]
                    items.append(CartItem(**d))
                return items
            except Exception:
                return []
        return []

    def _save(self) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump([it.to_dict() for it in self.items], f, ensure_ascii=False, indent=2)

    # ---------- 操作 ----------
    def add(self, name: str, platform: str, url: str, price: float,
            note: str = "", monitor: bool = False, pid: str = "",
            history_low: str = "") -> str:
        """加入购物车；已存在(同名同平台)则更新价格"""
        import time, uuid
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        # 去重：同名同平台视为同一件，更新价格与时间
        for it in self.items:
            if it.name == name and it.platform == platform:
                it.current_price = price
                it.added_at = ts
                if note:
                    it.note = note
                if history_low:
                    it.history_low = history_low
                self._save()
                return f"已更新「{name}」({platform}) 的价格为 ¥{price:.1f}"
        self.items.append(CartItem(
            id=str(uuid.uuid4())[:8],
            name=name, platform=platform, url=url, current_price=price,
            added_at=ts, note=note, monitor=monitor, pid=pid,
            history_low=history_low,
        ))
        self._save()
        return f"已加入购物车：{name}（{platform}）¥{price:.1f}"

    def remove(self, index: int) -> str:
        """移除第 N 项（1-based）"""
        if 1 <= index <= len(self.items):
            it = self.items.pop(index - 1)
            self._save()
            return f"已从购物车移除第{index}项：{it.name}"
        return f"购物车没有第{index}项（当前共{len(self.items)}项）"

    def clear(self) -> str:
        n = len(self.items)
        self.items.clear()
        self._save()
        return f"已清空购物车（共{n}项）"

    def set_note(self, index: int, note: str) -> str:
        if 1 <= index <= len(self.items):
            self.items[index - 1].note = note
            self._save()
            return f"已为第{index}项添加备注：{note}"
        return f"购物车没有第{index}项"

    def toggle_monitor(self, index: int) -> str:
        if 1 <= index <= len(self.items):
            it = self.items[index - 1]
            it.monitor = not it.monitor
            self._save()
            state = "已开启" if it.monitor else "已关闭"
            return f"第{index}项「{it.name}」的价格监控{state}"
        return f"购物车没有第{index}项"

    def monitor_all(self, on: bool = True) -> str:
        for it in self.items:
            it.monitor = on
        self._save()
        return f"已{'全部开启' if on else '全部关闭'} {len(self.items)} 项的价格监控"

    # ---------- 展示 ----------
    def list_text(self) -> str:
        if not self.items:
            return "虚拟购物车为空。推荐后可用「把第N款加入购物车」收藏。"
        lines = ["**我的虚拟购物车**（这是 Agent 内部收藏，不等同于平台官方购物车）",
                 "| # | 商品 | 平台 | 当前价 | 备注 | 监控 | 记录时间 |",
                 "|---|---|---|---|---|---|---|"]
        for i, it in enumerate(self.items, 1):
            mon = "开" if it.monitor else "—"
            note = it.note or "—"
            link = f"[链接]({it.url})" if it.url else "—"
            lines.append(
                f"| {i} | {it.name} | {it.platform} | ¥{it.current_price:.1f} | "
                f"{note} | {mon} | {it.added_at} |"
            )
        lines.append("")
        lines.append("历史最低价：暂不支持历史曲线（按设置仅做当前价对比）。")
        lines.append("指令：`监控第N项` / `取消监控第N项` / `给第N项添加备注：xxx` / `从购物车移除第N项` / `清空购物车`")
        return "\n".join(lines)

    def to_list(self) -> List[Dict[str, Any]]:
        return [it.to_dict() for it in self.items]

    # ---------- 价格监控 ----------
    def check_prices(self) -> List[str]:
        """
        对 monitor=True 的项重新抓取当前价，若低于记录价则生成提醒。
        使用 web_scraper.grab_product_detail 重新抓取单商品详情页（遵守禁止批量搜索规则）。
        返回提醒文案列表。
        """
        alerts: List[str] = []
        targets = [it for it in self.items if it.monitor]
        if not targets:
            return []
        try:
            import web_scraper
        except Exception:
            return ["价格监控需要 web_scraper 模块，当前不可用。"]
        for it in targets:
            if not it.url:
                continue  # 无链接无法重新抓取
            try:
                # 重新抓取单个商品详情页（遵守反爬规则：单链接抓取）
                r = web_scraper.grab_product_detail(it.url, headless=False)
            except Exception:
                continue
            # 被拦截则跳过该项（遵守规则：不编造价格）
            if r.get("block_reason") or r.get("need_human"):
                continue
            new_p = r.get("product")
            if not new_p:
                continue
            if new_p.final_price < it.current_price:
                drop = it.current_price - new_p.final_price
                alerts.append(
                    f"价格提醒：「{it.name}」从 ¥{it.current_price:.1f} "
                    f"降至 ¥{new_p.final_price:.1f}（降价 ¥{drop:.1f}，{it.platform}）"
                )
                # 更新记录价
                it.current_price = new_p.final_price
        if any(alerts):
            self._save()
        return alerts


if __name__ == "__main__":
    c = VirtualCart()
    print(c.add("测试连衣裙", "淘宝/天猫", "https://taobao.com", 238.0, note="备选", monitor=True))
    print(c.list_text())
    print(f"共 {len(c.to_list())} 项")
