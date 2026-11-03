---
id: F_20261103_1036_topic-list-presentation
title: 首页话题推荐标题与无边框列表
type: F
author: Rossoneri
author_email: 863031676@qq.com
modules:
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel
files:
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/ChatPanel.css
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
designs: []
design_note: 按用户提供的列表样式参考实施，无独立设计。
---

# 首页话题推荐标题与无边框列表

有话题卡片时在列表前显示“为你推荐”，提供对应英文文案；关闭或尚无推荐时不显示空标题。
条目默认透明且无边框，移除蓝色悬浮描边及浅蓝背景，悬浮或内部焦点使用主题 surface-hover 背景。
条目宽度为推荐容器的 100%，取消原 440px 上限；推荐容器与聊天输入框均采用 min(100%, 768px)，使悬浮背景左右与输入框对齐。关闭按钮移除后使用单列网格，不保留动作列占位。
保留点击推荐、原有圆角及减少动态效果设置。用户后续要求移除右侧关闭按钮，因此移除该按钮及对应不感兴趣回调、待处理状态和专属样式；后端反馈协议及事件构造函数保持兼容。

验证：npm.cmd run test:recommendation，18 项通过。浏览器工具存在 URL 识别限制，未提供实际页面截图验证。

移除关闭按钮后重验：推荐测试 17 项通过，将两个已废弃的关闭交互用例替换为无关闭按钮的展示回归，保留已有点击及事件测试。
