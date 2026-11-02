# session.usage / ops.session.usage 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_21 / session-usage |
| 建档日期 | 2026-10-28 |
| 后续修订 | 2026-10-31，拆分独立查询特性、按事件命名；本次以 session.append 的七章结构补齐字段、接入实例与函数链，契约不变；2026-11-02，按原 session.append 对象示例、字段表及业务 Handler 写法统一说明，契约不变 |
| 关联计划 | [EXTV2-22 / 七项只读查询事件化](../designs/extensions-v2-development-plan.md#extv2-22-七项只读查询事件化) |
| 主责规约 | [S_16 查询公共协议](../specs/S_16_session-query-events.md)、[S_11](../specs/S_11_session-usage-ops.md) |

## 1. 范围、状态与依据

最近修订 2026-11-02。本文仅维护 `ops.session.usage()` 与同名查询事件 `session.usage` 的使用说明、接入和验证边界；一个方法与一个查询事件组成一项特性。开发状态、负责人和交付统一见[开发计划 EXTV2-22](../designs/extensions-v2-development-plan.md#extv2-22-七项只读查询事件化)，交付 [PR !100](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/100)。

公共协议主责为 [S_16](../specs/S_16_session-query-events.md)，专项字段主责为 [S_11_session-usage-ops.md](../specs/S_11_session-usage-ops.md)；下文表格解释怎样填写、怎样消费，不另立契约。共享实现和既有验收记录见[查询公共机制与七项特性索引](../designs/session-query-events.md)。

2026-10-31 按单个 Ops / Event 拆分特性文档；原合篇只作为文档组织历史，不代表仍需一起调用。此次拆分没有改变运行时契约或实现。

## 2. 能力说明与 Claude 对照

`session.usage` 是这一次查询调用的拦截事件。方法代理验证参数和已绑定会话后触发它；查询 Handler 接收下述参数，可继续默认处理、增加前后操作、返回合法替代视图或明确拒绝。默认 CORE 读取平台事实，结果返回原方法调用者；它不表示状态发生变化，也不自动写历史或显示到界面。

`await ops.session.usage()`：读取平台已有观测，返回上下文窗口占用、账户额度窗口和可得费用的统一视图，适合 Token Weather 等状态显示。默认不发模型请求、不重新计数、不触发测量事件。

| 项 | 本次实现 | 与固定 Claude 定义的关系 |
| --- | --- | --- |
| 事件输入 | breakdown / columns 可选映射 | 以同名查询 OpEvent 为扩展点；会话身份独立绑定，不作为可改写参数 |
| 默认数据来源 | 优先绑定 session_usage_info；键存在但为 None 时保持未知，不用旧历史兜底；无该绑定键时才读取已提交用量。 | 使用本地真实平台服务或绑定快照，不伪造 Claude 私有宿主数据 |
| 方法公开返回 | dict | 顶层及基础数字字段对齐 Claude；未采集额度/费用不造数。summary 仅返回已观测分类子集；full 默认明确不支持，不偷偷执行计数 API；columns 当前不生成网格。未知 window 可省略，这比 Claude 常态必有 window 更保守。 |
| 改写与拒绝 | 可 next、短路合法 value、返回合法 deny | 只影响本次查询结果，不修改平台会话事实或授权范围 |

对照依据是 Claude Code Mods 2.1.277 的固定公开类型声明，提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81`；不是运行 Claude 私有宿主的结果。

## 3. 事件契约

### 3.1 触发前后与粒度

```text
平台业务入口先收集可信会话资料，触发外层事件并传入 SessionContext
  → Core 为实际用户 Handler 绑定本次 Ops
  → 用户 await ops.session.usage(...)
  → 方法代理读取 ops.session_context，规范化参数，校验会话与重入
  → await Worker bus.trigger(Event("session.usage", arguments), context)
  → 查询 USER Handler：可 next、加工合法返回视图或明确 deny
  → await next_handler(event)
  → 共享查询 BUILTIN 开始计时并继续
  → CORE 读取绑定用量快照；没有绑定键时才按 session_id 读取已有用量，再投影公开字段
  → {value: 结果} 经 BUILTIN／USER 返回；或 USER 返回 {deny: 原因}
  → 代理校验、复制并解包；原 Ops 调用者得到值或明确错误
  → 用户消费结果，再 next 原外层事件
```

一次 Ops 调用发起一次本查询；前置校验未通过时不触发。同一外层 Handler 调用两次会读取两次；只有订阅、没有调用时不会自动查询。查询是等待期间另起的一条链，返回后原 Handler 从 await 后继续。每个匹配 USER 只有调用 next 才进入后续 Handler；合法短路可不进入默认 CORE。

### 3.2 输入：每个字段表达什么、由谁填写

实际类型是 `openjiuwen.core.events.Event`，event_input 为查询映射。下面展示 Ops 代理生成的对象形态；普通调用者使用第 4.1 节的方法即可。平台会话上下文通过 trigger 的独立参数传递。

```python
from openjiuwen.core.events import Event

query_event = Event("session.usage", {"breakdown": "summary", "columns": 80})
```

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| event_type | str，必有；固定为 session.usage | 本次查询的事件类型，Bus 据此选择处理链 | **Ops 代理填写**；示例为 "session.usage"，不填入参数映射 | 只读；保持当前类型 |
| event_input | 只读 Mapping，必有 | 本次查询选项，不包含目标会话、目录或模型快照 | **Ops 代理填写**；{"breakdown": "summary", "columns": 80} | USER 可在 next 前构造合法新 Event |

#### event_input 的全部字段

| 字段 | 类型 / 默认值 | 含义、实际取值和填写方式 | 可修改范围 |
| --- | --- | --- | --- |
| breakdown | summary/full，可缺省 | 当前查询希望取得哪种分类明细。代理从方法选项生成；USER 可在 next 前构造合法新输入。填写／示例："summary"；需改变选项时例如 Event(event.event_type, {"breakdown":"summary"})。用途：查询 Handler 可按它匹配或调整本次读取；匹配完成后改写不会重新匹配 Handler。缺失或错填：省略只读取基本数字；full 可进入事件链但未经 Hook 处理时默认 CORE 报不支持；不能补 None 或布尔值 | 可在 next 前改写为合法值；不可原地修改 |
| columns | 正 int，可缺省 | 本次查询透传的终端网格宽度建议。代理从方法选项生成；USER 可构造新 Event 调整。填写／示例：80。用途：自定义查询 Hook 可消费；当前默认 CORE 不据此构建网格。缺失或错填：省略就是未提供；0、负数、字符串、bool、None 都不符合已存在键的类型要求 | 可在 next 前改写为合法值；不可原地修改 |

省略 breakdown 时默认只读基本数字；summary 返回已有分类子集；full 可进入事件链，但默认 CORE 报 FULL_BREAKDOWN_UNAVAILABLE。Matcher 在初次 trigger 时固定，后续改写选项不会重新选择 Handler。

#### 随调用绑定的会话资料

这些资料由平台业务入口收集，Core 绑定到本次 Handler 的 Ops。用户读取 `ops.session_context` 即可；它是只读属性，不需要 await，也不负责采集或组装上下文。查询参数和返回视图都不能替换这份可信资料。

| 字段 | 类型 / 是否必有 | 含义与应该填写什么 |
| --- | --- | --- |
| ops.session_context | Core 只读属性，查询需要非 None | 本次 Handler 的可信会话资料入口。平台把上下文随外层 Event 交给 Bus，Core 绑定到实际 Handler 的 Ops。填写／示例：用户不用填，也不用 await；直接读取 ops.session_context。用途：代理取出这份资料，随查询 Event 的独立 context 参数继续传递。缺失或错填：缺少上下文时查询报错；不要把用户输入 payload 包装成平台可信上下文 |
| SessionContext.session_id | 平台验证过的非空 str，必需 | 本次绑定的是哪个会话。平台业务入口提供；用户普通调用不填写。填写／示例：平台例如绑定 session-a；查询方法和 Event 输入里都不再填 session_id。用途：读取相应会话的历史、保存模型或用量服务；同时参与隔离和循环保护。缺失或错填：缺失/非法不能查询；返回值或 Event 输入不能把目标改成另一个会话 |
| SessionContext.metadata["session_usage_info"] | 平台只读快照或 None，可缺省 | 这次查询已绑定的用量事实，和查询参数 breakdown/columns 分开。平台主窗口观测及已有用量整理逻辑。填写／示例：平台负责绑定快照；用户不把猜测 tokens、费用或会话 ID 当作快照传进查询。用途：有绑定键时优先消费该观测；没有键时才按 session_id 读取已有用量。缺失或错填：键存在且为 None 代表明确未知，不回退旧历史；没有可得数字时返回空 context，仍保留 rateLimits=[] |
| current_query_origin().package | str 或 None；helper 本身也可返回 None | 谁发起了本次查询，而不是当前查询 Hook 属于哪个包。Server 分派器从原 Ops 的包身份记录；不是用户传参。填写／示例：先 origin = current_query_origin()，非 None 时再读 origin.package。用途：用于用户自己的审计和来源判断；它不是 Core next.origin 或授权证明。缺失或错填：直接宿主触发而没有 Ops 调用域时 helper 可为 None；不要无判空访问 .package |

Bus 保留会话身份并生成规范化只读副本；外层和查询 Handler 不要求持有同一个 Python 对象。原查询包身份由分派器记录，不能用当前 Hook 所属包冒充原调用者，也不能把来源审计当作授权证明。

### 3.3 输出：默认值、替代结果与拒绝

默认 CORE 返回恰含 value 的映射；示例为 Python 对象，None/True/False 在 JSON 中分别对应 null/true/false：

```python
{'value': {'context': {'tokens': 120,
                       'window': 1000,
                       'percent': 12,
                       'breakdown': {'categories': [{'name': 'messages',
                                                     'tokens': 90,
                                                     'color': 'promptBorder',
                                                     'isDeferred': False,
                                                     'kind': 'used'},
                                                    {'name': 'tools',
                                                     'tokens': 30,
                                                     'color': 'promptBorder',
                                                     'isDeferred': False,
                                                     'kind': 'used'}],
                                     'totalTokens': 120}},
           'rateLimits': []}}
```

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| value | dict，必含 context/rateLimits，可含 cost；成功时唯一顶层键 | 当前可得的上下文占用、账户额度窗口和累计费用视图，不是原用量存储对象。CORE 读取已有主窗口观测；查询 USER 可返回符合相同结构的视图。填写／示例：{"context":{"tokens":120,"window":1000,"percent":12},"rateLimits":[]}；完全未知时填 {"context":{},"rateLimits":[]}。用途：把不同用量信息交给用户业务统一消费；调用不发模型请求、不重新测量。缺失或错填：不能返回 None、空对象或增加 schema_version/source/turn 等旧顶层键；未知字段按下表省略。Ops 校验、独立复制并解包；普通调用者拿到 value 本身，查询 Handler 从 next 得到完整封装。 |
| deny | 非空 str；拒绝时唯一顶层键 | USER 如需明确拒绝，返回 {"deny":"QUERY_DISABLED"}。原字符串最多 512 字符，去首尾空白后须非空；不得与 value 同时出现。代理抛 SessionQueryDenied，code=DENIED、reason 保留原原因。抛普通异常不等于明确拒绝。 |

#### value 的全部字段

| 字段 | 类型 / 默认值 | 含义、实际取值和填写方式 | 可修改范围 |
| --- | --- | --- | --- |
| context | dict，必有；可为空 {} | 模型当前可得的上下文占用视图；不是 session_context 身份对象，也不是一轮输入输出相加。当前主会话已有上下文窗口观测。填写／示例：{"tokens":120,"window":1000,"percent":12}；数字都未知时填 {}。用途：消费方从这里读取占用数字及可得分类明细。缺失或错填：必须保留该键；不知道时填 {}，不要填 None 或人为补 0 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| rateLimits | list[dict]，必有；默认 [] | 账户/网关额度窗口的已知读数，不是会话上下文容量。真实 API 额度窗口读数；平台默认没有这些窗口条目。填写／示例：没有额度读数填 []；有读数例如 [{"kind":"five_hour","percentUsed":23.5}]。用途：区分账户额度与当前上下文占用；不会因查询而申请或重置额度。缺失或错填：[] 表示无读数，不表示无限额度或零消耗；不要填 None | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| cost | 恰含 usd 的 dict，可缺省；默认省略 | 可得的会话累计定价结果，不是当前一轮 Token 数。真实会话费用账本或有相同事实依据的用户 Hook。填写／示例：有账本时如 {"usd":1.25}；没有账本就不返回 cost 键。用途：用户有费用展示需求时消费；查询本身不计价、不补造账本。缺失或错填：缺失表示没有费用事实，不等于免费；不要填 None、{} 或猜测的 0 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |

#### context 的全部字段

| 字段 | 类型 / 默认值 | 含义、实际取值和填写方式 | 可修改范围 |
| --- | --- | --- | --- |
| context.window | 有限正 number，可缺省；不能是 bool | 模型上下文窗口容量，不是本轮已花费或剩余 Token。平台观测的模型上下文容量 limit_tokens。填写／示例：1000，单位 Token。用途：配合 tokens 显示“120 / 1000”，有权威读数时计算占比。缺失或错填：未知省略；不能用 0、负数、字符串 "1000"、None 或无穷值代替 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| context.tokens | 有限非负 number，可缺省；不能是 bool | 已有权威输入侧占用，不是用户文字长度，也不是通知中 input_tokens+output_tokens 总和。平台主窗口权威输入 Token 读数，仅 authoritative_total=True 时默认提供。填写／示例：120；确知零占用可以填 0。用途：显示已用上下文 Token；与 window 一起形成占比。缺失或错填：无权威读数时省略；本地分类估计不能冒充该值，不用 0 或 None 表示未知 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| context.percent | 0～100 的 int，可缺省；不能是 bool | 上下文窗口占用的整数百分比，不是 0～1 的比例。默认 CORE 按权威 tokens/window 四舍五入并限制到 100。填写／示例：12 表示 12%；确知零为 0。用途：显示占比或让用户业务选择提示阈值；使用时不要再乘 100。缺失或错填：缺少必要数字时省略；不要填 0.12、"12%" 或 101，也不要用真假判断丢掉合法 0 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| context.breakdown | dict，可缺省 | 已有分类的估计视图，不是新的精确 Token 测量结果。请求 summary 且平台已有可用分类观测时，CORE 返回已知子集。填写／示例：{"categories":[{"name":"messages","tokens":90,"color":"promptBorder","isDeferred":False,"kind":"used"}],"totalTokens":90}。用途：解释上下文主要被哪些类别占用；用户 Hook 可提供自己有事实依据的分类视图。缺失或错填：未请求或没有分类时省略，不强行填 {}；默认不提供完整网格、压缩设置或逐工具/文件账本 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |

#### context.breakdown 的默认公开字段

| 字段 | 类型 / 默认值 | 含义、实际取值和填写方式 | 可修改范围 |
| --- | --- | --- | --- |
| context.breakdown.categories | list[dict]；默认有 breakdown 时包含 | 按已有类别拆分的占用估计列表，不是原生消息数组或工具调用记录。已有 parts 分类的投影。填写／示例：每类一个对象；例如 messages 90 Token、tools 30 Token。用途：分类显示、排序或汇总；不得把每项都当成权威计费数字。缺失或错填：默认没有有效分类会省略整个 breakdown；用户替代视图须保持分类对象的语义 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| categories[i].name | str；默认分类项有此键 | 这一类占用的名称，不是会话、消息或工具调用 ID。平台原分类键。填写／示例："messages"、"tools" 等实际已有标签。用途：给分类加标题；不要假定所有平台都用同一翻译文本。缺失或错填：不要用猜测的逐工具/文件名称补出平台未采集的分类 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| categories[i].tokens | 有限非负 number；默认分类项有此键 | 该类别估计占用，不是百分比或价格。这类 parts 的已有 Token 估计。填写／示例：90，单位 Token。用途：比较分类大小，并计算 totalTokens。缺失或错填：不能用 None、字符串或负数表示未知；没有读数的分类不为凑数制造一项 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| categories[i].color | str；默认分类项有此键 | 显示颜色的主题名称；不是统计来源或严重程度。默认 CORE 提供主题建议键。填写／示例：当前默认 "promptBorder"。用途：用户若有展示层可将主题键映射为颜色；字段不会自己渲染。缺失或错填：当前不从 Token 数推算颜色，也不承诺 CSS 颜色名或十六进制值 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| categories[i].isDeferred | bool；默认 False | 该分类是否标记为延迟加载；默认只描述已有占用，不推测未采集状态。当前已采集占用分类的投影。填写／示例：当前默认填 False。用途：有对应业务事实时区分类别；False 不能用来推断工具一定已运行。缺失或错填：不填字符串 "false"；没有延迟加载事实时不猜造 True | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| categories[i].kind | str；默认 used | 这项是已占用分类；不是模型类型、工具名或错误码。当前已采集占用分类的投影。填写／示例：当前默认 "used"。用途：让消费者识别分类性质；默认不制造 free/buffer 等缺少事实的项。缺失或错填：不要为补齐完整 Claude 图形而推算未采集的分类 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| context.breakdown.totalTokens | 有限非负 number；默认有 breakdown 时包含 | 已知分类估计总和，可能与权威 context.tokens 不同。已有 categories[].tokens 求和。填写／示例：90+30 得到 120，单位 Token。用途：展示分类合计；不能把两种口径强行拉齐，也不能作为累计费用。缺失或错填：未知分类不补零来凑权威总量；不要误当窗口容量或美元金额 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |

#### rateLimits 中每个额度窗口的全部字段

| 字段 | 类型 / 默认值 | 含义、实际取值和填写方式 | 可修改范围 |
| --- | --- | --- | --- |
| rateLimits[i].kind | 非空 str，窗口对象必有 | 这条额度数据对应哪一个统计窗口。真实额度窗口类型。填写／示例："five_hour"、"seven_day" 或网关真实的 "spend_limit"。用途：让用户业务区分不同额度限制，不把五小时和七天数值相加。缺失或错填：未知时不制造窗口条目；不要拿 session_id 或模型名替代 kind | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| rateLimits[i].percentUsed | 有限非负 number，窗口对象必有 | 此额度窗口已用百分比；它与 context.percent 的 0～100 整数约束不同。对应窗口的真实额度读数。填写／示例：23.5 表示已用 23.5%；超额时可以是 105。用途：显示额度使用情况或让用户业务提示超额；不要再乘 100。缺失或错填：没有读数不创建条目；不能用 0、字符串或 None 冒充未知 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |
| rateLimits[i].resetsAt | 非空 str，可缺省 | 该窗口预计何时重置，不是本次查询时间或消息创建时间。额度服务提供的真实重置时间。填写／示例：例如 2026-10-31T12:00:00Z，应表达真实 ISO 8601 时间。用途：用户可据真实时间显示重置提示；不由查询过程修改服务端窗口。缺失或错填：未知省略，不填当前时间占位。当前校验器检查非空字符串，不代替 ISO 时间解析 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |

#### cost 的全部字段

| 字段 | 类型 / 默认值 | 含义、实际取值和填写方式 | 可修改范围 |
| --- | --- | --- | --- |
| cost.usd | 有限非负 number，有 cost 时必有 | 美元金额，不是积分、Token 数、百分比或其他币种。账本累计的已定价 API 费用。填写／示例：1.25 表示 1.25 美元；真实账本确认零费用才填 0。用途：按美元金额显示或汇总已确认成本；不要把未知单价代入估算后冒充账本。缺失或错填：无金额事实省略整个 cost；不能填 "$1.25"、None、负数或增加未知账本字段 | 可提供符合结构的本次返回视图；不改写平台用量或触发测量 |

`breakdown` 是默认 CORE 返回的可得子集。当前验证器检查它是映射及可复制 JSON 数据，不逐项强制验证所有分类子字段；自定义 Handler 应保证表中的字段类型和统计口径。默认 CORE 不生成 rawMaxTokens、gridRows、压缩设置和逐工具/文件清单，不能把尚无事实的字段填成 0。费用、额度、上下文占用是三种口径，不能互相代替。

成功／拒绝只能保留表中对应的一个顶层键，不增加其他键，也不能直接返回裸值。 本方法的 value 不能为 None；数字全未知时仍返回 {"value": {"context": {}, "rateLimits": []}}。

正常继续返回 `await next_handler(event)` 的完整结果，不要再包一层 value。USER 可在 next 前返回合法 value 短路，或在 next 后加工自己的返回副本；真实平台资料、授权目标及后续默认读取不随之改变。

USER／BUILTIN 的普通异常或非法输出沿用 Core 故障隔离，可能继续原有效链或保留真实下游结果；不能靠抛异常或错填可靠拒绝。默认 CORE／框架读取故障和取消沿等待关系传播。多次 next 可能重复读取，不承诺一次调用内结果缓存。

### 3.4 注册、筛选、改写示例

本例要做的事情是：**为未指定明细的查询补上 summary 选项**。下面是扩展入口模块 `extension.py`；由已有 manifest 指定该模块并通过管理入口安装即可，包格式参照 [session_query_audit](../../examples/session_query_audit/manifest.yaml)。

```python
from openjiuwen.core.events import Event

class Extension:
    def __init__(self):
        self.parameters = {}

    def register(self, registrar):
        registrar.register_event_handler(
            "session.usage", self.prefer_summary, {}, "user",
        )
        registrar.register_event_handler(
            "context.usage.updated", self.on_usage, {}, "user",
        )

    async def prefer_summary(self, event, ops, next_handler):
        arguments = dict(event.event_input)
        # Keep explicit summary/full choices; add summary only when it was omitted.
        if "breakdown" not in arguments:
            arguments["breakdown"] = "summary"
        return await next_handler(Event(event.event_type, arguments))

    async def on_usage(self, event, ops, next_handler):
        session_id = ops.session_context.session_id
        value = await ops.session.usage()
        self.parameters[session_id] = {"usage": value}
        return await next_handler(event)


def create_extension():
    return Extension()
```

外层调用 `ops.session.usage()`，代理最初生成空映射；USER 复制输入并补 breakdown=summary，再交给默认 CORE。平台若已有 tokens=120、window=1000 和分类观测，外层参数含 percent=12 及已有分类；没有分类时省略 breakdown。明确请求 full 时本例保留 full，默认 CORE 仍报 FULL_BREAKDOWN_UNAVAILABLE，不偷偷降级或发起测量。

Matcher 为等值条件合取，空字典匹配该事件全部合法输入。还可按原始选项填写 `{"breakdown":"summary"}`；它只匹配初始已带该字段的查询，不匹配后来才由其他 Handler 补入的字段。 查询 Handler 中的 next 继续当前 `session.usage` 链；在其中再次调用同名 Ops 会报 QUERY_REENTRANT。

安装只发布订阅。下一次平台真实发布 `context.usage.updated` 并绑定会话时，on_usage 才调用 Ops、等待查询链、保存本扩展的 parameters，再 next 原外层事件。这里不要求 UI 渲染；`self.parameters[session_id]` 是本例可观察的消费结果，属于用户扩展自己的内存状态。其他由平台绑定了所需上下文的事件也可调用同一 Ops，不要求用户先调用 notify 或修改 Adapter。

## 4. 方法契约

### 4.1 ops.session.usage(arguments=None, *, breakdown=None, columns=None)

推荐调用（写在已由平台绑定 SessionContext 的业务 Handler 中）：

```python
usage = await ops.session.usage({"breakdown": "summary", "columns": 80})
percent = usage["context"].get("percent")
warning = None if percent is None else percent >= 80
```

| 输入字段 | 必填 / 默认 | 应填写的内容、范围及示例 |
| --- | --- | --- |
| arguments | Mapping 或 None，可省略；默认 None | breakdown/columns 的参数容器，不是会话上下文或用量快照。普通调用者二选一地提供参数对象或命名参数。填写／示例：await ops.session.usage({"breakdown":"summary","columns":80})；也可不提供这个位置参数。用途：便于集中传入查询选项；代理复制成事件输入映射。缺失或错填：省略/None 不增加参数；对象与非 None 命名参数混用报 TypeError。不能含 session_id 或其他未知键 |
| breakdown | summary/full 字符串，可省略；命名参数 None 等同未提供 | 决定是否请求上下文分类明细及其读取方式。普通调用者按是否需要分类明细填写。填写／示例：breakdown="summary"；只查数字时不传；full 是已识别但默认 CORE 暂不支持的选项。用途：summary 让默认 CORE 投影已有分类；full 可先被 Hook 处理，否则默认 CORE 报 FULL_BREAKDOWN_UNAVAILABLE。缺失或错填：省略不生成明细；没有已有分类仍省略 breakdown 返回。True/False/"detail" 非法；参数对象内的值不能填 None |
| columns | 正 int，可省略；命名参数默认 None；bool 非法 | 分类网格的终端字符列数建议，不是像素、Token 限额或输出长度。普通调用者按需填写终端列宽建议。填写／示例：columns=80；应为整数，不填 "80"、0、负数或 True。用途：选项会传给查询 Hook；当前默认 CORE 不生成网格，因此不会改变数字、触发测量或产生界面。缺失或错填：省略不增加该键；参数对象中 columns=None 非法。当前只传 columns 而不传 breakdown 也不会自动请求分类 |

也可写 `await ops.session.usage()` 或 `await ops.session.usage(breakdown="summary", columns=80)`。位置参数对象和非 None 命名参数不可混用。命名参数 None 等同省略；映射里显式写 breakdown=None／columns=None 非法。省略选项时默认只读基本数字；第 3.4 节用户 Hook 是安装后才生效的自定义补充规则。

成功返回 **dict**，直接返回值及每个字段的含义见第 3.3 节。代理把方法参数规范化为 Event 输入，从当前 Ops 取得已绑定上下文，另起 `session.usage` 查询；事件的 {value: 结果} 经校验、复制后解包为方法返回，{deny: 原因} 转为 SessionQueryDenied。目标只来自 `ops.session_context.session_id` 及所需可信资料，不从用户参数或返回视图选择另一会话。

主要失败：没有 SessionContext 抛 RuntimeError；方法／事件参数非法报 TypeError，非法会话 ID 沿平台会话服务原契约拒绝；显式拒绝报 SessionQueryDenied（code=DENIED）；活跃同名或间接循环报 QUERY_REENTRANT，嵌套深度超过 16 报 QUERY_DEPTH_EXCEEDED。读平台服务的原始错误继续传播，取消保持 CancelledError。 默认 CORE 不支持 full，报 FULL_BREAKDOWN_UNAVAILABLE；columns 只透传，不生成网格。 用户可在调用 Ops 处捕获错误；未捕获时仍受外层事件自身隔离规则约束，不能把外层回合继续当成查询成功。

### 4.2 旧 Handler / 返回值迁移

删除 breakdown=True/False；无明细用 usage()，需要已有分类用 breakdown='summary'。旧 context.context_window.input_tokens 改成 context.tokens，limit_tokens 改成 window，occupancy_rate 改成 percent（百分比单位）。旧 turn 不再是公开结果，单轮消耗仍可从现有用量通知的输入字段获取。

本次文档结构与说明调整不改变上述兼容边界。执行目录、模型、Git、历史和用量事实仍由平台服务维护；对返回视图的改写不成为另一项平台操作的可信输入。

| 旧调用 / 容易混淆的用法 | 现在怎样写 | 对使用者的影响 |
| --- | --- | --- |
| breakdown=True / False | 不需要明细用 usage()；已有分类用 breakdown="summary" | bool 参数非法，full 默认明确不支持 |
| context.context_window.input_tokens / limit_tokens | 改为 context.tokens / context.window | 字段缺失保持未知，不填 0 掩盖 |
| occupancy_rate | 改为 context.percent | 单位为 0～100 的百分数，不是 0～1 比例 |
| 依赖公开结果 turn 子对象 | 单轮字段从既有用量通知输入按其契约读取 | 新 usage 视图不再公开 turn |
| columns 当作 Token 限额或像素 | 仅作为分类网格列宽建议传给 Hook | 当前默认 CORE 不生成网格、不重新测量或发模型请求 |

## 5. 接入与兼容方案

<a id="51-启动登记上下文绑定与-core-分工"></a>

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

业务查询的默认 CORE 落点：**CORE 读取绑定用量快照；没有绑定键时才按 session_id 读取已有用量，再投影公开字段**。CORE 不回调公共同名 Ops，以免递归；查询 USER 执行本包规则，共享 BUILTIN 计时、记录原调用来源并复制结果，然后反向返回。它与启动时创建 Builder 的 ops.build CORE 是两种不同职责。

完整业务过程：平台发布外层事件并绑定上下文 → on_usage 调用 `ops.session.usage()` → 查询代理触发同名事件 → `prefer_summary` 介入 → next 进入 BUILTIN／默认 CORE → 返回查询结果并应用本例规则 → 代理解包 → 用户保存 parameters → next 继续原外层事件。具体可观察效果见第 3.4 节；第 4.1 节是独立调用法，不要求安装其他六项示例。

默认查询不写平台业务数据、不发模型请求、不自动渲染，也没有全局结果缓存。对返回视图的加工不会成为其他平台操作的可信输入。用户包卸载／重载撤销自己的 Hook，不撤销平台事件、CORE 或共享 Ops；用户自有内存参数与缓存的保留由用户扩展管理。

七项联合示例见 [session_query_audit](../../examples/session_query_audit/)；其中 collect_session_parameters(ops) 是普通用户辅助函数，分别调用七个 Ops 后返回参数字典。它与本文单项业务规则的示例分别维护。共享机制、来源及取消边界见[公共设计第 5 节](../designs/session-query-events.md#5-接入与兼容方案)。

源码位置：[事件与校验](../../event_types/session_queries.py)、[Ops 登记](../../handler_ops/session_ops.py)、[查询代理](../../handler_ops/session_queries.py)、[分派](../../query_dispatch.py)、[查询 CORE／BUILTIN](../../event_handlers/session_queries.py)、[平台读取](../../../server/runtime/session/query_views.py)、[现有平台通知入口](../../event_types/context_usage.py)。

## 6. 验收与未决问题

### 6.1 本特性的独立验收场景

| 场景 ID | 输入与前置 | 操作 | 预期结果 |
| --- | --- | --- | --- |
| F_21-V-01 | 绑定权威 tokens=120、window=1000 的主窗口观测 | 调用 usage() | 返回 context.tokens=120、window=1000、percent=12 |
| F_21-V-02 | 已有分类；或请求 full；或绑定快照未知 | 分别请求 summary/full/无参数 | summary 返回已有分类；full 明确不可用；未知数字省略且不伪造费用 |
| F_21-V-03 | 安装仅拦截 session.usage 的用户包 | 分别 next、短路 value、显式 deny | 默认值 / 合法替代值 / SessionQueryDenied 分别可观测 |
| F_21-V-04 | 不同会话绑定不同上下文；或在 Hook 中同名递归 | 分别调用本方法 | 会话隔离；活跃同名或间接循环报 QUERY_REENTRANT，不能无限递归 |
| F_21-V-05 | 读取失败、Hook 异常或查询取消 | 分别构造这些分支 | 按第 3.3 / 4.2 节区分隔离、明确拒绝、平台错误与取消，不把失败当成功空值 |

对应事件化用例位于 [test_session_query_events.py](../../../../tests/unit_tests/extensions_v2/test_session_query_events.py)：包含七项默认调用、各项短路 / 拒绝、非法参数、查询顺序、隔离、循环 / 取消及实际用户包装载卸载。每个场景只验证本方法承诺，不把其他六项也作为调用前置。

### 6.2 已有实施证据与本次文档调整

实现验证基于本 PR 实现提交 `f313f513ee9b`、已合入 Server 基线 `c6a07f909` 与 Core `bdde48a8`，Python 3.11。扩展 632 项、相关平台 140 项及独立进程 ZIP / notify / 七项参数返回验证的实际范围、命令和限制统一保留在[公共设计第 6 节](../designs/session-query-events.md#6-验收与未决问题)。这属于查询事件化实现的既有证据；这些数量属于实现阶段历史运行。本次按 session.append 对齐文档的结构和说明，运行代码不变；只核对字段、示例、源码链路、链接与差异，不声称重新执行业务或真实模型测试。

顶层及基础数字字段对齐 Claude；未采集额度/费用不造数。summary 仅返回已观测分类子集；full 默认明确不支持，不偷偷执行计数 API；columns 当前不生成网格。未知 window 可省略，这比 Claude 常态必有 window 更保守。 该限制属于已知支持边界；此轮未增加新的运行时待决项。未执行 Claude 私有宿主、新模型 API、浏览器或 Front/Supervisor 跨进程验收，不能由文档完整推断这些验收已完成。

2026-11-02 文档整理：以 2026-10-31 的 session.append 原件为参照，统一输入对象示例、字段表列式、输出嵌套字段、可运行 USER 业务案例、Ops 参数与迁移说明。运行代码、公共字段与既有历史验收证据保持；本轮仅验证文档结构、示例行为、类型／字段、链接及差异，不作为业务、模型或浏览器测试的新结果。

## 7. 参考资料

- [专项字段规约 S_11_session-usage-ops.md](../specs/S_11_session-usage-ops.md)
- [S_16 查询公共协议](../specs/S_16_session-query-events.md)
- [七项独立特性索引与共享实施记录](../designs/session-query-events.md)
- [Claude 会话方法](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude 同名 OpEventOf 查询事件](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [内置事件开发指南](../designs/builtin-event-development-guide.md)
