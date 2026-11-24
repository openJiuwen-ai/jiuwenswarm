---
id: F_20261123_1734_web-interface-font-scaling
title: Web 前端界面字号联动
type: F
author: alan_cheng
author_email: chengshuo5@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/index.css
  - jiuwenswarm/channels/web/frontend/src/multi-session/sidebar/ConversationSidebar.css
  - jiuwenswarm/channels/web/frontend/src/features/settings/components/SettingRow.css
  - jiuwenswarm/channels/web/frontend/src/features/settings/components/SettingsSection.css
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/appearance/AppearanceSettings.css
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
designs: []
design_note: 复用既有外观设置并补齐字号变量的主要消费方。
---

# Web 前端界面字号联动

开源版沿用「设置 → 外观 → 界面字号」和 90%、100%、110%、125%、140% 五档。
既有实现（`0472116b0`）在选择档位时更新根元素字号变量，并用 `jiuwenswarm.appearance.v1` 保存偏好。
前端启动时读取同一记录，选回 100% 可单独恢复字号。

本次让普通及流式聊天正文、Markdown、团队消息、会话列表标题及元数据、共享设置行与分组标题使用既有字号变量。
输入区已有变量消费逻辑，继续随档位更新。设置行及预览等文字随字号调整行高，100% 保持原有基准。
沿用根元素字号变量可让文字立即更新，同时保留组件间距、控件布局和历史存储格式。
中英文快速开始与设置说明同步更新。

## 验证

2026-11-23 在 macOS、Node v25.6.1 上，以 `599f01243` 加本次工作区差异验证。

- `npm run test:appearance-preferences` 通过 15 项，覆盖字号计算、坏数据回退、偏好读写和默认值恢复。
- `npm run build` 通过，构建仍报告依赖注解及包体积警告。
- Playwright Chromium 从真实设置页切换五档，验证设置项和预览即时更新、刷新后输入区继续应用 140%、恢复 100% 及清除本地偏好。600px 视口下验证最大字号和控件布局。
- 使用浏览器样式探针核对 Markdown、流式正文、团队消息和会话列表的实际计算值。消息发送和真实模型流式响应未纳入此次验证。
- 本地检视核对设置入口、字号变量消费方、默认值与持久化契约。企业版入口、Electron 客户端及最低版本 Chrome 107 未做运行验证。开源 Web 外壳已有 584px 最小宽度。
