# B_10 — 工具否决标记 per-call 迁移：宿主 legacy 写/读在并发轮次仍竞争

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_10 / skip-tool-per-call-marker |
| 关联模块 | `jiuwenswarm/agents/harness/common/skip_tool_compat.py`（新增）；`.../common/rails/task_execution_rail.py`、`.../common/rails/workspace_quota_rail.py`、`.../common/rails/read_file_validation.py`、`.../common/rails/avatar_rail.py`、`.../common/rails/progressive_tool_rail.py`、`.../common/rails/skill_credential_injection_rail.py`、`.../common/rails/stream_event_rail.py`、`.../common/auto_memory/tool_restriction_rail.py`、`.../code/rails/code_agent_mode_rail.py`、`.../code/rails/code_plan_approval_rail.py`、`.../flash/rails/flash_todo_rail.py`、`.../flash/skill_selection/rail.py`（复审补齐：单引号写法漏搜）；`jiuwenswarm/server/hooks/user_hook_rail.py`、`jiuwenswarm/server/runtime/skill_turbo/executor.py`、`jiuwenswarm/openjiuwen_skip_tool_patch.py` |
| 修复日期 | 2026-11-28 |
| 关联 spec | [[S_10]]（N6 / N10 的拒绝写点与 skip 协作语义）、[[S_15]]（N4 RejectResult 落地）；S_03 / S_04 的措辞同轮修订至 2026-11-28 |
| 关联测试 | `tests/unit_tests/agents/harness/common/test_skip_tool_compat.py`（新增）；受影响 rail 用例断言迁到 `was_skipped(ctx)` |

## 复现场景

agent-core 的否决标记原走整轮共享的 `extra["_skip_tool"]`：rail 在 BEFORE 钩子置位，执行体在进入工具前 `pop` 一次。一轮多个 tool call 并发时，仍停在 BEFORE 钩子里的调用会丢标记——兄弟执行体先 `pop`，兄弟被跳过并返回自己的空 result，被否决的调用反而真正执行（openJiuwen/agent-core#2413）。

agent-core 侧已把标记迁到 per-call `AgentCallbackContext`（`mark_tool_skipped()` / `tool_skipped`，见该仓 PR #3978 / #2413）。本仓当时仍是旧写法：

- 写点 12 处：`task_execution_rail`(×2)、`workspace_quota_rail`、`read_file_validation`、`avatar_rail`、`progressive_tool_rail`、`skill_credential_injection_rail`(×2)、`tool_restriction_rail`、`flash_todo_rail`、`server/hooks/user_hook_rail`
- 读点 5 处：`code_agent_mode_rail:151`（父类否决后的早退）、`stream_event_rail:1608`（被否决调用仍发 in_progress）、`code_plan_approval_rail:115`（兜底检查）、`skill_turbo/executor:1762`、`openjiuwen_skip_tool_patch:25`（补 ToolMessage 的 runtime patch）

后果有两半：新 core 下 marker 否决对 legacy 读者不可见（stream 发错事件、plan 模式二次 reject 覆盖父类消息）；legacy 写者在并发轮次仍保留 #2413 的误跳过竞争。

## 根因

`extra` 是整轮共享 dict，而否决标记是**一次性、单调用语义**——存储位置与语义不匹配。写得早的 rail 与读得晚的 rail 之间没有任何「这次调用」的归属信息，谁先到执行体谁消费。

## 修复口径

1. 新增 `common/skip_tool_compat.py`：
   - `mark_skipped(ctx)`：新 core 走 `mark_tool_skipped(ctx)` 写 per-call marker；旧 core 回落 `extra["_skip_tool"] = True`；
   - `was_skipped(ctx)`：新 core 走 `tool_call_skipped(ctx)`（该函数本身会回读 legacy key），旧 core 直接读 `extra`。
2. 12 个写点改调 `mark_skipped(ctx)`，5 个读点改调 `was_skipped(ctx)`（含 runtime patch，保证 marker 否决也能补 ToolMessage）。
3. 受影响单测断言迁到 `was_skipped(ctx)`；仍以 `extra["_skip_tool"] = True` 造场景的用例保留，正好覆盖 shim 的 legacy 回落分支。
4. spec `S_03` / `S_04` / `S_10` / `S_15` 与模块 `AGENTS.md` 术语表 / 铁律 20 同步：标记不再直接写共享 extra，跨版本兼容经 shim。

复审补充：`flash/skill_selection/rail.py`（显式 Skill 校验未通过时拒绝加载）原先写作 `ctx.extra['_skip_tool'] = True`，首轮按双引号写法 grep 漏搜，写点数量应为 12 而非 11。复审提出后已补齐 —— 该拒绝路径同样受共享 `extra` 被兄弟调用先 pop 的影响，补齐后全仓不再有直接写 legacy key 的代码。

兼容边界：本仓不强制 agent-core 升级——旧 core 下 shim 全部回落 legacy 写读，行为与改造前一致；新 core 下才获得 per-call 隔离。agent-core 侧对 legacy 写者的并发竞争由该仓 PR 的 CR-1 / CR-2 文档声明，不在本仓消除。

## 验证

- 新增 `tests/unit_tests/agents/harness/common/test_skip_tool_compat.py`：marker 分支写标记不污染 `extra`、legacy 分支回落写读、以及「兄弟调用写的 legacy 标记对本次 `was_skipped` 可见」。
- 受影响 rail 用例：`test_code_agent_mode_rail`、`test_skill_credential_injection_rail`、`test_flash_todo_close_nudge`、`test_skill_required_tool_visibility`、`test_flash_permission_lifecycle`、`test_file_skill_grant_store`、`test_flash_permission_actions`、`server/hooks/test_user_hook_rail`、`test_auto_memory_tool_restriction`、`test_stream_event_rail_image_unsafe_guard`、`test_deepresearch_execute_idempotency`、`test_task_execution_todo_close_nudge`、`test_tool_result_normalize_rail`。
- 先红后绿：旧断言（直接读 `ctx.extra["_skip_tool"]`）在新 core 上失败，迁移后转绿；shim 单测覆盖两条分支。
