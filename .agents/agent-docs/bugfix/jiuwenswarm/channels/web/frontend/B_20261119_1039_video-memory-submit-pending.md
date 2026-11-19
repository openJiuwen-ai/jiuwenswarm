---
id: B_20261119_1039_video-memory-submit-pending
title: 视频记忆任务 pending 导致提交工具持续执行中
type: B
author: ToNy_St4rk
author_email: dengjiahao9@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/features/tool-events/toolEventNormalizer.ts
  - jiuwenswarm/channels/web/frontend/tests/toolEventNormalizer.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/videoMemoryToolLifecycle.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/fixtures/videoMemoryToolExports.ts
  - jiuwenswarm/channels/web/frontend/package.json
designs: []
design_note: 现有工具结果状态的局部修复，无独立设计。
---

# 视频记忆提交工具持续执行中修复记录

## 现象与根因

`agent_memory_video_submit` 已返回 `job_id`，但视频任务的 `raw_output.status=pending`
被前端当作工具执行状态。`chatStore` 因而保留 pending，卡片显示执行中。
后续视频解析完成或独立调用 `agent_memory_video_status` 不会结算原提交调用。
本地历史日志中的提交结果已真实返回，回放旧 normalizer 得到 success=true、pending=true。

## 当前行为与修复决策

视频提交和状态查询工具返回的 pending 视频任务不延长本次工具调用生命周期。
在共享 normalizer 中仅排除这两个工具从业务输出推导的 pending；显式工具执行 status
仍然优先，其他工具保持现有 pending 行为。结果文本中的 job_id、视频状态和记忆 ID 保留。
实时消费和历史恢复共用该 normalizer，不增加后台轮询，也不把任务状态改写为 succeeded。
此次不修改后端、事件协议或展示组件，没有对应集中前端状态 spec。

English: A pending video ingest job no longer keeps a completed submit/status tool call
running. Explicit execution status and other tools retain their existing behavior;
the original job result remains available in both live and restored history.

## 回归证据与适用范围

基线：`xiaoyifanswarmToMemory` 的 `1ce9928d7`；环境：Windows、现有 Node/esbuild。
新增 normalizer 回归在修复前 6 项失败，修复后通过。

- `npm run test:tool-event-normalizer`：13 项通过，覆盖视频 submit/status、输出命名兼容、
  显式执行 pending/error/timeout、失败与其他工具的 pending。
- `npm run test:video-memory-tools`：2 项通过，验证真实 chatStore 在提交后立即 completed，
  后续查询 succeeded 时原提交仍 completed；历史 JSON 回放恢复为 completed 并保留原始任务结果。
- `npm run test:auto-reviewer-events`：13 项通过，保留权限拒绝、超时及普通异步工具行为。
- 将本地真实提交日志重新输入修复后的 normalizer，得到 success=true、pending=false，
  原始 jobStatus 仍为 pending。

本次验证的是事件消费、状态存储与历史恢复，未重新运行视频解析服务或浏览器端到端测试。
normalizer 或这些消费链发生变化时，重跑上述回归。

后续 rebase 到 `upstream/develop` 的 `3c3556f61`，通过重新生成索引解决文档索引冲突，
代码保留上游新增回归。三个测试入口重新执行，分别 14、2、13 项通过（共 29 项）。
相对上游仅保留本修复一个 `fix(memory)` 提交，目标分支统一为 `xiaoyifanSwarmToMemory`。

发布分支同步：`release0.2.9` rebase 到 `upstream/releases/0.2.9` 的 `81fb09f51`，
移植同一修复，代码无冲突，文档索引重新生成。三个入口分别 14、2、13 项通过。
打包测试出现发布基线中英文资源既有重复键警告；本修复未修改这些资源。

## 防错模式与遗留

业务任务状态与调用状态必须分别判断。本次约束通过 normalizer 局部注释和正反回归维护，
不扩展为全仓工具状态迁移。无新增已知遗留；后台视频任务仍通过状态工具查询。
