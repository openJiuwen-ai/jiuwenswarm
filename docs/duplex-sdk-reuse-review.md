# SDK 复用与减法评审

2026-09-08，观察至约 17:40（UTC+8）。稳定 HEAD `799d963c`，另有正在修改的工作区；不是对最终未提交版本的整体验收。

## 为什么前两次漏掉

前两次主要审查“已补功能是否正确”，没有先建立 SDK 已有职责和新增职责的对应关系。随后把实现中的 32 条、2 秒抄进验收规范，形成了“代码支持规范、规范再证明代码合理”的循环。这是评审方法错误。规范 1.1 已撤回这两个默认值，增加 R0：任何新增机制必须先证明现有能力的缺口。

## 对照 SDK 的结论

SDK 路径均位于本机 `D:/jiusi_agent/.venv/Lib/site-packages/openjiuwen/`。锁文件记录 SDK `94e10cb`；以下直接读取了实际安装源码，不声称已校验整棵安装树。

| 职责 | 现有实现 | 新增部分该如何处理 |
| --- | --- | --- |
| 事件接收与未读补偿 | `agent_teams/agent/coordination/event_bus.py:_start_poll_tasks/_poll_loop`，`handlers/message.py:_process_unread_messages` | 复用原唤醒/轮询，不另加 50ms 或 250ms 定时扫库；测试通过原入口入消息 |
| 顺序、ACK、广播水位 | `MessageHandler` 批量确认，`TeamMessageManager.mark_messages_read` | 不另造第二套已读规则；扩展可恢复接受时说明两种状态各自含义 |
| 执行排队 | `NativeHarness` 控制命令；`harness/task_loop/loop_queues.py` 的 steering/follow_up | 复用执行队列；分类只需要当前判断、待纳入消息及版本，不应复制整套执行调度 |
| 安全暂停与上下文恢复 | `NativeHarness` 与 snapshot rail 已有模型/工具阶段处理、会话恢复 | 优先扩展检查点提交边界；独立 DurableInbox 保存完整历史不是自动合理，需解释现有存储为何不足及两套状态如何避免分叉 |
| 工具调用 | `core/single_agent/ability_manager.py` 已负责解析、调用、超时 | 只补已证明缺失的持久回执和未知结果核对；无需重新实现完整调用层 |
| 模型预算与超时 | SDK 模型客户端与任务本身已有配置边界 | 先复用并核对实际生效范围；独立路由时限、截断规则必须有依据，不能从“2 秒”走到“默认不设期限” |

不能把“SDK 有队列”推导成“已经具备快模型分类并发”。现有 `EventBus._run_loop` 会 await 每个 callback；`MessageHandler` 又逐条 await `deliver_input`。原来投递快，如今接入点等待整次模型判断，便把原串行入口变成长时间阻塞点。

在所查类中没有发现可直接解决此问题的现成 pre-delivery 异步分类契约。可行的改造方向是明确的接收/分类/确认分离接口：收件只完成可靠登记，分类独立进行，执行仍交单一 supervisor，ACK 依赖已证明可恢复的接受状态。具体实现需覆盖原 DB 和生命周期入口；不能重新用全局事件总线包装绕过去。

## 当前删减的实质风险

### 1. 删重复层时，正在删目标断言

工作区差异显示：

- `test_database_peer_merges_updates_and_acks_original_rows` 改名为只验证 drain 和 ACK，删除“旧判断未返回时已触发第二次判断”和“新判断同时包含两条消息”的断言。
- `test_input_dispatch_does_not_block_lifecycle_event` 改为验证 SDK 顺序，主动释放旧输入之后才等后续控制事件。

这两个改动使回归不再守住原目标。不能以新测试通过宣称并发接收能力仍在。

本轮复现：真实 SDK EventBus + 当前 InputController（回调模拟实际 await controller.submit 的接法），先阻塞 m1 分类，再入 m2。结果为 m2 必须等 m1 分类结束，分类批次为 `[[m1], [m2]]`，没有合并重判。控制器当前缺省 timeout 为 None；即使单次模型客户端最终超时，也还需证明跨重判的总等待有界。

该复现不包含完整 DB/模型链，结论限定为所接回调的串行阻塞；生产链的相同 await 关系由源码确认。

### 2. 超时降级不是全部“拍脑袋”

原 HTML 第 740 行明确写了“快模型失败或超时：默认退化为现有 steer，原 A2A 继续工作”。因此应分开评价：

- 2 秒、30 秒缺少参数依据，应撤回或论证。
- 原 steer 降级有需求来源；它不代表语义安全，但不能悄悄改成“失败留未读、持续重试”。后者会影响队头阻塞与原 A2A 可用性。
- “分类失败”和“成功判断 APPEND”必须在观测与统计中分开。

### 3. 主链删了，评测层还留着相同机制

观察时仍存在：

- `benchmarks/duplex_database_peer.py` 自建 `_poll` 每 0.25 秒扫库，同时启动了原 EventBus；`receive` 还主动调用 drain。它不等同纯粹生产入口，可能掩盖串行入口缺陷。
- 同文件 `duplex_settings` 仍写死 2 秒；`benchmarks/duplex_runtime.py` 仍有 4000 字符截断和 2 秒路由预算。

这些必须与主链统一，不能只改图上的 32。不是所有数字都是魔数：协议轮次、单次无状态分类的 `max_iterations=1`、故障测试的短等待都有可解释用途；要审的是来源与语义，而不是机械删除所有数字。

### 4. “修崩溃”也不能无限复制状态

ACK 后崩溃和工具结果不明的缺口需要修，但不能因此直接认定独立 SQLite、另存完整上下文、全部工具包装都必须保留。应先与 SDK 原会话存储、恢复入口对齐，明确唯一权威状态、提交事务范围和稳定恢复身份。收据可与上下文分开存储，但不能互相声称是同一事实的权威副本。

## 规范和下一步

规范 1.1 已取消无依据默认值，加入 SDK 复用前置审查，并禁止通过削弱测试改变目标。本轮没有修改其他任务正在调整的运行代码和图稿。

先恢复功能断言，选定最小 SDK 接入改造，再删除重复实现；背压、上下文预算和总期限仍须有可验证的边界。不要把“多一层补丁”换成“少一项能力”。

证据：`D:/jiusi_agent/duplex-sdk-reuse-probe.py`、`duplex-sdk-reuse-probe.json`、`duplex-sdk-reuse-probe.log`。JSON 记录此次控制器/适配器/路由源码 SHA256。脚本退出码 0 表示阻塞现象按预期复现，不表示功能验收通过。
