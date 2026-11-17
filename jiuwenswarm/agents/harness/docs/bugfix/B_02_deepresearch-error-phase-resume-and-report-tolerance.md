# B_02 — deepsearch 续跑在 error 相位全链路死锁 + chart 占位符严格校验放大报告写入失败

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_02 / deepresearch-error-phase-resume-and-report-tolerance |
| 关联模块 | `jiuwenswarm/agents/harness/common/rails/deepresearch_execution_rail.py`、`common/tools/deepresearch/execution.py`、`common/tools/deepresearch_plugin/report_bundle.py` |
| 修复日期 | 2026-11-03 |
| 关联 spec | 无（全量 grep `docs/specs/` 实证：修复未触及 S_01~S_21 已归档路径，无需 spec 修订） |
| 关联测试 | `tests/unit_tests/agentserver/test_deepresearch_execute_tool.py`、`tests/unit_tests/agentserver/test_deepresearch_report_bundle.py` |
| 配套修复 | relay-claw `office-claw-skills/deepresearch/scripts/run_deepsearch.py`（自然完成 checkpoint 保留，见 relay-claw `docs/issues/2026-11-03-deepsearch-checkpoint-retention.md`） |

## 复现场景

两起生产事故（用户提供的会话日志摘录，2026-10-26/27，原始日志未留存本地仓库）：

**场景A（会话 8c7faba3，2026-10-26）— 自然完成后报告写入失败 → resume 全拒**：
主 run 17.5 分钟自然完成，工具侧写报告文件失败（`report_file_write_failed`），phase 置
error；此后模型所有 resume 尝试因 "no workflow checkpoint exists" 被拒，最终只能
salvage `raw_report.md` 兜底交付。用户视角：研究跑完了，成品报告却没有，且无法通过
对话修复。

**场景B（会话 517dadf3 / b0037703，2026-10-27 双轮）— 交互中断后取消 → 下一轮全量重跑**：
Turn1 在交互中断（等待反馈 / 大纲审阅）时用户取消，phase 被写为 error（覆盖原
wait_* 相位）；Turn2 用户再次发起研究，执行链无法从 error 相位认领中断快照，SDK 侧
按全新 run 处理——已有研究成果全部丢弃、从头重跑（两轮各 19.83 / 18.24 分钟白跑，
且丢失上下文）。

触发条件：deepsearch 工作流进入 error 相位后，用户在后续轮次尝试续跑或续交互。

## 根因

error 相位原先是一条"只进不出"的死路，五个环节叠加：

1. **chart 占位符严格校验（场景A 放大器）**：`_rewrite_chart_placeholders._replace`
   对占位符 ID 调 `_validate_bundle_id`（`SAFE_BUNDLE_ID_RE`），非法即抛
   ValueError——而占位符捕获正则本身是宽松的 `[^)]+`，报告正文里一个含中文/斜杠/
   空格的占位符就让整条报告写入链失败，把"自然完成"打成 error。
2. **rail 探针门硬要求 stopped**：`_bridge_stale_workflow` 对所有候选 phase 一律要求
   `probe.get("stopped")`；error 相位的探针常是交互中断快照（无 stopped 标志）→
   bridge 全拒 → stale workflow 状态无法桥接进当前轮。
3. **error 终态弹 state**：rail 在 `_persist` 后仅对 model_handoff /
   materials_prepared 保留 state，error 终态一律 pop——error 之后 conversation 上已无
   状态可供认领。
4. **execution 无 interrupt 认领**：error 相位下只有 stopped 认领（要求探针
   stopped）；重弹交互卡的逻辑只存在于 phase=new 分支（约 110 行内联），中断快照无从
   认领。
5. **判死无回落**：认领全未命中时直接以 error 终态返回，不回落 phase=new、不保留
   conversation_id——后续轮次永远卡在"请重新发起"。

配套根因（场景A "resume 全拒"的直接来源，修复在 relay-claw 侧）：openjiuwen
`PersistenceCheckpointer.post_workflow_execute` 的自然完成分支清除 io 侧 workflow
checkpoint 与图侧快照——写报告失败后即便 error 相位想 resume 也无 checkpoint 可用。

## 修复口径

jiuwenswarm 侧三文件，六个改动点：

1. **修复①（report_bundle.py `_rewrite_chart_placeholders`）**：`_replace` 对非法
   占位符 ID 原样保留（不匹配 `SAFE_BUNDLE_ID_RE` 即返回原文），不再抛错。chart_ids
   内 ID 均已在 `_validate_resources` 严格校验，非法占位符必不在其中——不漏替换任何
   合法图表；与 `_rewrite_inference_links` 的容错模式同构。
2. **修复③ 探针门放行（deepresearch_execution_rail.py `_bridge_stale_workflow`）**：
   error 相位候选只要有可恢复 checkpoint（Mapping 探针）即放行（停止快照 ∪ 交互
   中断快照）；in-flight 相位维持原硬要求 stopped 不变。
3. **修复③ error 终态保留 state（deepresearch_execution_rail.py `_persist` 后）**：
   `error_terminal` 与 model_handoff / materials_prepared 同列保留条件，不再 pop。
4. **修复③ interrupt 认领共用化（execution.py `_replay_interrupt_card`，新函数）**：
   从 phase=new 分支提取——feedback_handler 恢复问题清单出反馈卡（wait_feedback）、
   outline_interaction 重弹大纲审阅卡（wait_outline；DB 快照优先，缓存兜底仅在 DB
   确认可恢复时启用）；phase=new 与 phase=error 两入口共用，原 phase=new 分支约 110
   行内联逻辑替换为对该函数的调用。
   检视修复（2026-11-05）：feedback 分支的选项生成经 `_guard_user_stop` 包裹后，
   须对返回值加 `isinstance(options, list)` 守卫——`_guard_user_stop` 正常路径透传
   `list[list[dict]]`，CancelledError 路径返回 `_stopped_result(...)` 的 dict；
   不加守卫时取消路径的 dict 被按下标访问（`options[index]`）抛 KeyError，stopped
   终态丢失、退回框架通用 [Interrupted] 分支，违背 `_guard_user_stop` 的停止转译
   契约。非 list 时直接透传（phase 已回落 new、保留 conversation_id 的可续跑终态）。
5. **修复③ error 相位接入 interrupt 认领（execution.py error 分支）**：stopped 认领
   未命中后，`_query_matches_stopped_claim` 通过时调 `_replay_interrupt_card`，命中即
   返回交互结果——场景B 的 Turn2 从中断点继续而非全量重跑。
6. **fix-4c 判死回落（execution.py error 分支末）**：认领全未命中时
   `_persist(context, state, "new", conversation_id=error_cid)` 后再以 error 结果返回
   ——保留 conversation_id、回落 phase=new，防"请重新发起"死锁。

配套修复②（relay-claw 侧 run_deepsearch.py）：自然完成后保留 checkpoint（io save +
图侧哨兵终态快照，全新 run 时延迟清除），resume 时 PregelLoop 0 步结束不重跑、工具侧
completed 分支重新写报告文件——解决场景A 的 "no workflow checkpoint exists"。

## 验证

- **jiuwenswarm 定向回归**：`test_deepresearch_execute_tool.py` +
  `test_deepresearch_report_bundle.py` → **182 passed / 4 skipped**（.venv）。
- **新增 / 改写测试**：
  - `test_error_phase_replays_feedback_card_from_interrupt_snapshot` /
    `test_error_phase_replays_outline_card_from_interrupt_snapshot`（新增）：error 相位
    从中断快照重弹反馈卡 / 大纲卡。
  - `test_replay_interrupt_card_cancelled_option_generation_returns_stopped`
    （检视修复新增，2026-11-05）：mock `model.invoke` 抛 `asyncio.CancelledError`，
    断言 `_replay_interrupt_card` 返回 `kind=stopped` 终态（phase 回落 new、保留
    conversation_id）而非抛 KeyError。
  - `test_build_report_bundle_keeps_invalid_chart_placeholder_as_is`（新增，参数化
    `../escape` / `图表一` / `chart a`）：非法占位符原样保留 + 同文本内合法图表正常
    替换落盘。
  - `test_build_report_bundle_cleans_owned_resources_after_rewrite_failure`（改写）：
    原用非法占位符 `../escape` 触发 ValueError——修复①后该注入手段失效（本身即"先红"
    证据），改用 monkeypatch 注入 rewrite 失败，断言不变。
- **修复②单元级集成验证**（relay-claw 侧，真实 PersistenceCheckpointer + SQLite KV +
  PregelLoop）：T0-T9 十场景全 PASS——自然完成保留哨兵终态快照 →
  `validate_interactive_resume` 放行 → PregelLoop 恢复哨兵快照后 0 步结束不重跑 →
  全新 run 延迟清除等价旧行为 → 非哨兵残留 raise 原错误 / interrupt / exception 分支
  保持 SDK 原行为 → patch 幂等安装。
- 相关存量测试 37 个（error / bridge / rail / terminal / phase 关键词）全数通过。
- 检视修复回归（2026-11-05）：`test_deepresearch_execute_tool.py` +
  `test_skill_required_tool_visibility.py` → **172 passed**（.venv，含取消注入新用例）。

## 防错教训

- 中断 / 错误相位不能是"只进不出"的终态：每个把 phase 写成 error 的入口都要有对应的
  恢复（认领重弹）或回落（phase=new）路径，否则用户侧表现为死锁（"请重新发起"）或
  全量重跑。error 相位的探针门、终态保留、认领链、回落路径四者必须成套设计。
- 对自由文本中的占位符做严格校验 + 抛错，等于把一个格式问题升级成整条交付链失败。
  正确姿势：宽松捕获 + 命中即替换、未命中原样保留（本例与
  `_rewrite_inference_links` 同构）。
- monkey-patch SDK 内部行为（修复②）必须 fail-open：patch 安装失败 / 执行失败均退化为
  SDK 原行为并记 warning，不阻断研究主流程。

## 遗留与后续

- officeClaw 线上部署需同步本修复（harness 三文件）与 relay-claw 侧 run_deepsearch.py
  后自然恢复；均为代码侧修复，不依赖线上数据迁移。
- 修复②以 monkey-patch 覆盖 `PersistenceCheckpointer` 的 pre/post_workflow_execute，
  依赖 openjiuwen 内部属性（`_workflow_storage` / `_graph_state`）；openjiuwen 升级若
  改动这些内部 API，patch 自动退化为原行为（记 warning，仅失去"完成后可 resume"能力）
  ——长期应在 SDK 侧提供官方的"自然完成保留 checkpoint"选项。
