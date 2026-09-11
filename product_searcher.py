# -*- coding: utf-8 -*-
"""
跨平台商品搜索与数据抓取模块
支持平台：淘宝/天猫、京东、拼多多、抖音商城
由于真实电商平台反爬严格，默认提供【结构化Mock数据源】以便本地直接使用；
同时保留 Web 真实抓取入口（需联网、可能需要登录/验证码），可按需切换。
"""

import json
import os
import re
import random
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any, Tuple

# 本地数据缓存目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MOCK_DATA_FILE = os.path.join(BASE_DIR, "mock_products.json")

PLATFORMS = ["淘宝/天猫", "京东", "拼多多", "抖音商城"]

# Trae 浏览器桥接注入的真实抓取缓存（POST /api/scrape 写入），30 分钟过期
SCRAPE_CACHE_FILE = os.path.join(BASE_DIR, "scrape_cache.json")
SCRAPE_CACHE_TTL = 30 * 60  # 秒


def _load_scrape_cache(keyword: str) -> List[Dict[str, Any]]:
    """读取未过期的真实抓取缓存；命中返回商品 dict 列表，否则空"""
    import time
    if not os.path.exists(SCRAPE_CACHE_FILE):
        return []
    try:
        with open(SCRAPE_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    # 整体过期清理
    if data.get("_expire_at", 0) and time.time() > data.get("_expire_at", 0):
        return []
    kw_low = (keyword or "").strip().lower()
    for k, v in data.items():
        if k.startswith("_"):
            continue
        if k.lower() == kw_low and isinstance(v, dict):
            ts = v.get("_ts", 0)
            if time.time() - ts > SCRAPE_CACHE_TTL:
                continue
            return v.get("products", []) or []
    # 模糊命中：缓存键包含关键词
    for k, v in data.items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        if kw_low and kw_low in k.lower():
            ts = v.get("_ts", 0)
            if time.time() - ts > SCRAPE_CACHE_TTL:
                continue
            return v.get("products", []) or []
    return []


def save_scrape_cache(keyword: str, products: List[Dict[str, Any]]) -> None:
    """供 /api/scrape 端点调用，写入真实抓取缓存"""
    import time
    data: Dict[str, Any] = {}
    if os.path.exists(SCRAPE_CACHE_FILE):
        try:
            with open(SCRAPE_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    data[keyword] = {"products": products, "_ts": time.time()}
    data["_expire_at"] = time.time() + SCRAPE_CACHE_TTL
    try:
        with open(SCRAPE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


@dataclass
class Review:
    """真实买家评价（好评点+短板）"""
    good_points: List[str] = field(default_factory=list)   # 真实好评点
    bad_points: List[str] = field(default_factory=list)    # 真实短板/差评点
    image_reviews: int = 0                                 # 带图追评数
    review_count: int = 0                                  # 总评价数
    positive_rate: float = 0.95                            # 好评率


@dataclass
class Product:
    """商品统一结构（跨平台对齐）"""
    pid: str
    name: str
    platform: str
    category: str                    # 品类关键词，如"连衣裙/耳机/运动鞋"
    price: float                     # 原价
    final_price: float               # 到手价（含优惠/券）
    discount: str = ""               # 活动优惠说明
    stock: int = 999                 # 库存
    sales: int = 0                   # 销量
    ship_days: int = 2               # 预计发货天数
    seller: str = ""                 # 店铺/商家
    after_sale: List[str] = field(default_factory=list)  # 售后政策
    images: List[str] = field(default_factory=list)      # 实拍图（URL 或占位）
    tags: List[str] = field(default_factory=list)        # 属性标签：颜色、风格、材质、版型等
    review: Review = field(default_factory=Review)
    source_url: str = ""             # 商品链接
    data_source: str = "演示"          # 数据来源："真实"(网页抓取) / "演示"(Mock兜底)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


# ============ Mock 数据 ============
# 按品类预置一批结构化商品，模拟多平台搜索结果
MOCK_CATALOG: Dict[str, List[Dict[str, Any]]] = {
    "连衣裙": [
        {
            "pid": "tb-dress-001", "name": "法式茶歇碎花连衣裙夏季雪纺长裙",
            "platform": "淘宝/天猫", "category": "连衣裙",
            "price": 308.0, "final_price": 238.0, "discount": "新客立减30+叠满减40",
            "sales": 12500, "ship_days": 1, "seller": "小清新女装旗舰店",
            "after_sale": ["7天无理由", "运费险", "正品保证"],
            "tags": ["碎花", "雪纺", "长裙", "法式", "收腰", "米白色", "A字版"],
            "review": {
                "good_points": ["版型显瘦，收腰效果好", "雪纺面料不皱不透", "花色和图片一致，不俗气", "发货快，包装精美"],
                "bad_points": ["胸围处略紧，大胸建议大一码", "裙摆有轻微线头"],
                "image_reviews": 680, "review_count": 5890, "positive_rate": 0.97,
            },
        },
        {
            "pid": "jd-dress-002", "name": "通勤显瘦西装连衣裙气质OL职业裙",
            "platform": "京东", "category": "连衣裙",
            "price": 399.0, "final_price": 329.0, "discount": "京东自营 满299减70",
            "sales": 4300, "ship_days": 1, "seller": "京东自营 · 白领衣橱",
            "after_sale": ["京东上门取退", "7天无理由", "正品保证", "极速发货"],
            "tags": ["西装裙", "通勤", "黑色", "修身", "短裙", "正式", "弹力面料"],
            "review": {
                "good_points": ["面料垂感好，不易皱", "剪裁合体，职场穿很有气场", "黑色正，百搭", "物流次日达"],
                "bad_points": ["面料偏厚，35度以上有点热", "拉链略卡"],
                "image_reviews": 210, "review_count": 1980, "positive_rate": 0.96,
            },
        },
        {
            "pid": "pdd-dress-003", "name": "甜美日系吊带连衣裙两件套夏季",
            "platform": "拼多多", "category": "连衣裙",
            "price": 129.9, "final_price": 89.9, "discount": "百亿补贴 直降40",
            "sales": 28000, "ship_days": 2, "seller": "甜酷少女官方店",
            "after_sale": ["7天无理由", "退货包运费"],
            "tags": ["吊带", "两件套", "日系", "甜美", "粉色", "短裙", "针织"],
            "review": {
                "good_points": ["价格真的香，两件套划算", "颜色粉嫩显白", "版型少女感满满"],
                "bad_points": ["外套薄透，需要内搭", "针织易起球，手洗为佳"],
                "image_reviews": 1200, "review_count": 9800, "positive_rate": 0.93,
            },
        },
        {
            "pid": "dy-dress-004", "name": "辣妹风露肩修身包臀连衣裙party小礼服",
            "platform": "抖音商城", "category": "连衣裙",
            "price": 268.0, "final_price": 199.0, "discount": "直播间专属价 再减20",
            "sales": 7800, "ship_days": 3, "seller": "辣妹衣橱抖音官方店",
            "after_sale": ["7天无理由", "坏损包退"],
            "tags": ["辣妹", "露肩", "包臀", "修身", "黑色", "短裙", "缎面"],
            "review": {
                "good_points": ["显身材一绝，拍照好看", "面料有光泽，上档次", "回头率高"],
                "bad_points": ["对小腹不友好，需收腹", "肩带略松，建议改短"],
                "image_reviews": 450, "review_count": 3200, "positive_rate": 0.94,
            },
        },
        {
            "pid": "tb-dress-005", "name": "中式改良旗袍连衣裙日常可穿国风",
            "platform": "淘宝/天猫", "category": "连衣裙",
            "price": 358.0, "final_price": 298.0, "discount": "跨店满减60",
            "sales": 6200, "ship_days": 2, "seller": "江南古韵旗袍馆",
            "after_sale": ["7天无理由", "正品保证", "赠运费险"],
            "tags": ["国风", "旗袍", "改良", "中长裙", "绿色", "盘扣", "棉麻"],
            "review": {
                "good_points": ["国风味道正，盘扣做工精细", "日常穿不夸张，回头率高", "面料透气"],
                "bad_points": ["领口略紧，脖子短慎选", "棉麻易皱需熨烫"],
                "image_reviews": 320, "review_count": 2450, "positive_rate": 0.95,
            },
        },
        {
            "pid": "jd-dress-006", "name": "纯棉简约白色T恤连衣裙夏季休闲",
            "platform": "京东", "category": "连衣裙",
            "price": 159.0, "final_price": 119.0, "discount": "满99减40",
            "sales": 15000, "ship_days": 1, "seller": "京东自营 · 基础款工厂",
            "after_sale": ["7天无理由", "极速退款", "上门取退"],
            "tags": ["T恤裙", "休闲", "白色", "纯棉", "宽松", "短裙", "简约"],
            "review": {
                "good_points": ["纯棉亲肤，洗后不变形", "宽松遮肉，穿着舒服", "百搭"],
                "bad_points": ["白色略透，需穿浅色内搭", "版型偏长，小个子注意"],
                "image_reviews": 890, "review_count": 7200, "positive_rate": 0.96,
            },
        },
    ],
    "运动鞋": [
        {
            "pid": "tb-shoe-001", "name": "Nike Air Zoom Pegasus 40 跑鞋 透气缓震",
            "platform": "淘宝/天猫", "category": "运动鞋",
            "price": 899.0, "final_price": 649.0, "discount": "618大促 官方直营券250",
            "sales": 52000, "ship_days": 1, "seller": "Nike官方旗舰店",
            "after_sale": ["7天无理由", "正品鉴定", "假一赔十"],
            "tags": ["跑鞋", "Nike", "缓震", "透气", "黑色", "轻便", "专业运动"],
            "review": {
                "good_points": ["踩屎感十足，跑步膝盖舒服", "透气性好，夏天不闷脚", "尺码标准"],
                "bad_points": ["鞋头略窄，宽脚建议大半码", "鞋底纹路容易卡石子"],
                "image_reviews": 3200, "review_count": 28000, "positive_rate": 0.97,
            },
        },
        {
            "pid": "jd-shoe-002", "name": "李宁䨻科技超轻20 专业马拉松竞速跑鞋",
            "platform": "京东", "category": "运动鞋",
            "price": 799.0, "final_price": 599.0, "discount": "京东自营 满499减200",
            "sales": 18000, "ship_days": 1, "seller": "李宁京东自营旗舰店",
            "after_sale": ["7天无理由", "正品保证", "极速物流"],
            "tags": ["跑鞋", "李宁", "竞速", "超轻", "白色", "碳板", "专业运动"],
            "review": {
                "good_points": ["䨻科技回弹到位，5km无压力", "真的轻，几乎没感觉", "外观好看"],
                "bad_points": ["鞋楦偏瘦，宽脚劝退", "价格波动大"],
                "image_reviews": 1500, "review_count": 12300, "positive_rate": 0.96,
            },
        },
        {
            "pid": "pdd-shoe-003", "name": "回力经典帆布鞋男女同款 百搭小白鞋",
            "platform": "拼多多", "category": "运动鞋",
            "price": 129.0, "final_price": 69.9, "discount": "百亿补贴 5折秒杀",
            "sales": 98000, "ship_days": 2, "seller": "回力官方百亿补贴店",
            "after_sale": ["7天无理由", "退货包运费"],
            "tags": ["帆布鞋", "回力", "小白鞋", "百搭", "白色", "日常", "休闲"],
            "review": {
                "good_points": ["性价比天花板，经典款不过时", "鞋底软，走路不累", "搭什么都好看"],
                "bad_points": ["鞋底略薄，长时间站着脚酸", "新鞋有点磨脚后跟"],
                "image_reviews": 5600, "review_count": 58000, "positive_rate": 0.94,
            },
        },
        {
            "pid": "dy-shoe-004", "name": "New Balance 574 复古休闲运动鞋老爹鞋",
            "platform": "抖音商城", "category": "运动鞋",
            "price": 899.0, "final_price": 719.0, "discount": "直播间专属 限量色优惠180",
            "sales": 8500, "ship_days": 3, "seller": "NB潮鞋集合店",
            "after_sale": ["7天无理由", "坏损包退"],
            "tags": ["老爹鞋", "NB", "复古", "休闲", "灰色", "百搭", "增高"],
            "review": {
                "good_points": ["复古颜值在线，配裤子绝了", "脚感舒服，踩一天不累", "配色好看"],
                "bad_points": ["鞋身偏重，不太适合跑步", "灰白配色容易脏"],
                "image_reviews": 420, "review_count": 3100, "positive_rate": 0.95,
            },
        },
        {
            "pid": "tb-shoe-005", "name": "安踏KT8 专业篮球鞋 碳板支撑耐磨实战",
            "platform": "淘宝/天猫", "category": "运动鞋",
            "price": 699.0, "final_price": 529.0, "discount": "安踏会员券170",
            "sales": 9600, "ship_days": 2, "seller": "安踏官方旗舰店",
            "after_sale": ["7天无理由", "正品保证", "赠运费险"],
            "tags": ["篮球鞋", "安踏", "碳板", "耐磨", "蓝色", "专业实战", "高帮"],
            "review": {
                "good_points": ["包裹性好，脚踝安全感足", "抓地力强，不打滑", "碳板反馈明显"],
                "bad_points": ["高帮夏天略闷", "建议大半码穿厚篮球袜"],
                "image_reviews": 860, "review_count": 6200, "positive_rate": 0.96,
            },
        },
        {
            "pid": "jd-shoe-006", "name": "安踏通勤小白鞋 皮面百搭休闲板鞋 男款",
            "platform": "京东", "category": "运动鞋",
            "price": 359.0, "final_price": 249.0, "discount": "京东自营 满299减110",
            "sales": 24000, "ship_days": 1, "seller": "安踏京东自营旗舰店",
            "after_sale": ["7天无理由", "极速退款", "上门取退"],
            "tags": ["板鞋", "安踏", "小白鞋", "皮面", "白色", "通勤", "休闲"],
            "review": {
                "good_points": ["皮面好打理，上班通勤刚好", "鞋底软弹不累脚", "尺码准显脚小"],
                "bad_points": ["新鞋有轻微皮味", "白边容易脏"],
                "image_reviews": 1100, "review_count": 9200, "positive_rate": 0.96,
            },
        },
    ],
    "耳机": [
        {
            "pid": "tb-hp-001", "name": "Sony WH-1000XM5 头戴式无线降噪耳机",
            "platform": "淘宝/天猫", "category": "耳机",
            "price": 2899.0, "final_price": 2199.0, "discount": "Sony官方旗舰店 满减500+券200",
            "sales": 18000, "ship_days": 1, "seller": "Sony官方旗舰店",
            "after_sale": ["全国联保1年", "7天无理由", "正品保证"],
            "tags": ["头戴式", "Sony", "主动降噪", "无线", "黑色", "旗舰", "高音质"],
            "review": {
                "good_points": ["降噪无敌，地铁飞机秒静", "音质温润，人声出色", "佩戴舒适，3小时不夹头"],
                "bad_points": ["价格偏高", "头梁容易沾灰显旧"],
                "image_reviews": 1400, "review_count": 11000, "positive_rate": 0.97,
            },
        },
        {
            "pid": "jd-hp-002", "name": "Apple AirPods Pro 2 主动降噪  MagSafe充电",
            "platform": "京东", "category": "耳机",
            "price": 1899.0, "final_price": 1549.0, "discount": "京东自营A+会员 领券350",
            "sales": 88000, "ship_days": 1, "seller": "Apple产品京东自营旗舰店",
            "after_sale": ["全国联保1年", "7天无理由", "Apple正品"],
            "tags": ["真无线", "Apple", "主动降噪", "入耳式", "白色", "iOS生态", "空间音频"],
            "review": {
                "good_points": ["iPhone配对即连，生态无缝", "降噪强，通透模式自然", "佩戴稳固"],
                "bad_points": ["安卓用功能打折扣", "长时间入耳耳道略痒"],
                "image_reviews": 5800, "review_count": 52000, "positive_rate": 0.97,
            },
        },
        {
            "pid": "pdd-hp-003", "name": "红米Buds 5 真无线降噪耳机 入耳式",
            "platform": "拼多多", "category": "耳机",
            "price": 299.0, "final_price": 199.0, "discount": "百亿补贴 直降100",
            "sales": 128000, "ship_days": 2, "seller": "小米官方百亿补贴",
            "after_sale": ["7天无理由", "退货包运费", "全国联保1年"],
            "tags": ["真无线", "红米", "主动降噪", "入耳式", "黑色", "性价比", "长续航"],
            "review": {
                "good_points": ["200内降噪基本无对手", "续航给力，一周充一次", "连接稳定"],
                "bad_points": ["低频量多，人声略闷", "腔体略大，小耳道慎入"],
                "image_reviews": 8200, "review_count": 75000, "positive_rate": 0.95,
            },
        },
        {
            "pid": "dy-hp-004", "name": "Bose QuietComfort Ultra 头戴式降噪旗舰",
            "platform": "抖音商城", "category": "耳机",
            "price": 3499.0, "final_price": 2799.0, "discount": "Bose抖音官方直播间 立减700",
            "sales": 3200, "ship_days": 3, "seller": "Bose官方旗舰店",
            "after_sale": ["7天无理由", "全国联保1年", "正品保障"],
            "tags": ["头戴式", "Bose", "旗舰降噪", "无线", "黑色", "空间音频", "高音质"],
            "review": {
                "good_points": ["Bose降噪名不虚传", "佩戴舒适度行业第一", "空间音频沉浸感强"],
                "bad_points": ["价格高", "外观塑料感略重"],
                "image_reviews": 210, "review_count": 1800, "positive_rate": 0.96,
            },
        },
    ],
    "手机": [
        {
            "pid": "jd-phone-001", "name": "Apple iPhone 15 Pro 256G 原色钛金属",
            "platform": "京东", "category": "手机",
            "price": 8999.0, "final_price": 7999.0, "discount": "京东自营 满减1000",
            "sales": 120000, "ship_days": 1, "seller": "Apple产品京东自营旗舰店",
            "after_sale": ["全国联保1年", "7天无理由", "正品保障", "以旧换新补贴"],
            "tags": ["Apple", "旗舰", "256G", "钛金属", "原色", "A17Pro", "5G"],
            "review": {
                "good_points": ["钛金属真的轻，手感好", "A17Pro性能溢出", "拍照色彩真实"],
                "bad_points": ["发热明显，游戏时降频", "价格偏高", "接口仍非满血USB4"],
                "image_reviews": 8000, "review_count": 72000, "positive_rate": 0.96,
            },
        },
        {
            "pid": "tb-phone-002", "name": "小米14 Ultra 16+512 徕卡全焦段四摄",
            "platform": "淘宝/天猫", "category": "手机",
            "price": 6999.0, "final_price": 6299.0, "discount": "小米官方 百亿补贴700",
            "sales": 46000, "ship_days": 2, "seller": "小米官方旗舰店",
            "after_sale": ["全国联保1年", "7天无理由", "碎屏险可选"],
            "tags": ["小米", "旗舰", "16+512", "徕卡", "影像旗舰", "黑色", "骁龙8Gen3"],
            "review": {
                "good_points": ["影像天花板，随手出大片", "屏幕素质顶级", "续航表现不错"],
                "bad_points": ["偏重，221g长时间累手", "镜头凸起明显，平放不稳"],
                "image_reviews": 3200, "review_count": 28000, "positive_rate": 0.96,
            },
        },
        {
            "pid": "pdd-phone-003", "name": "Redmi K70 12+256 骁龙8Gen2 2K屏",
            "platform": "拼多多", "category": "手机",
            "price": 2699.0, "final_price": 2199.0, "discount": "百亿补贴 直降500",
            "sales": 180000, "ship_days": 2, "seller": "Redmi官方百亿补贴",
            "after_sale": ["7天无理由", "全国联保1年", "退货包运费"],
            "tags": ["Redmi", "性价比", "12+256", "骁龙8Gen2", "2K屏", "黑色", "5G"],
            "review": {
                "good_points": ["2K屏+骁龙8Gen2，性价比拉满", "5000mAh续航一天半", "充电快"],
                "bad_points": ["塑料中框手感一般", "拍照算法比较普通"],
                "image_reviews": 12000, "review_count": 105000, "positive_rate": 0.96,
            },
        },
    ],
    "T恤": [
        {
            "pid": "tb-t-001", "name": "优衣库男士圆领T恤 纯棉夏季短袖UT",
            "platform": "淘宝/天猫", "category": "T恤",
            "price": 99.0, "final_price": 59.0, "discount": "优衣库大促 直降40",
            "sales": 250000, "ship_days": 1, "seller": "优衣库官方旗舰店",
            "after_sale": ["7天无理由", "正品保障"],
            "tags": ["圆领", "纯棉", "短袖", "白色", "简约", "基础款", "男款"],
            "review": {
                "good_points": ["基础款百搭，内穿外穿都OK", "纯棉厚实不透", "洗后不掉色"],
                "bad_points": ["版型略长，小个子慎选", "领口洗久略有松动"],
                "image_reviews": 15000, "review_count": 180000, "positive_rate": 0.97,
            },
        },
        {
            "pid": "jd-t-002", "name": "海澜之家男士Polo衫商务休闲短袖",
            "platform": "京东", "category": "T恤",
            "price": 199.0, "final_price": 139.0, "discount": "京东自营 满199减60",
            "sales": 32000, "ship_days": 1, "seller": "海澜之家京东自营",
            "after_sale": ["7天无理由", "上门取退", "正品保障"],
            "tags": ["Polo", "商务", "短袖", "藏青色", "珠地棉", "修身", "男款"],
            "review": {
                "good_points": ["商务场合合适，版型精神", "珠地棉透气不闷", "做工精细"],
                "bad_points": ["修身版型肚子明显的建议大一码", "颜色略深"],
                "image_reviews": 1800, "review_count": 16000, "positive_rate": 0.96,
            },
        },
        {
            "pid": "pdd-t-003", "name": "美式复古oversize短袖T恤男女同款潮牌",
            "platform": "拼多多", "category": "T恤",
            "price": 89.9, "final_price": 39.9, "discount": "百亿补贴 爆款直降50",
            "sales": 300000, "ship_days": 2, "seller": "潮牌T恤工厂店",
            "after_sale": ["7天无理由", "退货包运费"],
            "tags": ["Oversize", "美式", "短袖", "黑色", "印花", "宽松", "中性"],
            "review": {
                "good_points": ["版型宽松显瘦，潮味够", "价格无敌，学生党友好", "印花手感OK"],
                "bad_points": ["面料偏薄，质感一般", "印花洗后可能轻微脱落"],
                "image_reviews": 20000, "review_count": 210000, "positive_rate": 0.93,
            },
        },
    ],
}


def _build_product(raw: Dict[str, Any]) -> Product:
    """从抓取/Mock 字典构造 Product；兼容多种字段命名（shop/platform_id/original_price 等）。"""
    import dataclasses as _dc
    raw = dict(raw)  # 浅拷贝，避免污染原数据

    # 1) 评价字段：多种命名 → Review
    review_fields: Dict[str, Any] = {}
    rating = raw.pop("rating", None)
    if rating is not None:
        try:
            r = float(rating)
            review_fields["positive_rate"] = round(r / 5.0, 3) if r > 1 else r
        except (ValueError, TypeError):
            pass
    for alias, target in [("review_count", "review_count"), ("comment_count", "review_count"),
                          ("review_images", "image_reviews"), ("image_reviews", "image_reviews"),
                          ("good_points", "good_points"), ("pros", "good_points"),
                          ("bad_points", "bad_points"), ("cons", "bad_points")]:
        if alias in raw:
            review_fields[target] = raw.pop(alias)
    review_existing = raw.pop("review", {}) or {}
    if isinstance(review_existing, dict):
        review_fields.update(review_existing)
    review = Review(**review_fields)

    # 2) 字段别名映射 → Product 字段名
    if "platform_id" in raw and "pid" not in raw:
        raw["pid"] = raw.pop("platform_id")
    else:
        raw.pop("platform_id", None)
    if "shop" in raw and "seller" not in raw:
        raw["seller"] = raw.pop("shop")
    else:
        raw.pop("shop", None)
    if "coupon" in raw and "discount" not in raw:
        raw["discount"] = raw.pop("coupon")
    else:
        raw.pop("coupon", None)
    if "url" in raw and "source_url" not in raw:
        raw["source_url"] = raw.pop("url")
    else:
        raw.pop("url", None)
    if "link" in raw and "source_url" not in raw:
        raw["source_url"] = raw.pop("link")
    else:
        raw.pop("link", None)

    # 3) 价格映射：scraped price=当前售价, original_price=原价
    #    → Product.price=原价, Product.final_price=到手价
    original_price = raw.pop("original_price", None)
    scraped_price = raw.get("price")
    if "final_price" not in raw:
        raw["final_price"] = float(scraped_price) if scraped_price is not None else 0.0
    if original_price is not None:
        raw["price"] = float(original_price)
    elif scraped_price is not None:
        raw["price"] = float(scraped_price)
    else:
        raw["price"] = 0.0

    # 4) 必填字段兜底
    raw.setdefault("pid", "")
    raw.setdefault("name", "未知商品")
    raw.setdefault("platform", "未知")
    raw.setdefault("category", "")

    # 5) 移除 Product 不识别的字段（如 is_flagship）
    valid_keys = {f.name for f in _dc.fields(Product)}
    clean = {k: v for k, v in raw.items() if k in valid_keys}

    return Product(review=review, **clean)


class ProductSearcher:
    """跨平台商品搜索器"""

    def __init__(self, use_mock: bool = True):
        self.use_mock = use_mock
        self._last_block_reason: str = ""   # 最近一次真实抓取被拦原因（供上层提示）

    # ---------- 核心搜索 ----------
    def search(
        self,
        keyword: str,
        category: Optional[str] = None,
        price_min: Optional[float] = None,
        price_max: Optional[float] = None,
        platforms: Optional[List[str]] = None,
        exclude_tags: Optional[List[str]] = None,
        require_tags: Optional[List[str]] = None,
        limit: int = 30,
    ) -> List[Product]:
        """
        综合搜索入口，返回按基础相关度排序的商品列表。
        三层回退：① Trae 浏览器桥接缓存(真实) → ② urllib 真实抓取(真实) → ③ Mock(演示)。
        每条 Product 的 data_source 字段如实标注，绝不假装真实。
        """
        # 归一化品类（用于命中Mock数据）
        cat = self._guess_category(keyword, category)
        self._last_block_reason = ""
        results: List[Product] = []

        # ① Trae 浏览器桥接注入的真实抓取缓存（最高优先级，真实数据）
        cached = _load_scrape_cache(keyword or cat)
        if cached:
            try:
                results = [_build_product(dict(r)) for r in cached]
                for p in results:
                    p.data_source = "真实"
            except Exception:
                results = []

        # ② urllib 真实抓取（仅当未纯 Mock 模式且缓存未命中）
        if not results and not self.use_mock:
            web_products, reason = self._web_search(keyword, cat)
            if web_products:
                results = web_products
                for p in results:
                    p.data_source = "真实"
            else:
                self._last_block_reason = reason

        # ③ Mock 兜底（标注「演示」）
        if not results:
            results = self._mock_search(cat, keyword)
            for p in results:
                p.data_source = "演示"

        # 过滤：价格区间
        if price_min is not None:
            results = [p for p in results if p.final_price >= price_min]
        if price_max is not None:
            results = [p for p in results if p.final_price <= price_max]

        # 过滤：平台
        if platforms:
            results = [p for p in results if p.platform in platforms]

        # 过滤：必含标签（任一命中即可，如"修身,雪纺"）
        if require_tags:
            # 同义词扩展（与 _mock_search 中保持一致）
            SYN = {
                "夏天": ["夏天", "夏季", "夏日", "夏装"],
                "冬季": ["冬季", "冬天", "冬日", "冬装", "秋冬"],
                "春季": ["春季", "春天", "春日", "春装", "春秋"],
                "秋季": ["秋季", "秋天", "秋日", "秋装", "春秋", "秋冬"],
                "显瘦": ["显瘦", "修身", "收腰"],
                "宽松": ["宽松", "oversize", "A字", "直筒", "阔"],
                "白色": ["白色", "米白", "奶白", "纯白"],
                "黑色": ["黑色", "炭黑", "纯黑"],
                "粉色": ["粉色", "樱花粉", "粉", "粉嫩"],
                "通勤": ["通勤", "职业", "OL", "商务"],
            }
            def expand(w: str) -> List[str]:
                lw = w.lower()
                for base, alts in SYN.items():
                    if lw == base or lw in [a.lower() for a in alts]:
                        return [a.lower() for a in alts]
                return [lw]
            expanded_tags: List[str] = []
            for rt in require_tags:
                expanded_tags.extend(expand(rt))
            expanded_tags = list(dict.fromkeys(expanded_tags))  # 去重保序

            def match_any(p: Product) -> bool:
                tag_text = " ".join(p.tags).lower() + " " + p.name.lower()
                return any(r in tag_text for r in expanded_tags)
            filtered = [p for p in results if match_any(p)]
            # 策略：如果严格过滤后结果为 0，则降级为「命中至少一个关键词或 category 命中」
            if filtered:
                results = filtered
            else:
                # 降级：至少匹配其中 1 个 tag（只做关键词+tag_name 软包含），或者全部返回
                # 已由 match_any 返回 0，说明一个都没命中；此时不做强过滤
                results = results

        # 过滤：排除标签
        if exclude_tags:
            def none_match(p: Product) -> bool:
                tag_text = " ".join(p.tags).lower() + " " + p.name.lower()
                return not any(e.lower() in tag_text for e in exclude_tags)
            results = [p for p in results if none_match(p)]

        # 排序：销量 + 好评率 加权
        results.sort(key=lambda p: (p.sales * (0.5 + p.review.positive_rate)), reverse=True)
        return results[:limit]

    # ---------- Mock 搜索 ----------
    def _mock_search(self, cat: str, keyword: str) -> List[Product]:
        products: List[Product] = []
        if cat in MOCK_CATALOG:
            products.extend(_build_product(dict(r)) for r in MOCK_CATALOG[cat])

        # 常见同义词/近义词扩展（让"夏天"能匹配到"夏季"等词）
        SYNONYMS = {
            "夏天": ["夏天", "夏季", "夏日", "夏装"],
            "冬季": ["冬季", "冬天", "冬日", "冬装", "秋冬"],
            "春季": ["春季", "春天", "春日", "春装", "春秋"],
            "秋季": ["秋季", "秋天", "秋日", "秋装", "春秋", "秋冬"],
            "显瘦": ["显瘦", "修身", "收腰"],
            "宽松": ["宽松", "oversize", "A字", "直筒", "阔"],
            "白色": ["白色", "米白", "奶白", "纯白"],
            "黑色": ["黑色", "炭黑", "纯黑"],
            "粉色": ["粉色", "樱花粉", "粉", "粉嫩"],
            "通勤": ["通勤", "职业", "OL", "商务"],
        }
        def _expand(tok: str) -> List[str]:
            if not tok: return []
            for base, alts in SYNONYMS.items():
                if tok == base or tok in alts:
                    return alts
            return [tok]

        # 关键词匹配：拆成多 token，任一命中即算相关（空格、标点都算分隔符）
        import re as _re
        raw_tokens = [t for t in _re.split(r"[\s,，。、;；/\\\-]+", (keyword or "").lower()) if t]
        # 过滤单字中文字（量词/语气词/停用字："个"、"的"、"了"等），避免干扰匹配
        _CJK_STOP_CHARS = set("个的了着过呢啊吧吗呀哦嗯和与及就又也都还只就给让要到下上")
        tokens: List[str] = []
        for rt in raw_tokens:
            if len(rt) == 1 and rt in _CJK_STOP_CHARS:
                continue
            for ex in _expand(rt):
                if ex and ex not in tokens: tokens.append(ex)
        # 去掉纯品类词，避免所有连衣裙都被跨品类拉回（品类匹配已通过 cat 分支覆盖）
        cat_stop = set()
        if cat in MOCK_CATALOG:
            cat_stop.add(cat.lower())

        def _hits(name_low: str, tags_low: str) -> bool:
            if not tokens: return True  # 无关键词，全部返回
            hit_count = 0
            total_active = 0
            for tok in tokens:
                if tok in cat_stop: continue
                # 对同义词扩展出来的「短词」加强约束：2字或更短的同义词必须命中 tag 而非仅 name 里被部分包含
                # (避免 "粉" 错匹配到 "红米"、"OL" 错匹配到 "volumes" 等)
                total_active += 1
                strict = len(tok) <= 2  # 短词走严格 tag-only
                if strict:
                    if tok and tok in tags_low.split():  # 直接等于 tag 子项
                        hit_count += 1
                        continue
                    # 宽松一点：作为完整中文词（被中文/空格隔开）
                    import re as _re2
                    pat = r"(?<![\u4e00-\u9fa5A-Za-z])" + _re2.escape(tok) + r"(?![\u4e00-\u9fa5A-Za-z])"
                    if _re2.search(pat, tags_low):
                        hit_count += 1
                        continue
                else:
                    if tok and (tok in name_low or tok in tags_low):
                        hit_count += 1
            if total_active == 0:
                return True  # 全是停用词，全部返回
            # 命中至少一半（且至少1个）才算通过；单token场景必须命中
            threshold = max(1, (total_active + 1) // 2)
            return hit_count >= threshold

        # 1) 品类内的商品：按关键词匹配打分筛选（严格阈值）
        #    但保留少量「品类内都不匹配」时的兜底：只取前N个不做过滤
        cat_results: List[Product] = []
        for p in products:
            nm = p.name.lower()
            tg = " ".join(p.tags).lower()
            if _hits(nm, tg):
                cat_results.append(p)
        # 如果关键词过滤后品类内太少（<3），放宽一点：允许至少命中1个token的也算
        if len(cat_results) < 3:
            extra: List[Product] = []
            for p in products:
                if p in cat_results: continue
                nm = p.name.lower()
                tg = " ".join(p.tags).lower()
                one_hit = False
                for tok in tokens:
                    if tok in cat_stop or not tok: continue
                    if tok in nm or tok in tg:
                        one_hit = True; break
                if one_hit:
                    extra.append(p)
            cat_results.extend(extra[:3 - len(cat_results)])
        # 还不够，就取品类内的全部商品兜底（保证能推荐出东西）
        if len(cat_results) < 3:
            for p in products:
                if p not in cat_results:
                    cat_results.append(p)
                if len(cat_results) >= 3:
                    break

        # 2) 跨品类命中 — 要求「严格匹配 >= 阈值」且至少命中2个词（避免噪声）
        for c, items in MOCK_CATALOG.items():
            if c == cat:
                continue
            for raw in items:
                name = raw["name"].lower()
                tag_text = " ".join(raw.get("tags", [])).lower()
                if _hits(name, tag_text):
                    # 跨品类再做一道：至少命中 2 个非停用词，避免 1 字同义词错连
                    extra_hits = 0
                    for tok in tokens:
                        if tok in cat_stop or not tok: continue
                        if tok in name or tok in tag_text: extra_hits += 1
                    if extra_hits >= 2:
                        cat_results.append(_build_product(dict(raw)))

        matched = cat_results

        if not matched:
            # 兜底：返回所有品类中随机几条，模拟模糊搜索
            all_items = []
            for items in MOCK_CATALOG.values():
                all_items.extend(items)
            pick = random.sample(all_items, k=min(6, len(all_items)))
            matched = [_build_product(dict(r)) for r in pick]

        return matched

    # ---------- 品类推断 ----------
    def _guess_category(self, keyword: str, hint: Optional[str]) -> str:
        if hint and hint in MOCK_CATALOG:
            return hint
        kw = keyword
        mapping = [
            ("连衣裙", ["连衣裙", "裙子", "裙", "长裙", "短裙", "旗袍"]),
            ("运动鞋", ["运动鞋", "跑鞋", "球鞋", "篮球鞋", "帆布鞋", "老爹鞋", "小白鞋", "跑鞋"]),
            ("耳机", ["耳机", "airpods", "降噪耳机", "头戴式", "tws"]),
            ("手机", ["手机", "iphone", "小米", "红米", "安卓手机", "旗舰机"]),
            ("T恤", ["t恤", "polo", "短袖", "体恤", "tee"]),
        ]
        for cat, keys in mapping:
            for k in keys:
                if k.lower() in kw.lower():
                    return cat
        # 兜底返回第一个有数据的品类
        return "连衣裙" if not hint else hint

    # ---------- 单链接详情页抓取（新流程：用户粘贴链接 → 逐个抓取） ----------
    def grab_from_urls(self, urls: List[str]) -> Tuple[List[Product], str]:
        """
        接收用户粘贴的 1-5 条商品详情链接，逐个调用 Playwright 抓取。
        返回 (products, block_reason)。
        - 抓取成功 → products 含真实商品（data_source="真实"）
        - 验证码/滑块 → 返回部分结果 + block_reason 含「继续抓取」提示
        - 抓取失败 → 如实告知，严禁编造。
        """
        try:
            import web_scraper
        except Exception as e:
            return [], f"web_scraper 模块不可用：{e}"

        products: List[Product] = []
        block_reasons: List[str] = []
        for url in urls[:5]:
            r = web_scraper.grab_product_detail(url, headless=False)
            if r.get("product"):
                products.append(r["product"])
            if r.get("block_reason"):
                block_reasons.append(r["block_reason"])
            if r.get("need_human"):
                # 遇到验证码，暂停后续抓取，交还用户
                break

        reason = "；".join(block_reasons) if block_reasons else ""
        return products, reason

    # ---------- 限量真实搜索：需求直达商品（2026-09-12 新增） ----------
    def search_real(self, keyword: str, platforms: Optional[List[str]] = None,
                    max_per_platform: int = 6) -> Tuple[List[Product], str, bool]:
        """
        打开平台搜索结果页限量抓取（每平台 ≤10 条，默认 6），返回 (products, block_reason, need_human)。
        卡片层拿不到的字段（评价等）留空，绝不编造；登录墙 need_human=True 交还人工。
        """
        try:
            import web_scraper
        except Exception as e:
            return [], f"web_scraper 模块不可用：{e}", False
        if not keyword or not keyword.strip():
            return [], "搜索关键词为空", False
        if not platforms:
            platforms = ["京东", "淘宝/天猫"]

        products: List[Product] = []
        reasons: List[str] = []
        need_human = False
        for pf in platforms:
            r = web_scraper.search_platform(keyword.strip(), pf,
                                            max_results=max_per_platform)
            for card in r.get("cards") or []:
                try:
                    p = self._product_from_card(card, pf, keyword)
                    products.append(p)
                except Exception:
                    continue
            if r.get("block_reason"):
                reasons.append(r["block_reason"])
            if r.get("need_human"):
                need_human = True
                break   # 登录墙/验证码：停止后续平台，交还人工
        return products, "；".join(r for r in reasons if r), need_human

    @staticmethod
    def _product_from_card(card: Dict[str, Any], platform: str,
                           keyword: str = "") -> Product:
        """搜索卡片 → Product。价格/销量解析失败留 0，图片缺省空列表。"""
        import re as _re
        price = 0.0
        m = _re.search(r'(\d+(?:\.\d{1,2})?)',
                       (card.get("price_text") or "").replace(',', ''))
        if m:
            price = float(m.group(1))
        sales_text = card.get("sales_text") or ""
        m2 = _re.search(r'(\d+(?:\.\d+)?)\s*万', sales_text)
        if m2:
            sales = int(float(m2.group(1)) * 10000)
        else:
            m3 = _re.search(r'(\d[\d,]*)', sales_text.replace(',', ''))
            sales = int(m3.group(1)) if m3 else 0
        url = card.get("url") or ""
        raw = {
            "pid": url or f"{platform}-{(card.get('name') or '')[:24]}",
            "name": card.get("name") or "未知商品",
            "platform": platform,
            "price": price,
            "final_price": price,
            "seller": card.get("seller", ""),
            "sales": sales,
            "images": [card["image"]] if card.get("image") else [],
            "source_url": url,
            "category": keyword.strip()[:12],
        }
        p = _build_product(raw)
        p.data_source = "真实"
        return p


    # ---------- Mock 演示搜索（仅演示场景，须用 ⚠️ 标记） ----------
    def mock_search(self, keyword: str, category: Optional[str] = None,
                    price_max: Optional[float] = None,
                    require_tags: Optional[List[str]] = None,
                    exclude_tags: Optional[List[str]] = None,
                    limit: int = 8) -> List[Product]:
        """演示用 Mock 搜索，结果 data_source 标注「演示」"""
        cat = self._guess_category(keyword, category)
        results = self._mock_search(cat, keyword)
        for p in results:
            p.data_source = "演示"
        if price_max:
            results = [p for p in results if p.final_price <= price_max]
        if require_tags:
            tag_low = [t.lower() for t in require_tags]
            results = [p for p in results
                       if any(t in " ".join(p.tags).lower() + " " + p.name.lower()
                              for t in tag_low)]
        if exclude_tags:
            ex_low = [e.lower() for e in exclude_tags]
            results = [p for p in results
                       if not any(e in " ".join(p.tags).lower() + " " + p.name.lower()
                                  for e in ex_low)]
        return results[:limit]


# ========== 持久化（搜索历史缓存，便于展示） ==========
def save_search_history(query: str, products: List[Product]) -> None:
    cache_file = os.path.join(BASE_DIR, "search_history.json")
    history: Dict[str, Any] = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = {}
    history[query] = [p.to_dict() for p in products]
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    s = ProductSearcher()
    res = s.search("夏天连衣裙", price_max=200)
    for p in res:
        print(f"[{p.platform}] {p.name}  到手价¥{p.final_price}  标签:{','.join(p.tags)}")
