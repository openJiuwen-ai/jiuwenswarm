---
id: S_01_app-market-windows
title: 应用市场分端展示契约
type: S
author: default
author_email: default
modules: [jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel]
files: [jiuwenswarm/channels/desktop/electron/app_market_windows.cjs]
---

# 应用市场分端展示

- 普通 Web 保留应用市场右侧内嵌面板，使用 sandbox iframe；Electron 使用独立 BrowserWindow，不显示内嵌面板。两种容器不相互回退，均按稳定 app_id 复用页面。
- Electron 受信前端通过 openMarketApp(appId, url, title) 与 closeMarketApp(appId) 调用桌面 IPC；只接受无凭据的 loopback HTTP/HTTPS 初始入口。独立窗口标题为“应用名 · WorkSwarm”，图标复用主窗口解析入口。
- Electron 窗口设置 skipTaskbar，不显示独立任务栏按钮；最小化后隐藏，从市场“打开／恢复应用”恢复现有窗口，不重载页面。隐藏期间的导航完成不重新弹出。Windows 不设置原生 parent，其他平台保留 owner。
- 每个 Electron 应用沿用 persist:app-market-<编码appId> 分区；页面 sandbox/contextIsolation 开启，无 Node 或主界面 preload，权限默认拒绝。主窗口销毁时清理全部应用窗口。
- Web iframe 由前端注册表持有，面板卸载/收起仅隐藏实例。支持展开/恢复分栏、关闭面板、模态遮挡隐藏和恢复不抢焦点。应用必须允许 iframe 嵌入；不绕过 CSP/X-Frame-Options。Web 模块不依赖 Electron 视口接口。
- 手动关闭应用窗口/页面不停止服务；停止/删除 RPC 成功后关闭对应容器，失败保留。停止成功后的窗口清理异常单独提示，不回滚服务状态。显式停止销毁不执行 beforeunload，应用不能以卸载作为唯一存档点。
- Electron 仅最新入口导航可提交缓存或清理失败，市场入口和页面目标分开。原生 HTTP 导航保留 POST，popup 转入同窗口并保留正文、Content-Type/boundary 和 referrer；非 HTTP 导航拒绝。
- Electron 持久分区与 Web 浏览器存储不合并，均遵守协议/主机/端口的同源规则，换端口不自动迁移数据。多存档、世界状态和 NPC session 绑定由应用管理；市场容器不管理聊天会话或浏览器 headless。
- 桌面窗口说明与 Web 面板说明分开提供，确认状态只存在前端 localStorage，不参与游戏存档。旧 Electron 缺少打开接口时提示更新重启。
