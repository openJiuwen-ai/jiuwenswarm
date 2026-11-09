---
id: S_02_durable-context-recovery
title: 有效上下文持久化、增量恢复与旧历史迁移
type: S
author: 刘瑜
author_email: liuyu524@huawei.com
updated: "2026-11-09"
modules:
  - jiuwenswarm/runtime
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/agents/harness/common
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/runtime/context_checkpointer.py
  - jiuwenswarm/runtime/checkpointer.py
  - jiuwenswarm/server/runtime/session/context_snapshot.py
  - jiuwenswarm/server/runtime/session/context_migration.py
  - jiuwenswarm/server/runtime/session/history_stream.py
  - jiuwenswarm/server/runtime/session/history_artifacts.py
  - jiuwenswarm/server/runtime/session/recovery_budget.py
  - jiuwenswarm/agents/harness/common/rails/stream_event_rail.py
  - jiuwenswarm/agents/harness/common/session_ops_service.py
  - jiuwenswarm/server/handlers/session.py
---

# 有效上下文持久化、增量恢复与旧历史迁移

## 1. 范围与现有依赖

修复仅位于 Swarm。目标 `develop` 保留原 Core `f3f8f576164cc405c046e43079c8ad00ee17f0c4`，以及本分支原 Protocol/Gateway、`pyproject.toml` 和 `uv.lock`；不使用原型中新加的 Core persistence hook、UserMessageInputs.metadata 或惰性卸载格式。

Swarm 通过既有 `Checkpointer` 接口装配 `ContextCheckpointer`，底层仍为原生 PersistenceCheckpointer（或测试中的原生 InMemoryCheckpointer）。普通提交和审批中断提交均在委托原生存储前准备窗口引用；恢复、工作流、Team 存储、清理和 graph_store 继续委托原对象，原生检查点保持唯一提交权威。

## 2. 保存、所有权与提交一致性

Swarm 在冷恢复/主请求开始时，将实际原生 Session 与 ContextEngine 绑定。绑定按 `(session_id, agent_id)` 和每个活实例 nonce 隔离，使用弱引用，不能把另一个实例或子代理继承的状态当成本实例所有权。相同会话的提交串行；不同事件循环不复用旧循环的锁。

`post_agent_execute()` / `interrupt_agent_execute()` 收集现有 ContextEngine 的真实 state，先流式写不可变快照，再将引用和 context 放入同一 Session；冻结本次提交视图后交给原生存储，避免等待 I/O 时后续 state 改动污染该次提交。显式压缩/迁移保存遵守同一锁顺序和冻结视图，不递归准备。

文件发布成功而原生提交失败时，旧的已提交引用仍是恢复依据；新文件可以作为孤儿保留。hint 仅用于抑制全历史预读，不是恢复指针。空 context pool 不得覆盖已经提交的有效窗口；受管 rewind 清除引用，不能借旧引用复活回退后的上下文。

## 3. 消费边界与恢复

`ON_USER_MESSAGE` 表示 Core 已从输入队列取出该批输入交给处理链。Swarm 记录请求身份和独立语音消费边界；即使后续 rail 有意丢弃输入或失败保存只保留清理后的前缀，该输入也不能作为未处理请求自动重放。排队且未取出的输入不计入该集合。

```text
原生 Session.pre_run 恢复检查点
  → 校验已提交快照引用、SHA-256、会话与历史文件代次/边界
  → 安装真实有效窗口
  → 从已消费位置仅回放未消费增量，排除已消费请求迟到输出
  → 在当前请求的 user 行之前停止
  → 正常接纳新输入、执行模型/工具并由原生 Session 提交
```

实际入窗的历史来源 metadata 仍可保留用于去重，但新输入消费不依赖新增 Core 消息字段。语音、steer 和排队输入分别核对；历史原子重写、身份/边界锚或生命周期变化使旧游标失效。

## 4. 预算与旧历史迁移

| 项目 | 当前约束 |
| --- | --- |
| 单份有效快照 | 16 MiB，流式编码；包含原生内存卸载数据 |
| context / 窗口消息 | 最多 16 个 context；每窗口最多 8192 条 |
| 消费请求 / 语音 / 原生内存卸载 | 各有 8192 项数量预算；卸载消息合计最多 8192 条 |
| 正常成功保存的快照保留 | 最近 4 份及保护引用；不删除审计原文 |
| 旧历史迁移扫描 | 8 GiB / 120 秒扫描解析预算 |
| 分块迁移 | 约 512 KiB 触发压缩，工具组/上下文受 4 MiB 预算保护 |
| 同一 Worker 的异步恢复 | 最多 2 个，等待最多 5 秒，取消释放名额 |

已有 Core filesystem 卸载行为保留；in_memory 回退数据完整计入快照，不能无预算累积。超限明确返回 HISTORY_RESTORE_LIMIT 并保留原数据/旧检查点；不宣称对任何大小的原生卸载内容都提供新 Core 的惰性恢复能力。原型中的 offload_directory/offload_versions 格式不能被旧 Core 静默误读。

没有有效新快照时先走 S_01 有界恢复；超过快速路径预算或遇到旧自动压缩审计摘要时，使用流式 JSON/JSONL 解析。巨型诊断字符串不整体解码，语义页调用实际配置的原生压缩器；迁移临时采用零保护尾部，完成后恢复原策略。工具配对不能拆分；成功后才安装和提交。审计摘要不替代真实有效窗口；仅剩摘要且无原生有效状态时明确失败。

## 5. 大负载、分叉与原文读取

完整 prompt 的 messages/tools 和大工具字段流式写入 `history_payloads/<sha256>`。事件保留短预览及 `payload_refs`；小值保持原有字符串/对象语义，MCP App 内容保留。引用含 sha256、bytes、media_type 和宿主路径；application/json 与 text/plain 的读取语义不同。

复用普通、非流式 history.get：

```json
{"session_id":"web_example","artifact_sha256":"<64 位小写 SHA-256>","offset":0,"max_bytes":65536}
```

返回 content/offset/next_offset/eof/bytes/sha256；UTF-8 字节位置对齐，每次最多 64 KiB，只接受会话内摘要。适配器、usage、history collapse 保留引用；fork 复制工件并改写预览路径，删除父会话后子会话仍可读。

## 6. 失败、取消与生命周期

MemoryError、损坏快照、校验失败、资源超限或无效游标明确失败；不能返回空/部分历史后继续调用业务模型。未恢复的冷适配器从缓存清理；取消不能留下半恢复实例。原生普通/中断提交失败恢复内存引用，原生检查点仍是重启权威；快照锁和提交锁在异常、取消时释放。

历史写入、工件发布、游标读取遵守 lifecycle/write_guard。错误去重、旧数组转换、推荐和语音同步避免全量回读。退出窗口与兼容错误回包见 S_01：600 秒内 3 次就绪后确认退出触发失败与冷却；不是进程 RSS 上限。

## 7. 验证与边界

针对两条目标分支分别加载原固定 Core，并检查源码路径和 Core 工作树未修改。回归覆盖原生普通提交/审批中断、旧所有者拒绝、子会话继承、并发冻结提交、取消、原生内存卸载预算、迁移、分叉、语音、权限和分块读取。真实 Worker 场景核对 SQLite 检查点、快照、实际 HTTP 输入及 PID/创建时间清理。

本地确定性模型和合成历史不等于原现场、发布制品或真模型长稳验收。没有产品 RSS 硬上限或无进展循环检测；不把 940 MiB 当作收敛承诺。原始证据与当前结果见同模块 B 文档。
