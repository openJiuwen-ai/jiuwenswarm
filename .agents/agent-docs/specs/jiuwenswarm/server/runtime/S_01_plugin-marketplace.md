---
id: S_01_plugin-marketplace
title: 插件市场列表归属与操作契约
type: S
author: lizhiyuan
author_email: lizhiyuan65@huawei.com
modules:
  - jiuwenswarm/server/runtime
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/runtime/extension_package_manager.py
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketplacePage.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MyMarketCard.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/MarketCard.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/PluginDetailPage.tsx
---

# 插件市场列表归属与操作契约

最近修订日期：2026-11-16。

## 范围与事实源

本契约适用于 `plugin_packages.list` 和 Web 的插件广场、我的插件、插件详情。
插件目录中的 `manifest.json` 仅证明存在包定义；安装状态来自 `marketplace.json` 的对应记录。
安装记录不存在时按未安装处理，不凭目录存在推断已安装，不清理用户草稿。

## 列表归属

- `filter=mine` 只返回已安装插件；本地草稿、未安装本地包和未完成安装的 Hub 包均不返回。
- `filter=builtin+hub` 继续提供广场目录，保留已安装与未安装插件。
- Hub 插件继续使用市场资产 ID 展示，以 `packageName` 关联本地目录，筛选不改变身份映射。
- 旧 `filter=local` 的兼容语义保持；专家、专家团、连接器列表不随插件规则改变。

## Web 操作

- 我的插件卡片提供“使用”，不得调用安装；依赖未就绪时可以进入详情处理连接。
- 广场未安装插件保留“安装”；已安装插件保留“使用”。
- 从我的扩展进入详情时不提供“安装”。实际使用仍要求插件已安装且依赖已就绪。
- 我的连接器继续保留其安装、连接、使用流程，不套用插件的按钮规则。

## 验证入口

后端通过插件本地列表与 Hub 聚合列表验证安装筛选、来源映射及兼容行为；
前端通过卡片与详情的渲染、点击行为验证操作限制，并覆盖广场及我的连接器对照。
