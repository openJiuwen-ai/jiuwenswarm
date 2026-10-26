# A2UI 支持 IM Channel 的可行性分析

## 背景

JiuwenSwarm 的 A2UI 是 Web-only 的生成式界面能力。Web channel 具备受控前端运行时：可以解析 `<a2ui-json>` block、通过 `@a2ui/react` 渲染组件、维护 surface/data model/component state，并把点击、选择、表单提交等交互包装为 `a2ui.client_event` 发回后端。

非 Web channel 不进入 A2UI 链路：不注入 A2UI prompt，不处理 A2UI client event，不执行 A2UI response finalizer，也不做 A2UI 文本 fallback。微信、飞书等 IM channel 继续走普通文本/Markdown 对话流程。

A2UI 的完整闭环不是“后端生成 JSON”这么简单，而是：

1. 后端生成并校验 A2UI 0.8 server-to-client message list。
2. 客户端解析 A2UI 消息。
3. 客户端维护 surface/data model/component state。
4. 客户端渲染可交互 UI。
5. 客户端把用户交互转换为标准 `a2ui.client_event`。
6. 后端把 `a2ui.client_event` 转为模型可读 prompt，进入下一轮对话。

Web channel 具备上述完整客户端能力；IM channel 当前只具备平台消息收发能力。

## 1. 把 Web A2UI Renderer 搬到 IM Channel 是否可行

结论：工程上“局部可行”，产品上“不建议作为 IM channel 的主方案”。

| 方案 | 可行性 | 说明 |
| --- | --- | --- |
| 把 `@a2ui/react` renderer 原样运行在微信/飞书客户端内 | 基本不可行 | 微信、飞书原生聊天窗口不会执行 JiuwenSwarm 自带 React bundle，也不会开放任意 DOM/JS 渲染能力。 |
| 服务端把 A2UI 渲染成图片或 HTML 链接再发到 IM | 局部可行 | 可做只读展示，但交互能力弱；点击、输入、组件状态回传仍要走平台能力或跳转 Web。 |
| 把 A2UI 语义映射为 IM 平台原生卡片/消息 | 可行但不是搬 renderer | 需要为飞书、微信分别写 adapter，将 A2UI 组件转换成飞书卡片、微信文本/图文/可用消息形态。 |

### Web Renderer 原样迁移的障碍

Web A2UI renderer 依赖浏览器运行环境：

- React 组件树、DOM、CSS、事件系统。
- `@a2ui/react` 的 surface 状态管理。
- 客户端主动调用 `processMessages` 处理 A2UI message list。
- action bridge 把用户操作转换成结构化 `a2ui.client_event`。
- WebSocket hook 把结构化 content 发送回后端。

IM 客户端不是 JiuwenSwarm 可控的浏览器容器。飞书、微信聊天窗口只渲染平台定义的消息类型，例如文本、图片、文件、音视频、飞书 interactive card、微信/iLink 文本 item 等。它们不会加载 JiuwenSwarm Web 前端，也不会执行 `@a2ui/react`。

所以，“把 Web A2UI renderer 搬到 IM channel”如果指原样复用 React renderer，本质上需要 IM 客户端提供一个可嵌入 Web runtime 的宿主，并允许 JiuwenSwarm 控制渲染和事件回传。普通聊天消息不满足这个条件。

### 优势

如果强行做某种“Web renderer in IM”的方案，理论收益包括：

- 协议复用度最高：继续使用 A2UI 0.8，不需要为每个 IM 平台重新定义 UI 语义。
- 展示一致性最好：Web、飞书、微信看到的理论 UI 可以保持一致。
- 组件能力完整：表单、多选、卡片、数据模型、组件状态、action context 都可按 A2UI 原语运行。
- 后端链路改动较小：后端仍只产出 A2UI block，客户端承担渲染。

这些收益依赖一个前提：IM 客户端必须能运行 JiuwenSwarm 控制的前端 runtime。现实中这个前提通常不成立。

### 劣势

- 无法控制 IM 客户端渲染环境。微信、飞书聊天窗口不是通用浏览器，不会执行任意 React bundle。
- 无法保证组件事件回传。A2UI 的关键价值在交互，而不是静态展示；没有事件回传就不是完整 A2UI。
- 平台安全模型不允许任意脚本。IM 平台通常会限制消息中的 HTML/JS，避免钓鱼、注入和隐私风险。
- 体验会割裂。如果用链接跳转 Web 页面承载 A2UI，用户实际离开 IM 对话上下文，回传身份、会话、授权也要重新处理。
- 维护成本高。每个 IM 平台的卡片、按钮、表单、回调、消息长度、频控、撤回、更新机制都不同，难以用 Web renderer 直接覆盖。

### 风险点

| 风险 | 影响 |
| --- | --- |
| 平台能力不一致 | 飞书支持 interactive card 和 card action；微信/iLink 当前更偏文本消息。 |
| 交互状态难同步 | A2UI surface/data model 状态需要客户端维护；IM 平台消息通常是一次性消息或平台卡片状态。 |
| 事件回传不标准 | A2UI 期望 `a2ui.client_event`；IM 平台回调字段、鉴权、用户身份、消息 ID 都各不相同。 |
| 消息更新能力有限 | A2UI 可以持续更新 surface；IM 平台对消息编辑、卡片更新、撤回、局部刷新支持不同。 |
| 流式输出冲突 | A2UI block 需要完整校验后渲染；IM channel 当前常做流式文本聚合、分片、限流。 |
| 安全与权限边界扩大 | 卡片 action、链接跳转、用户身份映射、跨平台回调都可能引入权限绕过或伪造交互风险。 |
| 降级路径复杂 | 当平台卡片发送失败、客户端版本不支持、回调失败时，必须回退到文本交互。 |

## 2. 是否需要微信、飞书等 IM 客户端支持或修改

如果目标是“完整支持 A2UI”，需要 IM 客户端支持。这里的“客户端支持”不一定是修改微信/飞书官方客户端源码，这通常不现实；但必须满足以下任一条件：

1. IM 官方客户端原生支持足够表达 A2UI 的消息组件和交互回调。
2. IM 平台允许在消息中嵌入 JiuwenSwarm 可控的 WebView/小程序/应用页面，并能可靠回传事件。
3. JiuwenSwarm 提供独立 companion Web 页面，IM 消息只负责打开链接，交互在 Web 页面完成。

否则，IM channel 只能发送文本、图片、文件或平台有限卡片，不能承载完整 A2UI renderer。

### 为什么必须有客户端支持

A2UI 的核心在客户端，而不是消息文本。

后端可以把 A2UI JSON 发给任何 channel，但如果客户端不理解这些 JSON，它只能显示一段原始文本，或者服务端需要做降级摘要。完整 A2UI 至少要求客户端具备：

- 协议解析：识别 `beginRendering`、`surfaceUpdate`、`dataModelUpdate`、`deleteSurface`。
- 状态管理：维护 surface、component tree、data model。
- 组件渲染：把 TextField、MultipleChoice、Button、Card/List 等组件渲染成可操作 UI。
- 事件采集：捕获点击、选择、输入、提交。
- 事件回传：把交互转换成 `a2ui.client_event`，带上 action context、surfaceId、componentId。
- 会话绑定：保证事件回到正确 channel、session、message/request。

这些能力都发生在用户使用的客户端侧。服务器无法通过普通 IM 文本消息替用户完成输入控件、按钮状态、组件事件这些交互语义。

## 不建议非 Web Channel 支持完整 A2UI 的逻辑

不建议把“完整 A2UI”扩展到 IM channel，原因不是后端做不到，而是产品边界和平台边界不匹配。

1. A2UI 是客户端渲染协议，Web 是受控客户端，IM 是第三方客户端。
2. IM 平台支持的是“平台消息组件”，不是通用 UI runtime。
3. 各 IM 平台能力差异太大，统一体验成本很高。
4. A2UI 交互闭环容易在 IM 的群聊、转发、引用、多端登录、卡片过期、回调重试等场景中失真。
5. Web-only 边界更清晰：非 Web channel 不感知 A2UI，普通聊天流程不受影响。

## 建议结论

不建议把 Web A2UI renderer 搬到 IM channel，也不建议把“完整 A2UI 支持”作为微信、飞书等非 Web channel 的统一目标。

建议采用以下边界：

1. Web channel：继续作为完整 A2UI renderer 的唯一一等支持端。
2. Feishu channel：保持普通文本/Markdown 流程；如果未来要做平台卡片，应作为独立 Feishu card adapter 设计，不宣称完整 A2UI。
3. WeChat channel：保持普通文本/Markdown 流程；可增强为编号选择、确认/取消、字段模板回复等“结构化文本交互”，不宣称完整 A2UI。
4. 其他 IM channel：默认不感知 A2UI；只有当平台原生组件和回调能力足够成熟时，再按平台 adapter 模式局部支持。
5. 如确需完整 A2UI：在 IM 内发送链接，跳转 JiuwenSwarm Web companion 页面完成交互；IM channel 只承担通知和入口。

这个方向能保留 A2UI 的核心价值，同时避免把第三方 IM 客户端当作可控 Web runtime 使用。
