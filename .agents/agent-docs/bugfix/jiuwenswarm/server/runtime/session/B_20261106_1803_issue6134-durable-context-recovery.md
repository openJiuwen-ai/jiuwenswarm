---
id: B_20261106_1803_issue6134-durable-context-recovery
title: Issue 6134 有效上下文持久化、旧历史迁移与原文外置
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
date: "2026-11-06"
designs: []
design_note: 用户在本次会话确认第二批方案并授权实施和验证；沿用已确认方案，不新增独立 D。
modules:
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/agents/harness
  - jiuwenswarm/runtime
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/extensions/video_duplex/backend
files:
  - jiuwenswarm/runtime/context_checkpointer.py
  - jiuwenswarm/runtime/checkpointer.py
  - tests/unit_tests/test_context_checkpointer.py
  - jiuwenswarm/server/runtime/session/context_snapshot.py
  - jiuwenswarm/server/runtime/session/context_migration.py
  - jiuwenswarm/server/runtime/session/history_stream.py
  - jiuwenswarm/server/runtime/session/history_artifacts.py
  - jiuwenswarm/server/runtime/session/recovery_budget.py
  - jiuwenswarm/server/runtime/session/session_history.py
  - jiuwenswarm/agents/harness/common/session_ops_service.py
  - jiuwenswarm/agents/harness/common/rails/stream_event_rail.py
  - jiuwenswarm/agents/harness/agent_observability.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/handlers/session.py
  - jiuwenswarm/server/wire_truncate.py
  - jiuwenswarm/extensions/video_duplex/backend/conversation_context.py
  - tests/unit_tests/test_context_snapshots.py
  - tests/unit_tests/agentserver/test_stream_event_rail_wire_budget.py
---

# Issue 6134 有效上下文持久化、旧历史迁移与原文外置

## 现象与根因范围

第一批解决全量读取放大、MemoryError 被当坏行跳过、重复 Worker 崩溃和错误回包问题，但超预算旧会话仍被拒绝，重启恢复仍缺少真正的压缩窗口边界，完整 prompt/大工具结果仍可能持续放大审计历史。本批完成已确认的第二批方案。

首次实现基于 Swarm `acf2b35b263d5813517d8b9fac187d1df0a9c017` 加第一批补丁，配套 Core 基于 `f3f8f576164cc405c046e43079c8ad00ee17f0c4`。Protocol/Gateway 固定版本沿用第一批。原日志仍不能证明全部 STALLED 由 OOM 导致；也没有原始 1.57 GB history、checkpoint、部署 wheel 和内核原始日志。

## 2026-11-06 原配套方案与决策（历史）

1. 将保存接到 Core 原生 `save_contexts()` 准备 hook：先原子发布不可变文件，再把引用与 context 放入同一原生 Session。检查点为唯一提交权威；hint 和未提交文件不能决定恢复。
2. 保存真实有效窗口、历史文件身份/消费边界、已消费请求与独立语音游标。恢复安装压缩窗口后只重放增量，区分排队/steer 输入，排除已消费请求的迟到输出；rewind 清理旧引用并重新绑定边界。
3. 用受限流式 JSON/JSONL 解析迁移旧大历史，分块调用实际配置的原生压缩器；诊断巨型字符串不整体解码。迁移临时使用零保护尾部，成功后恢复用户原压缩策略。仅审计摘要不能伪造恢复；有效 SDK 原生窗口可作为无原始语义记录时的保留来源。
4. Core 原生卸载内容转为不可变内容版本，检查点只含索引，按需加载原文。完整 prompt（messages/tools）和大工具字段存入会话工件目录，事件保留预览与 `payload_refs`；适配器、usage、history collapse、fork 和 `history.get` 分块读取贯通。
5. 新写入遵守 lifecycle/write_guard。恢复并发受限并支持取消。坏引用明确失败，旧数据/旧检查点保留；受管历史重写不会复活失效窗口。

以上为原配套方案。现行单仓契约见 `S_02_durable-context-recovery.md`；下方历史测试仅证明对应配套版本，不能替代单仓回归。

## 本地验证

证据根：`D:/work_space/.codex-temp/issue6134-phase2-20261106/`。

- Swarm 495 项、Core 132 项定向回归通过，0 失败、0 跳过；分别见 `regression-verified.xml`、`core-regression.xml`。
- `acceptance-final-result.json`：真实 RuntimeSupervisor/Worker/SDK/SQLite 检查点，Windows 原生进程，1536 MiB 私有内存 Job 上限；1,610,676,420 字节合成历史完成迁移。采样 RSS 峰值 421.82 MiB，私有内存峰值 903.98 MiB。人工压缩保留 4 条尾部的测试配置下，原生 checkpoint 与引用窗口相等，重启后的实际 HTTP 输入包含相同有效窗口/工具配对，无原始重复回放；原始文件前缀散列不变。
- `default-policy-result.json`：默认零保护尾部策略也完成同样的压缩、重启和坏引用负向验证。两种策略各 20 次模型 HTTP 调用全部保留，包含各 3 次独立 capability probe；失败恢复请求的业务模型调用为 0，同一 Worker 后续健康会话成功。
- 完整大工具结果由真实 `read_file` 产生，wire 中的引用已送达，原文起止标记及摘要/长度校验通过；单测额外覆盖分块 API、分叉后删除父会话、Unicode 字节边界和大结构化值。
- `publication-verified-result.json`：在快照文件已准备而原生 Session 未提交时注入暂停并终止 Worker；新文件作为孤儿保留，重启仍采用旧的已提交引用；未提交输入在后续实际 HTTP 中出现一次。该实验是可控故障注入，不是自然 OOM。
- 所有验收 Worker 均核对实际 PID/创建时间，结束后相关 PID 已消失。内存是采样峰值，采样间隙在验收报告披露，不等于操作系统生命周期峰值。

## 2026-11-09 收敛为 Swarm 单仓（替代配套交付要求）

用户明确要求只修改 Swarm，并关闭本次已创建的 PR。Core PR !3656 已于 2026-11-09 10:56:20+08:00 关闭并回读确认，单仓收敛阶段尚未创建 Swarm PR。原 Core 分支和配套工作树保留为历史对照，不能作为当前依赖。

首次单仓验证的目标 `develop` 基于 `7a3924ee13d73f91afdc8506dea7d9bd199b7a91`，保留 Core `f3f8f576164cc405c046e43079c8ad00ee17f0c4`；原 Protocol/Gateway、pyproject 和 uv.lock 均不修改。曾进行的配套 pin 调整与未完成锁定留在旧工作树，本单仓分支不包含它们。

实现通过 Swarm `runtime/context_checkpointer.py` 适配现有 Checkpointer 的普通/审批中断提交入口，沿用原生存储、恢复、工作流和 Team 后端。窗口文件准备、消费边界和冻结提交视图留在 Swarm；所有权弱绑定、串行提交和取消释放由 Swarm 管理。新输入消费按 ON_USER_MESSAGE 已从队列取出的事实记录，不再依赖新 Core metadata 字段。

原生 filesystem 卸载保持既有行为；内存卸载原文完整计入 16 MiB 快照预算，超限拒绝并保留旧状态，不新增 Core 惰性存储格式。原始审计和大负载工件仍完整保留，不把外置解释为全局磁盘用量归零。

单仓证据根：`D:/work_space/.codex-temp/issue6134-swarm-only-20261109/`。本分支 `507` 项回归通过，0 失败、0 跳过；新增原生 Checkpointer 集成验证普通/中断提交、并发冻结、取消、所有者代次、继承作用域和卸载预算。`acceptance-develop/log-scale-final2-result.json` 为真实 Worker 的 1,570,000,000 字节合成历史迁移、压缩、重启、坏引用及同进程健康恢复验证；原始历史前缀不变，最终 Worker 全部退出。本分支 publication 真实故障注入已通过：文件发布后、原生提交前终止 Worker，重启采用旧引用，未提交输入在后续实际 HTTP 中只出现一次。对应 `log-scale-publication-result.json`，没有复用旧配套版本的结论。

以上为提交 PR 前的隔离源码验证记录。未进行原历史/原 wheel、真模型 8 小时、Linux 自然 OOM 或远端 CI 验收；无产品 RSS 固定上限与无进展检测。前端等待器和此前集群 STALLED 仍为独立事项。

## 2026-11-09 develop PR 候选适配与重新验证

本轮将完整 Swarm 单仓修复适配到最新 `develop` 基线 `6bd9ed01d8ce3274a076cadffc85bc1c9dda89fe`。保持此基线的 `pyproject.toml` / `uv.lock` 不变，实际加载 Core `f3f8f576164cc405c046e43079c8ad00ee17f0c4`、Protocol `4c5f3e9eb41bd33d34d012b06a3e364e8497ac11`、Gateway `435d3323425e1051c6fe2d1feadcba68b4be020d` 的干净源码工作树。与初次单仓验证相比，沿用本轮上游的数据目录和消费者变更。

- 相关 pytest 文件规范化去重，并增加最新分支的 interrupt、debug trace、wire truncate 消费方回归：`609 passed`，0 失败、0 跳过。证据为 `develop-pr-tests.xml` 与 `develop-pr-tests-summary.json`。
- `acceptance-develop-pr/log-scale-pr-runtime-result.json`：1,570,000,000 字节合成历史的真实 Windows Worker 迁移、工具原文工件、压缩、重启恢复、坏引用拒绝和后续健康会话均通过；原始历史前缀未变，最终 Worker 身份全部退出。OS RSS 峰值 421.59 MiB，OS 私有内存峰值 903.39 MiB；1536 MiB 为测试 Job 私有内存限额，不是产品 RSS 限额。
- `acceptance-develop-pr/log-scale-pr-publication-result.json`：在快照文件发布后、原生提交前暂停并终止真实 Worker，重启仍采用旧的已提交引用，未提交输入在后续模型 HTTP 中出现一次，进程清理完成。
- 验收脚本适配当前 infra/domain 数据目录；修正路径初始化缓存导致的源码模板误写，最终三组验证均核对源码配置模板散列未变。旧路径假设的启动失败与配置隔离修订前记录保留，以修订后的最终记录为本轮证据。

本轮仍为隔离源码、本地确定性模型和合成历史验证；未将其描述为原现场、发布 wheel、真实模型长稳或远端 CI 验收。当前 PR 不含 Core 改动和依赖变更；PR 审阅、CI 与合入状态以远端对象为准。
