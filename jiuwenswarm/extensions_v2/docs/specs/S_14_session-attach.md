# session.attach 界面会话关联通知规约

| 项 | 值 |
| --- | --- |
| 编号 | S_14 / session-attach |
| 最近修订日期 | 2026-10-29 |
| 特性与验收 | [F_25](../features/F_25_session-attach.md) |

## 1. 事件输入输出

输入为 frozen dataclass `SessionAttachInput`，全部字段由平台填写。用户 Handler 只读，并原样 next；不把界面声明当作授权凭据。

| 字段 | 类型 / 默认 | 含义、来源及实际取值 |
| --- | --- | --- |
| session_id | str，必填 | 已提交界面关联的合法主会话 ID；来自 Runtime 提交目标，并再次确认其存在、未归档/删除、未被生命周期屏障禁入 |
| surface_id | str，必填 | 现有提交上下文中的 view_id，表示具体窗口/客户端视图，不是会话 ID、渲染槽位 ID 或 A2UI 卡片 ID。1～128 个 ASCII 字母/数字或 ._:-；现有 Web 为每个 App 实例生成唯一值 |
| attachment_id | str，必填 | 平台为这次新关联生成的 32 位十六进制 UUID；同一现存关联重放不换 ID、不再次通知 |
| channel_id | str，必填 | 本次提交所用渠道，如 web；不代表传输连接或物理设备身份 |
| mode | str，必填 | Runtime 已确认目标的模式，例如 agent.plan；不是用户 Handler 可选择的切换参数 |
| previous_session_id | str 或 None，必填 | 平台关联表记录的该视图上一个主会话 ID；首次关联或离开未跟踪范围后为 None。不采用请求自报的 previous_session_id 作为关联表权威 |

默认输出为 frozen dataclass `SessionAttachOutput`，以下字段均由 CORE 填写、无可省略构造参数：

| 字段 | 类型 / 默认 | 含义与消费方式 |
| --- | --- | --- |
| accepted | bool，无默认 | 严格 True，表示默认 CORE 确认当时该关联仍为表中当前版本且目标会话检查通过；不是所有 USER 展示均已成功 |
| session_id | str，无默认 | 回显平台绑定的目标会话，仅核对 |
| surface_id | str，无默认 | 回显具体视图，仅核对，不回填另一个目标 |
| attachment_id | str，无默认 | 回显这次关联身份，可用于用户日志和幂等处理 |

## 2. 触发和不变量

- SA-01：复用现有 session.create/session.switch 提交后的 Runtime foreground_changed(visible=True) 事实；Worker 平台关联表先确认并提交，再 trigger。create 保持先交付响应、后异步发布；switch 保持提交时等待该通知流程。无新增客户端方法或前端改动。
- SA-02：传输 Gateway attach、ui.ready、输入意图、执行开始/完成、单纯隐藏/空闲不等于界面进入目标会话。没有有效视图或有效主会话时不生成成功关联通知。
- SA-03：关联按 channel_id + surface_id 标识。同会话不同视图分别通知；同视图同会话重放或稳定 view_id 的断线重连不重复通知；同视图切换目标、再切回会得到新的关联 ID。Worker/Runtime 重启后仅从后续真实提交重建，不承诺持久 exactly-once。
- SA-04：只有仍为表中当前关联、且默认 CORE 再确认目标会话可用的调用才能取得有效 CORE 回执；更新目标、超时或作用域退出使旧调用失效。已经发生的 USER 副作用不自动回滚，用户应按关联 ID 幂等。
- SA-05：USER 改输入、提前伪造成功、后置改回执无效；原样 next 继续当前链。默认 CORE 只确认已建立的事实，不再次切换会话，不创建 Agent/新消息，不自动渲染。
- SA-06：通知等待预算默认 2 秒（从事件任务启动计，不含此前目录/metadata 确认），失败/超时不回滚已提交关联或改变原创建/切换结果。取消原调用时请求取消通知并传播取消，不伪造接收回执。忽略取消的任务可能晚退出，不能产生迟到有效 CORE，包在途计数保留至其退出。
- SA-07：平台提供本次只读 SessionContext。cwd 未确认则 None；root 仅取保存的绝对项目目录，model 沿用保存信息读取规则，不为展示创建/激活模型。无需先发生用量事件。
- SA-08：宿主 foreground 观察者与 KVC 动态参与者组合；其他 activity 事件和输入意图返回语义不变，KVC 配置启停/lease release 不移除宿主关联观察；EventBus 只在 Worker，Core 无变化。
- SA-09：关联表为一个 Runtime 的内存事实；按视图替换旧关联，不写会话文件，不提供 surfaces Ops；解除与替换通知由 [S_15](S_15_session-detach.md) 维护。Team/子 Agent 不发本事件；视图转入未跟踪的 Team 目标会清除其旧主会话关联记录。

## 3. 使用边界

现有 Web 已发送稳定 view_id。旧调用省略 view_id 时，原宿主会提供 ws:<id> 或 default-view 降级标识；多个窗口必须显式使用各自 view_id，不能把该降级值当作独立窗口身份。没有物理在线检测、断连自动 detach、viewport、Claude resume/终端绑定承诺。

安装包只发布 USER 订阅，不对已经存在的关联补发通知。卸载移除 USER，默认创建/切换继续。PR08 与 PR11 同 PR 交付、特性文档分开；detach 使用 S_15，attach 的首次证据见 F_25，联合验证见 F_26。
