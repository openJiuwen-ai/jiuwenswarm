---
id: F_20261103_1106_app-market-electron
title: 应用市场分端浏览器容器
type: F
author: default
author_email: default
modules: [jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend, jiuwenswarm/extensions/app_market]
files: [jiuwenswarm/channels/desktop/electron/app_market_windows.cjs, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx]
designs: []
design_note: 用户明确授权初步本地实现，无独立设计文档。
---

# 应用市场分端浏览器容器

应用市场原入口复用聊天文件预览逻辑，受会话布局和 headless 设置影响。改为应用专属容器，避免将应用页面生命周期绑定到聊天会话。

## 最终实现与边界

- Web 使用右侧 sandbox iframe 面板，按 app_id 复用实例；收起或切走市场只隐藏，保留页面运行状态。应用需要允许 iframe 嵌入，不绕过浏览器安全策略。
- Electron 使用独立 BrowserWindow，按 app_id 复用 persist 分区。采用 WorkSwarm 图标与标题，不显示任务栏按钮；最小化后从市场恢复，不重载页面。初版按用户选择只保留独立窗口；后续“编辑”入口的局部替代见下方修订，常规“打开／恢复”仍使用独立窗口。
- 市场停止或删除成功后只关闭对应页面；停止失败保留页面，窗口清理错误不回滚已成功的服务停止。手动关闭页面不停止服务。
- 入口缓存与页面跳转分离，导航代次保护并发取消/完成；保留原生 POST 及弹出页正文、Content-Type/boundary 和 referrer。
- 浏览器容器不负责游戏存档或 NPC session 生命周期。Web 与 Electron 存储不合并；同源规则和服务端口变化限制见当前契约。

当前接口与不变量见 [S_01_app-market-windows](../specs/jiuwenswarm/channels/desktop/S_01_app-market-windows.md)。未新增依赖或服务端生命周期。

## 验证

最终分端实现通过独立标准审核，无阻塞发现。定向行为验证包括窗口/browser panels 19 项、前端组件/文案 24 项、桌面契约 44 项，共 87 项。用户已验证 Web 展示；本地源码启动检查确认对应进程与端口，未构建安装包。

真实任务栏分组、最小化动画、跨进程存档、真实 POST 收包和 macOS/安装包运行未作专项验证。完整开发过程与运行证据保留在工作区外，不作为提交内容。

## 2026-11-06 Web 展开布局修订

原展开按钮仅隐藏列表，仍受扩展页标题区约束，导致顶部留白。按用户要求，仅在既有展开状态下使用固定定位覆盖整个浏览器页面视口，并去掉面板外框和圆角；保留工具栏以恢复分栏、收起或关闭。复用现有 ResizeObserver 同步 iframe 边界，不修改页面实例、存储或 Electron 窗口逻辑。

验证基线为 origin/feat/app-market 的 fb39204c5；本次仅修改布局样式和契约/开发记录。`npm run test:app-market` 的 23 项组件测试通过，覆盖展开切换、隐藏/恢复、模态遮挡和页面生命周期；这些测试不验证 CSS 像素布局，尚未进行真实浏览器布局验证。

## 2026-11-06 应用编辑会话入口

按用户确认同时支持 Web 与 Electron：卡片“编辑”复用启动和页面注册流程，待应用运行后通过既有新会话入口预填名称与源码路径，展开工具侧栏中的应用页签，不发送消息。预览关联作为 sessionStore 的本地展示状态，在正式会话创建的统一入口迁移，与一次性模型 metadata 分离。新会话路由的默认收起逻辑仅在无应用预览时执行，避免覆盖编辑入口的展开意图。

Web 复用已有 iframe；Electron 将独立窗口改为 BaseWindow 承载 WebContentsView，以便将同一原生页面移入主窗口，保持 app_id、持久分区及运行中页面状态。继续复用原导航、POST、权限、关闭和最小化逻辑，不接入会话自动化浏览器。ExpandedPanel 只新增可选附加页签插槽，应用状态与容器仍由应用市场模块负责；编辑侧栏不重复绘制市场工具栏。

验证基线为 fb39204c5 加本地候选。应用市场组件 30 项、文案 4 项、Electron 页面与浏览器注册 21 项、桌面契约 44 项、草稿/正式会话迁移 2 项通过。Web 实机验证点击编辑后进入新会话并展示2048与源码提示，放大可占满页面并恢复分栏，未发送消息或操作棋盘。iframe 层级随承载面板同步，避免被工具面板全屏层盖住；监听祖先样式变化并排除 iframe 自身样式写入，避免观察器自循环。Electron 原生冒烟验证视图挂载、尺寸与隐藏、往返独立窗口、WebContents 身份不变及销毁清理通过；证据位于工作树外 issue-evidence/app-market/edit-preview-20261106。完整 Vite 前端构建通过（存在既有大包与动态导入提示），最后的视口层级与离开市场取消跳转修正另经组件打包和回归验证；尚未进行完整桌面会话 UI、macOS 或安装包验证。编辑预览关联仅在当前前端运行期维护，刷新后从市场重新进入。

### P2 修复与编辑预览重载

标准审核发现：独立窗口手动 X 后原生页面已销毁，前端按运行实例的登记仍存在；返回原编辑会话时原视口接口仅返回 false，造成持续空白。修复复用既有 openMarketApp：编辑预览挂载前先读取实例并确保原生页面存在；只在就绪后挂载视口。已有页面复用，关闭后的页面按当前入口重建并沿用持久分区。恢复失败清除当前会话关联并提示，不新增页面查询 IPC，也不将正常隐藏的 false 误判为故障。

用户补充编辑时需频繁重载，增加顶栏刷新操作：应用模块提供动作，工具面板只增加可选通用 actions 插槽。Web 重新导航现有 iframe 至入口，Electron 新增受信 reloadMarketApp IPC 调用现有 WebContents.reload；不启动/停止服务、不新建会话、不清存储。页面内存态会重置，Web 的应用内子路径会回到入口。

本轮定向验证：应用市场组件34项、文案4项、Electron容器/注册22项、桌面契约44项、会话迁移2项，共106项通过。新增覆盖手动关闭后恢复、等待就绪后挂载、恢复失败移除关联及两端连续重载；真实Electron恢复冒烟验证手动关闭→重建→同一持久Session→两次加载完成→清理通过，证据为工作树外 native-preview-recovery-smoke.cjs / native-recovery-result.json。普通Web已实机验证编辑会话内连续点击刷新两次，应用重新加载且会话源码提示和页签保持；本轮未重跑完整构建、macOS或安装包测试。后续独立标准复审另行记录结论。

### 真实空会话、来源项目与刷新实例隔离

用户确认编辑入口应直接进入已创建但没有消息的会话，并归属应用来源会话的原项目，替代上一版最终停在新建草稿欢迎页的体验。读取市场已有 source_session，通过 projectRegistryClient 查询来源会话 metadata 与项目；当前选择的其他项目不参与归属。来源会话/项目不可读或项目已隐藏时明确失败。App 复用原本为应用插件即时创建会话的既有实现，将其更名为 ensureConversationSession，不复制 session.create/注册/迁移流程；未发送草稿随真实会话迁移。ChatPanel 仅新增通用 conversationCreated 展示参数，应用业务留在 App/市场模块，不伪造消息触发普通布局。

修复复审新增的刷新 P2：在真正调用 Web/native 重载前，使用既有 hasAppMarketView 核对 app_id 与 instanceId；过期关联直接拒绝并提示，不能重载同应用的新实例。未新增 IPC 或替代事实源。

本轮验证：市场组件40项通过（含Web/Electron旧实例刷新拒绝、原项目与work_mode选择、来源删除/项目缺失/隐藏的失败分支）；文案4项、草稿迁移2项通过。ChatPanel 定向行为测试验证真实空会话使用底部输入框，消息数组仍为空。已有原生恢复/重载22项、桌面契约44项代码前提未变，沿用上一轮证据。组件测试打包包含当前市场代码；ChatPanel/App以既有buildTopicStarterModelHandoff构建并定向测试，无tsc/格式化。

实机普通Web点击2048编辑，进入真实 /chat/web_1a1b01ebdd3_452df446f662，普通会话标题与底部输入框出现，应用在右栏。后端核对来源与新会话 project_id 均为 default，work_mode均为work；新会话 history.jsonl 为0字节，未发AI消息或操作游戏。该原应用未属于2048命名项目，因此非默认项目场景使用定向替身验证。完整最终Vite构建、完整Electron会话UI、macOS/安装包仍未重验。


### 固定编辑会话与取消交接（2026-11-06）

用户新增每个应用固定编辑会话的要求。本轮替代前述每次创建和 new 草稿迁移路径，删除前端来源项目解析 helper 与不再使用的预览迁移改动。应用市场私有 AppEditorSessions 按应用串行处理查找/创建/绑定；清单新增可缺省的 editor_session_id、editor_create_token，重新发布保留。通过注入的 Core client 和既有 fetch_agent_unary 获取会话/项目、创建会话，Gateway 不导入 Core runtime 或访问用户会话文件。正常恢复不依赖来源；仅 NOT_FOUND 重建，临时错误保留绑定。清单原子替换并传播保存错误，创建期间应用删除不会复活清单。创建令牌用于同一 Core 进程内超时重试；它不是跨 Core 重启的事务日志，极端创建成功但绑定未落盘并同时 Core 重启的情况仍可能留下未绑定空会话。

前端直接等待持久会话结果，再走既有会话恢复链，首次预填未发送编辑输入，之后不覆盖草稿/历史。复审 P2 的根因路径已移除：不再进入 new/改写当前项目；市场意图撤销后忽略结果，恢复队列在执行前及 session.switch 后再次校验 canActivate，避免晚到响应强制切页。后台完成但未展示的会话保持已绑定，下次点击恢复。

定向验证：市场组件 40 项（含实际 App 编辑回调结合挂载市场的 Web/Electron 离开取消、旧会话草稿保留）、文案 4 项、后端编辑绑定/发布/运行/配置 34 项通过。首次 Python 测试使用系统默认编码导致既有中文 HTML 夹具失败，设置 PYTHONUTF8=1 后通过；未修改该既有用例。后续实机和独立审核另行补充。

用户补充预览关闭后缺少恢复入口。标题栏通用 headerActions 插槽承载市场私有 AppMarketEditorAction，按 app_market.list 的持久 editor_session_id 识别关联（页面刷新同样有效），恢复已有实例或先启动，复用与市场相同的 openAppMarketView，不创建会话。关闭展示关联不影响持久绑定；普通会话无按钮。新增5项两端恢复、停服启动和取消/无关联测试通过，市场组件共45项。实机已验证两次市场编辑回到同一 web_1a1b03d91fe_ee3fe288ee20，项目2048，未发送消息。

最终普通 Web 实机验证：刷新已绑定编辑会话后标题栏按钮仍出现；点击恢复预览，点击页签关闭后按钮仍保留，再次点击恢复同一会话的2048预览。持久清单绑定与后端 metadata 项目 proj_eb670372（2048）一致，历史无消息。截图留在工作树外 editor-preview-reopen.png。服务来源均为 feat/app-market 的 fb39204c5 加本地候选；前端5173、Gateway19000/19001、AgentServer18092，沿用源码虚拟环境。


### 首次编辑提示交接 P2 修复（2026-11-06）

独立标准审核覆盖37项候选，确认一项P2：运行中应用首次编辑连点，或取消首次进入后重入，首次 created:true 响应因意图取消而被丢弃，后续 created:false 不预填源码提示。此次按应用合并前端在途 Promise；请求成功时先初始化新会话自身的草稿，再独立判断是否导航。无需新增后端字段、RPC、持久事务或提示交接协议。仅在 created:true 且该会话尚无 ChatRuntime 时预填并建立 runtime；已有 runtime（包括主动清空输入）不覆盖。取消不触碰当前会话/新任务草稿，但保留目标会话的本地输入供重入。请求无论成功失败均移出 Map，重试可以重新发起。

本轮在修改前先核对并打开既有源码 Web：feat/app-market 基线 fb39204c5，5173/19000/19001/18092 监听进程均来自同一工作树和 .venv。fetch/ff-only 确认远端无更新。修复后市场组件51项通过，新增6项覆盖Web/Electron实组件连点合并、取消完成后重入、其他草稿隔离、已有/主动清空草稿保留、失败后重试，使用真实 chatStore 和实际 App 回调。独立审核结论仅适用前一候选；本轮修复尚未独立复审，未提交/推送。
