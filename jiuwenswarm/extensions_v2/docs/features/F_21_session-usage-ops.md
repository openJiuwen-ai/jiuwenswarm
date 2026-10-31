# session.turns / session.usage 与统计查询 Ops 特性设计

## 1. 范围、状态与依据

本分支已实现。最近修订 2026-10-31，维护者刘瑜；本次基于已合入 Server `c6a07f909` / Core `bdde48a8`，不依赖未合入的生命周期 PR。公共机制以 [S_16](../specs/S_16_session-query-events.md) 为准，专项字段主责为 [S_11_session-usage-ops.md](../specs/S_11_session-usage-ops.md)；本文字段表提供填写与消费解释，不另立契约。

本次按用户确认的方向，以 Claude Code Mods 2.1.277 的返回形态优先；固定参考提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81`。此前直接读实现已改为查询事件链；旧验证不能代替本次事件化验证。统一实施与最终证据见 [F_27](F_27_session-query-events.md)。

## 2. 能力说明与 Claude 对照

`await ops.session.turns()`：统计主会话已提交且仍保留的原生用户输入轮次，适合摘要或提示阈值。它不是工具调用次数、模型重试次数或扩展通知次数。

`session.turns` 在每次该 Ops 调用时触发，Handler 接收查询参数，可继续、加工独立返回视图、短路或拒绝；默认 CORE 完成读取，结果由原 Ops 调用者消费。它不是状态变化通知，也不会自动渲染或写入记录。

`await ops.session.usage(breakdown="summary", columns=80)`：读取平台已有观测，返回上下文窗口占用、账户额度窗口和可得费用的统一视图，适合 Token Weather 等状态显示。默认不发模型请求、不重新计数、不触发测量事件。

`session.usage` 在每次该 Ops 调用时触发，Handler 接收查询参数，可继续、加工独立返回视图、短路或拒绝；默认 CORE 完成读取，结果由原 Ops 调用者消费。它不是状态变化通知，也不会自动渲染或写入记录。

| 能力 | 默认 CORE 数据来源 | 公开返回 | 与 Claude 的差异 |
| --- | --- | --- | --- |
| session.turns | read_session_turns(session_id)：优先 request_id 去重，再用记录 id 和旧记录位置；排除扩展留档/自动化等。 | int 或 None | 语义对齐 Claude 用户 prompt 轮次；本地只统计已提交且保留历史，未知为 None。 |
| session.usage | 优先绑定 session_usage_info；键存在但为 None 时保持未知，不用旧历史兜底；无该绑定键时才读取已提交用量。 | dict | 顶层及基础数字字段对齐 Claude；未采集额度/费用不造数。summary 仅返回已观测分类子集；full 默认明确不支持，不偷偷执行计数 API；columns 当前不生成网格。未知 window 可省略，这比 Claude 常态必有 window 更保守。 |

## 3. 事件契约

### 3.1 触发位置与完整流程

```text
示例入口：原生回合完成，平台收集可信会话资料
  → notify_context_usage(...) 安排 context.usage.updated
  → Worker EventBus 给实际 USER on_usage 绑定本次 Ops
  → USER 调用 ops.session.turns() / ops.session.usage()
  → 查询代理：验证参数与会话，固定原调用包，建立循环保护
  → 同一 Worker EventBus.trigger(同名查询 Event, 原可信 SessionContext)
  → 查询 USER：前置操作 → next_handler(event)
  → 查询 BUILTIN：计时 → next_handler(event)
  → 查询 CORE：直接读取平台事实或服务，不调用同名公共 Ops
  ← {'value': 查询结果} / {'deny': 原因} 逐层返回
  ← 代理验证、复制、解包；拒绝转 SessionQueryDenied
  ← 外层 USER 使用值；按原事件契约 next，继续原通知 CORE
```

这个入口只是现成示例，不是查询前置依赖。任何已由平台绑定相应会话的 Handler 都可调用；用户不用修改 Adapter、创建 EventBus 或先调用 notify 取得上下文。Front/Supervisor 不新增 EventBus。

### 3.2 事件输入、上下文和调用者

| 对象 | 类型 | 谁提供 | 含义 | 如何使用 |
| --- | --- | --- | --- | --- |
| ops.session_context | Core 只读属性 | 平台在业务边界提供，Core 规范化冻结 | 当前 Handler 的可信会话入口；不是 await 方法，不负责采集或组装数据 | 直接读取属性；不要把用户 payload 转成可信上下文 |
| SessionContext.session_id | str | 平台业务入口 | 当前会话身份；query 参数中没有这个字段 | session-a；用户不能通过修改 Event 输入换目标 |
| SessionContext.cwd / metadata | 只读快照 | 实际业务/运行时绑定 | 目录、项目根及可用主模型/用量快照；不要求装载全部历史或 Git 数据 | repo/messages/turns 可凭身份调用底层服务 |
| current_query_origin().package | str 或 None | 查询代理从原 Ops 的包身份固定 | 发起查询的包；不是当前查询 USER Handler 所属包。直接由宿主触发且无 Ops 调用域时 helper 返回 None | query.reader；不包含一个猜测的 tier |

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

### 3.3 事件输出、拒绝与合法改写

| 事件结果键 | 类型 | 出现规则 | 含义 | 使用方式 |
| --- | --- | --- | --- | --- |
| value | 按方法定义 | 成功时唯一顶层键 | 下游或 CORE 给出的查询值；合法 None 仍须包装成 {'value': None} | 普通 Ops 调用已解包，不读取 result['value'] |
| deny | str，去空白后非空，最多 512 字符 | 拒绝时唯一顶层键 | 明确拒绝原因；不能与 value 同时出现，也不靠 raise 来代替 | return {'deny': 'WORKSPACE_QUERY_DISABLED'} |

普通 Ops 返回的是 value 本身，查询 Handler 的 next 返回的是封装结果。字段改写只改变本次查询视图，不会更换可信目标、切目录、切模型或改历史。CORE 和原始服务不使用可改写的 cwd/root/model 查询结果来重定向其他查询。

普通 USER/BUILTIN 异常及无效输出依 Core 隔离，默认读取可能继续；这不等于明确拒绝。需要拒绝就返回 deny，调用方捕获 SessionQueryDenied。取消继续传播，调用域 finally 清理；CORE/框架故障不冒充空值或成功。多个 Handler 按 USER→BUILTIN→CORE 及同层注册顺序进入，结果反向返回。多次 next 可能重复读取，不承诺查询默认缓存。

### 3.4 用户 Handler 注册示例

```python
class Extension:
    def register(self, registrar):
        registrar.register_event_handler("session.turns", self.on_query, {}, "user")

    async def on_query(self, event, ops, next_handler):
        # 这里可记录审计、检查自己的规则；默认查询用 next，不能再次调同名 Ops。
        result = await next_handler(event)
        if "deny" in result:
            return result
        return {"value": result["value"]}  # 可按本方法契约加工独立返回副本
```

短路查询可直接 `return {"deny": "QUERY_DISABLED"}` 或 `return {"value": 合法值}`；不会执行后续默认查询。无参数查询使用空匹配模式；usage 可按 `breakdown` 等参数匹配。Matcher 在 trigger 时固定，next 改写参数不会重新匹配。

## 4. 方法契约

### 4.1 签名、调用场合与填写方式

```python
turns = await ops.session.turns()
logger.info("committed user turns: %s", turns)
```

```python
usage = await ops.session.usage()
context = usage["context"]
if "percent" in context:
    logger.info("context occupancy: %s%%", context["percent"])
# 没有 cost 时不显示“费用为零”。
```

### 4.2 返回值与错误

各方法字段和单位已在第 3.2 节逐项列出。返回副本可供用户加工，不写回平台，也不自动保存到 SessionContext。结果未知、空数组、真实零值和读取失败按对应方法区分；不得统一回填零或空字符串。

无 SessionContext 时 RuntimeError；会话身份非法由平台检查拒绝；非法方法参数 TypeError。显式拒绝为 SessionQueryDenied（code=DENIED、reason 是用户收到的公开原因）。嵌套同名或间接查询循环为 SessionQueryError(code=QUERY_REENTRANT)，深度超过 16 为 QUERY_DEPTH_EXCEEDED。调用参数不能覆盖 bus/event_type。平台具体读取错误继续保留其错误类型和稳定 code。

## 5. 接入与兼容方案

### 5.1 平台装配和业务调用链

bootstrap 注册本次同名查询类型、Matcher、Validator 和唯一默认 CORE；再注册真实查询 BUILTIN（计时与独立副本）以及 ops.build BUILTIN。ops.build CORE 只创建共享 Builder，BUILTIN 登记查询代理；每个实际 Handler 获得的是共享目录全部能力与当次可信上下文，不是只装载当前事件的一个方法。

实现分工：`event_types/session_queries.py` 定义、校验和注册；`handler_ops/session_queries.py` 创建封闭代理；`query_dispatch.py` 负责 trigger、调用者归属、重入和解包；`event_handlers/session_queries.py` 提供查询 BUILTIN/CORE；真实读取位于 `server/runtime/session/query_*`。新增查询定义模块不包含 trigger，本次不迁移既有 notify 入口的位置。

内部旧直接读取辅助函数/分页服务仍可供平台和存储回归使用，但不再登记为这七个公开 Ops。查询不创建 Agent、不发模型请求；用户包卸载移除其 USER Hook，不移除平台查询事件或默认 CORE。没有全局结果缓存；并发和嵌套会话分别绑定。

### 5.2 迁移清单

| 方法 | 本次需要调整什么 |
| --- | --- |
| turns | 返回类型和默认口径保持；现在会派发 session.turns。不要再把累计收到的通知数当作权威轮次。 |
| usage | 删除 breakdown=True/False；无明细用 usage()，需要已有分类用 breakdown='summary'。旧 context.context_window.input_tokens 改成 context.tokens，limit_tokens 改成 window，occupancy_rate 改成 percent（百分比单位）。旧 turn 不再是公开结果，单轮消耗仍可从现有用量通知的输入字段获取。 |

### 5.3 可运行案例与效果

分支示例为 [examples/session_usage_info](../../examples/session_usage_info/)，统一七项示例见 [session_query_audit](../../examples/session_query_audit/)。验证复用 Token Weather 订阅的既有 context.usage.updated 通知作为入口；collect_session_parameters 返回包含七项真实查询结果的参数字典，交给用户自己的 on_parameters 业务函数。测试逐字段核对字典，并将 ui.render 设置为一旦调用就失败；不介入原 Token Weather 渲染代码。

具体例子：平台在一次真实回合后绑定 session-a 的 cwd、项目根、模型/用量观测并触发 context.usage.updated；用户 on_usage 调用 ops.session.turns / ops.session.usage，代理进入同名查询链，查询 USER 可审计，BUILTIN 计时，CORE 读取原服务；代理解包，用户消费返回参数，再 next 原用量通知。用户只安装自己的包和 Handler，不编写 Adapter 收集资料。

## 6. 验收与未决问题

| 本次实际验证 | 结果 | 范围 |
| --- | --- | --- |
| 扩展模块回归 | 632 项通过 | 生产装配、七项查询、包加载/卸载、原历史与既有扩展行为；源码使用已合入 Core bdde48a8 |
| 相关平台回归 | 140 项通过 | 原生用量入口、历史路径/写入/游标、模型选择迁移及运行配置 |
| 独立进程参数验收 | 通过，7 项返回值逐字段一致 | 真实 ZIP 包→平台 notify→context.usage.updated→七个 Ops/同名查询 Event→返回参数→卸载/释放 |
| 不进入渲染链 | ui.render 调用 0 次 | 验收中 render 一旦被调用即失败；原 Token Weather 渲染文件保持原样 |

专项用例覆盖默认结果、类型/缺失语义、拦截/短路/拒绝、故障隔离、绑定身份、并发/递归/取消、真实包加载与卸载；关联平台用例仍覆盖真实 Git/JSONL 和原存储规则。模型查询验证绑定快照与保存信息，不据此宣称执行了新的模型 API 请求。未执行的浏览器/跨进程用户操作不列为通过。

## 7. 参考资料

- [Claude 会话方法](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude OpEventOf 与拦截规则](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [S_16 查询公共协议](../specs/S_16_session-query-events.md)
- [F_27 统一实施记录](F_27_session-query-events.md)
- [内置事件开发指南](../designs/builtin-event-development-guide.md)
