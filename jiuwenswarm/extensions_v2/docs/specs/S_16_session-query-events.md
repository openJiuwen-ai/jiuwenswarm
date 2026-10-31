# 会话七项查询事件公共协议

最近修订 2026-10-31；本分支已实现。主责模块为 event_types/session_queries.py、query_dispatch.py、handler_ops/session_queries.py、event_handlers/session_queries.py。实施记录见 [F_27](../features/F_27_session-query-events.md)。

## 1. 能力、粒度与调用位置

七个公开 Ops 各触发一次同名查询 Event，在同一个 Worker EventBus 内执行 USER→BUILTIN→CORE 并反向返回。查询事件不是状态变化通知；无调用不自动触发。业务入口先提供可信 SessionContext，用户不改 Adapter、不创建 Bus，也不通过参数指定另一个目标。

| Event / Ops 名称 | 事件输入 | Ops 解包后的类型 |
| --- | --- | --- |
| session.cwd | {}（无参数） | str 或 None |
| session.root | {}（无参数） | str 或 None |
| session.model | {}（无参数） | str 或 None |
| session.repo | {}（无参数） | dict 或 None |
| session.messages | {}（无参数） | list[dict] |
| session.turns | {}（无参数） | int 或 None |
| session.usage | 可选 breakdown/columns | dict |

## 2. 事件参数与可信上下文

六个无参数事件的输入必须为空 Mapping，不能添加 session_id/cwd/model/origin。usage 仅接受 breakdown/columns：breakdown 为 summary/full；columns 为正整数且不是 bool。初始参数为只读映射；USER 改写 usage 参数应复制成新对象并传 next，所有参数在 next 边界重新验证。Matcher 只在 trigger 初始匹配，参数改写不重新匹配。

| 字段 / 对象 | 类型 / 默认 | 谁提供 | 准确含义 | 怎么填写或消费 |
| --- | --- | --- | --- | --- |
| ops.session_context | Core 只读属性 | 平台在业务边界提供，Core 规范化冻结 | 当前 Handler 的可信会话入口；不是 await 方法，不负责采集或组装数据 | 直接读取属性；不要把用户 payload 转成可信上下文 |
| SessionContext.session_id | str | 平台业务入口 | 当前会话身份；query 参数中没有这个字段 | session-a；用户不能通过修改 Event 输入换目标 |
| SessionContext.cwd / metadata | 只读快照 | 实际业务/运行时绑定 | 目录、项目根及可用主模型/用量快照；不要求装载全部历史或 Git 数据 | repo/messages/turns 可凭身份调用底层服务 |
| current_query_origin().package | str 或 None | 查询代理从原 Ops 的包身份固定 | 发起查询的包；不是当前查询 USER Handler 所属包。直接由宿主触发且无 Ops 调用域时 helper 返回 None | query.reader；不包含一个猜测的 tier |

current_query_origin 是 Server 的辅助函数，返回冻结的 SessionQueryOrigin(package)；不是 Core 已有的 next.origin，也不猜测当前调用层级。查询 Hook B 的 ops.package_metadata 属于 B，origin.package 则保留发起调用的包 A。普通宿主直接 trigger 查询时可没有该调用域；依然必须提供有效会话。

## 3. 事件输出、Ops 解包与错误

| 字段 | 类型 | 出现条件 | 含义 | 调用方式 |
| --- | --- | --- | --- | --- |
| value | 按方法定义 | 成功时唯一顶层键 | 下游或 CORE 给出的查询值；合法 None 仍须包装成 {'value': None} | 普通 Ops 调用已解包，不读取 result['value'] |
| deny | str，去空白后非空，最多 512 字符 | 拒绝时唯一顶层键 | 明确拒绝原因；不能与 value 同时出现，也不靠 raise 来代替 | return {'deny': 'WORKSPACE_QUERY_DISABLED'} |

事件输出必须是只含 value 或只含 deny 的映射；不允许裸值、同时包含两键、None 整体返回或未知键。数据需为有限、可复制的 JSON 兼容结构；各查询按专项 spec 验证 value。比如 cwd 的成功未知是 {'value': None}，messages 的成功空数组是 {'value': []}。普通 Ops 调用者只收到 value。

deny 转 SessionQueryDenied(code=DENIED, reason=原原因)。普通 USER/BUILTIN 抛错、无效输出按 Core 隔离规则处理，可能继续默认 CORE；不能用抛错代替有业务意义的拒绝。CORE/框架故障及取消传播，不把错误包装成成功空值。调用者若不捕获其 Ops 错误，外层 USER 也遵循原事件故障隔离，不能把外层继续当作查询成功。

## 4. 结果视图、顺序与重入

USER 可在 next 前后增加操作、返回合法替代值或明确拒绝；BUILTIN 默认计时并返回独立副本；CORE 完成平台真实默认读取。任何结果改写只影响本次返回视图，不改变 SessionContext、执行目录、模型、Git、历史或计量。权限与目标根仍由原始平台服务维护，不依据可改写的其他查询结果决定。

同一层按登记顺序执行，next 推进当前查询链；Ops 方法触发另一条查询链。多次 next 可能重复执行下游，不默认缓存；无全局结果缓存。共享 Builder 只在启动构建一次，每个实际 Handler 获得的是全部已登记能力与当次会话/包绑定。

同一 Bus/会话/事件在活跃查询栈中重复进入报 QUERY_REENTRANT，A→B→A 同样拒绝；活跃深度超过 16 报 QUERY_DEPTH_EXCEEDED。获取默认值应使用 next，不再次调用同名 Ops。调用域在 finally 清理，失效父帧不会污染继承它的子任务；当前 Core 不提供 Claude 跳过调用 Hook 的接口，本实现不宣称完全相同。

## 5. 逐项字段主责与迁移

- cwd/root：[S_03](S_03_session-workspace-ops.md)
- model：[S_04](S_04_session-model-ops.md)，返回字符串或 None
- repo：[S_05](S_05_session-repo-ops.md)，返回 root/remote/internal/name
- messages：[S_06](S_06_session-history-ops.md)，无参数数组；产品分页服务保留在内部
- turns/usage：[S_11](S_11_session-usage-ops.md)，usage 对齐 context/rateLimits/可选 cost

不改变 append、files/read_file，也不扩展到 Team 或新的公共网络入口。原 Token Weather 渲染代码不改；验证只复用它订阅的 context.usage.updated 作为后端入口，收集七项返回参数。
