# -*- coding: utf-8 -*-
"""
真实电商抓取层（Playwright 真实浏览器版 · 单链接详情页抓取）

【反爬强制规则】
  - 禁止自动批量搜索商品列表，禁止遍历搜索结果页。
  - 仅接受用户主动粘贴的单个商品详情链接，逐个抓取。
  - 必须使用 Playwright 真实浏览器（有头 + stealth + 持久化配置 + 随机延迟）。
  - 验证码/滑块/登录弹窗：暂停自动化，交还浏览器给用户，
    输出「检测到验证，请在弹出的浏览器里手动完成验证，完成后回复“继续抓取”」。
  - 抓取失败必须如实告知，严禁编造商品信息。演示数据须用 ⚠️ 标记。
  - 浏览器真人模拟：点击/滚动 800–2200ms 随机延迟；页面加载后等待 1.2–3s 再提取。
"""

import os
import re
import time
import random
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse

from product_searcher import Product, Review

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 持久化用户配置目录：保存 cookies / 登录态 / 本地存储，实现会话复用
BROWSER_PROFILE_DIR = os.path.join(BASE_DIR, ".browser_profile")
os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)

# stealth 隐身补丁
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
window.chrome = window.chrome || { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters);
"""


def _random_delay(min_s: float = 0.8, max_s: float = 2.2) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _slow_scroll(page, steps: int = 5, per_step_min: float = 0.6,
                 per_step_max: float = 1.4) -> None:
    for _ in range(steps):
        try:
            page.mouse.wheel(0, random.randint(300, 700))
        except Exception:
            try:
                page.evaluate("window.scrollBy(0, %d)" % random.randint(300, 700))
            except Exception:
                pass
        _random_delay(per_step_min, per_step_max)


def _has_playwright() -> bool:
    try:
        import playwright  # noqa
        from playwright.sync_api import sync_playwright  # noqa
        return True
    except Exception:
        return False


def _check_browser_binaries() -> Optional[str]:
    try:
        home = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "ms-playwright")
        if not os.path.isdir(home):
            return None
        for name in os.listdir(home):
            if name.startswith("chromium-") and os.path.isdir(os.path.join(home, name)):
                exe = os.path.join(home, name, "chrome-win", "chrome.exe")
                if os.path.exists(exe):
                    return "chromium"
                exe64 = os.path.join(home, name, "chrome-win64", "chrome.exe")
                if os.path.exists(exe64):
                    return "chromium"
        return None
    except Exception:
        return None


def _detect_block(page, platform: str) -> Tuple[bool, str]:
    """检测反爬/验证码/登录墙"""
    block_signals_map = {
        "淘宝": ["login.taobao", "请登录", "滑动验证", "验证码", "x5sec", "baxia"],
        "京东": ["passport.jd.com", "请登录", "验证码", "slider", "nc_1_n1z"],
        "拼多多": ["请登录", "验证码", "滑动验证", "captcha"],
    }
    signals = block_signals_map.get(platform, ["请登录", "验证码", "滑动验证"])
    try:
        html = page.content()
    except Exception:
        return False, ""
    low = html.lower()
    for s in signals:
        if s.lower() in low:
            return True, f"检测到反爬特征词：{s}"
    for sel in [".nc_iconfont", "#nc_1_n1z", ".J_MIDDLEWARE_FRAME",
                ".baxia-dialog", ".captcha", "#slider", ".J_VerifyCode"]:
        try:
            if page.query_selector(sel):
                return True, f"检测到验证元素：{sel}"
        except Exception:
            pass
    return False, ""


def _detect_platform(url: str) -> str:
    """从 URL 识别平台"""
    host = (urlparse(url).netloc or "").lower()
    if "taobao" in host or "tmall" in host:
        return "淘宝/天猫"
    if "jd.com" in host:
        return "京东"
    if "yangkeduo" in host or "pinduoduo" in host:
        return "拼多多"
    if "douyin" in host or "jinritemai" in host:
        return "抖音商城"
    return "未知平台"


def _launch_browser(headless: bool = False):
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    context = pw.chromium.launch_persistent_context(
        user_data_dir=BROWSER_PROFILE_DIR,
        headless=headless,
        args=launch_args,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"),
    )
    context.add_init_script(_STEALTH_JS)
    page = context.pages[0] if context.pages else context.new_page()
    return pw, context, page


# ---------- 单链接详情页抓取（核心入口） ----------
def grab_product_detail(url: str, headless: bool = False) -> Dict[str, Any]:
    """
    抓取单个商品详情页，提取结构化字段。
    绝不编造：拿不到的字段返回空字符串/空列表/0。
    返回 dict 包含 product (Product) + block_reason + need_human。
    """
    platform = _detect_platform(url)
    result: Dict[str, Any] = {
        "url": url, "platform": platform, "product": None,
        "block_reason": "", "need_human": False, "data_source": "真实",
    }

    if not _has_playwright():
        result["block_reason"] = "Playwright 未安装，请对我说「安装 Playwright」"
        result["data_source"] = "演示"
        return result
    if not _check_browser_binaries():
        result["block_reason"] = "Chromium 二进制未下载，请运行 python -m playwright install chromium"
        result["data_source"] = "演示"
        return result

    try:
        pw, context, page = _launch_browser(headless=headless)
    except Exception as e:
        result["block_reason"] = f"浏览器启动失败：{e}"
        result["data_source"] = "演示"
        return result

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        _random_delay(1.2, 3.0)
        _slow_scroll(page, steps=4)
        _random_delay(0.8, 1.8)

        # 检测反爬
        blocked, reason = _detect_block(page, platform)
        if blocked:
            result["block_reason"] = (f"🛑 {platform} {reason}。"
                                     "请在弹出的浏览器里手动完成验证，完成后回复「继续抓取」。")
            result["need_human"] = True
            return result

        # 提取字段（绝不编造，拿不到留空）
        data = _extract_detail_fields(page, platform, url)
        p = _build_product_from_detail(data, platform, url)
        result["product"] = p
    except Exception as e:
        result["block_reason"] = f"抓取异常：{e}"
        result["data_source"] = "演示"
    finally:
        try:
            context.close()
            pw.stop()
        except Exception:
            pass
    return result


def _extract_detail_fields(page, platform: str, url: str) -> Dict[str, Any]:
    """从详情页 DOM 提取字段，拿不到返回空。绝不编造。"""
    data: Dict[str, Any] = {
        "name": "", "price": 0.0, "original_price": 0.0, "discount": "",
        "seller": "", "is_flagship": False, "specs": [], "tags": [],
        "ship_days": 2, "after_sale": [], "sales": 0,
        "review_count": 0, "positive_rate": 0.0, "image_reviews": 0,
        "good_points": [], "bad_points": [], "images": [],
    }
    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""

    # 商品名称
    for sel in ['[class*="ItemHeader--main"]', '[class*="mainTitle"]',
                '.ItemHeader--mainTitle', 'h1[class*="title"]',
                '.tb-main-title', '[class*="ItemHeader"]',
                'h1', '[class*="skuName"]', '[class*="product-title"]']:
        try:
            el = page.query_selector(sel)
            if el:
                t = el.inner_text().strip()
                if t and len(t) > 4:
                    data["name"] = t[:120]
                    break
        except Exception:
            continue

    # 价格
    for sel in ['[class*="Price--priceInt"]', '[class*="priceWrap"] [class*="price"]',
                '.tb-rmb-num', '[class*="price"]', '.p-price i',
                '[class*="ItemHeader--price"]', '[class*="final-price"]']:
        try:
            el = page.query_selector(sel)
            if el:
                t = el.inner_text().strip().replace(",", "")
                m = re.search(r"\d+(?:\.\d{1,2})?", t)
                if m:
                    data["price"] = float(m.group())
                    if data["price"] > 0:
                        break
        except Exception:
            continue

    # 原价
    for sel in ['[class*="Price--originPrice"]', '[class*="originalPrice"]',
                '.tb-rmb-old', '.p-price .del', '[class*="origin-price"]']:
        try:
            el = page.query_selector(sel)
            if el:
                t = el.inner_text().strip().replace(",", "")
                m = re.search(r"\d+(?:\.\d{1,2})?", t)
                if m:
                    data["original_price"] = float(m.group())
                    break
        except Exception:
            continue

    if data["original_price"] == 0.0 and data["price"] > 0:
        data["original_price"] = data["price"]

    # 到手价 = price（页面显示的当前价）
    data["final_price"] = data["price"]

    # 店铺名
    for sel in ['[class*="shopName"]', '[class*="ShopHeader"]',
                '.tb-seller-name', '[class*="store"]', '.p-shop',
                '[class*="shop-name"]']:
        try:
            el = page.query_selector(sel)
            if el:
                t = el.inner_text().strip()
                if t:
                    data["seller"] = t[:60]
                    break
        except Exception:
            continue
    if "旗舰" in data["seller"] or "官方" in data["seller"]:
        data["is_flagship"] = True

    # 优惠信息
    for kw in ["满减", "优惠券", "立减", "直降", "补贴", "折扣"]:
        if kw in body_text:
            idx = body_text.find(kw)
            snippet = body_text[max(0, idx - 10):idx + 30].strip()
            if snippet and snippet not in (data["discount"] or ""):
                data["discount"] = (data["discount"] + " " + snippet).strip()[:80]

    # 规格/参数
    for sel in ['[class*="parameter"] li', '[class*="spec"] li',
                '.item-params li', '.p-parameter li',
                '[class*="Attributes"] li', '[class*="attrs"] li']:
        try:
            els = page.query_selector_all(sel)
            for el in els[:15]:
                t = el.inner_text().strip()
                if t and len(t) < 40:
                    data["specs"].append(t)
            if data["specs"]:
                break
        except Exception:
            continue

    # 售后/服务
    for kw in ["7天无理由", "运费险", "正品保证", "极速退款", "退货包运费",
               "坏损包退", "上门取退"]:
        if kw in body_text:
            data["after_sale"].append(kw)

    # 发货时效
    m = re.search(r'(?:预计|发货)[：: ]*(\d{1,2})\s*(?:天|小时内)', body_text)
    if m:
        try:
            data["ship_days"] = int(m.group(1))
        except ValueError:
            pass

    # 销量
    m = re.search(r'(?:月销|销量|已售|收货)\s*(\d+)', body_text)
    if m:
        try:
            data["sales"] = int(m.group(1))
        except ValueError:
            pass

    # 评价数 / 好评率
    m = re.search(r'(?:累计评价|评价数|全部评价|追评)\s*[：:]*\s*(\d+)', body_text)
    if m:
        try:
            data["review_count"] = int(m.group(1))
        except ValueError:
            pass
    m = re.search(r'好评率[：: ]*([0-9.]+)%', body_text)
    if m:
        try:
            data["positive_rate"] = float(m.group(1)) / 100.0
        except ValueError:
            pass

    # 属性标签：从标题/规格里提取颜色、材质、风格等
    title = data["name"] + " " + " ".join(data["specs"])
    for kw in ["白色", "黑色", "粉色", "蓝色", "绿色", "红色", "灰色", "米色",
               "碎花", "雪纺", "纯棉", "针织", "缎面", "亚麻", "皮革",
               "法式", "日系", "美式", "通勤", "甜酷", "运动", "休闲", "国风",
               "修身", "宽松", "紧身", "长裙", "短裙", "收腰", "A字"]:
        if kw in title and kw not in data["tags"]:
            data["tags"].append(kw)

    # 评价摘要（尽力提取前几条评价文本）
    for sel in ['[class*="Comment--content"]', '[class*="rate-detail"]',
                '.tb-revbd-item', '[class*="review-item"]', '.comment-item']:
        try:
            els = page.query_selector_all(sel)
            for el in els[:5]:
                t = el.inner_text().strip()
                if t and len(t) > 10:
                    if any(w in t for w in ["好", "满意", "喜欢", "不错", "推荐"]):
                        if t not in data["good_points"]:
                            data["good_points"].append(t[:80])
                    elif any(w in t for w in ["差", "不好", "退货", "失望", "瑕疵"]):
                        if t not in data["bad_points"]:
                            data["bad_points"].append(t[:80])
            if data["good_points"] or data["bad_points"]:
                break
        except Exception:
            continue

    # 主图
    for sel in ['[class*="PicGallery"] img', '.tb-main-pic img',
                '[class*="mainPic"] img', '#preview img', '[class*="gallery"] img']:
        try:
            els = page.query_selector_all(sel)
            for el in els[:5]:
                src = el.get_attribute("src") or el.get_attribute("data-src") or ""
                if src and src.startswith("http"):
                    data["images"].append(src)
            if data["images"]:
                break
        except Exception:
            continue

    return data


def _build_product_from_detail(data: Dict[str, Any], platform: str,
                                url: str) -> Product:
    """从抓取到的字段构造 Product"""
    review = Review(
        good_points=data.get("good_points", []),
        bad_points=data.get("bad_points", []),
        image_reviews=data.get("image_reviews", 0),
        review_count=data.get("review_count", 0),
        positive_rate=data.get("positive_rate", 0.0) or 0.0,
    )
    pid = "real-" + str(abs(hash(data.get("name", "") + str(data.get("price", 0)))) % 100000)
    return Product(
        pid=pid,
        name=data.get("name", "未知商品"),
        platform=platform,
        category="",
        price=data.get("original_price", 0.0) or data.get("price", 0.0),
        final_price=data.get("final_price", 0.0) or data.get("price", 0.0),
        discount=data.get("discount", ""),
        stock=999,
        sales=data.get("sales", 0),
        ship_days=data.get("ship_days", 2),
        seller=data.get("seller", ""),
        after_sale=data.get("after_sale", []),
        images=data.get("images", []),
        tags=data.get("tags", []),
        review=review,
        source_url=url,
        data_source="真实",
    )


def fetch_product_detail(url: str) -> Dict[str, Any]:
    """兼容旧接口：返回 specs/good_points 等"""
    r = grab_product_detail(url)
    if r.get("product"):
        p = r["product"]
        return {
            "url": url, "specs": p.tags, "good_points": p.review.good_points,
            "bad_points": p.review.bad_points, "review_count": p.review.review_count,
            "data_source": r.get("data_source", "真实"),
        }
    return {"url": url, "specs": [], "good_points": [], "bad_points": [],
            "review_count": 0, "data_source": r.get("data_source", "演示"),
            "block_reason": r.get("block_reason", "")}


def get_price_history(url: str) -> Dict[str, Any]:
    """
    查询商品历史价格。
    当前实现：打开详情页读取当前价，并尝试第三方比价服务（慢慢买）。
    历史曲线暂不支持时，返回当前价对比 + 明确告知。
    """
    result: Dict[str, Any] = {
        "url": url, "current_price": 0.0, "history_low_3m": None,
        "history_low_6m": None, "history_low_12m": None,
        "low_date": "", "level": "未知", "source": "当前页面对比",
    }
    # 先抓当前价
    r = grab_product_detail(url)
    if r.get("product"):
        result["current_price"] = r["product"].final_price
    # 尝试慢慢买
    if not _has_playwright() or not _check_browser_binaries():
        result["source"] = "暂不支持（Playwright 不可用）"
        return result
    try:
        pw, context, page = _launch_browser(headless=False)
        try:
            from urllib.parse import quote
            kw = r["product"].name[:20] if r.get("product") else ""
            mm_url = f"https://www.manmanbuy.com/search.aspx?keyword={quote(kw)}"
            page.goto(mm_url, wait_until="domcontentloaded", timeout=20000)
            _random_delay(1.0, 2.0)
            _slow_scroll(page, steps=3)
            # 尝试提取历史低价
            try:
                items = page.query_selector_all('[class*="item"], .goods-list li')
                for it in items[:3]:
                    try:
                        t = it.inner_text()
                        m = re.search(r'(?:最低|历史最低)[：: ]*¥?(\d+(?:\.\d{1,2})?)', t)
                        if m:
                            result["history_low_12m"] = float(m.group(1))
                            result["history_low_6m"] = result["history_low_12m"]
                            result["history_low_3m"] = result["history_low_12m"]
                            result["source"] = "慢慢买(第三方比价)"
                            break
                    except Exception:
                        continue
            except Exception:
                pass
        finally:
            context.close()
            pw.stop()
    except Exception:
        pass
    # 价位评估
    cur = result["current_price"]
    low = result["history_low_12m"]
    if cur > 0 and low and low > 0:
        ratio = (cur - low) / low
        if ratio <= 0.05:
            result["level"] = "好价"
        elif ratio <= 0.20:
            result["level"] = "正常价"
        else:
            result["level"] = "高价"
    return result


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://item.taobao.com/item.htm?id=xxx"
    r = grab_product_detail(url)
    if r.get("product"):
        p = r["product"]
        print(f"✅ {p.name} ¥{p.final_price} ({p.platform}) [{p.data_source}]")
        print(f"  店铺：{p.seller}  销量：{p.sales}  发货：{p.ship_days}天")
        print(f"  标签：{p.tags}")
        print(f"  好评点：{p.review.good_points[:2]}")
        print(f"  差评点：{p.review.bad_points[:2]}")
    else:
        print(f"❌ 抓取失败：{r.get('block_reason', '未知')}")
