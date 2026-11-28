---
id: S_01_app-market-browser-containers
title: 应用市场分端展示契约
type: S
author: default
author_email: default
updated: 2026-11-27
modules: [jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel]
files: [jiuwenswarm/channels/desktop/electron/app_market_windows.cjs, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/AppMarketPage.tsx, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/AppMarketAiRoles.tsx]
---

# 应用市场分端展示

- 页签按“应用广场”“我的应用”排列，默认打开“我的应用”。在我的应用中，点击运行中的卡片直接切换到该应用页面，使用同一运行实例已有的页面。运行中应用可从卡片菜单进入“查看详情”，停止的应用可点击卡片进入详情。应用广场卡片点击进入上架详情。列表页显示“应用”页头与副标题；进入应用详情或运行中的应用页面时收起页头；再次点击已选中的“我的应用”会收起运行页面并回到列表，不停止应用。
- 列表页沿用技能页的工具栏布局，左侧为主标签，右侧为两个页签共用的搜索框，资源监控和刷新放到页头。详情页隐藏列表工具栏，分栏预览时列表列仍提供页签和搜索。
- 我的应用在同一列表展示所有来源的应用，按名称与描述即时过滤。应用广场把去空格后的关键字作为 `query` 交给 Hub 服务端分页搜索，输入防抖 300ms。关键字变化（含清空）时回到第 1 页，同一关键字下重新进入页签时保留页码。有关键字但无结果时显示“没有匹配项”，空关键字下显示各自的默认空列表文案。
- 普通 Web 使用 sandbox iframe 承载右侧面板。支持视口同步的 Electron 将 WebContentsView 放在右侧面板，旧版 Electron 使用独立 BaseWindow。“编辑”将同一 WebContentsView 移入主窗口的会话侧栏。两端均按稳定 app_id 复用页面。
- Electron 受信前端通过 openMarketApp(appId, url, title, embedded?) 与 closeMarketApp(appId) 调用桌面 IPC；只接受无凭据的 loopback HTTP/HTTPS 初始入口。独立窗口标题以“应用名 · WorkSwarm”为前缀并显示页面 CPU、内存，图标复用主窗口解析入口。
- Electron 窗口设置 skipTaskbar，不显示独立任务栏按钮；最小化后隐藏，从市场“打开／恢复应用”恢复现有窗口，不重载页面。隐藏期间的导航完成不重新弹出。Windows 不设置原生 parent，其他平台保留 owner。
- 每个 Electron 应用沿用 persist:app-market-<编码appId> 分区；页面 sandbox/contextIsolation 开启，无 Node 或主界面 preload，权限默认拒绝。主窗口销毁时清理全部应用窗口。
- Electron 根据实际请求页面校验权限，应用容器内与当前应用入口同源的 HTTP/HTTPS 页面可申请鼠标锁定（`pointerLock`）。独立窗口还可申请标准全屏（`fullscreen`），嵌入主窗口的应用使用面板放大。浏览器继续校验用户操作和焦点。
- Web iframe 由前端注册表持有，面板卸载/收起仅隐藏实例。展开时面板固定占满浏览器页面视口，覆盖市场工具栏与列表区域，仅保留应用工具栏；恢复时回到原分栏且不重建 iframe。支持关闭面板、模态遮挡隐藏和恢复不抢焦点。应用必须允许 iframe 嵌入，市场遵守 CSP/X-Frame-Options。Web iframe 与 Electron 原生视图通过同一面板的尺寸同步接入，各自使用 webAppViews.setViewport 与 setMarketAppViewport，不接入聊天自动化浏览器或 headless 配置。两端按运行实例跟踪页面，实例结束后清理对应容器，见 [S_02_app-market-stop-and-page-lifecycle](../../extensions/app_market/S_02_app-market-stop-and-page-lifecycle.md)。
- 手动关闭应用窗口/页面不停止服务；停止/删除 RPC 成功后关闭对应容器，失败保留。停止成功后的窗口清理异常单独提示，不回滚服务状态。显式停止销毁不执行 beforeunload，应用不能以卸载作为唯一存档点。
- Electron 仅最新入口导航可提交缓存或清理失败，市场入口和页面目标分开。原生 HTTP 导航保留 POST，popup 转入同窗口并保留正文、Content-Type/boundary 和 referrer；非 HTTP 导航拒绝。
- Electron 持久分区与 Web 浏览器存储不合并。Web 使用每个应用专属的 `*.localhost` 主机，浏览器数据按协议、主机和端口保存。原 `127.0.0.1` 来源及不同端口的数据不会自动迁移。多存档、世界状态和 NPC session 绑定由应用管理；市场容器不管理聊天会话或浏览器 headless。
- 桌面窗口说明与 Web 面板说明分开提供，确认状态只存在前端 localStorage，不参与游戏存档。旧 Electron 缺少打开接口时提示更新重启。
- 应用详情对应的列表条目消失时返回列表。详情页删除或卸载成功后返回我的应用，重新安装注销旧登记后进入应用广场。列表和详情页共用右上角操作通知。安装目录输入、任务进度和取消入口在详情页保持可用。
- 从已安装应用详情选择“打开／恢复应用”时，确认应用打开后再返回列表。等待期间保留详情，失败时提示并允许在详情页重试。
- 已安装应用的描述首段作为卡片摘要。空行后的内容显示在详情页，支持 Markdown 段落、引用和 HTTPS 来源链接。

- Web/Electron 应用卡片的“编辑”通过 `app_market.edit_session` 在应用项目下创建真实空会话并保存绑定。同应用在途编辑请求在客户端合并，Gateway 按应用串行化，失败重试沿用同一 `edit_token`。Gateway 通过既有 E2A 请求查询项目、创建和读取会话。草稿独立于导航激活初始化，仅在新会话尚无本地运行态时预填元信息、源码入口和 `workswarm-app-builder` 技能引导，不自动发送。成功进入会话后，支持项目列表的侧栏切到会话所属工作模式、展开所属项目，并在侧栏可见时滚动到新会话。离开市场会取消前端激活。再次独立点击创建新会话，已有编辑会话保留历史、模型和草稿。
- 重新发布保留每个编辑会话的应用绑定，删除应用保留会话。关闭预览页签仅移除本次展示关联。绑定会话标题栏在刷新后可通过 `app_market.editor_app` 按当前所有者及编辑会话查询预览所需的应用信息，应用未运行时先启动。切换市场标签页会隐藏当前预览并撤销尚在启动的自动展示，应用服务继续运行。实例结束后移除该实例的预览关联。
- 编辑侧栏复用工具面板的放大、收起和关闭页签操作，仅补充应用资源读数与预览重载入口。Electron 页面在独立窗口/编辑侧栏间移动不导航、不复制 WebContents、不切换持久分区；主窗口退出或停止应用仍销毁原页面。

- 进入 Electron 编辑预览时先读取存活实例，通过既有 openMarketApp(..., true) 确保原生页面存在，再挂载视口。原生独立窗口被手动关闭但应用服务仍运行时，使用当前实例入口重建页面，沿用应用持久分区；已有页面不重载。恢复失败清除当前会话的展示关联并提示错误，不重启或停止应用服务。合法的隐藏返回 false 不作为失效信号。
- 编辑预览顶栏提供“重新加载应用预览”：Web 将现有 iframe 重新导航至应用入口，Electron 经 reloadMarketApp(appId) 重载当前页面；均不更换容器身份、会话或存储分区，不调用应用启停。未持久化的页面内存状态随重载重置；持久化数据仍由应用管理。

- 刷新执行前必须核对当前 app_id 的实例登记与 preview.instanceId 一致；过期会话关联不得刷新同应用的新实例。未匹配时拒绝刷新并提示错误，旧关联由现有实例校验清理。

- 资源每 3 秒采样。Web 和 Electron 的运行卡片在版本号旁显示 WorkSwarm 服务端采集的应用进程树内存，分栏和页面未打开时也可显示。Electron 的应用窗口和编辑预览沿用页面渲染进程读数。服务采样用既有实例 token 与上下文文件名校验进程身份，支持后端重启后的存活应用。无法核实或采样失败时读数不可用，CPU 首次采样等待下一轮。

- 应用市场列表页头统一展示整机 CPU、内存百分比及资源提醒。应用窗口和预览显示各自的资源占用。Electron 的本机读数与服务端应用内存分别刷新，服务端请求迟延或失败时，本机读数和告警继续更新。Web 使用同一次服务端采样，并按连接地址区分“服务端（本地连接）”与“服务器”，本地连接不保证未经过代理或转发，提示中说明该边界。整机采样独立于应用窗口数量，随资源订阅启动和停止。CPU 首轮未知，约 3 秒后取得增量，内存使用平台可用/可回收量估算，macOS 不将 file-backed 缓存计入占用。
- 整机资源提醒不弹重复通知、不自动停止应用。CPU 或内存连续 3 个不同采样点达到 85% 后提示；两项均低于 75% 时清除。失效采样、退出订阅和超过 10 秒的采样间隔重置提醒状态；后台 RPC 和 Electron IPC 沿用既有资源读取入口。

- 整机 CPU 与内存百分比独立按数值变色：低于 50% 使用成功色、50% 至不足 85% 使用警示色、85% 及以上使用危险色；不可用使用灰色。只着色百分比数值，沿用主题语义变量，文字提醒仍遵循持续采样和恢复阈值。
