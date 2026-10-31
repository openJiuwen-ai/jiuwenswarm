# session.start 激活初始化规约

最近修订：2026-10-29。关联 [F_22](../features/F_22_session-start.md)，计划 EXTV2-18 / PR06。以下为本次已实现契约；实际验证范围和证据在 F_22 维护，交付进度以 EXTV2-18 为准。

## 1. 输入与输出

SessionStartInput 为 frozen dataclass：package_id:str、generation:int、session_id:str、request_id:str、channel_id:str、cwd:str|None、project_root:str|None、is_interactive:bool。无可改写字段。包/代次来自 Core 发布事实，会话来自平台实际输入，目录来自已绑定运行时；不从用户事件或进程默认资料推断其他会话。

SessionStartOutput 为 frozen dataclass：accepted:bool、package_id:str、generation:int、session_id:str、request_id:str。默认 CORE 返回 accepted=True，表示本通知链完成，不代表所有 USER 外部初始化成功。合法 USER 保留真实下游回执；提前伪造成功、改写身份/目录、直接触发无可信调用均不构成合法初始化。matcher 用输入字段相等匹配，空字典匹配当前被选中的本包 Handler。

所有输入/输出字段均由平台显式构造，没有可省略的构造参数；cwd/project_root 可为 None，request_id/channel_id 保留平台字符串，不据此决定路由。is_interactive 表示该原生请求支持用户交互的能力，不声称已经关联 RenderSurface。matcher 对这一次通知筛选，未匹配不延期到以后输入。

### 字段契约表

| 输入字段 | 类型 | 含义 | 默认与可改写 |
| --- | --- | --- | --- |
| package_id | str | 平台选中的包 ID | 必须显式传入；只读 |
| generation | int | 当前激活记录的代次 | 必须显式传入；只读，不单独作为实例唯一标识 |
| session_id | str | 首个受支持输入的真实会话 ID | 必须显式传入；只读 |
| request_id | str | 该原生输入的请求标识 | 必须显式传入，平台无值时为空串；只读 |
| channel_id | str | 该原生输入的渠道标识 | 必须显式传入，平台无值时为空串；只读，不作为 surface 保证 |
| cwd | str 或 None | 已绑定的实际工作目录 | 必须显式传入，允许 None；只读 |
| project_root | str 或 None | 已绑定项目根；无项目资料为 None | 必须显式传入；只读 |
| is_interactive | bool | 原生请求支持用户交互的能力 | 必须显式传入；Adapter 缺省能力视为 True，不表示已有 UI 挂载 |

| 输出字段 | 类型 | 含义 | 默认与可改写 |
| --- | --- | --- | --- |
| accepted | bool | 真实默认通知链已完成 | 构造参数必填；默认 CORE 生成 True，不代表所有用户资源初始化成功 |
| package_id / generation | str / int | 对应真实通知目标 | 构造参数必填；必须保留 CORE 回执 |
| session_id / request_id | str / str | 对应首个输入 | 构造参数必填；必须保留 CORE 回执 |

## 2. 时机和执行

SS-01：仅由 Worker 持同一 EventBus，真实原生 chat.send 的 unary/stream child 在权限准入、模型应用、运行目录绑定后且执行模型前等待初始化门。控制请求、重连、历史查询、其他 Harness、Team 和 Auto-Harness 不借此新增启动通知。

SS-02：每个 Worker 的包激活实例最多一次通知尝试。平台保留公开 ActivePackageRecord 对象引用（含 generation）及公开发布 ID，不只比较代次数值；可区分卸载重装后 generation=1 且发布列表为空的新实例。不按每个会话重新触发；成功重载/重新安装的新实例在下一次受支持输入前通知，失败重载保留旧实例状态。

SS-03：Core trigger 的 handler_ids 为该包公开发布 ID，仍按原 matcher/层级/顺序执行，其他包的空 matcher 也不被误调用。默认 CORE 保留真实回执；无新增用户 Ops。

SS-04：并发输入串行等待初始化门；回调期间不持管理操作锁或文件锁。Runtime.begin_calls/end_calls 成对保护卸载，退出/异常/取消均释放计数。重载沿 Core 热切换，门重新检查当前实例。

SS-05：调用开始即记录本实例已尝试。USER 错误按 Core 隔离；框架错误或取消传播给当前输入，不自动重放已开始的通知。普通输入重试不重复副作用，需要重新激活才能重新尝试。

SS-06：同任务或尚在初始化中的子任务递归进入门明确报错，不死锁；Worker 进程重启丢失内存状态，重新激活后重新通知。卸载不删除其他会话/包状态，不冒充资源清理。

## 3. 上下文与边界

真正的 bus.trigger 位于 management/session_start.py 平台初始化门；event_types 只维护定义、校验作用域和注册，CORE 位于 event_handlers。EventBus 给 USER 绑定可信 PackageMetadata 和 SessionContext；原 cwd/root/model/repo 可读。事件字段只是观察资料，不能改变真实运行目录/目标/包归属。本项不发送输入、不追加历史、不测量或计费、不渲染 UI，不依赖 PR04/05。

初始化的外部副作用不回滚；at-most-once 是平台通知尝试语义，不承诺断电后全局 exactly-once。已开始请求与并发热加载沿既有快照语义，不据此承诺对所有业务事件的全局激活屏障。

## 4. 验证入口

使用 tests/unit_tests/extensions_v2/test_session_start.py 和原生接线测试。Server 253 项、配套 Core 568 项通过（1 项环境权限跳过）；2026-10-29 真实 Worker 7 个模型回合、初始化及后续用量 USER Handler、卸载与退出清理全部通过。具体场景、命令、版本与边界见 F_22 第 6 节。
