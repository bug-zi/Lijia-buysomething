# -*- coding: utf-8 -*-
"""多账户机制集成自测：内存起服务于 8901（不动 8765），临时数据根（不碰真实
accounts.json / accounts/ / 根目录数据文件）；不触发抓取与 LLM 连通探测。"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "core"))

import account_manager  # noqa: E402
import ai_client  # noqa: E402

TMP = tempfile.mkdtemp(prefix="_t9_acct_")
account_manager.ACCOUNTS = account_manager.AccountStore(os.path.join(TMP, "accounts.json"))
account_manager.DATA_ROOT = os.path.join(TMP, "accounts")
account_manager.LEGACY_ROOT = os.path.join(TMP, "legacy")
os.makedirs(account_manager.LEGACY_ROOT, exist_ok=True)
# 预置旧数据：验证「首个注册账户继承存量数据」
with open(os.path.join(account_manager.LEGACY_ROOT, "virtual_cart.json"), "w", encoding="utf-8") as f:
    json.dump([{"id": "t1", "name": "旧购物车商品", "platform": "演示", "url": "",
                "current_price": 9.9, "added_at": "2026-09-15 10:00:00"}], f, ensure_ascii=False)

import web_server  # noqa: E402

PORT = 8901


def api(path, method="GET", body=None, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}" + path, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Auth-Token", token)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        with urllib.request.urlopen(req, data=data, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


threading.Thread(target=lambda: web_server.run(port=PORT, open_browser=False), daemon=True).start()
for _ in range(80):
    try:
        api("/api/health")
        break
    except Exception:
        time.sleep(0.1)
else:
    raise SystemExit("服务未就绪")

# 1) 未登录一律 401；匿名 health 只给基础信息
assert api("/api/profile")[0] == 401, "未登录应 401"
assert api("/api/cart")[0] == 401
st, h0 = api("/api/health")
assert st == 200 and "cart_count" not in h0, f"匿名 health 应无账户数据：{h0}"

# 2) 注册校验
st, r = api("/api/auth/register", "POST", {"username": "a b", "email": "x@y.com", "password": "1234"})
assert st == 200 and not r["ok"], "非法用户名应被拒绝"
st, r = api("/api/auth/register", "POST", {"username": "小明", "email": "bad", "password": "1234"})
assert not r["ok"], "非法邮箱应被拒绝"

# 3) 首个注册账户继承存量数据
st, ra = api("/api/auth/register", "POST", {"username": "alice", "email": "a@x.com", "password": "1234"})
assert ra["ok"], ra
ta = ra["token"]
ad = os.path.join(account_manager.DATA_ROOT, "alice")
assert os.path.isfile(os.path.join(ad, "virtual_cart.json")), "旧购物车应迁入 alice"
assert not os.path.isfile(os.path.join(account_manager.LEGACY_ROOT, "virtual_cart.json")), "旧文件应已移走"

# 4) A 的数据只在 A
st, _ = api("/api/cart", "POST", {"action": "add",
            "product": {"name": "A的测试商品", "platform": "演示", "url": "", "current_price": 1.0}}, token=ta)
assert st == 200, "加购应成功"
st, cart_a = api("/api/cart", token=ta)
assert len(cart_a["cart"]) == 2, f"alice 应见 2 项（迁移1+新增1），实际 {len(cart_a['cart'])}"
st, me = api("/api/auth/me", token=ta)
assert me["user"]["username"] == "alice"

# 5) B 注册后从空白开始，看不到 A 的数据
st, rb = api("/api/auth/register", "POST", {"username": "bob", "email": "b@x.com", "password": "5678"})
assert rb["ok"], rb
tb = rb["token"]
st, cart_b = api("/api/cart", token=tb)
assert len(cart_b["cart"]) == 0, "bob 应为空购物车"
st, hb = api("/api/health", token=tb)
assert hb["cart_count"] == 0, "bob health 计数应为 0"
assert len(api("/api/cart", token=ta)[1]["cart"]) == 2, "A 数据不受 B 影响"

# 6) 登出 / 登录 / token 互不影响
api("/api/auth/logout", "POST", {}, token=ta)
assert api("/api/cart", token=ta)[0] == 401, "登出后旧 token 失效"
assert api("/api/cart", token=tb)[0] == 200, "B 的 token 不受影响"
st, rl = api("/api/auth/login", "POST", {"username": "alice", "password": "wrong"})
assert not rl["ok"] and rl["error"] == "用户名或密码错误。"
st, rl = api("/api/auth/login", "POST", {"username": "alice", "password": "1234"})
assert rl["ok"]
assert len(api("/api/cart", token=rl["token"])[1]["cart"]) == 2, "重登后数据还在（持久化隔离）"

# 7) API Key 路径按账户隔离（进程级断言，不触发网络）
account_manager.set_current("alice")
kf_a = ai_client._key_file()
account_manager.set_current("bob")
kf_b = ai_client._key_file()
account_manager.set_current(None)
assert kf_a != kf_b and "alice" in kf_a and "bob" in kf_b, "API Key 文件应按账户分路径"

print("_t9 多账户集成自测通过：401/注册校验/首账户迁移/双账户隔离/登出重登/Key路径隔离")
