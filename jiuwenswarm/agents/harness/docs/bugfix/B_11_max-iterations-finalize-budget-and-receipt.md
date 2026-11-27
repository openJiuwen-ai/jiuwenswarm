# B_11 — 轮数耗尽时无最终答复，且编辑稿保存状态不明

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_11 / max-iterations-finalize-budget-and-receipt |
| 修复日期 | 2026-11-26 |
| 严重程度 | 中，长任务轮数耗尽时用户拿不到产物状态和答复 |
| 关联 Issue | OfficeAce 1.9.0「班主任家长会 PPT 达到最大思考轮数」，错误参考号 E-1125-1212-85c6 |
| 关联 spec | 无（结果契约 `result_type=error` + `finish_reason=max_iterations` 与文案前缀不变） |

## 现象与定位证据

flash 模式 `react.max_iterations=100`。pptx-craft 模板复用在第 100 轮仍在调用 `apply_ops`，
[`after_react_iteration`](../../common/rails/last_iteration_finalize_rail.py#L189) 只能
`force_finish(error)`，模型没有机会保存或总结。会话 history 显示：

- 整个会话没有调用 `save_pptx` 或 `execute_fill_plan`。Live 编辑工具都返回 `saved: null`，
  只有 `<session-meta>` 的 revision 增长到 44。编辑稿只在 Live 会话里，不能说「产物已保存」。
- 权限中断恢复时，轮数接着之前的计数继续，这符合 AgentCore 的设计（`interruption_state.iteration + 1`），
  只用掉 2 轮，不改。
- RelayClaw 为此维护了一个按 SHA 校验后替换本文件的打包补丁。该补丁把收尾提示写进 system prompt，
  每轮剩余数都变，最后 15 轮的 KV/前缀缓存会全部失效（本会话每轮约 25 万 token、缓存命中 93%）；
  它的回执只认保存工具，把 Live 编辑的修订号当成已保存同样会误导用户。

## 根因与修复决策

1. **收尾预算**：剩余轮数（含本轮）≤ `min(15, max(2, max_iterations // 5))` 时，
   [`before_model_call`](../../common/rails/last_iteration_finalize_rail.py#L134) 经
   `prompt_attachment_manager` 写入 `task_finalization_budget` 运行时附件。附件作为尾部 user
   消息注入，system prompt 不变，缓存前缀保持稳定。内容包括当前轮次和剩余轮数，以及是否有未保存的
   修改；最后一轮要求「有未保存修改就只保存，否则不调工具直接总结」。不在窗口内时清除附件。
2. **文稿状态**：[`after_tool_call`](../../common/rails/last_iteration_finalize_rail.py)
   拆 `invoke_tool` / MCP 信封。Live revision 只由 `<session-meta>` 维护；`open_pptx` /
   `switch_file` / `save_pptx` 载荷只提供 `documentId` 和 `workingPath`，绑定工具
   （`open_pptx` / `switch_file`）载荷里的 revision 只作为基线，不写 Live revision，
   迟到的载荷也不会让 Live revision 回退。基线取首次观察到该文稿时的修订号（绑定载荷或
   首个 `<session-meta>`）：复用的 Live 文稿打开时可能已是版本 43，只读不改时不应判为
   未保存。「未保存」= Live revision 大于已保存版本和基线中较大者。只有
   `DEFAULT_SAVE_TOOL_NAMES` 中的工具成功返回时才记为已保存（`status:"saved"` 或
   `saved:{path,revision}`）；回执未带 revision 时视为保存到当时的 Live revision。
   失败结果、`bash` / `read_file` 等文本类工具的输出都不采信。
3. **收尾文案**：错误前缀和「建议」行不变，
   [`_document_status_lines`](../../common/rails/last_iteration_finalize_rail.py) 追加：
   有保存回执时写「最近一次已确认保存的草稿：{path}（版本 N）」，与 RelayClaw
   `agent-error-transform.ts` 的正则一致，修订号未知时省略「（版本 N）」；Live revision
   超过已保存版本和基线时，追加「编辑中的文稿…有未保存的修改：版本 N 尚未写入文件」；
   本次运行没有文稿活动时不追加任何内容。「已保存」与「未保存」不会因同一修订同时成立。
4. **状态生命周期**：rail 实例由同一 adapter 下的所有会话共享，所以状态按 session 分键（最多保留 256 个）。
   文稿的保存回执和修订号是会话内的事实，跨运行保留；每次新运行只重置「本次运行操作的
   文稿」，收尾文案和预算附件只报告本次运行操作过的文稿。新运行的判定：`before_invoke`
   收到文本 query，或 `react_iteration == 1`（外层 DeepAgent 上的 invoke 钩子可能拿不到
   session）。权限中断恢复从后续轮次继续，不会重置。`after_invoke` 只清预算附件、不清
   状态：流式单轮 invoke 在权限中断时外层结果不带 `result_type=interrupt`，在这里按结果
   清状态会丢掉中断前的保存回执。`max_iterations` 优先从当前 ctx 读取，读不到时回退到
   `before_invoke` 记下的值。

## 回归证据与适用范围

- [单元测试](../../../../tests/unit_tests/agentserver/rails/test_last_iteration_finalize_rail.py#L189)
  使用 issue 里的结果形态（`invoke_tool` 信封 + `<session-meta>`），覆盖：预算窗口边界；
  附件不改 system prompt；未保存、已保存、保存后又编辑三种情况；失败或不可信的结果；
  权限中断保留状态、新请求只报告自己操作过的文稿；会话隔离。检视补充（先红后绿）：
  回执缺 revision 不再同时输出「已保存」和「未保存」或「版本 None」；复用文稿打开后只读
  不判未保存；迟到的绑定载荷不回退 Live revision；流式权限中断（外层结果非 interrupt）
  后恢复仍保留保存回执；下一次请求只读已保存的文稿不判未保存。
- [真实 ReAct 循环](../../../../tests/unit_tests/agentserver/rails/test_last_iteration_finalize_rail.py#L612)：
  `max_iterations=5` 时只有第 4、5 轮的模型输入带预算附件，5 次调用的 system 消息完全一致；
  保存回执经过真实的 `after_tool_call` 进入最终文案。
- 未连接 OfficeAce 实机验收。轮数能否在 100 轮内跑完，仍依赖 pptx-craft 的 `execute_fill_plan` 回补。
- 合入后需通知 RelayClaw 同步删除 `scripts/jiuwen-finalization-patch.mjs` 及其调用点（或更新 hash），
  否则上游源码变更会让该补丁的 SHA 校验抛错，导致打包中断。
