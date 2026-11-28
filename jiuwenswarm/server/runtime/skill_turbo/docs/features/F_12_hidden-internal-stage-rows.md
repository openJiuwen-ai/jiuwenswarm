# 所有 Turbo 技能统一隐藏内部阶段行

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature |
| 编号 / slug | F_12 / hidden-internal-stage-rows |
| 关联模块 | jiuwenswarm/server/runtime/skill_turbo/skill_turbo_tools.py |
| 日期 | 2026-11-25 |
| 关联 spec | [[S_08]]、[[S_09]] |
| 状态 | 本地实现及模块回归完成，待真实服务验收，未提交 |

## 目标与边界

用户明确授权对所有 Turbo 技能隐藏对话区的内部阶段行（绿色状态图标、阶段标题及展开入口），
不限 PPT，不按 Stage / generate_inputs 等名称匹配。不删除内部工作内容，不改技能执行或前端。
外层助手的 todo 行保持原有管理；内部阶段不再外发，因此右侧也不会收到内部阶段快照。
修改后新调用生效；既有会话执行历史不清理。

## 实际决策

原实现仅在 has_outer_todo 为真时隐藏阶段；假/未绑定时会转发内部 task 事件，
结束补发还可能再次生成阶段行。旧 PR 仅删除技能主动输出的文字横幅，无法替代任务事件过滤。

在公开 skill_acceleration_exec 工具边界统一拦截 task.start / task.complete / task.update，
并在其他事件副本上去除 task_ 前缀 task_id，保留 todo: 外层 id、正文、思考、工具、usage、
产物、交互和既有错误语义。不按文本过滤，因此正文提到 Stage 1 也原样保留。

单段/自动组批/显式列表共用 _invoke_segment_tool；旧 query / __full__ / legacy resume
共用原工具循环，两处均修改。executor、PlanNode、内部 task_states、台账、HITL 断点和技能
源码不改。删除已失去调用路径的任务快照 OOB、槽位登记及终态补发代码，避免旁路重建阶段行。
[[B_03]] 的工具侧补发决策由本记录替代；历史 bugfix 验证事实保留，外层 rail 的旧槽位兼容代码不扩展清理。

不采用技能逐个删除横幅、修改节点结构或前端按标题过滤：前两者不能统一控制引擎生成的任务行，
后者需要额外改动前端且容易把业务文字当阶段。

## 验证

候选为 fix/ppt-segment-file-handoff 当前工作区，保留此前未提交的 auto_batch 三态改动。
使用仓内 .venv，临时技能、内存 checkpointer 与模拟模型/发送边界；不连接生产持久化存储。

先红：真实 SDK 工具入口中，无外层 todo 的 legacy 和 segment 过滤断言失败；有 todo 的两例通过
（2 failed / 2 passed），失败原因为内部任务事件仍被转发。

第一轮定向回归 76 passed。覆盖所有技能通用路径、外层 todo 真/假、阶段快照不经 FIFO/OOB、
旧槽位不触发终态补发；对工作内容、模型/工具/usage/产物/交互的完整透传及源事件不变作独立断言。
扩展覆盖 __full__ 的三个技能名、todo 未绑定/假/真、真实单段/自动批次/显式列表与暂停恢复。

最终回归命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py -o addopts="--asyncio-mode=auto" -o log_cli=false --tb=short --show-capture=no -q
```

结果：903 passed，31.86s。暂停恢复用例独立读取断点确认 task_states 非空，并验证恢复后内部
阶段事件仍未外发、节点未重复执行。完整流程矩阵使用三个技能名及 todo 未绑定/假/真，检查
正文、思考、工具、usage、产物、交互、fallback 事件逐项完整保留，外层 todo id 保留且源事件未改。

扩展初跑中人为设置 log_level=ERROR 导致既有 caplog 警告断言失败；移除此测试运行参数。
新增真实节点 fixture 原本无工作正文，因此为本用例加入明确 message，并断言其真实透传，未放宽
内容保留要求。恢复正常日志配置后完整重跑通过；首次失败不作为产品缺陷或通过证据。

未重启现有服务、未做真实模型或浏览器 UI 验收、未提交或推送。
本次为引擎事件边界改动，不修改 Relay 仓库，无需运行其前端构建/格式化。
