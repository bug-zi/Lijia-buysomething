# -*- coding: utf-8 -*-
"""
下单流程模拟 + 物流跟踪模块
- 下单：严格校验用户明确同意 → 自动填充收货信息 → 生成订单 → 返回订单号
- 物流：按时间推进模拟真实物流节点，支持主动查询与状态推送
"""

import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any

from product_searcher import Product
from profile_module import ProfileManager


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根（数据文件仍存根目录）
ORDERS_FILE = os.path.join(BASE_DIR, "orders.json")


@dataclass
class LogisticsEvent:
    time: str
    status: str           # 已下单/已发货/运输中/派送中/已签收/售后处理中
    detail: str


@dataclass
class Order:
    order_id: str
    create_time: str
    status: str                 # 待支付 / 已支付 / 已发货 / 运输中 / 派送中 / 已签收 / 售后 / 已取消
    product_id: str
    product_name: str
    platform: str
    seller: str
    price: float
    final_price: float
    receiver: str
    phone: str
    address: str
    remark: str = ""
    track_no: str = ""
    carrier: str = ""            # 快递公司
    events: List[LogisticsEvent] = field(default_factory=list)
    pay_time: Optional[str] = None
    cancel_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


class OrderManager:
    """订单与物流管理器"""

    def __init__(self, profile: ProfileManager, orders_file: str = ORDERS_FILE):
        self.profile = profile
        self.orders_file = orders_file
        self.orders: Dict[str, Order] = self._load()

    # ---------- 持久化 ----------
    def _load(self) -> Dict[str, Order]:
        if os.path.exists(self.orders_file):
            try:
                with open(self.orders_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                result = {}
                for oid, data in raw.items():
                    events = [LogisticsEvent(**e) for e in data.get("events", [])]
                    data["events"] = events
                    result[oid] = Order(**data)
                return result
            except Exception:
                return {}
        return {}

    def _save(self) -> None:
        data = {oid: o.to_dict() for oid, o in self.orders.items()}
        with open(self.orders_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ---------- 下单主流程 ----------
    def build_order(self, product: Product, quantity: int = 1, remark: str = "") -> Optional[Order]:
        """创建待确认订单草稿（不保存），用于「确认前预览」"""
        # 收货信息校验
        receiver = self.profile.get("receiver") or ""
        phone = self.profile.get("phone") or ""
        address = self.profile.get("address") or ""
        missing = []
        if not receiver: missing.append("收货人姓名")
        if not phone: missing.append("联系电话")
        if not address: missing.append("收货地址")
        if missing:
            raise ValueError("收货信息不完整，请先补充：" + "、".join(missing)
                             + f"\n（示例：修改 收货人=张三 电话=13800000000 地址=北京市朝阳区XX路XX号）")

        final_price = round(product.final_price * max(1, quantity), 2)
        order_id = "OD" + time.strftime("%Y%m%d") + str(uuid.uuid4().int)[-6:]
        track_no = "SF" if product.ship_days <= 1 else "YT" + str(uuid.uuid4().int)[-10:]
        carrier = "顺丰速运" if product.ship_days <= 1 else "圆通速递"
        order = Order(
            order_id=order_id,
            create_time=time.strftime("%Y-%m-%d %H:%M:%S"),
            status="待支付",
            product_id=product.pid,
            product_name=product.name,
            platform=product.platform,
            seller=product.seller,
            price=product.price,
            final_price=final_price,
            receiver=receiver,
            phone=phone,
            address=address,
            remark=remark,
            track_no=track_no,
            carrier=carrier,
            events=[
                LogisticsEvent(time=time.strftime("%Y-%m-%d %H:%M:%S"), status="已下单",
                               detail=f"您已提交订单，商品：{product.name} × {quantity}"),
            ],
        )
        return order

    def confirm_order(self, order: Order) -> str:
        """
        用户明确同意后，保存订单并返回订单摘要 + 支付风险提示
        【硬性约束】此方法必须在用户明确确认后调用
        """
        if order.order_id in self.orders:
            return f"ℹ️  订单 {order.order_id} 已存在，请勿重复提交"
        order.status = "已支付"
        order.pay_time = time.strftime("%Y-%m-%d %H:%M:%S")
        order.events.append(
            LogisticsEvent(
                time=order.pay_time,
                status="已支付",
                detail=f"支付成功 ¥{order.final_price:.2f}（仅为流程模拟，未真实扣款）",
            )
        )
        self.orders[order.order_id] = order
        self._save()
        return self._format_order_confirm(order)

    def _format_order_confirm(self, o: Order) -> str:
        lines = []
        lines.append("🎉 **下单成功**")
        lines.append(f"- **订单号**：`{o.order_id}`  （请保存，用于查询物流/售后）")
        lines.append(f"- **商品**：{o.product_name}")
        lines.append(f"- **平台/店铺**：{o.platform} · {o.seller}")
        lines.append(f"- **实付金额**：¥{o.final_price:.2f}  （⚠️ 模拟下单，未真实扣款，支付请自行前往官方平台完成）")
        lines.append(f"- **收货信息**：{o.receiver}  {o.phone}  {o.address}")
        lines.append(f"- **快递**：{o.carrier} · 运单号 `{o.track_no}`")
        lines.append(f"- **下单时间**：{o.create_time}")
        lines.append("")
        lines.append("📌 后续：你可以回复「物流 {订单号}」查询物流，或「我的订单」查看全部订单")
        return "\n".join(lines)

    # ---------- 订单查询 ----------
    def list_orders(self) -> str:
        if not self.orders:
            return "ℹ️  暂无订单。使用「买第X款」可发起下单。"
        lines = ["📋 **我的订单**"]
        for oid in sorted(self.orders.keys(), reverse=True):
            o = self.orders[oid]
            lines.append(
                f"- `{o.order_id}`｜{o.status}｜{o.product_name[:18]}…｜¥{o.final_price:.2f}｜{o.create_time}"
            )
        lines.append("\n回复「物流 <订单号>」查看详细物流进度。")
        return "\n".join(lines)

    def get_order(self, order_id: str) -> Optional[Order]:
        # 支持简写，如只给后6位
        if order_id in self.orders:
            return self.orders[order_id]
        for oid, o in self.orders.items():
            if order_id in oid:
                return o
        return None

    # ---------- 物流 ----------
    def query_logistics(self, order_id_or_short: str) -> str:
        order = self.get_order(order_id_or_short)
        if not order:
            return f"⚠️  未找到订单「{order_id_or_short}」，请核对订单号。"
        self._advance_logistics(order)  # 按"虚拟时间"推进物流节点
        lines = [f"🚚 **物流详情**（订单：{order.order_id}）"]
        lines.append(f"- 商品：{order.product_name}")
        lines.append(f"- 当前状态：**{order.status}**")
        lines.append(f"- 快递：{order.carrier} · 运单号 `{order.track_no}`")
        lines.append(f"- 收货：{order.receiver} {order.address}")
        lines.append("")
        lines.append("**物流轨迹：**")
        for evt in reversed(order.events):
            lines.append(f"  [{evt.time}] **{evt.status}** - {evt.detail}")
        return "\n".join(lines)

    def _advance_logistics(self, order: Order) -> None:
        """
        根据下单时间流逝，模拟真实物流节点推进（幂等，同一节点只会添加一次）
        """
        now = time.time()
        try:
            t0 = time.mktime(time.strptime(order.create_time, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            t0 = now
        elapsed_h = (now - t0) / 3600.0
        # 为了演示效果，若刚下单（<10分钟）且物流节点<3，立刻加速到24小时显示完整轨迹
        if elapsed_h < (10 / 60) and len(order.events) < 3:
            elapsed_h = 24.0  # 演示加速

        has_status = {e.status for e in order.events}

        def add_event(status: str, detail: str, hour_offset: float) -> None:
            if status in has_status:
                return
            if elapsed_h < hour_offset:
                return
            t = time.localtime(t0 + hour_offset * 3600)
            order.events.append(
                LogisticsEvent(time=time.strftime("%Y-%m-%d %H:%M:%S", t), status=status, detail=detail)
            )
            order.status = status

        add_event("已发货",
                  f"商家已发货，包裹已交付{order.carrier}揽收（仓库：华东仓）",
                  hour_offset=4)
        add_event("运输中",
                  "包裹已离开【上海转运中心】，正发往目的城市",
                  hour_offset=10)
        add_event("运输中",
                  "包裹已到达【北京分拨中心】，分拣完成，即将派送",
                  hour_offset=20)
        add_event("派送中",
                  f"快递员 李师傅(138****8888) 正在为您派送，请保持电话畅通",
                  hour_offset=26)
        add_event("已签收",
                  "包裹已签收（签收人：本人），如有问题请于7天内联系售后",
                  hour_offset=32)

        # 订单最终状态以events最新一条为准
        if order.events:
            order.status = order.events[-1].status
        self._save()

    # ---------- 售后 ----------
    def apply_after_sale(self, order_id: str, reason: str) -> str:
        order = self.get_order(order_id)
        if not order:
            return f"⚠️  未找到订单「{order_id}」。"
        if order.status in ("售后", "已取消"):
            return f"ℹ️  订单当前已是「{order.status}」状态。"
        self._advance_logistics(order)
        evt = LogisticsEvent(
            time=time.strftime("%Y-%m-%d %H:%M:%S"),
            status="售后处理中",
            detail=f"用户发起售后：{reason}（将在1-3个工作日内处理）",
        )
        order.events.append(evt)
        order.status = "售后"
        self._save()
        return (
            f"✅ **售后申请已提交**\n"
            f"- 订单：{order.order_id}\n"
            f"- 原因：{reason}\n"
            f"- 当前状态：售后处理中（请保留商品/包装完整，配合商家核验）\n"
            f"- 商家预计处理时限：3个工作日，可回复「物流 {order.order_id}」查看进度。"
        )

    def cancel_order(self, order_id: str, reason: str = "") -> str:
        order = self.get_order(order_id)
        if not order:
            return f"⚠️  未找到订单「{order_id}」。"
        if order.status in ("已发货", "运输中", "派送中", "已签收"):
            return f"⚠️  订单已进入物流环节，无法直接取消。可使用「售后 {order.order_id} <原因>」申请退换货。"
        order.status = "已取消"
        order.cancel_reason = reason or "用户取消"
        order.events.append(
            LogisticsEvent(time=time.strftime("%Y-%m-%d %H:%M:%S"), status="已取消",
                           detail="订单已取消：" + order.cancel_reason)
        )
        self._save()
        return f"✅ 订单 `{order.order_id}` 已取消。原因：{order.cancel_reason}"


if __name__ == "__main__":
    from product_searcher import ProductSearcher
    pm = ProfileManager()
    pm.update_batch({"receiver": "测试", "phone": "13800000000", "address": "北京市朝阳区XX路"})
    om = OrderManager(pm)
    s = ProductSearcher()
    p = s.search("连衣裙")[0]
    try:
        o = om.build_order(p)
        print(om.confirm_order(o))
    except ValueError as e:
        print(e)
    # 推进物流查看
    print()
    print(om.query_logistics(list(om.orders.keys())[0]))
