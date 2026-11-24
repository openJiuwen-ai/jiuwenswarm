---
id: B_20261120_1024_entity-header-tag-overflow
title: 实体头部标签溢出处理
type: B
author: null
author_email: null
author_note: 创建时仓库有效 Git user.name 和 user.email 均未配置，作者身份待补充。
modules:
  - jiuwenswarm/channels/web/frontend/src/components/ui/EntityHeader
files:
  - jiuwenswarm/channels/web/frontend/src/components/ui/EntityHeader/EntityHeader.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ui/EntityHeader/EntityHeader.css
  - jiuwenswarm/channels/web/frontend/tests/entityHeaderTags.test.mjs
  - jiuwenswarm/channels/web/frontend/package.json
designs: []
design_note: 按用户确认的 jiuwenclaw 修复方案迁入同源缺陷修复，无独立设计文档。
---

# 实体头部标签溢出处理

## 现象与根因

`jiuwenswarm_develop` 的 Skills 页面经 PageCard 使用 EntityHeader。两个标签
`Source: SwarmSkillHub` 和 `Team Skills` 在可用宽度不足时出现硬裁切。
根因是标签数量不超过两个时跳过宽度测量，隐藏测量区也仅在超过两个时渲染；
单行标签不收缩，而标签容器隐藏溢出，第二个标签因此被部分裁掉。
此外，原算法强制保留至少一个标签，无法覆盖首标签加省略号也放不下的情况。

## 修复决策与适用范围

- 修改前核对两个分支的 EntityHeader.tsx/CSS 与 jiuwenclaw 修复前基线一致，迁入已确认方案。
- 所有标签数量统一测量，保留可完整显示的有序前缀，并为溢出提示预留宽度。
- 允许零个标签可见，只显示省略号；title/aria-label 保留全部标签文字。
- 标签行维持 20px 行高，提示宽度不超过容器；零宽时同样计算并监听恢复。
- 同时观察标签行和隐藏测量区，标签内容或实际尺寸变化后重新计算。
- 调用方的公共 props、标签顺序、业务文案保持原有契约；适用于卡片、详情头部和
  OfficeSuite 直接使用的标签列表，没有后端协议改动。

## 回归证据

- 候选：基线 `d26e3aba3b84366e30192b9777ebf2c5a843e8c7` 上的本次工作区差异。
- 环境：Windows、Node 24.22.3，临时依赖 React/React DOM 18.2.0、JSDOM 26.1.0、
  esbuild 0.25.12；未更改项目依赖或锁文件。
- `npm run test:entity-header-tags`：7 项通过，覆盖 0/1/2/多个标签、精确容纳、
  首标签加提示放不下、零宽与极窄空间、变宽恢复、同数量文案变化和观察器清理。
- Chrome 无头浏览器加载该分支真实组件和 CSS：0/1/2/3 个标签分别验证
  500/200/50/10/0/500px，共 24 组，无可见标签或提示越出容器。
  携带操作区的实体头部在 380/260/500px 下分别显示 1/0/2 个标签，宽度恢复正常。
- 修复前同样的浏览器诊断确认：两个标签在 200px 被裁切；三个标签在 50px 仍保留
  放不下的首标签。修复后上述触发场景均由溢出提示承接。
- 验证为组件级渲染与布局，不涉及真实后端或已部署页面；未运行全量前端构建、格式化或静态检查。

## 资料与后续

测试锁定“可见标签必须完整放下；否则可全部收进提示”的组件行为，不增加全仓规则。
按 Web 规范登记 entity-header 测试标识前缀。故障记录和文档索引保留本次证据。
当前仓库缺少 Git 作者配置，明确保留 author 为 null，不猜测身份。
完整 sync_documents.py 命令因 Git 身份缺失未运行成功；使用该工具现有的
read_document/render_index 渲染函数刷新总索引与故障索引，不修改历史文档作者元数据。
组件或布局、字体、依赖发生影响宽度的变化时，应重跑上述验证。
本次未提交、推送或发布，部署环境须重新构建前端后应用修复。

## 2026-11-20：PR 10682 合并验证

以修复提交 `551653235a39e5e06a4f2615f400563f727623fb` 合并官方 develop
`5a160db762f566d43e0a88ee2d0b38f7b3b956de`，使用 `--no-commit --no-ff`，不提交或推送。
唯一冲突是总文档索引；按合并后的源文档重新生成索引，同时保留官方新增条目和本故障条目，
生成文件换行与官方 LF 保持一致。标签 TSX/CSS 与原修复提交一致。

候选验证：标签回归 7 项、国际化 6 项、代码模式发布/变更状态行为 9 项通过；
Vite 生产构建通过。合并前端产物在独立 25174 端口连接既有 run-env 后端，
以实际 SwarmSkillHub 小学辅导技能验证 900/1100/1440/1590/1920/900px 视口，
完整标签、省略号标题和宽度恢复均通过，无页面脚本错误。验证结束后关闭临时监听。
本轮没有运行全量后端测试，也没有替换当前 run-env 正式服务的后端源码。
