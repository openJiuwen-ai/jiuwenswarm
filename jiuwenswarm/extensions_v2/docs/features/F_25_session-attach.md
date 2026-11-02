# session.attach 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_25 / session-attach |
| 建档日期 | 2026-10-29 |
| 归档日期 | 2026-10-29 |
| 后续修订 | 2026-10-29，既有实现与验证；2026-10-31，以 session.append 为标准补齐逐字段说明、业务案例和源码调用链；运行契约不变；2026-11-02，按原 session.append 的对象示例、输入／输出字段表及具体 Handler 案例统一说明，契约不变 |
| 计划与进度 | [EXTV2-20 / Server PR08](../designs/extensions-v2-development-plan.md#extv2-20-界面与主会话关联建立通知) |
| 主责规约 | [S_14](../specs/S_14_session-attach.md) |

## 1. 范围、状态与依据

首次开发基于扩展目标 rsi_native_agent_os 的 7795851916dd5ce1d34b70f5a20d6d30eafc25e3，Core 使用 ead700d7d18a247968d64de54918cf5d201a207a。采用现有 Web view_id → Server 提交上下文 → Runtime foreground_changed 真实链路，不依赖尚未合入 PR04/05/06/10 或 Core !110。

源码已经有 session.create/session.switch 的提交事实，缺少独立的界面与主会话关联表和扩展通知。本特性补充 Worker 平台关联表、session.attach 事件/默认 CORE、宿主 foreground 接线和用户示例。实现与定向、真实服务验证已完成，结果见第 6 节。

## 2. 能力说明与 Claude 对照

session.attach 表示一个具体视图已经进入一个有效主会话。用户可据此初始化这个视图的会话展示、记录关联日志或读取既有只读 Ops；平台先提交关联事实，Worker EventBus 再调用用户 Handler。用户原样 next，CORE 校验当前关联并确认，不再次执行会话切换或启动聊天。

Claude 固定提交 bf2323b3021c9d3db7a31e1e7d61068c3f704497、声明头 2.1.277 的 session.attach 描述客户端加入会话，包含渲染面、客户端 ID 和可选 viewport，默认回显客户端身份。本地以已有 view_id 表示具体视图，另含会话/关联身份，不把 A2UI surfaceId、Gateway 连接或 ui.ready 冒充客户端关联；暂不提供 viewport 或终端隐式绑定，不承诺类型可直接互换。

| 项 | 本次实现 | 与固定 Claude 定义的关系 |
| --- | --- | --- |
| 事件职责 | 具体视图与有效主会话提交关联后的通知 | 保留通知扩展用途；具体粒度以本地真实入口为准 |
| 输入 | session_id, surface_id, attachment_id, channel_id, mode, previous_session_id | 使用本地平台事实和 frozen dataclass；字段逐项见第 3.2 节，不直接互换类型 |
| 输出与改写 | 默认 CORE 的真实通知回执；全部输入只读 | 不补造目录变更、拒绝或取消已提交业务的能力 |
| 方法能力 | 不新增用户同名 Ops；复用已有只读会话 Ops | 内部触发入口、用户调用与结果消费见第 4 节 |

## 3. 事件契约

### 3.1 触发前后与粒度

沿用已有 `session.create` / `session.switch` 的真实提交点：prepare/abort 不通知；Runtime 提交后由宿主 `foreground_changed(visible=True)` 确认有效主会话，先写入平台关联表，再发通知。create 保持原响应先交付、后后台提交；switch 保持原提交 await 链。

```text
客户端原创建/切换请求 + 稳定 view_id → Front/Supervisor → Worker
  → 原 Runtime prepare → commit → foreground_changed(visible=True)
  → 确认有效主会话，构造只读上下文，提交 channel+view 的新关联
  → 有旧关联则先通知旧 detach；再确认新关联仍有效
  → bind_attach_invocation → await bus.trigger(Event("session.attach", input), context)
  → USER 读取已有 Ops / 准备自己本次关联的资源 → 原样 next
  → session_attach_core 再确认关联版本、目标和绑定会话 → 真实回执返回
  → 原创建/切换继续；不额外聊天或切换
```

粒度是每个 channel_id+surface_id 的一次新主会话关联。同一现存关联重放不重复发；同会话的两个视图分别发；切换再切回形成新 attachment_id。单纯 ui.ready、输入意图、模型执行、断连重连不构成新关联。

### 3.2 输入：每个字段表达什么、由谁填写

实际导入位置是 `jiuwenswarm.extensions_v2.event_types.session_attach`，Handler 接收 frozen dataclass `SessionAttachInput`，不是任意 JSON 字典。以下是平台生成后可观察到的形状示意；用户只读，不复制这个构造器直接触发事件。

```python
from jiuwenswarm.extensions_v2.event_types.session_attach import SessionAttachInput

SessionAttachInput(
    session_id='demo-session-a',
    surface_id='view-main',
    attachment_id='0123456789abcdef0123456789abcdef',
    channel_id='web',
    mode='agent.plan',
    previous_session_id=None,
)
```

全部构造字段均必填、没有可省略默认；表中的 None/空串是平台明确提供的值。下表解释怎样读、代表什么、怎样使用；主责契约仍为 [S_14](../specs/S_14_session-attach.md)。

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| session_id | str，必填，无构造默认 | 这个视图已经进入的目标主会话。用途：作为本次会话 Ops 的身份；记录关联到哪个会话 | 平台填写真实提交并再次确认可用的主会话 ID，如 demo-session-a | 只读；不存在、已归档/删除或禁入目标不发成功关联通知 |
| surface_id | str，必填，无构造默认 | 一个具体客户端视图/窗口。用途：结合 channel_id 标识关联槽位；多个窗口各用稳定不同值 | 来自已有提交上下文的 view_id，如 view-main；1～128 个 ASCII 字母/数字或 ._:- | 只读，不是 A2UI 卡片、渲染槽位或 Gateway 连接 ID |
| attachment_id | str，必填，无构造默认 | 这个视图本次进入目标会话所形成的关联身份。用途：用户按它建立本次关联资源；以后 detach 用它精确清理 | 平台生成 32 位十六进制 UUID，如 0123456789abcdef0123456789abcdef | 只读；同一关联重放不换 ID，离开再进入会产生新 ID |
| channel_id | str，必填，无构造默认 | 关联属于哪个平台渠道。用途：与 surface_id 一起区分关联；可用于 matcher | 平台填写本次提交渠道，如 web | 只读，不是物理设备或传输连接身份 |
| mode | str，必填，无构造默认 | 这个新目标原本的主会话模式。用途：用户选择自己的展示或资源策略；不作为切换命令 | 平台填写已确认目标模式，如 agent.plan | 只读，不能改成另一个模式要求平台执行 |
| previous_session_id | str 或 None，必填，无构造默认 | 同一 channel+surface 上一次关联的主会话。用途：观察切换来源；不是删除目标，也不表示旧会话已经结束 | 平台关联表的旧目标 ID；首次进入/无旧主会话关联时为 None | 只读，不采用请求自报值作为权威，不改成旧 attachment_id |

#### 本次 Handler 的会话上下文与包身份

Event 输入是可观察的通知内容，SessionContext 是平台另行提供的可信会话资料；两者不是用户填写后再相互组装的关系。平台通知函数构造 context，Bus 为每个实际 Handler 创建绑定该资料的 Ops。

| 字段 | 类型 / 是否必有 | 含义与应该填写什么 |
| --- | --- | --- |
| ops.session_context | Core 只读属性，不用 await | 本次调用使用哪份会话资料。平台先构造，随 trigger 的独立 context 参数传入；Core 绑定给本次 Handler。用途：用户读取已有资料；它不负责采集或组装上下文。边界：不能通过改 Event 字段替换，不是用户补填对象 |
| session_context.session_id | 有效非空 str | 可信查询目标是新关联的目标会话。平台提供新关联的目标会话 ID；与事件输入 session_id 一致。用途：既有会话 Ops 根据它读取对应平台服务。边界：只读；不得拿 next_session_id/previous_session_id 代替当前绑定 |
| session_context.cwd | str 或 None | 可得的真实执行目录观察值。start 使用已绑定运行目录；其他三种通知未确认 cwd，保持 None。用途：已有 cwd 查询可以使用；None 表示该入口未收集到。边界：不以进程默认目录或项目根伪造 cwd |
| session_context.metadata.project_root | str 或 None | 已确认的用户项目根资料。start 使用已绑定项目根；其余通知只接受目标保存的绝对 project_dir。用途：已有 root 查询读取这个字段；不是自动推断的 Git 根。边界：不可信/缺失时为 None；不为通知创建项目或模型 |
| ops.package_metadata | Core 绑定的当前 Handler 包身份 | 当前正在执行的 USER 属于哪个扩展包。来自已注册 Handler 的真实包元信息；CORE 没有用户包身份。用途：管理自己包的资源和日志；与被观察会话是两个维度。边界：用户不从 payload 指定或冒充其他包 |

本入口不主动采集运行模型；model 查询沿用已有保存信息读取规则，不为展示创建或激活模型。 本 PR 沿用已合入的只读 Ops；七项查询事件化由独立 PR !100 提供，不是本生命周期特性的依赖。

### 3.3 输出：真实通知回执、继续与失败

默认 CORE 返回 `SessionAttachOutput`。正常用户 Handler 必须返回 `await next_handler(event)` 得到的真实回执内容；本通知不使用查询事件的 value/deny 封装，不返回 `{"accepted": True}` 字典，也没有用户拒绝/改写平台事实的协议。

下面仅展示回执形状，**不让用户自行构造并返回假成功**；字段由 CORE 填写：

```python
from jiuwenswarm.extensions_v2.event_types.session_attach import SessionAttachOutput

SessionAttachOutput(
    accepted=True,
    session_id='demo-session-a',
    surface_id='view-main',
    attachment_id='0123456789abcdef0123456789abcdef',
)
```

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| accepted | bool，CORE 实际填严格 True | 检查时这次关联仍然有效。CORE 在当前关联及目标可用性再次检查后生成。用途：可以用来确认本次通知的有效版本。边界：不证明所有 USER 展示成功，也不保证返回后永远不会再切换 |
| session_id | str，必有 | 被确认的目标会话。CORE 回显已绑定新目标主会话。用途：与输入和当前 Ops 的会话核对。边界：只读，不通过回执执行切换 |
| surface_id | str，必有 | 被确认的视图。CORE 回显本次具体视图。用途：结合 attachment_id 保存自己的资源键。边界：只读，不替换成另一个视图 |
| attachment_id | str，必有 | 检查时仍有效的关联版本。CORE 回显这次当前关联的 UUID。用途：为日志、用户幂等及后续解除定位提供身份。边界：只读，不能拿旧 ID 声称新关联也已确认 |

所有输入只读、没有可改写字段。空 matcher 匹配本次候选中的该事件；可用第 3.2 节字段等值筛选，字段不匹配不会替用户延后重放。 正常 next 继续当前通知链；提前短路伪造成功、修改输入、后置改写回执均不构成合法平台操作，按既有 Core USER 故障隔离处理。

CORE 在当前可信调用作用域内核对原始输入和绑定会话，再记录真实回执；同次调用重复 next 复用该回执，不重复业务操作。用户抛普通异常可能被 Core 隔离后继续默认处理；不能把抛错当作取消平台业务的机制。

通知失败或超时不会恢复已经提交的关联事实；原调用取消仍传播，不把已发生的变更伪装成未发生。 单次通知等待预算默认 2 秒，从通知任务启动计；超时使作用域失效并请求取消，忽略取消的任务可能晚退出，但不能再产生迟到有效 CORE 回执。包在途计数直到任务退出才释放。 accepted 只说明默认通知被确认，用户自有资源操作是否成功应由自己的代码记录；外部副作用不自动补偿。 本事件不是跨重启的 exactly-once。

### 3.4 注册、筛选、处理示例

本例要做的事情是：**真实切到一个主会话后，为该界面关联保存项目根摘要；离开时按 attachment_id 清理，允许同一会话被多个视图分别关联。** 下列代码可放入扩展包的 `extension.py`；包格式参照[现有示例包的 manifest](../../examples/session_attach/manifest.yaml)，由现有管理入口安装。所有事件字段保持只读，用户处理自己拥有的资源并原样返回 next 的真实回执。

```python
class Extension:
    def __init__(self):
        self.views = {}

    def register(self, registrar):
        registrar.register_event_handler("session.attach", self.on_attach, {}, "user")
        registrar.register_event_handler("session.detach", self.on_detach, {}, "user")

    async def on_attach(self, event, ops, next_handler):
        root = await ops.session.root()
        receipt = await next_handler(event)
        # CORE has checked this association; perform no further await here.
        self.views[receipt.attachment_id] = {
            "session_id": receipt.session_id,
            "surface_id": receipt.surface_id,
            "root": root,
        }
        return receipt

    async def on_detach(self, event, ops, next_handler):
        self.views.pop(event.event_input.attachment_id, None)
        return await next_handler(event)


def create_extension():
    return Extension()
```

视图 view-main 沿原 session.create/session.switch 提交到 demo-session-a 后，平台建立带唯一 attachment_id 的关联，再触发 session.attach。on_attach 读取可得项目根，next 让 CORE 复核关联和目标仍有效，再在 views[attachment_id] 保存 session_id、surface_id、root；None 表示该入口没有可得项目根。

第二个视图进入同一会话会有另一 attachment_id，拥有独立摘要。后续 session.detach 只清理对应旧键。CORE 返回后到保存状态之间没有额外 await；若用户改为调用外部异步服务，需要自行处理关联过期和清理竞态，accepted 不保证关联永久有效。

这个摘要是用户侧数据，可交给自己的后续业务函数；通知不会自动渲染、写历史或启动 Agent。安装包不补发当前已存在的关联，应通过后续真实的新关联提交触发案例。

Matcher 为等值条件合取，空字典匹配候选中的全部合法通知；按 channel_id、reason 等实际存在字段筛选时，不匹配的通知不会替用户延后重放。多个 USER 按既有顺序通过 next 继续；本生命周期事件未新增业务 BUILTIN。初始化共享 Ops 的 ops.build BUILTIN 属于启动装配，不在这条业务通知链中重新登记方法。

## 4. 方法契约

### 4.1 用户侧：复用已有 Ops，不新增同名方法

不新增 Ops。平台将已确认的 session_id/保存项目根绑定到 SessionContext，Core 为 Handler 创建本次 Ops。cwd 未确认可为 None；root/model 直接使用各自既有只读规则，不要求先调用 notify_context_usage，不创建模型。用户不提交“收集好的上下文”或修改平台 Adapter。

现成用户包见 [session_attach](../../examples/session_attach/README.md)，注册 on_attach、记录关联及项目根，不自动写历史或渲染页面。

本项没有 `ops.session.attach()`。用户订阅的是通知事件，不能把无可信平台调用作用域的 Event 当作原业务请求替代品；复用已有 Ops 也不会重新触发本生命周期事件。

### 4.2 平台内部入口与结果消费

Runtime 在原创建/切换提交后调用宿主 `foreground_changed(event)`；event 为真实 SessionForegroundEvent，平台确认并提交关联后再通知，返回 None。它不是用户填写任意 session_id 就能调的远程方法；原创建/切换结果沿原请求返回。 通知输入和上下文由第 3.1 节真实边界收集；没有要求用户自己创建 Bus、注册 CORE 或改平台 Adapter。

平台入口参数（只由对应业务流程填写）：

| 输入字段 | 必填 / 默认 | 应填写的内容、范围及示例 |
| --- | --- | --- |
| event | SessionForegroundEvent，必填 | 原创建／切换提交后的真实 foreground 事实；包含目标、渠道及 view_id，visible=True 时进入关联确认。prepare 不通知，用户不自行构造调用。 |

## 5. 接入与兼容方案

Worker 启动时登记 session.attach 的类型、Matcher、Validator 和唯一默认 CORE；既有 ops.build 的 CORE 创建 Builder、BUILTIN 登记共享能力，Bus 保存方法目录，用户包 register 发布 USER 订阅。通知发生时，Core 为每个实际 Handler 绑定本次 SessionContext 和可信包身份，Builder 不在启动时查询某个会话的数据。

平台资料从哪里来：平台在已确认的关联提交处从目标描述读取会话资料，只保留可得的可信绝对 project_root，生成唯一 attachment_id 并保存这次关联的上下文；cwd 未确认时保持 None。 平台将通知输入和 SessionContext 作为 trigger 的两部分传入；ops.session_context 只提供只读入口，已有会话 Ops 根据它读取。用户不采集平台资料，也不需要先触发用量通知。

默认 CORE 的逻辑：**session_attach_core 检查有效平台调用、原始输入、绑定会话和当前关联，等待 validate_target 再确认目标可用，随后重新核对当前关联，生成或复用 SessionAttachOutput。它不再次切换会话、建立关联或发起聊天。** USER 负责本包自己的初始化、摘要或清理，按 next 进入 CORE 并原样返回回执；没有新增业务 BUILTIN 或同名用户 Ops。第 3.1 节是实际触发链，第 3.4 节给出可以消费这些事实的用户业务案例。

类型/校验/注册在 event_types，CORE 在 event_handlers；实际 trigger 和关联表在 Server 平台通知模块。RuntimeParticipantRegistry 提供固定宿主 foreground 注册（register_host_foreground/snapshot_foreground），与 KVC 动态发布相加；Runtime 仍不知道 EventBus。其他 activity 和输入意图的参与者列表不改变；KVC 关闭时仍保留原有 disabled 语义。此入口已经有真实 view_id 提交事实，因此不新增前端请求、特殊 IPC 或 Gateway attach 解释。

本轮同 PR 增补 PR11：替换旧关联时先提交新事实，再通知旧 detach，之后仅对仍有效的新关联发 attach；显式移除和旧上下文保留见 S_15。一次切换可能包含两个分别限时的通知。

不直接使用 ui.ready，它是包槽位挂载；不使用 session.input.intent，它是输入意图；不在第一次模型调用时猜测关联。没有用量也能发 attach，没有用户包也保留真实关联和原业务。

本次文档整理沿用当前分支既有共享只读 Ops；PR !100 的查询事件化及返回格式迁移单独交付。session.start 的 handler_ids 仍以 Core !110 为硬前置，合并顺序保持 Core 在先、Server 在后。其他三项单项不要求 handler_ids，组合 PR 的依赖仍成立。

源码位置：[事件定义／校验／注册](../../event_types/session_attach.py)、[默认 CORE](../../event_handlers/session_attach.py)、[关联表及提交通知参与者](../../../server/runtime/session/attach_notifications.py)。平台实现按职责和源码链接查阅；用户只注册文中示例 Handler，平台仍拥有原业务操作的提交和后续响应。

## 6. 验收与未决问题

本次 2026-10-31 仅按 F_16 的写法补齐输入/输出字段、注册示例、真实触发条件、Handler 职责和完整业务案例；运行代码与契约不变。文档核对包括 dataclass 字段与示例值、源码节选、Python/YAML/JSON 示例及相对链接。下列回归和真实服务结果均保留原运行日期与范围，不宣称本次重新执行业务或模型验收。

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原 attach/detach 合并交付为 Server !95；本次保留同一个关联服务、独立事件与文档，并与 session.end 固定删除观察者组合。前端、共享 SessionFileService 和已合入历史/用量能力沿用当前目标分支。

以下 378 项与原 real-195031 是 PR08 首次提交的历史证据；本轮 PR08/11 在同一 PR !95 交付，追加解除语义由 [F_26](F_26_session-detach.md) / [S_15](../specs/S_15_session-detach.md) 单独维护。当前联合源码的 446 项回归与 6 次 attach / 5 次 detach 真实验证见 F_26 第 6 节。

2026-10-29，Windows / Python 3.11.15，Server 基于上文固定基线并包含首次 PR08 变更，Core 为 ead700d7d18a247968d64de54918cf5d201a207a。378 项扩展、Runtime、创建/切换提交、KVC 参与者和架构回归通过，无跳过；另单独复验本特性及相关生命周期 27 项通过，包含在前述范围中、不重复累计。只有外部 Authlib 弃用提示，未运行格式化或静态检查工具。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/runtime/test_session_attach_provisioner.py tests/unit_tests/runtime/test_session_create_provisioner.py tests/unit_tests/runtime/test_session_switch_provisioner.py tests/unit_tests/runtime/test_session_provision_contract.py tests/unit_tests/runtime/test_session_lifecycle_participants.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_runtime_architecture.py --asyncio-mode=auto -o log_cli=false
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2/test_session_attach.py tests/unit_tests/runtime/test_session_attach_provisioner.py tests/unit_tests/runtime/test_session_lifecycle_participants.py --asyncio-mode=auto -o log_cli=false
```

| 场景 | 操作和预期 | 实际证据 |
| --- | --- | --- |
| 没有用量也能读取 Ops | 真用户包接收 attach，读取 root/cwd/model，原样 next 得到默认回执 | Registry/EventBus 定向测试；真实创建后首个通知早于所有 usage |
| 多视图、切换、重放 | 两视图进入同会话独立通知；同一关联重放不重复，切换再切回新 ID | 定向与真实 6 次关联记录、稳定 view_id 重连 |
| 真实提交接线 | prepare/abort 不通知，commit 后才有真实表项和 CORE 回执，重复 commit 不重放 | test_session_attach_provisioner；真实 WebSocket 创建/切换经原 IPC |
| 防改写/伪造 | 6 个输入字段、提前成功、后置修改和 int/bool 替换无效 | 参数化用户包测试核对实际调用状态中的 CORE 回执 |
| 失效关联/目标 | 关联已被替换、或 USER 期间目标目录已移除，不得取得迟到有效 CORE 回执 | 定向竞态和目标删除；真实缺失/归档目标不通知 |
| 超时与取消 | 保留已提交事实；超时任务无迟到确认，包在途计数最终释放 | 短预算构造、忽略取消和原调用取消测试 |
| KVC 共存 | 开关、suspend、lease release 不移除固定观察者，不改变输入意图列表 | 实际 KVCacheApplicationOwner 和 Registry 回归 |
| 缺失/未跟踪范围/卸载 | 无效 view、Team 不通知，未安装或卸载后原业务仍工作 | 定向测试与真实包卸载后正常切换 |

真实服务证据目录：`D:\work_space\.codex-temp\server-session-attach-event-20261029\real-195031`。安装现成示例和验收用户 ZIP，经真实 Front → Supervisor → 子 Worker 完成创建/切换；观察 6 个独立关联 ID、正确会话/视图/项目根和默认 CORE 回执。首次通知在没有任何用量与产品历史消息时发生；一个主会话可关联两个 view_id，同视图切换再切回正确记录前一目标，稳定 view_id 重连不重放。缺失/归档目标不产生 attach，原创建/切换协议保持原样。

单独发送一次真实 Qwen3.8-Flash 回合，得到有效用量；这次聊天不会多发 attach，之后新视图可正常关联。不是靠聊天或手工 notify 提供第一次上下文。未 mock Ops、模型或 Adapter，未手工构造 SessionContext。现成示例日志 6 次 accepted=True；卸载后默认切换正常。result.json、user-handler-results.jsonl、wire.jsonl、source-before/source-after 收据保存来源和一致性，cleanup.json 确认 Worker 退出和端口关闭。

边界：内存事实不跨 Worker/Runtime 重启保存，当时 view 关闭的显式撤销尚未接入，本轮 PR11 已在同 PR 增补，详见 S_15；表项不是物理在线证明。首次 PR08 不提供 viewport、surfaces Ops、detach 事件、Team/子 Agent 或产品浏览器视觉验收；当前 detach 由 F_26 独立说明。没有非 Windows 实机、远端 CI、大规模视图性能或跨重启 exactly-once 证据。2 秒仅为事件等待预算，不是整个文件校验或创建/切换的耗时上限。外部初始化副作用可能已经发生，平台不承诺回滚。

2026-11-02 文档整理：以原 session.append 为参照，统一对象示例、五列通知输入表、三列输出表、会话资料说明及具体用户 Handler 案例；保留所有字段来源、默认／缺失、权限、超时和历史验证记录。只改文档，当前契约及 Core !110 前置不变。本轮核对结构、字段、示例与真实默认 CORE 回执、链接及差异，不重跑真实业务、模型或浏览器验收。

## 7. 参考资料

- [S_14](../specs/S_14_session-attach.md)、[Runtime 提交编排](../../../runtime/session_provisioner.py)、[生命周期协议](../../../runtime/session_lifecycle.py)、[Server 请求交付](../../../server/agent_ws_server.py)。
- [Web 实际 view_id 调用方](../../../channels/web/frontend/src/App.tsx)、[内置事件指南](../designs/builtin-event-development-guide.md)。
- [Claude 固定声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。

- [事件定义](../../event_types/session_attach.py)、[默认 CORE](../../event_handlers/session_attach.py)、[平台关联表与触发](../../../server/runtime/session/attach_notifications.py)、[用户包](../../examples/session_attach/README.md)。
