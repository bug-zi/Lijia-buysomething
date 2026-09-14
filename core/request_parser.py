# -*- coding: utf-8 -*-
"""
购物需求解析器 — 从用户自然语言输入中提炼结构化需求
识别：关键词、品类、预算（min/max）、平台、硬性要求（如材质/风格/版型/排除项）

支持两级：
  1) LLM 模式（可选，当 ai_client 已配置Key）：让大模型产出JSON，再与规则结果合并
  2) 规则模式：纯正则 + 词典，永不依赖网络
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from ai_client import parse_shopping_request_with_llm  # 可选 AI 增强
except Exception:
    parse_shopping_request_with_llm = None


@dataclass
class ShoppingRequest:
    """结构化购物需求"""
    raw: str
    keyword: str = ""                       # 核心搜索词
    category: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    platforms: List[str] = field(default_factory=list)
    require_tags: List[str] = field(default_factory=list)   # 硬性要求属性
    exclude_tags: List[str] = field(default_factory=list)   # 排除属性
    purpose: str = ""                       # 用途
    is_adjustment: bool = False             # 是否为对上次推荐的修改
    target_rank: Optional[int] = None       # 指向"第X款"
    needs_clarify: List[str] = field(default_factory=list)  # 待追问的信息点
    top_n: Optional[int] = None             # 用户指定的最终输出条数（如"排名前5"），None=默认TOP3
    per_platform_n: Optional[int] = None    # 用户指定的每平台候选条数（如"各筛前4名"）

    def summary(self) -> str:
        parts = []
        if self.keyword: parts.append(f"品类/关键词：{self.keyword}")
        budget = ""
        if self.price_min and self.price_max:
            budget = f"¥{self.price_min}~¥{self.price_max}"
        elif self.price_max:
            budget = f"≤¥{self.price_max}"
        elif self.price_min:
            budget = f"≥¥{self.price_min}"
        if budget: parts.append("预算：" + budget)
        if self.platforms: parts.append("平台：" + "/".join(self.platforms))
        if self.require_tags: parts.append("要求：" + "、".join(self.require_tags))
        if self.exclude_tags: parts.append("避雷：" + "、".join(self.exclude_tags))
        if self.top_n: parts.append(f"条数：前{self.top_n}名")
        return "；".join(parts) if parts else "（空）"


PLATFORM_WORDS = {
    "淘宝/天猫": ["淘宝", "天猫", "taobao", "tmall", "tmail"],
    "京东": ["京东", "jd", "京东自营"],
    "拼多多": ["拼多多", "pdd", "百亿补贴"],
    "抖音商城": ["抖音", "抖音商城", "直播间", "dy"],
}

# 典型品类线索词
CATEGORY_HINTS = {
    "连衣裙": ["连衣裙", "裙子", "长裙", "短裙", "半身裙", "旗袍", "裙"],
    "运动鞋": ["运动鞋", "跑鞋", "球鞋", "帆布鞋", "老爹鞋", "篮球鞋", "小白鞋"],
    "耳机": ["耳机", "降噪耳机", "头戴式耳机", "tws", "airpods"],
    "手机": ["手机", "旗舰机", "iphone"],
    "T恤": ["t恤", "polo", "短袖", "体恤", "tee"],
}

# 品类追问问题集（表单化）：core_attrs=关键属性问题（attrs 维度缺失时给出，带选项或自填）；
# extra=次要方向问题（恒 2 问，选填，用户写进补充栏）。预算/用途两问全品类通用。
# 各品类表单总计 = 预算1 + 用途1 + core_attrs 1~2 + extra 2 → 4~6 问。
# 注意：选项文案不得含「数字-数字/数字以上」形态（会被 _parse_budget 误解析为预算区间）。
_BUDGET_CHIPS = ["100以内", "100-300", "300-800", "800-1500", "1500以上"]
_Q_BUDGET = {"key": "budget", "label": "预算大概多少", "options": list(_BUDGET_CHIPS), "multi": False}
_Q_PURPOSE = {"key": "purpose", "label": "主要什么场景用",
              "options": ["自用", "送礼", "办公", "通勤", "运动"], "multi": True}
_CATEGORY_QUESTIONS = [
    {"match": ("杯子", "水杯", "马克杯", "保温杯"),
     "core_attrs": [
         {"key": "material", "label": "什么材质", "options": ["玻璃", "陶瓷", "不锈钢", "塑料", "硅胶"], "multi": True},
         {"key": "capacity", "label": "容量偏好", "options": ["小容量", "中容量", "大容量"], "multi": False},
     ],
     "extra": [{"key": "color", "label": "颜色偏好", "optional": True},
               {"key": "func", "label": "功能款式（带盖/吸管/可挂绳/保温时长等）", "optional": True}]},
    {"match": ("手机壳", "保护壳", "手机套"),
     "core_attrs": [
         {"key": "model", "label": "适配什么手机型号", "options": None, "multi": False},
         {"key": "material", "label": "材质/款式", "options": ["硅胶", "透明", "磁吸", "防摔", "皮革", "挂绳"], "multi": True},
     ],
     "extra": [{"key": "color", "label": "颜色偏好", "optional": True},
               {"key": "detail", "label": "细节要求（镜头保护/边角加厚/轻薄等）", "optional": True}]},
    {"match": ("连衣裙", "裙子", "旗袍"),
     "core_attrs": [
         {"key": "style_fit", "label": "风格/版型", "options": ["法式", "通勤", "甜酷", "收腰", "宽松", "直筒"], "multi": True},
     ],
     "extra": [{"key": "occasion", "label": "具体场合（婚礼/日常/约会等）", "optional": True},
               {"key": "fabric", "label": "面料偏好（雪纺/针织/棉麻等）", "optional": True}]},
    {"match": ("运动鞋", "跑鞋", "球鞋", "帆布鞋", "篮球鞋"),
     "core_attrs": [
         {"key": "shoe_type", "label": "类型/款式", "options": ["跑步", "板鞋", "篮球鞋", "老爹鞋", "小白鞋"], "multi": True},
     ],
     "extra": [{"key": "venue", "label": "使用场地（公路/塑胶跑道/健身房等）", "optional": True},
               {"key": "foot", "label": "脚型特点（宽脚/高足弓/扁平足等）", "optional": True}]},
    {"match": ("耳机", "耳麦"),
     "core_attrs": [
         {"key": "shape", "label": "形态", "options": ["头戴式", "入耳式", "半入耳", "骨传导"], "multi": False},
         {"key": "func", "label": "功能要求", "options": ["降噪", "无线", "长续航", "运动防汗"], "multi": True},
     ],
     "extra": [{"key": "sound", "label": "音质取向（低音/人声/游戏低延迟等）", "optional": True},
               {"key": "wear", "label": "佩戴场景（通勤地铁/久坐办公等）", "optional": True}]},
    {"match": ("手机", "旗舰机"),
     "core_attrs": [
         {"key": "brand", "label": "系统/品牌", "options": ["苹果", "安卓", "华为", "小米", "OPPO", "vivo"], "multi": True},
         {"key": "usage", "label": "主要用途", "options": ["游戏", "拍照", "商务", "长辈用"], "multi": True},
     ],
     "extra": [{"key": "storage", "label": "存储/内存要求（大存储、长续航等）", "optional": True},
               {"key": "condition", "label": "新机/二手接受度", "optional": True}]},
    {"match": ("t恤", "polo", "短袖", "体恤"),
     "core_attrs": [
         {"key": "style_fit", "label": "风格/版型", "options": ["纯棉", "宽松", "修身", "印花", "polo"], "multi": True},
     ],
     "extra": [{"key": "wear_scene", "label": "穿法场景（内搭/外穿/运动等）", "optional": True},
               {"key": "pattern", "label": "图案偏好（纯色/字母/动漫联名等）", "optional": True}]},
]
# 未知品类兜底问题集
_QUESTIONS_GENERIC = {
    "match": (),
    "core_attrs": [
        {"key": "attrs", "label": "有什么具体要求（材质/颜色/规格等）", "options": None, "multi": True},
    ],
    "extra": [{"key": "color", "label": "颜色偏好", "optional": True},
              {"key": "other", "label": "其他要求（尺寸/规格/品牌等）", "optional": True}],
}
_PURPOSE_Q = "主要什么场景用？（如自用/送礼/办公/通勤/运动…）"
_PURPOSE_CHIPS = ["自用", "送礼", "办公", "通勤"]


class RequestParser:

    def parse(self, text: str, *, profile: Optional[Dict[str, Any]] = None,
              previous: Optional["ShoppingRequest"] = None,
              history: Optional[List[Dict[str, str]]] = None) -> ShoppingRequest:
        # 1) 先走规则引擎得到稳定结构
        req = self._parse_rules(text)

        # 2) 尝试 LLM 补充（可选），失败完全不影响结果
        if parse_shopping_request_with_llm is not None:
            try:
                prev_d = self._req_to_dict(previous) if previous else None
                prof_d = dict(profile or {})
                j = parse_shopping_request_with_llm(text, prof_d, prev_d, history=history)
                if j:
                    self._merge_llm_json(req, j)
                    # 若规则没识别到"买第X款"但LLM识别到了，保留LLM的判断
                    if req.target_rank is None and j.get("target_rank"):
                        try: req.target_rank = int(j["target_rank"])
                        except Exception: pass
            except Exception:
                # AI 增强失败静默回退
                pass

        # 3) 档案默认预算上限：用户未在本次输入里明示预算时，取档案 budget_max 作默认
        if req.price_max is None and profile:
            pb = profile.get("budget_max")
            if pb is not None and str(pb).strip() not in ("", "0", "None"):
                try:
                    v = float(pb)
                    if v > 0:
                        req.price_max = v  # 视为预算已有默认值，不再追问
                except (ValueError, TypeError):
                    pass

        # 4) 核心维度缺失标记（预算/用途/品类关键属性），在规则+LLM+档案合并完成后统一计算
        form = self.clarify_form(req)
        req.needs_clarify = [f"{q['label']}？" for q in (form or {}).get("questions", [])]

        return req

    def missing_dims(self, req: "ShoppingRequest") -> List[str]:
        """核心维度完整度判定（纯规则，LLM 不参与门槛）：返回缺失维度名列表。
        dim ∈ budget/purpose/attrs。无品类且无关键词（泛词）时返回空——该场景由品类问答闸负责。"""
        has_target = bool((req.keyword or "").strip()) or bool(req.category)
        if not has_target:
            return []
        dims: List[str] = []
        if req.price_min is None and req.price_max is None:
            dims.append("budget")
        if not (req.purpose or "").strip():
            dims.append("purpose")
        if not req.require_tags:
            dims.append("attrs")
        return dims

    def clarify_form(self, req: "ShoppingRequest") -> Optional[Dict[str, Any]]:
        """结构化澄清表单（网页端追问表单的数据源）：核心缺失维度在前（带选项或自填），
        次要方向问题恒 2 问殿后（选填，用户写进补充栏），总计 4~6 问；无核心缺失返回 None。"""
        dims = self.missing_dims(req)
        if not dims:
            return None
        t = f"{req.category or ''} {req.keyword or ''}".strip().lower()
        cq = _QUESTIONS_GENERIC
        for entry in _CATEGORY_QUESTIONS:
            if any(w in t for w in entry["match"]):
                cq = entry
                break
        questions: List[Dict[str, Any]] = []
        if "budget" in dims:
            questions.append(dict(_Q_BUDGET))
        if "purpose" in dims:
            questions.append(dict(_Q_PURPOSE))
        if "attrs" in dims:
            for q in cq["core_attrs"]:
                questions.append(dict(q))
        for q in cq["extra"]:
            questions.append(dict(q))
        target = (req.category or (req.keyword or "").strip() or "商品")
        return {"target": target, "questions": questions, "skip_text": "直接搜"}

    def _parse_rules(self, text: str) -> ShoppingRequest:
        """原来的 parse 全量逻辑（规则版），改名后不改动内部流程"""
        req = ShoppingRequest(raw=text)
        t = text.strip()
        t_lower = t.lower()

        # 0) 先识别「指向第几款购买」的强意图：开头是"买/确认/就要/就选 第X款/第X个/第X号"时，
        #    不再做需求解析，避免"买第2款"被误识别为搜索"买"。
        buy_first = re.match(
            r"^\s*(买|购买|确认|就要|就选|选定|拍|下单|我要|我买)\s*"
            r"(第\s*(?:[一二三四五六七八九十]|10|[1-9])\s*(款|个|号)|(?:10|[1-9])\s*(款|个|号))",
            t,
        )
        if buy_first:
            req.target_rank = self._parse_rank(t)
            return req

        # 1) 预算解析："200以内 / 300块以下 / 100到200 / 预算500 / ≤300 / 200-300元 / 预算升到300"
        req.price_min, req.price_max = self._parse_budget(t)

        # 1.5) 条数解析："排名前5 / 前10名 / TOP5 / 推荐8款 / 各平台前4名"（用户指定优先于默认TOP3）
        req.top_n, req.per_platform_n = self._parse_top_n(t)

        # 2) 平台识别
        for plat, words in PLATFORM_WORDS.items():
            if any(w.lower() in t_lower for w in words):
                req.platforms.append(plat)

        # 3) 品类识别
        for cat, hints in CATEGORY_HINTS.items():
            for h in hints:
                if h.lower() in t_lower:
                    req.category = cat
                    break
            if req.category:
                break

        # 4) 用途: "送礼/自用/上班/跑步/健身/拍照"
        purpose_words = ["送礼", "礼物", "自用", "上班", "通勤", "跑步", "健身", "运动", "拍照",
                         "约会", "旅游", "婚礼", "面试", "办公", "户外", "车载"]
        for pw in purpose_words:
            if pw in t:
                req.purpose = pw
                break

        # 5) 排除项："不要XX/避开XX/讨厌XX/避雷XX" + "XX太XX，换"结构（如"第二款太宽松" -> 宽松→排除）
        req.exclude_tags = self._parse_exclude(t)
        req.exclude_tags.extend(self._parse_too_x_to_change(t))

        # 6) 硬性要求：风格/材质/版型/颜色 线索词（若已在排除项，则不再加入要求）
        require_candidates = self._parse_require(t)
        excl_set = set(req.exclude_tags)
        req.require_tags = [w for w in require_candidates if w not in excl_set]

        # 7) 指向第几款："第2款" / "买第二款" / "1号"
        req.target_rank = self._parse_rank(t)

        # 8) 是否为修改意见（在推荐结果基础上）："换"/"改"/"不要那么"/"更XX"
        adjust_markers = ["换", "改", "更", "不要那么", "不要太", "调", "换个", "换成", "重新选",
                          "再挑", "再推荐", "换更"]
        if any(m in t for m in adjust_markers) and (req.require_tags or req.exclude_tags or req.price_max):
            req.is_adjustment = True

        # 9) 核心关键词：提取剩余名词短语 — 简单策略：把已知字段去掉，再清理
        req.keyword = self._extract_keyword(t, req)

        # 10) 待追问信息点（needs_clarify）不再在此计算——须等 LLM 增强与档案默认预算
        #     合并完成后才有准确结论，统一移到 parse() 末尾用 missing_dims() 计算
        return req

    # --------- 子方法 ---------
    _CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

    @classmethod
    def _cn_to_int(cls, s: str) -> Optional[int]:
        if s.isdigit():
            return int(s)
        return cls._CN_NUM.get(s)

    def _parse_top_n(self, t: str) -> Tuple[Optional[int], Optional[int]]:
        """解析用户指定的推荐条数。返回 (最终输出条数, 每平台候选条数)，均可为 None。
        安全上限 10（与抓取克制规则一致：每平台每次 ≤10 条）。"""
        n_final: Optional[int] = None
        n_per: Optional[int] = None

        # 每平台候选数："各(筛选/选出…)前4名"
        m_per = re.search(r"各[^前。,，]{0,8}?前\s*([0-9一二三四五六七八九十]+)\s*(?:名|个|款|位)", t)
        if m_per:
            v = self._cn_to_int(m_per.group(1))
            if v:
                n_per = v

        # 最终输出条数：取不在"各…前N"片段内的最后一个"前N"
        # （"淘宝京东各筛前4名…最终排名前5"→前4归每平台、前5归最终；单位可省略，如"排名前5的商品"）
        per_span = m_per.span() if m_per else None
        for mm in re.finditer(r"前\s*([0-9一二三四五六七八九十]+)\s*(?:名|个|款|位|条)?", t):
            if per_span and mm.start() < per_span[1] and mm.end() > per_span[0]:
                continue
            v = self._cn_to_int(mm.group(1))
            if v:
                n_final = v  # 持续覆盖 → 保留最后一个匹配

        if n_final is None:
            m = re.search(r"\btop\s*(\d{1,2})\b", t, re.IGNORECASE)
            if m:
                n_final = int(m.group(1))
        if n_final is None:
            # "推荐/挑/选/给我 + N + 款"（N≥2 才认定，避免"推荐一款"误判）
            m = re.search(r"(?:推荐|挑选?|选出|给?我)\s*(\d{1,2})\s*款", t)
            if m:
                v = int(m.group(1))
                if v >= 2:
                    n_final = v

        if n_final is not None:
            n_final = max(1, min(10, n_final))
        if n_per is not None:
            n_per = max(1, min(10, n_per))
        return n_final, n_per

    def _parse_budget(self, t: str) -> Tuple[Optional[float], Optional[float]]:
        t = t.replace("￥", "¥").replace("元", "").replace("块", "").replace("左右", "")
        lo, hi = None, None
        # 格式1: 100-300 / 100~300 / 100到300
        m = re.search(r"(\d+(?:\.\d+)?)\s*[-～~到至]\s*(\d+(?:\.\d+)?)", t)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            return lo, hi
        # 格式2: 预算升到/调到/改为 XXX 或 预算XXX 或 XXX以内/以下/封顶/不超过/最多/≤XXX
        m = re.search(
            r"(预算|内|以内|以下|封顶|不超|不超过|最多|≤|小于等于|升到|调到|改为|提高到|增加到|改成)\s*"
            r"(\d+(?:\.\d+)?)",
            t,
        )
        if m:
            hi = float(m.group(2))
        else:
            m = re.search(r"(\d+(?:\.\d+)?)\s*(以内|以下|封顶|之内|以内的|以下的)", t)
            if m:
                hi = float(m.group(1))
        # 格式3: ≥XXX / 不低于XXX / XXX以上
        m2 = re.search(r"(不低于|至少|≥|大于等于|以上)\s*(\d+(?:\.\d+)?)", t)
        if m2:
            lo = float(m2.group(2))
        else:
            m2 = re.search(r"(\d+(?:\.\d+)?)\s*(以上|起|起步)", t)
            if m2 and (lo is None):
                lo = float(m2.group(1))
        # 格式4: 纯"预算 200" 或 "预算:200"
        if hi is None and lo is None:
            m = re.search(r"预算\s*[:：]?\s*(\d+(?:\.\d+)?)", t)
            if m:
                hi = float(m.group(1))
        return lo, hi

    def _parse_exclude(self, t: str) -> List[str]:
        res: List[str] = []
        # 用正则切出"不要XX/避开XX/讨厌XX/避雷XX"片段
        # 说明：不再贪婪匹配到标点前的一切，改为「2~8 字内连续词」，避免"不要黑色，京东的"把"京东"当成排除项
        pattern = r"(不要|讨厌|避开|避雷|不想要|别要|避免)\s*([\u4e00-\u9fa5A-Za-z0-9]{2,8})"
        platform_tokens = set()
        for words in PLATFORM_WORDS.values():
            for w in words:
                platform_tokens.add(w)
        for m in re.finditer(pattern, t):
            seg = m.group(2)
            # 过滤: 平台词不算排除项
            if seg in platform_tokens: continue
            # 按分隔符切
            items = re.split(r"[、,，/]", seg)
            for it in items:
                it = it.strip()
                if 1 <= len(it) <= 8:
                    # 过滤纯标点/停用词
                    if it in ("的", "呢", "哦", "啊", "也", "还", "再", "就", "又"): continue
                    res.append(it)
        return res

    def _parse_too_x_to_change(self, t: str) -> List[str]:
        """
        识别"第二款太宽松，换更修身的"模式：
        - "太/有点/略 XX，(换/改/不要/调整/重新)" 中的 XX -> 加入排除
        词表限定在版型/材质/风格等常见属性，避免误伤。
        """
        attr_words = [
            "修身", "收腰", "显瘦", "宽松", "oversize", "紧身", "包臀", "A字",
            "直筒", "阔腿", "高腰", "低腰", "短款", "长款", "长裙", "短裙",
            "厚", "薄", "透", "大", "小", "紧", "长", "短", "重",
            "纯棉", "棉", "雪纺", "针织", "缎面", "真丝", "亚麻", "棉麻", "牛仔",
            "日系", "美式", "法式", "复古", "通勤", "商务", "甜酷", "辣妹",
            "运动", "休闲", "国风", "新中式", "简约", "甜美", "可爱",
            "白色", "米白", "黑色", "粉色", "红色", "蓝色", "绿色", "黄色", "灰色",
            "米色", "卡其", "藏青", "紫色", "碎花", "印花",
        ]
        pattern = r"(?:第?[一二三四五六\d]*[款个号]?\s*)?(?:太|有点|略|过于|不够|不|比较|挺)\s*" \
                  r"([\u4e00-\u9fa5A-Za-z]{1,8})\s*[，,。]?\s*(?:换|改|不要|调整|重新|换更|换个)"
        excludes: List[str] = []
        for m in re.finditer(pattern, t):
            word = m.group(1).strip()
            for aw in attr_words:
                if aw.lower() in word.lower():
                    excludes.append(aw)
                    break
        # 去重保序
        seen = set()
        out = []
        for w in excludes:
            if w not in seen:
                seen.add(w)
                out.append(w)
        return out

    def _parse_require(self, t: str) -> List[str]:
        res: List[str] = []
        # 颜色/风格/版型/材质 — 硬编码常见词表
        color_words = ["白色", "米白", "黑色", "粉色", "红色", "蓝色", "绿色", "黄色", "灰色",
                       "米色", "卡其", "藏青", "紫色", "杏色", "奶茶色", "碎花", "印花"]
        style_words = ["日系", "美式", "法式", "复古", "通勤", "商务", "甜酷", "辣妹",
                       "运动", "休闲", "国风", "新中式", "简约", "甜美", "可爱", "oversize",
                       "街头", "潮牌", "正式", "职业"]
        material_words = ["纯棉", "棉", "雪纺", "针织", "缎面", "真丝", "亚麻", "棉麻",
                          "牛仔", "速干", "皮质", "透气", "玻璃", "陶瓷", "不锈钢", "塑料", "硅胶", "透明"]
        fit_words = ["修身", "收腰", "显瘦", "宽松", "oversize", "紧身", "包臀", "A字",
                     "直筒", "阔腿", "高腰", "低腰", "短款", "长款", "长裙", "短裙", "中裙"]
        func_words = ["降噪", "防水", "无线", "蓝牙", "专业", "旗舰", "长续航", "自拍", "影像",
                      "磁吸", "防摔", "挂绳", "腕带", "带盖", "吸管", "保温"]
        size_words = ["大码", "小码", "加大", "加小", "小个子", "高个"]
        tables = [color_words, style_words, material_words, fit_words, func_words, size_words]
        lower = t.lower()
        for table in tables:
            for w in table:
                if w.lower() in lower:
                    res.append(w)
        # 去重，保序
        seen = set()
        ordered = []
        for w in res:
            if w not in seen:
                seen.add(w)
                ordered.append(w)
        return ordered

    def _parse_rank(self, t: str) -> Optional[int]:
        m = re.search(r"(买|确认|就要|就选)?\s*第\s*([一二三四五六七八九十]|10|[1-9])\s*[款个号]", t)
        if m:
            digit = m.group(2)
            v = self._cn_to_int(digit)
            if v: return v
        # "就1款" / "1号"
        m = re.search(r"(买|确认|就要|就选)\s*(10|[1-9])\s*(款|个|号)?", t)
        if m:
            try: return int(m.group(2))
            except: return None
        return None

    def _extract_keyword(self, t: str, req: ShoppingRequest) -> str:
        s = t
        # 移除数量/价格/平台等干扰词（顺序必须先处理 X-Y 范围，再处理单独带符号的残片如"-300"）
        s = re.sub(r"(购买|帮我|我要|想要|想买|推荐|看看|搜索|查找|给我|请|麻烦)", "", s)
        # 条数短语先整段移除（前N名 / 各筛前N名 / TOP N / 推荐N款），避免数字剥离后留下残片
        s = re.sub(r"各[^。,，\s]{0,8}?前\s*[0-9一二三四五六七八九十]+\s*(?:名|个|款|位|条)?", "", s)
        s = re.sub(r"(?:排名|综合)?前\s*[0-9一二三四五六七八九十]+\s*(?:名|个|款|位|条)?", "", s)
        s = re.sub(r"top\s*\d{1,2}", "", s, flags=re.IGNORECASE)
        s = re.sub(r"(?:推荐|挑选?|选出|给?我)?\s*\d{1,2}\s*款", "", s)
        s = re.sub(r"(各|筛选出?|选出|最合适|综合比较|最终|排名|放在一块)", "", s)
        s = re.sub(r"预算\s*[:：]?\s*\d+(?:\.\d+)?", "", s)
        # 先处理完整范围 200-300 / 200～300 / 200到300
        s = re.sub(r"\d+(?:\.\d+)?\s*[-～~到至]\s*\d+(?:\.\d+)?", "", s)
        # 再单独清掉残留的孤立数字（例如预算200留下的"200"、或误伤出来的"-300"）
        s = re.sub(r"(?<![A-Za-z\u4e00-\u9fa5])[-～~到至]?\s*\d+(?:\.\d+)?(?![A-Za-z\u4e00-\u9fa5])", "", s)
        # 金额单位与范围修饰（含"之间/以内/以下/以上/封顶/不超"）
        s = re.sub(r"\d+(?:\.\d+)?\s*(元|块|以内|以下|以上|封顶|不超|以内的|以下的|以上的|之间)", "", s)
        s = re.sub(r"(左右|之间|上下)", "", s)
        for plat_words in PLATFORM_WORDS.values():
            for w in plat_words:
                # 整词替换，避免误伤夹在中文里的词
                s = re.sub(re.escape(w), "", s)
        for w in list(req.exclude_tags) + ["不要", "讨厌", "避开", "避雷", "不想要", "别要", "避免",
                                          "更", "换", "改", "调", "的话", "一些", "一点", "的吧",
                                          "然后", "的话", "还有", "或者", "什么", "那个", "的", "了",
                                          "和", "与", "及", "着", "过", "啊", "呀", "呢", "哦",
                                          "买", "一条", "一件", "一双", "一个", "一套", "一款",
                                          "需要", "需求", "觉得", "打算", "准备", "东西",
                                          "物品", "商品", "其他"]:
            if w: s = re.sub(re.escape(w), "", s)
        # 移除"第X款"类
        s = re.sub(r"第\s*[一二三四五六1-6]\s*[款个号]", "", s)
        s = re.sub(r"[、,，。.!！?？；;：:·\-\s]+", " ", s).strip()
        # 清理粘在实词前面的单字量词/助词（例如"个通勤运动鞋" → "通勤运动鞋"；
        # "的连衣裙" → "连衣裙"）。循环剥到不再变化为止
        _CJK_STOPS_PREFIX = set("个的了着过呢啊吧吗呀哦嗯和与及就又也都还只给让要到下上")
        changed = True
        while changed:
            changed = False
            if len(s) >= 3 and s[0] in _CJK_STOPS_PREFIX and s[1] not in _CJK_STOPS_PREFIX:
                s = s[1:].lstrip()
                changed = True

        # 要求标签里的属性词 + category 去重（顺序保留 category 在前，属性在后）
        final_parts: List[str] = []
        if req.category:
            final_parts.append(req.category)
        # require_tags 中的属性词（若未出现在 keyword 残部中则追加）
        tail_lower = s.lower()
        for tag in req.require_tags:
            if not tag or len(tag) <= 1: continue
            if tag.lower() in tail_lower or (req.category and tag.lower() in req.category.lower()): continue
            # 只追加非纯数字/预算表达式的属性词
            if re.fullmatch(r"[-～~到至\d.元块]+", tag): continue
            final_parts.append(tag)
        # 额外保留关键字里其余不在 final_parts 的中文名词短语
        existing_lower = " ".join(final_parts).lower()
        cat = (req.category or "").lower()
        # 单字量词/助词/停用字 — 直接丢弃
        _CJK_STOPS = set("个的了着过呢啊吧吗呀哦嗯和与及就又也都还只给让要到下上")
        for chunk in s.split():
            chunk = chunk.strip()
            if len(chunk) < 1: continue
            # 丢弃纯数字/单字虚词
            if chunk.isdigit(): continue
            if len(chunk) == 1 and chunk in _CJK_STOPS: continue
            cl = chunk.lower()
            if cl in existing_lower: continue
            if cat and cl == cat: continue
            # 如果 chunk 包含整个 category 作为尾部（如"法式连衣裙"包含"连衣裙"=cat），
            # 则剥离cat后把剩余部分（如"法式"）加入，避免最终关键词重复
            if cat and cl.endswith(cat) and len(cl) > len(cat):
                rest = chunk[:len(chunk) - len(req.category)].strip(" \t-_")
                if len(rest) == 1 and rest in _CJK_STOPS:
                    continue
                if rest and len(rest) >= 2 and rest.lower() not in existing_lower:
                    final_parts.append(rest)
                    existing_lower += " " + rest.lower()
                continue
            # chunk 若是 "X通勤" 之类单字前缀拼接 + 合法属性词，则切分
            if len(chunk) >= 3 and chunk[0] in _CJK_STOPS and chunk[1:] not in existing_lower:
                rest = chunk[1:]
                if len(rest) >= 2:
                    final_parts.append(rest)
                    existing_lower += " " + rest.lower()
                    continue
            if len(chunk) == 1: continue  # 其他单字丢弃
            final_parts.append(chunk)

        # 去重保序
        seen = set()
        uniq = []
        for x in final_parts:
            if x in seen: continue
            seen.add(x)
            uniq.append(x)
        if uniq:
            return " ".join(uniq)
        return req.category or ""

    # ---------------- AI 辅助合并工具 ----------------
    def _merge_llm_json(self, req: ShoppingRequest, j: Dict[str, Any]) -> None:
        """把 LLM 返回的JSON合并进现有规则解析结果（LLM 有值时覆盖/追加，无值保持规则不变）"""
        # 核心 query/category
        for src, dst, mode in [
            ("query",    "keyword",  "overwrite_if"),
            ("category", "category", "overwrite_if"),
            ("use",      "purpose",  "overwrite_if"),
        ]:
            v = j.get(src)
            if isinstance(v, str) and v.strip():
                if not getattr(req, dst):
                    setattr(req, dst, v.strip())
                elif mode == "overwrite_if" and len(v.strip()) > len(getattr(req, dst)):
                    setattr(req, dst, v.strip())

        # 预算：LLM 给的数值优先（允许 0；0 表示"无上限"或"无下限"这类明确值）
        for src, dst in (("budget_min", "price_min"), ("budget_max", "price_max")):
            v = j.get(src)
            if v is None:
                continue
            if isinstance(v, (int, float)):
                # LLM 明确给了数字（包括 0）都覆盖；负数按 None 处理
                nv = float(v)
                setattr(req, dst, nv if nv >= 0 else None)
            elif isinstance(v, str) and v.strip() == "0":
                setattr(req, dst, 0.0)

        # 平台
        plats = j.get("platforms") or []
        if isinstance(plats, list):
            for p in plats:
                p = str(p).strip()
                if p and p not in req.platforms:
                    req.platforms.append(p)

        # 要求/排除 tag
        def _add_list(arr, name):
            vals = j.get(name) or []
            if not isinstance(vals, list): return
            for v in vals:
                s = str(v).strip()
                if s and s not in arr:
                    arr.append(s)

        _add_list(req.require_tags, "require_tags")
        _add_list(req.exclude_tags, "exclude_tags")

        # --- 去重与净化：避免 query/预算/品类信息重复塞进 require_tags ---
        bag_str = f"{req.keyword}|{req.category or ''}|{req.purpose or ''}"
        def _is_redundant(tag: str) -> bool:
            if not tag: return True
            # 明显预算表达式
            if re.fullmatch(r"[-～~到至\d.元块以下以上以内封顶不超]+", tag): return True
            t = tag.lower()
            b = bag_str.lower()
            # tag 已被 keyword 完全包含，或已包含 category（如 tag="法式连衣裙" 与 category="连衣裙"）
            if t and (t in b or b and (req.category or "").lower() in t and len(t) - len((req.category or "").lower()) <= 2):
                return True
            return False

        req.require_tags = [r for r in req.require_tags if not _is_redundant(r)]
        req.exclude_tags = [r for r in req.exclude_tags if not _is_redundant(r)]

        # 排除项与要求项冲突时优先排除
        excl = set(req.exclude_tags)
        req.require_tags = [r for r in req.require_tags if r not in excl]

        # 条数：规则优先，仅当规则未解析到时采纳 LLM（clamp 1..10，与抓取克制一致）
        for src, dst in (("top_n", "top_n"), ("per_platform_n", "per_platform_n")):
            if getattr(req, dst) is None:
                v = j.get(src)
                if isinstance(v, (int, float)) and v >= 1:
                    setattr(req, dst, int(max(1, min(10, v))))

        # 增量 intent / target_rank / is_adjustment（LLM明确判断时覆盖规则）
        if j.get("is_adjustment") is True:
            req.is_adjustment = True
        if j.get("target_rank"):
            try: req.target_rank = int(j["target_rank"])
            except Exception: pass

    @staticmethod
    def _req_to_dict(r: "ShoppingRequest") -> Dict[str, Any]:
        return {
            "query": r.keyword, "category": r.category,
            "budget_min": r.price_min, "budget_max": r.price_max,
            "platforms": list(r.platforms), "require_tags": list(r.require_tags),
            "exclude_tags": list(r.exclude_tags), "use": r.purpose,
            "is_adjustment": r.is_adjustment, "target_rank": r.target_rank,
        }


if __name__ == "__main__":
    p = RequestParser()
    for case in [
        "买一条夏天的连衣裙，预算200以内，不要黑色，修身的",
        "耳机 降噪 1500以下，京东的",
        "第一款太宽松了，换更修身的，预算升到300",
        "买第2款",
        "推荐个通勤运动鞋，200-400之间",
    ]:
        r = p.parse(case)
        print(f"输入: {case}")
        print(f"  -> {r.summary()}  指向第{r.target_rank}款  修改?={r.is_adjustment}")
        print(f"     require={r.require_tags}  exclude={r.exclude_tags}")
