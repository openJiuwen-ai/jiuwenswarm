# duplicate 作答占位返回的 NEUTRAL 停止提示矛盾 + 防重三件套测试缺位

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_01 / duplicate-resume-stop-hint |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/skill_turbo_tools.py`（工具体 duplicate 分支）、`agent_adapter/interface_deep.py`（pending reload 前置） |
| 归档日期 | 2026-10-20 |
| 关联 spec | S_06_hitl-interrupt-resume.md（P3 统一 resume 路径步 6 + 边界 case 2 + 测试映射）；S_04 / S_07 的 NEUTRAL 收尾语义不受影响（属消费侧误用） |
| 发现渠道 | PR 检视意见（阶段 2 + Path B 统一 PR，删 Path A 后引入的边路行为变化） |

## 复现场景

用户双击 ask_user 卡片提交（或网关重试）时，请求 A 正在 `resume_stream` 执行中并已
`mark_resume_in_flight`；请求 B 命中 `resume_ctx.resume_in_flight=True` 走工具体
duplicate 提前返回（skill_turbo_tools.py 工具体 resume 分支）。

修复前 B 的 tool_result：

```
任务正在处理中，本次重复作答已忽略

[SYSTEM] skill_acceleration_exec 任务已结束，但内部交付流水线未能确认文件已
生成并发送给用户……不要声称文件已发送。
```

B 轮 LLM 据此向用户回复"PPT 尚未交付"，与 A 轮稍后的交付总结直接矛盾。

## 根因

duplicate 分支的返回值走了 `_wrap_skill_turbo_result`。wrap 对 `success=True` 的
结果按 artifact_holder 三分支追加收尾提示，而该分支的 `SkillTurbo` 是 fresh 实例
（`SkillTurbo(config)`，artifact_holder 恒空）——必然落入 NEUTRAL 分支（无 P10
骨架、无 `send_file_status=sent` 通用确认），追加"任务已结束…不要声称文件已发送"。
该文案描述的是"执行已结束但交付未确认"，与占位语义"任务正在处理中，原始请求仍在跑"
相位相反。

Path A 时代该路径为空流 no-op（无 tool_result 文案），删除 Path A 统一走 handle_resume
重放后，防重逻辑移入工具体但沿用了 wrap 包装，引入此矛盾。

## 修复口径

1. **duplicate 分支裸 dict 返回且不携带 success 键**：
   `{"result": "任务正在处理中，本次重复作答已忽略，请等待原始请求完成"}`。
   - 不经 `_wrap_skill_turbo_result`（wrap 的产物摘要 / PPT 骨架 / 停止提示三件
     能力对本分支全部无意义——B 未产出任何产物，占位语义禁止任何"结束态"收尾；
     wrap 内 ContextVar 操作在 B 请求独立上下文无副作用，跳过安全）。
    - 不携带 success 键：`stream_event_rail._emit_tool_result` 仅在
      `_infer_tool_result_error` 可判定（success / is_error / status / exit_code）
      时才写帧级 success；本 dict 形态判 None → `chat.tool_result` 帧不带
      success 字段 → 上层应用侧 `isSuccessfulPptTurboToolResult`
      （纯结构匹配 event_type + tool_name +
      success===true，下游消费点已代码取证）不会把占位帧误判成 PPT turbo
      成功、清空摘要缓冲 / 替换最终正文（原"成功占位"携带 success=True
      会确定性触发该匹配）。
2. **防重三件套测试补齐**（此前零直接测试；`_did_mark_in_flight` 守卫修复——PR
   评论 193793875——无先红后绿复现用例）：
   - 判重命中：不 mark、不误清原始请求 in-flight、不启动第二次 resume_stream、
     返回不含任何 `[SYSTEM]` 停止提示且 `_infer_tool_result_error` 判 None
     （帧级无 success 的守护断言）；
   - 正常路径：mark 一次、finally 清一次；
   - AbortError 二次中断：清 in-flight（防对断点的下次作答被误判重复）但保留
     新断点（不 clear_resume_ctx / 不 post_run）。
3. **恢复 pending reload 前置守护**：Path A 删除时同因删除的
   `test_process_stream_applies_pending_reload_before_skill_turbo_resume` 无等价
   替代，而 `interface_deep.py` 的 skill_turbo resume 请求前置
   `_maybe_apply_pending_reload()`（配置重载先于 resume 恢复模型）仍在——以
   参数化用例恢复守护（resume 请求触发 reload / 普通请求不触发，以模型配置
   校验失败提前收口流）。

## 测试

`tests/unit_tests/agentserver/test_skill_turbo_tools.py`：
`test_duplicate_resume_answer_returns_clean_placeholder_without_stop_hint` /
`test_normal_resume_marks_and_clears_in_flight_once` /
`test_hitl_abort_clears_in_flight_but_keeps_checkpoint`；
`tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py`：
`test_process_stream_applies_pending_reload_for_skill_turbo_resume`。

修复前 `test_duplicate_resume_answer_returns_clean_placeholder_without_stop_hint`
红（`'[SYSTEM]' not in '任务正在处理中…'` 失败，NEUTRAL 提示确在被追加）。
