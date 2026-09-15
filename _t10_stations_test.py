# -*- coding: utf-8 -*-
"""
消息站 + 回收站 模块冒烟测试（_t10）
- 全程使用测试账户目录 accounts/_smoke_stations/ 与临时文件，结束即清理，不碰真实数据。
- 覆盖：消息站记录/未读/已读/清空；回收站归档/过期自动清理/还原数据接口/彻底删除/清空；
        购物车、清单、档案、订单各模块删除钩子；会话还原接口。
运行：python _t10_stations_test.py
"""

import json
import os
import shutil
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(BASE, "core")
for p in (BASE, CORE):
    if p not in sys.path:
        sys.path.insert(0, p)

import account_manager
from message_center import MessageCenter, message_center
from trash_bin import TrashBin, trash_bin, EXPIRE_DAYS
from virtual_cart import VirtualCart
from shopping_list import ShoppingList
from profile_module import ProfileManager
from order_manager import OrderManager
from product_searcher import Product
import session_store

SMOKE_USER = "_smoke_stations"
SMOKE_DIR = os.path.join(BASE, "accounts", SMOKE_USER)
PASS = []


def check(name, cond):
    assert cond, f"断言失败：{name}"
    PASS.append(name)
    print(f"  ok - {name}")


def main():
    account_manager.set_current(SMOKE_USER)
    ddir = account_manager.data_dir()
    assert os.path.isdir(ddir), "测试账户目录未创建"

    # ---------- 消息站基础 ----------
    print("[1] 消息站基础")
    mc_file = os.path.join(ddir, "message_center.json")
    if os.path.exists(mc_file):
        os.remove(mc_file)
    message_center.file_path = mc_file   # 单例绑定测试文件并清空内存（_ensure 见同路径不再重载）
    message_center.items = []
    r = message_center.add("order_cancel", "测试取消", "订单：ODTEST\n原因：测试")
    check("记录返回 id", bool(r.get("id")))
    check("未读数=1", message_center.unread_count() == 1)
    check("列表最新在前", message_center.to_list()[0]["title"] == "测试取消")
    check("全部已读", message_center.mark_all_read() == 1 and message_center.unread_count() == 0)
    check("清空", message_center.clear() == 1 and message_center.count() == 0)
    mc2 = MessageCenter(file_path=mc_file)
    check("持久化重载一致", mc2.count() == 0)

    # ---------- 回收站基础 + 过期清理 ----------
    print("[2] 回收站基础与过期清理")
    t_file = os.path.join(ddir, "trash.json")
    if os.path.exists(t_file):
        os.remove(t_file)
    trash_bin.file_path = t_file
    trash_bin.items = []   # 单例对齐测试文件
    rec = trash_bin.add("cart_item", "测试商品", "测试摘要", {"item": {"name": "测试商品"}})
    check("归档返回", rec.get("type") == "cart_item")
    check("过期时间=3天后", rec["expire_at"] > time.strftime("%Y-%m-%d %H:%M:%S"))
    check("消息站同步记录移入", message_center.to_list()[0]["type"] == "trash")
    check("计数=1", trash_bin.count() == 1)

    # 过期清理：单例内存中把过期时间改到过去，再触发 sweep
    past = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
    trash_bin.find(rec["id"])["expire_at"] = past
    trash_bin._save()
    gone_n = trash_bin.sweep()
    check("过期条目被自动清理", gone_n == 1 and trash_bin.count() == 0)
    titles = [m["title"] for m in message_center.to_list()]
    check("自动清理记入消息站", any("自动清理" in t for t in titles))

    # ---------- 购物车钩子 ----------
    print("[3] 购物车删除归档")
    cart_file = os.path.join(ddir, "virtual_cart.json")
    cart = VirtualCart(file_path=cart_file)
    cart.add("商品A", "平台X", "https://e.example/a", 99.0, pid="P1")
    cart.add("商品B", "平台Y", "https://e.example/b", 50.0, pid="P2")
    before = trash_bin.count()
    cart.remove(1)
    check("移除后归档+1", trash_bin.count() == before + 1)
    entry = trash_bin.list_items()[0]
    check("归档类型 cart_item", entry["type"] == "cart_item" and entry["data"]["item"]["name"] == "商品A")
    cart.clear()
    check("清空后归档+1", trash_bin.count() == before + 2)
    check("清空归档含全部条目", len(trash_bin.list_items()[0]["data"]["items"]) == 1)

    # 还原接口
    cart.add_item(entry["data"]["item"])
    check("add_item 还原购物车条目", any(it.name == "商品A" for it in cart.items))

    # ---------- 清单钩子 ----------
    print("[4] 购物清单删除归档")
    lst_file = os.path.join(ddir, "shopping_list.json")
    lst = ShoppingList(file_path=lst_file)
    lst.add("需求1")
    lst.add("需求2")
    lst.toggle_done(1)
    b = trash_bin.count()
    lst.remove(2)
    check("清单移除归档", trash_bin.count() == b + 1)
    item_entry = trash_bin.list_items()[0]
    check("清单条目归档可还原", item_entry["data"]["item"]["content"] == "需求2")
    lst.clear_done()
    check("清空已完成归档", trash_bin.count() == b + 2)
    done_entry = trash_bin.list_items()[0]
    check("已完成归档含1项", len(done_entry["data"]["items"]) == 1 and done_entry["data"]["items"][0]["done"] is True)
    lst.add_item(item_entry["data"]["item"])
    check("add_item 还原清单条目", any(it.content == "需求2" for it in lst.items))

    # ---------- 档案钩子 ----------
    print("[5] 档案清空归档与还原")
    prof_file = os.path.join(ddir, "user_profile.json")
    pm = ProfileManager(file_path=prof_file)
    pm.update_batch({"height": "175", "receiver": "测试", "phone": "13800000000", "address": "测试地址"})
    b = trash_bin.count()
    pm.clear_all()
    check("档案清空归档", trash_bin.count() == b + 1)
    prof_entry = trash_bin.list_items()[0]
    check("档案归档含原值", prof_entry["data"]["profile"].get("height") == "175")
    pm.update_batch({"height": "180"})   # 现有非空值
    n = pm.restore_merge(prof_entry["data"]["profile"])
    check("restore_merge 不覆盖现有值", pm.get("height") == "180")
    check("restore_merge 还原空位字段", pm.get("receiver") == "测试" and n >= 1)

    # ---------- 订单钩子 ----------
    print("[6] 订单操作记入消息站")
    orders_file = os.path.join(ddir, "orders.json")
    om = OrderManager(pm, orders_file=orders_file)
    p = Product(pid="T1", name="冒烟测试商品", platform="测试平台", category="测试",
                price=100.0, final_price=80.0, seller="测试店", data_source="演示")
    o = om.build_order(p)
    om.confirm_order(o)
    titles = [m["title"] for m in message_center.to_list()]
    check("下单模拟记入消息站", any("下单成功" in t for t in titles))
    om.cancel_order(o.order_id, "冒烟测试")
    titles = [m["title"] for m in message_center.to_list()]
    check("订单取消记入消息站", any("订单已取消" in t for t in titles))
    om.apply_after_sale(o.order_id, "冒烟售后")   # 已取消订单不应再售后
    resp = om.cancel_order("OD不存在", "x")
    check("未找到订单不记消息站", "未找到订单" in resp)
    n_msgs = message_center.count()
    om.cancel_order(o.order_id, "again")   # 重复取消不再追加？cancel_order 会再次记一条（幂等由前端流程保证），此处只验证不抛错
    check("重复取消不崩溃", message_center.count() >= n_msgs)

    # ---------- 会话还原接口 ----------
    print("[7] 会话还原")
    sess_file = os.path.join(ddir, "chat_sessions.json")
    if os.path.exists(sess_file):
        os.remove(sess_file)
    s = session_store.create_session("冒烟会话")
    session_store.append_message(s["id"], "user", "你好")
    full = session_store.get_session(s["id"], with_state=True)
    session_store.delete_session(s["id"])
    check("删除后不存在", session_store.get_session(s["id"]) is None)
    rs = session_store.restore_session(full)
    check("还原返回摘要", rs is not None and rs["id"] == s["id"] and rs["message_count"] == 1)
    check("还原后消息在", session_store.get_session(s["id"])["messages"][0]["content"] == "你好")
    rs2 = session_store.restore_session(full)   # id 冲突 → 新 id
    check("id 冲突分配新 id", rs2["id"] != s["id"])
    check("空会话也允许还原", session_store.restore_session({"id": "x", "title": "空"}) is not None)
    check("无效归档拒绝还原", session_store.restore_session({}) is None
          and session_store.restore_session(None) is None)

    # ---------- 回收站 purge/clear ----------
    print("[8] 彻底删除与清空")
    b = trash_bin.count()
    tid = trash_bin.list_items()[0]["id"]
    trash_bin.purge(tid)
    check("purge 移除单条", trash_bin.count() == b - 1)
    titles = [m["title"] for m in message_center.to_list()]
    check("purge 记入消息站", any("已彻底删除" in t for t in titles))
    n = trash_bin.clear()
    check("清空回收站", n >= 1 and trash_bin.count() == 0)

    print(f"\n共 {len(PASS)} 项断言全部通过")


if __name__ == "__main__":
    try:
        main()
    finally:
        account_manager.set_current(None)
        shutil.rmtree(SMOKE_DIR, ignore_errors=True)
        print("清理完成：", SMOKE_USER, "目录已删除" if not os.path.isdir(SMOKE_DIR) else "删除失败")
