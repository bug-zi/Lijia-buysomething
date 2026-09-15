# -*- coding: utf-8 -*-
"""
推荐打分模块 + TOP-N 格式化输出（默认3条，用户指定条数优先）
评分维度：
1. 档案匹配度   (30%) — 颜色、风格、尺码、材质、版型偏好对齐程度
2. 性价比       (25%) — 同价位销量/优惠/原价对比
3. 真实口碑     (20%) — 好评率、带图追评数、评价总量
4. 差评风险     (15%) — 差评点数量/严重程度
5. 发货时效     (10%) — 发货天数、是否自营/次日达
输出条数默认 TOP3，支持用户指定条数（安全上限10，抓取克制），并附带横向对比与档案适配说明
"""

from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Any
import re

from profile_module import ProfileManager
from product_searcher import Product, ProductSearcher

# 可选：AI 评价摘要 + 推荐润色
try:
    from ai_client import summarize_reviews_with_llm, polish_recommendation_with_llm  # noqa
    _AI_AVAILABLE = True
except Exception:
    summarize_reviews_with_llm = None
    polish_recommendation_with_llm = None
    _AI_AVAILABLE = False


# 关键词权重表 — 用于计算"偏好匹配度"
COLOR_KEYWORDS = {
    "白色": ["白色", "米白", "奶白"],
    "黑色": ["黑色", "炭黑", "纯黑"],
    "粉色": ["粉色", "樱花粉", "粉"],
    "蓝色": ["蓝色", "藏青", "天蓝", "湖蓝"],
    "绿色": ["绿色", "墨绿", "薄荷绿", "翠绿"],
    "红色": ["红色", "酒红", "中国红"],
    "灰色": ["灰色", "灰白", "深灰"],
    "米色": ["米色", "米白", "卡其"],
    "碎花": ["碎花", "花色", "印花"],
}

STYLE_KEYWORDS = {
    "日系": ["日系", "甜美", "清新", "森系"],
    "美式": ["美式", "复古", "oversize", "街头", "潮牌"],
    "通勤": ["通勤", "商务", "西装", "正式", "职业", "OL"],
    "甜酷": ["辣妹", "甜酷", "潮酷", "露肩", "包臀", "party"],
    "运动": ["运动", "跑鞋", "篮球鞋", "健身", "速干"],
    "休闲": ["休闲", "简约", "T恤", "基础款", "百搭"],
    "国风": ["国风", "旗袍", "改良", "新中式", "盘扣"],
    "法式": ["法式", "茶歇", "碎花", "浪漫"],
}

MATERIAL_KEYWORDS = {
    "纯棉": ["纯棉", "全棉", "棉", "珠地棉"],
    "雪纺": ["雪纺", "乔其纱"],
    "针织": ["针织", "毛衣", "毛线"],
    "缎面": ["缎面", "真丝", "丝绸"],
    "亚麻": ["亚麻", "棉麻", "麻"],
    "牛仔": ["牛仔", "丹宁"],
    "皮革": ["皮革", "皮质", "真皮"],
}

FIT_KEYWORDS = {
    "修身": ["修身", "包臀", "收腰", "合身", "显瘦"],
    "宽松": ["宽松", "oversize", "A字", "阔", "直筒"],
    "紧身": ["紧身", "偏紧", "弹力"],
}


@dataclass
class ScoreBreakdown:
    """评分明细，用于横向对比展示"""
    profile_match: float  # 档案匹配 0-100
    value: float          # 性价比 0-100
    reputation: float     # 口碑 0-100
    risk: float           # 差评风险(越低越好) 0-100
    ship: float           # 发货时效 0-100
    total: float          # 加权总分 0-100

    def as_row(self) -> str:
        return (
            f"匹配{self.profile_match:>3.0f}  "
            f"性价{self.value:>3.0f}  "
            f"口碑{self.reputation:>3.0f}  "
            f"风险{100-self.risk:>3.0f}  "
            f"时效{self.ship:>3.0f}  "
            f"**总分{self.total:>3.0f}**"
        )

    # ---- 0-10 归一化（规格评测卡片用） ----
    @property
    def adapt_10(self) -> float:
        """适配度 0-10"""
        return round(self.profile_match / 10.0, 1)

    @property
    def value_10(self) -> float:
        """性价比 0-10"""
        return round(self.value / 10.0, 1)

    @property
    def total_10(self) -> float:
        """综合推荐得分 0-10"""
        return round(self.total / 10.0, 1)


class Recommender:
    """推荐器：结合个人档案对商品打分并输出TOP-N（默认3条）"""

    WEIGHTS = dict(
        profile_match=0.30,
        value=0.25,
        reputation=0.20,
        risk=0.15,
        ship=0.10,
    )

    def __init__(self, profile: ProfileManager, searcher: Optional[ProductSearcher] = None):
        self.profile = profile
        self.searcher = searcher or ProductSearcher()
        # 上一次推荐结果缓存，便于"换更修身的"这类增量请求
        self._last_products: List[Product] = []
        self._last_scores: Dict[str, ScoreBreakdown] = {}

    # ========== 打分 ==========
    def score_product(self, p: Product, budget: Optional[float] = None) -> ScoreBreakdown:
        profile_match = self._score_profile_match(p)
        value = self._score_value(p, budget)
        reputation = self._score_reputation(p)
        risk = self._score_risk(p)           # 风险(越高=风险越大)，公式用(100-risk)作安全分
        ship = self._score_ship(p)

        total = (
            profile_match * self.WEIGHTS["profile_match"]
            + value * self.WEIGHTS["value"]
            + reputation * self.WEIGHTS["reputation"]
            + (100 - risk) * self.WEIGHTS["risk"]
            + ship * self.WEIGHTS["ship"]
        )
        return ScoreBreakdown(
            profile_match=round(profile_match, 1),
            value=round(value, 1),
            reputation=round(reputation, 1),
            risk=round(risk, 1),
            ship=round(ship, 1),
            total=round(total, 1),
        )

    # --------- 1. 档案匹配度 ---------
    def _score_profile_match(self, p: Product) -> float:
        text = " ".join(p.tags) + " " + p.name
        score = 60.0  # 基础分，无档案时保底
        has_rule = False

        def hit(text: str, whitelist: Optional[str], blacklist: Optional[str], lexicon: Dict[str, List[str]]) -> Tuple[float, float]:
            pos = 0.0
            neg = 0.0
            if whitelist:
                for word, alts in lexicon.items():
                    if any(x.lower() in whitelist.lower() for x in [word] + alts):
                        if any(a.lower() in text.lower() for a in [word] + alts):
                            pos += 1.0
            if blacklist:
                for word, alts in lexicon.items():
                    if any(x.lower() in blacklist.lower() for x in [word] + alts):
                        if any(a.lower() in text.lower() for a in [word] + alts):
                            neg += 1.0
            return pos, neg

        # 颜色
        pos, neg = hit(text, self.profile.get("color_like"), self.profile.get("color_dislike"), COLOR_KEYWORDS)
        if self.profile.get("color_like") or self.profile.get("color_dislike"):
            has_rule = True
            if pos > 0: score += 10 * min(pos, 2)
            if neg > 0: score -= 25 * min(neg, 2)

        # 风格
        pos, neg = hit(text, self.profile.get("style_like"), None, STYLE_KEYWORDS)
        if self.profile.get("style_like"):
            has_rule = True
            if pos > 0: score += 15 * min(pos, 2)

        # 材质
        pos, neg = hit(text, self.profile.get("material_like"), self.profile.get("material_dislike"), MATERIAL_KEYWORDS)
        if self.profile.get("material_like") or self.profile.get("material_dislike"):
            has_rule = True
            if pos > 0: score += 8 * min(pos, 1.5)
            if neg > 0: score -= 20 * min(neg, 1.5)

        # 版型
        pos, neg = hit(text, self.profile.get("fit_like"), self.profile.get("fit_dislike"), FIT_KEYWORDS)
        if self.profile.get("fit_like") or self.profile.get("fit_dislike"):
            has_rule = True
            if pos > 0: score += 12 * min(pos, 2)
            if neg > 0: score -= 22 * min(neg, 2)

        # 尺码习惯（简单：若商品标签含"偏小"且用户选码"偏小"，加分；反之亦然）
        size_habit = str(self.profile.get("size_habit") or "")
        if size_habit:
            has_rule = True
            # 尺码标签检查 — 我们在Product.tags中没有显式字段，可在商品描述中检索
            if ("偏大" in size_habit and "宽松" in text) or ("偏小" in size_habit and "修身" in text and "偏小" not in text):
                score += 5
            if ("偏宽松" in size_habit and "宽松" in text) or ("偏紧" in size_habit and "修身" in text):
                score += 8

        # ---- 全流程增强：品牌/杂牌/预售/讨厌元素/发货地区 ----
        # 品牌偏好-允许：命中加 8
        brands_like = str(self.profile.get("brands_like") or "")
        if brands_like:
            has_rule = True
            for b in re.split(r"[,，、\s]+", brands_like):
                b = b.strip()
                if b and b.lower() in text.lower():
                    score += 8
                    break
        # 品牌偏好-排除：命中扣 25
        brands_dislike = str(self.profile.get("brands_dislike") or "")
        if brands_dislike:
            has_rule = True
            for b in re.split(r"[,，、\s]+", brands_dislike):
                b = b.strip()
                if b and b.lower() in text.lower():
                    score -= 25
                    break
        # 是否接受杂牌：不接受且商品无品牌词扣分
        accept_no_name = str(self.profile.get("accept_no_name") or "").strip()
        if accept_no_name in ("否", "否接受", "不接受", "no", "n"):
            has_rule = True
            # 品牌词粗判：常见品牌大写英文或知名中文品牌
            has_brand = bool(re.search(r"(Nike|Adidas|Apple|Sony|小米|华为|李宁|安踏|优衣库|红米|Bose|New Balance|回力|海澜之家)", text, re.I))
            if not has_brand:
                score -= 10  # 杂牌嫌疑扣分

        # 讨厌的元素（通用兜底）：命中扣 18
        dislike_elements = str(self.profile.get("dislike_elements") or "")
        if dislike_elements:
            has_rule = True
            for e in re.split(r"[,，、\s]+", dislike_elements):
                e = e.strip()
                if e and e.lower() in text.lower():
                    score -= 18

        # 发货地区偏好：命中加 6
        ship_region = str(self.profile.get("ship_region") or "").strip()
        if ship_region and ship_region not in ("不限", "无所谓", "都可以"):
            has_rule = True
            if ship_region in text:
                score += 6

        # 无档案规则时，返回中性分；有规则时做clamp
        if not has_rule:
            return 65.0
        return max(0.0, min(100.0, score))

    # --------- 2. 性价比 ---------
    def _score_value(self, p: Product, budget: Optional[float]) -> float:
        # 折扣力度
        discount_rate = 0.0
        if p.price > 0:
            discount_rate = (p.price - p.final_price) / p.price  # 0~0.5+
        score = 50.0 + min(discount_rate, 0.6) * 80  # 最多+48

        # 销量加成（市场接受度）
        if p.sales >= 100000: score += 10
        elif p.sales >= 50000: score += 7
        elif p.sales >= 10000: score += 4
        elif p.sales >= 1000: score += 2

        # 预算匹配
        if budget and budget > 0:
            if p.final_price <= budget:
                ratio = p.final_price / budget
                # 越接近预算（但不超）越"合理花钱"
                score += 10 * (1 - abs(0.85 - ratio))
            else:
                over = (p.final_price - budget) / budget
                score -= min(over * 100, 30)  # 超预算最多扣30

        return max(0.0, min(100.0, score))

    # --------- 3. 真实口碑 ---------
    def _score_reputation(self, p: Product) -> float:
        rate = p.review.positive_rate  # 0~1
        score = rate * 100
        # 带图追评占比：越高越可信
        total = max(p.review.review_count, 1)
        img_ratio = min(p.review.image_reviews / total, 0.15) / 0.15
        score += img_ratio * 8
        # 评价量太少打折扣
        if p.review.review_count < 200:
            score -= 8
        return max(0.0, min(100.0, score))

    # --------- 4. 差评风险（越高越差） ---------
    def _score_risk(self, p: Product) -> float:
        bad_n = len(p.review.bad_points)
        risk = bad_n * 18
        # 严重关键词（售后、质量）
        severity_words = ["起球", "掉色", "破", "线头", "磨脚", "卡", "发热", "变形", "漏", "发霉"]
        hit = sum(1 for bp in p.review.bad_points for w in severity_words if w in bp)
        risk += hit * 10
        return min(100.0, risk)

    # --------- 5. 发货时效 ---------
    def _score_ship(self, p: Product) -> float:
        days = max(1, p.ship_days)
        score = 100.0 - (days - 1) * 18
        if "自营" in p.seller or "次日达" in " ".join(p.after_sale):
            score += 10
        return max(0.0, min(100.0, score))

    # ========== 推荐主流程 ==========
    def recommend(
        self,
        keyword: str,
        category: Optional[str] = None,
        price_min: Optional[float] = None,
        price_max: Optional[float] = None,
        platforms: Optional[List[str]] = None,
        exclude_tags: Optional[List[str]] = None,
        require_tags: Optional[List[str]] = None,
        topn: int = 3,
    ) -> Tuple[List[Product], List[ScoreBreakdown]]:
        products = self.searcher.search(
            keyword=keyword,
            category=category,
            price_min=price_min,
            price_max=price_max,
            platforms=platforms,
            exclude_tags=exclude_tags,
            require_tags=require_tags,
        )
        if not products:
            return [], []

        budget = price_max
        scored = [(p, self.score_product(p, budget)) for p in products]
        scored.sort(key=lambda x: x[1].total, reverse=True)
        # 多样性：同一平台最多1款进入TOP3
        chosen_p: List[Product] = []
        chosen_s: List[ScoreBreakdown] = []
        used_platforms = set()
        for p, s in scored:
            if len(chosen_p) >= topn:
                break
            if p.platform in used_platforms and len([x for x in chosen_p if x.platform == p.platform]) >= 1:
                # 允许最多2款同平台，但优先拉开
                if len(chosen_p) < 2:
                    pass
                else:
                    continue
            chosen_p.append(p)
            chosen_s.append(s)
            used_platforms.add(p.platform)

        # 补齐
        if len(chosen_p) < topn:
            for p, s in scored:
                if p not in chosen_p:
                    chosen_p.append(p)
                    chosen_s.append(s)
                if len(chosen_p) >= topn:
                    break

        self._last_products = chosen_p
        self._last_scores = {p.pid: s for p, s in zip(chosen_p, chosen_s)}
        return chosen_p, chosen_s

    def recommend_from_products(
        self,
        products: List[Product],
        budget: Optional[float] = None,
        topn: int = 3,
    ) -> Tuple[List[Product], List[ScoreBreakdown]]:
        """对用户粘贴链接抓取到的商品列表打分排序，输出前 topn 条"""
        if not products:
            return [], []
        scored = [(p, self.score_product(p, budget)) for p in products]
        scored.sort(key=lambda x: x[1].total, reverse=True)
        chosen_p = [p for p, _ in scored[:topn]]
        chosen_s = [s for _, s in scored[:topn]]
        self._last_products = chosen_p
        self._last_scores = {p.pid: s for p, s in zip(chosen_p, chosen_s)}
        return chosen_p, chosen_s

    # ========== 输出格式化 ==========
    def format_top(
        self,
        products: List[Product],
        scores: List[ScoreBreakdown],
        extra_require: Optional[str] = None,
    ) -> str:
        if not products:
            return "未找到符合条件的商品，建议调整价格区间或关键词。"

        lines = []
        lines.append(f"**为你选出TOP{len(products)}最优商品**（结合你的个人档案打分）")
        if extra_require:
            lines.append(f"   · 当前筛选条件：{extra_require}")
        lines.append("")

        profile_snapshot = self.profile.get_all()

        # --- AI 增强 1：好评/差评真实摘要（失败或无Key时原样展示原数组） ---
        # 并行执行 3 款商品的摘要 LLM 调用（最多等待 7 秒，超时则用原始规则数组兜底）
        if _AI_AVAILABLE and summarize_reviews_with_llm is not None and products:
            try:
                import concurrent.futures as _cf
                tasks: List[Tuple[int, Product]] = [
                    (i, p) for i, p in enumerate(products)
                    if p.review.good_points or p.review.bad_points
                ]
                if tasks:
                    def _worker(tup):
                        i, p = tup
                        try:
                            s = summarize_reviews_with_llm(p.name,
                                                           list(p.review.good_points),
                                                           list(p.review.bad_points))
                            return (i, s)
                        except Exception:
                            return (i, None)
                    with _cf.ThreadPoolExecutor(max_workers=min(3, len(tasks))) as ex:
                        futs = [ex.submit(_worker, t) for t in tasks]
                        try:
                            for fut in _cf.as_completed(futs, timeout=7):
                                i, s = fut.result()
                                if not s: continue
                                p = products[i]
                                if s.get("good_summary"):
                                    p.review.good_points = [s["good_summary"]]
                                if s.get("bad_summary"):
                                    p.review.bad_points = [s["bad_summary"]]
                        except _cf.TimeoutError:
                            # 超过总预算就取消后续；已返回的应用，未返回的静默放弃
                            for f in futs:
                                f.cancel()
            except Exception:
                pass  # 静默回退

        for idx, (p, s) in enumerate(zip(products, scores), 1):
            src_badge = "[真实数据]" if p.data_source == "真实" else "[演示数据]"
            lines.append(f"### 第{idx}名｜**{p.name}**  `{src_badge}`")
            if p.images:
                lines.append(f"![商品图]({p.images[0]})")
            # 基础信息
            store_info = p.seller or "—"
            lines.append(f"- **基础信息**：{p.platform}  ·  店铺：{store_info}  ·  {p.category or '未分类'}")
            # 买家评价（有几条真数据显几条；无数据如实说明，绝不编造凑数）
            pros = list(p.review.good_points[:3]) if p.review.good_points else []
            if pros:
                lines.append("- **买家好评**：")
                for gp in pros:
                    lines.append(f"  - {gp}")
            else:
                lines.append("- **买家好评**：未抓取到买家评价明细")
            hl = self._core_highlights(p)
            if hl:
                lines.append(f"- **商品卖点**（来自属性信息，非买家评价）：{hl}")
            # 买家差评
            cons = list(p.review.bad_points[:3]) if p.review.bad_points else []
            if cons:
                lines.append("- **买家差评**：")
                for bp in cons:
                    lines.append(f"  - {bp}")
            elif p.review.review_count > 0:
                lines.append(f"- **买家差评**：评价明细未抓取到（平台总评{p.review.review_count}条），无法判断差评情况")
            else:
                lines.append("- **买家差评**：未抓取到评价数据，无法判断差评情况")
            # 好评率统计行仅在确有评价数时输出（避免无数据冒出默认好评率）
            if p.review.review_count > 0:
                lines.append(f"  （好评率{p.review.positive_rate*100:.0f}%｜总评{p.review.review_count}｜带图追评{p.review.image_reviews}）")
            # 价格分析
            if p.price > 0 and p.final_price < p.price:
                drop_pct = (p.price - p.final_price) / p.price * 100
                price_line = (f"- **价格分析**：现价 **¥{p.final_price:.1f}**  ~~原价¥{p.price:.1f}~~"
                              f"  ({p.discount or '无活动'}，降幅{drop_pct:.0f}%)")
            else:
                price_line = f"- **价格分析**：现价 **¥{p.final_price:.1f}**  ({p.discount or '无活动'})"
            price_line += "  · 历史最低价：暂不支持曲线（按设置仅当前价对比）"
            lines.append(price_line)
            # 适配说明
            lines.append(f"- **适配说明**：{self._match_reason(p, profile_snapshot)}")
            # 风险提示
            lines.append(f"- **风险提示**：{self._risk_hints(p, s)}")
            # 0-100 评分
            lines.append(f"- **评分（0-100）**：匹配 **{s.profile_match:.0f}**  ·  性价比 **{s.value:.0f}**  ·  口碑 **{s.reputation:.0f}**  ·  风险 **{100-s.risk:.0f}**  ·  时效 **{s.ship:.0f}**  ·  总分 **{s.total:.0f}**")
            # 购买建议
            advice = "首选" if idx == 1 else ("备选" if idx <= 3 else "谨慎选择")
            lines.append(f"- **购买建议**：{advice}。{'、'.join(p.after_sale) if p.after_sale else '无'}｜预计{p.ship_days}天内发货")
            # 商品链接
            if p.source_url:
                lines.append(f"- **商品链接**：{p.source_url}")
            lines.append("")

        # 横向对比
        lines.append("---")
        lines.append("**横向对比**")
        header = f"| 排名 | 商品简称 | 平台 | 到手价 | 匹配分 | 性价比 | 口碑 | 风险 | 时效 | 总分 |"
        sep = "|---|---|---|---|---|---|---|---|---|---|"
        rows = [header, sep]
        for idx, (p, s) in enumerate(zip(products, scores), 1):
            short = p.name[:10] + ("…" if len(p.name) > 10 else "")
            rows.append(
                f"| {idx} | {short} | {p.platform} | ¥{p.final_price:.0f} "
                f"| {s.profile_match:.0f} | {s.value:.0f} | {s.reputation:.0f} "
                f"| {100-s.risk:.0f} | {s.ship:.0f} | **{s.total:.0f}** |"
            )
        lines.extend(rows)
        lines.append("")

        # --- AI 增强 2：个性化导购点评（单次独立调用，超时硬上限 10 秒） ---
        try:
            if _AI_AVAILABLE and polish_recommendation_with_llm is not None and products:
                import concurrent.futures as _cf
                # 把 items 序列化为 dict 传进去
                items_dict = []
                for i, (p, s) in enumerate(zip(products, scores), 1):
                    items_dict.append({
                        "rank": i,
                        "name": p.name,
                        "platform": p.platform,
                        "seller": p.seller,
                        "final_price": round(p.final_price, 1),
                        "tags": list(p.tags),
                        "highlights": self._core_highlights(p),
                        "good_points": list(p.review.good_points[:3]),
                        "bad_points": list(p.review.bad_points[:3]),
                        "match_reason": self._match_reason(p, profile_snapshot),
                        "score": {
                            "match": int(s.profile_match),
                            "value": int(s.value),
                            "reputation": int(s.reputation),
                            "risk_low": int(100 - s.risk),
                            "ship": int(s.ship),
                            "total": int(s.total),
                        },
                    })
                with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(polish_recommendation_with_llm,
                                    items_dict, (profile_snapshot or {}))
                    try:
                        polished = fut.result(timeout=10)
                        if polished:
                            lines.append("---")
                            lines.append("### AI 导购点评")
                            lines.append(polished.strip())
                            lines.append("")
                    except _cf.TimeoutError:
                        fut.cancel()
        except Exception:
            pass  # 静默回退

        lines.append(
            "**请告诉我：**\n"
            "   · `把第1款加入购物车` / `对比第1款和第2款`\n"
            "   · `查历史价 [链接]` 查询某商品历史最低价\n"
            "   · 粘贴更多商品链接继续对比\n"
            "   · 修改选购条件重新评估"
        )
        return "\n".join(lines)

    # ---------- 文案辅助 ----------
    def _risk_hints(self, p: Product, s: ScoreBreakdown) -> str:
        """生成风险提示：色差/尺码偏差/假货风险/预售等待等"""
        hints: List[str] = []
        text = " ".join(p.tags) + " " + p.name
        # 色差风险（白色/亮色系更易色差）
        if any(c in text for c in ["白色", "米白", "粉色", "亮色"]):
            hints.append("浅色系存在色差风险，建议参考带图追评")
        # 尺码偏差风险
        if any(c in text for c in ["修身", "包臀", "紧身", "偏小", "偏大"]) or "尺码" in " ".join(p.review.bad_points):
            hints.append("版型偏紧/偏松，建议核对尺码表或大一码")
        # 假货风险：非自营且无正品保障
        if p.seller and "自营" not in p.seller and not any(a in p.after_sale for a in ["正品保证", "正品保障", "假一赔十"]):
            hints.append("非自营店铺，注意甄别正品与售后")
        # 预售等待风险
        if p.ship_days and p.ship_days >= 3:
            hints.append(f"发货需{p.ship_days}天，急用请选次日达")
        # 差评风险分高
        if s.risk >= 30:
            hints.append("差评风险偏高，重点看真实短板")
        if not hints:
            hints.append("暂无明显风险提示")
        return "；".join(hints)

    def _core_highlights(self, p: Product) -> str:
        # 优先结合销量、优惠、标签
        parts = []
        if p.discount:
            parts.append(f"活动力度大「{p.discount}」")
        if p.sales >= 100000:
            parts.append(f"全网爆款（月销10w+）")
        elif p.sales >= 10000:
            parts.append(f"热销款（销量{p.sales//10000}w+）")
        # 提取最具代表性的标签
        key_tags = p.tags[:3]
        if key_tags:
            parts.append("主打" + "/".join(key_tags))
        if "自营" in p.seller:
            parts.append("自营发货售后稳")
        return "；".join(parts)

    def _match_reason(self, p: Product, profile: Dict[str, Any]) -> str:
        text = " ".join(p.tags) + " " + p.name
        reasons: List[str] = []
        # 档案字段的中文友好名（用于匹配说明文案）
        field_cn = dict(
            color_like="色彩偏好", color_dislike="色彩避雷",
            style_like="风格偏好", style_dislike="风格避雷",
            material_like="材质偏好", material_dislike="材质避雷",
            fit_like="版型偏好", fit_dislike="版型避雷",
        )

        def find(like_key: Optional[str], dislike_key: Optional[str], lexicon) -> None:
            like_val = profile.get(like_key) if like_key else None
            dis_val = profile.get(dislike_key) if dislike_key else None
            for word, alts in lexicon.items():
                check = [word] + alts
                if like_val and any(x.lower() in str(like_val).lower() for x in check):
                    if any(x.lower() in text.lower() for x in check):
                        label = field_cn.get(like_key or "", "")
                        reasons.append(f"符合你{label}里的「{word}」")
                        break
            if dis_val:
                for word, alts in lexicon.items():
                    check = [word] + alts
                    if any(x.lower() in str(dis_val).lower() for x in check):
                        if any(x.lower() in text.lower() for x in check):
                            label = field_cn.get(dislike_key or "", "避雷项")
                            reasons.append(f"可能踩雷：包含你{label}的「{word}」")
                            break

        find("color_like", "color_dislike", COLOR_KEYWORDS)
        find("style_like", None, STYLE_KEYWORDS)
        find("material_like", "material_dislike", MATERIAL_KEYWORDS)
        find("fit_like", "fit_dislike", FIT_KEYWORDS)

        if not reasons:
            if any(profile.values()):
                return "当前商品与你的档案偏好中性匹配，可结合真实评价进一步判断"
            return "未建立档案，匹配度按中性评分；建议录入偏好后可获得更精准推荐（输入「录入档案」）"
        return "｜".join(reasons)

    # ========= 工具方法：按ID或序号取上次推荐 =========
    def get_last_product(self, index_or_pid) -> Optional[Product]:
        if isinstance(index_or_pid, int) and 1 <= index_or_pid <= len(self._last_products):
            return self._last_products[index_or_pid - 1]
        if isinstance(index_or_pid, str):
            for p in self._last_products:
                if p.pid == index_or_pid or p.name == index_or_pid:
                    return p
        return None

    def last_recommendation_brief(self) -> List[Dict[str, Any]]:
        """上次推荐的紧凑摘要（自由问答的材料源；不含评价全文，控制 token）"""
        brief: List[Dict[str, Any]] = []
        for i, p in enumerate(self._last_products, 1):
            s = self._last_scores.get(p.pid)
            brief.append({
                "rank": i,
                "name": p.name,
                "platform": p.platform,
                "seller": p.seller,
                "final_price": round(p.final_price, 1),
                "total_score": int(s.total) if s else None,
                "good": (list(p.review.good_points) or ["—"])[0],
                "bad": (list(p.review.bad_points) or ["—"])[0],
                "url": p.source_url or "",
            })
        return brief


if __name__ == "__main__":
    from profile_module import ProfileManager
    pm = ProfileManager()
    pm.update_batch({
        "height": "165", "weight": "52",
        "color_like": "白色,粉色", "color_dislike": "荧光色",
        "style_like": "法式,甜美",
        "fit_like": "收腰,修身",
    })
    rec = Recommender(pm)
    prods, scores = rec.recommend("夏天连衣裙", price_max=250)
    print(rec.format_top(prods, scores))
