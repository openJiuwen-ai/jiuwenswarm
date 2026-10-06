# 全双工第二次评审

评审日期：2026-09-08。代码：`ab595cc9f5ee3d2da6c55415855eeaab9a38869b`。对照：`duplex-acceptance-spec.md` R1–R7 / T01–T13。

**结论：架构方向已经纠正，可以在现分支继续补齐，不建议重新开炉重写；当前仍不通过整体验收。**

## 已确认的进步

- A2A 增加 `DatabasePeer`：外部消息入 Jiuwen DB，经原 MessageHandler / EventBus / Poll 投递和 DAO ACK；注册成员、绑定会话的遗漏已修。
- 普通输入不再持有 NativePeer 的全程路由锁；单控制器可取消旧分类并合并新消息，预算从最早消息计时。
- InterruptBench 同题复用同一个 NativePeer；正常动作间不再重复创建、关闭 Native。
- 增加边界状态 rail 和 SQLite 工具收据。对同一 scope / call_id 的已提交结果能复用，对未知非幂等结果能拒绝重试。

这些是代码和测试可见的修正，不代表真实任务效果已经证明。

## 仍需修复的发现

### F1 / P1：旧摘要覆盖新计划，版本更新仍可能携带旧方向

位置：`jiuwenswarm/agents/harness/team/duplex_state.py:51`、`:70`；`duplex_shadow.py:snapshot_from_native`。

`publish_state` 始终优先取 `_duplex_intent` 中非空的 goal / next_action，且快照组装时显式 intent 再次覆盖控制状态。若模型先发布 Kafka 意图，随后更新权威计划为 PostgreSQL，但未再次调用摘要工具，则后续边界仍发布 Kafka 目标和下一动作。当前没有字段级来源版本、过期标识或更新时间，快模型无法辨别它已经落后。

验证：直接回放仓库中 `publish_state` 的函数体，传入更新后的 PostgreSQL 计划和上次 Kafka intent，实际输出仍为 `Use Kafka / Implement Kafka`。这是函数级复现，未冒充完整模型场景。

修复要求：给显式意图关联计划/任务版本；新目标采纳或计划变化后，旧目标/下一动作失效或明确标记过期，不能覆盖新权威状态。快照组装遵循同一优先级。补“显式摘要发布后，计划改变但模型不再调用摘要工具”的 T06 回归。

### F2 / P1：消息已读与可恢复接受之间仍有丢失窗口

位置：`jiuwenswarm/agents/harness/team/duplex_native.py:231`。

APPEND 目前只调用 `_push_steer` 写入内存队列，更新内存 `_duplex_received`，然后返回 ACK，允许上游 DB 标记已读。锁定 SDK 的 `LoopQueues.push_steer` 只是 `asyncio.Queue.put_nowait`，这段路径没有先持久化消息接受记录。

因此，在 DB ACK 成功、下一调用保存消息之前进程退出，会出现 DB 已读而新消息只在旧进程内存中的窗口。新增工具 SQLite 账本没有关闭这个窗口。此发现基于调用链审查，本轮未做杀进程故障实验。

修复要求：ACK 前持久化可恢复的消息接受状态，重启后根据记录重放并去重；验证 ACK 前后、上下文提交前后的故障点。稳定任务/会话/逻辑工具调用标识也必须随恢复保留。T08、R5 未通过。

### F3 / P1：U2A 仍未统一网页动作、输入入口与恢复状态

位置：`jiuwenswarm/benchmarks/interruptbench_runner.py:242`、`:254`；`duplex_runtime.py:MeasuredModel._arguments`。

复用 Native 修好了生命周期，但仍然 `tools=[]`；网页 `env.step` 和官方轨迹由外部 runner 管理，模型 messages 又由外部 prompt 覆盖。官方更新直接调用 `peer.receive`，没有经过生产 `AgentLifecycleHandler.on_user_input`。故当前只能证明持久 Native 的模型调用适配，不能证明生产 U2A 入口、网页副作用与 Native checkpoint 的统一恢复。

修复要求：官方更新进入生产 U2A 入口；网页动作及结果通过统一工具提交/恢复契约接回 Native。保留上游解析器和评分器。补真实多动作轨迹及副作用边界验证；不能只断言 peer 对象未重建。

### F4 / P2：安全全量打断基线在 U2A 入口不可用

位置：`jiuwenswarm/benchmarks/interruptbench_runner.py:274`；`duplex_experiment.py:77`。

底层支持 `always_interrupt`，但 U2A CLI 的 choices 缺失该值，默认实验列表也未包含它。配置该策略实际退出码为 2，报 `invalid choice: 'always_interrupt'`。文档里区分两种 abort，还没有完成执行入口的对应接线。

修复要求：让该策略贯通 CLI、实验配置、逐题输出和配对报告；补一次真实入口解析/执行测试。用相同恢复实现对比 model 与 always_interrupt，再另报 abort_restart，避免混淆路由价值和恢复实现差异。

## 本轮验证证据

代码工作区没有新的运行代码改动，本次只增加复审文档并更新旧规范的阅读提示。

执行命令（工作目录 `D:/jiusi_agent/wt-jiuwen-duplex-a2a`）：

```powershell
D:/jiusi_agent/.venv/Scripts/python.exe -m pytest tests/unit_tests/agentserver/test_duplex_controller.py tests/integration_tests/test_duplex_e2e.py -q -o addopts='' -o log_cli=false --tb=short
D:/jiusi_agent/.venv/Scripts/python.exe D:/jiusi_agent/duplex-reassessment-probes.py
```

- 回归实际结果：**29 passed, 2 skipped，39.54 秒**。模型响应为受控端点；跳过项未计通过。
- 回归日志：`D:/jiusi_agent/duplex-reassessment-tests.txt`。
- F1/F4 复现脚本与结果：`D:/jiusi_agent/duplex-reassessment-probes.py`、`D:/jiusi_agent/duplex-reassessment-probes.json`。脚本退出码 0 表示两个缺陷均按预期复现；不是功能验收通过。
- 代码锁文件为 agent-core `94e10cb6102c36fe78a64547957c0def97299273`；运行检查了本地实际 SDK 的相关路径。本次未另行验证已安装包完整树与该提交逐文件一致。
- 原任务报告本机 140 项、服务器 68 项通过；本轮没有重复全套或远端验证，因此不把这些计为本轮独立结果。
- 原任务公开记录的真实快模型调用约 2010 ms 超时后降级 APPEND；这证明降级接线，尚不能证明快模型成功及时作出 INTERRUPT，也没有正式任务收益结果。

## 验收状态与下一步

| 规范项 | 本轮状态 |
| --- | --- |
| R1 输入主链 | A2A 有实质修正；U2A 评测生产入口仍有缺口 |
| R2 持续接收 | 合并重判、期限与生命周期不被阻塞的现有回归通过；未声称完成所有满载故障组合 |
| R3 工作状态 | 部分实现；F1 复现，未通过 |
| R4 同实例恢复 | 现有模型/工具边界回归通过；网页恢复未覆盖 |
| R5 持久恢复 | 工具收据已有基础；消息接受原子性、完整崩溃恢复未通过 |
| R6 正式主链评测 | 持久 Native 已改；真实网页动作与生产 U2A 入口未统一，正式环境未跑完 |
| R7 对照与收益 | F4 复现；无完整公开任务成绩，净收益未证明 |

先修 F1、F2、F3，F4 可作为小改动一并补齐；再做一个真实 A2A 和一个多动作 U2A 的完整验收。保留当前分支和已验证机制，不用重写仓库，也不能以已有测试数量宣布结束。
