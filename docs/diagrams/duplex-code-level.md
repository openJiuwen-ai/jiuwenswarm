# 最简双工：文件 / 类级详细版

核心接入由三个文件负责；外部厂商接口由 `duplex_jev.py`、`duplex_mindshub.py`、`duplex_clef.py` 提供：`duplex_shadow.py` 接消息并调用快模型，`duplex_router.py` 校验一次判断，`duplex_native.py` 对接 SDK 内部暂停和恢复。

[打开完整详细图](duplex-code-flow.svg) · [PlantUML 源文件](duplex-code-flow.puml)

## 先区分两条并行流程

[流程概览](duplex-routing-flow.svg)和详细图均将正常执行与新请求处理放进 `par` 并行框，并分别使用 A / B 编号。

- **A：正常执行。** 任务执行期间，接纳原任务 / steer 后调用慢模型。完整模型回复、工具结果按原 SDK 写入上下文；任务还需继续就进入下一次循环。
- **B：新请求到达后处理。** 用户补充输入或 A2A 普通消息到达才触发。满足路由条件时，读取 A 的现有任务信息并调用一次快模型；判断期间 A 的模型 / 工具继续运行。
- **两者的连接。** APPEND 把消息交给原 steer，A 后续模型调用前接纳；INTERRUPT 暂停 A，生成中保留当前 ctx、跳过回滚，工具执行中等待工具完成，再作废旧计划、结合新消息重新规划。

内部打断保留已接纳的需求和完整工具结果。给快模型看的 `ControlSnapshot` 是当前任务和执行位置，用于判断新消息；状态标识只作诊断记录。

## 改动颜色

相对原版 Jiuwen：**蓝色 = 新增逻辑；橙色 = 原入口 / 回调的包装或覆盖；灰色 = 原 SDK 直接复用。**

图中不仅给类着色，每个调用步骤也有同色箭头与【新增 / 包装 / 原有】标签。新增子类调用原 `super()` 方法时仍是灰色；SDK 文件本身没有被改写。

## 详细图 B12—B20 在做什么

这几步合起来只做一件事：**拿当前任务和新消息问快模型“继续还是打断”，检查返回格式并按运行状态执行。**

| 步骤 / 函数 | 做什么、为什么做 |
| --- | --- |
| B12—B13 `snapshot_from_native()` | 读取已有任务目标、当前计划项、执行阶段，以及适配器提供的实际操作和最近已完成操作，让快模型知道新消息是否推翻当前工作。没有操作信息就留空，不调用慢模型生成摘要。 |
| `ControlSnapshot` | 装这些信息的 Python 数据对象。它不是用于回滚上下文的安全点；`context_version` 用于诊断记录，`round_id` / `checkpoint_id` 是轮次和安全点编号，都不发给快模型。 |
| B14 `observe()` | 给一次判断加上等待上限，处理错误和超时。普通函数，不是另一个 Agent。 |
| B15 `classify()` | 调用传入的快模型请求函数，把当前任务信息和新消息交给它。 |
| B16 `create_tiny_agent()` | SDK 后端准备独立快模型对象；Jev、MindsHub、Clef 则使用各自 HTTP 接口。 |
| B17 `agent.run(prompt_for(...))` | 按所选后端的协议组装任务信息和新消息，提交判断请求。比如“当前计划使用 Kafka”加“禁止 Kafka”，让它判断是否 INTERRUPT。 |
| B18—B19 返回并检查 | 各后端统一返回 action。代码检查格式；等待期间状态哈希变化不会使判断失效。 |
| B20 `Observation` | 返回建议、是否有效和本地记录字段。它是结果对象，不是新的处理机制。 |

图中每条箭头现在先写中文用途，再附函数名；新增 / 包装 / 原有的颜色不变。

## 文件和类具体负责什么

| 文件 / 类（点击打开源码） | 具体职责和调用 |
| --- | --- |
| [bootstrap.py](../../jiuwenswarm/agents/harness/team/bootstrap.py) | `configure_agent_teams_home()` 调用 `install_shadow_observer()`，安装入口包装。 |
| [duplex_shadow.py](../../jiuwenswarm/agents/harness/team/duplex_shadow.py) | `RoutedInput` 保留原投递文本并附消息 ID；`deliver_routed()` 选择原路径或快模型判断；`snapshot_from_native()` 读取已有任务和执行位置。`classify_input()` 直接发起一次独立快模型请求，没有客户端外壳类。 |
| [duplex_router.py](../../jiuwenswarm/common/duplex_router.py) | `observe()` 只判断一次；`prompt_for()` 组装 `{snapshot, messages}`；`decision_schema()` 约束输出；`validate_decision()` 仅校验 action；不再比较状态哈希。失败、超时返回相应状态。 |
| [duplex_native.py · DuplexNativeHarness](../../jiuwenswarm/agents/harness/team/duplex_native.py) | 继承原 `NativeHarness`。`interrupt()` 只接受 INTERRUPT；`_dispatch() → _route()` 在原 supervisor 中校验并暂停；`_finish_transaction()` 作废旧计划、替换接续指令，再调用原 `_on_send()` 重新规划。没有另起执行器。 |
| SDK `openjiuwen/agent_teams/agent/coordination/handlers/message.py` · `MessageHandler` | A2A 原入口：`_process_unread_messages()` 读取、展开、投递；投递返回后调用原消息管理器确认已读。`_format_message()` 被包装以携带消息 ID。 |
| SDK `openjiuwen/agent_teams/agent/coordination/handlers/agent_lifecycle.py` · `AgentLifecycleHandler` | 用户原入口：`on_user_input()` 把 content 交给 `TeamAgent.deliver_input()`，与 A2A 汇合。 |
| SDK `openjiuwen/agent_teams/harness/team_harness.py` · `TeamHarness` | 原 `TeamAgent.deliver_input()` 调用其 `send()`，再转交 `inner_agent.send()`。active 工厂创建的 inner_agent 是 `DuplexNativeHarness`。 |
| SDK `openjiuwen/agent_teams/harness/native_harness.py` · `NativeHarness` | `_on_send()` 负责原 steer 和 PAUSED 后的 continuation；`_on_pause()` 选择取消模型或等待工具。子类仅在内部打断时跳过 `_rollback_to_snapshot()`，显式生命周期控制仍使用 SDK 回滚。 |
| SDK `openjiuwen/core/single_agent/agents/react_agent.py` · `ReActAgent` | 下一次模型调用前 `_drain_steering_batch() → _admit_user_message(source="steering")`，真正把新消息放入模型上下文。 |

SDK 路径以 `agent-core` 仓库为根目录。当前整合在最新 develop 锁定的 SDK 提交 `9e3390195a9ea15235b2b5f7412cb2aa440622cc` 上验证。本图描述调用关系，没有修改 SDK 源文件。

## 两条路径落实到方法

**APPEND / 快模型失败或超时**

`deliver_routed.steer()` → 原 `TeamAgent.deliver_input()` → `TeamHarness.send(immediate=True)` → 原 `NativeHarness.send()` → `_CmdSend` → 原 `_on_send()` → `_push_steer()`。

投递成功即返回原入口确认；慢模型继续当前计算。下一次原有模型调用才由 ReActAgent 接纳新消息。这条路径不进入 `interrupt()`。

**INTERRUPT**

`deliver_routed()` → `DuplexNativeHarness.interrupt()` → `_RouteInput` 放入原 `_control` → `_dispatch()` → `_route()` → 原 `_on_pause()`。

- 生成中：`_hard_cancel_round()` → 子类 `_rollback_to_snapshot()` 跳过内部回滚、保留当前 ctx → PAUSED。
- 工具执行中：PAUSING，等待工具完成；原 `PhaseSnapshotRail` 停在模型边界，`_on_round_done()` 收尾为 PAUSED。

随后 `_finish_transaction()` 清空并保存 `task_plan`，将 `paused_query` 替换为重新规划指令和新消息，再调用原 `_on_send()` 的 PAUSED 分支。历史中的有效需求、工具结果保留；旧计划在新轮安全点保存前就已失效。重新规划指令明确：队友发现是证据，不能覆盖用户要求；已完成成果需核对后再复用。

例如用户要 Redis、Agent 计划用 Kafka：监控消息触发暂停后，Kafka 的待执行计划被清除，再依据 Redis 要求重新规划。代码保证旧结构化计划不再自动执行；新方案是否正确仍由模型与测试验证，并不声称取消了已发生的外部工具效果。

## 图里特别标出的边界

1. 快模型输入是已有任务 / 计划、新消息和执行位置；`last_action` 为适配器提供的最近已完成操作，未提供时为空。快模型只输出 action，不回传状态标识。
2. 快模型判断期间慢模型继续，但这条消息的投递调用会等待判断结果。没有独立后台投递承诺。
3. 版本复用 SDK 原有 `seq_counter`，没有额外计数器。版本只作诊断记录，不作为打断条件；同实例已接收的消息 ID 不重复投递。
4. 内部暂停保留原任务、已接纳补充消息和完整工具结果，并作废旧计划，要求模型结合新消息重新规划。
5. 只有原 A2A 投递成功才确认已读；没有新增持久收件箱、工具账本或跨进程恢复。

## 保留适配的原因

SDK 有暂停和发送接口，但没有“内部打断并携新消息继续”的组合接口。`DuplexNativeHarness` 只补这一段：保证首次打断不丢原任务、内部暂停不写入用户取消提示，以及用户停止后不被内部恢复覆盖。普通发送不再单独拦截或维护版本。

## 完整调用时序

![文件与类级调用时序](duplex-code-flow.svg)
