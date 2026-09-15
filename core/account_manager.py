# -*- coding: utf-8 -*-
"""
账户管理模块（本地多账户）

- accounts.json（项目根，全局一份）：用户名/邮箱/加盐哈希密码/token；
- 每账户数据目录 DATA_ROOT/<用户名>/，业务模块经 data_file() 按当前账户取路径；
- 当前账户上下文：threading.local，web_server 每请求鉴权后 set_current()；
  跨线程（线程池）用 bound() 显式传播；
- migrate_legacy_into：注册第一个账户时把项目根旧数据文件移入其账户目录（首个注册继承存量数据）。
只用标准库；无账户上下文时路径回落 LEGACY_ROOT（项目根，兼容存量与各模块自测）。
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
DATA_ROOT = os.path.join(BASE_DIR, "accounts")   # 每账户数据目录的父目录
LEGACY_ROOT = BASE_DIR                            # 无账户上下文时的回落目录

# 首个注册账户可继承的存量数据文件白名单（与 spec 第 4 节 8 文件一致）
LEGACY_DATA_FILES = (
    "user_profile.json", "orders.json", "virtual_cart.json", "shopping_list.json",
    "chat_sessions.json", "user_center.json", "api_key.json", "search_history.json",
)

_lock = threading.RLock()
_local = threading.local()

_USERNAME_RE = re.compile(r"^[一-龥A-Za-z0-9_]{2,20}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _hash_password(password: str, salt_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt_hex) + password.encode("utf-8")).hexdigest()


def _read_store(path: str) -> Dict[str, Any]:
    """读账户表；缺失/损坏（备份 .broken）按空表处理"""
    if not os.path.exists(path):
        return {"accounts": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            raise ValueError("bad accounts.json")
        return data
    except Exception:
        try:
            os.replace(path, path + ".broken")
        except OSError:
            pass
        return {"accounts": {}}


def _write_store(path: str, data: Dict[str, Any]) -> None:
    """原子写：临时文件 + os.replace"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def validate_register(username: str, email: str, password: str) -> str:
    """注册校验；返回错误文案，空串=通过"""
    if not isinstance(username, str) or not _USERNAME_RE.match(username.strip()):
        return "用户名需 2-20 位，仅限中英文、数字、下划线，不含空格。"
    if not isinstance(email, str) or not _EMAIL_RE.match(email.strip()):
        return "邮箱格式不正确。"
    if not isinstance(password, str) or len(password) < 4:
        return "密码至少 4 位。"
    return ""


class AccountStore:
    """账户表：注册/登录/登出/token 解析（文件路径可注入，便于自测）"""

    def __init__(self, accounts_file: str = ACCOUNTS_FILE):
        self.accounts_file = accounts_file

    def _read(self) -> Dict[str, Any]:
        return _read_store(self.accounts_file)

    def _write(self, data: Dict[str, Any]) -> None:
        _write_store(self.accounts_file, data)

    def is_empty(self) -> bool:
        with _lock:
            return not self._read()["accounts"]

    def register(self, username: str, email: str, password: str) -> Dict[str, Any]:
        username = (username or "").strip()
        email = (email or "").strip()
        err = validate_register(username, email, password)
        if err:
            return {"ok": False, "error": err}
        with _lock:
            data = self._read()
            accounts = data["accounts"]
            if username in accounts:
                return {"ok": False, "error": "该用户名已被注册。"}
            if any(a.get("email") == email for a in accounts.values()):
                return {"ok": False, "error": "该邮箱已被注册。"}
            salt = os.urandom(16).hex()
            token = uuid.uuid4().hex
            accounts[username] = {
                "username": username, "email": email,
                "salt": salt, "password_hash": _hash_password(password, salt),
                "created_at": _now(), "token": token, "token_time": _now(),
            }
            self._write(data)
            return {"ok": True, "token": token, "username": username}

    def login(self, username: str, password: str) -> Dict[str, Any]:
        username = (username or "").strip()
        with _lock:
            data = self._read()
            a = data["accounts"].get(username)
            if not a or a.get("password_hash") != _hash_password(password or "", a.get("salt", "")):
                return {"ok": False, "error": "用户名或密码错误。"}
            token = uuid.uuid4().hex
            a["token"] = token
            a["token_time"] = _now()
            self._write(data)
            return {"ok": True, "token": token, "username": username}

    def logout(self, token: str) -> bool:
        if not token:
            return False
        with _lock:
            data = self._read()
            for a in data["accounts"].values():
                if a.get("token") == token:
                    a["token"] = ""
                    self._write(data)
                    return True
        return False

    def delete_account(self, username: str, password: str) -> Dict[str, Any]:
        """注销：验密后移除账户条目并删除其数据目录（不可恢复）"""
        username = (username or "").strip()
        with _lock:
            data = self._read()
            a = data["accounts"].get(username)
            if not a:
                return {"ok": False, "error": "账户不存在。"}
            if a.get("password_hash") != _hash_password(password or "", a.get("salt", "")):
                return {"ok": False, "error": "密码不正确。"}
            del data["accounts"][username]
            self._write(data)
            # 目录名净化与 data_dir() 同规则；realpath 收敛在 DATA_ROOT 下才删（防路径意外）
            safe = "".join(c for c in username if c not in '\\/:*?"<>|')
            user_dir = os.path.realpath(os.path.join(DATA_ROOT, safe))
            root = os.path.realpath(DATA_ROOT)
            if user_dir != root and user_dir.startswith(root + os.sep) and os.path.isdir(user_dir):
                shutil.rmtree(user_dir, ignore_errors=True)
            # data_dir() 的"已建目录"缓存同步失效：否则同名账户再注册时目录缺、写文件即 FileNotFoundError
            _made_dirs.discard(os.path.join(DATA_ROOT, safe))
        return {"ok": True}

    def resolve(self, token: str) -> Optional[Dict[str, Any]]:
        """token → 账户公开信息；无效返回 None"""
        if not token:
            return None
        with _lock:
            for a in self._read()["accounts"].values():
                if a.get("token") and a["token"] == token:
                    return {"username": a["username"], "email": a["email"],
                            "created_at": a.get("created_at", "")}
        return None


ACCOUNTS = AccountStore()   # web_server 使用的全局账户表（测试可整体替换）


# ---------- 当前账户上下文 ----------

def set_current(username: Optional[str]) -> None:
    _local.username = username


def current() -> Optional[str]:
    return getattr(_local, "username", None)


_made_dirs: set = set()


def data_dir(username: Optional[str] = None) -> str:
    """账户数据目录（自动确保存在）；无账户（未传且无上下文）回落 LEGACY_ROOT"""
    u = username if username is not None else current()
    if not u:
        return LEGACY_ROOT
    safe = "".join(c for c in u if c not in '\\/:*?"<>|')
    d = os.path.join(DATA_ROOT, safe)
    if d not in _made_dirs:
        os.makedirs(d, exist_ok=True)
        _made_dirs.add(d)
    return d


def data_file(filename: str) -> str:
    """业务模块统一入口：按当前账户解析数据文件路径"""
    return os.path.join(data_dir(), filename)


def bound(fn: Callable) -> Callable:
    """把当前账户上下文绑进将在其他线程执行的 callable（线程池传播）；无上下文时原样返回"""
    acct = current()
    if not acct:
        return fn

    def runner(*args, **kwargs):
        set_current(acct)
        try:
            return fn(*args, **kwargs)
        finally:
            set_current(None)
    return runner


# ---------- 存量数据迁移（首个注册账户继承） ----------

def migrate_legacy_into(username: str, legacy_dir: str = "",
                        dest_dir: str = "") -> List[str]:
    """把项目根旧数据文件原子移动进账户目录；返回实际移动的文件名列表"""
    src_dir = legacy_dir or LEGACY_ROOT
    dst = dest_dir or data_dir(username)
    os.makedirs(dst, exist_ok=True)
    moved: List[str] = []
    for name in LEGACY_DATA_FILES:
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            continue
        try:
            os.replace(src, os.path.join(dst, name))
            moved.append(name)
        except OSError:
            pass
    return moved


if __name__ == "__main__":
    # 冒烟：全部走临时目录，不碰真实 accounts.json / accounts/
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="_acct_selftest_")
    DATA_ROOT = os.path.join(tmpdir, "accounts")     # 自测内重定向数据根（模块级重绑定）
    LEGACY_ROOT = os.path.join(tmpdir, "legacy")
    os.makedirs(LEGACY_ROOT, exist_ok=True)
    store = AccountStore(accounts_file=os.path.join(tmpdir, "accounts.json"))

    # 1) 校验
    assert validate_register("a b", "x@y.com", "1234"), "用户名含空格应拒绝"
    assert validate_register("小明", "bad-mail", "1234"), "邮箱格式错应拒绝"
    assert validate_register("小明", "x@y.com", "123"), "短密码应拒绝"
    assert validate_register("小明", "x@y.com", "1234") == "", "合法注册应通过"

    # 2) 注册 / 重复
    r1 = store.register("小明", "a@x.com", "1234")
    assert r1["ok"] and r1["token"], r1
    assert not store.register("小明", "b@x.com", "1234")["ok"], "重名应拒绝"
    assert not store.register("小红", "a@x.com", "1234")["ok"], "重邮箱应拒绝"

    # 3) token 解析 / 明文不落盘
    assert store.resolve(r1["token"])["username"] == "小明"
    assert store.resolve("bad-token") is None
    raw = open(store.accounts_file, encoding="utf-8").read()
    assert "1234" not in raw, "密码明文泄漏"

    # 4) 登录：错误统一文案；成功换发新 token，旧 token 失效
    assert store.login("小明", "wrong")["error"] == "用户名或密码错误。"
    r2 = store.login("小明", "1234")
    assert r2["ok"] and r2["token"] != r1["token"], "登录应换发 token"
    assert store.resolve(r1["token"]) is None, "旧 token 应失效"

    # 5) 登出
    assert store.logout(r2["token"])
    assert store.resolve(r2["token"]) is None

    # 6) 重启存活：同文件新实例仍能解析 token
    r3 = store.register("小红", "b@x.com", "5678")
    store2 = AccountStore(accounts_file=store.accounts_file)
    assert store2.resolve(r3["token"])["username"] == "小红", "token 应持久存活"

    # 7) 上下文路径
    set_current("小明")
    assert data_file("orders.json") == os.path.join(DATA_ROOT, "小明", "orders.json")
    set_current(None)
    assert data_file("orders.json") == os.path.join(LEGACY_ROOT, "orders.json")

    # 8) 迁移：白名单内移动、缺失跳过
    open(os.path.join(LEGACY_ROOT, "user_profile.json"), "w", encoding="utf-8").write("{}")
    open(os.path.join(LEGACY_ROOT, "api_key.json"), "w", encoding="utf-8").write("{}")
    open(os.path.join(LEGACY_ROOT, "not_in_list.json"), "w", encoding="utf-8").write("{}")
    moved = migrate_legacy_into("小红")
    assert sorted(moved) == ["api_key.json", "user_profile.json"], moved
    assert os.path.isfile(os.path.join(DATA_ROOT, "小红", "user_profile.json"))
    assert not os.path.exists(os.path.join(LEGACY_ROOT, "user_profile.json"))
    assert os.path.isfile(os.path.join(LEGACY_ROOT, "not_in_list.json")), "白名单外不应移动"

    # 9) 线程传播 bound
    set_current("小明")
    wrapped = bound(lambda: data_file("x.json"))
    set_current(None)
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=1) as ex:
        got = ex.submit(wrapped).result()
    assert got == os.path.join(DATA_ROOT, "小明", "x.json"), "bound 应在线程内恢复上下文"

    # 10) 注销：密码错误不删；密码正确条目+目录都消失、旧 token 失效；目录缺失仅删条目
    rd = store.register("注销用户", "del@x.com", "pass1234")
    assert rd["ok"], rd
    udir = os.path.join(DATA_ROOT, "注销用户")
    os.makedirs(udir, exist_ok=True)
    open(os.path.join(udir, "user_center.json"), "w", encoding="utf-8").write("{}")
    assert not store.delete_account("注销用户", "wrong")["ok"], "密码错误应拒绝注销"
    assert "注销用户" in store._read()["accounts"], "密码错误不应移除条目"
    assert os.path.isfile(os.path.join(udir, "user_center.json")), "密码错误不应删目录"
    assert store.delete_account("注销用户", "pass1234")["ok"], "正确密码应注销成功"
    assert "注销用户" not in store._read()["accounts"], "注销后条目应移除"
    assert not os.path.exists(udir), "注销后数据目录应删除"
    assert store.resolve(rd["token"]) is None, "注销后旧 token 应失效"
    rn = store.register("无目录用户", "nodir@x.com", "1234")
    assert rn["ok"] and store.delete_account("无目录用户", "1234")["ok"], "数据目录缺失时也应注销成功"

    print("account_manager 冒烟通过：校验/注册/登录/登出/token持久化/上下文路径/迁移/线程传播/注销")
