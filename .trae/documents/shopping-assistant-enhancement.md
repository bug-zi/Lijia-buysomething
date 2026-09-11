# 智能购物助手全流程增强方案

## Context（为什么做）

现有购物助手([shopping\_agent.py](file:///d:/buy%20something/shopping_agent.py))已具备：Mock 商品库、5 维评分、个人档案、订单模拟、LLM 增强、Web UI。但用户的新规格要求把它升级为「全流程购物辅助」，关键缺口：

1. **真实数据**：现 [product\_searcher.py](file:///d:/buy%20something/product_searcher.py#L605-L614) 的 `_web_search` 是空占位，全部走 Mock——违反"不要编造商品数据"。
2. **档案字段不全**：[profile\_module.py](file:///d:/buy%20something/profile_module.py#L18-L36) 缺新规格的「预算上限/优先因素/品牌偏好/接受杂牌/接受预售/发货地区/特殊需求/讨厌元素」。
3. **无虚拟购物车**：收藏、价格监控、备注模块完全缺失。
4. **评测卡片不规范**：[recommender.py](file:///d:/buy%20something/recommender.py#L365-L380) 现有卡片缺「优点≥3/缺点≥2/风险提示/0-10 三档评分」。
5. **无追问/取消**：信息不足时直接给推荐，缺少澄清提问与"停止"指令。
6. **无跨平台切换**：反爬时无平台切换逻辑。

用户已确认架构决策：**增强 Python 服务器为主入口**，浏览器抓取通过我(Trae)桥接；反爬时切其他平台再回退；历史价格只做当前价对比。

## 架构决策

### 数据源三层回退

`ProductSearcher.search` → ① 真实抓取缓存(我通过 `/api/scrape` 注入) → ② urllib 真实抓取(检测反爬自动切平台) → ③ Mock 演示数据(明确标注「演示」)。每条 Product 新增 `data_source` 字段("真实"/"演示")，UI 与回复都标注，绝不假装真实。

### Trae 浏览器桥接

新增 `POST /api/scrape` 端点：我用浏览器工具抓到真实商品后，按 Product schema JSON 数组 POST 进来，服务端缓存到 `scrape_cache.json`(带 keyword+过期时间 30 分钟)。`_web_search` 优先读缓存。这样「Python 主入口 + Trae 桥接」闭环成立。

### 反爬应对(用户选: 切其他平台再回退)

`web_scraper.py` 依次尝试 淘宝→京东→拼多多 的公开搜索页(urllib)，检测登录跳转/验证码关键词即切下一平台；全失败返回空列表 + `block_reason`，上层回退 Mock 并在回复里告知"真实抓取被拦截，已切换为演示数据，如需真实数据请对我说「用浏览器抓取 关键词」"。

### 历史价格(用户选: 仅当前价对比)

"查历史最低价"指令 → 跨平台当前价对比表 + 明确文案"历史价格曲线暂不支持，以下为当前各平台实时价对比"。

## 改动清单

### 新建文件

**`d:\buy something\web_scraper.py`** — 真实抓取层

- `PLATFORMS` = \[淘宝, 京东, 拼多多]，每个含 search\_url 模板 + 反爬特征词

- `scrap_search(keyword, category, price_max) -> (List[Product], block_reason)`：依次尝试各平台 urllib 抓取；解析 HTML 提取商品名/价格/店铺/链接(正则+JSON-LD)；检测登录/captcha 即切平台

- `fetch_product_detail(url) -> dict`：进详情页抓规格/评价摘要(尽力而为，失败返回部分字段)

- 全程 zero-deps(urllib + re + json)

**`d:\buy something\virtual_cart.py`** — 虚拟购物车

- `CartItem` dataclass：name, platform, url, current\_price, added\_at, history\_low(暂空), note, monitor(bool)

- `VirtualCart` 类：持久化 `virtual_cart.json`；方法 `add/remove/list/clear/set_note/toggle_monitor/monitor_all`

- `check_prices(web_scraper)`：对 monitor=True 的项重新抓价，低于记录价返回提醒

### 修改文件

**`profile_module.py`** — 扩展档案字段(保留旧字段兼容)
在 `PROFILE_FIELDS` 追加(中文名按规格)：

- `budget_max` 预算上限, `priority_factor` 优先因素, `secondary_factor` 次要因素

- `brands_like` 品牌偏好-允许, `brands_dislike` 品牌偏好-排除

- `accept_no_name` 是否接受杂牌, `accept_presale` 是否接受预售

- `ship_region` 发货地区偏好, `special_needs` 特殊需求

- `dislike_elements` 讨厌的元素(通用兜底，与现有 color/material/fit\_dislike 并存)
  `COLLECT_STEPS` 末尾追加一批次收集这些新字段。

**`product_searcher.py`** — 三层回退 + data\_source

- `Product` 增字段 `data_source: str = "演示"` (默认演示，真实抓取设"真实")

- `search()` 改：① 读 `scrape_cache.json`(keyword 命中且未过期) → ② `web_scraper.scrap_search()` → ③ `_mock_search()`，每层注入对应 data\_source

- `_web_search` 改为调用 `web_scraper`，删除占位空返回

**`recommender.py`** — 评测卡片规范化 + 0-10 评分

- `ScoreBreakdown` 增 `adapt_10, value_10, total_10`(0-10 归一化)

- `format_top3` 改卡片为规格格式：每款「基础信息/价格/优点≥3/缺点≥2/风险提示/适配度0-10/性价比0-10/综合0-10」

- `_score_profile_match` 增品牌偏好/接受杂牌/接受预售/讨厌元素/发货地区判定

- 卡片末尾标注 data\_source（"📦 真实数据" 或 "🎭 演示数据"）

**`shopping_agent.py`** — 工作流 + 购物车 + 追问 + 取消

- `__init__` 加 `self.cart = VirtualCart()`, `self._scraper_cache`

- `chat()` 前置命令分流：① 取消("停止"/"取消这次搜索") ② 购物车命令(加入/移除/展示/清空/备注/监控) ③ 历史价对比 ④ 现有档案/订单命令

- 需求解析后：若 `req.needs_clarify` 且缺关键词/预算/场景 → 主动提问不直接推荐

- `_flow_recommend` 调 `recommender` 后，若全部 data\_source=="演示" 且有 block\_reason → 回复附加"真实抓取被拦截"提示

- `snapshot()` 增 `cart_count`

**`request_parser.py`** — 档案优先因素感知

- `parse()` 把 `profile.budget_max` 作为默认预算上限(用户未明示预算时)

- `_merge_llm_json` 已有 budget 处理，无需大改

**`web_server.py`** — 新端点

- `GET /api/cart` / `POST /api/cart`(action:add/remove/clear/note/monitor) / `DELETE /api/cart/<i>`

- `POST /api/scrape`(body: {keyword, products:\[Product...]}) → 写 scrape\_cache.json

- `GET /api/health` 增 `cart_count, data_source`

- `/api/chat` 响应 snapshot 增 cart

**`index.html`** — 新 tab + 数据源标识

- 侧栏加「🛒 虚拟购物车」tab：表格展示 cart，每行按钮「监控」「移除」「备注」

- 推荐卡片渲染 data\_source 徽章(真实=绿/演示=灰)

- 聊天 chip 加「用浏览器抓取 连衣裙」(触发我介入)

## 复用项

- [ai\_client.py](file:///d:/buy%20something/ai_client.py) `summarize_reviews_with_llm`/`polish_recommendation_with_llm` 不动

- [order\_manager.py](file:///d:/buy%20something/order_manager.py) 下单/物流流程不动

- 现有 LLM key 配置、安全脱敏逻辑不动

## 验证

1. **单元**：`python profile_module.py` 看新字段收集；`python -c "from virtual_cart import VirtualCart; c=VirtualCart(); c.add(...); print(c.list())"`
2. **反爬回退**：启动服务，发"买连衣裙 预算200" → 期望回复含"🎭 演示数据"标识 + "真实抓取被拦截"提示(因本机无浏览器抓取缓存)
3. **桥接闭环**：我手动 `POST /api/scrape` 注入 1 条真实商品 → 再发同一关键词搜索 → 期望该商品出现并标"📦 真实数据"
4. **购物车**：发"把第1款加入购物车" → "展示我的购物车" → 期望表格返回
5. **追问**：发"帮我找个东西" → 期望反问品类/预算而非直接推荐
6. **取消**：发"停止" → 期望确认取消提示
7. **价格对比**：发"对比一下第1个和第2个商品" → 期望当前价对比表(无历史曲线文案)
8. 启动 `启动购物助手-网页版.bat`，浏览器开 <http://127.0.0.1:8765/> 验证 UI tab

