# 会话七项查询事件公共协议

最近修订 2026-11-02；本分支已实现。主责模块为 event_types/session_queries.py、query_dispatch.py、handler_ops/session_queries.py、event_handlers/session_queries.py。本次仅归整编号、引用及文档归属，运行契约不变；逐项特性与历史验证入口见第 7 节。

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
- turns/usage：[S_07](S_07_session-usage-ops.md)，usage 对齐 context/rateLimits/可选 cost

不改变 append、files/read_file，也不扩展到 Team 或新的公共网络入口。原 Token Weather 渲染代码不改；验证只复用它订阅的 context.usage.updated 作为后端入口，收集七项返回参数。


### 5.1 既有调用迁移

| 旧代码涉及的方法 | 迁移方式 |
| --- | --- |
| cwd | 方法返回类型保持；内部现在派发 session.cwd。用户 Handler 新增拦截后可改变本次返回视图，真实执行目录不变。 |
| root | 方法返回类型保持；内部现在派发 session.root。改写查询值不会改变平台项目绑定或文件访问根。 |
| model | 旧 info['model_name'] / info.get('provider') 等访问改为直接消费字符串；需要模型配置详情的业务应使用自己已有的配置能力，不从名称反推供应商、凭据或激活状态。 |
| repo | repo_root 改为 root；移除 branch/head/detached 访问。注意 linked worktree 的 root 现在对齐 Claude 的主工作树根；当前执行目录仍由 cwd() 给出。平台内部原 Git 身份读取服务保留。 |
| messages | 删除 limit/cursor 参数和 page 包装；page['messages'][0]['content'] 改为 messages[-1]['text']。只需少量条目时在返回数组切片；产品原分页服务保留在平台内部，不作为新的第八个 Ops 暴露。 |
| turns | 返回类型和默认口径保持；现在会派发 session.turns。不要再把累计收到的通知数当作权威轮次。 |
| usage | 删除 breakdown=True/False；无明细用 usage()，需要已有分类用 breakdown='summary'。旧 context.context_window.input_tokens 改成 context.tokens，limit_tokens 改成 window，occupancy_rate 改成 percent（百分比单位）。旧 turn 不再是公开结果，单轮消耗仍可从现有用量通知的输入字段获取。 |

调用者可从任何已绑定会话的业务 Handler 使用，不必须依赖用量通知。六项无参数，usage 可用参数对象或命名参数（两者不能混用）。返回值是独立视图，不修改平台真实状态、不缓存进 SessionContext、不自动触发界面更新。

## 6. 平台接入与资源边界

平台启动时先登记七项事件及唯一默认 CORE，再登记共享 BUILTIN 和 Ops Build Handler；现有 ops.build 创建一次方法目录。每个实际 Handler 的 Ops 由该目录及当次可信会话、包身份构造，业务调用不重新触发 ops.build，也不提前计算全部查询结果。

实际读取由 server/runtime/session/query_views.py、query_repo.py、query_messages.py 和既有平台服务承担。query_dispatch.py 负责可信绑定、调用来源、重入保护、trigger 和解包；event_types/session_queries.py 只定义、校验和注册查询，不承载业务触发。平台内部的分页、模型详情、Git 分支/HEAD 和用量存储服务继续由原流程使用；旧直接读取帮助函数不作为当前公开查询 Ops 登记。

用户包卸载/重载只撤销自己的 Hook，不撤销平台事件、默认 CORE 或共享 Ops。用户自有参数与缓存由插件管理；查询结果不写入 SessionContext，也不自动渲染。默认查询不发模型请求；显式 full 用量明细按专项规约拒绝，不悄悄降级或测量。其他已绑定可信上下文的业务 Handler 同样可以调用，不以 notify_context_usage 为前置。

## 7. 特性与验收索引

| Ops / Event | 独立特性 | 专项字段主责 |
| --- | --- | --- |
| session.cwd | [F_13](../features/F_13_session-cwd.md) | [S_03](S_03_session-workspace-ops.md) |
| session.root | [F_19](../features/F_19_session-root.md) | [S_03](S_03_session-workspace-ops.md) |
| session.model | [F_14](../features/F_14_session-model.md) | [S_04](S_04_session-model-ops.md) |
| session.repo | [F_15](../features/F_15_session-repo.md) | [S_05](S_05_session-repo-ops.md) |
| session.messages | [F_18](../features/F_18_session-messages.md) | [S_06](S_06_session-history-ops.md) |
| session.turns | [F_20](../features/F_20_session-turns.md) | [S_07](S_07_session-usage-ops.md) |
| session.usage | [F_17](../features/F_17_session-usage.md) | [S_07](S_07_session-usage-ops.md) |

逐项字段、用户案例和独立验收在各 feature 维护；共享历史实施记录见[EXTV2-22 历史验证记录](../designs/extensions-v2-development-plan.md#extv2-22-七项查询事件化历史验证记录)，编号沿革见[文档编号映射](../designs/extensions-v2-development-plan.md#extv2-22-文档编号映射)。历史验证保留原日期和范围，本次文档整理不构成业务、模型或浏览器测试的新结果。

对照依据为 Claude Code Mods 2.1.277 的固定公开类型声明，字段差异及本地未知值语义由各专项规约维护：

- [Claude OpEventOf](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [Claude Session API](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude 查询输出](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5832-L5867)
