# session.attach 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_11 / session-attach |
| 最近修订日期 | 2026-11-02 |
| 特性与验收 | [F_23](../features/F_23_session-attach.md) |

## 1. 范围与公共契约

本项在具体 Web 视图与有效主会话的关联提交后，通知用户准备该关联的资料或资源。本次把公开输入收敛为 surface/clientId/viewport，默认输出收敛为 clientId；会话、渠道和关联版本保留为可信上下文或宿主状态。原 session.create/session.switch 接线保持，不改前端、不新增同名 Ops。

公开类型使用 Claude Mods 字段大小写；不把下表解释为用户可填写的任意字典。输入/输出是 frozen dataclass，通知由平台拥有可信调用作用域；USER 原样 next 并返回真实 CORE 回执。完整使用示例及差异说明见关联 feature。

| 输入字段 | 类型 / 是否必有 | 准确含义 | 填写方式与示例 | 权限 |
| --- | --- | --- | --- | --- |
| surface | RenderSurface，必填 | 该客户端使用哪种渲染面，便于插件选择兼容的展示逻辑 | 宿主明确填写 terminal / desktop / mobile / vscode；当前 Web 声明 desktop。不能填写 view-main、会话 ID、槽位 ID 或 web 渠道名 | 只读 |
| clientId | str，必填，无省略默认 | 加入该会话的具体客户端身份；同一种 surface 上可有多个客户端 | 当前宿主从 channel_id + ":" + view_id 生成，如 web:view-main。不是 attachment_id；同一窗口切换会话仍是同一个客户端 | 只读；不作为授权凭据 |
| viewport | RenderViewport 或 None，可省略，默认 None | 宿主已知的整个渲染面的字符尺寸和可选全屏状态，供插件布局判断 | 真实上报时填写 RenderViewport(columns=120, rows=40, isFullscreen=False)。当前原生入口未采集，填 None；不用浏览器像素或虚构默认值 | 对象及嵌套字段只读 |

| 输出字段 | 类型 / 是否必有 | 准确含义与消费 |
| --- | --- | --- |
| clientId | str，必填，无省略默认 | 默认 CORE 原样回显本次客户端。USER 原样返回 next 的真实回执；平台继续核对当前关联和目标。它不是切换命令，不证明插件已展示成功，也不保证返回后永远不再切换 |

#### viewport 的全部字段

`viewport` 是 `RenderViewport` 对象；Python 的 None 表示该可选对象未上报。当前原生 Web 关联入口未采集字符尺寸，所以实际通知为 None；没有默认填入 80×24，也不拿浏览器像素冒充字符格。宿主以后提供真实测量时使用下列形状：

```python
from jiuwenswarm.extensions_v2.event_types.session_surface import RenderViewport

RenderViewport(columns=120, rows=40, isFullscreen=False)
```

| 字段 | 类型 / 默认值 | 准确含义、应填写什么及作用 | Handler 权限 |
| --- | --- | --- | --- |
| columns | int，必填 | 整个渲染面的横向字符格数，须为正整数，如 120。用于布局换行与宽度判断；不是像素宽度，也不是某张卡片的宽度 | 只读 |
| rows | int，必填 | 整个渲染面的纵向字符格数，须为正整数，如 40。用于读取当前可见尺寸；不是某组件剩余可用行数 | 只读 |
| isFullscreen | bool 或 None，默认 None | 宿主上报的全屏呈现状态：True/False 是明确的状态，None 是尚未上报。不能把未知填成 False，也不据此承诺已打开侧栏 | 只读 |



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

每个 channel+view 的一次新主会话关联通知一次。不同窗口分别通知；相同现存关联重放、稳定 view_id 的重连不重复通知；离开再回来是新关联。create 保持原响应先交付、后后台提交，switch 保持原提交等待。仅包安装或 ui.ready 不补发已有关联。

- SA-F01：平台先确定真实业务事实和 SessionContext，再实际 trigger。event_types 只定义、校验和注册，不承载业务触发；默认 CORE 只确认事实。
- SA-F02：Core 为每个实际 USER Handler 绑定真实包和本次会话；事件改写不能更换查询会话、包或关联版本。公开字段减少不等于删除内部权限和版本校验。
- SA-F03：只接受本任务仍有效的宿主作用域、完整原输入和真实 CORE 回执。短路假成功、后置改写回执、直接伪造 trigger 不构成合法生命周期操作。
- SA-F04：通知没有新增专门 BUILTIN 或同名用户 Ops；共享能力在既有 ops.build 初始化，业务触发时仅创建绑定本次上下文的 Ops。

### 延续的生命周期不变量

- SA-01：复用现有 session.create/session.switch 提交后的 Runtime foreground_changed(visible=True) 事实；Worker 平台关联表先确认并提交，再 trigger。create 保持先交付响应、后异步发布；switch 保持提交时等待该通知流程。无新增客户端方法或前端改动。

- SA-02：传输 Gateway attach、ui.ready、输入意图、执行开始/完成、单纯隐藏/空闲不等于界面进入目标会话。没有有效视图或有效主会话时不生成成功关联通知。

- SA-03：内部关联按 channel_id + view_id 标识。同会话不同视图分别通知；同视图同会话重放或稳定 view_id 的断线重连不重复通知；同视图切换目标、再切回会得到新的关联 ID。Worker/Runtime 重启后仅从后续真实提交重建，不承诺持久 exactly-once。

- SA-04：只有仍为表中当前关联、且默认 CORE 再确认目标会话可用的调用才能取得有效 CORE 回执；更新目标、超时或作用域退出使旧调用失效。已经发生的 USER 副作用不自动回滚，用户应按关联 ID 幂等。

- SA-05：USER 改输入、提前伪造成功、后置改回执无效；原样 next 继续当前链。默认 CORE 只确认已建立的事实，不再次切换会话，不创建 Agent/新消息，不自动渲染。

- SA-06：通知等待预算默认 2 秒（从事件任务启动计，不含此前目录/metadata 确认），失败/超时不回滚已提交关联或改变原创建/切换结果。取消原调用时请求取消通知并传播取消，不伪造接收回执。忽略取消的任务可能晚退出，不能产生迟到有效 CORE，包在途计数保留至其退出。

- SA-07：平台提供本次只读 SessionContext。cwd 未确认则 None；root 仅取保存的绝对项目目录，model 沿用保存信息读取规则，不为展示创建/激活模型。无需先发生用量事件。

- SA-08：宿主 foreground 观察者与 KVC 动态参与者组合；其他 activity 事件和输入意图返回语义不变，KVC 配置启停/lease release 不移除宿主关联观察；EventBus 只在 Worker，Core 无变化。

- SA-09：关联表为一个 Runtime 的内存事实；按视图替换旧关联，不写会话文件，不提供 surfaces Ops；解除与替换通知由 [S_12](S_12_session-detach.md) 维护。Team/子 Agent 不发本事件；视图转入未跟踪的 Team 目标会清除其旧主会话关联记录。

## 3. 平台入口、结果和兼容

无新增 `ops.session.attach()`。宿主继续调用 `ExtensionSessionAttachParticipant.foreground_changed(event)`；event 来自 Runtime 已提交的前台变化，包含原稳定 view_id。默认宿主渠道声明为 web→desktop，不从客户端可改事件推断身份。可选真实 viewport 由平台关联提交函数接收；当前原生接线不提供该测量，不新增前端采集。

[平台关联表及通知](../../../server/runtime/session/attach_notifications.py) 以私有 SessionAssociation 保存真实目标、view_id、attachment_id 和上下文。公开输入由 event_input() 单独构造；CORE 使用私有调用状态核对绑定会话和关联版本，不从公开 clientId 猜目标。默认 [session_attach_core](../../event_handlers/session_attach.py) 仅确认事实。

等待预算默认 2 秒。USER 错误或通知超时不恢复已提交关联；原调用取消继续传播。超时使作用域失效并请求取消，忽略取消的任务不能产生迟到有效 CORE 回执，在途计数直到实际退出才归零。USER 外部副作用不自动回滚，按 attachment_id 清理自己的资源。

本次输入/输出的旧字段迁移唯一解释入口为 [F_23 第 4.2 节](../features/F_23_session-attach.md#42-旧-handler--回执迁移)。平台方法与 SessionContext 的 session_id 保持 snake_case，不将公开 camelCase 修改扩大到全部内部接口。

## 4. 验证与关联

运行验证、环境和证据范围在 [关联特性第 6 节](../features/F_23_session-attach.md#6-验收与未决问题) 维护。定向入口为 tests/unit_tests/extensions_v2/test_session_attach.py 和 test_session_lifecycle_contracts.py；Server/Runtime 原生接线与真实进程按 feature 的实际记录核对。
