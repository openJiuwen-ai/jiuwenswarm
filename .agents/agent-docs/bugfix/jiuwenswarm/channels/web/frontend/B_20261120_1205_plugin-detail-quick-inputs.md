---
id: B_20261120_1205_plugin-detail-quick-inputs
title: 未安装 Hub 插件的「试试这样用」显示空白
type: B
author: dingchi
author_email: dingchi5@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/services/pluginPackagesApi.ts
  - jiuwenswarm/channels/web/frontend/tests/pluginDetailInstall.test.mjs
designs: []
design_note: 修复现有插件详情输入兼容性，不改变后端协议或操作契约。
---

# 未安装 Hub 插件的「试试这样用」显示空白

## 现象与根因

0.2.9 的未安装 Hub 插件「PR质量检查」有三条示例，但详情页只渲染出三个空白条。详情免下载后，远端预览改读 Hub 版本元数据而不再读包内 manifest。`plugin_packages.show` 的 Hub 路径返回纯字符串，本地路径则可返回 `{ zh, en }`。前端适配层直接透传，详情页按语言属性取值时得不到文字。

## 修复决策

`pluginPackagesApi.ts` 在详情适配边界接受两种格式：字符串使用原文填充 `zh` 和 `en`，已有双语对象保持原样。页面继续接收 `LocalizedText[]`，不改未安装时的点击限制，没有恢复 ZIP 下载，也不修改 Hub、MCP 或专家的接口。

## 回归证据

在远端 `releases/0.2.9` 的 `62b38de5b` 基线上，用 Node 22 / JSDOM 渲染真实插件详情、store 和 API 适配层，仅替换 RPC 传输。新增测试在修复前得到三个空字符串，修复后中英文界面都能显示三条原文，未安装时为不可点击的 `span`；模拟安装就绪后双语文案按界面语言显示，点击回调收到可预填的文案和插件运行时名称。`npm run test:plugin-mine-actions`（17 项）和 `npm run test:plugin-replacement`（9 项）通过。未在修复后重新部署或连接真实 Hub 进行浏览器验收。
