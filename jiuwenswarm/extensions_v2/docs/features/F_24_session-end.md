# session.end 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_24 / session-end |
| 建档日期 | 2026-10-29 |
| 归档日期 | 2026-10-29 |
| 后续修订 | 2026-10-31，以 session.append 为标准补齐逐字段说明、业务案例和源码调用链；运行契约不变；2026-11-02，按原 session.append 的对象示例、输入／输出字段表及具体 Handler 案例统一说明，契约不变 |
| 计划与进度 | [EXTV2-19 / Server PR10](../designs/extensions-v2-development-plan.md#extv2-19-主会话结束通知与用户资源清理) |
| 主责规约 | [S_13](../specs/S_13_session-end.md) |

## 1. 范围、状态与依据

首次开发在扩展目标 rsi_native_agent_os（7795851916dd5ce1d34b70f5a20d6d30eafc25e3）开发，使用现有 Core ead700d7d18a247968d64de54918cf5d201a207a，不依赖未合入 PR04/05/06 或 Core !110。实现、定向回归和真实服务验证已完成，证据与范围见第 6 节。

当前永久删除、归档、连接断开、Runtime 缓存清理相互独立。采用已有 Runtime 删除参与者协议，在明确的主会话永久删除收尾阶段发通知；不把每轮完成或缓存释放当作会话结束。不修改 Team/子 Agent，不添加前端入口或额外用户 Ops。

## 2. 能力说明与 Claude 对照

`session.end` 让用户在主会话永久删除流程中，释放自己为该会话准备的缓存、订阅和连接。Runtime 已停止目标执行、准备释放资源时，Worker EventBus 调用 USER Handler；用户依据平台给出的 session_id 清理自己的资源，再继续默认通知 CORE。平台随后继续原资源释放和文件删除。事件不会替用户执行清理，也不允许否决删除。

Claude 固定提交 bf2323b3021c9d3db7a31e1e7d61068c3f704497（声明文件头 2.1.277）的 session.end 包含引擎提供的结束原因、会话身份和恢复资料，默认返回会话身份。本地保留只读观察与继续默认链的用途，首版只覆盖 session_deleted，不伪造 resume、进程退出、clear 或切换会话信息，不承诺直接复用其类型。

PR06 的 session.start 按包激活实例首次输入前初始化；本事件按主会话删除收尾通知，两者粒度不同。用户的会话资源应按 session_id 管理，不能只保存一份“最后打开会话”的资源。

| 项 | 本次实现 | 与固定 Claude 定义的关系 |
| --- | --- | --- |
| 事件职责 | 主会话永久删除的资源释放前收尾通知 | 保留通知扩展用途；具体粒度以本地真实入口为准 |
| 输入 | session_id, end_id, reason, channel_id, mode | 使用本地平台事实和 frozen dataclass；字段逐项见第 3.2 节，不直接互换类型 |
| 输出与改写 | 默认 CORE 的真实通知回执；全部输入只读 | 不补造目录变更、拒绝或取消已提交业务的能力 |
| 方法能力 | 不新增用户同名 Ops；复用已有只读会话 Ops | 内部触发入口、用户调用与结果消费见第 4 节 |

## 3. 事件契约

### 3.1 触发前后与粒度

已有 `session.delete` 请求经过原 Front/Supervisor 路由到 Worker，Runtime 确认目标并 quiesce 后，在宿主删除参与者的 `release_resources` 阶段发通知。通知返回或达到等待预算后，原 Runtime 才继续资源释放、Runner.release 和文件删除；默认 CORE 不负责再次删除。

```text
原 session.delete → 目标校验、删除屏障、quiesce
  → ExtensionSessionEndParticipant.release_resources
  → 从 Runtime 目标构造输入、end_id、SessionContext，记录本次已尝试
  → bind_end_invocation → await bus.trigger(Event("session.end", input), context)
  → USER 按 session_id 清理自己的资源 → 原样 next → session_end_core
  → 真实通知回执返回 → 参与者结束等待
  → 原 Runtime 释放平台资源、删除文件 → 原删除请求返回实际结果
```

粒度是一个 Runtime 对同一未完成永久删除最多一次通知尝试。普通回合结束、归档/恢复、断连、页面隐藏、Runtime 缓存回收不触发；没有成功通知也不能反推原删除没有继续。

### 3.2 输入：每个字段表达什么、由谁填写

实际导入位置是 `jiuwenswarm.extensions_v2.event_types.session_end`，Handler 接收 frozen dataclass `SessionEndInput`，不是任意 JSON 字典。以下是平台生成后可观察到的形状示意；用户只读，不复制这个构造器直接触发事件。

```python
from jiuwenswarm.extensions_v2.event_types.session_end import SessionEndInput

SessionEndInput(
    session_id='demo-session-a',
    end_id='0123456789abcdef0123456789abcdef',
    reason='session_deleted',
    channel_id='web',
    mode='agent.plan',
)
```

全部构造字段均必填、没有可省略默认；表中的 None/空串是平台明确提供的值。下表解释怎样读、代表什么、怎样使用；主责契约仍为 [S_13](../specs/S_13_session-end.md)。

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| session_id | str，必填，无构造默认 | 本次清理针对哪个主会话。用途：用户按此键释放自己为该会话保存的缓存/订阅 | Runtime 提供待永久删除的主会话 ID，如 demo-session-a | 只读；不把另一个会话 ID 回填给 next |
| end_id | str，必填，无构造默认 | 本 Runtime 第一次删除收尾通知的身份。用途：用于清理日志关联；不是新的会话 ID、请求 ID 或用户重试参数 | 平台 uuid.uuid4().hex，如 0123456789abcdef0123456789abcdef | 只读，32 位十六进制；同一未完成删除的重试不重新通知 |
| reason | str，必填，首版固定 session_deleted | 已经进入明确的永久删除收尾阶段。用途：可用作 matcher 条件；不能据此判断文件已经删除成功 | 平台填写 session_deleted，用户不自行选择 | 只读，不支持 switch、shutdown、clear、resume 等推测原因 |
| channel_id | str，必填，无构造默认 | 目标主会话所属的渠道事实。用途：用于日志与筛选，不代表当前在线网页连接 | Runtime 从保存资料/原请求解析，如 web；缺失为 default | 只读，不因当前传输断开而变化为另一目标 |
| mode | str，必填，无构造默认 | 被删除会话原来的执行模式。用途：按已保存模式筛选自己的清理规则 | Runtime 提供目标主会话模式，如 agent.plan | 只读；Team/子 Agent 不在本事件覆盖范围 |

#### 本次 Handler 的会话上下文与包身份

Event 输入是可观察的通知内容，SessionContext 是平台另行提供的可信会话资料；两者不是用户填写后再相互组装的关系。平台通知函数构造 context，Bus 为每个实际 Handler 创建绑定该资料的 Ops。

| 字段 | 类型 / 是否必有 | 含义与应该填写什么 |
| --- | --- | --- |
| ops.session_context | Core 只读属性，不用 await | 本次调用使用哪份会话资料。平台先构造，随 trigger 的独立 context 参数传入；Core 绑定给本次 Handler。用途：用户读取已有资料；它不负责采集或组装上下文。边界：不能通过改 Event 字段替换，不是用户补填对象 |
| session_context.session_id | 有效非空 str | 可信查询目标是被永久删除的会话。平台提供被永久删除的会话 ID；与事件输入 session_id 一致。用途：既有会话 Ops 根据它读取对应平台服务。边界：只读；不得拿 next_session_id/previous_session_id 代替当前绑定 |
| session_context.cwd | str 或 None | 可得的真实执行目录观察值。start 使用已绑定运行目录；其他三种通知未确认 cwd，保持 None。用途：已有 cwd 查询可以使用；None 表示该入口未收集到。边界：不以进程默认目录或项目根伪造 cwd |
| session_context.metadata.project_root | str 或 None | 已确认的用户项目根资料。start 使用已绑定项目根；其余通知只接受目标保存的绝对 project_dir。用途：已有 root 查询读取这个字段；不是自动推断的 Git 根。边界：不可信/缺失时为 None；不为通知创建项目或模型 |
| ops.package_metadata | Core 绑定的当前 Handler 包身份 | 当前正在执行的 USER 属于哪个扩展包。来自已注册 Handler 的真实包元信息；CORE 没有用户包身份。用途：管理自己包的资源和日志；与被观察会话是两个维度。边界：用户不从 payload 指定或冒充其他包 |

本入口不主动采集运行模型；model 查询沿用已有保存信息读取规则，不为展示创建或激活模型。 本 PR 沿用已合入的只读 Ops；七项查询事件化由独立 PR !100 提供，不是本生命周期特性的依赖。

### 3.3 输出：真实通知回执、继续与失败

默认 CORE 返回 `SessionEndOutput`。正常用户 Handler 必须返回 `await next_handler(event)` 得到的真实回执内容；本通知不使用查询事件的 value/deny 封装，不返回 `{"accepted": True}` 字典，也没有用户拒绝/改写平台事实的协议。

下面仅展示回执形状，**不让用户自行构造并返回假成功**；字段由 CORE 填写：

```python
from jiuwenswarm.extensions_v2.event_types.session_end import SessionEndOutput

SessionEndOutput(
    accepted=True,
    session_id='demo-session-a',
    end_id='0123456789abcdef0123456789abcdef',
)
```

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| accepted | bool，CORE 实际填严格 True | 默认清理通知已经被确认。默认 CORE 生成，用户只接收。用途：核对本次通知；最终删除是否成功要看原删除响应。边界：不得构造假成功、deny 或取消删除结果；不保证全部外部清理成功 |
| session_id | str，必有 | 已确认通知属于哪个主会话。CORE 回显平台删除目标。用途：核对自己的资源键与日志。边界：只读，不切换清理目标 |
| end_id | str，必有 | 被确认的具体收尾通知。CORE 回显这次通知的 32 位十六进制 UUID。用途：将处理日志与同一次通知对应起来。边界：只读，不是业务重试参数或删除提交证明 |

所有输入只读、没有可改写字段。空 matcher 匹配本次候选中的该事件；可用第 3.2 节字段等值筛选，字段不匹配不会替用户延后重放。 正常 next 继续当前通知链；提前短路伪造成功、修改输入、后置改写回执均不构成合法平台操作，按既有 Core USER 故障隔离处理。

CORE 在当前可信调用作用域内核对原始输入和绑定会话，再记录真实回执；同次调用重复 next 复用该回执，不重复业务操作。用户抛普通异常可能被 Core 隔离后继续默认处理；不能把抛错当作取消平台业务的机制。

通知失败或超时后原删除继续；原删除请求取消仍传播，由原 Runtime 保留其补偿语义。 单次通知等待预算默认 2 秒，从通知任务启动计；超时使作用域失效并请求取消，忽略取消的任务可能晚退出，但不能再产生迟到有效 CORE 回执。包在途计数直到任务退出才释放。 accepted 只说明默认通知被确认，用户自有资源操作是否成功应由自己的代码记录；外部副作用不自动补偿。 本事件不是跨重启的 exactly-once。

### 3.4 注册、筛选、处理示例

本例要做的事情是：**正常回合中为不同会话保存自己的项目根缓存；永久删除某个主会话时，只释放该会话的缓存，并记录对应 end_id。** 下列代码可放入扩展包的 `extension.py`；包格式参照[现有示例包的 manifest](../../examples/session_end/manifest.yaml)，由现有管理入口安装。所有事件字段保持只读，用户处理自己拥有的资源并原样返回 next 的真实回执。

```python
class Extension:
    def __init__(self):
        self.session_cache = {}
        self.released = {}

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")
        registrar.register_event_handler("session.end", self.on_end, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        session_id = ops.session_context.session_id
        self.session_cache[session_id] = {"root": await ops.session.root()}
        return await next_handler(event)

    async def on_end(self, event, ops, next_handler):
        value = event.event_input
        # Release only resources owned by this package for this session.
        self.session_cache.pop(value.session_id, None)
        receipt = await next_handler(event)
        self.released[value.end_id] = value.session_id
        return receipt


def create_extension():
    return Extension()
```

假设 on_usage 已给 demo-session-a 和 demo-session-b 分别建立缓存。用户永久删除 a 时，Runtime 在 quiesce 后、平台文件删除前发 session.end；on_end 只 pop(a)，next 由 CORE 确认有效通知，再在 released[end_id] 记下 a，b 的缓存保留。

可观察结果是 a 的用户缓存消失，released 关联到这次真实 end_id；它不删除平台 JSONL，也不操作其他包资源。CORE 返回后，Runtime 继续原平台资源释放及文件删除，最终成功仍以原删除响应为准。用户清理用 pop(..., None) 保持幂等；真实外部资源应由用户自己的代码释放和补偿。

Matcher 为等值条件合取，空字典匹配候选中的全部合法通知；按 channel_id、reason 等实际存在字段筛选时，不匹配的通知不会替用户延后重放。多个 USER 按既有顺序通过 next 继续；本生命周期事件未新增业务 BUILTIN。初始化共享 Ops 的 ops.build BUILTIN 属于启动装配，不在这条业务通知链中重新登记方法。

## 4. 方法契约

### 4.1 用户侧：复用已有 Ops，不新增同名方法

本特性没有新增用户 Ops。既有只读 cwd/root/model/repo 可在平台绑定的上下文下使用；会话 ID 已知不等于工作目录和运行模型已知。cwd 无可信值时为 None，root 只接受保存的绝对项目目录，model 仍遵循其保存值读取契约。

用户不调用通知函数或手工构造 SessionContext。正式示例包见 [session_end](../../examples/session_end/README.md)，只注册 USER Handler；原会话删除入口负责触发。

本项没有 `ops.session.end()`。用户订阅的是通知事件，不能把无可信平台调用作用域的 Event 当作原业务请求替代品；复用已有 Ops 也不会重新触发本生命周期事件。

### 4.2 平台内部入口与结果消费

Runtime 调用固定参与者的 `release_resources(target)`；target 为已确认的 SessionLifecycleTarget，参与者返回 None。它只等待通知，不用这个 None 表示删除成功；删除结果仍由原 Runtime 请求返回。 通知输入和上下文由第 3.1 节真实边界收集；没有要求用户自己创建 Bus、注册 CORE 或改平台 Adapter。

平台入口参数（只由对应业务流程填写）：

| 输入字段 | 必填 / 默认 | 应填写的内容、范围及示例 |
| --- | --- | --- |
| target | SessionLifecycleTarget，必填 | Runtime 已确认并完成 quiesce 的删除目标，来自原删除编排；主会话才通知。参与者读取 session_id、channel_id、mode、可信项目目录，用户不自行构造调用。 |

## 5. 接入与兼容方案

Worker 启动时登记 session.end 的类型、Matcher、Validator 和唯一默认 CORE；既有 ops.build 的 CORE 创建 Builder、BUILTIN 登记共享能力，Bus 保存方法目录，用户包 register 发布 USER 订阅。通知发生时，Core 为每个实际 Handler 绑定本次 SessionContext 和可信包身份，Builder 不在启动时查询某个会话的数据。

平台资料从哪里来：删除参与者从 SessionLifecycleTarget.descriptor 读取目标身份、渠道、模式和保存项目目录，只保留可信绝对 project_root；cwd 没有确认时保持 None，不临时创建模型。 平台将通知输入和 SessionContext 作为 trigger 的两部分传入；ops.session_context 只提供只读入口，已有会话 Ops 根据它读取。用户不采集平台资料，也不需要先触发用量通知。

默认 CORE 的逻辑：**session_end_core 先检查有效平台调用、输入完全等于原删除目标、Ops 绑定同一 session_id，再生成或复用 SessionEndOutput。它只确认这次清理通知，实际 KVC／Runner 释放和文件删除仍由原 Runtime 在通知返回后完成。** USER 负责本包自己的初始化、摘要或清理，按 next 进入 CORE 并原样返回回执；没有新增业务 BUILTIN 或同名用户 Ops。第 3.1 节是实际触发链，第 3.4 节给出可以消费这些事实的用户业务案例。

类型和注册归 event_types，CORE 归 event_handlers；实际 trigger 及通知任务归 server/runtime/session 的平台参与者。RuntimeParticipantRegistry 增加可选宿主固定删除参与者，与 KVC 的动态发布组合；AgentServer 在构建 Runtime 时注入，Runtime 不直接依赖 EventBus。

选择通知 CORE，不在该 CORE 里第二次释放原资源，避免双重释放和破坏现有删除事务。无需 Core handler_ids 新接口；一次通知沿现有 matcher 执行当前用户链，并为活动包记录在途使用直至任务结束。无装配、无用户包或卸载后保留默认删除，不更改 Front/Supervisor 协议。

本次文档整理沿用当前分支既有共享只读 Ops；PR !100 的查询事件化及返回格式迁移单独交付。session.start 的 handler_ids 仍以 Core !110 为硬前置，合并顺序保持 Core 在先、Server 在后。其他三项单项不要求 handler_ids，组合 PR 的依赖仍成立。

源码位置：[事件定义／校验／注册](../../event_types/session_end.py)、[默认 CORE](../../event_handlers/session_end.py)、[Runtime 删除通知参与者](../../../server/runtime/session/end_notifications.py)。平台实现按职责和源码链接查阅；用户只注册文中示例 Handler，平台仍拥有原业务操作的提交和后续响应。

## 6. 验收与未决问题

本次 2026-10-31 仅按 F_16 的写法补齐输入/输出字段、注册示例、真实触发条件、Handler 职责和完整业务案例；运行代码与契约不变。文档核对包括 dataclass 字段与示例值、源码节选、Python/YAML/JSON 示例及相对链接。下列回归和真实服务结果均保留原运行日期与范围，不宣称本次重新执行业务或模型验收。

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原独立交付为 Server !93；合并后同一个 RuntimeParticipantRegistry 同时保留固定删除观察者和固定 foreground 观察者。KVC 启停、挂起与 lease 释放的用例同时核对两者不会互相覆盖；本事件自身不要求 handler_ids，也不因此新增 end/detach 联动。

2026-10-29，Windows / Python 3.11.15，使用上文固定的 Server 基线与 Core ead700d7d18a247968d64de54918cf5d201a207a，当前工作区变更包含本特性。404 项扩展/Runtime/删除与归档回归通过，无跳过；另单独重验本特性的 19 项用例通过（包含在该回归范围中，不重复累计）。仅出现外部 Authlib 弃用提示，未运行格式化或静态检查工具。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/runtime/test_runtime_session_provisioner.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_session_lifecycle_participants.py tests/unit_tests/runtime/test_runtime_architecture.py tests/unit_tests/server/test_archive_lifecycle.py tests/unit_tests/server/test_agent_lifecycle_request.py --asyncio-mode=auto -o log_cli=false
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2/test_session_end.py --asyncio-mode=auto -o log_cli=false
```

| 场景 | 操作与预期 | 实际证据 |
| --- | --- | --- |
| 真用户包与默认 CORE | 安装用户模块，目标/包身份/Ops 正确，next 得到真实回执 | test_session_end.py；真实服务的两个安装包均执行 |
| 重复/并发与失败重试 | 同一未完成删除至多尝试一次，提交后释放状态 | 定向用例与真实 concurrent-delete/repeated-delete 记录 |
| 防改写/短路 | 五个输入字段、提前伪造结果、后置改写及 bool/int 替换无效，CORE 仍处理原目标 | 注册包参数化用例，检查真实调用作用域中的 CORE 回执 |
| 超时/取消 | 合作取消、忽略取消的任务、原请求取消均有明确边界 | 2 秒生产预算的短预算定向构造；超时后不允许迟到 CORE，包计数最终归零 |
| 原生删除顺序 | USER/通知 CORE 完成时目录仍在，随后原 Runtime 实际删除 | 实际 AgentRuntime.delete_session 测试；真实 Worker 读回同样事实 |
| 不误判结束 | 普通 cleanup、断连/重连、归档/恢复不通知 | Runtime 定向测试；真实连接与归档操作，通知计数保持不变 |
| 参与者共存 | KVC 启停、suspend 和 lease release 保留宿主参与者 | 实际 KVCacheApplicationOwner 与 Registry 回归 |
| 作用域与卸载 | 两会话互不串用，Team 排除；卸载后原删除继续 | 定向 Team/跨会话用例；真实卸载后创建并删除第三个会话 |

真实服务证据：`D:\work_space\.codex-temp\server-session-end-event-20261029\real-191323`。实际 Front → Supervisor → 子 Worker → Qwen3.8-Flash 完成两个模型回合；用户包在已有用量事件创建自己拥有的资源文件，在 session.end 内删除它们并取得默认 CORE 回执。断连、归档/恢复不产生结束通知；两个目标均只通知一次，平台目录随后实际删除。现成示例日志出现两次 released=True。未 mock Ops、模型或 Adapter，未手工构造 SessionContext。

result.json、user-handler-results.jsonl、各删除/归档响应、日志和 source-before/source-after 收据记录来源及一致性；cleanup.json 确认 Worker 已退出、端口关闭。首次联调脚本把归档请求误写为单个 session_id，收到 BAD_REQUEST 后已按既有 session_ids 协议修正；该次不计为验收通过，当前结论以上述成功运行记录为准。

已知边界：没有跨进程崩溃/重启 exactly-once；用户资源在取消或平台随后失败时可能已清理，平台不恢复外部副作用。没有真实服务下的恶意无限任务终止保证、非 Windows 实机、产品浏览器 UI、远端 CI 或大并发性能证据。不同 Harness 的统一结束语义、进程退出和恢复资料仍不在首版范围。

2026-11-02 文档整理：以原 session.append 为参照，统一对象示例、五列通知输入表、三列输出表、会话资料说明及具体用户 Handler 案例；保留所有字段来源、默认／缺失、权限、超时和历史验证记录。只改文档，当前契约及 Core !110 前置不变。本轮核对结构、字段、示例与真实默认 CORE 回执、链接及差异，不重跑真实业务、模型或浏览器验收。

## 7. 参考资料

- [S_13](../specs/S_13_session-end.md)、[内置事件开发指南](../designs/builtin-event-development-guide.md)。
- [Runtime 生命周期协议](../../../runtime/session_lifecycle.py)、[删除编排](../../../runtime/session_provisioner.py)、[AgentServer 装配](../../../server/agent_ws_server.py)。
- [Claude 固定声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。

- [事件定义](../../event_types/session_end.py)、[默认 CORE](../../event_handlers/session_end.py)、[平台通知参与者](../../../server/runtime/session/end_notifications.py)、[用户示例](../../examples/session_end/README.md)。
