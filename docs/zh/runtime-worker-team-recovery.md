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

## 隔离预检与已知缺口

使用该平台环境的 Python，从仓库根目录执行以下测试，不需要真实模型密钥：

```bash
python -m pytest tests/unit_tests/agentserver/test_team_reliability_preflight.py tests/unit_tests/agentserver/test_team_snapshot.py tests/unit_tests/agentserver/test_worker_loop_liveness.py tests/unit_tests/observability/test_trace_sink.py --no-cov -q
```

预检直接导入环境实际安装的 agent-core，因此结果必须同时记录平台和 SDK 的完整 SHA；不能拿另一份 checkout 的测试结果代替运行环境。

- **慢日志隔离**：writer 告警被阻塞时，事件循环继续生产 2,500 条 frame，并在批次之间推进心跳。它验证队列溢出路径，不保证任意日志 handler 或磁盘故障都不会阻塞业务。
- **读池回收**：临时文件 SQLite 的两个读槽饱和后，普通取消和 watchdog 超时释放槽位；另有 10 轮、每轮 32 个读请求验证连接归还。这不是旧现场的泄漏根因复现。
- **驱动阻塞边界**：测试 SQLite 原生函数被控制屏障阻塞时，即使 Python watchdog 已到期，连接清理仍等待该函数返回。watchdog 不是终止驱动线程的硬截止，不能据此宣布 wedged driver 已可自动恢复。
- **冷池与持久化分离**：保留真实临时 Team DB 中的 in_progress 任务、替换进程内 runtime pool 后，`restore_resumable_runtime` 不会凭任务行自动重建运行时。该测试模拟池替换，不是完整 Worker 杀进程/重启测试，也没有执行冷恢复或重放外部副作用。
- **完成边界**：确定性模型替身通过真实 ReAct/TaskLoop 返回 PARTIAL，普通 invoke 没有自动创建 Goal；显式 Goal + output consumer + `continue` 评估则启动第二次尝试。另以受控任务板调用真实 Team 完成谓词：pending/in_progress 阻止完成，completed/cancelled、成员已收敛且消息已读则可完成，即使产品还有未映射进任务板的要求。这不是“看到 PARTIAL 就立即杀掉整个 Team”。

不能直接给每个成员绑定同一个无限 Goal：成员职责、Leader 的协调等待、团队级验收、输出租约与恢复身份需要分别处理。应先定义通用工程目标的持久化契约：剩余要求不能因任务板排空而消失，完成必须对应固定候选及验收证据；阻塞、预算耗尽、用户停止与成功交付须为不同终态。

在这些契约和真实 Worker 故障注入尚未通过前，长程无人值守全量实验仍应标为 **NOT READY**，而不是仅凭本组回归通过就关闭恢复与提前收口问题。
