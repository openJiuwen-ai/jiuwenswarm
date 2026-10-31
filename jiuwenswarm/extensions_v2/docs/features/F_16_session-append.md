# session.append / ops.session.append 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_16 / session-append |
| 归档日期 | 2026-10-27 |
| 后续修订 | 2026-10-29，完整消息、door/origin、行级 uuid；历史查询已拆至 F_18；2026-10-30，合并目标分支并验证历史与统计 Ops 共存；2026-10-31，按事件名对齐文件名与引用，契约与字段不变 |
| 关联计划 | [EXTV2-16 / Server PR-04](../designs/extensions-v2-development-plan.md#extv2-16-会话历史读写与追加前扩展) |
| 主责规约 | [S_06](../specs/S_06_session-history-ops.md) |

## 1. 范围、状态与依据

本文只说明追加前事件和追加方法。历史消息查询 `ops.session.messages` 的事件、返回数组字段、错误和独立只读用户示例已拆至 [F_18](F_18_session-messages.md)，不再与写入契约合写；两份文档仍属于同一个 PR04 / EXTV2-16，不新增开发任务。

本特性在 [PR !83](https://gitcode.com/huenrui_/jiuwenswarm/pull/83) 内升级事件契约，原编号不变。实现位置为 Worker；Front/Supervisor 沿用既有路由，agent-core 不修改。依赖既有 Core EventBus、Registry、Ops Builder 和 USER 故障隔离，验证使用 Core `ead700d7d18a247968d64de54918cf5d201a207a`。

覆盖主会话 `run_history_io(append_history_record, **kwargs)`、`history.append_record` RPC、扩展 append Ops。已有流式产品记录转换为公开消息视图；平台也可以在该异步入口的 content 参数传入原生 BaseMessage，此时保存原生消息快照并在写入前进入同一事件链。同步后台记录、Team/子 Agent 专用记录仍走原路径。

这不是 agent-core 所有存储点的全局拦截。不会额外启动 Agent、发送输入或自动渲染界面；不会把历史改写自动同步到已经使用的活跃模型上下文。恢复仍由原服务消费产品记录，不承诺新增多模态恢复协议。

对照官方 `anthropics/claude-code` 固定提交 `bf2323b3021c9d3db7a31e1e7d61068c3f704497` 的 `mods/types/claude-code.d.ts`（文件头 2.1.277），不以当前最新版作兼容保证。2026-10-28 的字符串事件为旧草稿契约；本次由以下 message/door/origin/uuid 契约替代。

## 2. 能力说明与 Claude 对照

`session.append` 是一条主会话历史记录保存前的扩展点。平台先确定会话、待存消息、行身份与来源，再触发事件。USER 可以查看完整公开消息结构、按来源筛选并改写允许的文本；默认 CORE 校验并实际提交。仅扩展自己发起的 note 可显式拒绝。一次处理一条通过原前置过滤的记录尝试，重试也进入事件链，提交层负责去重。

`await ops.session.append(note)` 为扩展追加 transcript_only 留档消息，使用绑定会话和真实包身份，另起 session.append 事件链，返回实际提交、重复或拒绝的回执。需要读回历史时，使用 [F_18 的独立查询方法](F_18_session-messages.md)；读取不要求先执行追加。

| 项 | 本次实现 | 与固定 Claude 定义的关系 |
| --- | --- | --- |
| 事件输入 | message、door、origin、uuid | 对齐消息/进入环节/产生者/行身份的分工；不是直接复用 TypeScript 对象 |
| 消息内容 | 原生消息公开视图，包含文本/媒体块、工具调用及关联 ID | 本地 tool 为独立 type/role，不硬套 Claude 的 user-role tool_result 块 |
| 改写 | 只允许原位置文本块的 text；其余字段固定 | 首版比 Claude 收窄：不新增、删除、移动媒体或内容块，不改 tool_result 错误标记 |
| 来源 | kind/name 表示产生者，entry 表示平台入口 | entry 是本地附加维度；未知名字为 None，不伪造具体模型或工具 |
| 返回 | message、uuid、history_record，以及 status/session_id/message_id/reason | 保留本地幂等与历史查询能力；不能直接粘贴 Claude 返回对象 |
| 方法输入 | 稳定 id + message；兼容旧 id/role/content | 稳定 id 为本地重试要求；无 agentId，不扩大到子 Agent |
| 生效位置 | 产品历史及相应呈现字段 | 不承诺改写当前模型已使用的消息 |

## 3. 事件契约

### 3.1 触发前后与粒度

```text
主会话平台异步写入 / history.append_record RPC / 用户 append Ops
  → 原记录构建、临时记录过滤、会话生命周期预检
  → 平台适配：产品事件记录或 BaseMessage → SessionAppendMessage
  → 固定 SessionContext、door、origin、行级 uuid；私有事实留在调用封装
  → history_events.dispatch_session_append
  → await Worker bus.trigger(Event("session.append", input), context)
  → USER Handler：只改允许的文本或拒绝自己的 note
  → await next_handler(Event(event.event_type, rewritten))
  → 剩余 Handler → session_append_core
  → 再校验固定事实、合并允许内容、实际提交并记录真实回执
  → 沿链返回；平台入口或 append Ops 消费回执
```

这里的“写入前”是本次记录提交前；并不代表模型执行前。产品历史可能含模型回复、工具结果及流式事件，同一回合可触发多次。过滤掉的临时/空记录不触发，查询历史不触发。

### 3.2 输入：每个字段表达什么、由谁填写

#### 输入相关定义与实际派发函数

输入的实际字段定义如下；这里的 message、door、origin、uuid 与下表同名，不是 Ops 入参 note 的原始字典。

```python
@dataclass(frozen=True, slots=True)
class SessionAppendInput:
    """One pending row, its semantic door, producer and final row identity."""

    message: SessionAppendMessage
    door: str
    origin: SessionAppendOrigin
    uuid: str
```

下面的实际平台函数确定行 UUID、来源和公开消息后，构造 original=SessionAppendInput，再作为 Event 的输入触发处理链。context 独立传入，prepared/原始指纹等私有提交事实保留在 AppendInvocation。

```python
async def dispatch_session_append(
    bus: ExtensionEventBus, context: SessionContext, prepared: PreparedHistoryRecord,
    *, message_id: str, source: str, append_key: str, fingerprint: str, update_metadata: bool,
    native_message: SessionAppendMessage | None = None,
) -> SessionAppendResult:
    """Await the policy chain and one real commit with trusted session facts."""
    if append_invocation() is not None:
        raise SessionHistoryError("APPEND_REENTRANT")
    if context.session_id != prepared.session_id:
        raise SessionHistoryError("INVALID_SESSION")
    generation = await asyncio.to_thread(preflight_append, prepared.session_id, require_existing=source == "extension")
    row_uuid = prepared.record["id"] if source == "extension" else "row:" + hashlib.sha256(append_key.encode("utf-8")).hexdigest()
    prepared = replace(prepared, record={**prepared.record, "uuid": row_uuid}, metadata={**prepared.metadata, "append_generation": generation})
    model_info = (context.metadata or {}).get("session_model_info")
    model_name = model_info.get("model_name") if isinstance(model_info, Mapping) else None
    door, origin = classify_record(prepared.record, source, model_name)
    message = native_message if native_message is not None else message_from_record(prepared.record, prefer_stored=source == "extension")
    original = SessionAppendInput(copy.deepcopy(message), door, origin, row_uuid)
    state = AppendInvocation(copy.deepcopy(prepared), copy.deepcopy(original), message_id, append_key, fingerprint, update_metadata)
    token = _INVOCATION.set(state)
    try:
        result = await bus.trigger(Event(SESSION_APPEND_EVENT, copy.deepcopy(original)), context)
        if state.receipt is not None:
            return copy.deepcopy(state.receipt)
        if isinstance(result, SessionAppendRejection) and origin.kind == "extension" and door == "note":
            return SessionAppendResult("rejected", prepared.session_id, message_id, reason=result.reason.strip())
        raise SessionHistoryError("APPEND_RESULT_INVALID")
    finally:
        _INVOCATION.reset(token)
```

| 字段位置 | 在哪段代码形成或读取 | 沿链传到哪里 |
| --- | --- | --- |
| message | native_message 或 message_from_record(prepared.record, ...)，深拷贝后放入 original.message | USER 从 event.event_input.message 读取，并只改允许的文本 |
| door / origin | classify_record(prepared.record, source, model_name) 的两个返回值 | 原记录来源决定，不由可改写正文决定 |
| uuid | row_uuid：扩展沿用 ext 身份，平台行由 append_key 摘要生成 | 同时写入待提交记录及事件输入，不能由用户替换目标行 |
| SessionContext | 函数参数 context，且必须与 prepared.session_id 一致 | 作为 bus.trigger 的独立参数，不是 Event 内的可改写字段 |

实际导入位置：`jiuwenswarm.extensions_v2.event_types.session_history`。Handler 接收 dataclass 对象，不接收下面示意 JSON 的任意变体。

```python
SessionAppendInput(
    message=SessionAppendMessage(
        type="tool", role="tool", name="lookup", tool_call_id="call-42",
        content=[{"type": "text", "text": "工具返回的待保存正文"}],
    ),
    door="tool-result",
    origin=SessionAppendOrigin(kind="tool", name="lookup", entry="history"),
    uuid="row:<平台生成的摘要>",
)
```

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| message | SessionAppendMessage，必有 | 这一条待存消息的公开视图，不是另一会话的消息，不含全部私有元数据 | **平台填写**；用户通过 `event.event_input.message` 读取，改写时用 replace 保留其他字段 | 仅下表允许的文本可改 |
| door | str，必有 | 这条记录经哪个业务环节进入历史 | **平台填写**；prompt / response / tool-call / tool-result / attachment / notice / note | 只读，可用于 matcher |
| origin | SessionAppendOrigin，必有 | 谁产生记录，以及由哪个平台入口提交 | **平台填写**；例如 tool、lookup、history | 只读，可用点路径匹配 |
| uuid | str，必有 | 此次待存行的稳定标识；新记录实际写入 JSONL 的 uuid 字段 | **平台生成**；普通平台记录为 row:摘要，扩展记录沿用其 ext:摘要身份 | 只读，不允许自填目标身份 |

**三个 ID 的区别：**方法参数 id 是用户给同一留档动作的稳定重试键；返回 message_id 回显该键；事件/回执 uuid 是平台行身份。产品记录原有 id 可能是 request_id:role 的分组标识，同一请求的多条事件可复用它，因此不能当行级 uuid。新平台行独立生成 uuid；旧记录重试回执可回退到旧 id，不能据此宣称历史旧行已有唯一 uuid。

#### message 的全部字段

消息对象的实际字段定义（默认值可以直接与表格对应）：

```python
@dataclass(frozen=True, slots=True)
class SessionAppendMessage:
    """A detached message view; only existing text blocks can be rewritten."""

    type: str
    content: list[dict[str, Any]]
    role: str | None = None
    name: str | None = None
    is_meta: bool = False
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return independent JSON-compatible message fields."""
        return copy.deepcopy(asdict(self))
```

平台原生 BaseMessage 的字段由这个实际适配函数读取；产品历史记录则由 dispatch 中的 message_from_record 适配。

```python
def message_from_base_message(message: Any) -> SessionAppendMessage:
    """Adapt a native BaseMessage; private metadata stays with the platform.

    This converter does not install another persistence or model-input hook.
    """
    from openjiuwen.core.foundation.llm import BaseMessage

    if not isinstance(message, BaseMessage):
        raise ValueError("A native BaseMessage is required")
    role = message.role
    if role not in ("user", "assistant", "tool", "system"):
        raise ValueError("Unsupported native message role")
    calls = getattr(message, "tool_calls", None)
    return SessionAppendMessage(
        type=role, content=content_blocks(message.content), role=role, name=message.name,
        tool_call_id=getattr(message, "tool_call_id", None),
        tool_calls=[call.model_dump(mode="json") if hasattr(call, "model_dump") else copy.deepcopy(call) for call in calls] if calls is not None else None,
    )
```

| 字段 | 类型 / 默认值 | 含义、实际取值和填写方式 | 可修改范围 |
| --- | --- | --- | --- |
| type | str，必有 | 消息种类：user 用户消息、assistant 助手消息、tool 工具结果、attachment 附件、system 通知。平台从原生消息或产品 event_type 适配 | 只读 |
| content | list[dict]，必有 | 按原顺序排列的公开内容块。纯字符串规范化为 `[{'type':'text','text':'正文'}]`；媒体、工具或未知块保留其 JSON 结构 | 只改已有 text 块的 text；不改数量/顺序 |
| role | str 或 None，构造器默认 None | 消息的原生角色，通常与 type 相同；attachment 可为 None。工具结果为 tool，不冒充 user/assistant。由平台填写 | 只读 |
| name | str 或 None，默认 None | 可用的消息/工具名称，或通知/附件的子类型。没有可信名称则 None；如 lookup、system.notice | 只读 |
| is_meta | bool，默认 False | 公开消息是否属于扩展等附加记录；扩展 note 由平台固定为 True。**不表示是否允许访问元数据** | 只读 |
| tool_call_id | str 或 None，默认 None | 工具结果对应的原调用 ID；非工具消息通常为 None。来自原生 ToolMessage 或平台记录 | 只读，不能换到另一个工具调用 |
| tool_calls | list[dict] 或 None，默认 None | 助手已声明的工具调用，含 ID、名称及参数等原始公开结构；不同原生生产者的字典字段可不同，整体保留 | 整体只读，不能改参数/ID |

text 块必须是 `{'type':'text','text': str, ...原附加字段}`；用户只填写新的 text 字符串。原 text 非空时不能把整条文本改成全空白；其他键保持。原有 image/image_url、tool_use、厂商未知块等按 JSON 整体保留，不能伪造新增或改写。总 JSON UTF-8 大小不超过 max(1 MiB, 原消息 JSON 大小)，不截断。

原始工具 result、时间、会话路由、父级关系、原生 metadata 和其他宿主字段留在平台记录中。改写公开工具结果会同步历史 rendered_result；**不承诺删除原始 result 中的敏感内容**。原生 BaseMessage 的 native_message 快照只合并允许的 content，metadata、工具 ID/调用保持。

#### origin 的全部字段

来源对象的实际定义及生成函数：

```python
@dataclass(frozen=True, slots=True)
class SessionAppendOrigin:
    """Host-pinned producer identity and actual persistence entrance."""

    kind: str
    name: str | None
    entry: str
```

```python
def classify_record(record: Mapping[str, Any], source: str, model_name: str | None = None) -> tuple[str, SessionAppendOrigin]:
    """Separate the row's semantic door from producer and adapter entrance."""
    if source == "extension":
        return "note", SessionAppendOrigin("extension", record.get("extension_package_id"), "ops.session.append")
    event_type = record.get("event_type", "")
    if event_type == "chat.tool_result":
        door, kind, name = "tool-result", "tool", record.get("tool_name") or record.get("name")
    elif event_type == "chat.tool_call":
        door, kind, name = "tool-call", "model", model_name
    elif event_type in ("chat.attachment", "attachment"):
        door, kind, name = "attachment", "platform", None
    elif event_type in ("context.compact_boundary", "chat.notice", "system.notice"):
        door, kind, name = "notice", "platform", None
    elif record.get("role") == "user":
        door, kind, name = "prompt", "user", None
    else:
        door, kind, name = "response", "model", model_name
    if source == "history.append_record":
        kind, name = "platform", "history.append_record"
    return door, SessionAppendOrigin(kind, name, source)
```

classify_record 的第一个返回值是 door，第二个返回值是 SessionAppendOrigin；source 是平台入口名，真实包身份取自记录中的 extension_package_id。

| 字段 | 类型 / 是否必有 | 含义与应该填写什么 |
| --- | --- | --- |
| kind | str，必有 | 平台判定的产生者种类：user / model / tool / extension / platform；用户 Handler 不填写或更改 |
| name | str 或 None，必有 | 有可信信息时的模型名、工具名或真实包 ID；未知为 None。extension 为 Core 绑定包身份，平台 RPC 为 history.append_record；不是用户可冒充的显示名称 |
| entry | str，必有 | 实际入口：history（原平台异步历史）、history.append_record（RPC）、ops.session.append（扩展追加）。例如 door=response 不代表一定由模型产生，RPC 的 origin.kind 为 platform |

door 的本地映射：用户记录→prompt；普通助手记录→response；chat.tool_call→tool-call；chat.tool_result→tool-result；chat.attachment/attachment→attachment；compact boundary、chat.notice/system.notice→notice；扩展 Ops→note。未知助手事件保留 response，事件特有元数据仍在历史记录中，不凭空增设所有 Claude door。

### 3.3 输出：成功、重复与拒绝

#### 输出相关定义、提交回执与字典返回

事件链返回实际 SessionAppendResult；自己的 note 合法拒绝时 USER 可返回 SessionAppendRejection，派发器会转换成 rejected 回执。

```python
@dataclass(frozen=True, slots=True)
class SessionAppendRejection:
    """Explicitly refuse an extension note before calling next_handler."""

    reason: str
```

```python
@dataclass(frozen=True, slots=True)
class SessionAppendResult:
    """An actual commit receipt, or rejection without a committed row."""

    status: str
    session_id: str
    message_id: str
    message: SessionAppendMessage | None = None
    reason: str | None = None
    uuid: str | None = None
    history_record: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a detached receipt without exposing mutable commit state."""
        return {
            "status": self.status, "session_id": self.session_id, "message_id": self.message_id,
            "message": self.message.to_dict() if self.message is not None else None,
            "reason": self.reason, "uuid": self.uuid, "history_record": copy.deepcopy(self.history_record),
        }
```

真实提交及输出字段在默认 CORE 的这些位置产生：

```python
async def session_append_core(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> SessionAppendResult:
    """Persist once per invocation after all earlier handlers have finished."""
    del next_handler
    state = current_append_invocation()
    if ops is None or ops.session_context is None or ops.session_context.session_id != state.prepared.session_id:
        raise SessionHistoryError("INVALID_SESSION")
    if not valid_append_input(event.event_input):
        raise SessionHistoryError("INVALID_MESSAGE")
    if state.receipt is not None:
        return copy.deepcopy(state.receipt)
    prepared = apply_message(state.prepared, event.event_input.message, state.original.message)
    record, duplicate = await run_history_io(
        commit_session_message, prepared, state.append_key, state.fingerprint, state.update_metadata,
    )
    state.receipt = SessionAppendResult(
        "duplicate" if duplicate else "persisted", prepared.session_id, state.message_id,
        message_from_record(record), uuid=record.get("uuid", record["id"]), history_record=record,
    )
    return copy.deepcopy(state.receipt)
```

| 字段位置 | 在哪段代码形成或读取 | 沿链传到哪里 |
| --- | --- | --- |
| status | CORE 根据真实 duplicate 标志选择 duplicate/persisted；派发函数把合法 note 拒绝转成 rejected | 不是用户伪造成功结果来替代写入 |
| session_id / message_id | prepared.session_id 与私有 state.message_id | 目标身份和用户稳定重试键分开 |
| message / uuid / history_record | 从 commit_session_message 实际返回的 record 构造 | 返回的是实际存储事实，不是进入 Handler 前的任意消息 |
| reason | 拒绝分支从 SessionAppendRejection.reason.strip() 取得；正常回执默认 None | 向自己的调用方说明为什么未写入 |
| Ops 返回字典 | 下方 session_append 调用 result.to_dict()；此方法定义就在 SessionAppendResult 内 | message 也转成字典，history_record 独立复制 |

默认 CORE 返回 `SessionAppendResult`；正常 Handler 原样返回 `await next_handler(...)` 的真实对象。不能自行构造“成功”来替代写入。

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| status | str，必有 | persisted：本次完整提交；duplicate：同一原请求已经提交；rejected：扩展自己的 note 被合法拒绝。不要把 duplicate 当作新增一次 |
| session_id | str，必有 | 平台绑定的目标主会话；只核对，不回填成另一个目标 |
| message_id | str，必有 | Ops 调用时回显用户参数 id；平台调用时为原产品记录 id。不是通用行唯一身份 |
| message | SessionAppendMessage 或 None | 成功/重复为**实际存下来的**公开消息（同 3.2 的全部字段）；拒绝为 None。可能与进入 Handler 时正文不同 |
| uuid | str 或 None | 成功/重复为实际记录 uuid，旧行可回退到原 id；拒绝为 None，不能凭拒绝回执声称存在一行 |
| history_record | dict 或 None | 成功/重复为实际产品历史行的独立副本，移除内部 _session_append；常见 id/role/content/request_id/channel_id/timestamp，可有 uuid/transcript_message/native_message。它不等于 messages() 的 role/text/toolUses 消息投影，不能直接比较两个结构；拒绝为 None |
| reason | str 或 None | 拒绝时的非空原因；成功/重复为 None。用于给自己的调用方解释拒绝，不是异常堆栈 |

唯一合法拒绝对象是 `SessionAppendRejection(reason="说明原因")`：reason 必填 str，去首尾空白后 1～512 字符，且必须在 next 之前、针对自己的 note 返回。普通平台/RPC 历史不能被 USER 拒绝；非法拒绝/改写依 Core USER 故障隔离继续原有效输入。抛异常不等于业务拒绝。CORE 已真实提交后，回传改写或后置拒绝不能撤销已写事实。

### 3.4 注册、筛选、改写示例

```python
from dataclasses import replace
from openjiuwen.core.events import Event

class Extension:
    def register(self, registrar):
        registrar.register_event_handler(
            "session.append", self.scrub,
            {"door": "tool-result", "origin.kind": "tool"}, "user",
        )

    async def scrub(self, event, ops, next_handler):
        message = event.event_input.message
        blocks = [
            {**block, "text": block["text"].replace("13800138000", "[phone]")}
            if block.get("type") == "text" else block
            for block in message.content
        ]
        rewritten = replace(event.event_input, message=replace(message, content=blocks))
        return await next_handler(Event(event.event_type, rewritten))

def create_extension():
    return Extension()
```

matcher 为等值条件合取，空字典匹配全部；嵌套字段用 `origin.kind` / `message.role` 点路径，不写成嵌套匹配字典。这里 next 继续当前链，不再调用 append Ops，以免 APPEND_REENTRANT。用户提供 manifest.yaml 和模块，通过管理入口安装；平台负责触发和收集上下文，不要求用户手写平台 Adapter。

## 4. 方法契约

### 4.1 ops.session.append(note)

推荐调用：

```python
receipt = await ops.session.append({
    "id": "audit-order-42",  # 同一业务动作重试必须沿用
    "message": {
        "type": "assistant",
        "content": [{"type": "text", "text": "本轮已完成审计。"}],
    },
})
if receipt["status"] in ("persisted", "duplicate"):
    stored_uuid = receipt["uuid"]
    stored_blocks = receipt["message"]["content"]
```

该方法只接受一个 note 字典，顶层必须恰好为 id/message，或兼容格式 id/role/content。不填写 session_id、路径、door、origin、uuid、时间或任意平台 extra。

#### 方法入参在哪个函数读取

公开调用为 `ops.session.append(note)`。下面内部函数的参数名 message 指整个 note 字典；其中 message["message"] 才是内层公开消息。ops 由 Core 注入、bus 由平台登记时绑定，用户只提供第 4.1 节约定的 note。

```python
async def session_append(
    ops: EventHandlerOps,
    message: object,
    *,
    bus: ExtensionEventBus | None = None,
) -> dict[str, object]:
    """Await session.append policy and commit a transcript-only message.

    Raises:
        RuntimeError: No session is bound to this invocation.
        SessionHistoryError: Invalid input, conflict, lifecycle, dispatch or I/O.
        asyncio.CancelledError: Cancellation, after draining an accepted write.
    """
    from jiuwenswarm.server.runtime.session.history_events import dispatch_session_append

    context = _require_session(ops, "session.append")
    if bus is None:
        raise SessionHistoryError("APPEND_BUS_UNAVAILABLE")
    package = ops.package_metadata
    package_id = package.package_id if package is not None else "<host>"
    prepared, message_id, key, fingerprint = prepare_extension_append(context.session_id, package_id, message)
    result = await dispatch_session_append(
        bus, context, prepared, message_id=message_id, source="extension", append_key=key,
        fingerprint=fingerprint, update_metadata=False,
    )
    return result.to_dict()
```

note 的 id、message、兼容 role/content 在这个实际校验/准备函数中读取；返回的 message_id 回显用户 id，key/fingerprint 用于幂等提交。

```python
def prepare_extension_append(
    session_id: str,
    package_id: str,
    message: object,
) -> tuple[PreparedHistoryRecord, str, str, str]:
    """Validate a user message and create a namespaced transcript-only record."""
    require_session_id(session_id)
    if not isinstance(message, Mapping) or set(message) not in ({"id", "role", "content"}, {"id", "message"}):
        raise SessionHistoryError("INVALID_MESSAGE")
    message_id = message["id"]
    if not isinstance(message_id, str) or _MESSAGE_ID.fullmatch(message_id) is None:
        raise SessionHistoryError("INVALID_MESSAGE")
    legacy = "message" not in message
    try:
        if legacy:
            if message["role"] not in ("user", "assistant") or not isinstance(message["content"], str):
                raise ValueError("Invalid legacy note")
            view = message_from_dict({"type": message["role"], "content": message["content"], "is_meta": True})
        else:
            view = message_from_dict(message["message"])
            if view.type not in ("user", "assistant", "system") or view.role != view.type or view.tool_calls is not None or view.tool_call_id is not None:
                raise ValueError("Notes cannot impersonate tool messages")
            if any(block.get("type") != "text" for block in view.content):
                raise ValueError("Extension notes accept text blocks only")
            view = replace(view, is_meta=True)
        role = "user" if view.type == "user" else "assistant"
        content = message_text(view)
        if not legacy and len(json.dumps(view.to_dict(), ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_APPEND_BYTES:
            raise ValueError("Note exceeds the message limit")
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise SessionHistoryError("INVALID_MESSAGE") from None
    if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
        raise SessionHistoryError("INVALID_MESSAGE")
    try:
        content_bytes = len(content.encode("utf-8"))
    except UnicodeError:
        raise SessionHistoryError("INVALID_MESSAGE") from None
    if content_bytes > MAX_APPEND_BYTES:
        raise SessionHistoryError("INVALID_MESSAGE")
    if not isinstance(package_id, str) or not package_id:
        raise SessionHistoryError("PACKAGE_CONTEXT_REQUIRED")
    key = hashlib.sha256(f"{package_id}\0{message_id}".encode("utf-8")).hexdigest()
    prepared = prepare_history_record(
        session_id=session_id, request_id="ext-" + key[:32], channel_id="extension",
        role=role, content=content, timestamp=time.time(),
        event_type="chat.final" if role == "assistant" else None,
        extra={"id": "ext:" + key, "extension_message_id": message_id,
               "extension_package_id": package_id, "message_origin": "extension", "transcript_only": True,
               "transcript_message": view.to_dict()},
    )
    if prepared is None:
        raise SessionHistoryError("INVALID_MESSAGE")
    return prepared, message_id, "extension:" + key, original_fingerprint(dict(message))
```

内层 message.type/content/role/name/is_meta/tool_call_id/tool_calls 的解析及默认值在这里：

```python
def message_from_dict(value: object) -> SessionAppendMessage:
    """Parse the public JSON message schema with explicit field validation."""
    if not isinstance(value, Mapping) or not {"type", "content"} <= set(value):
        raise ValueError("A message requires type and content")
    fields = {"type", "content", "role", "name", "is_meta", "tool_call_id", "tool_calls"}
    if set(value) - fields or value["type"] not in MESSAGE_TYPES:
        raise ValueError("Unsupported message fields or type")
    kind = value["type"]
    role = value.get("role", kind if kind != "attachment" else None)
    if role not in (None, "user", "assistant", "tool", "system"):
        raise ValueError("Unsupported message role")
    name, tool_id, calls = value.get("name"), value.get("tool_call_id"), value.get("tool_calls")
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 256):
        raise ValueError("Invalid message name")
    if tool_id is not None and (not isinstance(tool_id, str) or not tool_id):
        raise ValueError("Invalid tool call id")
    if calls is not None and (not isinstance(calls, list) or any(not isinstance(x, dict) for x in calls)):
        raise ValueError("Invalid tool calls")
    if not isinstance(value.get("is_meta", False), bool):
        raise ValueError("Invalid is_meta")
    result = SessionAppendMessage(kind, content_blocks(value["content"]), role, name, value.get("is_meta", False), tool_id, copy.deepcopy(calls))
    _json_size(result.to_dict())
    return result
```

字段位置可沿这条路径核对：`note → session_append 的 message 参数 → prepare_extension_append 内读取 message["id"] / message["message"] → SessionAppendMessage → SessionAppendInput.message`。

| 输入字段 | 必填 / 默认 | 应填写的内容、范围及示例 |
| --- | --- | --- |
| id | 必填，无自动值 | 1～128 个 ASCII 字母、数字、`.`、`_`、`:`、`-`；如 audit-order-42。同一业务动作在同一包、同一会话重试使用相同 id |
| message | 新格式必填 | 字典，type/content 必填；允许下述 role/name/is_meta；不要传整个 Event 或 BaseMessage 对象 |
| message.type | 必填 | user / assistant / system；留档为助手说明通常填 assistant，纯通知可填 system；不能填 tool/attachment 冒充平台产生者 |
| message.content | 必填 | 非空文本，可填字符串或 text 块数组；推荐数组。多段按原顺序拼接为兼容显示正文，不自动增加换行；需要换行时写入 text |
| message.role | 可省略，默认 type | 只能与 type 相同；推荐省略。不能用 type=user、role=assistant 的矛盾组合 |
| message.name | 可省略，默认 None | 非空字符串，最多 256 字符；给自己的说明命名。不是写入目标或授权身份 |
| message.is_meta | 可省略，平台固定 True | 即使输入 False，也按扩展附加消息存为 True；推荐不填 |
| message.tool_call_id / tool_calls | 应省略或为 None | note 不提供真实工具结果；任何非 None 值均拒绝 |
| 兼容格式 role | 仅旧格式必填 | user / assistant；与 message 互斥，不混用 |
| 兼容格式 content | 仅旧格式必填 | 非空字符串，UTF-8 不超过 1 MiB；不能在旧格式塞入块数组 |

新格式整个消息 JSON UTF-8 不超过 1 MiB；不允许媒体、未知块、空白正文、非 JSON 数值或未知字段。平台规范化为公开消息，固定来源为 note / extension / 真实包 ID / ops.session.append，再触发新事件链。目标只来自 `ops.session_context.session_id`，不依赖 cwd/root/model/用量事件。

方法返回**字典**：status/session_id/message_id/message/uuid/history_record/reason 的含义同 3.3；message 在字典内也转成字典，content 为数组。失败抛 SessionHistoryError，其 code 不含原始内容；没有 SessionContext 抛 RuntimeError。

幂等范围为真实包 ID + 会话 + 参数 id；必须连同原参数格式和内容原样重试。相同原请求返回 duplicate 与先前实际消息；同一 id 改原参数报 APPEND_ID_CONFLICT。更改 Hook 后重试仍返回第一次实际写入，不用新规则重写旧行。取消/结果未知时同 id 原样重试确认。

### 4.2 旧 Handler / 回执迁移

| 旧用法 | 本次应该改成 |
| --- | --- |
| event.event_input.content | 遍历 event.event_input.message.content 的 text 块 |
| replace(input, content=text) | replace(input, message=replace(input.message, content=blocks))；保持块位置 |
| matcher source=extension | `{'door':'note', 'origin.kind':'extension'}` |
| matcher source=history.append_record | `{'origin.entry':'history.append_record'}` |
| 输入 message_id | 单行关联用 uuid；用户自己的 id 由调用方保留，Ops 回执 message_id 回显 |
| receipt['message']['content'] 是字符串 | 现在为内容块数组；兼容产品正文读取 receipt['history_record']['content'] |
| receipt['message'] 与分页行比较 | 改为 receipt['history_record'] 与分页行比较 |

旧 append 参数 id/role/content 保持可用；旧事件字段和旧回执 message 的形态不保持兼容。PR 尚未合入，随 PR 内示例、测试和文档一起迁移，不声称任意旧 Handler 可原样运行。

## 5. 接入与兼容方案

event_types/session_history.py 只维护类型、Validator 和注册；server/runtime/session/history_events.py 收集调用事实并 trigger；history_message.py 负责原生/产品消息映射和受控合并；event_handlers/session_history.py 的 CORE 实际写入。私有调用封装固定原记录、原输入、目标、生命周期代次与回执，不由事件可改正文决定路由。

平台原生消息入口示意（**平台代码**，不由用户扩展收集上下文）：

```python
await run_history_io(
    append_history_record,
    session_id=session_id, request_id=request_id, channel_id=channel_id,
    role=base_message.role, content=base_message, timestamp=timestamp,
)
```

该异步入口收到原生 BaseMessage 时，在过滤/提交之前适配为公开消息，同时将原生 JSON 快照留在 native_message 中。工具关联、metadata、工具参数由宿主保留，允许的 content 改写合并回待存快照，原调用者的 BaseMessage 实例不被就地修改。原有产品流式事件入口继续适配，不要求把全系统历史迁移为 BaseMessage 存储。

启动阶段，现有 ops.build CORE 返回 Builder，平台 BUILTIN 在 next 返回后登记 append 等共享方法；这不是写入事件的 CORE。业务阶段 Builder 为实际 Handler 绑定包身份和 SessionContext，USER 调 append 才另起新 session.append；[历史查询的同名事件链](F_18_session-messages.md#51-启动登记上下文绑定与-core-分工) 独立说明。session.append 没有新增 BUILTIN；默认 CORE 负责写入，USER 负责自己的规则。

生命周期预检在 Hook 前捕获 generation，提交锁内复验；不持文件/队列锁执行 USER。文件关闭后才成功回执，同一次调用的重复 next 复用真实结果；回执深拷贝，后置篡改不改变已经保存的事实。写入开始后取消先排空，再传播 CancelledError。写入与元数据仍非跨存储原子事务，不承诺 fsync 断电耐久。

主要失败：INVALID_MESSAGE（输入/参数非法）、INVALID_SESSION/SESSION_NOT_FOUND（会话非法/追加目标不存在）、APPEND_ID_CONFLICT、APPEND_REENTRANT、HISTORY_IO_FAILED、APPEND_OUTCOME_UNKNOWN。历史查询的参数、游标和快照错误见 F_18。USER 内未捕获的 Ops 异常仍可能被外层 Core 隔离；必须查看真实返回/自行捕获，不能把外层回合继续当作追加成功。

卸载/重载撤销用户 Handler，不删除已提交历史或平台 CORE；共享 Ops 在有效 SessionContext 下可供其他事件 Handler 使用。无需用户调用 notify_context_usage 来创建新入口；已有示例仅选用量通知展示接入。

## 6. 验收与未决问题

2026-10-31 输入输出源码补充：本次只调整文档，将当前类型定义、构造/读取函数和回执返回位置放到字段表旁；代码摘录按本分支实际实现核对，省略的分支明确标注。以下历史业务验证保留原日期和范围，本次未重跑业务、模型或浏览器验收。

2026-10-30 冲突集成验证：合并目标分支 `rsi_native_agent_os` 的 `9115c12d`，保留 PR04 的 messages/append、目标分支已合入的 turns/usage，以及两边原生 Adapter 测试和开发计划。八项 session 方法由同一个共享 Builder 登记，append 继续绑定当前 Worker 的 EventBus；事件和方法契约未调整。

本次在 Windows、Python 3.11.15、Core `ead700d7d18a247968d64de54918cf5d201a207a` 下，扩展、历史、原生流式回合及模型上下文定向回归 **513 项通过、1 项跳过**；`test_symlink_escape_blocked` 因环境不能创建符号链接跳过。另新增并单独运行 `test_history_and_statistics_ops_coexist_in_one_user_handler`，**1 项通过**：真实安装的用户包在同一个 Handler 中调用八项 session Ops，追加经既有 USER 策略改写后实际写入 JSONL，查询页与真实回执一致，扩展 user note 不增加两轮原生用户输入的统计，也不覆盖已有用量；另一会话保持不变。未提供目录和模型时，相关只读方法仍返回 None。

本次完整命令、源码路径和 Core 版本、pytest XML 见 `D:\work_space\.codex-temp\pr83-conflicts-20261030` 的 regression-command.json、regression.xml、coexist-command.json、coexist.xml。新增用例的定向命令为 `python -m pytest -o addopts= -q tests/unit_tests/extensions_v2/test_session_history_ops.py::test_history_and_statistics_ops_coexist_in_one_user_handler --asyncio-mode=auto -o log_cli=false`。本次未重跑下述真实模型或浏览器验收，历史真实服务证据仍对应其原始日期。

2026-10-29 本次仅按职责拆分文档，未改运行代码，未重跑下述业务或真实模型验收。以下保留 PR04 读写联合验证的日期、总数与范围；查询单独的场景和证据归属见 [F_18 第 6 节](F_18_session-messages.md#6-验收与未决问题)。

本轮 定向回归 388 项通过、1 项跳过；模型兼容回归 74 项通过。命令与证据目录：`D:\work_space\.codex-temp\server-pr04-message-contract-20261029`，对应 regression.xml、model-regression.xml、各 command.json；未运行格式化或静态检查工具。

| 场景 | 输入/操作 | 验证结果或证据 |
| --- | --- | --- |
| 原历史能力 | 历史读取、幂等、重入、隔离、取消、生命周期、RPC、原生上下文 | 定向回归覆盖，见本轮 XML |
| 新消息契约 | 结构化 note、多个文本块、成功/拒绝回执、原参数兼容 | 新增测试核对实际 JSONL、uuid、message/history_record 一致 |
| 工具消息 | 工具结果文本改写、工具关联、原始 result/私有字段 | Registry USER → EventBus → CORE → JSONL 读回；关联/原始字段保留 |
| 防越权改写 | 改 uuid/origin/door/role/tool ID、媒体、未知块、块数 | 非法 USER 被隔离，默认保存原有效消息 |
| 原生 BaseMessage | ToolMessage 与多模态 UserMessage 经真实平台异步函数 | 写入前实际触发，内容允许改写，原对象及 metadata 不变 |
| 行身份 | 同请求多条产品记录、重复追加 | 产品 id 保持，行 uuid 独立；同一幂等请求回执沿用实际行 |

真实服务验证：真实 Front → Supervisor → 子 Worker → Qwen3.8-Flash，两包/两会话运行通过；结构化 note、真实工具结果文本改写与工具 ID、实际 JSONL uuid/消息一致、平台/RPC 改写与不可拒绝、幂等/拒绝/分页、旧四项 Ops、卸载与进程端口清理通过。无 mock 模型/Ops/Adapter，未手工构造 SessionContext。源码 SHA256 收据证明运行期间实现未变；目录 `D:\work_space\.codex-temp\server-pr04-message-contract-20261029\real-155735`，见 structured-tool-evidence.json、result.json、cleanup.json。

2026-10-28 旧契约曾通过 363 项定向回归和 74 项模型兼容测试，并完成真实两包/两会话验证；这是历史记录，不能代替本轮结构化契约验证。当前证据以本节本轮结果为准。

未验证浏览器产品 UI、非 Windows 实机、远端 CI、所有 Harness/同步后台入口、活跃模型消息同步、全量多模态恢复与大历史性能。历史查询/持久去重首版仍扫描文件。未知门类/供应商块只读保留，不以“完整消息视图”宣称所有原生私有字段向用户开放。

## 7. 参考资料

- [F_18 历史查询](F_18_session-messages.md)：无参数查询、消息数组、只读用户包与查询证据。
- [S_06 主责规约](../specs/S_06_session-history-ops.md)：不变量、兼容与错误范围。
- [内置事件接入说明](../designs/builtin-event-development-guide.md)：公共 EventBus/Handler/Ops 机制。
- [事件类型](../../event_types/session_history.py)、[平台触发](../../../server/runtime/session/history_events.py)、[消息映射](../../../server/runtime/session/history_message.py)、[默认 CORE](../../event_handlers/session_history.py)。
- [Ops](../../handler_ops/session_ops.py)、[历史查询和提交服务](../../../server/runtime/session/session_history_ops.py)、[平台上下文](../../../server/runtime/session/history_context.py)。
- [可安装示例](../../examples/session_history_audit/README.md)、[原生 BaseMessage](https://gitcode.com/huenrui_/agent-core/tree/rsi_native_agent_os/openjiuwen/core/foundation/llm/schema/message.py)。
- [Claude 固定提交类型定义](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)：SessionAppendInput/Message/Origin/Args/Result；不宣称当前最新版互换兼容。
