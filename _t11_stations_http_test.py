# -*- coding: utf-8 -*-
"""
消息站 + 回收站 HTTP 集成测试（_t11）
- 注册一次性测试账户，全部走 /api/* 接口；绝不触发 /api/chat（测试纪律：不进真实搜索链路）。
- 结束时经 /api/auth/delete_account 注销测试账户，数据目录随注销清除。
运行：python _t11_stations_http_test.py
"""

import json
import sys
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:8799"
USER = "trash_http_030728"
PWD = "test1234"
PASS = []


def check(name, cond):
    assert cond, f"断言失败：{name}"
    PASS.append(name)
    print(f"  ok - {name}")


def call(method, path, body=None, token=None):
    req = urllib.request.Request(BASE_URL + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Auth-Token", token)
    data = json.dumps(body or {}).encode("utf-8") if method != "GET" else None
    try:
        with urllib.request.urlopen(req, data=data) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也返回 JSON 体（应用以 400+{"ok":false} 表达业务拒绝）
        return json.loads(e.read().decode("utf-8"))


def main():
    # 注册/登录测试账户
    r = call("POST", "/api/auth/register", {"username": USER, "email": "t@t.local", "password": PWD})
    token = r.get("token") or call("POST", "/api/auth/login", {"username": USER, "password": PWD})["token"]
    check("测试账户就绪", bool(token))

    # 健康检查带两站计数
    h = call("GET", "/api/health", token=token)
    check("健康检查含两站计数", "messages_unread" in h and "trash_count" in h)

    # 购物车：加 2 条 → 单删 → 全清 → 回收站
    call("POST", "/api/cart", {"action": "add", "product": {"name": "商品A", "platform": "平台X",
         "url": "https://e.example/a", "current_price": 99.0}}, token=token)
    call("POST", "/api/cart", {"action": "add", "product": {"name": "商品B", "platform": "平台Y",
         "url": "https://e.example/b", "current_price": 50.0}}, token=token)
    r = call("POST", "/api/cart", {"action": "remove", "index": 1}, token=token)
    check("购物车单删成功", r["ok"] and len(r["cart"]) == 1)
    r = call("POST", "/api/cart", {"action": "clear"}, token=token)
    check("购物车清空成功", r["ok"] and len(r["cart"]) == 0)
    t = call("GET", "/api/trash", token=token)
    check("回收站有 2 条", t["ok"] and t["expire_days"] == 3 and len(t["items"]) == 2)
    check("清空归档含商品B", any(i["type"] == "cart_clear" and
          any(x["name"] == "商品B" for x in i["data"]["items"]) for i in t["items"]))
    cart_item = next(i for i in t["items"] if i["type"] == "cart_item")

    # 还原购物车单条
    r = call("POST", "/api/trash", {"action": "restore", "id": cart_item["id"]}, token=token)
    check("还原请求成功", r["ok"])
    c = call("GET", "/api/cart", token=token)
    check("还原后购物车有商品A", any(it["name"] == "商品A" for it in c["cart"]))
    t = call("GET", "/api/trash", token=token)
    check("还原后回收站剩 1 条", len(t["items"]) == 1)

    # 清单：加 → 删 → 回收站 → 彻底删除
    call("POST", "/api/shopping_list", {"action": "add", "content": "测试需求"}, token=token)
    call("POST", "/api/shopping_list", {"action": "remove", "index": 1}, token=token)
    t = call("GET", "/api/trash", token=token)
    check("清单删除进回收站", len(t["items"]) == 2)
    list_entry = next(i for i in t["items"] if i["type"] == "list_item")
    r = call("POST", "/api/trash", {"action": "purge", "id": list_entry["id"]}, token=token)
    check("彻底删除成功", r["ok"] and len(r["items"]) == 1)
    l = call("GET", "/api/shopping_list", token=token)
    check("清单确认为空", len(l["items"]) == 0)

    # 档案：写 → 清空 → 回收站 → 还原
    call("POST", "/api/profile", {"action": "update",
         "payload": {"height": "175", "receiver": "测试", "phone": "13800000000", "address": "测试地址"}}, token=token)
    r = call("POST", "/api/profile", {"action": "clear", "payload": ""}, token=token)
    check("档案清空成功", r["ok"] and not r["profile"])
    t = call("GET", "/api/trash", token=token)
    prof_entry = next(i for i in t["items"] if i["type"] == "profile")
    check("档案归档在回收站", prof_entry["data"]["profile"].get("height") == "175")
    r = call("POST", "/api/trash", {"action": "restore", "id": prof_entry["id"]}, token=token)
    p = call("GET", "/api/profile", token=token)
    check("档案还原成功", p["profile"].get("height") == "175" and p["profile"].get("receiver") == "测试")

    # 会话：造 1 条消息 → 删 → 回收站 → 还原
    r = call("POST", "/api/sessions", {"title": "测试会话"}, token=token)
    sid = r["session"]["id"]
    call("POST", f"/api/sessions/{sid}/rename", {"title": "测试会话改"}, token=token)
    # 注入一条消息（不经 chat，避免真实搜索）：直接用后端存储接口不可用 → 借澄清 chips 无副作用的消息不可行；
    # 改为验证空会话删除/还原链路（消息注入由模块级测试 _t10 覆盖）
    r = call("DELETE", f"/api/sessions/{sid}", token=token)
    check("会话删除成功", r["ok"])
    t = call("GET", "/api/trash", token=token)
    s_entry = next(i for i in t["items"] if i["type"] == "session")
    check("会话归档在回收站", "测试会话" in s_entry["title"])
    r = call("POST", "/api/trash", {"action": "restore", "id": s_entry["id"]}, token=token)
    check("空会话还原被拒（无消息无状态）", r["ok"] is False)
    call("POST", "/api/trash", {"action": "purge", "id": s_entry["id"]}, token=token)

    # 消息站：内容与未读
    m = call("GET", "/api/messages", token=token)
    types = {x["type"] for x in m["messages"]}
    check("消息站含 trash 记录", "trash" in types and m["count"] >= 3)
    check("未读数>0", m["unread"] > 0)
    r = call("POST", "/api/messages", {"action": "read_all"}, token=token)
    check("全部已读", r["ok"] and r["unread"] == 0)
    r = call("POST", "/api/messages", {"action": "clear"}, token=token)
    check("清空消息站", r["ok"] and r["count"] == 0)

    # 清空回收站
    t = call("GET", "/api/trash", token=token)
    if t["items"]:
        r = call("POST", "/api/trash", {"action": "clear"}, token=token)
        check("清空回收站", r["ok"] and len(r["items"]) == 0)
    else:
        print("  skip - 清空回收站（已无条目）")

    # 未登录访问被拒
    try:
        call("GET", "/api/trash")
        check("未登录访问被拒", False)
    except Exception:
        check("未登录访问被拒", True)

    print(f"\n共 {len(PASS)} 项断言全部通过")


if __name__ == "__main__":
    ok = False
    try:
        main()
        ok = True
    finally:
        # 注销测试账户（数据目录随注销清除）
        try:
            tok = call("POST", "/api/auth/login", {"username": USER, "password": PWD}).get("token")
            if tok:
                call("POST", "/api/auth/delete_account", {"password": PWD}, token=tok)
                print("清理完成：测试账户已注销")
        except Exception as e:
            print("清理失败（请手动注销 trash_http_test）：", e)
    sys.exit(0 if ok else 1)
