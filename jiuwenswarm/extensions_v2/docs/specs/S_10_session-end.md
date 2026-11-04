# session.end 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_10 / session-end |
| 最近修订日期 | 2026-11-02 |
| 特性与验收 | [F_22](../features/F_22_session-end.md) |

## 1. 范围与公共契约

本项保留“当前主会话运行结束时，给插件一次收尾机会”的语义，不恢复旧删除专用设计。本次 session_id 改为与 Claude 一致的公开 sessionId；reason/resume.id 保持，默认回执仅含 sessionId。平台的真实运行代次、去重和停止结果仍留在 Runtime，不加入公开字段。

公开类型使用 Claude Mods 字段大小写；不把下表解释为用户可填写的任意字典。输入/输出是 frozen dataclass，通知由平台拥有可信调用作用域；USER 原样 next 并返回真实 CORE 回执。完整使用示例及差异说明见关联 feature。

| 输入字段 | 类型 / 是否必有 | 准确含义 | 填写方式与示例 | 权限 |
| --- | --- | --- | --- | --- |
| reason | str，必填，无省略默认 | 为什么结束本次运行，用于日志、matcher 和用户收尾策略；不表示是否永久删除 | 宿主按真实业务填写 clear / resume / logout / prompt_input_exit / other。当前资源释放与正常停机为 other，不填 session_deleted | 只读；不能据此取消结束 |
| sessionId | str，必填，无省略默认 | 本次结束的主会话身份，用于定位本包按会话维护的资源 | Runtime 已登记且已停稳的真实会话 ID，如 demo-session-a；须与 ops.session_context.session_id 一致，不填窗口 ID 或包 ID | 只读；Python 也使用这一大小写 |
| resume | SessionResume，必填 | 以后可用于恢复该持久会话身份的资料对象；不携带历史正文或授权 | 平台填写 SessionResume(id="demo-session-a")；USER 使用 value.resume.id 属性，不把它当 dict 索引 | 对象及嵌套字段只读 |
| resume.id | str，嵌套必填 | 原会话的恢复 ID。可保存到用户摘要，之后交由平台原恢复入口使用 | 等于本次 sessionId，如 demo-session-a。没有写过历史、关闭持久化或存档后来被删除时，不能保证可恢复 | 只读；不是一次性访问令牌 |

| 输出字段 | 类型 / 是否必有 | 准确含义与消费 |
| --- | --- | --- |
| sessionId | str，必填，无省略默认 | 默认 CORE 原样回显实际结束会话。USER 返回 next 的真实结果，宿主核对后继续原释放。它不证明进程已经退出、外部资源全部释放或历史删除成功 |

## 2. 时机、身份与不变量

一个已登记且停稳的 Runtime 主会话代次最多尝试一次。重复 cleanup 或随后 close 不重放；同一持久 ID 恢复后会登记新 generation，新运行结束可再次通知。普通回复完成、取消当前生成、idle、单纯界面切换/关联变化和传输重连不触发。删除已登记运行也会结束它；仅有离线存档时不虚构运行结束。

- SE-F01：平台先确定真实业务事实和 SessionContext，再实际 trigger。event_types 只定义、校验和注册，不承载业务触发；默认 CORE 只确认事实。
- SE-F02：Core 为每个实际 USER Handler 绑定真实包和本次会话；事件改写不能更换查询会话、包或关联版本。公开字段减少不等于删除内部权限和版本校验。
- SE-F03：只接受本任务仍有效的宿主作用域、完整原输入和真实 CORE 回执。短路假成功、后置改写回执、直接伪造 trigger 不构成合法生命周期操作。
- SE-F04：通知没有新增专门 BUILTIN 或同名用户 Ops；共享能力在既有 ops.build 初始化，业务触发时仅创建绑定本次上下文的 Ops。

### 延续的生命周期不变量

- SE-01：仅 Worker 持有 EventBus。装配注册类型、Matcher、Validator、默认 CORE，沿用共享 Builder/现有 Ops；无同名 Ops 或 Core API 变更。

- SE-02：AgentRuntime.cleanup_session 和 Runtime.close 终止真实已登记运行时，先停稳执行，再在原内存资源释放前调用固定宿主结束参与者。永久删除只有确实终止活跃运行时经过该通知；离线存档删除不虚构运行。

- SE-03：回复完成、取消生成、idle、界面切换/attach/detach 和 Supervisor 临时断连不等于结束。明确 client.disconnect 若实际释放运行，则通知 other；没有实际关闭路径的强杀/崩溃不保证通知。

- SE-04：按 Runtime 内 session_id + Coordinator generation 合并至多一次通知尝试；重复 cleanup/随后停机不重放。同 ID 恢复形成新代次，新代次结束可再次通知；新登记等待旧代次通知和资源清理完成。

- SE-05：USER 保存自己的状态或释放自己的资源，再 next；CORE 核对有效宿主调用、原输入、绑定会话，回显真实 sessionId。平台继续原释放；事件不删除历史、不重新创建 Agent、不发送额外输入。

- SE-06：所有输入及嵌套 resume.id 只读；伪造目标、提前回执、next 后替换身份无效，沿用既有 USER 故障隔离。作用域外无法自行 trigger 一个有效结束通知。

- SE-07：整个通知等待预算默认 1.5 秒。异常/超时记日志并继续释放；超时失效作用域并请求取消，迟到任务不能再生成有效回执；包在途计数直到任务真实退出才释放，不承诺回滚外部副作用。

- SE-08：Runtime.close 停稳执行后并发通知各主会话，之后释放资源并最后卸载扩展；不按会话数串行累计预算。停稳超时不伪造该代次正常结束。

- SE-09：SessionContext 绑定真实目标；cwd 未确认则 None，project_root 仅使用已确认绝对目录；不使用进程 cwd 或初始化模型兜底。ops.session_context 是已有只读属性。

- SE-10：固定宿主结束观察者与动态 KVC activity/delete 参与者分开组合。无扩展时保留原释放行为；卸载只撤销用户订阅。无跨进程重启 exactly-once，收尾必须幂等。

## 3. 平台入口、结果和兼容

没有新增 `ops.session.end()`。宿主已有 Python 入口为：

```python
await runtime.cleanup_session(channel_id="web", session_id="demo-session-a")
await runtime.close()
```

这些平台方法仍采用 Python session_id 参数，不是 Event 的公开 sessionId。end_reason 可由宿主传入 SessionRunEndReason 五值之一，默认 OTHER；无效原因在资源变化前拒绝。cleanup_session 返回原 Manager 清理 bool，close 返回 None；不拿事件回执冒充最终清理成功。reset_plan_state 沿用原 API 与删除事务补偿规则。

[Runtime](../../../runtime/service.py) 持有停稳、代次去重和实际释放；[平台通知模块](../../../server/runtime/session/end_notifications.py) 构造上下文、设置预算、在途计数和实际 trigger；[默认 CORE](../../event_handlers/session_end.py) 校验原输入和绑定身份，仅返回 sessionId。Server 组合入口把关联清理回调接入同一结束任务；独立 Runtime 没有关联表时无需创建一个。

宿主结束任务默认总预算 1.5 秒，包括关联收尾与 end 通知。失败/超时记日志后继续原释放，不允许 USER 拒绝或替换结果；原关闭取消仍按实际取消传播。已尝试不自动重放，忽略取消的任务可能晚退出，在途计数直到实际退出才释放。崩溃、强杀或执行未能停稳时不保证正常结束 Hook；不承诺撤销已提交外部操作。

本次输入/输出的旧字段迁移唯一解释入口为 [F_22 第 4.2 节](../features/F_22_session-end.md#42-旧-handler--回执迁移)。平台方法与 SessionContext 的 session_id 保持 snake_case，不将公开 camelCase 修改扩大到全部内部接口。

## 4. 验证与关联

运行验证、环境和证据范围在 [关联特性第 6 节](../features/F_22_session-end.md#6-验收与未决问题) 维护。定向入口为 tests/unit_tests/extensions_v2/test_session_end.py 和 test_session_lifecycle_contracts.py；Server/Runtime 原生接线与真实进程按 feature 的实际记录核对。
