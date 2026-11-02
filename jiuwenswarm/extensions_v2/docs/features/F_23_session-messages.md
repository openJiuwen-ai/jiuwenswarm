# session.messages / ops.session.messages 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_23 / session-messages |
| 归档日期 | 2026-10-29 |
| 后续修订 | 2026-10-31，拆分独立查询特性、按事件命名；本次以 session.append 的七章结构补齐字段、接入实例与函数链，契约不变；2026-11-02，按原 session.append 对象示例、字段表及业务 Handler 写法统一说明，契约不变；同日补充查询结果的资料来源、读取函数、复用与新增边界及缺失处理，契约不变 |
| 关联计划 | [EXTV2-22 / 七项只读查询事件化](../designs/extensions-v2-development-plan.md#extv2-22-七项只读查询事件化) |
| 主责规约 | [S_16 查询公共协议](../specs/S_16_session-query-events.md)、[S_06](../specs/S_06_session-history-ops.md) |

## 1. 范围、状态与依据

最近修订 2026-11-02。本文仅维护 `ops.session.messages()` 与同名查询事件 `session.messages` 的使用说明、接入和验证边界；一个方法与一个查询事件组成一项特性。开发状态、负责人和交付统一见[开发计划 EXTV2-22](../designs/extensions-v2-development-plan.md#extv2-22-七项只读查询事件化)，交付 [PR !100](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/100)。

公共协议主责为 [S_16](../specs/S_16_session-query-events.md)，专项字段主责为 [S_06_session-history-ops.md](../specs/S_06_session-history-ops.md)；下文表格解释怎样填写、怎样消费，不另立契约。共享实现和既有验收记录见[查询公共机制与七项特性索引](../designs/session-query-events.md)。

2026-10-31 按单个 Ops / Event 拆分特性文档；原合篇只作为文档组织历史，不代表仍需一起调用。此次拆分没有改变运行时契约或实现。

## 2. 能力说明与 Claude 对照

`session.messages` 是这一次查询调用的拦截事件。方法代理验证参数和已绑定会话后触发它；查询 Handler 接收下述参数，可继续默认处理、增加前后操作、返回合法替代视图或明确拒绝。默认 CORE 读取平台事实，结果返回原方法调用者；它不表示状态发生变化，也不自动写历史或显示到界面。

`await ops.session.messages()`：读取当前主会话已提交历史的消息视图，适合生成最近对话摘要或做返回副本的文本加工。最多返回最新 4096 条，数组从旧到新；不是活跃模型上下文的完整系统提示或产品原始 JSONL 行。

| 项 | 本次实现 | 与固定 Claude 定义的关系 |
| --- | --- | --- |
| 事件输入 | 无业务选项，Event 输入为空映射 {} | 以同名查询 OpEvent 为扩展点；会话身份独立绑定，不作为可改写参数 |
| 默认数据来源 | read_session_messages_view(session_id)：排空已受理写入边界并读取一致历史，投影用户/助手与可配对工具结果。 | 使用本地真实平台服务或绑定快照，不伪造 Claude 私有宿主数据 |
| 方法公开返回 | list[dict] | role/text/toolUses/toolResults 和 4096 上限对齐 Claude。默认来源是平台已提交主会话产品历史的消息投影；排除用量、UI notice/progress、子会话等行，不宣称等于当前活跃模型完整上下文。 |
| 改写与拒绝 | 可 next、短路合法 value、返回合法 deny | 只影响本次查询结果，不修改平台会话事实或授权范围 |

对照依据是 Claude Code Mods 2.1.277 的固定公开类型声明，提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81`；不是运行 Claude 私有宿主的结果。

## 3. 事件契约

### 3.1 触发前后与粒度

```text
平台业务入口先收集可信会话资料，触发外层事件并传入 SessionContext
  → Core 为实际用户 Handler 绑定本次 Ops
  → 用户 await ops.session.messages()
  → 方法代理读取 ops.session_context，规范化参数，校验会话与重入
  → await Worker bus.trigger(Event("session.messages", arguments), context)
  → 查询 USER Handler：可 next、加工合法返回视图或明确 deny
  → await next_handler(event)
  → 共享查询 BUILTIN 开始计时并继续
  → CORE 按 session_id 调用 read_session_messages_view，读取已提交主历史并生成消息数组
  → {value: 结果} 经 BUILTIN／USER 返回；或 USER 返回 {deny: 原因}
  → 代理校验、复制并解包；原 Ops 调用者得到值或明确错误
  → 用户消费结果，再 next 原外层事件
```

一次 Ops 调用发起一次本查询；前置校验未通过时不触发。同一外层 Handler 调用两次会读取两次；只有订阅、没有调用时不会自动查询。查询是等待期间另起的一条链，返回后原 Handler 从 await 后继续。每个匹配 USER 只有调用 next 才进入后续 Handler；合法短路可不进入默认 CORE。

### 3.2 输入：每个字段表达什么、由谁填写

实际类型是 `openjiuwen.core.events.Event`，event_input 为查询映射。下面展示 Ops 代理生成的对象形态；普通调用者使用第 4.1 节的方法即可。平台会话上下文通过 trigger 的独立参数传递。

```python
from openjiuwen.core.events import Event

query_event = Event("session.messages", {})
```

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| event_type | str，必有；固定为 session.messages | 本次查询的事件类型，Bus 据此选择处理链 | **Ops 代理填写**；示例为 "session.messages"，不填入参数映射 | 只读；保持当前类型 |
| event_input | 只读 Mapping，必有 | 本次查询选项，不包含目标会话、目录或模型快照 | **Ops 代理填写**；{} | 只能保持空映射；没有可改写的业务选项 |

本事件没有额外业务字段，event_input 必须为 {}。用户调用是 `await ops.session.messages()`，括号留空；不能传 session_id、路径、空字典或其他选项。

#### 随调用绑定的会话资料

这些资料由平台业务入口收集，Core 绑定到本次 Handler 的 Ops。用户读取 `ops.session_context` 即可；它是只读属性，不需要 await，也不负责采集或组装上下文。查询参数和返回视图都不能替换这份可信资料。

| 字段 | 类型 / 是否必有 | 含义与应该填写什么 |
| --- | --- | --- |
| ops.session_context | Core 只读属性，查询需要非 None | 本次 Handler 的可信会话资料入口。平台把上下文随外层 Event 交给 Bus，Core 绑定到实际 Handler 的 Ops。填写／示例：用户不用填，也不用 await；直接读取 ops.session_context。用途：代理取出这份资料，随查询 Event 的独立 context 参数继续传递。缺失或错填：缺少上下文时查询报错；不要把用户输入 payload 包装成平台可信上下文 |
| SessionContext.session_id | 平台验证过的非空 str，必需 | 本次绑定的是哪个会话。平台业务入口提供；用户普通调用不填写。填写／示例：平台例如绑定 session-a；查询方法和 Event 输入里都不再填 session_id。用途：读取相应会话的历史、保存模型或用量服务；同时参与隔离和循环保护。缺失或错填：缺失/非法不能查询；返回值或 Event 输入不能把目标改成另一个会话 |
| 额外目录 / 模型 / 历史快照 | 本方法不要求 | 只需绑定有效会话身份即可定位历史。历史由平台服务按会话 ID 读取。填写／示例：用户不用把消息数组、cwd 或模型放入 SessionContext。用途：避免调用方为了读消息先触发用量或追加消息。缺失或错填：没有完整历史资料在上下文中不会阻止按会话 ID 读取 |
| current_query_origin().package | str 或 None；helper 本身也可返回 None | 谁发起了本次查询，而不是当前查询 Hook 属于哪个包。Server 分派器从原 Ops 的包身份记录；不是用户传参。填写／示例：先 origin = current_query_origin()，非 None 时再读 origin.package。用途：用于用户自己的审计和来源判断；它不是 Core next.origin 或授权证明。缺失或错填：直接宿主触发而没有 Ops 调用域时 helper 可为 None；不要无判空访问 .package |

Bus 保留会话身份并生成规范化只读副本；外层和查询 Handler 不要求持有同一个 Python 对象。原查询包身份由分派器记录，不能用当前 Hook 所属包冒充原调用者，也不能把来源审计当作授权证明。

### 3.3 输出：默认值、替代结果与拒绝

默认 CORE 返回恰含 value 的映射；示例为 Python 对象，None/True/False 在 JSON 中分别对应 null/true/false：

```python
{'value': [{'role': 'user', 'text': '请读取 README.md', 'toolUses': []},
           {'role': 'assistant',
            'text': '我来读取项目说明。',
            'toolUses': [{'tool_use_id': 'call-read-1',
                          'tool': 'Read',
                          'input': {'file_path': 'README.md'},
                          'text': '项目说明',
                          'result': {'text': '项目说明'}}]},
           {'role': 'user',
            'text': '',
            'toolUses': [],
            'toolResults': [{'tool_use_id': 'call-read-1',
                             'text': '项目说明',
                             'isError': False,
                             'result': {'text': '项目说明'}}]}]}
```

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| value | list[dict]，最多 4096 项；成功时唯一顶层键 | 最近保留的用户/助手消息投影，不是原始 JSONL 行、分页对象或活跃模型的全部上下文。CORE 读取已提交主会话历史并生成独立消息视图。填写／示例：无消息填 []；有消息按旧到新排列，例如 [{"role":"user","text":"你好","toolUses":[]}]。用途：生成近期摘要；最近五条用 messages[-5:]，最后一条先判非空再取 messages[-1]。缺失或错填：无可读历史用 []，不是 None；I/O 或解析故障不能伪装为空数组；不再填写 limit/cursor。Ops 校验、独立复制并解包；普通调用者拿到 value 本身，查询 Handler 从 next 得到完整封装。 |
| deny | 非空 str；拒绝时唯一顶层键 | USER 如需明确拒绝，返回 {"deny":"QUERY_DISABLED"}。原字符串最多 512 字符，去首尾空白后须非空；不得与 value 同时出现。代理抛 SessionQueryDenied，code=DENIED、reason 保留原原因。抛普通异常不等于明确拒绝。 |

#### value 中每条消息的全部字段

| 字段 | 类型 / 默认值 | 含义、实际取值和填写方式 | 可修改范围 |
| --- | --- | --- | --- |
| [i].role | user 或 assistant 字符串，每条必有 | 这条消息在模型消息视图中的发送角色；不等同于原产品历史行 role。历史消息角色投影；独立工具结果行投影成 user。填写／示例："user" 或 "assistant"。用途：按用户输入和助手回复分组；工具结果通过 toolResults 表达。缺失或错填：不能省略或填 system/tool 等其他值；不要用昵称、Agent ID 代替角色 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].text | str，每条必有；可为空字符串 | 人能阅读的普通正文；图片和工具结构不会被强行序列化到这里。原消息的文本块拼接。填写／示例："请读取 README.md"；没有普通文本时填 ""。用途：摘要、检索或正文展示；工具调用参数从 toolUses 读取。缺失或错填：无文本为 "" 而非 None；不要把整条 JSON 记录、列表或图片对象填进 text | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].toolUses | list[dict]，每条必有 | 本条消息记录的工具调用及可配对结果，不是当前可用工具清单。默认从助手消息里的结构化工具调用提取。填写／示例：没有调用填 []；有调用填下述对象数组。用途：查看助手调用了哪些工具、输入是什么、是否已有结果；读取不会再次执行工具。缺失或错填：没有也必须保留 []；旧记录缺失调用 ID/名称/参数时不编造调用 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].toolResults | list[dict]，可缺省，只允许出现在 user 消息 | 作为模型输入呈现的工具响应，可与前面的助手调用配对。用户侧工具结果块或独立工具结果行。填写／示例：有结果时填下述结果对象数组；没有时省略。用途：查看工具回答了哪次调用；与普通用户正文区分。缺失或错填：没有时可省略，不强制补 []；assistant 消息不能增加这个字段 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |

#### toolUses 中每次调用的全部字段

| 字段 | 类型 / 默认值 | 含义、实际取值和填写方式 | 可修改范围 |
| --- | --- | --- | --- |
| [i].toolUses[j].tool_use_id | 非空 str，调用对象必有 | 一次具体工具调用的关联标识；不是消息 id、会话 id 或工具名。原始工具调用 ID。填写／示例："call-read-1"，并与对应 toolResults 项使用相同 ID。用途：把工具请求与响应配对，避免同一工具多次调用混淆。缺失或错填：不能省略、填空字符串或用另一条调用的 ID 占位 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].toolUses[j].tool | 非空 str，调用对象必有 | 这次调用使用的工具名称，不是给用户看的操作说明。原始工具名称。填写／示例："Read"、"Bash" 或实际的 MCP 工具名。用途：标注或按工具类型汇总调用；字段本身不注册工具或授予调用权限。缺失或错填：不能省略或填空；不要拿工具 ID、会话 ID 代替名称 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].toolUses[j].input | 可复制 JSON 对象，必有 | 这一次调用传给工具的参数对象，不是工具返回值或字符串化 JSON。原结构化工具参数。填写／示例：{"file_path":"README.md"}；无参数的调用填 {}。用途：解释某次工具操作针对什么目标；消费方按工具自身参数定义读取。缺失或错填：不能填 None 或普通字符串；不要猜补原记录未保存的参数或加入对象实例 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].toolUses[j].text | str，可缺省 | 该调用已观察到的结果文字，不是原调用前的助手正文。已配对工具结果的可读文本。填写／示例：结果确有文字时填 "项目说明"；结果文本确为空时填 ""。用途：在工具调用旁显示结果摘要；不能仅靠缺省判断工具成功或失败。缺失或错填：尚无结果文字时省略键，不用 None 或虚构“执行成功”占位 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].toolUses[j].result | 可复制的 JSON 值，可缺省 | 结构化结果，与面向阅读的 text 分开。存档中已保存且可公开的工具结果。填写／示例：例如 {"text":"项目说明"}；也可为实际保存的数组或标量。用途：让用户业务按结构处理结果；不会反向执行或更新工具状态。缺失或错填：未知时省略；只有真实结果就是 JSON null 时才填 None，不用 None 代表“还没读到” | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].toolUses[j].isError | 仅 True，可缺省 | 该调用已观察到错误；与 toolResults 中必填布尔字段规则不同。确知该工具调用的结果报错。填写／示例：确知错误填 True；成功或未标记错误时省略该键。用途：标记错误调用，提醒用户业务检查结果。缺失或错填：不要填 False、None 或字符串；此字段不存在不能证明工具已经完成 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |

#### toolResults 中每条结果的全部字段

| 字段 | 类型 / 默认值 | 含义、实际取值和填写方式 | 可修改范围 |
| --- | --- | --- | --- |
| [i].toolResults[j].tool_use_id | 非空 str，结果对象必有 | 这份结果属于哪一次工具请求。它所回答的原调用 ID。填写／示例："call-read-1"，对应 toolUses 中同一调用。用途：跨消息关联请求和结果；不能仅按工具名配对。缺失或错填：不能省略或为空，也不能拿消息行 uuid 替代 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].toolResults[j].text | str，结果对象必有；可为空字符串 | 模型能阅读的工具返回正文。工具结果的文本块拼接。填写／示例："项目说明"；结果无可读文本时填 ""。用途：查看或摘要结果；结构化处理优先读取可得的 result。缺失或错填：无文字仍保留 ""，不能省略或填 None | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].toolResults[j].isError | bool，结果对象必有 | 这条工具响应是否标记为错误；它与 toolUses.isError 的可省略 True 规则不同。已保存的工具结果状态。填写／示例：明确报错填 True，否则按已保存状态填 False。用途：区分正常工具响应和错误响应。缺失或错填：不能省略，不能填字符串 "false" 或 0/1；不要把状态未知硬写成成功 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |
| [i].toolResults[j].result | 可复制 JSON 值，可缺省 | 这份工具响应的机器可读结果。存档中的公开结构化工具结果。填写／示例：例如 {"text":"项目说明"}；仅填真实保存的结果。用途：按业务字段读取结果，而不是重新解析展示文本。缺失或错填：存档没有时省略；不填 UI 进度、内部元数据或编造结果 | 可提供符合结构的本次返回视图；不改写平台存储和真实状态 |

不返回的字段：handle/id/uuid/request_id/产品 extra（本消息视图没有这些键）：公开消息视图与原始产品历史行、compact 专用 handle 是不同结构。不由 messages 查询返回。填写／示例：不要增加或访问这些键。用途：要核对追加记录身份时使用 append 的真实回执；消息查询只消费约定的角色、正文和工具字段。缺失或错填：访问不存在的键可能 KeyError；不要把缺失理解成历史记录没有真实身份

`toolUses[].isError` 只允许出现 True；`toolResults[].isError` 则是必须填写的 True/False。两种字段不能套用同一缺省规则。可选结构化 result 缺失与真实 JSON null 也不同；查询返回的是独立消息投影，不能直接拿它和 append 回执中的 history_record 逐字段比较。

成功／拒绝只能保留表中对应的一个顶层键，不增加其他键，也不能直接返回裸值。 本方法的 value 不能为 None；没有可读消息时返回 {"value": []}。

正常继续返回 `await next_handler(event)` 的完整结果，不要再包一层 value。USER 可在 next 前返回合法 value 短路，或在 next 后加工自己的返回副本；真实平台资料、授权目标及后续默认读取不随之改变。

USER／BUILTIN 的普通异常或非法输出沿用 Core 故障隔离，可能继续原有效链或保留真实下游结果；不能靠抛异常或错填可靠拒绝。默认 CORE／框架读取故障和取消沿等待关系传播。多次 next 可能重复读取，不承诺一次调用内结果缓存。

### 3.4 注册、筛选、改写示例

本例要做的事情是：**对返回消息的 text 做手机号脱敏，原历史保持不变**。下面是扩展入口模块 `extension.py`；由已有 manifest 指定该模块并通过管理入口安装即可，包格式参照 [session_query_audit](../../examples/session_query_audit/manifest.yaml)。

```python
class Extension:
    def __init__(self):
        self.parameters = {}

    def register(self, registrar):
        registrar.register_event_handler(
            "session.messages", self.scrub_messages, {}, "user",
        )
        registrar.register_event_handler(
            "context.usage.updated", self.on_usage, {}, "user",
        )

    async def scrub_messages(self, event, ops, next_handler):
        result = await next_handler(event)
        if "deny" in result:
            return result
        messages = [
            {**message, "text": message["text"].replace("13800138000", "[phone]")}
            for message in result["value"]
        ]
        # Other message fields and the persisted history keep their original values.
        return {"value": messages}

    async def on_usage(self, event, ops, next_handler):
        session_id = ops.session_context.session_id
        value = await ops.session.messages()
        self.parameters[session_id] = {"messages": value}
        return await next_handler(event)


def create_extension():
    return Extension()
```

平台历史中消息 text 为“联系 13800138000”时，查询 USER 返回“联系 [phone]”；外层把数组保存到自己的 parameters。用户、助手角色以及工具字段原样保留。本例只处理消息的 text，不宣称脱敏工具参数、结构化 result 或所有敏感信息；JSONL 和活跃模型上下文均不改写。

Matcher 为等值条件合取，空字典匹配该事件全部合法输入。本事件输入为空映射，没有会话 ID 或输出字段可用作输入 matcher；用户规则可读取绑定上下文或默认结果。 查询 Handler 中的 next 继续当前 `session.messages` 链；在其中再次调用同名 Ops 会报 QUERY_REENTRANT。

安装只发布订阅。下一次平台真实发布 `context.usage.updated` 并绑定会话时，on_usage 才调用 Ops、等待查询链、保存本扩展的 parameters，再 next 原外层事件。这里不要求 UI 渲染；`self.parameters[session_id]` 是本例可观察的消费结果，属于用户扩展自己的内存状态。其他由平台绑定了所需上下文的事件也可调用同一 Ops，不要求用户先调用 notify 或修改 Adapter。

## 4. 方法契约

### 4.1 ops.session.messages()

推荐调用（写在已由平台绑定 SessionContext 的业务 Handler 中）：

```python
messages = await ops.session.messages()
recent = messages[-5:]
texts = [message["text"] for message in recent]
```

| 输入字段 | 必填 / 默认 | 应填写的内容、范围及示例 |
| --- | --- | --- |
| 方法参数 | 无参数 | 查询目标由当前 Ops 已绑定的会话上下文确定。普通调用者不填写参数。填写／示例：await ops.session.messages()。用途：直接读取这次会话的视图；不需要另填会话 ID 或路径。缺失或错填：括号留空；不要调用 ops.session.messages({})，也不要传 None/session_id/cwd；空 {} 仅由代理生成在 Event 输入中 |

本方法没有用户参数，调用括号留空；`ops.session.messages({})` 也非法。Core 自动注入的当前 Ops 不由用户填写。

成功返回 **list[dict]**，直接返回值及每个字段的含义见第 3.3 节。代理把方法参数规范化为 Event 输入，从当前 Ops 取得已绑定上下文，另起 `session.messages` 查询；事件的 {value: 结果} 经校验、复制后解包为方法返回，{deny: 原因} 转为 SessionQueryDenied。目标只来自 `ops.session_context.session_id` 及所需可信资料，不从用户参数或返回视图选择另一会话。

主要失败：没有 SessionContext 抛 RuntimeError；方法／事件参数非法报 TypeError，非法会话 ID 沿平台会话服务原契约拒绝；显式拒绝报 SessionQueryDenied（code=DENIED）；活跃同名或间接循环报 QUERY_REENTRANT，嵌套深度超过 16 报 QUERY_DEPTH_EXCEEDED。读平台服务的原始错误继续传播，取消保持 CancelledError。 用户可在调用 Ops 处捕获错误；未捕获时仍受外层事件自身隔离规则约束，不能把外层回合继续当成查询成功。

### 4.2 旧 Handler / 返回值迁移

删除 limit/cursor 参数和 page 包装；page['messages'][0]['content'] 改为 messages[-1]['text']。只需少量条目时在返回数组切片；产品原分页服务保留在平台内部，不作为新的第八个 Ops 暴露。

本次文档结构与说明调整不改变上述兼容边界。执行目录、模型、Git、历史和用量事实仍由平台服务维护；对返回视图的改写不成为另一项平台操作的可信输入。

| 旧调用 / 容易混淆的用法 | 现在怎样写 | 对使用者的影响 |
| --- | --- | --- |
| messages(limit=5, cursor=...) | 改为 messages()，需要少量条目在返回数组上切片 | 无分页入参；最多最新 4096 条，从旧到新 |
| page["messages"][0]["content"] | 改用数组项的 role/text/toolUses/toolResults | 返回不是 page，也不是产品 JSONL 原始行 |
| 读取历史前先 append | 直接查询当前绑定会话 | 不要求先写入，不触发 session.append，也不修改活跃模型上下文 |

## 5. 接入与兼容方案

<a id="51-启动登记上下文绑定与-core-分工"></a>

### 5.1 启动登记、上下文绑定与 CORE 分工

event_types/session_queries.py 定义查询映射、Matcher、Validator 并注册同名事件及唯一 CORE；handler_ops/session_queries.py 创建封闭代理；query_dispatch.py 负责上下文、来源、重入保护、trigger 和结果解包；event_handlers/session_queries.py 提供共享查询 BUILTIN 与 CORE；实际读取由 server/runtime/session/query_views.py 及所属平台服务负责。

启动阶段，Registry.init 触发一次 ops.build：其 CORE 创建 Builder，平台 BUILTIN 从 next 取得同一 Builder，登记七个 session 查询代理，Bus 保存方法目录。业务阶段，Core 在实际进入每个 Handler 前，用该目录、可信包身份和本次 SessionContext 创建 Ops；方法访问器自动注入当前 Ops。Builder 保存方法定义，查询结果在每次调用时产生。

现成平台通知入口的调用形态如下（**平台代码示意**；变量来自真实回合，用户扩展不负责采集）：

```python
notify_context_usage(
    update,
    cwd=cwd,
    project_root=project_root,
    model_info=model_info,
    context_usage=context_usage,
)
```

notify_context_usage 把会话 ID、可信目录与可得快照整理为 SessionContext，调度 _dispatch；后者调用 `bus.trigger(Event("context.usage.updated", update), context)`。用户 on_usage 使用 Core 已绑定到 Ops 的同一份会话资料。这个通知仅是现成示例入口；平台其他业务已绑定了有效上下文时，同样可以调用查询 Ops，无需先执行用量通知。

业务查询的默认 CORE 落点：**CORE 按 session_id 调用 read_session_messages_view，读取已提交主历史并生成消息数组**。CORE 不回调公共同名 Ops，以免递归；查询 USER 执行本包规则，共享 BUILTIN 计时、记录原调用来源并复制结果，然后反向返回。它与启动时创建 Builder 的 ops.build CORE 是两种不同职责。

完整业务过程：平台发布外层事件并绑定上下文 → on_usage 调用 `ops.session.messages()` → 查询代理触发同名事件 → `scrub_messages` 介入 → next 进入 BUILTIN／默认 CORE → 返回查询结果并应用本例规则 → 代理解包 → 用户保存 parameters → next 继续原外层事件。具体可观察效果见第 3.4 节；第 4.1 节是独立调用法，不要求安装其他六项示例。

默认查询不写平台业务数据、不发模型请求、不自动渲染，也没有全局结果缓存。对返回视图的加工不会成为其他平台操作的可信输入。用户包卸载／重载撤销自己的 Hook，不撤销平台事件、CORE 或共享 Ops；用户自有内存参数与缓存的保留由用户扩展管理。

七项联合示例见 [session_query_audit](../../examples/session_query_audit/)；其中 collect_session_parameters(ops) 是普通用户辅助函数，分别调用七个 Ops 后返回参数字典。它与本文单项业务规则的示例分别维护。共享机制、来源及取消边界见[公共设计第 5 节](../designs/session-query-events.md#5-接入与兼容方案)。

源码位置：[事件与校验](../../event_types/session_queries.py)、[Ops 登记](../../handler_ops/session_ops.py)、[查询代理](../../handler_ops/session_queries.py)、[分派](../../query_dispatch.py)、[查询 CORE／BUILTIN](../../event_handlers/session_queries.py)、[平台读取](../../../server/runtime/session/query_views.py)、[现有平台通知入口](../../event_types/context_usage.py)。

### 5.2 查询结果如何获取

以下说明查询 USER 正常 `await next_handler(event)` 后，默认 CORE 如何取值。公开 `ops.session.messages()` 仍先经过同名 `session.messages` Event；`ops.session_context` 是 Core 绑定的只读属性，不负责采集资料。USER 短路返回合法 value 时不会执行下列默认读取；USER 在 next 返回后加工 value 时，Ops 拿到的是加工后的视图。

```text
SessionContext.session_id
  → to_thread(read_session_messages_view, session_id)
  → read_committed_main_history(session_id)
  → 已有写入队列边界 / 文件锁 / 主历史 JSONL 或兼容 JSON
  → 过滤非消息记录 → message_from_record / message_text
  → 投影并保留最近 4096 条 → 按旧到新返回消息数组
  → Event 包装 value → Ops 返回 list
```

| 阶段 | 具体从哪里获取 | 如何形成当前结果 |
| --- | --- | --- |
| 查询身份 | 只读取 `context.session_id`；默认 CORE 不要求 cwd、模型或用量快照 | 用户调用 `ops.session.messages()` 不传 ID、limit 或 cursor；主历史由平台根据绑定会话定位 |
| 读取已提交历史 | [read_session_messages_view](../../../server/runtime/session/query_messages.py) 调用 [read_committed_main_history](../../../server/runtime/session/session_history.py) | 后者先等待已入队写入的提交边界，再在文件锁内按平台路径读取主历史 JSONL／兼容 JSON；不创建会话或追加消息 |
| 过滤与消息解释 | 排除用量、进度、通知等非消息事件，以及子 Agent／成员记录；由 `message_from_record`、`message_text` 解释原记录 | 仅投影当前主会话可用的 user/assistant 消息视图；system/attachment 不进入该返回数组，工具结果行转换为带 toolResults 的 user 视图 |
| 输出转换与上限 | 生成 role/text/toolUses，存在工具结果时加 toolResults；按 request_id 与 tool_use_id 配对工具结果；最后取最新最多 4096 条，深复制返回 | 返回仍按旧到新排列，如 `[{"role":"user","text":"你好","toolUses":[]}]`；不是旧分页对象，也不是把 JSONL 原记录直接暴露出去 |

**复用与新增：** 原仓库已有 `load_history_records`、`read_history_cursor_page`、`read_session_history_records` 等历史查询入口，见 [session_history.py](../../../server/runtime/session/session_history.py)。PR-04 增加扩展历史 Ops 适配，PR-05 在既有存储／队列／锁上新增严格的 `read_committed_main_history`；事件化改造新增 `read_session_messages_view` 的过滤、工具字段关联及数组投影。当前 Ops 不调用旧分页接口，不是原仓库此前没有消息查询。

**缺失与失败：** 未知会话、已有会话没有主历史文件，或历史中没有合格消息，均返回 `[]`。读盘失败、提交边界等待超时或历史损坏报 `HISTORY_IO_FAILED`；消息解释非法报 `INVALID_TRANSCRIPT_MESSAGE`。查询结果是当次已提交历史视图，不含尚未持久化的流式片段。

共享查询 BUILTIN 只计时、记录调用来源并复制结果，具体读数由默认 CORE 委派平台服务完成；CORE 将结果包成 `{"value": ...}`，Ops 代理再校验、复制、解包。结果返回调用它的用户 Handler，是否保存为自有参数由用户决定，不自动写历史或进入 UI。

## 6. 验收与未决问题

### 6.1 本特性的独立验收场景

| 场景 ID | 输入与前置 | 操作 | 预期结果 |
| --- | --- | --- | --- |
| F_23-V-01 | JSONL 含用户、助手、可配对工具记录和 UI 行 | 调用 messages() | 返回 role/text/toolUses/toolResults 投影，排除 UI 行 |
| F_23-V-02 | 历史超过 4096 条，或尚无可读历史 | 调用 messages() | 返回最新 4096 项并按旧到新排列；无历史为 [] |
| F_23-V-03 | 安装仅拦截 session.messages 的用户包 | 分别 next、短路 value、显式 deny | 默认值 / 合法替代值 / SessionQueryDenied 分别可观测 |
| F_23-V-04 | 不同会话绑定不同上下文；或在 Hook 中同名递归 | 分别调用本方法 | 会话隔离；活跃同名或间接循环报 QUERY_REENTRANT，不能无限递归 |
| F_23-V-05 | 读取失败、Hook 异常或查询取消 | 分别构造这些分支 | 按第 3.3 / 4.2 节区分隔离、明确拒绝、平台错误与取消，不把失败当成功空值 |

对应事件化用例位于 [test_session_query_events.py](../../../../tests/unit_tests/extensions_v2/test_session_query_events.py)：包含七项默认调用、各项短路 / 拒绝、非法参数、查询顺序、隔离、循环 / 取消及实际用户包装载卸载。每个场景只验证本方法承诺，不把其他六项也作为调用前置。

### 6.2 已有实施证据与本次文档调整

实现验证基于本 PR 实现提交 `f313f513ee9b`、已合入 Server 基线 `c6a07f909` 与 Core `bdde48a8`，Python 3.11。扩展 632 项、相关平台 140 项及独立进程 ZIP / notify / 七项参数返回验证的实际范围、命令和限制统一保留在[公共设计第 6 节](../designs/session-query-events.md#6-验收与未决问题)。这属于查询事件化实现的既有证据；这些数量属于实现阶段历史运行。本次按 session.append 对齐文档的结构和说明，运行代码不变；只核对字段、示例、源码链路、链接与差异，不声称重新执行业务或真实模型测试。

role/text/toolUses/toolResults 和 4096 上限对齐 Claude。默认来源是平台已提交主会话产品历史的消息投影；排除用量、UI notice/progress、子会话等行，不宣称等于当前活跃模型完整上下文。 该限制属于已知支持边界；此轮未增加新的运行时待决项。未执行 Claude 私有宿主、新模型 API、浏览器或 Front/Supervisor 跨进程验收，不能由文档完整推断这些验收已完成。

2026-11-02 文档整理：以 2026-10-31 的 session.append 原件为参照，统一输入对象示例、字段表列式、输出嵌套字段、可运行 USER 业务案例、Ops 参数与迁移说明。运行代码、公共字段与既有历史验收证据保持；本轮仅验证文档结构、示例行为、类型／字段、链接及差异，不作为业务、模型或浏览器测试的新结果。

## 7. 参考资料

- [专项字段规约 S_06_session-history-ops.md](../specs/S_06_session-history-ops.md)
- [S_16 查询公共协议](../specs/S_16_session-query-events.md)
- [七项独立特性索引与共享实施记录](../designs/session-query-events.md)
- [Claude 会话方法](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude 同名 OpEventOf 查询事件](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [内置事件开发指南](../designs/builtin-event-development-guide.md)
