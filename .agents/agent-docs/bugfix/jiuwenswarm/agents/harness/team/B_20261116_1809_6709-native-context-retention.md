---
id: B_20261116_1809_6709-native-context-retention
title: 集群旧代 Native 上下文驻留与并发 checkpoint 峰值
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/agents/harness/team
  - jiuwenswarm/agents/harness/common/rails
  - jiuwenswarm/runtime
  - jiuwenswarm/server/runtime/session
files:
  - jiuwenswarm/agents/harness/team/native_lifecycle.py
  - jiuwenswarm/agents/harness/team/rail_registry.py
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/agents/harness/common/rails/stream_event_rail.py
  - jiuwenswarm/runtime/checkpoint_budget.py
  - jiuwenswarm/runtime/context_checkpointer.py
  - jiuwenswarm/server/runtime/session/context_snapshot.py
  - tests/unit_tests/agentserver/test_native_context_lifecycle.py
  - tests/unit_tests/agentserver/test_team_memory_ownership.py
  - tests/unit_tests/agentserver/test_team_manager_registry.py
  - tests/unit_tests/test_checkpoint_memory_budget.py
designs: []
design_note: 修复违反现有 harness 所有权和终止清理约束的保留链，并在现有 Checkpointer 委托边界约束临时工作并发，无独立设计。
---

# 集群旧代 Native 上下文驻留与并发 checkpoint 峰值

关联 [Issue #6709](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6709)。同一组 PR 的退休自动恢复见
[B_20261116_1726_6709-retirement-retry](../../../server/supervisor/B_20261116_1726_6709-retirement-retry.md)。

## 归档与事故归因

`topic_history.load_all_session_records()` 的会话来源是 `get_all_sessions_metadata()`；后者只遍历
`get_agent_sessions_dir()` 顶层，并排除生命周期已归档、已删除及产品归档目录已存在的会话。
用户截图中的外部 `/opt/huawei/session-archive/sessions-big-116/` 不在这条扫描路径内。
459 个活跃会话、24 个外部归档及相应字节量来自用户截图，本地附件不包含这两个目录的完整现场副本，未重测这些数量。

但 ZIP 的 `agent_logs/agent_server.log.gz` 在日志记录时间 15:17 和 15:29 仍有多个线程的调用栈位于
`_default_context_snapshot → _load_completed_records → permitted_records → load_all_session_records`。
外层日志时间可能受 stderr 排队影响，不等于扫描启动时刻。因此不能仅凭非整点或外部归档，
认定推荐后台读取与当时内存压力完全无关。当前两个分支的主动 tick
请求入口已返回 `skipped/proactive_removed`；本次不恢复该旧功能，也不把以下可复现缺陷直接等同于现场唯一 OOM 原因。

## 根因与实际修复

1. `TeamHarness.start()` 在旧 native 终止后重建 NativeHarness。TeamManager 的热更新 agent/rail/mount-context
   注册表原先保持强引用，并按对象身份追加成员，导致旧代对象及其 ContextEngine 可被长期保留。
   注册表现在观察弱引用，mount-context 保持原对象读取接口；只有实际 harness/runtime 才拥有 agent 与 rail。
   已 TERMINATED 的挂载上下文不再用于热重建，存活及 PAUSED owner 的热更新继续工作。
2. 仅修改上述注册表还不足以释放真实 native。Core 的实例级 AgentCallbackManager 把 rail 回调注册到
   进程级 Runner callback framework，PhaseSnapshotRail 等回调又反向持有 native。已触发回调的签名元数据
   缓存也可能以 bound method 为键保留 owner。
   Swarm stream rail 在 `before_invoke` 注册公开的 `NativeHarness.subscribe(on_state=...)` 观察者。
   只有 TERMINATED 且 active round 已清空后，才通过公开 `agent_callback_manager.clear()` 清理该 native
   及其内层 ReActAgent 的实例命名空间；随后失效当前锁定 Core 的派生签名缓存。
   缓存失效只会重算检查元数据，不删除其他存活 native 的回调。对相关缓存使用能力检查，未 monkey-patch Core。
3. 原先每个 `(session_id, agent_id)` 只有自己的锁，不同成员可同时执行 save_contexts、JSON 快照准备、
   frozen-state 复制、native pickle/base64 及存储 await，产生叠加的临时内存。
   新的进程级单槽配额在这些阶段之前取得，并持有到委托持久化完成；显式保存、普通提交与中断提交共用配额。
   同一 task 重入不重复取得配额，子 task 不继承准入；等待取消不会遗留后台获取线程，也不增加新的写入超时。
   锁顺序统一为配额、binding lock、context snapshot lock。

没有通过删除活跃 context、缩短模型窗口、丢弃工具结果、改写 checkpoint 格式或升级 Core 依赖降低内存。
正常运行和可恢复暂停的上下文仍由各自 owner 持有。

## 验证证据

比较基线为原 PR 头：develop `79a45823aaf9b30a8e24bed1218232794a9bef98`，
releases/0.2.9 `2a02bbceb8ff43a6c9e6aa459d48f272d87a1581`。
Windows / Python 3.12.10 / 独立 uv frozen 环境；Core 分别为 `2f4bedb9` 与 `45d53dd1`，没有本地 Core 替身。

- 每条分支均有 7 个原代码对照用例按预期失败：旧代所有权、跨成员普通/显式/中断提交准入，及实际 stream-rail
  安装的 native 终止清理。新增 10 个用例全部通过，包括存活 owner、PAUSED、同卡号其他实例、持久化失败和取消重试的控制。
- 无模型的真实 TeamHarness/NativeHarness 五轮启停：原代码 live context 为 `1,2,3,4,5`，销毁 harness 后仍为 5；
  修复后各轮为 `1,1,1,1,1`，销毁 harness 后 context 与 native 弱引用均为 0。
  当前代在 stop 后仍由 TeamHarness 持有以支持配置读取，未提前销毁它的 context。
  持久化失败及后续重试也保留该窗口；底层提交没有明确的成功回执，本次不额外引入主动卸载。
- 真实 Core ContextEngine → Session.commit → Swarm ContextCheckpointer → SQLite，四成员各 8 MiB：

| 分支 | 原代码进程 RSS 峰值 | 候选 RSS 峰值 | 原代码新增 Python 分配峰值 | 候选新增 Python 分配峰值 |
|---|---:|---:|---:|---:|
| develop | 382.9 MiB | 312.0 MiB | 65.5 MiB | 30.2 MiB |
| releases/0.2.9 | 390.3 MiB | 311.4 MiB | 65.5 MiB | 30.2 MiB |

  四份有效快照均为 8,400,034 字节；额外执行 native SQLite 恢复，四成员各 128 条消息的内容均逐项一致。
  内存数字来自受控上下文，不能外推为生产内存上限或固定缩放系数。
- 完整相关回归：develop 455 passed / 2 skipped；0.2.9 460 passed / 2 skipped。
  已包括 Swarm provider 装配和持久化失败保留窗口验证。两项跳过要求 POSIX Unix socket 或 SIGTERM/SIGKILL，Windows 未执行。
- 16 文件回归包括新增三文件、TeamManager 注册表、context checkpointer/snapshots、handler dispatch、既有八个
  Supervisor/IPC/退出测试及 Swarm provider 装配。命令前缀为
  `uv run --frozen --no-sync pytest -o addopts= --asyncio-mode=auto -o log_cli=false -q -rs`。
  原始日志、源码加载路径、Core direct_url 与 JSON 结果保留在本地 `issue6709-retirement-retry-20261116/`。

## 当前约束与后续验收

本修复消除已退休代次的上述保留链，并控制正常 checkpoint 的并发放大；不声明活跃会话/成员的总字节配额，
也未改变仍在使用的上下文驻留、压缩策略或所有其他历史消费者。原始 Linux/真实模型长稳仍需在新构建部署后复验。
判定进程是否被系统 OOM kill 还需要相应内核/cgroup 证据，不能以 Supervisor 退出诊断替代。

English summary: release retired native callback owners and observe hot-reload registrations weakly.
Serialize complete checkpoint preparation and persistence across members while retaining the existing checkpoint/recovery format.
