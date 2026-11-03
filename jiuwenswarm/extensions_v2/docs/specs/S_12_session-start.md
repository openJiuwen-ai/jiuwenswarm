# session.start 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_09 / session-start |
| 最近修订日期 | 2026-11-03 |
| 特性与验收 | [F_21](../features/F_21_session-start.md) |

## 1. 范围与公共契约

本项保留每个 Worker 包激活实例在首次受支持原生输入前至多尝试一次初始化通知的粒度。公开输入仍只有 cwd、surface、isInteractive；包身份、真实激活记录、generation、发布的 Handler IDs 和请求路由仍由平台持有。宿主在发布 `session.start` Handler 时用其已核实的 PackageMetadata 绑定私有包匹配条件；Gate 在本次调用中绑定目标包，使用 Core 原有 matcher 派发，不依赖 Core 调用级 `handler_ids` 接口，也不改 session.append。

公开类型使用 Claude Mods 字段大小写；不把下表解释为用户可填写的任意字典。输入/输出是 frozen dataclass，通知由平台拥有可信调用作用域；USER 原样 next 并返回真实 CORE 回执。完整使用示例及差异说明见关联 feature。

| 输入字段 | 类型 / 是否必有 | 准确含义 | 填写方式与示例 | 权限 |
| --- | --- | --- | --- | --- |
| cwd | str，必填，无省略默认 | 首次受支持输入实际运行的绝对目录，供插件定位初始化资料 | 平台从已绑定 SessionContext.cwd 填写，如 D:/projects/demo/src；不填写项目名、相对路径或 Worker 进程目录。缺失/相对目录在进入初始化尝试前拒绝 | 只读；与绑定 cwd 一致 |
| surface | RenderSurface 或 None，必填 | 初始化时已知的渲染面类型，供插件选择自己的呈现策略 | 宿主明确声明 terminal / desktop / mobile / vscode 之一；不知道时填 None。当前 Web 为 desktop；不是窗口 ID 或 ui.render 槽位 | 只读；不表示窗口已挂载 |
| isInteractive | bool，必填，无省略默认 | 本次原生请求是否支持用户交互，便于插件选择非交互准备流程 | 平台取 supports_user_interaction 的真实能力，True/False，不填字符串。当前原生 Adapter 缺省能力按原行为视为 True | 只读；不作为授权，也不保证已有 UI |

| 输出字段 | 类型 / 是否必有 | 准确含义与消费 |
| --- | --- | --- |
| cwd | str，必填 | CORE 原样回显本次可信绝对目录。USER 返回 next 的真实结果；Gate 据此确认本通知经过 CORE。它不改变目录，不保证所有用户外部初始化均成功，也不表示 Agent 已完成回答 |

## 2. 时机、身份与不变量

每个激活实例至多一次通知尝试；并发输入串行等待初始化门。安装/加载只发布订阅，第一次受支持原生输入才进入此门；成功重载/重装的新实例在下一次受支持输入前获得机会，失败重载不重复旧实例。控制请求、历史查询、打开/切换会话、重连、普通回合完成不触发。本范围沿用原生 Adapter，不增加 Team、Auto-Harness、外部 Harness 或前端接线。

- SS-F01：平台先确定真实业务事实和 SessionContext，再实际 trigger。event_types 只定义、校验和注册，不承载业务触发；默认 CORE 只确认事实。
- SS-F02：Core 为每个实际 USER Handler 绑定真实包和本次会话；事件改写不能更换查询会话、包或关联版本。公开字段减少不等于删除内部权限和版本校验。
- SS-F03：只接受本任务仍有效的宿主作用域、完整原输入和真实 CORE 回执。短路假成功、后置改写回执、直接伪造 trigger 不构成合法生命周期操作。
- SS-F04：通知没有新增专门 BUILTIN 或同名用户 Ops；共享能力在既有 ops.build 初始化，业务触发时仅创建绑定本次上下文的 Ops。

### 延续的生命周期不变量

SS-01：仅由 Worker 持同一 EventBus，真实原生 chat.send 的 unary/stream child 在权限准入、模型应用、运行目录绑定后且执行模型前等待初始化门。控制请求、重连、历史查询、其他 Harness、Team 和 Auto-Harness 不借此新增启动通知。

SS-02：每个 Worker 的包激活实例最多一次通知尝试。平台保留公开 ActivePackageRecord 对象引用（含 generation）及公开发布 ID，不只比较代次数值；可区分卸载重装后 generation=1 且发布列表为空的新实例。不按每个会话重新触发；成功重载/重新安装的新实例在下一次受支持输入前通知，失败重载保留旧实例状态。

SS-03：宿主 EventBus 在发布 `session.start` Handler 时从真实 PackageMetadata 注入私有包匹配键；扩展不能自填该键。Gate 的可信调用作用域持有目标包 ID，事件 matcher 先比较包目标，再按用户声明的公开字段 pattern 匹配。用户传 `{}` 仍只进入本包 Handler；默认 CORE 对每次合法调用保留真实回执。公开 Event 不含包 ID，无新增用户 Ops。

SS-04：并发输入串行等待初始化门；回调期间不持管理操作锁或文件锁。Runtime.begin_calls/end_calls 成对保护卸载，退出/异常/取消均释放计数。重载沿 Core 热切换，门重新检查当前实例。

SS-05：调用开始即记录本实例已尝试。USER 错误按 Core 隔离；框架错误或取消传播给当前输入，不自动重放已开始的通知。普通输入重试不重复副作用，需要重新激活才能重新尝试。

SS-06：同任务或尚在初始化中的子任务递归进入门明确报错，不死锁；Worker 进程重启丢失内存状态，重新激活后重新通知。卸载不删除其他会话/包状态，不冒充资源清理。

## 3. 平台入口、结果和兼容

无新增 `ops.session.start()`。平台方法仍为：

```python
await manager.ensure_session_started(
    context, request_id="request-a", channel_id="web",
    is_interactive=True, surface="desktop",
)
```

context 是 Adapter 已收集的可信资料；request_id/channel_id 仍为原路由观察值，不参与公开 Event 字段。surface 是宿主声明，默认 None；is_interactive 是平台方法的 Python 参数，对应公开字段 isInteractive。方法返回本次新尝试实例的 SessionStartOutput 元组；没有新实例返回空元组。

初始化门为 [management/session_start.py](../../management/session_start.py)，注册期包匹配和事件 matcher 为 [event_types/session_start.py](../../event_types/session_start.py)，真实 Adapter 接线为 [interface_deep.py](../../../server/runtime/agent_adapter/interface_deep.py)。默认 CORE 核对私有调用作用域里的 session_id，并核对绑定 cwd 与公开 cwd 一致，生成一次真实 cwd 回执。原激活记录和发布 ID 的去重逻辑保持；这些 ID 不再传给 Core `trigger()`。

USER 普通异常沿 Core 隔离；框架错误和原输入取消传播，不自动重放已经开始的初始化尝试。调用开始即标记已尝试，外部副作用不自动回滚。门拒绝重入，重载仍沿 Core 热切换并重新核对真实实例；在途计数成对保护卸载。

本次输入/输出的旧字段迁移唯一解释入口为 [F_22 第 4.2 节](../features/F_22_session-start.md#42-旧-handler--回执迁移)。平台方法与 SessionContext 的 session_id 保持 snake_case，不将公开 camelCase 修改扩大到全部内部接口。

## 4. 验证与关联

运行验证、环境和证据范围在 [关联特性第 6 节](../features/F_22_session-start.md#6-验收与未决问题) 维护。定向入口为 tests/unit_tests/extensions_v2/test_session_start.py 和 test_session_lifecycle_contracts.py；Server/Runtime 原生接线与真实进程按 feature 的实际记录核对。
