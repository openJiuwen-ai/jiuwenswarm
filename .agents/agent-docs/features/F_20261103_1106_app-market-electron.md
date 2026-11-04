---
id: F_20261103_1106_app-market-electron
title: 应用市场分端浏览器容器
type: F
author: default
author_email: default
modules: [jiuwenswarm/channels/desktop/electron, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel]
files: [jiuwenswarm/channels/desktop/electron/app_market_windows.cjs, jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx]
designs: []
design_note: 用户明确授权初步本地实现，无独立设计文档。
---

# 应用市场分端浏览器容器

应用市场原入口复用聊天文件预览逻辑，受会话布局和 headless 设置影响。改为应用专属容器，避免将应用页面生命周期绑定到聊天会话。

## 最终实现与边界

- Web 使用右侧 sandbox iframe 面板，按 app_id 复用实例；收起或切走市场只隐藏，保留页面运行状态。应用需要允许 iframe 嵌入，不绕过浏览器安全策略。
- Electron 使用独立 BrowserWindow，按 app_id 复用 persist 分区。采用 WorkSwarm 图标与标题，不显示任务栏按钮；最小化后从市场恢复，不重载页面。按用户最终选择保留此独立窗口形式，不提供 Electron 内嵌视图接口。
- 市场停止或删除成功后只关闭对应页面；停止失败保留页面，窗口清理错误不回滚已成功的服务停止。手动关闭页面不停止服务。
- 入口缓存与页面跳转分离，导航代次保护并发取消/完成；保留原生 POST 及弹出页正文、Content-Type/boundary 和 referrer。
- 浏览器容器不负责游戏存档或 NPC session 生命周期。Web 与 Electron 存储不合并；同源规则和服务端口变化限制见当前契约。

当前接口与不变量见 [S_01_app-market-windows](../specs/jiuwenswarm/channels/desktop/S_01_app-market-windows.md)。未新增依赖或服务端生命周期。

## 验证

最终分端实现通过独立标准审核，无阻塞发现。定向行为验证包括窗口/browser panels 19 项、前端组件/文案 24 项、桌面契约 44 项，共 87 项。用户已验证 Web 展示；本地源码启动检查确认对应进程与端口，未构建安装包。

真实任务栏分组、最小化动画、跨进程存档、真实 POST 收包和 macOS/安装包运行未作专项验证。完整开发过程与运行证据保留在工作区外，不作为提交内容。
