# -*- coding: utf-8 -*-
"""
LLM 客户端封装：负责安全加载 API Key，调用 chat.completions，
提供 3 个高层能力：
    1) parse_shopping_request()   自然语言购物需求 → 结构化 Request
    2) summarize_reviews()        好评+差评文本 → 提炼「好评点」和「真实短板」
    3) polish_recommendation()    推荐对象列表 → 润色为更像人类的推荐评论文本 + 档案适配点

设计原则：
    * 零第三方依赖（只用标准库 urllib / json）
    * 全链路回退：Key 缺失 / 网络异常 / 模型报错 / 返回格式异常 → 返回 None，
      上层用原来的规则引擎兜底，绝不影响使用。
    * 安全：Key 绝不在日志/异常信息里全量打印，只在错误里保留前后各4字符做识别。
    * 多服务商兼容：自动探测 bigmodel.cn / deepseek / qwen / siliconflow / kimi
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import threading
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ============== 基础路径与配置存储 ==============
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根（数据文件仍存根目录）
KEY_FILE  = os.path.join(BASE_DIR, "api_key.json")

ENV_API_KEY   = "SHOPPING_LLM_API_KEY"
ENV_BASE_URL  = "SHOPPING_LLM_BASE_URL"
ENV_MODEL     = "SHOPPING_LLM_MODEL"

DEFAULT_PROVIDER_HINTS: List[Dict[str, Any]] = [
    # (display, base_url, model_for_chat, model_for_probe)
    # 服务商探测时会按序尝试，优先匹配已知格式key（检测通过后会固化写入KEY_FILE）
    {"name": "智谱 bigmodel.cn", "base": "https://open.bigmodel.cn/api/paas/v4",      "chat": "glm-4-flash",     "probe": "glm-4-flash"},
    {"name": "DeepSeek",         "base": "https://api.deepseek.com/v1",                "chat": "deepseek-chat",   "probe": "deepseek-chat"},
    {"name": "通义千问",         "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "chat": "qwen-turbo", "probe": "qwen-turbo"},
    {"name": "硅基流动",         "base": "https://api.siliconflow.cn/v1",              "chat": "Qwen/Qwen2.5-7B-Instruct", "probe": "Qwen/Qwen2.5-7B-Instruct"},
    {"name": "豆包 火山方舟",    "base": "https://ark.cn-beijing.volces.com/api/v3",   "chat": "doubao-1-5-pro-32k-250115", "probe": "doubao-1-5-lite-32k-250115"},
    {"name": "Kimi 月之暗面",    "base": "https://api.moonshot.cn/v1",                 "chat": "moonshot-v1-8k",  "probe": "moonshot-v1-8k"},
]


def mask_key(k: str) -> str:
    """只保留前后4位，中间打码，用于错误日志"""
    if not k: return ""
    if len(k) <= 8: return "*" * len(k)
    return k[:4] + "****" + k[-4:]


# ============== emoji 净化（红线：AI 消息任何地方不得出现 emoji 图标） ==============
# 覆盖（unicode 转义书写，源码保持零 emoji 字符）：
#   U+1F000-1FAFF 象形符号；U+2139 信息符；U+2600-27BF 杂项符号与丁巴特；U+2B00-2BFF 箭头星形；
#   U+2300-23FF 计时符号；U+FE0E/U+FE0F 变体选择符；U+200D 零宽连接符；U+20E3 按键帽组合符
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2139\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0E\uFE0F\u200D\u20E3]+[ \t]*"
)

def strip_emoji(text: str) -> str:
    """移除文本中的 emoji 图标（含紧随空格）。LLM 返回文本的统一出口过滤，
    防止模型自发的 emoji 混进 AI 消息；规则引擎模板本身已不写 emoji。"""
    if not text:
        return text or ""
    return _EMOJI_RE.sub("", text)


def _safe_read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _safe_write_json(path: str, data: Dict[str, Any]) -> bool:
    try:
        # Windows 下 chmod 不保证，但至少以独占写避免污染
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# ============== Key 管理（单例） ==============
@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    provider: str = ""  # 展示名
    # 统计
    success: int = 0
    fails: int = 0
    last_error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def masked_key(self) -> str:
        return mask_key(self.api_key)


_cfg_lock = threading.RLock()
_CFG: LLMConfig = LLMConfig()


def _load_from_sources_into(cfg: LLMConfig, key_file: str) -> None:
    """按优先级加载进指定配置对象：环境变量 > .env 文件 > key_file"""
    api_key = os.getenv(ENV_API_KEY, "").strip()
    base_url = os.getenv(ENV_BASE_URL, "").strip()
    model = os.getenv(ENV_MODEL, "").strip()

    # .env 文件（兼容，只是读取不写入）
    env_path = os.path.join(BASE_DIR, ".env")
    if not api_key and os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line: continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if not api_key and k == ENV_API_KEY:   api_key = v
                    if not base_url and k == ENV_BASE_URL: base_url = v
                    if not model and k == ENV_MODEL:       model = v
        except OSError:
            pass

    # 本地 json（用户给的 key 落这里；多账户时为 accounts/<用户名>/api_key.json）
    stored = _safe_read_json(key_file)
    if not api_key:  api_key  = str(stored.get("api_key", "") or "").strip()
    if not base_url: base_url = str(stored.get("base_url", "") or "").strip()
    if not model:    model    = str(stored.get("model", "") or "").strip()
    provider = str(stored.get("provider", "") or "").strip()

    cfg.api_key = api_key
    cfg.base_url = base_url
    cfg.model = model
    cfg.provider = provider


def _load_from_sources() -> None:
    with _cfg_lock:
        _load_from_sources_into(_CFG, KEY_FILE)


_ACCT_CFG: Dict[str, LLMConfig] = {}   # 按账户的配置缓存（含各自成功/失败统计）


def _key_file() -> str:
    """Key 落盘路径：有当前账户上下文时用 accounts/<用户名>/api_key.json，否则项目根"""
    from account_manager import current, data_dir
    u = current()
    return os.path.join(data_dir(), "api_key.json") if u else KEY_FILE


def _cur() -> LLMConfig:
    """当前生效配置对象：有账户上下文用该账户的（惰性加载），否则全局 _CFG"""
    from account_manager import current, data_dir
    u = current()
    if not u:
        return _CFG
    with _cfg_lock:
        c = _ACCT_CFG.get(u)
        if c is None:
            c = LLMConfig()
            _load_from_sources_into(c, os.path.join(data_dir(), "api_key.json"))
            _ACCT_CFG[u] = c
        return c


# 启动即加载一次；后续 set_api_key 会实时写回
_load_from_sources()


def get_config() -> Dict[str, Any]:
    """给前端展示：只回传脱敏状态"""
    with _cfg_lock:
        c = _cur()
        return {
            "enabled": c.enabled,
            "provider": c.provider or (c.base_url and _infer_provider(c.base_url)),
            "base_url": c.base_url,
            "model": c.model,
            "masked_key": c.masked_key(),
            "success": c.success,
            "fails": c.fails,
            "last_error": c.last_error,
        }


def _infer_provider(base_url: str) -> str:
    u = (base_url or "").lower()
    for p in DEFAULT_PROVIDER_HINTS:
        if p["base"].lower() in u: return p["name"]
    if "bigmodel" in u: return "智谱 bigmodel.cn"
    if "deepseek" in u: return "DeepSeek"
    if "dashscope" in u or "aliyun" in u: return "通义千问"
    if "siliconflow" in u: return "硅基流动"
    if "volces" in u: return "豆包 火山方舟"
    if "moonshot" in u: return "Kimi 月之暗面"
    return "自定义 OpenAI 兼容"


# ============== 探测 / 设置 Key ==============
def probe_and_set_api_key(api_key: str, base_url: str = "", model: str = "",
                          timeout: int = 6) -> Dict[str, Any]:
    """
    给定 key（可选 base/model），做一次连通性测试，成功则永久写入本地配置。
    返回 {"ok": bool, "message": str, "config": {...}}
    """
    api_key = (api_key or "").strip()
    if not api_key:
        return {"ok": False, "message": "API Key 不能为空。", "config": get_config()}

    # 1) 用户提供了 base + model → 直接测
    if base_url and model:
        ok, code, msg = _test_once(api_key, base_url, model, timeout)
        if ok:
            provider = _infer_provider(base_url)
            _write_and_apply(api_key, base_url, model, provider)
            return {"ok": True, "message": f"已连接到 {provider}（model={model}）", "config": get_config()}
        return {"ok": False,
                "message": f"指定的地址连通失败（HTTP {code}）：{_shorten(msg)}。请检查 base_url / model 是否匹配你的服务商。",
                "config": get_config()}

    # 2) 否则按预设服务商顺序依次探测
    attempts = []
    for p in DEFAULT_PROVIDER_HINTS:
        ok, code, msg = _test_once(api_key, p["base"], p["probe"], timeout)
        attempts.append((p["name"], code, ok, msg))
        if ok:
            _write_and_apply(api_key, p["base"], p["chat"], p["name"])
            return {"ok": True,
                    "message": f"自动识别为 {p['name']}（base={p['base']}，默认模型={p['chat']}），已保存。",
                    "config": get_config()}
    # 汇总失败原因（只保留非敏感信息）
    reasons = "; ".join([f"{a[0]}=HTTP{a[1]}" for a in attempts if a[1]])
    return {"ok": False,
            "message": f"未能在主流服务商验证该 Key。各服务响应：{reasons or '无响应'}。如使用自建/其他服务商，请手动填写 base_url 与 model。",
            "config": get_config()}


def _write_and_apply(api_key: str, base_url: str, model: str, provider: str) -> None:
    c = _cur()
    with _cfg_lock:
        c.api_key = api_key
        c.base_url = base_url.rstrip("/")
        c.model = model
        c.provider = provider
    data = {"api_key": api_key, "base_url": base_url.rstrip("/"), "model": model, "provider": provider,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    _safe_write_json(_key_file(), data)


def clear_api_key() -> Dict[str, Any]:
    """删除本地配置（环境变量若仍存在则依然优先使用环境变量）；并重置内存统计（成功/失败计数清0）"""
    # 先归档进回收站（3 天内可还原；本地存储安全级等同 api_key.json 本体）
    try:
        if os.path.exists(_key_file()):
            with open(_key_file(), "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict) and cfg.get("api_key"):
                from trash_bin import trash_bin
                trash_bin.add("apikey", f"API Key 配置（{cfg.get('provider') or '自定义'}）",
                              f"provider={cfg.get('provider') or '自定义'} · model={cfg.get('model') or '-'} · base={cfg.get('base_url') or '-'}",
                              {"config": cfg})
    except Exception:
        pass
    try:
        if os.path.exists(_key_file()): os.remove(_key_file())
    except OSError:
        pass
    with _cfg_lock:
        c = _cur()
        # 先清零统计与错误（无论最终是环境变量还是空）
        c.success = 0
        c.fails = 0
        c.last_error = ""
        # 统一重新加载（如果环境变量里有 key，保持内存里那个值；否则清空）
        _load_from_sources_into(c, _key_file())
        # 防御：若加载因为环境变量不存在也没json，确保是干净的
        if not c.api_key and not os.getenv(ENV_API_KEY):
            c.api_key = c.base_url = c.model = c.provider = ""
    return {"ok": True, "message": "本地 API Key 已删除。", "config": get_config()}


def restore_key_config(api_key: str, base_url: str, model: str, provider: str) -> Dict[str, Any]:
    """从回收站还原 Key 配置：直接写回本地并应用（不再做联网探测）"""
    if not api_key:
        return {"ok": False, "message": "归档数据缺少 api_key，无法还原。", "config": get_config()}
    _write_and_apply(api_key, base_url or "", model or "", provider or "自定义")
    return {"ok": True, "message": "API Key 配置已还原。", "config": get_config()}


# ============== 底层 HTTP 调用 ==============
_SSL_CTX = ssl.create_default_context()


def _shorten(s: Any, n: int = 160) -> str:
    if s is None: return ""
    t = str(s)
    return t if len(t) <= n else t[:n] + "…"


def _test_once(api_key: str, base_url: str, model: str, timeout: int):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "你好，只回复一个汉字“1”。"}],
        "max_tokens": 2,
        "temperature": 0,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            raw = r.read().decode("utf-8")
            try:
                j = json.loads(raw)
                if j.get("choices") and isinstance(j["choices"], list) and len(j["choices"]) > 0:
                    return True, r.status, raw[:80]
            except json.JSONDecodeError:
                pass
            return False, r.status, "非预期响应: " + _shorten(raw, 80)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        return False, e.code, _shorten(raw, 200)
    except Exception as e:
        return False, 0, f"{type(e).__name__}: {_shorten(e, 120)}"


def chat_completion(messages: List[Dict[str, str]], *, temperature: float = 0.2,
                    max_tokens: int = 800, json_mode: bool = False, timeout: int = 15,
                    model: Optional[str] = None) -> Optional[str]:
    """返回纯文本响应，失败返回 None。调用方自行决定如何兜底。

    model：按次覆盖所用模型（None=用全局配置）。轻任务可指到更快的模型，避免拖慢主流程。

    说明：针对"长响应被截断"做了两点处理：
      1) 默认不再给用户侧设置 max_tokens 的 2 倍上限；若服务商报错再自动重试一次。
      2) 读取 HTTP body 时用 r.read() 一次性读完（非流式），避免 urllib 在 chunked 下
         因 Content-Length 缺失而被上层过早解析为半截的问题。
    """
    c = _cur()
    with _cfg_lock:
        if not c.enabled:
            return None
        api_key, base_url = c.api_key, c.base_url
        use_model = (model or c.model or "").strip()

    url = base_url.rstrip("/") + "/chat/completions"

    def _build_payload(max_t: int) -> bytes:
        payload: Dict[str, Any] = {"model": use_model, "messages": messages,
                                   "temperature": temperature, "max_tokens": max_t}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _do_post(body: bytes, t_out: int) -> Optional[str]:
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=t_out, context=_SSL_CTX) as r:
                raw = r.read()  # 一次性读到 EOF，避免被截断
                text = raw.decode("utf-8", "ignore") if raw else ""
                try:
                    j = json.loads(text)
                    choices = j.get("choices")
                    if isinstance(choices, list) and len(choices) > 0:
                        msg = choices[0].get("message", {}) or {}
                        content = msg.get("content", "")
                        # 兼容部分服务商字符串形式
                        finish = choices[0].get("finish_reason", "")
                        if finish in ("length", "max_tokens") and json_mode:
                            # 疑似被截断，让上层用更大 max_tokens 重试
                            raise ValueError("finish_reason=length")
                        with _cfg_lock: c.success += 1
                        return strip_emoji(content).strip()
                except ValueError:
                    raise  # 上面 raise 的 finish=length 重抛
                except Exception:
                    # 非 JSON 形态：直接返回原文（允许调用方自己解析）
                    pass
                with _cfg_lock: c.success += 1
                return strip_emoji(text).strip() if text else ""
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore") if hasattr(e, "read") else ""
            err_msg = f"HTTP {e.code}: {_shorten(raw)}"
            with _cfg_lock:
                c.fails += 1
                c.last_error = f"{c.masked_key()} | {err_msg}"
            # 400 且 max_tokens 相关，抛出给重试逻辑
            if e.code in (400, 422) and ("max_tokens" in raw or "max_output_tokens" in raw or "length" in raw):
                raise ValueError("max_tokens_exceeded")
            return None
        except Exception as e:
            err_msg = f"{type(e).__name__}: {_shorten(str(e))}"
            with _cfg_lock:
                c.fails += 1
                c.last_error = f"{c.masked_key()} | {err_msg}"
            # ValueError("finish_reason=length") 让外层能重试
            if isinstance(e, ValueError) and "length" in str(e):
                raise
            return None

    # 第一次尝试
    try:
        result = _do_post(_build_payload(max_tokens), timeout)
        if result is not None:
            return result
    except ValueError as ve:
        # length 截断 / max_tokens 超限 → 自动以 2x 重试一次
        if "length" in str(ve).lower() or "max_tokens" in str(ve).lower():
            try:
                return _do_post(_build_payload(max_tokens * 2), timeout)
            except Exception:
                return None
        return None
    return None


# ============== 3 个高层能力（结构化 JSON 优先，失败返回 None） ==============
def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出里抽取 JSON（容忍前后解释性文字 / markdown 代码块）"""
    if not text: return None
    # markdown 代码块
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
    if m:
        try: return json.loads(m.group(1))
        except Exception: pass
    # 首尾抓取 {...}
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        try: return json.loads(text[i:j+1])
        except Exception: pass
    return None


def parse_shopping_request_with_llm(user_text: str, profile: Dict[str, Any],
                                    prev: Optional[Dict[str, Any]],
                                    history: Optional[List[Dict[str, str]]] = None) -> Optional[Dict[str, Any]]:
    """
    让 LLM 做一次需求解析，输出 JSON（对齐 Request 对象字段）。
    history：最近对话窗口（可空），用于理解指代与增量表达。
    失败返回 None，上层走原来的 RequestParser 规则兜底。
    """
    sys_prompt = """你是严格的JSON输出器，只输出一个JSON对象。
任务：把用户的购物自然语言输入解析为结构化购物需求（中文）。
必须包含字段：
  query: string|null       主搜索关键词/品类，例如「雪纺连衣裙」；若本次只是调整颜色/预算/尺码等条件而没换品类，必须填null（沿用上一轮品类，严禁脑补合并出新品类词）
  size: string|null        尺码/鞋码，如「42码」填"42"；没提则null
  category: string         粗略品类（裙子/上衣/裤子/鞋/配饰/数码/美妆/家居/食品/书籍/其他），只填一个
  budget_min: number|null  预算下限，元
  budget_max: number|null  预算上限，元
  platforms: string[]      允许的电商平台（淘宝/天猫/京东/拼多多/抖音商城 之一或多个；如用户没提就返回空数组）
  require_tags: string[]   硬性要求关键词，例如「修身」「纯棉」「V领」「法式」。用户正面要求的词才放这里
  exclude_tags: string[]   避雷关键词，例如「宽松」「涤纶」「紫色」。用户「不要/避开/讨厌」提到的词（如颜色）必须放这里，严禁放进require_tags
  use: string|null         用途场景，如通勤/约会/运动/送礼
  style: string|null       风格，如法式/美式休闲/甜酷/通勤
  is_adjustment: boolean   如果用户是"换"或"调"类请求，true；否则false
  target_rank: number|null  当用户说"买第X款"时填X，否则null
  top_n: number|null       用户指定的最终输出条数（如"排名前5"填5；没提则null）
  per_platform_n: number|null 用户指定的每平台候选条数（如"各筛前4名"填4；没提则null）
  intent: string           分类：demand=新需求/search=搜索/profile=档案相关/order=下单或确认/query_order=查订单/logistics=查物流/aftersale=售后/other=其他
结合[最近对话]理解指代与增量表达（如"再要2个""换成京东的""第二种呢"）；条数要求填入 top_n/per_platform_n。
所有 string 必须为中文简洁表述；不要输出任何文字解释。严禁输出任何emoji表情。输出必须是一个合法的JSON对象。"""
    hist_block = ""
    if history:
        lines = []
        for m in history[-8:]:
            role = "用户" if m.get("role") == "user" else "助手"
            t = str(m.get("text") or "")[:100]
            lines.append(f"{role}：{t}")
        if lines:
            hist_block = "[最近对话]\n" + "\n".join(lines) + "\n\n"
    user_prompt = (
        f"[已保存的用户个人档案]\n{json.dumps(profile, ensure_ascii=False)}\n\n"
        f"[上一轮的需求参考（若is_adjustment为true则增量叠加，否则忽略）]\n{json.dumps(prev or {}, ensure_ascii=False)}\n\n"
        f"{hist_block}"
        f"[用户当前输入]\n{user_text}\n\n请输出JSON："
    )
    text = chat_completion([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ], temperature=0.1, max_tokens=700, json_mode=True, timeout=8)
    j = _extract_json(text) if text else None
    return j


def generate_clarify_questions_with_llm(keyword: str, raw_text: str = "",
                                        known: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """按商品关键词现场生成品类适配的澄清问卷（AI 增强：任何商品都能问出其专属选购要点）。
    返回 {"questions":[{key,label,options,multi,optional}...], "purpose_options":[...]}；
    失败/不可用/产出非法返回 None，上层回退静态品类问题表（规则兜底红线）。"""
    kw = (keyword or "").strip()
    if not kw:
        return None
    try:
        if not get_config().get("enabled"):
            return None  # AI 关闭 → 直接走静态兜底（离线测试亦经此短路）
    except Exception:
        return None
    sys_prompt = """你是购物需求澄清助手，严格输出JSON对象。针对用户想买的商品生成贴合其核心选购属性的澄清问题，帮助后续电商搜索更精准。
{"questions":[{"key":"英文小写","label":"中文短问","options":["选项"],"multi":true,"optional":false}],"purpose_options":["用途选项"]}
硬性要求：
- questions 共3个：前2个为该商品的关键选购属性（各带3~6个常见取值选项，multi表示可否多选），第3个为次要方向问（optional=true，options填null，label括号内给方向提示）。任何商品都问得出它独有的选购要点（例：泡面→口味/包装规格；机械键盘→轴体/连接方式；窗帘→材质/遮光）。
- 选项2~8个字且严禁包含任何数字或价格（容量写「小/中/大容量」禁止「500ml」），防止被误解析成预算。
- 不问预算，不问[已知信息]里已有的内容；label 不超过20字。
- purpose_options：3~5个贴合该品类的用途选项（食品给「囤货/夜宵加班」，数码给「游戏/办公」，不要张冠李戴）。
- 只输出一个合法JSON对象，禁止emoji，不要输出思考过程。"""
    user_prompt = (
        f"[商品关键词] {kw}\n"
        f"[用户原始表述] {(raw_text or kw).strip()[:120]}\n"
        f"[已知信息（不要重复追问）]\n{json.dumps(known or {}, ensure_ascii=False)}\n\n请输出JSON："
    )
    # 问卷生成属轻任务：智谱直连时指到 flash 快档（实测主档 glm-5.3 生成一张问卷 40s+，flash 约 15s；
    # 其他服务商不指名，避免请求不存在的模型）
    model_override = None
    try:
        if "bigmodel.cn" in (get_config().get("base_url") or "").lower():
            model_override = "glm-5.3-flash"
    except Exception:
        model_override = None
    text = chat_completion([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ], temperature=0.3, max_tokens=1200, json_mode=True, timeout=25, model=model_override)
    j = _extract_json(text) if text else None
    if not j or not isinstance(j.get("questions"), list) or not j["questions"]:
        return None
    return j


def answer_free_question_with_llm(question: str, materials: Dict[str, Any]) -> Optional[str]:
    """自由问答：基于会话材料（上次推荐/最近对话/档案摘要）回答用户追问。
    返回回答文本；需新搜索或与材料无关时返回 "NEED_SEARCH"；失败返回 None（上层回退规则流程）。"""
    if not question or not question.strip() or not isinstance(materials, dict):
        return None
    sys_prompt = (
        "你是购物助手的对话答疑模块。用户在当前会话中追问，你只能依据【材料】回答。\n"
        "红线：\n"
        "1) 材料里没有的事实（历史价格/库存/真伪/未出现过的商品）如实回答「材料中没有，无法判断」，"
        "绝不编造商品、价格、评价；\n"
        "2) 「买哪个好」类问题：基于评分材料给建议并说明理由，结尾提醒一句下单前核对尺码/价格；\n"
        "3) 回答用简洁中文 Markdown，不超过300字。严禁使用任何emoji表情。\n"
        "输出约定：若用户是在要求搜索/推荐新商品，或问题与本会话材料完全无关，只输出一行：NEED_SEARCH"
    )
    user_prompt = (
        f"[材料]\n{json.dumps(materials, ensure_ascii=False, indent=2)}\n\n"
        f"[用户问题]\n{question.strip()}\n\n请回答："
    )
    return chat_completion([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ], temperature=0.4, max_tokens=500, timeout=10)


def summarize_reviews_with_llm(product_name: str, good_raw: List[str],
                               bad_raw: List[str]) -> Optional[Dict[str, str]]:
    """好评+差评 → 结构化 {'good_summary': '…', 'bad_summary': '…'}；失败返回None"""
    if not (good_raw or bad_raw): return None
    sys_prompt = """你是电商评价分析师，严格输出JSON对象：
{
  "good_summary": "用简洁中文，分2~4条不换行（用；分隔），总结买家一致的好评点。不要重复商品介绍，只保留真实使用反馈。尽量具体：如'腰部显瘦/洗后不起球'。",
  "bad_summary": "用简洁中文，分2~4条不换行，总结真实的吐槽和问题点；如果没有差评则写空字符串''。"
}
只输出一个合法JSON对象，不要解释。严禁使用任何emoji表情。"""
    good_str = "\n".join([f"- {g}" for g in good_raw[:20]]) or "(无好评数据)"
    bad_str  = "\n".join([f"- {g}" for g in bad_raw[:20]])  or "(无差评数据)"
    user_prompt = f"商品：{product_name}\n\n【好评】\n{good_str}\n\n【差评】\n{bad_str}\n\n请输出JSON："
    text = chat_completion([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ], temperature=0.2, max_tokens=400, json_mode=True, timeout=6)
    j = _extract_json(text) if text else None
    if j and (j.get("good_summary") or j.get("bad_summary")):
        return {"good_summary": str(j.get("good_summary") or "").strip(),
                "bad_summary":  str(j.get("bad_summary")  or "").strip()}
    return None


# 人群交互风格：按用户中心 persona 偏好注入导购润色的系统提示词（仅 LLM 增强，规则引擎不受影响）
_PERSONA_STYLES: Dict[str, str] = {
    "student": "用户是学生党：语气轻快亲切，点评侧重性价比与预算友好，少用营销话术。",
    "office":  "用户是上班族：语气高效专业，点评侧重品质耐用、办公通勤场景与省时间。",
    "senior":  "用户是长辈：语气亲切耐心、通俗易懂，点评侧重易用性、售后保障与性价比，避免网络用语。",
}


def polish_recommendation_with_llm(items: List[Dict[str, Any]],
                                    profile: Dict[str, Any],
                                    persona: Optional[str] = None) -> Optional[str]:
    """把推荐TOP-N对象润色为一份更像人类导购的建议报告（纯文本 Markdown）；
    persona 缺省时自动读用户中心偏好，仅影响语气侧重，不改变事实判断"""
    if not items: return None
    if persona is None:
        try:
            import user_center  # 仅标准库模块，无循环依赖
            persona = user_center.get_prefs().get("persona", "default")
        except Exception:
            persona = "default"
    style = _PERSONA_STYLES.get(persona or "", "")
    n_items = len(items)
    word_cap = min(1000, 500 + max(0, n_items - 5) * 100)  # ≤5款500字起，每多1款+100字，封顶1000
    sys_prompt = f"""你是贴心但客观的购物顾问，语气简洁，不吹捧商品。
输入为 TOP{n_items} 推荐对象列表和用户个人档案。
请输出一段中文 Markdown 报告（不超过{word_cap}字）：
1) 用1~2句话总结本次推荐总体风格（如何结合用户档案）；
2) 针对每款，给出一句「为什么适合这个用户」的个性化点评，一定要点名用户档案里的身高/体重/风格/颜色偏好等具体点；
3) 客观指出每款的「明显短板」；
不要重复返回整张大表格（上层已经渲染过），重点在"人味"点评和档案适配理由。
最后加一句「下单前请再次核对尺码/颜色；如需调整请回：换第X款/更修身/换颜色等」。
不要输出任何JSON。严禁使用任何emoji表情图标。"""
    if style:
        sys_prompt += "\n" + style
    user_prompt = (
        f"[用户个人档案]\n{json.dumps(profile, ensure_ascii=False)}\n\n"
        f"[TOP{n_items} 推荐列表]\n{json.dumps(items, ensure_ascii=False, indent=2)}\n"
    )
    return chat_completion([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_prompt},
    ], temperature=0.5, max_tokens=900, timeout=10)


# ============== 4) 视觉多模态：图片分析（场景A找同款 / 场景B多图对比） ==============
# provider base_url → 视觉模型映射（当前配置的 chat 模型不支持视觉时自动切换）
_VISION_MODELS: Dict[str, str] = {
    "open.bigmodel.cn":     "glm-4v-flash",   # 智谱：glm-4v-flash 免费，glm-4v-plus 付费
    "dashscope.aliyuncs.com": "qwen-vl-plus", # 通义千问视觉
    "ark.cn-beijing.volces.com": "doubao-1-5-vision-pro-250328",  # 豆包视觉
    "api.moonshot.cn":      "moonshot-v1-8k-vision-preview",  # Kimi 视觉预览
    # OpenAI 兼容：gpt-4o / gpt-4o-mini 自带视觉，无需切换
}


def _pick_vision_model(base_url: str, current_model: str) -> str:
    """根据 base_url 匹配 provider，返回对应的视觉模型名。"""
    m_low = (current_model or "").lower()
    # 已经是视觉模型（含 v/vl/vision/gpt-4o），直接返回
    if any(k in m_low for k in ("4v", "-vl", "vision", "gpt-4o", "qwenvl")):
        return current_model
    url_low = (base_url or "").lower()
    for host_frag, vmodel in _VISION_MODELS.items():
        if host_frag in url_low:
            return vmodel
    return current_model  # 未知 provider，沿用当前模型


def vision_completion(prompt: str, image_data_list: List[str], *,
                       temperature: float = 0.3, max_tokens: int = 1200,
                       timeout: int = 30) -> Optional[str]:
    """
    视觉 LLM 调用：传入文本 prompt + 1~5 张图片（base64 data URI）。
    image_data_list 形如 ["data:image/jpeg;base64,...", ...]
    返回纯文本响应，失败返回 None（上层回退到规则提示）。
    会自动切换到当前 provider 对应的视觉模型（如智谱 glm-4-flash → glm-4v-flash）。
    """
    c = _cur()
    with _cfg_lock:
        if not c.enabled:
            return None
        api_key, base_url, model = c.api_key, c.base_url, c.model

    # 自动切换到视觉模型：当前 model 不含 'v'/'vl'/'vision' 时，按 provider 映射
    vision_model = _pick_vision_model(base_url, model)
    if vision_model != model:
        model = vision_model

    url = base_url.rstrip("/") + "/chat/completions"
    # 构建 OpenAI vision 格式的 content 块
    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img_uri in image_data_list[:5]:
        if not img_uri:
            continue
        content.append({"type": "image_url",
                        "image_url": {"url": img_uri, "detail": "low"}})
    payload: Dict[str, Any] = {
        "model": model, "messages": [{"role": "user", "content": content}],
        "temperature": temperature, "max_tokens": max_tokens,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            raw = r.read()
            j = json.loads(raw.decode("utf-8", "ignore") or "{}")
            choices = j.get("choices") or []
            if choices:
                content_text = (choices[0].get("message", {}) or {}).get("content", "")
                with _cfg_lock:
                    c.success += 1
                return strip_emoji(content_text).strip()
            return None
    except Exception as e:
        err_msg = f"{type(e).__name__}: {_shorten(str(e))}"
        with _cfg_lock:
            c.fails += 1
            c.last_error = f"{c.masked_key()} | vision: {err_msg}"
        return None


def analyze_image_find_similar(image_data_uri: str) -> Optional[Dict[str, Any]]:
    """
    场景A：单张图片找同款。
    返回 dict: {keyword, category, color, style, material, details}
    失败返回 None。
    """
    sys_prompt = """你是电商选品专家。请仔细分析这张商品图片，提取以下信息并以 JSON 格式返回：
{
  "keyword": "精准搜索关键词（适合在淘宝/京东搜索框直接使用的，10-20字）",
  "category": "品类（如连衣裙/运动鞋/耳机/T恤）",
  "color": "主色调",
  "style": "风格（如法式/日系/通勤/运动等）",
  "material": "可见材质推测（如雪纺/纯棉/皮革等，不确定写未知）",
  "details": "设计细节（1-2句话描述亮点）",
  "blurry": false
}
如果图片模糊或无法识别，blurly 设为 true，其他字段留空。只返回 JSON，不要其他文字。严禁使用任何emoji。"""
    raw = vision_completion(sys_prompt, [image_data_uri], temperature=0.2, max_tokens=500)
    if not raw:
        return None
    j = _extract_json(raw)
    return j if isinstance(j, dict) else None


def analyze_images_compare(image_data_list: List[str],
                           profile: Dict[str, Any]) -> Optional[str]:
    """
    场景B：多张图片对比分析。
    返回 Markdown 对比表格文本，失败返回 None。
    """
    profile_brief = json.dumps({k: v for k, v in profile.items() if v}, ensure_ascii=False) if profile else "无"
    sys_prompt = f"""你是电商选品专家。用户上传了 {len(image_data_list)} 张商品图片，请对每张图片分别识别并做横向对比。

[用户档案摘要] {profile_brief}

请输出 Markdown 格式：
1. 先输出对比表格，列：商品编号 | 外观特点 | 优势 | 劣势 | 适合人群
2. 表格后输出选购建议（结合用户档案的预算/偏好）
3. 最后加一句：**图片分析仅为视觉推测，完整参数请以商品网页为准**

如果某张图片是截图且能看到价格/规格，可提取；但需标注【该信息仅来自图片，需要打开商品链接确认真实参数】。
如果图片模糊，该行标注「图片细节不足」。输出严禁使用任何emoji表情。"""
    return vision_completion(sys_prompt, image_data_list, temperature=0.4, max_tokens=1024)
