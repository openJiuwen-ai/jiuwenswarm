# session.detach 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_15 / session-detach |
| 最近修订日期 | 2026-11-02 |
| 特性与验收 | [F_26](../features/F_26_session-detach.md) |

## 1. 范围与公共契约

本项在平台移除一个具体客户端与旧会话的关联后，通知插件清理该关联资源。本次公开输入对齐 surface/clientId/reason，输出对齐 clientId；reason 使用 detach/end。切换细分来源及下一会话留在只读上下文；实际运行结束会清除仍在关联的客户端并发送 end 原因，然后进入 session.end。

公开类型使用 Claude Mods 字段大小写；不把下表解释为用户可填写的任意字典。输入/输出是 frozen dataclass，通知由平台拥有可信调用作用域；USER 原样 next 并返回真实 CORE 回执。完整使用示例及差异说明见关联 feature。

| 输入字段 | 类型 / 是否必有 | 准确含义 | 填写方式与示例 | 权限 |
| --- | --- | --- | --- | --- |
| surface | RenderSurface，必填 | 该客户端离开前使用的渲染面类型，供插件选择自己的清理策略 | 从原 attach 的宿主事实回填 desktop 等枚举；不是旧窗口 ID 或下一会话的渲染面 | 只读 |
| clientId | str，必填，无省略默认 | 此次离开旧会话的客户端身份，用于日志或识别客户端 | 从原 attach 回填同一个值，如 web:view-main；一个客户端之后仍可加入 B，故迟到清理要结合旧 attachment_id | 只读；不换成新关联版本 |
| reason | str，必填，仅 detach/end | 为什么离开：detach 是明确离开，包括切换到其他会话；end 是其所在会话运行结束 | 平台按真实来源填写 detach 或 end。原 switch 不再是公开取值；需要细分切换时读取只读关联资料的 cause="switch" | 只读；不把断网/隐藏伪装为离开 |

| 输出字段 | 类型 / 是否必有 | 准确含义与消费 |
| --- | --- | --- |
| clientId | str，必填，无省略默认 | CORE 原样回显已离开的客户端。USER 返回真实 next 回执；宿主核对后继续原业务。它不证明全部外部清理成功，不恢复旧关联，也不替代 session.end 或删除结果 |


平台另在 `ops.session_context.metadata["session_attachment"]` 提供只读的本地关联资料。它不属于 Claude 的公开 Event 字段，主要用于稳定识别本平台的某次关联：

| 字段 | 类型 / 何时提供 | 准确含义、取值和作用 |
| --- | --- | --- |
| `attachment_id` | str，attach/detach 均有 | 平台生成的 32 位小写十六进制 UUID，例如 `0123456789abcdef0123456789abcdef`。离开再进入会换新值，插件按它创建和清理资源，避免迟到清理误删新资源 |
| `view_id` | str，均有 | 原 Web 稳定窗口标识，如 `view-main`。它是原业务路由的窗口身份；不是 `surface` 类型、卡片 ID 或 WebSocket ID |
| `channel_id` | str，均有 | 原渠道，如 `web`。与 view_id 共同组成平台关联槽位；不能用它推断物理设备类型 |
| `mode` | str，均有 | 关联建立时确认的会话模式，如 `agent.work.normal`。仅观察，不作为模式切换命令 |
| `previous_session_id` | str 或 None，均有 | 该关联建立之前同一窗口的旧目标；首次进入为 None，不表示旧会话已经结束 |
| `cause` | str，仅 detach 有 | 本地解除来源：`switch` 切换替换、`detach` 显式解除、`end` 运行结束。对外 reason 中前两者统一为 detach，插件确需区分来源时读这里 |
| `next_session_id` | str 或 None，仅 detach 有 | 切到另一个已跟踪主会话时为新目标 ID；主动离开、运行结束或进入未跟踪范围时为 None。当前 Ops 仍绑定旧会话，读取新目标应等待下一条 attach |

USER 不修改这些资料。Server 显式解除请求仍使用原有 `session_id / view_id / attachment_id`，与 Event 的 `surface / clientId / reason` 分工不同。

## 2. 时机、身份与不变量

每条旧关联的一次解除；重复显式解除在已无关联时返回 detached=False，不重发事件。切换先旧 detach、再新 attach；运行结束时先从表中移除该会话全部关联，再并发通知每个旧客户端，避免超时留下部分旧事实。稳定重连、页面隐藏、普通回答完成和空闲不推断为解除。

- SD-F01：平台先确定真实业务事实和 SessionContext，再实际 trigger。event_types 只定义、校验和注册，不承载业务触发；默认 CORE 只确认事实。
- SD-F02：Core 为每个实际 USER Handler 绑定真实包和本次会话；事件改写不能更换查询会话、包或关联版本。公开字段减少不等于删除内部权限和版本校验。
- SD-F03：只接受本任务仍有效的宿主作用域、完整原输入和真实 CORE 回执。短路假成功、后置改写回执、直接伪造 trigger 不构成合法生命周期操作。
- SD-F04：通知没有新增专门 BUILTIN 或同名用户 Ops；共享能力在既有 ops.build 初始化，业务触发时仅创建绑定本次上下文的 Ops。

### 延续的生命周期不变量

- SD-01：先提交解除或替换，再通知；只触发一次旧关联的 detach，不使用请求自报 previous_session_id。默认 CORE 只确认事实，不执行第二次移除。

- SD-02：按 channel_id + view_id 区分，匹配旧 attachment_id 后只移除这一项。同会话其他视图、平台历史、Agent 和会话目录保持原业务管理。

- SD-03：切换先发布旧关联 detach，再发布仍然有效的新 attach；重复同目标提交、稳定 view_id 重连不产生 detach。通知期间出现更晚替换，旧清理仍针对自己的 attachment_id，失效新 attach 不获得成功确认。

- SD-04：普通解除业务等待预算 2 秒，从通知任务启动计；用户异常/超时不恢复关联，不阻止原业务继续。原调用取消传播，已提交事实保持；忽略取消的任务不能取得迟到有效 CORE 回执，在途计数保留至退出。已发生的外部副作用不回滚。

- SD-05：输入所有字段只读，提前伪造/后置修改成功无效；原样 next 继续本条链。无新增 Ops；平台提供旧关联绑定的只读 SessionContext，不依赖用量事件，也不因会话已不可读而伪造目录/模型。

- SD-06：不将传输断开、页面隐藏、KVC 回收/暂停推断为解除。不新增前端自动关闭处理，不提供 surfaces Ops，运行结束由同一宿主结束任务先解除仍有关联并通知 reason=end，再继续 session.end；无 Team/子 Agent 新扩展。内存事实不承诺跨重启恢复或持久 exactly-once。

## 3. 平台入口、结果和兼容

没有 `ops.session.detach()`。Server 现有业务请求仍为：

```python
params = {"session_id": "demo-session-a", "view_id": "view-main",
          "attachment_id": "0123456789abcdef0123456789abcdef"}
```

三个参数均必填、无省略默认：session_id 是当前旧会话；view_id 是原窗口标识；attachment_id 是仍有效的旧关联 UUID。channel_id 来自已路由请求，不允许 params 覆盖。关联身份从平台已有资料取得；USER Handler 可以从 ops.session_context.metadata["session_attachment"] 读取自己的旧关联事实。

成功 payload 沿用 session_id、surface_id（原 view_id）、attachment_id、detached。detached=True 表示实际移除了匹配项；False 表示无现存关联，不重复通知。过期版本为 STALE_ATTACHMENT，格式错误为 BAD_REQUEST，服务未就绪为 NOT_READY；这些业务响应字段不属于 SessionDetachOutput。

[attach_notifications.py](../../../server/runtime/session/attach_notifications.py) 保留平台关联表、精确移除和 release_for_end；[detach_notifications.py](../../../server/runtime/session/detach_notifications.py) 在可信作用域实际 trigger；[默认 CORE](../../event_handlers/session_detach.py) 核对旧会话和解除事实后只回显客户端。公开 reason 不携带本地 switch 扩展值，内部 cause 保留真实来源。

普通解除通知等待预算默认 2 秒；运行结束产生的 detach 与 session.end 共用宿主 1.5 秒结束预算。某个 USER 消耗预算时，后续通知可能没有执行机会；平台事实已解除，原 Runtime 继续释放，不伪造 CORE 回执。原调用取消传播，忽略取消的代码不能取得迟到有效回执；外部副作用不自动回滚。

本次输入/输出的旧字段迁移唯一解释入口为 [F_26 第 4.2 节](../features/F_26_session-detach.md#42-旧-handler--回执迁移)。平台方法与 SessionContext 的 session_id 保持 snake_case，不将公开 camelCase 修改扩大到全部内部接口。

## 4. 验证与关联

运行验证、环境和证据范围在 [关联特性第 6 节](../features/F_26_session-detach.md#6-验收与未决问题) 维护。定向入口为 tests/unit_tests/extensions_v2/test_session_detach.py 和 test_session_lifecycle_contracts.py；Server/Runtime 原生接线与真实进程按 feature 的实际记录核对。
