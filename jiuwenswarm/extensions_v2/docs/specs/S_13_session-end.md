# session.end 会话运行结束通知规约

| 项 | 值 |
| --- | --- |
| 编号 | S_13 / session-end |
| 最近修订日期 | 2026-11-02 |
| 关联特性 | [F_24](../features/F_24_session-end.md) |

## 1. 输入、输出与填写规则

本次修订替代原永久删除专用契约，保留 session.start。平台构造 frozen dataclass SessionEndInput，用户只读并原样 next。

| 字段 | 类型 / 默认 | 含义、填写内容与作用 |
| --- | --- | --- |
| reason | str，必填 | clear/resume/logout/prompt_input_exit/other 五值之一；当前实际资源释放和停机使用 other，不能把切页伪报成 resume |
| session_id | str，必填 | 正在结束的真实 Runtime 主会话 ID，例如 demo-a；对应 Claude sessionId |
| resume | SessionResume，必填 | 恢复资料对象；resume.id 等于本次 session_id，例如 demo-a；不是令牌或路径，不保证无持久记录时能够恢复 |

默认 CORE 返回 frozen dataclass SessionEndOutput(session_id: str)，回显原身份；不再提供 accepted/end_id、deny 或取消结束协议。USER 必须返回真实 next 回执。逐字段例子与用途唯一展开于 F_24 第 3 节。

## 2. 范围和不变量

- SE-01：仅 Worker 持有 EventBus。装配注册类型、Matcher、Validator、默认 CORE，沿用共享 Builder/现有 Ops；无同名 Ops 或 Core API 变更。
- SE-02：AgentRuntime.cleanup_session 和 Runtime.close 终止真实已登记运行时，先停稳执行，再在原内存资源释放前调用固定宿主结束参与者。永久删除只有确实终止活跃运行时经过该通知；离线存档删除不虚构运行。
- SE-03：回复完成、取消生成、idle、界面切换/attach/detach 和 Supervisor 临时断连不等于结束。明确 client.disconnect 若实际释放运行，则通知 other；没有实际关闭路径的强杀/崩溃不保证通知。
- SE-04：按 Runtime 内 session_id + Coordinator generation 合并至多一次通知尝试；重复 cleanup/随后停机不重放。同 ID 恢复形成新代次，新代次结束可再次通知；新登记等待旧代次通知和资源清理完成。
- SE-05：USER 保存自己的状态或释放自己的资源，再 next；CORE 核对有效宿主调用、原输入、绑定会话，回显真实 session_id。平台继续原释放；事件不删除历史、不重新创建 Agent、不发送额外输入。
- SE-06：所有输入及嵌套 resume.id 只读；伪造目标、提前回执、next 后替换身份无效，沿用既有 USER 故障隔离。作用域外无法自行 trigger 一个有效结束通知。
- SE-07：整个通知等待预算默认 1.5 秒。异常/超时记日志并继续释放；超时失效作用域并请求取消，迟到任务不能再生成有效回执；包在途计数直到任务真实退出才释放，不承诺回滚外部副作用。
- SE-08：Runtime.close 停稳执行后并发通知各主会话，之后释放资源并最后卸载扩展；不按会话数串行累计预算。停稳超时不伪造该代次正常结束。
- SE-09：SessionContext 绑定真实目标；cwd 未确认则 None，project_root 仅使用已确认绝对目录；不使用进程 cwd 或初始化模型兜底。ops.session_context 是已有只读属性。
- SE-10：固定宿主结束观察者与动态 KVC activity/delete 参与者分开组合。无扩展时保留原释放行为；卸载只撤销用户订阅。无跨进程重启 exactly-once，收尾必须幂等。

## 3. 兼容与验收

旧 reason=session_deleted、end_id、accepted、事件 channel_id/mode 被替换为 reason/session_id/resume.id 和真实身份回执。恢复标识只描述身份，持久化关闭、未产生记录或随后永久删除时不承诺恢复。词表对齐 Claude Mods，但不声称产品已有全部 clear/resume/logout UI；有真实替换/退出入口的宿主才能提供相应原因。

Python 使用 session_id 对应 Claude sessionId。参考固定提交 bf2323b3021c9d3db7a31e1e7d61068c3f704497，不把公开声明当引擎源码。当前验证与旧删除版本历史证据分别记录于 [F_24 第 6 节](../features/F_24_session-end.md#6-验收与未决问题)。
