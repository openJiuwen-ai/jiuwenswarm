# session.end 主会话删除收尾通知规约

| 项 | 值 |
| --- | --- |
| 编号 | S_13 / session-end |
| 最近修订日期 | 2026-10-29 |
| 关联特性 | [F_24](../features/F_24_session-end.md) |

## 1. 输入、输出与填写规则

输入为 frozen dataclass `SessionEndInput`，全部由平台填写，用户只读、原样传给 next。

| 字段 | 类型 / 默认 | 含义和实际填写内容 |
| --- | --- | --- |
| session_id | str，必填 | 删除目标主会话的合法 ID，由 Runtime 的 SessionLifecycleTarget 提供 |
| end_id | str，必填 | 此 Runtime 内第一次收尾通知的平台 UUID（32 位十六进制字符串）；同一未完成删除的重试不重新通知 |
| reason | str，必填 | 首版固定 session_deleted，表示明确的永久删除收尾，不表示删除已提交 |
| channel_id | str，必填 | Runtime 从保存元数据/原请求解析的渠道，缺失时为 default；不是当前网页连接 ID |
| mode | str，必填 | 保存的主会话模式，例如 agent.plan；不提供 Team/子 Agent 的结束通知 |

默认输出为 frozen dataclass `SessionEndOutput`，构造器的三个字段都必填，实际由 CORE 提供：

| 字段 | 类型 / 默认 | 含义与消费方式 |
| --- | --- | --- |
| accepted | bool，无省略默认 | 默认 CORE 返回严格 True，通知链已被接受；不是删除事务提交或全部外部资源成功的证明 |
| session_id | str，无省略默认 | 原样回显平台删除目标，只读核对，不回填另一会话 |
| end_id | str，无省略默认 | 原样回显本次 32 位十六进制 UUID 通知身份；不作为会话 ID 或重试参数 |

accepted 为严格 bool 且为 True，表示默认通知 CORE 已接受；session_id/end_id 原样回显平台目标和本次通知身份。三个字段均由 CORE 填写，不接收拒绝、改写、目录修改或取消删除的协议。accepted 不证明每个用户的外部资源已成功释放，也不代表删除事务已经提交。

## 2. 触发、范围和不变量

- SE-01：Worker 装配注册类型、matcher、Validator、默认 CORE，沿用一次 ops.build；无新增 Ops 或 agent-core 变更。
- SE-02：AgentServer 构建 Runtime 时注入宿主删除参与者，在 Runtime 主会话永久删除的 release_resources 阶段派发。此时执行已 quiesce，原资源释放/Runner.release/删除目录在通知返回后继续；普通断连、foreground/inactive、归档和临时 Worker 回收不触发。
- SE-03：默认 CORE 仅返回真实通知回执；原资源释放和文件删除仍由 Runtime 唯一执行，避免第二次释放。USER 释放自己按 session_id 管理的资源，再 next；不删除平台历史或调用删除入口重入。
- SE-04：目标和来源来自平台。输入改写、伪造提前成功或后置修改回执无效，按 Core USER 故障隔离继续原有效链；在平台调用作用域外不能伪造此通知。
- SE-05：每个 Runtime 对一个未完成删除至多尝试通知一次，开始通知即记录；重试不重放用户清理。删除提交后移除记录，成功重复删除由原 Runtime 去重。进程崩溃/重启不提供持久 exactly-once，用户释放操作应幂等。
- SE-06：通知等待预算默认 2 秒。异常或超时记日志后继续原删除；超时使调用作用域失效并请求取消。忽略取消的用户任务可能晚退出，不允许再产生有效 CORE 回执；Runtime 的包在途计数直至任务退出才释放。无法强制取消用户在外部系统的副作用。
- SE-07：原删除请求被取消时取消通知并传播 CancelledError，由原事务执行既有失败/补偿流程；不伪造成功删除。通知后的平台失败也不把已发生的用户清理回滚成未发生。
- SE-08：SessionContext 只绑定真实目标 ID；cwd 未确认则 None，project_root 仅使用目标保存的绝对 project_dir。无进程 cwd 兜底、无模型激活或跨会话资料。Core 自带 ops.session_context 为只读属性。
- SE-09：宿主固定参与者与 KVC 动态参与者分开组合；KVC 开关、suspend、release 不移除宿主通知。事件/用户代码不进入 Front/Supervisor，仍只有 Worker 持有 EventBus。

## 3. 兼容与验收入口

无扩展装配时沿用原删除行为；用户安装包仅发布 Handler，只有真实永久删除才触发。卸载移除用户订阅，平台默认删除及 CORE 保留。本事件不与 PR06 按包激活一次的 session.start 一一配对；一个扩展实例可观察多个主会话结束。

首版不提供 Claude 的 resume 信息、进程退出或切换会话语义。当前通知范围不是所有运行结束；差异、用户包、完整链和验证见 [F_24](../features/F_24_session-end.md)。
