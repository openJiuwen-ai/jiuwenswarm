# B_01 — A2UI repair 空输出被误判合法，suppress 扣发的 chat.final 静默丢失

- 日期：2026-10-29
- 分支：dev-stable
- 症状（触发率测试 case #26，双表单同回复场景）：relay-claw 落库内容只剩 `散文 + <a2ui<a2ui-json>` probe 残片（61 字符），模型实际产出的 9K~18K A2UI 整块静默丢失；UI 显示「组件不可用」，无任何报错，刷新后依旧。

## 1. 根因链

```
模型 A2UI 输出校验失败（validator: valid=False）
  → finalizer repair 循环调 repair_call
    → JiuWenSwarmDeepAdapter.repair_model_response：self._model is None（officeclaw tenant pool 适配器在
      finalize 时不持有模型实例——repair 在 producer finally cleanup 之后执行）→ 返回 None
  → _coerce_model_message_content(None) → ""
  → spec.validate_response("")：无 tagged block、无 a2ui part → vacuously valid=True
  → 误报 status="repaired"、finalized=""（changed=true）
  → interface.py 流式收尾 `if finalized_assistant_message and (...)`："" falsy
    → suppress 扣住的原 chat.final 的重发整体跳过 → 整块内容永不送达 relay
  → relay 收到空 final，不触发 replace_final → 持久化 delta 累积的 probe 残片
```

sidecar 关键日志签名（修复前）：

```
A2UI finalizer validation complete: valid=False
A2UI finalizer repair attempt started: attempt=1
[JiuWenSwarmDeepAdapter] repair skipped: no model instance available
A2UI finalizer repair attempt complete: attempt=1 valid=True     ← 空内容伪装成修复成功
A2UI response finalization status: status=repaired               ← 且不会走 repair_failed 的 retry 降级
```

## 2. 修复（三层守卫，本提交）

| 层 | 文件 | 改动 | 激活条件（全部为原异常态，健康路径零触达） |
|---|---|---|---|
| 校验洞 | `runtime/a2ui/runtime/finalizer.py` | repair 输出 strip 为空 → 记 `last_error="repair returned empty content"` 并 `continue`（跑满 attempt 后落 `repair_failed`，触发既有 retry_without_a2ui 降级，返回可读非空文本） | 仅 a2ui 校验失败后 repair 返回 None/空 |
| 流式兜底 | `agent_adapter/interface.py` L~4250 | `suppress_a2ui_stream` 且 finalized 为空且原始内容非空 → 回退重发被扣的原始内容（relay 前端 replace_final + jsonrepair 仍有渲染机会） | 仅 suppress 激活 + finalized 空 |
| unary 兜底 | `agent_adapter/interface.py` L~3128 | finalize 返回空 → 恢复 finalize 前快照，防正文被清空 | 仅 a2ui finalize 返回空 |

## 3. 影响范围

仅 a2ui 链（通道白名单 web/officeclaw、a2ui 开关开启、含 A2UI 标记的响应）；非 a2ui 流量逐字节不变。`validate_a2ui_response("")==valid` 的公共语义（普通文本合法）不改，守卫放在 finalizer repair 循环内。

## 4. 验证

- 单测：`tests/unit_tests/a2ui/test_finalizer.py` 新增 2 例（空/None repair 输出 → repair_failed + 非空降级）、`test_response_finalization.py` 新增 1 例（入口恒非空且不含破损标签）；全套 106 passed。
- live（2026-10-29 复验 case #26 提示词）：
  - 修复前签名：`attempt=1 valid=True → status=repaired`，落库 61 字符残片、表单全丢
  - 修复后签名：`attempt=1/2 returned empty → status=repair_failed → A2UI repair failed, retrying without A2UI → finalized changed=true`，重发激活、内容不再丢
- §9.1 跨仓契约（发帧侧已实施，本提交）：`_make_a2ui_final_chunk` payload 增加 `a2ui_resend: True`——声明"suppress 链重发的权威终稿"。relay 消费侧（见 relay-claw PR5508 后续提交）见标记即整段覆写正文、不再按 `<a2ui-json>` 标签门控，降级无标签终稿同样覆写，probe 残片不再落库。发帧先合入安全：旧 relay 忽略未知 payload 字段，行为不变。

## 5. 后续可选项

officeclaw pool 适配器在 finalize 时不持有模型（`_model is None`）导致 repair 在该通道结构性不可用；如需真正可用，需让 `repair_model_response` 懒解析 `_model_cache`/当前选择模型，或调整 cleanup 与 finalize 次序——独立评估，不在本缺陷修复范围。
