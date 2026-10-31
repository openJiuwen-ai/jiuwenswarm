# session.detach 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_26 / session-detach |
| 建档日期 | 2026-10-29 |
| 归档日期 | 2026-10-29 |
| 后续修订 | 2026-10-31，以 session.append 为标准补齐逐字段说明、业务案例和源码调用链；运行契约不变 |
| 计划与进度 | [EXTV2-21 / Server PR11](../designs/extensions-v2-development-plan.md#extv2-21-界面与主会话关联解除通知) |
| 主责规约 | [S_15](../specs/S_15_session-detach.md) |

## 1. 范围、状态与依据

PR11 依赖 PR08 的平台关联表，首次共同交付在 PR !95 中，当前合并至 PR !99；两项特性文档分别为 F_25 和 F_26。目标分支 rsi_native_agent_os 基线为 7795851916dd5ce1d34b70f5a20d6d30eafc25e3，Core 基线为 ead700d7d18a247968d64de54918cf5d201a207a。实现、回归和真实服务验证已完成，命令、证据与范围见第 6 节。

## 2. 能力说明与 Claude 对照

session.detach 在平台已经移除一个具体视图的旧主会话关联后通知用户。用户用 attachment_id 清理自己为这次关联创建的资源；该通知不结束会话、不删除会话历史、不停止 Agent，也不改变其他视图的关联。新的主会话提交替换旧关联时自动通知；主动离开时调用 Server 的 session.detach 请求，平台核对关联身份后移除并通知。

Claude 固定声明 bf2323b3021c9d3db7a31e1e7d61068c3f704497 / 2.1.277 的 session.detach 使用 surface、clientId 和 detach/end 原因，CORE 回显 clientId。本地复用 PR08 的 channel + view_id / attachment_id，reason 区分 switch/detach。PR10 的会话删除不在本次接入范围，暂不声称支持 Claude 的 end 原因；传输断开和页面隐藏不会推断为真实解除。

| 项 | 本次实现 | 与固定 Claude 定义的关系 |
| --- | --- | --- |
| 事件职责 | 具体视图的旧主会话关联已解除后的通知 | 保留通知扩展用途；具体粒度以本地真实入口为准 |
| 输入 | session_id, surface_id, attachment_id, channel_id, mode, reason, next_session_id | 使用本地平台事实和 frozen dataclass；字段逐项见第 3.2 节，不直接互换类型 |
| 输出与改写 | 默认 CORE 的真实通知回执；全部输入只读 | 不补造目录变更、拒绝或取消已提交业务的能力 |
| 方法能力 | 不新增用户同名 Ops；另有显式 Server session.detach 请求 | 内部触发入口、用户调用与结果消费见第 4 节 |

## 3. 事件契约

### 3.1 触发前后与粒度

触发来源有两个：已确认的新目标提交替换旧关联；或客户端向 Server 发送显式 `session.detach` 请求并通过旧关联身份核验。平台先移除/替换表项，再用旧会话资料发通知；事件名与请求方法名相同，但事件订阅和业务请求路由分别注册。

```text
切换提交替换旧表项                         显式 session.detach 请求
  → 保留旧 input/context                   → Worker 核验 channel/view/session/attachment
  → 提交新关联                             → 移除精确匹配的旧关联
                 ↘                      ↙
                 构造旧关联的 SessionDetachInput
                   → bind_detach_invocation
                   → await bus.trigger(Event("session.detach", input), 旧 context)
                   → USER 按旧 attachment_id 清理 → 原样 next
                   → session_detach_core 确认旧关联已解除 → 真实回执
  切换：再给仍有效的新关联发 attach          显式请求：返回实际 detached 结果
```

粒度是一条旧关联的一次解除；重复显式解除没有新表项可移除时 detached=False，不再发事件。传输断开、页面隐藏、归档、session.end 不自动发本事件；不结束旧会话，也不触发第二次移除。

### 3.2 输入：每个字段表达什么、由谁填写

实际导入位置是 `jiuwenswarm.extensions_v2.event_types.session_detach`，Handler 接收 frozen dataclass `SessionDetachInput`，不是任意 JSON 字典。以下是平台生成后可观察到的形状示意；用户只读，不复制这个构造器直接触发事件。

```python
from jiuwenswarm.extensions_v2.event_types.session_detach import SessionDetachInput

SessionDetachInput(
    session_id='demo-session-a',
    surface_id='view-main',
    attachment_id='0123456789abcdef0123456789abcdef',
    channel_id='web',
    mode='agent.plan',
    reason='switch',
    next_session_id='demo-session-b',
)
```

全部构造字段均必填、没有可省略默认；表中的 None/空串是平台明确提供的值。下表解释怎样读、代表什么、怎样使用；主责契约仍为 [S_15](../specs/S_15_session-detach.md)。

| 字段 | 类型 / 必填与默认 | 由谁填写 / 应填什么 | 代表什么意思 | 作用 / 怎样使用 | 权限与缺失处理 |
| --- | --- | --- | --- | --- | --- |
| session_id | str，必填，无构造默认 | 平台从旧关联填写，如 demo-session-a | 已解除关联的旧主会话 | 清理该旧关联资源；ops.session_context 也仍绑定此旧会话 | 只读，不是切换后新目标 |
| surface_id | str，必填，无构造默认 | 旧关联建立时的 view_id，如 view-main | 离开旧会话的具体视图 | 用于日志核对；单独使用它清理可能误伤该视图的新关联资源 | 只读，不是会话 ID、UI 卡片或连接 ID |
| attachment_id | str，必填，无构造默认 | 旧 attach 提供的原始 32 位十六进制 UUID | 已经解除的那一次旧关联身份 | 用户按此键 pop/关闭旧资源，避免迟到清理删掉新资源 | 只读，新关联不复用该 ID |
| channel_id | str，必填，无构造默认 | 旧关联记录中的渠道，如 web | 本次被解除关联所属渠道 | 结合 surface_id 理解来源，不能指定另一个渠道 | 只读，不是断开的 WebSocket ID |
| mode | str，必填，无构造默认 | 旧关联建立时确认的模式，如 agent.work.normal | 离开旧会话时原有模式 | 用于用户清理日志或筛选，与新目标模式分开 | 只读，不表示新目标将以该模式运行 |
| reason | str，必填，无构造默认 | 平台填写 switch 或 detach | switch：已提交新目标替换旧关联；detach：显式请求移除匹配旧关联 | 按原因选择自己的清理/记录方式 | 只读，无 end 值；传输断开、隐藏不自行产生此原因 |
| next_session_id | str 或 None，必填，无构造默认 | switch 到已跟踪主会话时为新 ID；主动解除或转入未跟踪范围为 None | 若可知，表示这个视图随后进入哪个主会话 | 只作切换关联说明；要查新会话资料应等新 attach 的可信上下文 | 只读，不能传给当前 Ops 以改变其绑定目标 |

#### 本次 Handler 的会话上下文与包身份

Event 输入是可观察的通知内容，SessionContext 是平台另行提供的可信会话资料；两者不是用户填写后再相互组装的关系。平台通知函数构造 context，Bus 为每个实际 Handler 创建绑定该资料的 Ops。

| 字段 | 类型 / 必填与默认 | 由谁填写 / 应填什么 | 代表什么意思 | 作用 / 怎样使用 | 权限与缺失处理 |
| --- | --- | --- | --- | --- | --- |
| ops.session_context | Core 只读属性，不用 await | 平台先构造，随 trigger 的独立 context 参数传入；Core 绑定给本次 Handler | 本次调用使用哪份会话资料 | 用户读取已有资料；它不负责采集或组装上下文 | 不能通过改 Event 字段替换，不是用户补填对象 |
| session_context.session_id | 有效非空 str | 平台提供已经解除的旧关联会话 ID；与事件输入 session_id 一致 | 可信查询目标是已经解除的旧关联会话 | 既有会话 Ops 根据它读取对应平台服务 | 只读；不得拿 next_session_id/previous_session_id 代替当前绑定 |
| session_context.cwd | str 或 None | start 使用已绑定运行目录；其他三种通知未确认 cwd，保持 None | 可得的真实执行目录观察值 | 已有 cwd 查询可以使用；None 表示该入口未收集到 | 不以进程默认目录或项目根伪造 cwd |
| session_context.metadata.project_root | str 或 None | start 使用已绑定项目根；其余通知只接受目标保存的绝对 project_dir | 已确认的用户项目根资料 | 已有 root 查询读取这个字段；不是自动推断的 Git 根 | 不可信/缺失时为 None；不为通知创建项目或模型 |
| ops.package_metadata | Core 绑定的当前 Handler 包身份 | 来自已注册 Handler 的真实包元信息；CORE 没有用户包身份 | 当前正在执行的 USER 属于哪个扩展包 | 管理自己包的资源和日志；与被观察会话是两个维度 | 用户不从 payload 指定或冒充其他包 |

本入口不主动采集运行模型；model 查询沿用已有保存信息读取规则，不为展示创建或激活模型。 本 PR 沿用已合入的只读 Ops；七项查询事件化由独立 PR !100 提供，不是本生命周期特性的依赖。

### 3.3 输出：真实通知回执、继续与失败

默认 CORE 返回 `SessionDetachOutput`。正常用户 Handler 必须返回 `await next_handler(event)` 得到的真实回执内容；本通知不使用查询事件的 value/deny 封装，不返回 `{"accepted": True}` 字典，也没有用户拒绝/改写平台事实的协议。

下面仅展示回执形状，**不让用户自行构造并返回假成功**；字段由 CORE 填写：

```python
from jiuwenswarm.extensions_v2.event_types.session_detach import SessionDetachOutput

SessionDetachOutput(
    accepted=True,
    session_id='demo-session-a',
    surface_id='view-main',
    attachment_id='0123456789abcdef0123456789abcdef',
)
```

| 字段 | 类型 / 必填与默认 | 由谁填写 / 应填什么 | 代表什么意思 | 作用 / 怎样使用 | 权限与缺失处理 |
| --- | --- | --- | --- | --- | --- |
| accepted | bool，CORE 实际填严格 True | CORE 在有效解除通知作用域中生成 | 旧 attachment 已不再是平台当前关联 | 确认平台解除事实；不代表所有 USER 外部清理完成 | 原样返回，不是恢复旧关联或结束会话的命令 |
| session_id | str，必有 | CORE 回显旧关联的 session_id | 被解除关联原来指向的主会话 | 核对旧资源归属 | 只读，不是新会话 ID |
| surface_id | str，必有 | CORE 回显旧关联的视图 | 发生解除的具体视图 | 用于日志，不单凭它删除所有同视图资源 | 只读，不改变新目标的关联 |
| attachment_id | str，必有 | CORE 回显已解除的旧 UUID | 本次应清理的旧关联身份 | 与用户资源键核对，精确清理一次旧关联 | 只读，新 attachment 仍由独立 attach 管理 |

所有输入只读、没有可改写字段。空 matcher 匹配本次候选中的该事件；可用第 3.2 节字段等值筛选，字段不匹配不会替用户延后重放。 正常 next 继续当前通知链；提前短路伪造成功、修改输入、后置改写回执均不构成合法平台操作，按既有 Core USER 故障隔离处理。

CORE 在当前可信调用作用域内核对原始输入和绑定会话，再记录真实回执；同次调用重复 next 复用该回执，不重复业务操作。用户抛普通异常可能被 Core 隔离后继续默认处理；不能把抛错当作取消平台业务的机制。

通知失败或超时不会恢复已经提交的关联事实；原调用取消仍传播，不把已发生的变更伪装成未发生。 单次通知等待预算默认 2 秒，从通知任务启动计；超时使作用域失效并请求取消，忽略取消的任务可能晚退出，但不能再产生迟到有效 CORE 回执。包在途计数直到任务退出才释放。 accepted 只说明默认通知被确认，用户自有资源操作是否成功应由自己的代码记录；外部副作用不自动补偿。 本事件不是跨重启的 exactly-once。

### 3.4 注册、筛选与用户接入示例

以下两文件放在 ZIP 根目录，用现有扩展管理入口安装。空匹配模式接收全部；需要筛选时可使用 channel_id、reason 等字段的等值模式。资源按 attachment_id 保存、清理，用户最后原样返回默认回执。

```yaml
schemaVersion: 1
package:
  id: example.session-detach
  version: 1.0.0
  displayName: Session Detach Example
extensions:
  - id: view-resources
    runtime: backend
    module: extension.py
```

```python
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Own resources by association identity, allowing independent view cleanup."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_detach")


class SessionDetachExample:
    """Keep only this package's per-association presentation resources."""

    def __init__(self):
        self._resources = {}

    def register(self, registrar):
        registrar.register_event_handler("session.attach", self.on_attach, {}, "user")
        registrar.register_event_handler("session.detach", self.on_detach, {}, "user")

    async def on_attach(self, event, ops, next_handler):
        receipt = await next_handler(event)
        self._resources[receipt.attachment_id] = {"session_id": ops.session_context.session_id}
        return receipt

    async def on_detach(self, event, ops, next_handler):
        value = event.event_input
        self._resources.pop(value.attachment_id, None)
        receipt = await next_handler(event)
        logger.info("session detach example: session=%s surface=%s attachment=%s reason=%s accepted=%s",
                    ops.session_context.session_id, value.surface_id, value.attachment_id, value.reason, receipt.accepted)
        return receipt


def create_extension():
    return SessionDetachExample()
```

输入中的 session_id 是离开的旧会话，surface_id 是离开的视图，attachment_id 是这次旧关联的身份；reason= switch 时 next_session_id 才可能指出新目标。CORE 回执的 accepted=True 只确认旧关联已解除，不是全部用户展示已完成。完整字段的默认、来源和填写责任以 S_15 第 1 节为准。

## 4. 方法契约

### 4.1 Server 业务请求与用户调用场合

不新增 Ops。新增 Server 业务请求 session.detach，详细请求字段及回执见 S_15 第 2 节。这是跨进程业务入口，进入 Worker 后才触发同名事件，两者不是同一个注册过程。客户端需要使用对应 attach 通知的 session_id、surface_id 和 attachment_id；平台从关联表取得旧上下文。现有 Web 的创建/切换已自动触发替换通知；主动关闭接入者应显式发送请求，本次不修改前端页面自动关闭行为。用户需要把关联身份送到自己的界面时，可在 on_attach 中使用自己的渲染链携带这些业务参数；这不需要改平台业务方法。

例如接入者发送以下原协议请求。示例 ID 必须替换为这次 attach 的真实值；request_id 每次生成不同 UUID。请求返回 detached=True 表示实际移除，重复请求为 False，和事件回执 accepted 的含义分别见 S_15。

```json
{
  "request_id": "client-generated-unique-uuid",
  "channel_id": "web",
  "req_method": "session.detach",
  "params": {
    "session_id": "demo-session-a",
    "view_id": "view-main",
    "attachment_id": "0123456789abcdef0123456789abcdef"
  }
}
```

这不是 `ops.session.detach()`，本次没有提供这个 Ops。平台自己的通知函数也不作为用户 API 发布。

### 4.2 请求与响应字段：填什么、如何定位旧关联

接入者先从自己的 attach Handler 取得关联身份，再通过原 Server 协议发请求。信封字段和 params 分开；不是向 `ops.session_context` 填资料。

| 字段 | 类型 / 必填与默认 | 由谁填写 / 应填什么 | 代表什么意思 | 作用 / 怎样使用 | 权限与缺失处理 |
| --- | --- | --- | --- | --- | --- |
| request_id | str，请求信封必填 | 客户端每次请求使用不同 UUID | 本条业务请求的身份，不是 attachment_id | 供既有请求响应关联与并发去重 | 重试生成新请求 ID；目标关联身份保持原值 |
| channel_id | str，请求信封字段 | 沿用当前已路由渠道，如 web；不在 params 中另加 channel_id | 这次请求所属渠道 | 与 view_id 一起定位平台关联 | 不能由 params 覆盖另一个渠道 |
| req_method | 固定 str | 填写 session.detach | 选择 Server 的业务方法 | 经原路由到 Worker 后才触发通知事件 | 不是用户直接调用 EventBus 的能力 |
| params.session_id | str，必填，无默认 | 填写旧 attach 输入中的 session_id | 要离开的旧主会话 | 与关联表当前身份核对 | 缺失/格式错报 BAD_REQUEST，目标不匹配报 STALE_ATTACHMENT |
| params.view_id | str，必填，无默认 | 填写旧 attach 输入的 surface_id，如 view-main | 要解除哪个具体视图的关联 | 1～128 个 ASCII 字母/数字或 ._:-；不会移除同会话其他视图 | 不填写 UI 卡片 ID 或连接 ID |
| params.attachment_id | str，必填，无默认 | 原样填写旧 attach 的 32 位小写十六进制 UUID | 精确选定这次旧关联 | 防止旧请求删除同视图已经建立的新关联 | 不能生成新 UUID 代替；不匹配保留新关联并报 STALE_ATTACHMENT |

| 字段 | 类型 / 必填与默认 | 由谁填写 / 应填什么 | 代表什么意思 | 作用 / 怎样使用 | 权限与缺失处理 |
| --- | --- | --- | --- | --- | --- |
| payload.session_id | str，成功响应必有 | 平台回显请求目标 | 本次请求指定的旧会话 | 只作核对，不改变 Ops 上下文 | 不是事件已执行的单独证明 |
| payload.surface_id | str，成功响应必有 | 平台回显请求 view_id | 被请求解除的具体视图 | 注意请求名 view_id 与输出名 surface_id 的对应 | 不是整个会话被删除 |
| payload.attachment_id | str，成功响应必有 | 平台回显旧关联 ID | 请求核验的旧关联身份 | 与用户保存的旧资源键对应 | 不用于确认另一个新关联 |
| payload.detached | bool，成功响应必有 | 实际移除填 True；当前没有关联填 False | 该请求有没有移除一项平台关联 | True 触发一次通知；False 不重复通知 | 不代表全部 USER 外部清理成功；与事件 accepted 区分 |

Worker 关联服务未就绪返回 NOT_READY；格式非法 BAD_REQUEST；视图已被其他关联占用返回 STALE_ATTACHMENT。失败不移除新关联。一个成功的 detached=True 响应与事件 accepted 的含义不同：前者是实际移除事实，后者是通知默认 CORE 的确认；无匹配关联也可以成功返回 detached=False。

## 5. 接入与兼容方案

### 5.1 两阶段完整调用链

```text
Worker 启动 → 注册 attach / detach 类型和默认 CORE → 共享 Ops Build → 用户注册两个 USER
创建/切换提交 → 确认新目标 → 平台提交替换关联
  → 若存在旧关联：detach(旧 session/context) → USER 清理旧 attachment → CORE 确认已解除
  → attach(新 session/context) → USER 初始化 → CORE 确认当前关联
显式 session.detach 请求 → 原 Front/Supervisor 路由 → Worker 核对 channel/view/session/attachment
  → 移除该关联 → detach USER → 默认 CORE → 返回原请求的实际移除结果
```

### 5.2 目录、生命周期与取舍

关键决策：以平台实际关联表为准，不采用请求自报的 previous_session_id；先提交事实再通知，避免用户异常回滚已生效的切换。单纯 foreground visible=False 也可能表示隐藏，不足以解除；转入未跟踪范围时只清理旧主会话关联，不对该范围新增扩展。

替代方案：不把 Gateway WebSocket 断开当作视图关闭，因为同连接可能承载多个视图，稳定 view_id 重连也不应反复 attach/detach。不让默认 CORE 再次移除关联，避免重复执行和用户拦截改变已提交事实。通知失败不自动重试；内存表不跨 Worker 重启持久保存。

### 5.3 完整业务案例：旧关联慢清理不能删除新关联资源

用户扩展在 attach 时按 attachment_id 保存自己的视图资源。view-main 原来关联 A，旧 ID 为 0123456789abcdef0123456789abcdef；切到 B 后平台生成新的 ID，例如 fedcba9876543210fedcba9876543210。

1. 原切换提交确认 B 后，平台先把关联表更新为 B，同时保留 A 的原上下文供旧通知使用。
2. 平台构造 session.detach：session_id=A、attachment_id=旧 ID、reason=switch、next_session_id=B；本次 Ops 仍绑定 A。
3. 用户 on_detach 只 pop 旧 attachment_id 对应的资源，再 next。不能简单清空 view-main 全部资源，否则延迟清理可能误伤 B。
4. session_detach_core 确认调用仍有效、输入保持原样、旧 ID 已不是当前关联且上下文属于 A，返回旧身份的真实回执；它不会第二次移除关联。
5. 旧 detach 结束后，平台只对仍有效的新 B 关联发 attach，由另一个 Handler 为新 ID 准备资源。A 的会话历史和 Agent 并没有被结束。
6. 主动离开时，接入者按第 4 节用 attach 的真实 session_id/surface_id/attachment_id 发 Server 请求；平台核验并移除后走同一通知链。重复请求 detached=False，不再通知。

可观察结果是旧资源消失、新 ID 的资源保留；请求 detached 表示实际移除，事件 accepted 只确认解除通知。页面隐藏、传输断开、session.end 不在本事件自动接线范围。

### 5.4 关键函数：平台触发、Ops 绑定与默认 CORE

以下为当前平台派发和默认 CORE 的源码节选；包在途计数、超时、去重等外层编排保留在 [平台通知模块](../../../server/runtime/session/detach_notifications.py)，不是让用户直接调用内部函数。event_types 只维护定义、校验作用域、Matcher 和注册。

```python
async def _dispatch(
    bus: ExtensionEventBus,
    manager: ExtensionManager,
    state: DetachInvocation,
    context: SessionContext,
) -> None:
    runtime = manager.registry.runtime
    packages = tuple(record.package_id for record in runtime.active_packages())
    runtime.begin_calls(packages)
    try:
        with bind_detach_invocation(state):
            result = await bus.trigger(Event(SESSION_DETACH_EVENT, state.original), context)
            if not state.is_released() or state.receipt is None or result != state.receipt:
                raise RuntimeError("session.detach requires the actual CORE release receipt")
    finally:
        runtime.end_calls(packages)
```

Event 是通知输入，独立 context 参数是可信会话资料。Core 为每个实际进入的 USER / CORE 调用 `builder.build(package_metadata, session_context)`，得到本次 Ops；USER 从现有 `ops.session_context` 只读属性读取，不通过这个属性采集资料。启动时的 ops.build BUILTIN 已经登记了共享方法，本次事件不再创建 Builder 或临时登记能力。

```python
async def session_detach_core(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> SessionDetachOutput:
    """Return a real receipt for the old identity, never release it again."""
    del next_handler
    state = current_detach_invocation()
    if not valid_detach_input(event.event_input):
        raise ValueError("session.detach input cannot be changed")
    context = ops.session_context if ops is not None else None
    if context is None or context.session_id != state.original.session_id:
        raise RuntimeError("session.detach requires the old bound SessionContext")
    if state.receipt is None:
        value = state.original
        state.receipt = SessionDetachOutput(True, value.session_id, value.surface_id, value.attachment_id)
    return state.receipt
```

current_detach_invocation 要求已提交解除且调用仍有效；valid_detach_input 保留旧身份；Ops 必须绑定旧 session_id。CORE 保存并返回旧身份回执，不再次移除表项、不终止会话，也不把新目标绑定进来。

本事件没有新增业务 BUILTIN；实际通知链是匹配 USER → 默认 CORE，结果反向返回。若有多个匹配 USER，各自原样 next 才继续；已有 ops.build 的 BUILTIN 只属于启动能力登记，不能混同为本次通知的业务处理层。

类型与校验见 [session_detach.py](../../event_types/session_detach.py)，CORE 见 [event_handlers/session_detach.py](../../event_handlers/session_detach.py)。


## 6. 验收与未决问题

本次 2026-10-31 仅按 F_16 的写法补齐输入/输出字段、注册示例、真实触发条件、Handler 职责和完整业务案例；运行代码与契约不变。文档核对包括 dataclass 字段与示例值、源码节选、Python/YAML/JSON 示例及相对链接。下列回归和真实服务结果均保留原运行日期与范围，不宣称本次重新执行业务或模型验收。

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原 attach/detach 合并交付为 Server !95；本次保留按 channel/view/attachment 身份解除的契约及旧 detach → 新 attach 顺序，增加组合观察者的回归，不把永久删除、断线或隐藏自动解释为显式 detach。

2026-10-29，Windows / Python 3.11.15，Swarm 使用第 1 节固定基线和 PR !95 的 attach/detach 联合变更，Core 为 ead700d7d18a247968d64de54918cf5d201a207a。446 项扩展、原创建/切换、Runtime、生命周期、E2A 编解码、Gateway 代理及 Supervisor 转发回归通过，无失败、无跳过；一项外部 Authlib 弃用提示。此前首轮 48 项定向通过，已包含在回归范围中，不重复累计。未运行格式化或静态检查工具。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/runtime/test_session_attach_provisioner.py tests/unit_tests/runtime/test_session_create_provisioner.py tests/unit_tests/runtime/test_session_switch_provisioner.py tests/unit_tests/runtime/test_session_provision_contract.py tests/unit_tests/runtime/test_session_lifecycle_participants.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_runtime_architecture.py tests/unit_tests/e2a/test_wire_codec.py tests/unit_tests/gateway/test_e2a_proxy.py tests/unit_tests/agentserver/test_supervisor_forwarding.py --asyncio-mode=auto -o log_cli=false
```

| 场景 | 操作与预期 | 已通过的证据 |
| --- | --- | --- |
| 两视图解除一个 | A 的两个视图只移除指定旧 attachment，另一个资源与关联保留 | 真实 USER 包、资源字典、显式请求及单测 |
| 真实切换提交 | prepare/abort 不通知；commit 后 detach 旧会话，再 attach 新目标；重复 commit 不重放 | test_session_attach_provisioner；真实 Front/Supervisor/子 Worker |
| 身份、重复与并发 | 旧 token 不删新关联；同视图重新进入生成新 ID；两个并发解除只一个 True | 真实 STALE_ATTACHMENT/BAD_REQUEST、并发 True/False 与单位测试 |
| 超时、取消和伪造 | 解除事实保留，迟到 CORE 无效，在途计数最终退出，所有输入不可改写 | 真实 Registry 用户包参数化与竞态测试 |
| 原会话不被结束 | 显式解除前后会话文件哈希相同；随后原会话可继续真实聊天 | 真实文件比较与一轮有效用量 |
| 归档与断线 | 归档不隐式解除；已有关联仍可显式清理；稳定 view_id 的重连不发 detach | 真实归档、清理与重连记录 |
| 卸载与来源 | 卸载 USER 后默认解除/切换继续；只用实际平台上下文和 Ops | 真实 ZIP 安装/卸载、源码哈希、子 Worker 进程与包身份 |

真实证据目录：`D:\work_space\.codex-temp\server-session-detach-event-20261029\real-202911`。最终成功运行记录 6 次 attach、5 次 detach；切换通知中旧 session/context/项目根正确，显式解除的旧关联清理不会删除新资源。没有任何用量时已经能通知和读取 root，未手工构造 SessionContext，未 mock 模型/Ops/Adapter。现成用户示例记录 5 次 accepted=True；另有 1 轮显式 Qwen3.8-Flash 业务回合及正用量证明解除不结束主会话。result.json、wire.jsonl、user-handler-results.jsonl、source-before/source-after 保存实际来源与前后一致性；cleanup.json 确认 Worker 退出、端口关闭。

实际接线发现原通用归档执行屏障会先拦截清理请求，现将 session.detach 作为仅清理已有匹配关联的请求交给自身身份核验，归档不会重新执行、恢复或写历史。验收客户端并发请求改用 UUID 避免时间粒度造成的请求编号复用；此前失败运行不作为通过证据，未因该测试客户端问题改动产品路由。

验证边界：真实链从 Front 开始，未验证产品浏览器视觉或真实 Gateway Web 连接；Gateway 此次只补已有 Web 转发表登记并运行代理回归。无非 Windows 实机、远端 CI 或跨重启恢复证据。用户的页面关闭自动请求接入、surfaces Ops、PR10 的结束联动仍未包含；通知异常不恢复已提交事实，旧资源清理不承诺外部副作用回滚。2 秒是每个通知的等待预算，一次切换含 detach 和 attach 两次通知，不代表整个切换只增加至多 2 秒。


## 7. 参考资料

- [F_25 / attach](F_25_session-attach.md)、[S_14](../specs/S_14_session-attach.md)、[S_15](../specs/S_15_session-detach.md)。
- [Claude 固定声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。

- [事件定义](../../event_types/session_detach.py)、[默认 CORE](../../event_handlers/session_detach.py)、[平台通知](../../../server/runtime/session/detach_notifications.py)、[关联表与实际移除](../../../server/runtime/session/attach_notifications.py)、[用户示例](../../examples/session_detach/README.md)。
