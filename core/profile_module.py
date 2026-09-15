# -*- coding: utf-8 -*-
"""
模块A：个人偏好档案模块
负责采集、保存、查看、修改、清空用户固定个人信息
档案数据作为所有商品推荐的永久参考依据
"""

import json
import os
import re
from typing import Dict, Optional, Any

# 档案文件路径 - 所有文件保存在同一目录下
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # core/ 的上级 = 项目根（数据文件仍存根目录）
PROFILE_FILE = os.path.join(BASE_DIR, "user_profile.json")

# 必填集合：决定推荐准确度与下单流程的最低字段集（其余全部为选填）
REQUIRED_FIELDS = (
    "height", "weight", "size_habit", "color_like", "color_dislike",
    "budget_max", "priority_factor", "receiver", "phone", "address",
)

# 档案字段定义与中文名称映射（必填在前，选填按「个人画像/风格审美/材质气候/品牌渠道/决策服务」分组）
PROFILE_FIELDS = {
    # ---- 必填：体型与色彩基础 + 预算决策 + 收货信息 ----
    "height": "身高(cm)",
    "weight": "体重(kg)",
    "size_habit": "尺码习惯(偏宽松/合身/偏紧，选码偏大/偏小)",
    "color_like": "喜爱色系(多个用逗号)",
    "color_dislike": "避雷颜色(多个用逗号)",
    "budget_max": "预算上限(元，作为默认预算)",
    "priority_factor": "优先因素(价格/质量/颜值/耐用/品牌/发货速度)",
    "receiver": "收货人姓名(用于下单)",
    "phone": "联系电话(用于下单)",
    "address": "收货地址(用于下单)",
    # ---- 选填：个人画像与体型尺码 ----
    "age_range": "年龄段(如 18-24/25-30/30-40/40+)",
    "occupation": "职业(如学生/上班族/教师)",
    "shoulder": "肩宽(cm)",
    "waist": "腰围(cm)",
    "top_size": "上装尺码(如 M/170/92A)",
    "bottom_size": "下装尺码(如 32/170/74A)",
    "shoe_size": "鞋码(如 42)",
    "skin_type": "肤质(如敏感肌/油性)",
    # ---- 选填：风格审美与场景 ----
    "style_like": "风格偏好(如日系简约/美式休闲/通勤正式，可2-3个)",
    "fit_like": "版型偏好(如修身/收腰/宽松)",
    "fit_dislike": "版型避雷",
    "usage_scenario": "常用场景(如通勤/运动/居家/旅行，多个用逗号)",
    "dislike_elements": "讨厌的元素(通用兜底，如荧光色/廉价拉链)",
    "other_preference": "其他偏好(如配饰偏好/忌口/特殊需求等)",
    # ---- 选填：材质与气候 ----
    "material_like": "材质偏好(喜欢的面料)",
    "material_dislike": "材质避雷(不喜欢的面料)",
    "climate": "所在气候(如南方湿热/北方干冷/四季分明)",
    # ---- 选填：品牌与渠道 ----
    "brands_like": "品牌偏好-允许(多个用逗号)",
    "brands_dislike": "品牌偏好-排除(多个用逗号)",
    "accept_no_name": "是否接受杂牌(是/否)",
    "accept_presale": "是否接受预售(是/否)",
    "ship_region": "发货地区偏好(如江浙沪/广东/不限)",
    # ---- 选填：决策与服务 ----
    "secondary_factor": "次要因素(价格/质量/颜值/耐用/品牌/发货速度)",
    "priority_order": "选购优先级排序(价格/品质/外观/发货速度/品牌，可多选逗号分隔)",
    "ship_fee_pref": "运费偏好(优先免运费/无所谓)",
    "after_sale_pref": "售后偏好(如优先7天无理由+运费险)",
    "special_needs": "特殊需求(如礼盒包装/加急)",
}

# 分批收集的引导顺序（必填在前，选填按角度分组靠后；同一批次只放"同类语义"字段，
# 防止逗号多值被错位分配；多字段批次内各字段应为单值，多值字段独占批次）
COLLECT_STEPS = [
    # —— 必填批次 ——
    ["height", "weight"],
    ["size_habit"],
    ["color_like"],
    ["color_dislike"],
    ["budget_max"],
    ["priority_factor"],
    ["receiver", "phone", "address"],
    # —— 选填批次：个人画像与体型尺码 ——
    ["age_range", "occupation"],
    ["shoulder", "waist"],
    ["top_size", "bottom_size", "shoe_size"],
    ["skin_type", "climate"],
    # —— 选填批次：风格审美与场景 ——
    ["style_like"],
    ["usage_scenario"],
    ["fit_like", "fit_dislike"],
    ["material_like", "material_dislike"],
    ["dislike_elements", "other_preference"],
    # —— 选填批次：品牌与渠道 ——
    ["brands_like"],
    ["brands_dislike"],
    ["accept_no_name", "accept_presale"],
    ["ship_region"],
    # —— 选填批次：决策与服务 ——
    ["secondary_factor"],
    ["priority_order"],
    ["ship_fee_pref", "after_sale_pref"],
    ["special_needs"],
]


class ProfileManager:
    """个人购物偏好档案管理器"""

    def __init__(self, file_path: str = PROFILE_FILE):
        self.file_path = file_path
        self.profile: Dict[str, Any] = self._load()
        self._collect_step_index = 0  # 分批收集进度

    # ---------- 持久化 ----------
    def _load(self) -> Dict[str, Any]:
        """从磁盘加载档案，不存在则返回空字典"""
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save(self) -> None:
        """保存档案到磁盘"""
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(self.profile, f, ensure_ascii=False, indent=2)

    # ---------- 基本操作 ----------
    def is_empty(self) -> bool:
        """档案是否为空"""
        return not any(v for v in self.profile.values() if v)

    def get(self, key: str, default: Any = None) -> Any:
        return self.profile.get(key, default)

    def get_all(self) -> Dict[str, Any]:
        """返回完整档案副本"""
        return dict(self.profile)

    def set_field(self, key: str, value: Any) -> str:
        """设置/修改某一项，返回提示信息"""
        if key not in PROFILE_FIELDS:
            # 允许自定义扩展字段
            valid = list(PROFILE_FIELDS.keys())
            return f"未识别的字段 '{key}'，已作为自定义字段保存。\n标准字段：{', '.join(valid)}"
        self.profile[key] = value
        self._save()
        label = PROFILE_FIELDS.get(key, key)
        return f"已更新【{label}】为：{value}"

    def update_batch(self, data: Dict[str, Any]) -> str:
        """批量更新字段；空字符串表示清空该字段（删除键，档案恢复为未填写）"""
        updates = []
        for k, v in data.items():
            if isinstance(v, str):
                v = v.strip()
            if v:
                self.profile[k] = v
                updates.append(f"{PROFILE_FIELDS.get(k, k)}={v}")
            elif k in self.profile:
                del self.profile[k]
                updates.append(f"{PROFILE_FIELDS.get(k, k)}=（已清空）")
        self._save()
        if updates:
            return "已更新以下字段：\n" + "\n".join(f"  · {u}" for u in updates)
        return "未提供有效字段值"

    def delete_field(self, key: str) -> str:
        """删除/清空某一项"""
        if key in self.profile:
            del self.profile[key]
            self._save()
            label = PROFILE_FIELDS.get(key, key)
            return f"已清空【{label}】"
        return f"字段 '{key}' 不存在或为空"

    def clear_all(self) -> str:
        """清空全部档案（先归档进回收站，3 天内可还原）"""
        if any(v for v in self.profile.values() if v):
            try:
                from trash_bin import trash_bin
                filled = {k: v for k, v in self.profile.items() if v}
                trash_bin.add("profile", "个人购物偏好档案",
                              f"{len(filled)} 项已填写内容整体归档",
                              {"profile": dict(filled)})
            except Exception:
                pass
        self.profile.clear()
        self._save()
        self._collect_step_index = 0
        return "已清空全部档案"

    def restore_merge(self, data: Dict[str, Any]) -> int:
        """从回收站还原档案：合并写入非空字段（不覆盖现有非空值），返回还原字段数"""
        n = 0
        for k, v in (data or {}).items():
            if v and not self.profile.get(k):
                self.profile[k] = v
                n += 1
        if n:
            self._save()
        return n

    # ---------- 查看 ----------
    def view_profile(self) -> str:
        """以清晰表格形式输出档案（必填/选填分节，与用户中心表单分组一致）"""
        if self.is_empty():
            return "个人档案为空，使用「录入档案」或直接告诉我身高体重等信息开始建立档案。"

        lines = []
        lines.append("=" * 50)
        lines.append("我的购物偏好档案")
        lines.append("=" * 50)
        lines.append(f"{'字段':<20} | {'值'}")
        lines.append("-" * 50)
        lines.append("【必填】（决定推荐准确度与下单流程）")
        for key, label in PROFILE_FIELDS.items():
            if key not in REQUIRED_FIELDS:
                continue
            value = self.profile.get(key, "")
            value_str = str(value) if value else "— 未填写 —"
            lines.append(f"{label:<20} | {value_str}")
        lines.append("-" * 50)
        lines.append("【选填】（填得越全，推荐越准）")
        for key, label in PROFILE_FIELDS.items():
            if key in REQUIRED_FIELDS:
                continue
            value = self.profile.get(key, "")
            value_str = str(value) if value else "— 未填写 —"
            lines.append(f"{label:<20} | {value_str}")
        # 自定义字段
        std_keys = set(PROFILE_FIELDS.keys())
        extra = [(k, v) for k, v in self.profile.items() if k not in std_keys and v]
        if extra:
            lines.append("-" * 50)
            lines.append("自定义字段：")
            for k, v in extra:
                lines.append(f"{k:<20} | {v}")
        lines.append("=" * 50)
        return "\n".join(lines)

    # ---------- 分批引导录入 ----------
    def start_collect(self) -> str:
        """开始引导录入档案"""
        self._collect_step_index = 0
        return self._next_collect_prompt()

    def continue_collect(self, user_answer: str) -> str:
        """接收用户回答，写入当前批次字段，返回下一批引导或完成提示"""
        if self._collect_step_index >= len(COLLECT_STEPS):
            return "档案信息已收集完成！使用「查看档案」可浏览。随时可继续补充或修改。"

        current_keys = COLLECT_STEPS[self._collect_step_index]
        # 简单解析用户回答（支持 键:值 或 逗号分隔 或 纯值）
        if user_answer.strip().lower() in ("跳过", "skip", "下一批", "下一组"):
            self._collect_step_index += 1
            return self._next_collect_prompt()

        parsed = self._parse_answer(user_answer, current_keys)
        updates = {}
        for k, v in parsed.items():
            if v:
                updates[k] = v
        self.update_batch(updates)

        self._collect_step_index += 1
        return self._next_collect_prompt()

    def collect_progress_hint(self) -> str:
        """建档进行中的进度提示：向导暂停、应答旁路问题后附上，方便用户接着填"""
        if self._collect_step_index >= len(COLLECT_STEPS):
            return "—— 建档步骤已全部走完，回「查看档案」可浏览。"
        keys = COLLECT_STEPS[self._collect_step_index]
        labels = " / ".join(PROFILE_FIELDS.get(k, k) for k in keys)
        return (f"—— 档案录入仍停在第 {self._collect_step_index + 1}/{len(COLLECT_STEPS)} 步"
                f"（待填：{labels}）。直接回答可继续，回「跳过」略过，回「完成」暂停。")

    def _parse_answer(self, answer: str, expected_keys: list) -> Dict[str, str]:
        """
        解析用户回答为 {字段: 值}；支持多种格式：
        1) 命名式：身高:175, 体重=65 或 身高改为175
        2) 序列式：当前批次有多个值，逗号按顺序 -> 对应字段
        3) 单字段批次多选项：当前批次只有 1 个字段（如 color_like），逗号分隔的多个值全归入该字段，不再错位！
        """
        result: Dict[str, str] = {}
        ans = answer.strip()

        # 统一分隔符为英文逗号
        ans_norm = ans.replace("，", ",").replace("：", ":").replace("=", ":").replace("改为", ":").replace("改成", ":").replace("设置为", ":")

        # 格式1: 命名式  key:value, key:value
        has_named = False
        if ":" in ans_norm:
            for part in re.split(r"[;,]", ans_norm):
                part = part.strip()
                if ":" in part:
                    k, v = part.split(":", 1)
                    k, v = k.strip().lower(), v.strip()
                    if not k or not v: continue
                    matched = self._match_field(k)
                    if matched and matched in expected_keys:
                        result[matched] = v
                        has_named = True
            # 如果匹配到了"喜欢色系/避雷色系"这类非expected但属于命令的，保留下来（例如修改指令会走上层）
            if has_named and result:
                return result

        # 拆分为值数组（全逗号）
        values = [v.strip() for v in re.split(r"[;,]", ans) if v.strip()]
        if not values:
            return result

        # 格式3: 当前批次只有 1 个字段 -> 所有值用逗号拼接归入这一个（解决了"白色,粉色,黑色"被当成3个字段写的Bug）
        if len(expected_keys) == 1:
            result[expected_keys[0]] = ",".join(values)
            return result

        # 格式2: 多字段批次，数量正好对得上，顺序赋值
        if len(values) <= len(expected_keys):
            for i, v in enumerate(values):
                result[expected_keys[i]] = v
        else:
            # 逗号数量超出字段数：全部拼到最后一个字段（宽容策略，避免丢信息）
            for i in range(len(expected_keys) - 1):
                result[expected_keys[i]] = values[i]
            result[expected_keys[-1]] = ",".join(values[len(expected_keys) - 1 :])
        return result

    def _match_field(self, keyword: str) -> Optional[str]:
        """根据关键词匹配字段名"""
        keyword = keyword.lower()
        for key, label in PROFILE_FIELDS.items():
            if keyword == key.lower() or keyword in label.lower():
                return key
        return None

    def _next_collect_prompt(self) -> str:
        """生成下一批引导语"""
        if self._collect_step_index >= len(COLLECT_STEPS):
            return "档案信息已收集完成！使用「查看档案」可浏览。随时可继续补充或修改。"

        current_keys = COLLECT_STEPS[self._collect_step_index]
        labels = [PROFILE_FIELDS.get(k, k) for k in current_keys]
        step_num = self._collect_step_index + 1
        total = len(COLLECT_STEPS)
        prompt = (
            f"档案录入 (第 {step_num}/{total} 步)\n"
            f"请告诉我以下信息，多个值用逗号分隔（不想填写可回复「跳过」）：\n"
            f"  · {(' / ').join(labels)}"
        )
        return prompt

    # ---------- 快捷命令分发 ----------
    def handle_command(self, user_input: str) -> Optional[str]:
        """
        处理档案相关命令，若命中则返回响应；未命中返回None由上层处理
        支持: 查看档案/档案 / 修改XX / 清空XX / 清空全部档案 / 录入档案/建立档案
        """
        text = user_input.strip().lower()

        # 查看档案
        if text in ("查看档案", "档案", "我的档案", "查看偏好", "profile"):
            return self.view_profile()

        # 录入档案
        if text in ("录入档案", "建立档案", "开始建档", "建档", "创建档案"):
            return self.start_collect()

        # 清空全部
        if text in ("清空全部档案", "清空档案", "重置档案"):
            return self.clear_all()

        # 修改某一项: "修改身高 175" / "身高改为175" / "修改: 身高=175, 体重=65"
        if text.startswith("修改") or "改为" in text or "修改:" in text or "修改：" in text or ("=" in text and any(f in text for f in PROFILE_FIELDS)):
            raw = user_input.strip()
            # 移除"修改"前缀
            raw = raw.replace("修改", "", 1).strip().lstrip(":：").strip()
            # 支持 "改为" 语义
            raw = raw.replace("改为", "=").replace("改成", "=").replace("设置为", "=")
            # 解析 k=v,k=v 或 k:v,k:v
            pairs = raw.replace("，", ",").replace("；", ";").replace("：", "=")
            updates = {}
            for part in pairs.split(","):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    matched = self._match_field(k.strip())
                    if matched:
                        updates[matched] = v.strip()
                    else:
                        # 尝试直接作为键
                        updates[k.strip()] = v.strip()
                else:
                    # "身高 175" 形式
                    tokens = part.split(None, 1)
                    if len(tokens) == 2:
                        matched = self._match_field(tokens[0])
                        if matched:
                            updates[matched] = tokens[1]
            if updates:
                return self.update_batch(updates)
            return "未识别到修改内容。示例：「修改身高=175，体重=65」"

        # 清空某一项: "清空身高" / "删除收货地址"
        if text.startswith("清空") or text.startswith("删除"):
            keyword = raw_keyword = (
                user_input.strip()
                .replace("清空", "", 1)
                .replace("删除", "", 1)
                .replace("字段", "", 1)
                .strip()
            )
            matched = self._match_field(keyword)
            if matched:
                return self.delete_field(matched)
            return f"未找到字段 '{raw_keyword}'，使用「查看档案」确认字段名"

        return None


if __name__ == "__main__":
    pm = ProfileManager()
    print(pm.view_profile())
    print("\n" + pm.start_collect())
