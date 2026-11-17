---
id: B_20261114_1302_terminal-metrics-and-model-errors
title: RSI 终态统计、侧栏运行态和论文模型错误修复
type: B
author: l_x_d
author_email: linxiangda@huawei.com
modules:
  - jiuwenswarm/agents/harness/common/rsi
  - jiuwenswarm/channels/web/frontend/src/features/rsi
files:
  - jiuwenswarm/agents/harness/common/rsi/services.py
  - jiuwenswarm/agents/harness/common/rsi/artifact_adapter.py
  - jiuwenswarm/channels/web/frontend/src/features/rsi/rsiStore.ts
designs: []
design_note: 按用户确认修复现有行为及 Issue 6558、6539、6532，无独立设计。
---

# RSI 终态统计与模型错误修复

## 现象与影响

论文候选已剪枝但汇总剪枝和轮次仍为零；详情失败但侧栏运行点继续闪动；模型失败被泛化为质量不足。

## 实施计划

采用现有 Python Provider/服务及 React/Zustand 数据流，保持接口字段兼容，不运行格式化或静态检查，不自动提交。

- [x] 任务一：在 rsiPolling.test.mjs 补失败、完成、终止、暂停及晚到事件回归；运行观察失败；修复 rsiStore.ts 的列表同步、终态进度清理和终态保护；重跑通过。
- [x] 任务二：新增真实报告服务测试，覆盖已剪枝节点、显式 Provider 指标、缺失树及程序计数；观察失败后在服务/适配边界补齐缺失统计；运行 RSI 回归。
- [ ] 任务三：在确认的 core 工作区补论文模型异常回归；保留简短错误摘要与任务级失败原因；跨仓验证。
- [ ] 收尾：核对快照/推送竞态、恢复任务、旧 Provider 和中英文说明，记录测试证据并同步索引。

## 关联设计与当前契约

无独立 D。当前契约见 S_01_observation-projection；中英文使用说明位于 docs/zh/RSI.md 和 docs/en/RSI.md。

## 根因与修复决策

详情轮询只写 detail，不写 list，且保留 liveProgress；报告缺少 metrics 时补零，未从树统计；论文引擎把技术错误归为内容质量剪枝。
拒绝仅隐藏闪动点、前端数节点覆盖全部轮次或从日志猜原因：会掩盖状态不一致、混淆程序候选与完成次数，或跨越 Provider 边界。

## 回归证据与适用范围

基线 e616133c31，锁定 core f3f8f576。前端既有轮询测试 3 项通过。新增终态/暂停/晚到事件回归先失败后通过，评审再补首次详情为空和暂停竞态，先观察两项失败，再按请求期间状态版本修复。最终轮询测试 12 项通过。报告服务先观察缺失统计的三项失败，再补显式统计无需树、root 注册前报告、程序完成计数兼容回归，总计 6 项通过。RSI Python 整体回归最终 307 项通过。

浏览器组件 fixture（非真实后端会话）验证失败论文轮次为 1、剪枝为 1、无侧栏运行点、无 Harness 组合评测，1440 宽截图 /tmp/rsi-terminal-1440.png。Vite 构建通过，未运行 tsc/格式化/静态检查，未调用真实模型。独立检视指出首次读取及暂停竞态，已补测试修复；未提交或推送。

已有 rsiJudgeSelection.test.mjs 的 esbuild 配置缺少 EmptyState.svg loader，启动阶段报错；不属于本次业务差异，未改无关测试配置。

## 遗留与后续

#6539 仍待 core 工作区确认，尚未修改或验证 core；不能把此记录解释为三个 Issue 均已完成。当前未切换 openjiuwen 本地依赖，pyproject.toml/uv.lock 无改动，无 key/真实模型/代理实验。

## 2026-11-14 启动与依赖更新

用户随后要求启动 WorkSwarm，并将 agent-core 依赖改为 upstream/release/v0.1.21。pyproject.toml 的主依赖、harmony、overrides（保留 extras）及 uv source 均已切换到官方 AtomGit release/v0.1.21；uv.lock 解析到 581ef02a4c932dfeb0ad20421cf3cf4ffd6c6790。包元数据版本仍为 0.1.20，以实际 Git commit 验证来源。

执行 uv lock、uv sync、uv sync --check，通过导入与 Provider 装配 smoke check，新版本下 RSI Python 回归 307 项通过。通过 make start-debug 复用本次构建的前端，后台入口 PID 12327；Web UI http://localhost:5173 返回 200，AgentServer 18092、WebChannel 19000/ws、Gateway 19001 的 WebSocket 握手通过。启动 banner 将 19001 标为 Gateway HTTP，但该端口实际提供 WebSocket，普通 HTTP /health 返回 426，并非启动故障。

本次未创建/修改 agent-core 工作区，没有本地 editable 依赖；pyproject.toml/uv.lock 是用户明确要求的 release 分支依赖变更，尚未提交。#6539 的模型异常透传仍未修复，升级本身不等于已解决。
