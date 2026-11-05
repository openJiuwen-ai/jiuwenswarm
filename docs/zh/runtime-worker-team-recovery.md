# Worker 中断与 Team 恢复边界

Worker 就绪只表示新的运行进程可接收请求，不表示旧 Team 已恢复。`chat.final` 是一次模型答复，不等于整个产品完成；任务 completed 也不等于独立产品验收。

## 只读判别

对同一个 session_id/team_name 查询 `team.list` 和 `team.snapshot`。快照新增字段：

| 字段 | 含义 |
|---|---|
| members_source | 成员/任务板来源，保持原有 live、live-scoped、db、empty 值 |
| members_live | 该板是否来自实时 monitor；不表示所有成员都正在工作 |
| runtime_available | 精确绑定的 Team monitor 是否存在；查询失败或未能确定身份时为 null |
| recovery_required | 已确认无运行时且历史板有未完成任务时为 true；运行时查询未知或缺失历史板时为 null |

DB 中的 busy/running 状态原样保留为历史事实，不改写成 idle，也不当作实时执行证明。实时运行时存在但任务板回退 DB 时，runtime_available 可以为 true，而 members_live 为 false。recovery_required 不证明中断原因或保证可以恢复，更不会自动重放任务。

## 故障排查

1. 在最后活动至首次缺测的有界时间窗内查看 Supervisor WARNING、worker PID/实例、退出及重启事件。这些行常不含 session_id，不能只按会话过滤。
2. 分开检查传输心跳、业务循环 beat、网关转发积压和 Supervisor 自身 lag。端口仍监听或新 worker ready 都不能排除 Team 现场丢失。
3. 读取对应 worker stderr/dump 的有界尾部。巨大日志、队列溢出和一次线程栈是线索，不是唯一根因证明。数据库连接池耗尽需要进一步检查 checkout/checkin、取消清理和连接持有者，不能靠扩大心跳阈值修复。
4. 本次 sink 补丁消除队列满时持状态锁逐条同步输出的风险：消费路径只计数，writer 以 30 秒间隔聚合告警，关闭时补报未报告丢弃。精确 dropped/dropped_final/dropped_frames 可读取，队列仍有界，不增加无界日志队列。慢日志仍可能阻塞 writer，但不应阻塞消费线程。

## 恢复与验证

保留工作区、未提交文件、任务数据库和 checkpoint。显式恢复前核对结果未知的工具副作用，尤其外部写入；不要把原请求直接全部重放，也不要重建同名任务来掩盖丢失现场。按平台正常恢复入口接回同身份后，取得 team.recovered、实时池/成员及新的工程活动证据，才称恢复执行。

这些修改没有实现 worker 崩溃后的自动任务重放，也没有证明长时间多成员运行不会再次失联。单元回归验证慢日志隔离、溢出计数和快照来源；发布前还需在隔离环境做 worker 故障注入、同身份恢复和长跑，不在真实实验上做无授权重启。
