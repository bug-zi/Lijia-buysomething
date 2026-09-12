# -*- coding: utf-8 -*-
"""
用户中心 · 账户信息与界面/行为偏好（本地 JSON 存储）

- 只存展示信息与偏好（昵称/头像色/字号/字体/浏览器弹出方式），绝不含任何账号凭据；
- browser_popup 偏好供 web_scraper 读取：background = 浏览器最小化启动（不抢焦点，
  收进任务栏；登录/扫码等需人工交互的环节由调用方强制弹出窗口）；
- 本模块只用标准库，顶层不导入任何项目模块（无循环依赖）；所有值白名单校验。
"""

import json
import os
import random
import string
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根（数据文件仍存根目录）
UC_FILE = os.path.join(BASE_DIR, "user_center.json")

AVATAR_COLORS = ("classic", "sakura", "forest", "sunset", "slate")
FONT_SIZES = ("small", "medium", "large", "xlarge")
FONT_FAMILIES = ("default", "song", "kai")
POPUP_MODES = ("popup", "background")
PERSONAS = ("default", "student", "office", "senior")

DEFAULTS = {
    "nickname": "",
    "user_id": "",
    "avatar_color": "classic",
    "prefs": {"font_size": "medium", "font_family": "default",
              "browser_popup": "popup", "persona": "default"},
}

# RLock：save() 持锁状态下要再调 load()（其内部也会拿锁）
_lock = threading.RLock()


def _load_raw() -> dict:
    try:
        with open(UC_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_raw(data: dict) -> None:
    try:
        with open(UC_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _norm_prefs(p) -> dict:
    """补全为完整合法偏好（未知键/非法值一律回落默认）"""
    p = p if isinstance(p, dict) else {}
    return {
        "font_size": p.get("font_size") if p.get("font_size") in FONT_SIZES else "medium",
        "font_family": p.get("font_family") if p.get("font_family") in FONT_FAMILIES else "default",
        "browser_popup": p.get("browser_popup") if p.get("browser_popup") in POPUP_MODES else "popup",
        "persona": p.get("persona") if p.get("persona") in PERSONAS else "default",
    }


def _gen_uid() -> str:
    return "SA-" + "".join(random.choices(string.digits, k=8))


def load() -> dict:
    """读取用户中心数据（缺省补默认；user_id 首次访问自动生成并落盘）"""
    with _lock:
        data = _load_raw()
        merged = {
            "nickname": str(data.get("nickname") or "").strip()[:20],
            "user_id": str(data.get("user_id") or "").strip() or _gen_uid(),
            "avatar_color": data.get("avatar_color") if data.get("avatar_color") in AVATAR_COLORS else DEFAULTS["avatar_color"],
            "prefs": _norm_prefs(data.get("prefs")),
        }
        if not str(data.get("user_id") or "").strip():
            _save_raw(merged)
        return merged


def save(patch: dict) -> dict:
    """合并保存（只认白名单键，非法值不采纳）；返回保存后的完整数据"""
    with _lock:
        cur = load()
        if "nickname" in patch:
            cur["nickname"] = str(patch.get("nickname") or "").strip()[:20]
        if patch.get("avatar_color") in AVATAR_COLORS:
            cur["avatar_color"] = patch["avatar_color"]
        prefs = patch.get("prefs")
        if isinstance(prefs, dict):
            for key, allowed in (("font_size", FONT_SIZES),
                                 ("font_family", FONT_FAMILIES),
                                 ("browser_popup", POPUP_MODES),
                                 ("persona", PERSONAS)):
                if prefs.get(key) in allowed:
                    cur["prefs"][key] = prefs[key]
        _save_raw(cur)
        return cur


def get_prefs() -> dict:
    """给其他模块读偏好（如 web_scraper 决定浏览器是否最小化启动）"""
    return load()["prefs"]


if __name__ == "__main__":
    # 函数级自测：默认值 / 白名单校验 / 部分更新 / uid 稳定 / 无敏感字段
    import tempfile
    UC_FILE = os.path.join(tempfile.gettempdir(), "_uc_selftest.json")
    try:
        os.remove(UC_FILE)
    except OSError:
        pass

    u = load()
    assert u["nickname"] == "" and u["avatar_color"] == "classic", u
    assert u["prefs"] == {"font_size": "medium", "font_family": "default",
                          "browser_popup": "popup", "persona": "default"}, u
    uid1 = u["user_id"]
    assert uid1.startswith("SA-") and len(uid1) == 11, uid1

    u2 = save({"nickname": "  小明  ", "avatar_color": "hacker", "action": "x",
               "prefs": {"font_size": "large", "browser_popup": "背景执行", "persona": "student"}})
    assert u2["nickname"] == "小明", u2                      # 昵称去空白
    assert u2["avatar_color"] == "classic", u2              # 非法头像色回落
    assert u2["prefs"]["font_size"] == "large", u2          # 合法偏好更新
    assert u2["prefs"]["browser_popup"] == "popup", u2      # 非法枚举不采纳
    assert u2["prefs"]["persona"] == "student", u2          # 合法人群更新
    assert u2["user_id"] == uid1, "uid 应保持稳定"

    u3 = save({"prefs": {"font_size": "small"}})            # 部分更新不丢其他键
    assert u3["prefs"]["font_size"] == "small", u3
    assert u3["prefs"]["font_family"] == "default", u3
    assert u3["nickname"] == "小明", u3

    assert get_prefs()["font_size"] == "small"
    with open(UC_FILE, "r", encoding="utf-8") as f:
        raw = f.read().lower()
    for bad in ("cookie", "token", "password"):
        assert bad not in raw, f"敏感字段泄漏：{bad}"

    os.remove(UC_FILE)
    print("✅ user_center 自测通过（默认值/白名单/部分更新/uid 稳定/无敏感字段）")
