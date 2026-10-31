# session.turns / session.usage 与统计查询 Ops 规约

最近修订 2026-10-31。七项查询公共机制见 [S_16](S_16_session-query-events.md)，usage 使用说明见 [F_21](../features/F_21_session-usage-ops.md)，turns 使用说明见 [F_29](../features/F_29_session-turns-query.md)。本规约维护字段和默认语义，特性文档解释填写方式。

## 1. 能力与默认来源

| Ops / Event | 默认来源 | 返回类型 |
| --- | --- | --- |
| session.turns | read_session_turns(session_id)：优先 request_id 去重，再用记录 id 和旧记录位置；排除扩展留档/自动化等。 | int 或 None |
| session.usage | 优先绑定 session_usage_info；键存在但为 None 时保持未知，不用旧历史兜底；无该绑定键时才读取已提交用量。 | dict |

## 2. 输入输出字段

### session.turns：输入

| 字段 | 类型 / 默认 | 谁填写 | 含义与边界 | 怎样填 / 例子 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.<方法>() |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

### session.turns：返回值字段

| 字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式 / 例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | 非负 int 或 None | 平台统计服务 | 原生主用户输入计数；0 是已知零轮，None 是无法建立统计事实，不能混为 0 | 2；不传历史数组或会话 ID |

### session.usage：输入

| 字段 | 类型 / 默认 | 谁填写 | 含义与边界 | 怎样填 / 例子 |
| --- | --- | --- | --- | --- |
| arguments | Mapping 或 None，可省略 | 调用者可填 | 可传 Claude 风格参数对象；与命名参数二选一，不能混用 | await ops.session.usage({'breakdown':'summary','columns':80}) |
| breakdown | summary / full，可省略 | 调用者按需填 | 省略只读状态数字；summary 返回已观察分类估计；默认 CORE 不支持 full 逐工具/文件精确计数，报 FULL_BREAKDOWN_UNAVAILABLE | breakdown='summary'；旧 True/False 参数不再接受 |
| columns | 正 int，可省略，不接受 bool | 调用者按需填 | 沿用 Claude 网格宽度参数名称；本地默认不构建终端网格，当前值只透传给查询 Hook；不会改变 token 数 | columns=80；不是像素、token 限额或计费数量 |
| Event.event_input | 参数映射 | Ops 代理规范化构造 | 只含实际提供的 breakdown/columns；无参数为 {}，不补 session_id/origin | {'breakdown':'summary','columns':80} |

### session.usage：返回值字段

| 字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式 / 例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | dict，必有 context/rateLimits | 平台 CORE 或查询 Handler | 使用 Claude 的顶层字段；完全未知时仍返回 {'context':{},'rateLimits':[]}，不伪造零读数 | 不是旧 schema_version/session_id/source/context/turn 存储对象 |
| value.context | dict，必有 | 当前主上下文观测 | 只包含已知的窗口/权威输入数字和按需分类；不是本轮输入输出 token 的总和 | {} 或 {'window':1000,'tokens':120,'percent':12} |
| context.window | 正 number，可缺省 | 观测的 limit_tokens | 模型上下文窗口容量，单位 token；不知道时省略，不能填 0 冒充未知 | 1000 |
| context.tokens | 非负 number，可缺省 | 权威主窗口 input_tokens | 最后 API 输入侧 token；只有 authoritative_total=True 时提供，本地估计不冒充该数字 | 120；确知零为 0 |
| context.percent | 0～100 的 int，可缺省 | 权威 tokens/window | 占窗口的整数百分比，四舍五入并限制到 100；不是 0～1 的比例 | 12 表示 12%，不是 0.12 |
| context.breakdown | dict，可缺省 | 请求 summary 且已有分类时 | Claude 分类字段的可得子集；不为获取此对象重新数 Token | 没有 parts 时省略整个 breakdown |
| breakdown.categories | list[dict] | 已采集的 parts 分类 | 每个有 token 估计的分类一项，不制造工具/文件级清单 | [{'name':'messages','tokens':90,'color':'promptBorder','isDeferred':False,'kind':'used'}] |
| categories[i].name | str | 平台原分类键 | 分类标签；业务判断优先看 kind，不假定翻译后的名称 | messages / tools 等已有分类 |
| categories[i].tokens | 非负 number | 原分类估计 | 该类估计 token；未分类数据不会为凑总数编造一项 | 90 |
| categories[i].color | str | 默认显示主题键 | 本地默认 promptBorder；是显示建议，不是统计来源 | promptBorder |
| categories[i].isDeferred / kind | bool / str | 本地分类投影 | 已有占用分类为 False / used；不推测 deferred 工具、free 或 buffer | False / used |
| breakdown.totalTokens | 非负 number | 已知分类 tokens 求和 | 分类估计总和，可能不同于权威 context.tokens，不能当作计费总量 | 90 |
| 未提供的 breakdown 字段 | 缺省 | 平台尚无事实或网格实现 | 不编造 rawMaxTokens、压缩设置、gridRows、memoryFiles/mcpTools/agents 清单；summary 是已知字段子集 | columns 当前不会生成 gridRows |
| value.rateLimits | list[dict]，必有 | 最后 API 额度读数或用户 Hook | 没有账户窗口读数时 []；不是无限额度，也不是零消耗 | 默认 [] |
| rateLimits[i].kind | 非空 str | 真实额度窗口类型 | 已有读数的额度窗口标识；本平台默认不生成条目 | five_hour / seven_day / spend_limit |
| rateLimits[i].percentUsed | 非负 number | 真实额度读数 | 已用百分比，可超过 100 表示超额；不是 0～1 比例 | 23.5 |
| rateLimits[i].resetsAt | str，可缺省 | 额度窗口重置时间 | 应为真实 ISO 8601 时间；不知道时省略，不用本地当前时间填充 | 2026-10-31T12:00:00Z |
| value.cost | dict，可缺省 | 真实会话费用账本 | 无账本时省略，不返回 {'usd':0} 或 None 冒充价格已知 | 本平台默认省略 |
| cost.usd | 非负 number | 会话累计已定价 API 响应 | 美元金额；不是当前轮 token 数乘一个猜测单价 | 1.25（只有真实账本才提供） |

## 3. 事件、可信绑定与错误

每个公开 Ops 触发同名查询 Event。USER 返回 value/deny 封装，Ops 解包；默认 CORE 调用原始服务，不经公共 Ops 再次发起同名查询。调用者不填 session_id，不从返回视图决定平台真实身份或授权根。缺失值、真实零值、读取故障不同；拒绝和重入见 S_16。查询结果不写平台数据、不自动渲染。

## 4. 兼容迁移与差异

| 方法 | 迁移 | Claude 对照差异 |
| --- | --- | --- |
| turns | 返回类型和默认口径保持；现在会派发 session.turns。不要再把累计收到的通知数当作权威轮次。 | 语义对齐 Claude 用户 prompt 轮次；本地只统计已提交且保留历史，未知为 None。 |
| usage | 删除 breakdown=True/False；无明细用 usage()，需要已有分类用 breakdown='summary'。旧 context.context_window.input_tokens 改成 context.tokens，limit_tokens 改成 window，occupancy_rate 改成 percent（百分比单位）。旧 turn 不再是公开结果，单轮消耗仍可从现有用量通知的输入字段获取。 | 顶层及基础数字字段对齐 Claude；未采集额度/费用不造数。summary 仅返回已观测分类子集；full 默认明确不支持，不偷偷执行计数 API；columns 当前不生成网格。未知 window 可省略，这比 Claude 常态必有 window 更保守。 |
