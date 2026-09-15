# -*- coding: utf-8 -*-
"""
主Agent总控 + 自由对话交互模块（模块B）
完整工作流程：
1. 需求解析 → 2. 跨平台搜索 → 3. 筛选打分 → 4. 输出TOP-N（默认3，用户指定条数优先） → 5. 用户确认 → 6. 下单执行 → 7. 物流跟踪
入口：
  - 编程调用：ChatSession.chat(user_input)
  - 命令行交互：python shopping_agent.py
"""

import os
import re
import sys
import json
from typing import Optional, Tuple, List, Dict, Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "core"))  # 业务模块目录

from profile_module import ProfileManager
from product_searcher import ProductSearcher, Product
from recommender import Recommender
from order_manager import OrderManager, Order, LogisticsEvent
from request_parser import RequestParser, ShoppingRequest
from virtual_cart import VirtualCart
from shopping_list import shopping_list
from account_manager import data_dir

# 视觉多模态分析（可选，未配置 API Key 时回退提示）
try:
    from ai_client import analyze_image_find_similar, analyze_images_compare, get_config as _get_llm_config
    _VISION_AVAILABLE = True
except Exception:
    analyze_image_find_similar = None
    analyze_images_compare = None
    _get_llm_config = None
    _VISION_AVAILABLE = False

HISTORY_MAX_MSGS = 8   # LLM 可见的对话窗口条数（4 轮）


class ChatSession:
    """完整购物对话会话"""

    # 欢迎语
    GREETING = (
        "你好，我是你的**全自动个人购物AI助手**。\n"
        "我可以帮你完成：建立偏好档案 → 跨平台选品 → 评价分析 → TOP3推荐 → 下单模拟 → 物流跟踪 全流程。\n\n"
        "你可以随时说：\n"
        "  · 「录入档案」建立/补充偏好；「查看档案」「修改身高=175」「清空档案」\n"
        "  · 「买一条夏天的连衣裙，预算200以内」直接提需求\n"
        "  · 推荐后可回「买第2款」「换更修身的」「预算升到300」「给我前5名」\n"
        "  · 「我的订单」「物流 ODxxxx」「售后 ODxxxx 尺码不合适」"
    )

    def __init__(self):
        ddir = data_dir()   # 按当前账户上下文取数据目录（无上下文=项目根，行为不变）
        self.profile = ProfileManager(os.path.join(ddir, "user_profile.json"))
        self.searcher = ProductSearcher(use_mock=False)   # 默认开启真实抓取尝试（失败自动回退 Mock）
        self.recommender = Recommender(self.profile, self.searcher)
        self.orders = OrderManager(self.profile, orders_file=os.path.join(ddir, "orders.json"))
        self.parser = RequestParser()
        self.cart = VirtualCart(file_path=os.path.join(ddir, "virtual_cart.json"))
        self.shopping_list = shopping_list   # 购物清单单例（跨会话共享的需求池；内部按上下文切换文件）
        # 会话上下文
        self._collecting_profile = False     # 是否处于分批建档模式
        self._last_request: Optional[ShoppingRequest] = None  # 上一次搜索请求（用于增量调整）
        self._pending_order: Optional[Order] = None           # 待确认的订单草稿
        self._pending_rank: Optional[int] = None              # 待确认购买的序号
        self._cancelled = False                                # 用户是否已取消当前搜索
        self._pending_urls: List[str] = []                    # 被验证码拦截的URL，用于"继续抓取"重试
        self._pending_search = False                          # 搜索流程被登录墙/验证码拦截，待「继续抓取」续跑
        self._pending_clarify: Optional[Dict[str, Any]] = None  # 澄清问答状态 {"text":原需求,"stage":1|2}
        self._last_chips: List[str] = []                      # 最近一次追问的选项按钮（仅供前端渲染）
        self._last_clarify_form: Optional[Dict[str, Any]] = None  # 最近一次澄清表单结构（仅供前端渲染）
        self._history: List[Dict[str, str]] = []              # 最近对话窗口（随会话态持久化，LLM 材料源）

    # ---------- 对外主入口 ----------
    def chat(self, user_input: str) -> str:
        """对外主入口：滑动窗口记录本轮对话（当前输入单独传给解析，不重复计入窗口）"""
        text = (user_input or "").strip()
        reply = self._chat_impl(user_input)
        if text:
            self._history.append({"role": "user", "text": text})
            self._history.append({"role": "ai", "text": reply})
            if len(self._history) > HISTORY_MAX_MSGS:
                del self._history[:len(self._history) - HISTORY_MAX_MSGS]
        return reply

    def _chat_impl(self, user_input: str) -> str:
        text = user_input.strip()
        if not text:
            return self.GREETING

        # 1) 档案命令优先处理（支持档案批量录入）
        if self._collecting_profile:
            # 建档过程中，用户可以说"完成"或跳出
            if text in ("完成", "结束建档", "退出建档", "停止录入",
                        "不想录了", "不录了", "不想录入了", "退出", "暂停"):
                self._collecting_profile = False
                return "档案录入已暂停，随时回复「继续建档」可补充未填项。\n" + self.profile.view_profile()
            # 明确转向新购物需求：用户改主意不想建档了 → 退出向导，转正常购物流程
            if self._BUY_INTENT_RE.match(text):
                self._collecting_profile = False
                return "（已暂停档案录入，随时回「继续建档」接着填）\n\n" + self._chat_impl(text)
            # 向导进行中的旁路意图（查档案/改档案/查订单/查清单/购物车/上下文提问）
            # → 先应答旁路、不推进向导步数，答完附当前进度提示
            side = self._collecting_side_intent(text)
            if side is not None:
                return side + "\n\n" + self.profile.collect_progress_hint()
            resp = self.profile.continue_collect(text)
            if "已收集完成" in resp:
                self._collecting_profile = False
            return resp

        cmd_resp = self.profile.handle_command(text)
        if cmd_resp is not None:
            if "档案录入" in cmd_resp and "第 " in cmd_resp:
                self._collecting_profile = True
            return cmd_resp

        if text in ("继续建档", "继续录入"):
            self._collecting_profile = True
            return self.profile.continue_collect("跳过")

        # 2) 订单 / 物流 / 售后 快捷命令
        order_resp = self._handle_order_commands(text)
        if order_resp is not None:
            return order_resp

        # 2.5) 购物车 / 取消 / 价格对比 / 历史价 等全流程命令
        flow_resp = self._handle_cart_and_flow_commands(text)
        if flow_resp is not None:
            return flow_resp

        # 2.6) 用户粘贴了商品链接 → 逐个抓取详情页 → 打分 → 对比
        urls = self._extract_urls(text)
        if urls:
            return self._flow_grab_and_compare(urls)

        # 2.7) 演示模式：用 Mock 数据展示效果（须用「演示数据」文字标记）
        if text in ("演示模式", "演示", "用演示数据", "演示一下"):
            if self._last_request:
                return self._flow_demo(self._last_request)
            return ("请先提出购物需求（如「买一条连衣裙 预算200」），"
                    "然后再说「演示模式」查看效果。")

        # 2.8) 查历史价 [链接]
        hist_m = re.match(r'(?:查历史价|历史价|查最低价)\s*(https?://.*)', text, re.IGNORECASE)
        if hist_m:
            return self._flow_price_history(hist_m.group(1))

        # 2.9) 澄清问答进行中：本轮回复并入原需求（选择式补全）；强意图指令（买第X款/确认下单）先退出澄清
        if self._pending_clarify:
            if re.match(r"^\s*(?:买|确认|就要|就选|拍|下单|我要)?\s*第\s*[一二三四五六七八九十\d]+\s*[款个号]\s*$", text) \
                    or re.match(r"^(确认|是|好|ok|对|支付|下单)$", text, flags=re.IGNORECASE):
                self._pending_clarify = None
            else:
                return self._flow_clarify_answer(text)

        # 3) 待确认订单二次确认：回复「确认」「是」「OK」
        if self._pending_order is not None:
            if re.match(r"^(确认|是|好|ok|对|买|下单|支付)$", text, flags=re.IGNORECASE):
                o = self._pending_order
                self._pending_order = None
                self._pending_rank = None
                return self.orders.confirm_order(o)
            else:
                self._pending_order = None
                self._pending_rank = None
                return "已取消下单流程，有其他需求可以继续告诉我。"

        # 4) 需求解析（规则 + 可选 AI 增强，失败自动回退）
        profile_dict = self.profile.get_all()
        req = self.parser.parse(text, profile=profile_dict, previous=self._last_request,
                                history=self._history)

        # 4.1 指向第几款购买（强意图优先级最高）：buy_first 短路需求已在 parse() 内
        #     挡住 LLM/档案回填污染；LLM 单独识别出 target_rank 且无其他字段时同样走下单
        strong_rank = req.rank_buy_intent or not (
            req.keyword or req.require_tags or req.exclude_tags or
            req.price_max is not None or req.price_min is not None or
            req.platforms or req.category)
        if req.target_rank is not None and strong_rank:
            return self._flow_confirm_buy(text, req.target_rank)

        # 4.2 如果是调整意见，叠加上次请求
        if req.is_adjustment and self._last_request is not None:
            merged = self._merge_request(self._last_request, req)
            return self._flow_recommend(text, merged)

        # 4.2.5 自由问答兜底：疑似上下文追问（疑问信号）或解析不出明确需求（泛词），
        #       且会话有材料、LLM 可用 → 尝试基于材料答疑；None → 落回下方原流程
        junk_kw = (not str(req.keyword or "").strip()) or \
                  req.keyword.strip() in ("其他", "东西", "商品", "物品")
        req_vague = junk_kw and not req.category and not req.require_tags and not req.purpose
        if self._looks_like_question(text) or req_vague:
            qa = self._try_free_qa(text)
            if qa is not None:
                return qa

        # 4.3 信息不全，主动追问（不盲目搜索）；品类/关键词全缺或只有泛词 → 澄清式品类问答
        #     泛词判定独立于 needs_clarify（LLM 脑补出「其他/东西」等关键词时 needs_clarify 可能为空）
        clarify_hint = ""
        if not req.target_rank and not req.category and junk_kw \
                and not req.require_tags and not req.purpose:
            return self._start_clarify(text)
        # 维度完整度门控：有关键词/品类但缺≥2核心维度（预算/用途/品类关键属性）
        # → 一轮式追问（可回「直接搜」跳过）；应答并入原需求后无论补全与否都搜，绝不无限盘问
        missing = [] if req.target_rank else self.parser.missing_dims(req)
        if len(missing) >= 2:
            return self._start_dims_clarify(text, req)
        if missing == ["budget"]:
            # 仅缺预算不拦截，直搜+提示
            clarify_hint = "\n小提示：暂未识别到预算，我先给出推荐，不合适可以随时调整价格范围。"

        # 4.4 推荐
        resp = self._flow_recommend(text, req)
        if clarify_hint:
            resp = clarify_hint.strip() + "\n\n" + resp
        return resp

    # ---------- 推荐流程（需求直达商品：限量真实搜索 → 打分 TOP3；失败回退粘贴链接） ----------
    # ---------- 澄清式问答（模糊需求 → 选择式补全，规则引擎兜底，不依赖 LLM） ----------
    _CLARIFY_CATEGORIES = ["连衣裙", "运动鞋", "耳机", "T恤", "手机"]
    _CLARIFY_BUDGETS = ["100以内", "100-300", "300-800", "800-1500", "1500以上"]
    _CLARIFY_SKIP = ("随便", "随便推荐", "都行", "不限", "跳过", "先搜", "直接搜", "无所谓")

    # 自由问答：疑问信号（短句+疑问标记）与档案材料白名单（姓名/电话/地址等隐私绝不入 prompt）
    _QA_QUESTION_RE = re.compile(
        r"[??]|为什么|哪个|哪些|怎么|怎么样|好不好|值不值|划算|值得吗|多少|有没有|能不能|可不可以|区别|差别|对比|理由|合适吗|好吗|行吗")

    # 建档向导进行中，明确转向新购物需求的入口（「买/求购/来点…」开头；
    # 「跳过/完成」等向导应答词不在此列，避免误退出）
    _BUY_INTENT_RE = re.compile(
        r"^\s*(?:我|帮我|帮忙|请|麻烦)?\s*(?:想|要|打算|准备|计划|需要)?\s*"
        r"(?:购买|买|求购|来点|来一份|想吃|想喝|搜一下|搜索|查一下|找个|找款)")

    _PROFILE_QA_KEYS = ["height", "weight", "budget_max", "color_like", "color_dislike",
                        "style_like", "style_dislike", "material_like", "material_dislike",
                        "fit_like", "fit_dislike", "size_habit", "brands_like", "brands_dislike",
                        "accept_no_name", "dislike_elements", "ship_region"]

    def _looks_like_question(self, text: str) -> bool:
        """短句 + 疑问信号 → 疑似基于上下文的追问（长句视为正常需求，避免误拦）"""
        t = (text or "").strip()
        return bool(t) and len(t) <= 40 and bool(self._QA_QUESTION_RE.search(t))

    def _collecting_side_intent(self, text: str) -> Optional[str]:
        """建档向导进行中的旁路意图：命中则应答该意图且不推进向导步数。
        只收编无歧义的命令/疑问——向导应答（如「175」「偏宽松」「白色,粉色」）不会被误拦。"""
        resp = self.profile.handle_command(text)
        if resp is not None:
            return resp
        resp = self._handle_order_commands(text)
        if resp is not None:
            return resp
        t = text.strip()
        # 清单/购物车查看（收窄白名单：避开「取消/算了/继续」等可能与向导应答混淆的词）
        if re.match(r"^(我的(购物)?清单|查看清单|购物清单)$", t):
            return self.shopping_list.list_text()
        if re.match(r"^(我的购物车|查看购物车|购物车)$", t):
            return self.cart.list_text()
        # 上下文提问：短句+疑问信号 → 自由问答兜底（无材料/LLM不可用时 None，落回向导应答）
        if self._looks_like_question(text):
            qa = self._try_free_qa(text)
            if qa is not None:
                return qa
        return None

    def _try_free_qa(self, question: str) -> Optional[str]:
        """自由问答兜底：基于会话材料（上次推荐+历史窗口+档案摘要）LLM 答疑。
        返回回答文本；材料缺失/LLM不可用/调用失败/NEED_SEARCH → None（上层落回原流程）。"""
        try:
            last_brief = self.recommender.last_recommendation_brief()
        except Exception:
            last_brief = []
        if not last_brief and not self._history:
            return None
        try:
            import ai_client
            if not ai_client.get_config().get("enabled"):
                return None
            materials = {
                "last_recommendation": last_brief,
                "recent_chat": [{"role": m["role"], "text": m["text"][:100]} for m in self._history],
                # 档案只取购物相关白名单字段
                "profile": {k: self.profile.get(k) for k in self._PROFILE_QA_KEYS if self.profile.get(k)},
            }
            ans = ai_client.answer_free_question_with_llm(question, materials)
        except Exception:
            return None
        if not ans or not ans.strip():
            return None
        if ans.strip().upper().startswith("NEED_SEARCH"):
            return None
        return ans.strip()

    def _start_clarify(self, text: str) -> str:
        self._pending_clarify = {"text": text, "stage": 1}
        self._last_chips = list(self._CLARIFY_CATEGORIES) + ["随便推荐"]
        return (
            "我还没听清你想买什么。**选一个品类**（点下方按钮或直接输入）：\n"
            "连衣裙 / 运动鞋 / 耳机 / T恤 / 手机，也可以输入其他品类；回「随便推荐」就按当前信息直接搜。"
        )

    def _start_dims_clarify(self, text: str, req: "ShoppingRequest") -> str:
        """维度补全追问（一轮式可跳过）：缺≥2核心维度时出结构化表单（4~6问）。
        网页端点选提交、每题可自填、末尾补充栏；纯文字消息保底供 CLI/无表单端使用。
        应答并入原需求后无论补全与否直接搜。"""
        form = self.parser.clarify_form(req)
        if not form:
            return self._flow_recommend(text, req)  # 理论不可达：调用方已保证 missing≥2
        self._pending_clarify = {"text": text, "stage": 3}
        self._last_clarify_form = form
        self._last_chips = []   # 表单取代 chips，避免混排误触即发送
        lines = [f"「{form['target']}」想帮你选得更准，确认几点"
                 "（网页端已生成选项卡：点选后按「提交」，直接文字回复也可以）："]
        n = 0
        for q in form["questions"]:
            n += 1
            if q.get("options"):
                tail = "，可多选" if q.get("multi") else ""
                lines.append(f"{n}. {q['label']}？（{'/'.join(q['options'])}{tail}）")
            else:
                pre = "选填·" if q.get("optional") else ""
                lines.append(f"{n}. {pre}{q['label']}？（可自行填写）")
        lines.append("")
        lines.append("一次答完即可，AI 没问到的可写在网页端补充栏。回「**直接搜**」就按当前信息搜。")
        return "\n".join(lines)

    def pop_clarify_form(self) -> Optional[Dict[str, Any]]:
        """取走最近的澄清表单结构（供 Web 端渲染追问表单；取后即清）"""
        f = self._last_clarify_form
        self._last_clarify_form = None
        return f

    def _flow_clarify_answer(self, answer: str) -> str:
        st = self._pending_clarify or {}
        base_text = str(st.get("text") or "")
        stage = int(st.get("stage") or 1)
        skipped = any(k in answer for k in self._CLARIFY_SKIP)

        # 第一轮：品类应答（跳过则直接搜原需求）
        if stage <= 1:
            if skipped:
                self._pending_clarify = None
                req = self.parser.parse(base_text, profile=self.profile.get_all(),
                                        previous=self._last_request, history=self._history)
                return self._flow_recommend(base_text, req)
            merged = f"{base_text} {answer.strip()}"
            req = self.parser.parse(merged, profile=self.profile.get_all(),
                                    previous=self._last_request, history=self._history)
            kw_ok = bool(req.category) or (req.keyword and req.keyword.strip() not in ("其他", "东西", "商品", "物品"))
            if kw_ok:
                if req.price_max is not None or req.price_min is not None:
                    self._pending_clarify = None
                    return self._flow_recommend(merged, req)
                # 品类已定、预算缺失 → 第二轮问预算
                self._pending_clarify = {"text": merged, "stage": 2}
                self._last_chips = list(self._CLARIFY_BUDGETS) + ["不限预算"]
                return ("好的，品类记下了：**" + (req.category or req.keyword) + "**。预算大概多少？\n"
                        "100以内 / 100-300 / 300-800 / 800-1500 / 1500以上"
                        "（点按钮或直接输入；回「不限预算」直接搜）")
            # 品类没认出来：不卡死，如实告知后按原需求直接搜
            self._pending_clarify = None
            req2 = self.parser.parse(base_text, profile=self.profile.get_all(),
                                     previous=self._last_request, history=self._history)
            return f"没认出「{answer.strip()}」这个品类，我先按原需求「{base_text}」直接搜了。\n\n" + \
                self._flow_recommend(base_text, req2)

        # 第二轮（stage 2 预算应答）与第三轮（stage 3 维度补全应答）同为收尾：
        # 应答并入原需求重解析后直接搜，一轮即止，无论信息补全与否
        merged = base_text if skipped else f"{base_text} {answer.strip()}"
        self._pending_clarify = None
        req = self.parser.parse(merged, profile=self.profile.get_all(),
                                previous=self._last_request, history=self._history)
        return self._flow_recommend(merged, req)

    def pop_chips(self) -> List[str]:
        """取走最近一次追问的选项（供 Web 端渲染按钮；取后即清）"""
        chips = self._last_chips
        self._last_chips = []
        return chips

    def _flow_recommend(self, raw_text: str, req: ShoppingRequest) -> str:
        keyword = req.keyword or req.category or "商品"
        # 记录为上一次请求（后续可基于此调整）
        self._last_request = req
        self._pending_search = False

        # 真实搜索优先（每平台限量 ≤10 条：默认 6，用户指定"各前N"时用 N；登录墙交还人工）
        per_plat = req.per_platform_n if req.per_platform_n else 6
        per_plat = max(2, min(10, per_plat))
        try:
            products, block_reason, need_human = self.searcher.search_real(
                keyword, platforms=req.platforms or None, max_per_platform=per_plat)
        except Exception as e:
            products, block_reason, need_human = [], f"真实搜索异常：{e}", False

        # 登录墙/验证码：暂停，等用户在弹出的浏览器里自行扫码/验证
        if need_human:
            self._pending_search = True
            return (f"已按需求准备搜索「{keyword}」。\n\n"
                    f"{block_reason}\n\n"
                    "完成后回复「**继续抓取**」，我会继续为你搜索并推荐。")

        if products:
            # 无价卡片无法参与比较，先剔除并如实说明
            no_price = [p for p in products if not p.final_price]
            products = [p for p in products if p.final_price]
            if len(products) >= 3:
                if no_price:
                    pass  # 数量少时静默剔除，避免误导性的 ¥0 展示
                products, scores = self.recommender.recommend_from_products(
                    products, budget=req.price_max, topn=req.top_n or 3)
                extra = req.summary() or None
                resp = self.recommender.format_top(products, scores, extra_require=extra)
                return resp + (
                    "\n---\n> 以上来自浏览器实时搜索的**真实商品**（价格/图片为搜索页所见，"
                    "评价等详情未抓取）。想深挖某款，把它的**详情链接**发我，我逐条细抓重新打分。")

        # 搜索不足或失败 → 如实告知 + 回退「粘贴链接」老路径
        lines = [
            f"**需求解析完成**，搜索关键词：`{keyword}`",
            f"   · 筛选条件：{req.summary()}",
        ]
        if block_reason:
            lines.append(f"\n自动搜索未成功：{block_reason}")
        lines += [
            "",
            "**下一步操作**：",
            f"   1. 在浏览器打开 淘宝/京东/拼多多，搜索 `{keyword}`",
            "   2. 浏览搜索结果，把你看中的 **1-5 个商品详情链接** 复制粘贴给我",
            "   3. 我会逐个抓取详情页，提取价格/评价/规格，打分对比后输出 TOP-N（默认3，可指定如「前5名」）",
            "",
            "你也可以说：",
            "   · `演示模式` —— 用演示数据先看效果（标注「演示数据」）",
            "   · 调整预算/条件，如 `预算升到300`",
        ]
        return "\n".join(lines)

    # ---------- 演示模式（Mock 数据，须用「演示数据」文字标记） ----------
    def _flow_demo(self, req: ShoppingRequest) -> str:
        keyword = req.keyword or req.category or "商品"
        products = self.searcher.mock_search(
            keyword=keyword,
            category=req.category,
            price_max=req.price_max,
            require_tags=req.require_tags,
            exclude_tags=req.exclude_tags,
        )
        if not products:
            return f"演示数据中未找到「{keyword}」，建议换个关键词。"
        products, scores = self.recommender.recommend_from_products(
            products, budget=req.price_max, topn=req.top_n or 3)
        self._last_request = req
        extra = req.summary() or None
        resp = self.recommender.format_top(products, scores, extra_require=extra)
        # 醒目标注演示数据
        resp = resp + (
            "\n---\n> ## 以上为演示数据，并非真实商品\n"
            "> 演示数据仅用于展示功能效果，价格/评价均为模拟。\n"
            "> 如需真实商品分析，请把电商商品详情链接粘贴给我。"
        )
        return resp

    # ---------- URL 抓取对比流程 ----------
    def _extract_urls(self, text: str) -> List[str]:
        """从用户输入中提取商品详情链接"""
        url_pattern = r'https?://[^\s<>"\']+(?:item\.taobao|tmall|jd\.com|yangkeduo|pinduoduo|detail)[^\s<>"\']*'
        urls = re.findall(url_pattern, text, re.IGNORECASE)
        # 也匹配纯链接
        if not urls:
            url_pattern2 = r'https?://[^\s<>"\']+'
            urls = re.findall(url_pattern2, text, re.IGNORECASE)
        # 去重，保留顺序
        seen = set()
        unique = []
        for u in urls:
            if u not in seen and any(kw in u.lower() for kw in
                                     ["taobao", "tmall", "jd.com", "yangkeduo",
                                      "pinduoduo", "detail", "item"]):
                seen.add(u)
                unique.append(u)
        return unique[:5]

    def _flow_grab_and_compare(self, urls: List[str]) -> str:
        """逐个抓取用户粘贴的商品链接，打分对比后输出 TOP-N（默认3，可指定如「前5名」）"""
        lines = [f"收到 {len(urls)} 个商品链接，正在用真实浏览器逐个抓取……"]
        lines.append("（请保持浏览器窗口可见，如遇验证码请手动完成并回复「继续抓取」）")
        lines.append("")

        # 逐个抓取
        products, block_reason = self.searcher.grab_from_urls(urls)
        # 检测到验证码/滑块拦截 → 保存URL以便"继续抓取"重试
        if block_reason and "继续抓取" in block_reason:
            self._pending_urls = list(urls)
        if not products:
            if block_reason:
                return (f"抓取失败：{block_reason}\n\n"
                        "请检查链接是否正确，或在浏览器登录后重试。")
            return "所有链接均抓取失败，未获取到有效商品数据。"

        # 初筛过滤
        budget = None
        if self._last_request:
            budget = self._last_request.price_max
        if budget:
            before = len(products)
            products = [p for p in products if p.final_price <= budget]
            if before > len(products):
                lines.append(f"初筛：过滤了 {before - len(products)} 个超出预算的商品")
                lines.append("")

        if not products:
            return f"初筛后无商品符合条件（预算 ¥{budget}），请放宽预算或换链接。"

        # 打分排序
        last_topn = self._last_request.top_n if self._last_request else None
        products, scores = self.recommender.recommend_from_products(
            products, budget, topn=last_topn or 3)
        extra = None
        if self._last_request:
            extra = self._last_request.summary()
        resp = self.recommender.format_top(products, scores, extra_require=extra)

        # 如果有部分被拦截，追加提示
        if block_reason:
            resp = resp + f"\n---\n部分链接抓取被拦截：{block_reason}"
        return "\n".join(lines) + resp

    # ---------- 历史价格查询 ----------
    def _flow_price_history(self, url: str) -> str:
        """查询单个商品链接的历史最低价"""
        try:
            import web_scraper
            data = web_scraper.get_price_history(url)
        except Exception as e:
            return f"历史价格查询失败：{e}"

        lines = ["**历史价格查询结果**"]
        lines.append(f"- 商品链接：{url}")
        lines.append(f"- 当前价格：**¥{data.get('current_price', 0):.1f}**")
        lines.append(f"- 近3个月最低价：{data.get('history_low_3m') or '暂无数据'}")
        lines.append(f"- 近6个月最低价：{data.get('history_low_6m') or '暂无数据'}")
        lines.append(f"- 近12个月最低价：{data.get('history_low_12m') or '暂无数据'}")
        if data.get("low_date"):
            lines.append(f"- 最低价日期：{data['low_date']}")
        lines.append(f"- 当前价位评估：**{data.get('level', '未知')}**")
        lines.append(f"- 数据来源：{data.get('source', '未知')}")
        level = data.get("level", "")
        if level == "好价":
            lines.append("- 建议：当前接近历史最低价，可考虑入手。")
        elif level == "高价":
            lines.append("- 建议：当前价格偏高，建议等待降价或寻找替代品。")
        else:
            lines.append("- 建议：价格处于正常区间，按需购买即可。")
        lines.append("")
        lines.append("你可以：把这个链接加入购物车 `把当前商品加入购物车`，或继续粘贴更多链接对比。")
        return "\n".join(lines)

    def _merge_request(self, base: ShoppingRequest, delta: ShoppingRequest) -> ShoppingRequest:
        """合并「增量调整」请求到上一次请求"""
        merged = ShoppingRequest(raw=delta.raw)
        merged.keyword = delta.keyword or base.keyword
        merged.category = delta.category or base.category
        merged.price_min = delta.price_min if delta.price_min is not None else base.price_min
        merged.price_max = delta.price_max if delta.price_max is not None else base.price_max
        merged.platforms = delta.platforms or list(base.platforms)
        merged.purpose = delta.purpose or base.purpose
        merged.is_adjustment = False
        # 条数：用户本次明示则用新值，否则沿用上次
        merged.top_n = delta.top_n if delta.top_n is not None else base.top_n
        merged.per_platform_n = delta.per_platform_n if delta.per_platform_n is not None else base.per_platform_n
        # 要求/排除取并集，重复去重
        merged.require_tags = list(dict.fromkeys(list(base.require_tags) + list(delta.require_tags)))
        merged.exclude_tags = list(dict.fromkeys(list(base.exclude_tags) + list(delta.exclude_tags)))
        # 排除项中若与要求冲突，优先排除
        # 常见冲突: 宽松 vs 修身
        conflicts = [("宽松", "修身"), ("长款", "短款"), ("宽松", "紧身")]
        for a, b in conflicts:
            if a in merged.require_tags and b in merged.require_tags:
                # delta中的后出现的保留
                if a in delta.require_tags:
                    merged.require_tags.remove(b)
                else:
                    merged.require_tags.remove(a)
        return merged

    # ---------- 购买确认流程 ----------
    def _flow_confirm_buy(self, raw_text: str, rank: int) -> str:
        product = self.recommender.get_last_product(rank)
        if product is None:
            return (
                f"没有找到第{rank}款商品，可能是还没做过推荐。\n"
                "请先告诉我你的购物需求，例如「买连衣裙 预算200」。"
            )
        # 构建订单草稿
        try:
            draft = self.orders.build_order(product)
        except ValueError as e:
            return f"{e}"

        # 展示确认信息
        self._pending_order = draft
        self._pending_rank = rank
        lines = []
        lines.append(f"**准备下单第{rank}款商品，请核对以下信息：**")
        lines.append(f"- 商品：**{product.name}**")
        lines.append(f"- 平台/店铺：{product.platform} · {product.seller}")
        lines.append(f"- 价格：原价¥{product.price:.1f}，活动到手 **¥{product.final_price:.1f}**（{product.discount or '无活动'}）")
        lines.append(f"- 收货：{draft.receiver} {draft.phone}")
        lines.append(f"- 地址：{draft.address}")
        lines.append("")
        lines.append(
            f"**支付风险提醒**：本系统**不会**代你支付任何费用；"
            f"确认后仅做下单流程模拟（返回订单号），真实支付请自行前往{product.platform}官方平台完成。"
        )
        lines.append("")
        lines.append(f"**是否确认购买第{rank}款商品？确认后将进入下单流程。**（回复「确认」执行，其他内容则取消）")
        return "\n".join(lines)

    # ---------- 订单/物流/售后命令处理 ----------
    def _handle_order_commands(self, text: str) -> Optional[str]:
        t = text.strip()

        # 我的订单
        if t in ("我的订单", "订单", "全部订单"):
            return self.orders.list_orders()

        # 物流（裸词"物流"/"快递"/"查物流"/"查快递"也匹配）
        m = re.match(r"^(物流|快递|查物流|查快递)(?:\s*[:：]?\s*(OD?\w*))?$", t, flags=re.IGNORECASE)
        if m:
            oid = m.group(2) if m.lastindex and m.lastindex >= 2 else None
            if not oid:
                all_ids = sorted(self.orders.orders.keys(), reverse=True)
                if not all_ids:
                    return "暂无订单，无法查询物流。请先下单后再查询。"
                oid = all_ids[0]
            return self.orders.query_logistics(oid)

        # 售后 / 退货 / 退换
        if re.match(r"^售后\b|^退货\b|^退换\b|^售后申请|^申请售后", t):
            m = re.match(r"^(售后|退货|退换|售后申请|申请售后)\s*[:：]?\s*(OD?\w*)?\s*(.*)", t)
            oid = m.group(2) if m else None
            reason = (m.group(3).strip() if m else "") or "未填写"
            if not oid:
                all_ids = sorted(self.orders.orders.keys(), reverse=True)
                if not all_ids:
                    return "暂无订单，无法发起售后。"
                oid = all_ids[0]
            return self.orders.apply_after_sale(oid, reason)

        # 取消订单
        m = re.match(r"^取消(?:订单)?\s*[:：]?\s*(OD?\w*)", t)
        if m:
            oid = m.group(1)
            if not oid:
                return "请告诉我要取消的订单号，格式：「取消 ODxxxx」"
            reason = ""
            tail = t.replace(m.group(0), "", 1).strip("，,。 ")
            if tail:
                reason = tail
            return self.orders.cancel_order(oid, reason)

        # 比价 / 优惠券 / 售后政策 — 在推荐基础上简单响应
        if t in ("比价", "对比价格", "看优惠券", "有什么券", "售后政策"):
            last = self.recommender._last_products
            if not last:
                return "请先做一次推荐，然后我会给你列出各款的优惠活动与售后政策。"
            lines = [f"**{t}对比**（当前TOP{len(last)}）"]
            for i, p in enumerate(last, 1):
                lines.append(
                    f"- 第{i}名｜{p.name[:18]}…｜{p.platform}\n"
                    f"  原价¥{p.price:.1f} → 到手¥{p.final_price:.1f} | 优惠：{p.discount or '无'}\n"
                    f"  售后：{'、'.join(p.after_sale) if p.after_sale else '无'} | 库存{p.stock} | {p.ship_days}天内发货"
                )
            return "\n".join(lines)

        return None

    # ---------- 购物车 / 取消 / 价格对比 / 历史价 ----------
    def _handle_cart_and_flow_commands(self, text: str) -> Optional[str]:
        t = text.strip()

        # 取消当前搜索（开放式中断）
        if t in ("停止", "取消", "取消这次搜索", "取消搜索", "算了", "不要了", "停下"):
            self._cancelled = True
            self._last_request = None
            return ("已取消当前搜索任务。\n"
                    "如需重新开始，告诉我新的购物需求即可（如「买一条连衣裙 预算200」）。")

        # 继续抓取：验证码/滑块手动完成后用户回复此指令，重试真实抓取
        if t in ("继续抓取", "继续抓", "验证完成", "已完成验证", "继续"):
            # 优先重试被验证码拦截的URL列表
            if self._pending_urls:
                urls = list(self._pending_urls)
                self._pending_urls = []
                return (f"正在用真实浏览器重新抓取 {len(urls)} 个被拦截的链接……\n"
                        "（请保持浏览器窗口可见，如再次出现验证码，请手动完成并回复「继续抓取」）\n\n"
                        + self._flow_grab_and_compare(urls))
            # 搜索流程被登录墙/验证码拦截 → 续跑搜索
            if self._pending_search and self._last_request is not None:
                self._pending_search = False
                return ("登录/验证已完成，正在继续搜索……\n"
                        "（请保持浏览器窗口可见，如再次遇到验证码请手动完成并回复「继续抓取」）\n\n"
                        + self._flow_recommend(t, self._last_request))
            if self._last_request is None:
                return "当前没有挂起的搜索任务。请先提出购物需求（如「买一条连衣裙 预算200」）。"
            req = self._last_request
            kw = req.keyword or req.category or "商品"
            return (f"当前没有待重试的商品链接。上次需求关键词：`{kw}`\n"
                    "请把你在浏览器搜索到的 **1-5 个商品详情链接** 粘贴给我，我会逐个抓取对比。")

        # 安装浏览器驱动：patchright 优先（playwright 兜底），供真实搜索/抓取使用
        if t in ("安装浏览器驱动", "安装驱动", "安装patchright", "安装 patchright",
                 "装浏览器驱动", "安装playwright", "安装 playwright",
                 "装playwright", "装 playwright"):
            return self._install_browser_driver()

        # --- 购物清单指令 ---
        # 必须在购物车指令块之前匹配：购物车的「移除第N项」正则会误吞「从清单移除第N项」
        m = re.match(r"记到清单[:：]\s*(.+)", t)
        if m:
            return self.shopping_list.add(m.group(1).strip())
        if t in ("我的清单", "展示我的清单", "查看清单", "购物清单", "我的购物清单"):
            return self.shopping_list.list_text()
        m = re.search(r"从清单移除\s*第?\s*(\d+)\s*项?", t)
        if m:
            return self.shopping_list.remove(int(m.group(1)))
        m = re.search(r"(?:取消完成|恢复)\s*第?\s*(\d+)\s*项?", t)
        if m:
            return self.shopping_list.toggle_done(int(m.group(1)))
        m = re.search(r"(?:标记完成|完成)\s*第?\s*(\d+)\s*项?", t)
        if m:
            return self.shopping_list.toggle_done(int(m.group(1)))

        # --- 虚拟购物车指令 ---
        # 加入购物车：把第N款加入购物车 / 加入购物车第N款 / 收藏第N款
        m = re.search(r"(?:把|将)?\s*第\s*(\d+)\s*款\s*(?:加入购物车|加入收藏|加入收藏购物车|收藏)", t)
        if not m:
            m = re.search(r"(?:加入购物车|加入收藏|收藏)\s*第?\s*(\d+)\s*款?", t)
        if m:
            rank = int(m.group(1))
            p = self.recommender.get_last_product(rank)
            if p is None:
                return f"没有找到第{rank}款商品，请先做一次推荐。"
            return self.cart.add(p.name, p.platform, p.source_url or "", p.final_price, pid=p.pid)

        # 展示购物车
        if t in ("展示我的购物车", "我的购物车", "购物车", "查看购物车", "收藏夹", "我的收藏"):
            return self.cart.list_text()

        # 清空购物车
        if t in ("清空购物车", "清空收藏", "清空收藏夹"):
            return self.cart.clear()

        # 监控第N项 / 取消监控第N项 / 全部监控
        m = re.search(r"(?:取消监控|关闭监控|停止监控)\s*第?\s*(\d+)\s*项?", t)
        if m:
            idx = int(m.group(1))
            if not (1 <= idx <= len(self.cart.items)):
                return f"购物车没有第{idx}项"
            if self.cart.items[idx - 1].monitor:       # 已开启才关闭
                self.cart.toggle_monitor(idx)
            return self.cart.list_text()
        m = re.search(r"(?:监控|价格监控|开启监控)\s*第?\s*(\d+)\s*项?", t)
        if m:
            idx = int(m.group(1))
            if not (1 <= idx <= len(self.cart.items)):
                return f"购物车没有第{idx}项"
            if not self.cart.items[idx - 1].monitor:   # 未开启才开启
                self.cart.toggle_monitor(idx)
            return self.cart.list_text()
        if t in ("监控购物车", "监控全部", "全部监控", "监控购物车里商品的价格"):
            self.cart.monitor_all(True)
            return "已开启购物车全部商品的价格监控。\n" + self.cart.list_text()

        # 从购物车移除第N项（明确要求量词"项"或"个"，避免误匹配"删除第N款"）
        m = re.search(r"(?:从购物车移除|移除|删除)\s*第?\s*(\d+)\s*(?:项|个)", t)
        if m:
            idx = int(m.group(1))
            return self.cart.remove(idx)

        # 给第N项添加备注：xxx
        m = re.search(r"(?:给|为)?\s*第?\s*(\d+)\s*项\s*(?:添加备注|备注|加备注)[:：]?\s*(.+)", t)
        if m:
            idx = int(m.group(1))
            note = m.group(2).strip()
            return self.cart.set_note(idx, note)

        # --- 价格对比 / 历史价 ---
        # 对比第X和第Y（商品）
        m = re.search(r"对比\s*第?\s*(\d+)\s*(?:个|款)?[和与及还有]\s*第?\s*(\d+)\s*(?:个|款)?\s*(?:商品)?", t)
        if m:
            return self._compare_two_products(int(m.group(1)), int(m.group(2)))
        # 查历史最低价 / 历史价
        if re.search(r"(历史最低价|历史价|查.*历史|最低价)", t):
            m = re.search(r"第?\s*(\d+)\s*(?:款|个|项)?", t)
            if m:
                return self._price_compare(int(m.group(1)))
            return ("**历史价格查询**\n"
                    "历史价格曲线暂不支持（按设置仅做当前价对比）。\n"
                    "可用指令：「查一下第1款的历史最低价」「对比一下第1个和第2个商品」。")

        # 价格监控提醒主动触发
        if t in ("检查价格", "查价格变动", "价格监控", "查监控"):
            alerts = self.cart.check_prices()
            if not alerts:
                return "已检查购物车监控商品，暂无降价提醒。\n（真实抓取若被拦截则跳过该项）"
            return "**价格变动提醒**\n" + "\n".join(alerts)

        return None

    def _compare_two_products(self, rank_a: int, rank_b: int) -> str:
        """对比两个上次推荐商品（当前价对比，无历史曲线）"""
        pa = self.recommender.get_last_product(rank_a)
        pb = self.recommender.get_last_product(rank_b)
        if not pa or not pb:
            return f"没有找到第{rank_a}或第{rank_b}款商品，请先做一次推荐。"
        lines = [f"**第{rank_a}款 vs 第{rank_b}款 对比**（当前价对比，历史曲线暂不支持）"]
        lines.append("| 项目 | {} | {} |".format(f"第{rank_a}款", f"第{rank_b}款"))
        lines.append("|---|---|---|")
        lines.append(f"| 名称 | {pa.name} | {pb.name} |")
        lines.append(f"| 平台 | {pa.platform} | {pb.platform} |")
        lines.append(f"| 现价 | ¥{pa.final_price:.1f} | ¥{pb.final_price:.1f} |")
        lines.append(f"| 原价 | ¥{pa.price:.1f} | ¥{pb.price:.1f} |")
        lines.append(f"| 优惠 | {pa.discount or '无'} | {pb.discount or '无'} |")
        lines.append(f"| 好评率 | {pa.review.positive_rate*100:.0f}% | {pb.review.positive_rate*100:.0f}% |")
        lines.append(f"| 发货 | {pa.ship_days}天 | {pb.ship_days}天 |")
        lines.append(f"| 数据来源 | {pa.data_source} | {pb.data_source} |")
        lines.append("")
        cheaper = pa if pa.final_price <= pb.final_price else pb
        lines.append(f"当前价更低：第{rank_a if cheaper is pa else rank_b}款（¥{cheaper.final_price:.1f}）")
        lines.append("如需加入虚拟购物车监控价格，回「把第N款加入购物车」。")
        return "\n".join(lines)

    def _price_compare(self, rank: int) -> str:
        """查某款商品的当前价对比（替代历史最低价查询）"""
        p = self.recommender.get_last_product(rank)
        if p is None:
            return f"没有找到第{rank}款商品，请先做一次推荐。"
        lines = [f"**第{rank}款价格查询**：{p.name}"]
        lines.append(f"- 平台：{p.platform}  ·  店铺：{p.seller}")
        lines.append(f"- 当前价：**¥{p.final_price:.1f}**  ~~原价¥{p.price:.1f}~~  ({p.discount or '无活动'})")
        lines.append(f"- 数据来源：{p.data_source}")
        lines.append("- 历史最低价：暂不支持历史曲线（按设置仅做当前价对比）。")
        lines.append("- 是否值得入手：综合好评率、当前折扣与档案匹配度判断；如需跨平台对比，可回「对比一下第1个和第2个商品」。")
        lines.append("如需价格监控，回「把第1款加入购物车」后再「监控第1项」。")
        return "\n".join(lines)

    def _install_browser_driver(self) -> str:
        """安装浏览器驱动：patchright 优先（默认源失败切清华镜像），playwright 兜底；
        本机无 Chrome/Edge 时按需下载 Chromium 内核"""
        import subprocess

        def _pip_install(pkg: str, mirror: bool = False):
            cmd = [sys.executable, "-m", "pip", "install", pkg, "--quiet"]
            if mirror:
                cmd += ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"]
            return subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        # 1) 驱动包：patchright 默认源 → patchright 清华镜像 → playwright 兜底
        installed = ""
        errors: List[str] = []
        for pkg, mirror in (("patchright", False), ("patchright", True),
                            ("playwright", False), ("playwright", True)):
            label = pkg + ("（清华镜像）" if mirror else "")
            try:
                r = _pip_install(pkg, mirror)
            except subprocess.TimeoutExpired:
                errors.append(f"{label} 超时")
                continue
            if r.returncode == 0:
                installed = pkg
                break
            errors.append(f"{label} 失败（退出码 {r.returncode}）")
        if not installed:
            return ("驱动包安装失败（已尝试默认源与清华镜像）。\n"
                    + "\n".join(f"· {e}" for e in errors[-2:])
                    + "\n请手动运行：`pip install patchright`")

        # 2) 浏览器：本机有 Chrome/Edge 则无需下载内核
        try:
            import web_scraper
            if web_scraper._find_system_browser():
                return self._verify_browser_driver(
                    installed, "检测到本机 Chrome/Edge，无需下载浏览器内核。")
        except Exception:
            pass
        try:
            r = subprocess.run([sys.executable, "-m", installed, "install", "chromium"],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                return (f"驱动包 {installed} 已装好，但 Chromium 下载失败。\n"
                        f"请手动运行：`python -m {installed} install chromium`，"
                        "或本机安装 Chrome/Edge 后无需下载。")
        except subprocess.TimeoutExpired:
            return (f"Chromium 下载超时（10 分钟）。驱动包 {installed} 已装好，"
                    f"可稍后手动运行 `python -m {installed} install chromium`。")
        return self._verify_browser_driver(installed, "已下载 Chromium 内核。")

    def _verify_browser_driver(self, pkg: str, note: str) -> str:
        try:
            import web_scraper
            if web_scraper._has_playwright() and web_scraper._check_browser_binaries():
                return (f"浏览器驱动就绪（{pkg}）。{note}\n"
                        "现在直接输入购物需求即可真实搜索；遇登录墙会弹出浏览器让你扫码"
                        "（登录一次后短期免登），遇验证码我会暂停交还人工，回复「继续抓取」续跑。")
        except Exception:
            pass
        return f"安装命令已执行（{pkg}），请重启服务后生效。"

    # ---------- 会话状态导出（用于调试/可视化） ----------
    def snapshot(self) -> Dict[str, Any]:
        # 懒加载ai_client避免循环导入
        ai_cfg = {}
        try:
            import ai_client
            ai_cfg = ai_client.get_config()
        except Exception:
            pass
        return {
            "profile": self.profile.get_all(),
            "last_request": self._last_request.summary() if self._last_request else None,
            "pending_order_id": self._pending_order.order_id if self._pending_order else None,
            "orders": [oid for oid in self.orders.orders.keys()],
            "cart_count": len(self.cart.items),
            "data_source_blocked": bool(getattr(self.searcher, "_last_block_reason", "")),
            "llm": ai_cfg,
            "vision_enabled": _VISION_AVAILABLE and (ai_cfg.get("enabled") if ai_cfg else False),
        }

    # ---------- 会话状态序列化（多会话持久化，配合 session_store） ----------
    def export_state(self) -> Dict[str, Any]:
        """导出会话上下文（会话态字段）；档案/订单/购物车已有各自 JSON 持久化，不在此列"""
        return {
            "collecting_profile": bool(self._collecting_profile),
            "last_request": self._shopping_request_to_dict(self._last_request),
            "pending_order": self._pending_order.to_dict() if self._pending_order is not None else None,
            "pending_rank": self._pending_rank,
            "cancelled": bool(self._cancelled),
            "pending_urls": list(self._pending_urls or []),
            "pending_search": bool(self._pending_search),
            "pending_clarify": dict(self._pending_clarify) if self._pending_clarify else None,
            "history": [dict(m) for m in (self._history or [])],
        }

    @staticmethod
    def _shopping_request_to_dict(req: Optional[ShoppingRequest]) -> Optional[Dict[str, Any]]:
        """ShoppingRequest 是纯 dataclass，直接 asdict；异常时降级为 None（不影响主流程）"""
        if req is None:
            return None
        try:
            from dataclasses import asdict
            return asdict(req)
        except Exception:
            return None

    def restore_state(self, state: Dict[str, Any]) -> None:
        """从持久化字典恢复会话上下文；无法识别的字段静默忽略，异常时置空（兜底红线：功能不中断）"""
        if not isinstance(state, dict) or not state:
            return
        try:
            self._collecting_profile = bool(state.get("collecting_profile", False))
            self._cancelled = bool(state.get("cancelled", False))
            self._pending_search = bool(state.get("pending_search", False))
            rank = state.get("pending_rank")
            self._pending_rank = int(rank) if isinstance(rank, int) else None
            self._pending_urls = [str(u) for u in (state.get("pending_urls") or []) if u]
            pc = state.get("pending_clarify")
            self._pending_clarify = dict(pc) if isinstance(pc, dict) and pc.get("text") else None
            h = state.get("history")
            self._history = []
            if isinstance(h, list):
                for m in h:
                    if isinstance(m, dict) and m.get("role") in ("user", "ai") and m.get("text"):
                        # 无损恢复（export/restore 严格一致）；进 prompt 时另有 100 字截断
                        self._history.append({"role": str(m["role"]), "text": str(m["text"])})
        except Exception:
            # 手改文件导致个别字段不可恢复时整体放行，不让恢复动作中断服务
            pass

        # 上一次购物需求：按 dataclass 字段逐一重建，失败置 None
        d = state.get("last_request")
        self._last_request = None
        if isinstance(d, dict):
            try:
                self._last_request = ShoppingRequest(
                    raw=str(d.get("raw") or ""),
                    keyword=str(d.get("keyword") or ""),
                    category=d.get("category"),
                    price_min=d.get("price_min"),
                    price_max=d.get("price_max"),
                    platforms=[str(p) for p in (d.get("platforms") or [])],
                    require_tags=[str(t) for t in (d.get("require_tags") or [])],
                    exclude_tags=[str(t) for t in (d.get("exclude_tags") or [])],
                    purpose=str(d.get("purpose") or ""),
                    is_adjustment=bool(d.get("is_adjustment", False)),
                    target_rank=d.get("target_rank"),
                    needs_clarify=[str(x) for x in (d.get("needs_clarify") or [])],
                    top_n=d.get("top_n"),
                    per_platform_n=d.get("per_platform_n"),
                )
            except Exception:
                self._last_request = None

        # 待确认订单草稿：按字段重建（events 逐条重建），失败丢弃该草稿
        od = state.get("pending_order")
        self._pending_order = None
        if isinstance(od, dict) and od.get("order_id"):
            try:
                events = []
                for e in (od.get("events") or []):
                    if isinstance(e, dict):
                        events.append(LogisticsEvent(
                            time=str(e.get("time") or ""),
                            status=str(e.get("status") or ""),
                            detail=str(e.get("detail") or ""),
                        ))
                self._pending_order = Order(
                    order_id=str(od.get("order_id") or ""),
                    create_time=str(od.get("create_time") or ""),
                    status=str(od.get("status") or "待支付"),
                    product_id=str(od.get("product_id") or ""),
                    product_name=str(od.get("product_name") or ""),
                    platform=str(od.get("platform") or ""),
                    seller=str(od.get("seller") or ""),
                    price=float(od.get("price") or 0),
                    final_price=float(od.get("final_price") or 0),
                    receiver=str(od.get("receiver") or ""),
                    phone=str(od.get("phone") or ""),
                    address=str(od.get("address") or ""),
                    remark=str(od.get("remark") or ""),
                    track_no=str(od.get("track_no") or ""),
                    carrier=str(od.get("carrier") or ""),
                    events=events,
                    pay_time=od.get("pay_time"),
                    cancel_reason=str(od.get("cancel_reason") or ""),
                )
            except Exception:
                self._pending_order = None

    # ============== 多模态图片处理 ==============
    def chat_with_images(self, images: List[str], text: str = "") -> str:
        """
        图片处理主入口。
        images: base64 data URI 列表（如 "data:image/jpeg;base64,..."）
        text: 用户附带的文字指令
        根据图片数量和指令自动分发到场景A（单图找同款）或场景B（多图对比）。
        """
        if not images:
            return "未检测到图片内容。"

        # 检查 LLM 视觉能力
        if not _VISION_AVAILABLE:
            return ("视觉分析需要配置 LLM API Key。\n"
                    "请在「AI设置」页配置支持视觉的模型（如 GLM-4V / GPT-4o / Qwen-VL）。\n"
                    "或直接把商品详情链接发给我，我可以用浏览器抓取真实数据。")

        llm_cfg = _get_llm_config() if _get_llm_config else {}
        if not llm_cfg.get("enabled"):
            return ("当前 LLM 未启用，无法做图片视觉分析。\n"
                    "请在「AI设置」页配置 API Key 后重试，或直接发商品链接给我抓取。")

        # 指令识别
        t = (text or "").strip()
        force_find = bool(re.search(r"找同款|找同款|搜同款|根据.*图", t))
        force_compare = bool(re.search(r"对比.*图|对比这几张|比较.*图", t))

        # 自动分发
        if len(images) == 1 and not force_compare:
            return self._flow_image_find_similar(images[0])
        if len(images) >= 2 or force_compare:
            return self._flow_image_compare(images)

        # 默认：单图当找同款
        return self._flow_image_find_similar(images[0])

    # ---------- 场景A：单图找同款 ----------
    def _flow_image_find_similar(self, image_data_uri: str) -> str:
        """单张图片：提取特征 → 输出搜索关键词 → 提示用户手动搜索"""
        lines = ["**图片分析中**（场景A：找同款）……"]
        result = analyze_image_find_similar(image_data_uri) if analyze_image_find_similar else None

        if not result:
            return ("图片视觉分析失败或未返回有效结果。\n"
                    "可能原因：模型不支持视觉 / 图片过大 / 网络异常。\n"
                    "你可以直接描述商品特征（如「白色雪纺连衣裙 收腰 法式」），或发商品链接给我抓取。")

        # 图片模糊提示
        if result.get("blurry"):
            return ("图片细节不足，请上传更清晰的商品图。\n"
                    "或直接把商品链接发给我，我可以用浏览器抓取真实数据。")

        keyword = result.get("keyword", "").strip()
        category = result.get("category", "")
        color = result.get("color", "")
        style = result.get("style", "")
        material = result.get("material", "")
        details = result.get("details", "")

        if not keyword:
            return "未能从图片中提取有效搜索关键词，请换一张更清晰的商品图。"

        lines = [
            "**图片分析完成**（场景A：找同款）",
            "",
            f"- **品类**：{category or '未识别'}",
            f"- **主色**：{color or '未识别'}",
            f"- **风格**：{style or '未识别'}",
            f"- **材质推测**：{material or '未识别'}",
            f"- **设计细节**：{details or '—'}",
            "",
            f"**精准搜索关键词**：`{keyword}`",
            "",
            "**下一步操作**：",
            f"   1. 复制上方关键词，在浏览器打开 淘宝/京东/拼多多 搜索",
            "   2. 把你看中的 **1-5 个商品详情链接** 粘贴给我",
            "   3. 我会逐个抓取详情页，打分对比后输出 TOP-N（默认3，可指定如「前5名」）",
            "",
            "> **图片分析仅为视觉推测，完整参数请以商品网页为准**",
        ]

        # 记录为上次请求（便于后续衔接）
        try:
            req = ShoppingRequest(raw=keyword)
            req.keyword = keyword
            req.category = category
            if color:
                req.require_tags = [color]
            self._last_request = req
        except Exception:
            pass

        return "\n".join(lines)

    # ---------- 场景B：多图对比 ----------
    def _flow_image_compare(self, image_data_list: List[str]) -> str:
        """多张图片：横向对比分析 → 输出对比表格 + 选购建议"""
        n = len(image_data_list)
        lines = [f"**图片对比分析中**（场景B：{n} 张图片对比）……"]

        profile_snapshot = self.profile.get_all()
        md = analyze_images_compare(image_data_list, profile_snapshot) if analyze_images_compare else None

        if not md:
            return ("多图对比分析失败或未返回有效结果。\n"
                    "可能原因：模型不支持视觉 / 图片过多 / 网络异常。\n"
                    "你可以分别发商品链接给我，我用浏览器抓取真实数据后做对比。")

        # 补充：图片分析后的衔接提示
        md = md.strip()
        if not md.endswith("为准"):
            md += "\n\n> **图片分析仅为视觉推测，完整参数请以商品网页为准**"

        result = md + (
            "\n\n---\n"
            "**想要真实价格/评价/历史价？**\n"
            "   把这几款商品的详情链接发给我，我会用浏览器抓取真实数据，重新打分对比。"
        )
        return result


# ================= 命令行交互入口 =================
def run_cli():
    os.system("")  # 启用ANSI颜色（Windows）
    session = ChatSession()
    print("\n" + "=" * 60)
    print(session.GREETING)
    print("=" * 60 + "\n")
    # 打印档案状态
    if not session.profile.is_empty():
        print("已检测到你有之前保存的档案：")
        print(session.profile.view_profile())
        print()

    while True:
        try:
            user = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见，随时欢迎回来购物～")
            break
        if not user:
            continue
        if user.lower() in ("quit", "exit", "退出", "再见", "拜拜"):
            print("再见，随时欢迎回来购物～")
            break
        if user.lower() in ("help", "帮助", "菜单"):
            print(ChatSession.GREETING)
            continue
        reply = session.chat(user)
        print("\nAI助手：")
        print(_indent(reply))
        print()


def _indent(text: str, prefix: str = "   ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


if __name__ == "__main__":
    # 支持参数：python shopping_agent.py demo — 自动跑一组演示
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        s = ChatSession()
        demo_cases = [
            "录入档案",
            "165, 52",
            "跳过",
            "合身",
            "白色,粉色,黑色",
            "法式,甜美,通勤",
            "纯棉,雪纺",
            "收腰,修身",
            "混合皮",
            "张三, 13800000000, 北京市朝阳区建国路88号",
            "查看档案",
            "买一条夏天的连衣裙，预算200以内",
            "第二款太宽松，换更修身的，预算升到300",
            "买第1款",
            "确认",
            "我的订单",
            "物流",
            "售后 尺码不合适想换小一码",
        ]
        for i, q in enumerate(demo_cases, 1):
            print(f"\n{'='*50}")
            print(f"Demo [{i}/{len(demo_cases)}] 用户：{q}")
            print(f"{'='*50}")
            r = s.chat(q)
            print(r)
    else:
        run_cli()
