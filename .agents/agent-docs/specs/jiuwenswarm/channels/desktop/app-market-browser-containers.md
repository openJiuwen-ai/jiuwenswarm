---
id: S_01_app-market-browser-containers
title: 应用市场分端展示契约
type: S
author: default
author_email: default
modules: [jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel]
files: [jiuwenswarm/channels/desktop/electron/app_market_windows.cjs]
---

# 应用市场分端展示

- 普通 Web 保留应用市场右侧内嵌面板，使用 sandbox iframe；Electron 的“打开／恢复”使用独立 BaseWindow 承载 WebContentsView；“编辑”将同一 WebContentsView 移入主窗口的会话侧栏。两端均按稳定 app_id 复用页面。
- Electron 受信前端通过 openMarketApp(appId, url, title, embedded?) 与 closeMarketApp(appId) 调用桌面 IPC；只接受无凭据的 loopback HTTP/HTTPS 初始入口。独立窗口标题以“应用名 · WorkSwarm”为前缀并显示页面 CPU、内存，图标复用主窗口解析入口。
- Electron 窗口设置 skipTaskbar，不显示独立任务栏按钮；最小化后隐藏，从市场“打开／恢复应用”恢复现有窗口，不重载页面。隐藏期间的导航完成不重新弹出。Windows 不设置原生 parent，其他平台保留 owner。
- 每个 Electron 应用沿用 persist:app-market-<编码appId> 分区；页面 sandbox/contextIsolation 开启，无 Node 或主界面 preload，权限默认拒绝。主窗口销毁时清理全部应用窗口。
- Web iframe 由前端注册表持有，面板卸载/收起仅隐藏实例。展开时面板固定占满浏览器页面视口，覆盖市场标题和导航区域，仅保留应用工具栏；恢复时回到原分栏且不重建 iframe。支持关闭面板、模态遮挡隐藏和恢复不抢焦点。应用必须允许 iframe 嵌入，市场遵守 CSP/X-Frame-Options。Web iframe 与 Electron 原生视图通过同一面板的尺寸同步接入，各自使用 webAppViews.setViewport 与 setMarketAppViewport，不接入聊天自动化浏览器或 headless 配置。两端按运行实例跟踪页面，实例结束后清理对应容器，见 [S_02_app-market-stop-and-page-lifecycle](../../extensions/app_market/S_02_app-market-stop-and-page-lifecycle.md)。
- 手动关闭应用窗口/页面不停止服务；停止/删除 RPC 成功后关闭对应容器，失败保留。停止成功后的窗口清理异常单独提示，不回滚服务状态。显式停止销毁不执行 beforeunload，应用不能以卸载作为唯一存档点。
- Electron 仅最新入口导航可提交缓存或清理失败，市场入口和页面目标分开。原生 HTTP 导航保留 POST，popup 转入同窗口并保留正文、Content-Type/boundary 和 referrer；非 HTTP 导航拒绝。
- Electron 持久分区与 Web 浏览器存储不合并。Web 使用每个应用专属的 `*.localhost` 主机，浏览器数据按协议、主机和端口保存。原 `127.0.0.1` 来源及不同端口的数据不会自动迁移。多存档、世界状态和 NPC session 绑定由应用管理；市场容器不管理聊天会话或浏览器 headless。
- 桌面窗口说明与 Web 面板说明分开提供，确认状态只存在前端 localStorage，不参与游戏存档。旧 Electron 缺少打开接口时提示更新重启。

- Web/Electron 应用卡片的“编辑”复用应用清单中持久绑定的编辑会话；没有绑定或确认会话已删除时，通过应用市场的 app_market.edit_session 在来源会话原项目创建真实空会话并保存绑定。Gateway 仅经既有 E2A 请求查询项目、创建和读取会话，不访问 Core 会话文件。绑定会话已存在时保留历史、模型与未发送输入，不依赖来源会话继续存在；只有首次创建且本地尚无该会话运行态时预填应用名与源码入口，不自动发送消息；同应用在途编辑请求合并，目标草稿初始化独立于是否激活导航，取消后重入仍可使用。已有草稿或主动清空的输入不得覆盖。临时查询失败不解除绑定。重复点击/客户端并发在市场按应用串行化；重新发布保留绑定，删除应用不删除编辑会话。请求期间离开市场会取消前端激活，后台已完成的绑定可在下次复用；不再借用 new 草稿。真实空会话使用普通布局及底部输入框。关闭预览页签只移除本次展示关联，标题栏“打开应用预览”基于后端绑定始终可恢复预览（含刷新后），应用未运行时先启动；重新点击编辑仍打开绑定会话；收起、切走、关闭会话不停止应用。实例结束后移除预览关联。
- 编辑侧栏复用工具面板的放大、收起和关闭页签操作，仅补充应用资源读数与预览重载入口。Electron 页面在独立窗口/编辑侧栏间移动不导航、不复制 WebContents、不切换持久分区；主窗口退出或停止应用仍销毁原页面。

- 进入 Electron 编辑预览时先读取存活实例，通过既有 openMarketApp(..., true) 确保原生页面存在，再挂载视口。原生独立窗口被手动关闭但应用服务仍运行时，使用当前实例入口重建页面，沿用应用持久分区；已有页面不重载。恢复失败清除当前会话的展示关联并提示错误，不重启或停止应用服务。合法的隐藏返回 false 不作为失效信号。
- 编辑预览顶栏提供“重新加载应用预览”：Web 将现有 iframe 重新导航至应用入口，Electron 经 reloadMarketApp(appId) 重载当前页面；均不更换容器身份、会话或存储分区，不调用应用启停。未持久化的页面内存状态随重载重置；持久化数据仍由应用管理。

- 刷新执行前必须核对当前 app_id 的实例登记与 preview.instanceId 一致；过期会话关联不得刷新同应用的新实例。未匹配时拒绝刷新并提示错误，旧关联由现有实例校验清理。

- 资源每 3 秒采样：Electron 仅统计应用页面渲染进程，Web 仅统计服务端应用进程树，不包含浏览器页面或 AI 会话。服务采样用既有实例 token 与上下文文件名校验进程身份，支持后端重启后的存活应用；PID 本身不作为归属凭据。无法核实或采样失败显示不可用，CPU 首次采样等待下一轮。

- 市场顶部及绑定应用的编辑会话顶部显示整机 CPU 与内存百分比，应用窗口/预览继续显示自身占用。Electron 直接采本机；Web 采服务端，并按连接地址区分“服务端（本地连接）”与“服务器”，本地连接不保证未经过代理或转发，提示中说明该边界。整机采样独立于应用窗口数量，不新增常驻计时器；CPU 首轮未知，约 3 秒后取得增量，内存使用平台可用/可回收量估算，macOS 不将 file-backed 缓存计入占用。
- 资源提醒在市场顶部及绑定应用的编辑会话顶部显示，不弹重复通知、不自动停止应用。CPU 或内存连续 3 个不同采样点达到 85% 后提示；两项均低于 75% 时清除。失效采样、退出订阅和超过 10 秒的采样间隔重置提醒状态；后台 RPC 和 Electron IPC 沿用既有资源读取入口。

- 编辑会话整机读数与“打开应用预览”使用同一后端绑定判定，未绑定的普通会话不展示。关闭预览不移除会话顶部的整机读数；与预览自身读数共用前端资源订阅，不新增轮询。

- 整机 CPU 与内存百分比独立按数值变色：低于 50% 使用成功色、50% 至不足 85% 使用警示色、85% 及以上使用危险色；不可用使用灰色。只着色百分比数值，沿用主题语义变量，文字提醒仍遵循持续采样和恢复阈值。
