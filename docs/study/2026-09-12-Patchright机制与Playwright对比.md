# Patchright 的机制：它如何修补 Playwright 的自动化指纹

> 创建于 2026-09-12
> 来源：开发者与 AI 的问答整理（study 学习笔记）
> 适用版本：2026-09-12 真实搜索功能落地后（web_scraper.py 以 Patchright 为反拦截基座，缺失回退原版 Playwright）

## 背景与问题

- **Q1：Patchright 这个项目（https://github.com/Kaliiiiiiiiii-Vinyzu/patchright）的机制是什么？**
- **Q2：为什么它能解决我们上网搜索商品被风控拦截的痛点？**
- **Q3：为什么原版 Playwright 不行？两者区别在哪里？**

---

## 一、Patchright 是什么、机制是什么

Patchright 不是新框架，而是 **Playwright 的「打补丁复刻版」**：API 与 Playwright 完全一致（drop-in replacement），内部通过一个 `patchright.patch` 文件修改 Playwright 源码，并对驱动程序（driver）打补丁，把 Playwright **暴露自动化身份的每一处痕迹都修补掉**。

核心补丁五类：

| 补丁 | 干了什么 |
|---|---|
| **Runtime.enable 泄漏**（最关键） | 原版 Playwright 执行 JS 前会通过 CDP 启用 `Runtime` 域，这个动作会在页面里留下可探测的副作用，是反爬厂商识别 Playwright 的头号指纹。Patchright 改用路由（Routes）把 JS 注入 HTML 响应，完全不碰 `Runtime.enable` |
| **Console.enable 泄漏** | 彻底禁用 Console API（代价：`page.on("console")` 不可用，官方建议改用 JS 日志工具） |
| **启动参数泄漏** | 移除 `--enable-automation`，加 `--disable-blink-features=AutomationControlled`、`--disable-popup-blocking` 等，让 Chromium 启动参数和真人浏览器一致 |
| **代码库内的明显泄漏** | 修掉 Playwright 源码里其余可被探测的点 |
| **Closed Shadow DOM** | 支持用普通 locator（含 XPath）操作封闭 Shadow DOM 中的元素 |

效果：官方宣称可通过 Cloudflare、Kasada、Akamai、Datadome、Fingerprint.com、CreepJS 等一线反爬检测。

**限制**：只修补 **Chromium 内核**（Firefox / WebKit 不支持）；InitScript 改为基于路由注入，理论上有被时序攻击探测的可能，但目前没有反爬产品这样做。

## 二、为什么它能解决我们的痛点，原版 Playwright 不能

我们的痛点很具体（见 `web_scraper.py` 的拦截检测表）：淘宝的 `x5sec` / `punish` 跳转、京东的 `passport.jd.com` 扫码墙、拼多多的滑动验证。这些风控的**第一道关卡就是判断「你是不是自动化浏览器」**。

- **原版 Playwright 是「测试工具」出身**，设计目标是稳定可靠，不在乎暴露身份。它一启动就自带全套自动化指纹：`navigator.webdriver === true`、`--enable-automation` 参数、CDP `Runtime.enable` 泄漏。淘宝/京东的风控引擎一摸就知道是机器人，直接甩验证码或跳登录页——这正是我们每次都撞墙的原因。
- **Patchright 从驱动层把这些指纹抹掉**，风控看到的就是一个「看起来正常的 Chrome」。这是**框架级修补**，比在页面上注入 JS 隐身脚本（`_STEALTH_JS`）高一个维度——注入脚本本身也会留痕。且 Patchright 官方明确警告：**不要叠加自定义 stealth / init 脚本，否则反而暴露自动化特征**。所以 `web_scraper.py` 只在回退原版 Playwright 时才启用 `_STEALTH_JS`。

一句话总结：**Playwright 输在「生下来就自报家门」，Patchright 把自报家门的每一处都改掉了。**

## 三、两者区别一览

| 维度 | 原版 Playwright | Patchright |
|---|---|---|
| API | 原生 | **完全相同**，换 import 即用 |
| 隐身性 | 无，指纹明显 | 驱动级隐身，可过主流反爬 |
| 原理 | 微软官方测试框架 | 对 Playwright 源码 + 驱动打补丁 |
| Console API | 可用 | 禁用（反检测代价） |
| 内核 | Chromium / Firefox / WebKit | 仅 Chromium |
| 版本跟进 | 官方维护 | 社区（Vinyzu）维护，滞后于 Playwright 版本 |

## 四、与我们项目的契合度（及边界）

本项目现有架构已是最优姿势：

- **Patchright 作为反拦截基座优先**，缺失时静默回退原版 Playwright + `_STEALTH_JS`（可选依赖红线：缺 patchright 不影响启动）；
- 只做**指纹层规避**，验证码 / 登录墙仍暂停自动化交还人工（用户自行扫码登录，程序绝不过手账号密码）——这与 Patchright 的定位（改指纹、**不破风控**）完全一致。

**能力边界（两点须如实认知）**：

1. 它只能修补「自动化框架指纹」，**IP 信誉、账号风控、行为分析不在其能力范围内**——风控不是只看指纹一层。
2. 社区版对 Chromium 新版本升级的跟进会略慢于官方 Playwright。
