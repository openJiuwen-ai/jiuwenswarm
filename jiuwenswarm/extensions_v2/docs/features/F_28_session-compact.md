# session.compact / ops.session.compact 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_28 / session-compact |
| 设计日期 | 2026-11-03 |
| 状态 | Ops / Event 接口与 Handler 链已开发；主流程接入注释保留，待合入 |
| 关联计划 | [EXTV2-26](../designs/extensions-v2-development-plan.md#extv2-26-会话压缩与压缩前扩展) |
| 主责规约 | [S_15_session-compact.md](../specs/S_15_session-compact.md) |
| 跨仓方案 | Core：D-20261103-1156-session-compaction-events，位于 `.agents/agent-docs/designs/active/` 同名目录 |

**2026-11-03 范围收缩：** 用户明确要求本轮只交付 Ops / Event 接口与 Handler 链，主流程接入注释保留，真实压缩暂不接入。生产环境不绑定原生引擎，不拦截原有手动、自动和溢出压缩，不自动调度预计算；默认 Ops 报 COMPACT_NOT_INTEGRATED。以下完整压缩实现和原验收记录仅说明预留机制及历史版本，不能作为当前主流程已启用的结论。

## 1. 范围、状态与依据

本文按 [session.append](F_16_session-append.md) 的七节结构维护已实现的事件、Ops、字段与调用案例。用户于 2026-11-03 确认完整 Core＋Server 设计后开发；当前实现依赖新的 Core 压缩协议和 Extension Runtime 调用有效性校验，需先使用对应 Core 版本。

基线为 Server `36e353db753390720744a1505dbc4b2db086251c`、Core `86990bd16a0aa4ef900c2255fa3a3c828f4c57de`。原有能力是原生 ContextEngine 压缩、command.compact、自动压缩及溢出恢复。本次新增压缩前 Hook、session.compact / ops.session.compact 与候选提交边界。开发中同步到 Server `4b6021ca873e4e3b697628e29b72fdd1eef344d6`、Core `45e6418c01302124da6736972eec20e68d0dd4f2` 后验收，原设计基线仅保留追溯。

对照 Claude Code Mods 固定提交 `bf2323b3021c9d3db7a31e1e7d61068c3f704497` 的类型声明，重点是 SessionCompactArgs/Input/Result、SessionMessage 和方法注释。字段尽量对齐，但使用 Python dataclass 顶层 snake_case；不把公开类型声明当作本项目已运行的证据。

本轮交付 **Ops / Event 接口阶段**：保留 manual、auto、plugin、precompute 四个协议值及独立候选机制，但生产主流程不产生本扩展事件。原 command.compact、原生自动压缩和溢出恢复继续执行原算法；输入前引擎绑定、任务门禁与预计算调度的接入点均已注释。agent_id 仍固定 None。真实主会话接入待后续主流程稳定后单独恢复并验收。

2026-11-03 按用户要求，以当前目录连续编号为本次文档修订口径：现有特性为 F_01～F_27，规约为 S_01～S_14，因此本特性采用 F_28/S_15。旧编号 F30/S17 仅保留历史追溯含义；本次重命名尚未合入的 compact 文档并同步引用，已合入文件不重排。EXTV2-26 是独立的开发计划编号，不要求与 F/S 编号相同。

## 2. 能力说明与 Claude 对照

`session.compact` 的预留语义是在一次会话压缩真正改写上下文之前运行；当前仅在独立受控绑定测试中触发，生产压缩流程尚未接入。USER 可以补充摘要重点、改变参与摘要的消息、返回自己的消息列表，或用 skip 跳过本次压缩。默认 CORE 只让 Core 引擎生成**候选消息**；等整个事件链返回后，引擎才校验并安装最终结果。

`await ops.session.compact(instructions=...)` 是插件主动请求压缩的入口，使用当前 Ops 绑定的真实会话。它先进入平台压缩服务和 Core 协调器，再由 Hook 触发同名事件；不是在没有活跃上下文的情况下直接拼一个 Event。**当前生产环境没有引擎绑定，合法的活动 Handler 调用会报 `COMPACT_NOT_INTEGRATED`，尚不进入内层事件或真实算法。**下述事件往返通过测试显式绑定受控引擎验证，不是生产使用教程。

| 能力 | 固定 Claude 定义 | 本次实现及差异 |
| --- | --- | --- |
| 触发原因 | manual / auto / plugin / precompute | 保留同四值；生产 manual/auto/plugin/precompute 均未连接，独立测试显式注入可信请求 |
| 方法参数 | instructions?；其它事实引擎提供 | Python `instructions: str | None = None`；不允许用户指定 trigger、messages、目标会话 |
| 事件输入 | trigger、agentId?、instructions?、messages | 同语义；agentId 写作 agent_id，首期仅主会话 None |
| 输出 | messages、tokensBefore?、tokensAfter?，或 skip | 同互斥语义；Token 字段写作 tokens_before/tokens_after；**没有独立 summary 字符串** |
| 消息 | SessionMessage；compact 专用 handle | 同 role/text/toolUses/toolResults/handle 分工；原生 ToolMessage 通过公开视图和 handle 还原 |
| 插件调用链 | 除调用 Hook 以外的 Hook，再到 core | 普通 matcher 执行完整匹配链；不增加通用排除接口。同会话 compact 内重入在第二次事件前拒绝，外层其它事件的订阅本来不匹配 compact |
| 改写与短路 | 修改输入、改写 next 结果、自定义结果、skip | 已支持；CORE 不提前安装，post-next skip 才能成立 |
| precompute | 不安装，保存候选，来源前缀有效时复用 | 候选机制保留；生产调度调用已注释，修改 enabled 配置也不会启用主流程 |
| 原生消息保留 | 有 handle 使用引擎原消息；无 handle 按公开字段构建 | 同语义；本地 dataclass 改写须保留 trigger/agent_id，不提供 JS 缺字段自动补回语法 |

压缩改变的是模型后续使用的活跃上下文及其 Session 保存。历史 JSONL 保留旧对话，并按原流程追加压缩标记。因此 `ops.session.messages()` 的产品历史查询结果，不一定等于本事件返回的压缩后消息数组；这两个查询对象不同。

## 3. 事件契约

### 3.1 触发前后与粒度

**当前生效链：注册完成，主流程接入停在注释处。**

```text
Worker 启动 → 注册 session.compact 类型 / 校验 / matcher / 唯一 CORE
  → ops.build 的 BUILTIN 在 Builder 登记 session.compact 方法代理
  → 为实际用户 Handler 创建 Ops 并绑定 SessionContext
用户 Handler 调用 await ops.session.compact(instructions="保留待办")
  → Server 校验 Runtime 当前调用及绑定会话
  → 没有生产引擎绑定 → 抛 COMPACT_NOT_INTEGRATED
  → 不触发内层 session.compact、不调用模型、不安装/保存、不写压缩历史

原生正常 query / command.compact / 自动压缩 / 溢出恢复
  → 按原流程执行，不触发本扩展的 session.compact
```

**独立测试的事件往返：** 测试显式调用 `service.bind(受控引擎, SessionContext, ...)`，再从活动 USER Handler 调用 Ops；可观察到内层 USER → 可选 BUILTIN → CORE → 受控 continuation → 逐层返回。这个测试绑定不在生产 Adapter 启动时执行，用户不能通过手工填写 SessionContext 获得真实压缩权限。

下面是保留代码的完整预留流程，**生产接入点均已注释，不能理解为当前主流程已启用**：

```text
启动登记
  → 平台注册 session.compact 的 matcher / 校验 / 唯一 CORE
  → session_ops_build 在 ops.build 的 Builder 上登记 compact 代理
  → 原生 Adapter 为真实 ContextEngine 注册宿主压缩前 Hook
  → 不在启动时读取全部消息，也不在启动时执行压缩

业务发生：command.compact / ops.session.compact / 原生自动压缩 / 宿主预计算
  → 平台解析真实会话和原生引擎；manual/plugin 通过空闲独占门禁
  → Core 捕获待压缩消息及版本，提供本次默认候选准备函数；尚不创建或安装 draft
  → Hook 取得可信 SessionContext，构建公开消息视图和本次 handle 映射
  → await Worker bus.trigger(Event("session.compact", input), context)
  → Bus 用共享 Builder 创建每个实际 Handler 的本次 Ops，绑定会话和包
  → Runtime 包装记录受管包 Handler 的当前 Ops / Task / 活动期，不给 Ops 新增 Handler 身份字段
  → USER：补充 instructions、改写 messages，或直接返回 skip/自定义结果
  → await next_handler(Event(event.event_type, rewritten_input))
  → 其它 USER → 可选 BUILTIN → session_compact_core
  → CORE 检查私有调用状态/固定身份，调用本次默认 prepare continuation
  → 默认 continuation 复用有效候选，或此时创建隔离 draft 并运行原算法；返回候选，计数通常尚为 None
  → 沿链返回 USER；USER 仍可改写候选或返回 skip
  → 整条链返回宿主，句柄还原为原生消息，Core 做最终版本/消息校验
  → skip：丢弃候选，当前会话不因本次压缩而替换
  → precompute：仅保存候选，不安装、不写压缩历史
  → 其它有效结果：安装一次并持久化，原平台后处理写历史标记/更新用量
  → 最终返回业务调用者；Ops 调用者此时才收到正式完成结果
```

事件字段及 USER 的改写/skip 权限继续按下面契约定义；CORE 只有在可信宿主调用帧中才能调用 continuation。自行拼 Event 不能越过平台绑定。

### 3.2 输入：每个字段表达什么、由谁填写

导入位置：`jiuwenswarm.extensions_v2.event_types.session_compact`。以下构造展示字段位置。普通用户只读取/replace 平台提供的对象，不自己收集模型私有消息来伪造平台输入。

```python
SessionCompactInput(
    trigger="manual",
    agent_id=None,
    instructions="保留未完成任务、文件路径和用户约束",
    messages=[
        {"role": "user", "text": "检查订单模块", "toolUses": [], "handle": "opaque-1"},
        {"role": "assistant", "text": "正在核对接口", "toolUses": [], "handle": "opaque-2"},
    ],
)
```

| 字段 | 类型 / 必填与默认 | 代表什么、起什么作用 | 谁填写、填写什么及例子 | USER 权限及错误边界 |
| --- | --- | --- | --- | --- |
| trigger | Literal 四值，必有 | 本次为何压缩；用于 matcher 和区分是否安装结果 | **平台填写** manual/auto/plugin/precompute；插件主动调用自动得到 plugin | 只读；不能把 auto 改成 manual 或把 plugin 改成 precompute |
| agent_id | str 或 None，默认 None | 被压缩子循环的身份，不是主会话 ID，也不是模型名称 | **平台填写**；首期主会话固定 None，不要填 session-a 或 qwen | 只读；首期非 None 不支持，不用它选择目标 |
| instructions | str 或 None，默认 None | 告诉摘要算法重点保留什么，不是用户下一轮 query，也不替代系统压缩指令 | **可信测试/未来宿主提供** Ops 参数；原 command.compact 的透传接入当前已注释；USER 可改，如“保留接口约束、测试结论和待办”；None/纯空白表示无附加要求 | 可 replace；非字符串报输入错误；不修改共享模型配置 |
| messages | list[SessionMessage]，必有，按最旧到最新 | 真实待压缩上下文的公开快照；决定摘要输入，不是历史分页游标或产品历史文件路径 | **独立测试由显式绑定的受控引擎提供**；未来宿主从活跃 ContextEngine 取出，自动 ADD 的 pending 接入目前未启用；USER 用新的列表替换 | 可选取、重排或构造，但须通过原生工具链/消息校验；保留 handle 时不能靠改 text 改写原消息 |

#### messages 中每条 SessionMessage

与七查询已有视图共享角色/工具键名，但 compact 额外有 handle；`ops.session.messages()` 不返回这个句柄。

| 字段 | 类型 / 必填与默认 | 表达什么、为什么需要 | 如何填写及例子 | 改写与注意事项 |
| --- | --- | --- | --- | --- |
| role | user / assistant，必有 | 这条公开消息属于哪种对话角色；不是插件权限级别 | 平台投影；新建摘要通常填 user，例如 `{"role":"user", ...}` | 无 handle 时决定原生构造类型；不能填 system/tool。原生 ToolMessage 用 user+toolResults 视图和 handle 还原 |
| text | str，必有；无文本填空字符串 | 文本块拼接内容；新摘要也放这里，不另设 summary 字段 | “用户要检查订单；接口已核对；下一步补测试”，或工具-only 消息填 `""` | 带 handle 时更改 text 不生效；要改写须移除 handle |
| toolUses | list[ToolUseSummary]，必有；无调用填 [] | assistant 消息发起的工具调用，及可用的结果摘要 | 普通摘要填 []；调用例子见下表 | user 消息不能新建 toolUses；有工具链时必须保留真实关联，不能编造已完成结果 |
| toolResults | list[ToolResultSummary]，可缺省 | user 视图所含工具结果；和 tool_use_id 配对 | 无结果省略；工具结果例见下表 | 仅 user 视图；不能留下没有对应调用的结果 |
| handle | str，可缺省；**仅平台生成** | 本次调用内的原生消息凭据，用来无损保留媒体、工具消息、边界等隐藏信息 | 保留平台给出的原字符串，如示意 opaque-1；新建摘要不要填写 | 有效 handle 优先恢复完整原消息，忽略该条公开字段改写；未知/重复/跨调用句柄失败；不存入配置或用于下一次事件 |

核心区别：`{**message, "text": "新正文"}` 仍带 handle，实际保留旧消息；要新正文生效，应构造无 handle 的 `{role, text, toolUses, toolResults?}`。这可能失去原消息未公开的媒体/元数据，因此只在确实要重建时使用。

#### 工具字段的全部含义

```python
# assistant 的 toolUses 元素
{"tool_use_id": "call-42", "tool": "read_file", "input": {"path": "src/order.py"},
 "text": "订单接口正文", "result": {"content": "..."}}
# user 视图的 toolResults 元素
{"tool_use_id": "call-42", "text": "订单接口正文", "isError": False,
 "result": {"content": "..."}}
```

| 所在对象 / 字段 | 类型 / 是否必填 | 含义、填写内容与用途 |
| --- | --- | --- |
| ToolUseSummary.tool_use_id | str，必填 | 这次工具调用的关联 ID，如 call-42；不能填写会话 ID；用于和工具结果配对 |
| ToolUseSummary.tool | str，必填 | 实际工具名，如 read_file；不是工具描述或 Python 文件名 |
| ToolUseSummary.input | JSON object，必填 | 模型实际给工具的参数，如 path；无参数填 {}；不能放函数、Session 或密钥对象 |
| ToolUseSummary.result | JSON 可表示值，可缺省 | 已记录的结构化结果；未完成或未记录时不填，不因为字段可选就伪造成功 |
| ToolUseSummary.text | str，可缺省 | 已返回给模型的结果文本；调用仍在执行时可缺省；不是发起工具时的参数 |
| ToolUseSummary.isError | 仅 True，可缺省 | 仅确有工具错误时出现 True；正常工具不必填写这个键 |
| ToolResultSummary.tool_use_id | str，必填 | 本结果回答哪次工具调用；须对应保留的 toolUses ID |
| ToolResultSummary.text | str，必填 | 模型读到的结果文本，无文本为 `""`；不是工具名 |
| ToolResultSummary.isError | bool，必填 | False 为普通结果，True 为工具错误；与 ToolUseSummary 的“仅错误时出现”规则不同 |
| ToolResultSummary.result | JSON 可表示值，可缺省 | 记录保存的结果数据，如 JSON；没有记录时省略，不要求重新执行工具来补 |

保留原 handle 时不需要用户重建这些工具字段。无 handle 的自定义工具消息必须满足 Core 的配对和结构约束；验证失败不会自动执行工具、补工具回执或重试摘要。

当前 codec 不主动填充公开的 result 字段；无 handle 重建时，assistant 仅从 tool_use_id / tool / input 构造 ToolCall，user 的工具结果仅从 tool_use_id / text / isError 构造 ToolMessage。共享结构允许携带的 result 等附加值不保证被这次重建保留；需要保留完整原生信息时沿用合法 handle。user 新建消息的 toolUses 应为 []，当前解码不会从 user.toolUses 恢复工具调用。这是原生适配范围，不应把字段名对齐理解为全部工具原始结果无损兼容。

### 3.3 输出：压缩候选、正式完成与跳过

```python
# 一个包含摘要和保留原消息的目标结果
SessionCompacted(
    messages=[
        {"role": "user", "text": "订单任务摘要：接口已核对，待补测试", "toolUses": []},
        original_last_message,  # 保留它的本次 handle
    ],
    tokens_before=18000,
    tokens_after=4200,
)
# 或者：本次不安装压缩结果
SessionCompactSkipped(skip="当前必须保留完整诊断过程，稍后再压缩")
```

| 返回类型 / 字段 | 类型 / 必填与默认 | 准确含义 | 应填写什么、怎样使用 |
| --- | --- | --- | --- |
| SessionCompacted.messages | list[SessionMessage]，该分支必有 | 整条链最终通过后，用来替换可压缩会话内容的完整列表；不是只给新增摘要、让平台自动拼剩余内容 | USER 自定义结果时填摘要＋全部需要保留的消息，按旧到新；使用与输入同一 SessionMessage 字段。[] 表示不保留可压缩对话内容，仍须通过原生控制/工具约束，不代表删除 JSONL |
| SessionCompacted.tokens_before | 非负 int 或 None，默认 None，bool 不算整数 | 引擎本地 TokenCounter 对压缩前消息的计数，可能是估算；不含独立系统提示/工具声明，不是 API 计费总量，也不是字符数 | 通常由引擎填写，用户自定义结果省略。无法测量就 None；对外数据导出时省略缺值，不能补 0 |
| SessionCompacted.tokens_after | 非负 int 或 None，默认 None | 最终安装消息的本地 Token 计数，可能估算；不是默认候选被后来改写前的计数 | 普通完成时有真实计数才填写；**precompute 始终缺省/None**。post-next 改消息后由引擎重算或删除旧计数 |
| SessionCompactSkipped.skip | str，必填，去首尾空白后 1～512 字符 | 用户能理解的本次跳过原因；不是技术堆栈、错误码或永久关闭配置 | “保留完整诊断过程，本次跳过”；调用者展示/记录此原因。空字符串非法；本地长度约束比 Claude 类型更明确 |

两个结果分支互斥，不接受同时含 messages 和 skip 的 dict；普通 dict、None 或 summary 字符串不是本地合法返回。Token 是引擎测量事实，最终出口不信任 USER 自填数字；如果改变了候选则重新计算或省略。

**next 返回的是候选，不代表已经持久化。** USER 可以在 next 前返回自定义结果，也可以在 next 后返回 skip；两者都由最终 Core 安装边界处理。Ops 调用方在整条链与安装完成后才得到权威结果。precompute 的“完成”只表示候选已准备/保存，不表示已替换当前会话。

默认算法无可压缩内容可返回 skip 原因；无会话、忙碌、模型失败、结果非法和保存失败是错误，不统一伪装成 skip。自动压缩被 skip 后仍超窗时可能导致当前模型请求失败，这不是承诺“跳过也一定能继续成功”。

### 3.4 注册、筛选和改写示例

以下是可按实际项目规则修改的用户代码。matcher 支持 trigger/agent_id 的等值合取，链开始时匹配结果固定；改 instructions 不重新选 Handler。

```python
from dataclasses import replace
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.session_compact import SessionCompactSkipped

class ProjectSummary:
    def register(self, registrar):
        registrar.register_event_handler("session.compact", self.on_compact, {}, "user")

    async def on_compact(self, event, ops, next_handler):
        value = event.event_input
        # Optional user policy; this is not a built-in product rule.
        if value.trigger == "auto" and self.keep_full_diagnostics(ops.session_context.session_id):
            return SessionCompactSkipped(skip="正在诊断，需要保留原始对话")
        instructions = "\n".join(filter(None, [
            value.instructions, "保留用户约束、改动文件、验证结果和下一步待办。",
        ]))
        result = await next_handler(Event(event.event_type, replace(value, instructions=instructions)))
        return result

    def keep_full_diagnostics(self, session_id):
        # Example plugin state, not a rule imposed by session.start or Core.
        return False

def create_extension():
    return ProjectSummary()
```

这个插件的需求是“压缩后项目助手仍记得约束和待办”。USER 负责加摘要重点；CORE 负责调用原压缩器准备候选；平台负责最终安装。注册并不额外触发压缩，只有真实业务入口需要压缩才进入 Handler。可按 `{"trigger": "manual"}` 只订阅手动压缩。

若要自定义摘要，USER 返回 `SessionCompacted(messages=[自建无 handle 的摘要, *保留消息])`，不调用 next；若仅在下游候选上做修改，用 replace 返回新列表。不能在本事件 Handler 里调用 ops.session.compact 递归启动另一条压缩链。

## 4. 方法契约

### 4.1 ops.session.compact(instructions=None)

Python 方法签名：

```python
async def compact(*, instructions: str | None = None) -> SessionCompacted | SessionCompactSkipped: ...
```

| 参数/前置条件 | 应填写什么 | 表达的意思与处理方式 |
| --- | --- | --- |
| instructions | 可省略；None/空白表示不追加要求；例“重点保留尚未完成的测试计划” | 只是本次摘要保留重点，不是手动填模型消息数组 |
| 当前 Ops 的 session_context | 用户不填写；平台已绑定可信 session_id | 定位本次主会话的真实活跃引擎；无上下文明确失败，不返回猜测值 |
| 当前包 Handler 调用 | 用户不填写；平台通过 Runtime 发布的包装记录 Ops、任务和活动期 | 仅当前层、当前任务的活动 Ops 可调用；退出/子任务/非受管直接注册及伪造 Ops 拒绝。不新增 handler_id 参数或属性 |
| 会话空闲状态 | 用户不能伪造；由 Worker 真实执行门禁检查 | 两轮之间受理；回合仍在运行报 COMPACT_BUSY，不等待自己所在回合退出 |
| 不接受的参数 | 不传 session_id、agent_id、trigger、messages、bus、回调或 Processor | 这些都来自平台可信绑定或已注册服务；方法不是任意目标压缩 API |

返回字段和分支完全复用第 3.3 节；在测试显式绑定后，方法会进入 session.compact(trigger=plugin)，不需要用户再 bus.trigger 一次。当前生产无绑定时抛 COMPACT_NOT_INTEGRATED，不返回伪造的成功/skip。不能把返回的 skip 当成异常，也不能把 errors 当作 skip 隐藏。

### 4.2 一个不用前端的完整验证案例

本轮需求是“接口已做好，但不接入可能继续改动的主流程”。[示例包](../../examples/session_compact/README.md) 默认只订阅 `session.compact` 策略，生产压缩不会自动触发该策略。显式选择 `ProjectCompaction(compact_on_attach=True)` 后，on_attach 会尝试 Ops 并记录 COMPACT_NOT_INTEGRATED，随后继续原 attach 链；不会真的压缩会话。

```text
生产装配 / 安装真实扩展包
  → session.start 或其它已绑定 SessionContext 的 USER 调用 compact Ops
  → Runtime 确认活动调用 → 平台服务没有原生绑定
  → 明确报 COMPACT_NOT_INTEGRATED → 用户记录原因 → next 原业务事件
  → 原业务 CORE 完成；会话消息、模型和历史均未因 compact 改变

独立受控测试（不接生产 Adapter）
  → 手工装配 Bus / Runtime / Builder / service，显式绑定测试引擎
  → 外层 test.caller USER 调用 compact Ops
  → service → Core 显式 compact_context → Hook → Event(session.compact, input)
  → 内层 USER 补充 instructions → 可选 BUILTIN → CORE 校验并调用 continuation
  → 受控模型准备候选 → BUILTIN 后置 → USER 后置
  → 测试引擎确认最终结果 → Ops 返回 messages / skip → 外层 USER 读取参数
  → next 原外层事件 → 原 CORE 返回；ui.render 0 次
```

源码验证入口：`test_default_ops_reports_not_integrated_without_dispatch_or_mutation` 核对默认未接入、没有事件/模型/提交副作用；`test_explicit_test_binding_traces_ops_user_builtin_core_and_returns_result` 核对完整层级往返与 instructions 传递；`test_installed_package_can_see_compact_ops_but_native_backend_is_not_integrated` 通过真实 Registry 安装扩展包后调用 Ops，核对不是因缺少注册或失效身份而偶然失败。`test_native_input_start_does_not_bind_compaction_engine` 核对输入前仍通知 session.start，但不安装 compact 绑定。

原真实模型、manual/auto/plugin 与 SQLite 冷恢复结果仅为第 6.1/6.2 节对应旧版本的历史证据，不证明本轮已启用真实压缩。

### 4.3 错误、重复操作与取消

| 情况 | 调用者得到什么 | 是否安装上下文 |
| --- | --- | --- |
| 当前生产未接入真实引擎 | COMPACT_NOT_INTEGRATED；事件尚未进入，也不调用模型 | 否 |
| 用户主动 skip / 默认无可压缩内容（显式受控绑定） | SessionCompactSkipped，含非空原因 | 否 |
| 无有效绑定或活跃上下文 | COMPACT_SESSION_UNAVAILABLE | 否 |
| 当前回合运行/另一独占操作 | COMPACT_BUSY；事件尚未进入 | 否 |
| 外部 Harness、未适配处理器 | COMPACT_UNSUPPORTED | 否，不能以 noop 假报支持 |
| 无平台私有调用状态而自行 trigger | COMPACT_UNBOUND_INVOCATION | 否 |
| 非法句柄、工具链、输出 | COMPACT_INVALID_RESULT；按 Bus 故障隔离规则恢复合法下游结果或报错 | 未通过最终校验的结果不安装 |
| compact 内重入、不同参数重复 next | COMPACT_REENTRANT / COMPACT_CONTINUATION_REUSED | 不重复执行摘要/安装 |
| 等待期间目标代次/消息变化 | COMPACT_STALE | 否，重新读取后再明确申请 |
| 模型请求/文件失败 | 明确压缩错误 | 安装前失败保持 live 原样；已发生模型费用不能撤回 |
| 安装前取消 | 传播 CancelledError，清理 draft | 否 |
| 安装/保存阶段取消或保存结果未知 | 有界收尾，必要时 COMPACT_COMMIT_UNCERTAIN 并暂停目标、重载核对 | 不能宣称未写入；不自动重试或伪造 skip |

连续两次调用是两次新压缩请求，不新增用户幂等 ID；一次事件内相同 next 调用复用候选，不重复模型请求。最终安装点校验版本，不能拿旧结果覆盖新对话。重复通知、历史边界和安装操作用平台私有 operation_id 去重，不把它添加进 Claude 结果。

## 5. 接入与兼容方案

实现按会话压缩能力归档到 `session_compaction/`：`dispatch.py` 管理事件分发、调用帧、消息转换与 CORE continuation；`service.py` 承接 Ops 并管理平台绑定及执行。`event_types`、`event_handlers`、`handler_ops` 继续保留现有定义/入口/登记。根目录不保留旧路径转发文件。此调整只改变内部路径，主流程桥接仍保持注释。

Core 保留显式压缩 Hook、候选准备/安装和 Processor 延迟副作用机制，供独立接口验证及后续接入。传统 compress_context、Context 直接压缩、自动 ADD/GET 的桥接代码均已注释，即使显式注册 Hook，这些原入口仍不改走扩展链。Core Extension Runtime 在既有包 Handler 包装中维护当前 Ops/Task/有效期；Server 注入只读 is_active_invocation 校验。EventBus、Ops 和 Builder 沿用最新上游接口，不增加筛选、身份或排除参数。Core 不依赖 Server 的 dataclass、EventBus 领域事件或 JSONL 服务。

Server 保留 `_bind_extension_compaction` 的预留实现，但其输入前、手动压缩等调用点已注释；忙碌门禁、清理接入及任务结束预计算调度也注释。生产装配只建立空的压缩服务和事件/Ops 注册，不绑定真实引擎。测试显式绑定时，Hook 转换消息/SessionContext，事件触发仍留在平台模块。event_types 只保留类型、校验、matcher 和注册，不把真实压缩通知/trigger 塞进事件定义模块。

| 层级 | 具体做什么 |
| --- | --- |
| ops.build CORE | 创建 Builder，共享能力目录 |
| ops.build BUILTIN（session_ops_build） | 从 next 取得 Builder，登记 compact 到平台压缩服务的代理；不执行压缩 |
| 业务事件 USER | 可以调用 compact Ops 发起请求；不是所有用量 USER 都会默认压缩 |
| session.compact USER | 按 trigger 筛选，补 instructions、修改消息、skip、自定义/改写候选 |
| session.compact BUILTIN | 本期不额外注册专用 BUILTIN；若平台已有匹配者，按 USER→BUILTIN→CORE 继续，可按相同规则处理候选 |
| session.compact CORE | 核对本次调用/固定会话与 trigger，调用私有默认 continuation 生成候选，返回结果；不直接改 live context |
| Core 协调器最终安装 | 在整条链结束后验证句柄/消息/版本，安装并保存真实结果；这是正式生效点 |

现有 command.compact 恢复原调用及响应结构，不透传本扩展 instructions/skip，不进入 SessionCompactionService；原算法和 JSONL 后处理照常执行。只有独立受控绑定测试才验证新增 instructions/skip 语义。

预计算配置及 `schedule_precompute` 机制预留，原任务结束处的调度调用已注释；当前即使将 `react.context_engine_config.compaction_precompute.enabled` 改为 True，也不会从生产主流程启动预计算。受控测试可显式调用该机制，ratio 要求 0＜ratio＜1。候选只存内存，每目标一项，包代次/模型/配置/会话结束等变化使它失效；前缀仍相同且追加消息完整时可以复用，实际压缩事件仍会再运行。

七查询和 append 的接口不变；本次 compact.messages 是 live 视图并带本次 handle，查询 messages 是产品历史且不带 handle。不要把旧历史查询的字段直接当作无损压缩消息。Server PR 依赖新的 Core PR，必须写明具体可用版本；不能仅靠 Server 改动宣称支持完整自动压缩和 skip。

## 6. 验收与未决问题

### 6.1 原实现验收记录

以下为 Core d816a0db / Server 5b495aa 旧实现的验收，不作为下述接口收敛版本的通过数。2026-11-03 已完成以下验收，运行与源码依据分别记录：

- Core 35 项定向验证通过：24 项压缩、3 项 Handler 身份/排除、8 项公共契约。覆盖 skip/post-next、自定义消息、文件与 Session memory 延迟、保存失败/恢复、取消收尾、预计算不占锁和尾部复用、原生/forked 适配及传统压缩入口。
- Server 压缩 10 项通过，生命周期相关 25 项通过；原 command.compact 7 项通过，包含 instructions 向原生 Adapter 传递。联合链实际使用 Bus/Builder、原生 ContextEngine 和受控摘要模型。
- 独立 Front/Supervisor/Worker 通过真实 ZIP 安装，使用 Qwen3.8-Flash 完成两个真实 query、manual post-next veto、真实默认摘要、plugin Ops、自定义 auto 安装与后续模型读取新标记。PID/创建时间、退出、端口关闭均核对；ui.render 0 次。
- 独立从停机后的 SQLite checkpoint 读取目标会话 typed pickle，以生产序列化器解码后交给全新 Session/ContextEngine 加载，恢复消息中保留了新标记。复核退出码 0。早期脚本的 raw-byte 文本搜索不适用于 Base64 保存；已修正检查器，并在同步最新 Server 装配后重新执行完整真实模型验收，主执行与独立存储复核均以 0 退出。旧失败记录仍独立保留。

本地证据：`D:\work_space\.codex-temp\session-compact-20261103\acceptance-verified.json`；实际 Worker/请求/Handler 结果：`real-155942`；源码指纹：real-final-exit.json。原始执行和独立存储复核分别保留；复核时运行源文件 SHA256 与实际验收时一致。远端 CI、Claude 实际宿主不冒称通过。

C-01～C-13 的主要边界已由上述定向、真实入口和存储恢复证据覆盖；角色范围限原生主会话。Server 扩展全组与 CLI 命令回归 841 项通过、2 项跳过；仅排除干净目标基线同样失败的 Token Weather 先短路用例，证据为 server-refreshed.xml。Core 大组回归累计 792 个不同用例通过（791 项整组通过，1 项 OpenBLAS 环境问题限定线程后复验通过）；Core 另排除 4 项 Windows 早期时间戳转换、1 项符号链接权限失败。这些基线问题和 Token Weather 短路已用干净目标基线复现，单独记录，不列为本特性已通过项或本次新增缺陷。

已知限制：第三方被选中 Processor 必须提供安全 fork；外部模型费用、trace 和用户自行执行的外部操作不因 skip 回滚；文件与 Session 不具备跨存储原子事务，保存未知需从权威状态重载。plugin 的计数只针对消息部分，原 command.compact 的完整窗口 stats 继续沿用原口径，二者数值不要求相等。无待确认产品范围项。

### 6.2 保持 EventBus 接口的修订验收（历史完整接入版本）

用户确认并要求实施：撤掉 EventBus 排除参数、Ops 身份属性和 Builder 参数改动。Core 同步 647c6d9aee4a4d6ae256263220705f17a3837569，Server 同步 23ba7697d8c9f3947b3bc3264209080246a3fd58；保留上游 session.start 包 matcher，Bus 只调用 trigger(Event("session.compact", input), context)。

Runtime 包装负责当前 Ops 对象、嵌套层、任务及活动期；compact 服务仅接受宿主注入的校验器，不开放用户填写。原事件 Handler 天然不匹配 compact；压缩内再调用同会话 Ops 在新事件前拒绝。同包其它匹配策略照常执行，不提供通用精确排除能力。

本修订 Core 定向 38 项、Server 定向 29 项通过；Server 全组与命令回归 845 项通过、2 项跳过、1 项既有基线失败排除。Core 完整回归 941 项通过、2 项跳过、7 项已复现基线失败排除（4 项 Windows 早期时间戳、1 项符号链接权限、2 项 store；并发锁路径属于偶发基线问题）。没有修改 store 代码。真实 Front/Supervisor/Worker 和 Qwen3.8-Flash 重新完成 manual veto/默认摘要、plugin Ops、auto 和后续模型读取新标记；新增子任务拒绝、同会话压缩重入拒绝，SQLite 全新 Session/ContextEngine 冷恢复及进程/端口清理均通过。

上述完整接入版本的历史证据：D:\work_space\.codex-temp\session-compact-eventbus-stable-20261103；真实入口 real-165733，acceptance-verified.json、real-final-exit.json、core-regression-final.xml 和 server-regression.xml。主执行与独立存储复核退出码为 0，运行源 SHA256 一致，ui.render 0 次。最初测试启动器工作目录错误的失败工件保留；最终结果来自修正后完整重跑。原 6.1 的结果保留原版本边界，未把远端 CI 列为通过。

### 6.3 当前接口阶段验证

对应本次“主流程接入注释、真实压缩暂不接入”的工作区修订。Core 31 项定向、139 项原生 ContextEngine / ContextModel / FullCompact 回归通过，共 170 个不同用例；Server 85 项 compact / start / 原命令回归及新增真实安装包用例 1 项通过，共 86 个不同用例。未排除失败用例。

覆盖默认活动 Ops 的 NOT_INTEGRATED、消息/提交/任务不变、输入前不绑定、原命令保持旧签名、原生 ADD/GET/直接压缩不进入已注册 Hook，以及显式受控绑定下 USER→BUILTIN→CORE 往返、正文规则、候选/skip 和调用有效性。当前阶段不执行生产真实压缩、真实模型或 SQLite 压缩恢复，不把第 6.1/6.2 的旧结果累计为本轮通过。

证据目录：`D:\work_space\.codex-temp\session-compact-interface-only-20261103`，Core 为 core-interface-final.xml / core-native-regression.xml；Server 为 server-interface-final.xml / server-installed-package.xml。早期启动遗漏 asyncio_mode 及两个新增测试断言不准确的失败记录保留，修正测试环境/断言后受影响范围全部重跑通过。远端 CI 不列为通过。恢复任何生产接入点后，必须重新完成对应业务入口、真实模型、保存及冷恢复验收。

### 6.4 实现目录迁移验证

按用户确认的目录方案，将原 compact_dispatch.py / compact_service.py 迁至 session_compaction/dispatch.py / service.py，增加最小 __init__.py；根目录不保留旧模块转发。装配、事件校验、CORE Handler、测试均改为新导入路径，事件校验的延迟导入继续保留。公开事件类型、Ops 名称、字段、主流程注释及默认 NOT_INTEGRATED 行为不变。

迁移后 compact、session.start 和原压缩命令回归合跑 **86 项通过**，无跳过或排除；新进程导入、旧模块不存在以及 setuptools 对新子包的发现均通过。两份 HTML 同步当前路径，历史固定提交的源码链接明确保留历史布局。Core 仅同步跨仓方案文档路径，不修改运行代码；本次不重复运行 Core 业务测试或生产真实压缩。

证据：`D:\work_space\.codex-temp\session-compact-package-layout-20261103` 中 server-regression.xml、package-verification.json 和 delivery-verification.json。

## 7. 参考资料

- [Core PR !125](https://gitcode.com/huenrui_/agent-core/pull/125) 与 [Server PR !109](https://gitcode.com/huenrui_/jiuwenswarm/pull/109)；先合入 Core，依赖提交 `203523d2022313df5c0ce2472baeeedac38eb643`。

- [固定 Claude Code Mods 类型声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)：SessionCompactArgs/Input/Result/Trigger、SessionMessage、ToolUseSummary、ToolResultSummary。
- [session.append 文档格式基线](F_16_session-append.md)、[七项查询共享协议](../specs/S_08_session-query-events.md)、[历史查询](F_18_session-messages.md)。
- [EXTV2-26 开发计划](../designs/extensions-v2-development-plan.md#extv2-26-会话压缩与压缩前扩展)。
- Core 联合方案稳定 ID：D-20261103-1156-session-compaction-events；文件位于 Core `.agents/agent-docs/designs/active/D-20261103-1156-session-compaction-events/design.md`。它维护具体 Core API、draft/提交/恢复设计和 C-01～C-13 验收条件。
