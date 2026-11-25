# B_10 — Flash 深度研究停止后续跑丢失待办进度和父任务标题

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_10 / flash-resume-todo-replay |
| 修复日期 | 2026-11-25 |
| 严重程度 | 中，影响停止续跑后的任务进度展示 |
| 关联 Issue | [#7036](https://gitcode.com/openJiuwen/jiuwenswarm/issues/7036) |
| 关联 spec | [S_20 P2](../specs/S_20_stream-event-emission-channel.md) 轮首补发；[S_06 N11](../specs/S_06_deepresearch-todo-persistence.md) 父任务与子节点边界 |

## 现象与定位证据

在 OfficeAce v1.9.0-B040 的 Flash 模式中，启动预置深度研究任务，确认大纲并在执行期间停止，
再发送「继续执行」。runner 以 `command=resume, node=stopped` 恢复研究，但右侧没有待办，
过程中的父任务行显示 `todo:research`。现场续跑轮的推送统计有 6 条 `task.start` 和
6 条 `task.complete`，模型后续调用 todo modify 前没有 `todo.updated`。该次 todo 更新
补上了清单和标题。

## 根因与修复决策

1. 「继续执行」走新的 `chat.send`。DeepResearch 工具恢复的是技能自己的停止快照，
   `has_pending_step_resume` 只识别 Agent 步骤检查点。原先
   [`before_invoke`](../../common/rails/stream_event_rail.py#L1153) 只在后者存在时补发，
   所以 RelayClaw 开始新轮时清理了旧的任务槽，却收不到当前计划的基线。现在轮首也识别
   短续跑文本，只有当前代存在执行中的计划项或待执行项时才发 `todo.updated`。
2. [请求预处理](../../../../server/runtime/agent_adapter/stale_todo_cleanup_helpers.py#L36)
   对适用的计划请求用 `is_resume_user_query` 判断是否沿用当前 todo generation token。
   轮首补发复用同一个谓词，再按当前代筛选执行项。两侧判断若各自演进，可能出现续跑时
   待办已按旧代隐藏，或保留待办却漏发的情况。仅有持久提醒也不会触发补发。
3. 子节点的 `task.start` 和 `task.complete` 带父 todo ID 与子流标识，标题描述的是节点。
   在停后续跑时，[`read_research_parent_title`](../../common/tools/deepresearch/todo_progress.py#L347)
   读取权威 todo 快照，仅在 `plan_id` 仍匹配本次捕获的计划、父待办仍为 `in_progress` 时取标题。
   [`_consume_stream`](../../common/tools/deepresearch/tools.py#L4082) 先发一条不带子流标识的
   父级 `task.start`，再发送子节点帧。父任务完成仍由外层待办状态更新决定，标题读取或发射失败时
   研究继续。

## 回归证据与适用范围

- [轮首补发测试](../../../../../tests/unit_tests/agentserver/rails/test_stream_rail_step_resume_todo.py#L94)
  使用已落盘待办验证短续跑文本发快照、已完成计划和普通新任务不发。
- [停止续跑父标题测试](../../../../../tests/unit_tests/agentserver/test_deepresearch_stream_tool.py#L4597)
  验证真实父标题先于子节点边界发出，子节点保留自己的子流标识。
- 使用 JiuwenSwarm `36e0f3089d` 和 RelayClaw `d4da19735a` 做本地事件回放，
  清单快照先到，随后是父任务和子节点。子节点完成时父任务仍为进行中。
  OfficeAce 实机停止和续跑尚未验收。

同一 [PR #10913](https://gitcode.com/openJiuwen/jiuwenswarm/pull/10913) 还包含报告改写
缺省 `facts_added` 的独立修复，关联
[`test_run_rewrite_fast_path_defaults_only_missing_facts_added`](../../../../../tests/unit_tests/agentserver/test_deepresearch_rewrite_fast_path.py#L611)。
