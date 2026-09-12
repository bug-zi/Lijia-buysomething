# -*- coding: utf-8 -*-
"""
配置库 · 网站登录态管理（状态缓存 / 手动检测 / 扫码登录 / 按域清除）

- 登录凭据（Cookie）始终由 web_scraper 的持久化配置目录 .browser_profile 管理；
  本模块只记录「状态元数据」（login_state.json），绝不存储任何 Cookie 内容。
- 检测/登录均用真实有头浏览器（复用 _launch_browser）；遇验证码/登录墙交还用户，
  不绕过任何平台风控；程序绝不过手账号密码（扫码登录只打开登录页等用户手动完成）。
- 本模块顶层不得导入 web_scraper（会被 web_scraper 顶层导入，构成循环）；
  浏览器相关函数体内惰性导入。
"""

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根（数据文件仍存根目录）
LOGIN_STATE_FILE = os.path.join(BASE_DIR, "login_state.json")

PLATFORMS: List[str] = ["京东", "淘宝/天猫"]

# 检测页与清理域（键名须与 web_scraper.LOGIN_URLS 平台名一致）
CHECK_URLS: Dict[str, str] = {
    "京东": "https://order.jd.com/center/list.action",
    "淘宝/天猫": "https://www.taobao.com/",
}
CLEAR_DOMAINS: Dict[str, List[str]] = {
    "京东": ["jd.com"],
    "淘宝/天猫": ["taobao.com", "tmall.com"],
}

# 可重入锁：_expire_stale_jobs 会在持锁状态下经 record_state 再入本锁（同一线程），故用 RLock
_lock = threading.RLock()
# 内存任务表：platform -> {"state": "检测中|登录中|清理中", "started_at": epoch}
_jobs: Dict[str, Dict[str, Any]] = {}

# 任务硬超时（自愈）：超过时长视为任务线程已异常滞留，get_states 自动释放并如实标注。
# 上限须大于最坏耗时：登录=启动重试(≤90s)+跳转(≤30s)+等扫码(180s)≈300s，故取 330；
# 检测/清理=启动重试(≤90s)+跳转/清理≈120s，故取 150。
_JOB_TTL: Dict[str, int] = {"登录中": 330, "检测中": 150, "清理中": 150}


def _short_err(e: Exception, limit: int = 160) -> str:
    """压缩 Playwright 等抛出的多行原始错误，只保留首行要点"""
    first = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
    return first[:limit]


def _profile_busy_hint() -> str:
    return ("（.browser_profile 同一时刻只能开一个浏览器：请先关闭残留的登录/检测窗口，"
            "或等正在进行的抓取/扫码结束后再试）")


def _now_str(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def _read_states() -> Dict[str, Dict[str, Any]]:
    """读取状态缓存文件（调用方需已持有 _lock，或接受自测场景的瞬时读）"""
    try:
        with open(LOGIN_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _expire_stale_jobs() -> None:
    """超时任务自愈释放（调用方需已持有 _lock）"""
    now = time.time()
    for p, j in list(_jobs.items()):
        ttl = _JOB_TTL.get(j.get("state", ""), 330)
        if now - j.get("started_at", now) > ttl:
            record_state(p, None, "任务超时", note=f"任务「{j.get('state', '')}」超过 {ttl}s 未结束，已自动释放（如实标注，非编造）")
            _jobs.pop(p, None)


def _busy_response() -> Dict[str, Any]:
    """全局互斥应答：profile 同时只允许一个浏览器任务"""
    items = [f"{p}·{j.get('state', '')}" for p, j in _jobs.items()]
    return {"ok": False,
            "message": "已有浏览器任务正在进行（" + "、".join(items) + "）" + _profile_busy_hint()}


def record_state(platform: str, status: Optional[str], source: str, note: str = "") -> None:
    """写入状态；status=None 表示保留原状态、仅更新来源与备注"""
    with _lock:
        data = _read_states()
        old = data.get(platform) or {}
        data[platform] = {
            "status": status if status is not None else old.get("status", "未检测"),
            "checked_at": _now_str(),
            "source": source,
            "note": note,
        }
        try:
            with open(LOGIN_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass


def get_states() -> List[Dict[str, Any]]:
    """给前端展示：缓存状态 + 合并进行中任务（任务态优先；超时任务先自愈释放）"""
    with _lock:
        _expire_stale_jobs()
        cached = _read_states()
        states = []
        for p in PLATFORMS:
            job = _jobs.get(p)
            if job:
                states.append({
                    "platform": p, "status": job["state"],
                    "checked_at": _now_str(job.get("started_at")),
                    "source": "任务进行中", "note": "",
                })
            else:
                st = cached.get(p) or {}
                states.append({
                    "platform": p,
                    "status": st.get("status", "未检测"),
                    "checked_at": st.get("checked_at", ""),
                    "source": st.get("source", ""),
                    "note": st.get("note", ""),
                })
        return states


def _set_job(platform: str, state: str) -> None:
    with _lock:
        _jobs[platform] = {"state": state, "started_at": time.time()}


def _clear_job(platform: str) -> None:
    with _lock:
        _jobs.pop(platform, None)


def _close_browser(pw, context) -> None:
    try:
        if context is not None:
            context.close()
    except Exception:
        pass
    try:
        if pw is not None:
            pw.stop()
    except Exception:
        pass


def _detect_logged_in(platform: str, page) -> bool:
    """平台登录判定：与 _check_block 同思路（URL 域名/特征词），不绕风控"""
    low = (page.url or "").lower()
    if platform == "京东":
        return "passport" not in low and "login" not in low
    # 淘宝/天猫：unb cookie（用户编号）存在 且 页面无「亲，请登录」→ 已登录
    has_unb = False
    try:
        cookies = page.context.cookies("https://www.taobao.com")
        has_unb = any(c.get("name") == "unb" for c in cookies)
    except Exception:
        pass
    need_login_text = False
    try:
        body = page.inner_text("body")[:2000]
        need_login_text = "亲，请登录" in body
    except Exception:
        pass
    return has_unb and not need_login_text


def check_login(platform: str) -> Dict[str, Any]:
    """同步检测登录态：真实有头浏览器短暂打开检测页，判定后写缓存并关闭（≤25s）"""
    if platform not in CHECK_URLS:
        return {"ok": False, "message": f"暂不支持{platform}的登录态检测（如实告知）"}
    with _lock:
        _expire_stale_jobs()
        if _jobs:
            return _busy_response()
        _jobs[platform] = {"state": "检测中", "started_at": time.time()}
    try:
        try:
            from web_scraper import _launch_browser, _random_delay  # 惰性导入，避免循环依赖
        except Exception as e:
            record_state(platform, None, "检测", note=f"Playwright 未安装，无法检测：{e}")
            return {"ok": False, "message": "Playwright 未安装，无法检测（如实告知，不影响其他功能）"}
        pw = context = None
        try:
            pw, context, page = _launch_browser()   # 真实有头浏览器（红线）
            loaded = True
            try:
                page.goto(CHECK_URLS[platform], wait_until="domcontentloaded", timeout=20000)
            except Exception:
                loaded = False   # 加载失败多为风控静默拦截：不判定，如实告知
            _random_delay(1.0, 2.0)
            if not loaded:
                record_state(platform, None, "检测", note="检测页加载失败（疑似风控拦截），本次不判定")
                return {"ok": False, "message": f"{platform} 检测页加载失败，本次不判定（请稍后重试）"}
            status = "已登录" if _detect_logged_in(platform, page) else "未登录"
            record_state(platform, status, "手动检测")
            return {"ok": True, "status": status, "message": f"{platform}：{status}"}
        except Exception as e:
            note = f"检测失败：{_short_err(e)} {_profile_busy_hint()}"
            record_state(platform, None, "检测", note=note)
            return {"ok": False, "message": note}
        finally:
            _close_browser(pw, context)
    finally:
        _clear_job(platform)


def start_manual_login(platform: str) -> Dict[str, Any]:
    """弹出有头浏览器跳登录页等用户人工扫码（程序绝不过手凭据）；立即返回，前端轮询"""
    if platform not in CHECK_URLS:   # 平台范围与 LOGIN_URLS 一致
        return {"ok": False, "message": f"暂不支持{platform}的网页端登录（如实告知）"}
    with _lock:
        _expire_stale_jobs()
        if _jobs:
            return _busy_response()
        _jobs[platform] = {"state": "登录中", "started_at": time.time()}
    threading.Thread(target=_manual_login_worker, args=(platform,), daemon=True).start()
    return {"ok": True, "running": True,
            "message": f"已弹出 {platform} 登录窗口，请在浏览器里扫码/手动登录（最长等待 3 分钟）"}


def _manual_login_worker(platform: str) -> None:
    pw = context = None
    try:
        from web_scraper import _launch_browser, _goto_login_and_wait  # 惰性导入
        pw, context, page = _launch_browser(minimized=False)   # 扫码登录必须弹窗可见
        try:
            page.bring_to_front()   # 登录窗口置前，避免用户找不到待扫码的窗口
        except Exception:
            pass
        ok = _goto_login_and_wait(page, platform, max_wait_s=180)
        if ok:
            record_state(platform, "已登录", "扫码登录")
        else:
            record_state(platform, None, "扫码登录",
                         note="本次登录未完成（超时/窗口被关闭/平台验证未通过——如遇滑块或验证码请一并完成后重试）")
    except Exception as e:
        record_state(platform, None, "扫码登录", note=f"登录任务异常：{_short_err(e)} {_profile_busy_hint()}")
    finally:
        _clear_job(platform)
        _close_browser(pw, context)


def clear_login(platform: str) -> Dict[str, Any]:
    """按平台域清除本项目 profile 内的 Cookie（不影响其他平台）；状态置回「未检测」"""
    if platform not in CLEAR_DOMAINS:
        return {"ok": False, "message": f"暂不支持{platform}的登录态清理（如实告知）"}
    with _lock:
        _expire_stale_jobs()
        if _jobs:
            return _busy_response()
        _jobs[platform] = {"state": "清理中", "started_at": time.time()}
    try:
        try:
            from web_scraper import _launch_browser  # 惰性导入
        except Exception as e:
            return {"ok": False, "message": f"Playwright 未安装，无法清理：{e}"}
        pw = context = None
        try:
            pw, context, page = _launch_browser()
            cookies = context.cookies()
            domains = CLEAR_DOMAINS[platform]
            victims = [c for c in cookies
                       if any((c.get("domain") or "").lstrip(".").endswith(d) for d in domains)]
            degraded = False
            for c in victims:
                try:
                    context.clear_cookies(name=c.get("name"), domain=c.get("domain"),
                                          path=c.get("path") or "/")
                except TypeError:
                    # 旧版 Playwright 不支持按 name/domain/path 过滤：如实降级为全清
                    context.clear_cookies()
                    degraded = True
                    break
            if degraded:
                record_state(platform, "未检测", "清除登录态",
                             note="旧版 Playwright 不支持按域过滤，已清除全部站点登录态（如实告知）")
                return {"ok": True, "message": "已清除全部站点登录态（当前 Playwright 版本不支持按域过滤，如实告知）"}
            record_state(platform, "未检测", "清除登录态",
                         note=f"已清除 {len(victims)} 条 Cookie（仅 {platform} 相关域）")
            return {"ok": True, "message": f"已清除 {platform} 登录态（{len(victims)} 条 Cookie），重新检测将显示未登录"}
        except Exception as e:
            note = f"清理失败：{_short_err(e)} {_profile_busy_hint()}"
            record_state(platform, None, "清除登录态", note=note)
            return {"ok": False, "message": note}
        finally:
            _close_browser(pw, context)
    finally:
        _clear_job(platform)


if __name__ == "__main__":
    # 函数级自测（无需浏览器）：缓存读写 / 状态合并 / 任务互斥 / 无敏感字段
    import tempfile
    LOGIN_STATE_FILE = os.path.join(tempfile.gettempdir(), "_login_state_selftest.json")
    try:
        os.remove(LOGIN_STATE_FILE)
    except OSError:
        pass

    # 1) 初始状态：全部「未检测」
    sts = {s["platform"]: s["status"] for s in get_states()}
    assert sts == {"京东": "未检测", "淘宝/天猫": "未检测"}, sts

    # 2) 写入与保留原状态
    record_state("京东", "已登录", "自测")
    record_state("淘宝/天猫", None, "自测", note="仅更新备注")
    sts = {s["platform"]: (s["status"], s["source"], s["note"]) for s in get_states()}
    assert sts["京东"] == ("已登录", "自测", ""), sts["京东"]
    assert sts["淘宝/天猫"][0] == "未检测" and sts["淘宝/天猫"][2] == "仅更新备注", sts["淘宝/天猫"]

    # 3) 进行中任务合并、互斥与超时自愈
    _set_job("淘宝/天猫", "登录中")
    busy = [s["status"] for s in get_states()]
    assert busy == ["已登录", "登录中"], busy
    r = start_manual_login("淘宝/天猫")   # 同平台互斥
    assert r["ok"] is False, r
    r = start_manual_login("京东")        # 全局互斥：跨平台也拒绝（profile 单实例）
    assert r["ok"] is False, r
    r = start_manual_login("拼多多")      # 不支持平台
    assert r["ok"] is False, r
    # 超时自愈：回拨 started_at 超过 TTL 后 get_states 应释放任务并如实标注
    with _lock:
        _jobs["淘宝/天猫"]["started_at"] -= (_JOB_TTL["登录中"] + 1)
    healed = {s["platform"]: s for s in get_states()}
    assert healed["淘宝/天猫"]["status"] != "登录中", healed["淘宝/天猫"]
    assert "超时" in (healed["淘宝/天猫"]["source"] + healed["淘宝/天猫"]["note"]), healed["淘宝/天猫"]
    with _lock:
        assert "淘宝/天猫" not in _jobs   # 自愈后任务表已释放（入口再次发起不再被互斥拒绝）
    _clear_job("淘宝/天猫")

    # 4) 缓存文件不得含任何敏感字段
    with open(LOGIN_STATE_FILE, "r", encoding="utf-8") as f:
        raw = f.read().lower()
    for bad in ("cookie", "token", "password", "pt_key", "unb"):
        assert bad not in raw, f"敏感字段泄漏：{bad}"

    os.remove(LOGIN_STATE_FILE)
    print("config_store 自测通过（缓存读写 / 状态合并 / 任务互斥 / 无敏感字段）")
