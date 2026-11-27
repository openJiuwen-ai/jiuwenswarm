# B_08 — SkillTurbo chat.error 出口未复用 server 侧结构化错误码兜底

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-24 |
| 严重程度 | 低（仅影响错误帧分类，不改变控制流；image_url unsafe 经这些路径浮出时会丢 `code`） |
| 范围 | `jiuwenswarm/server/runtime/skill_turbo/executor.py`、`tests/unit_tests/server/runtime/skill_turbo/test_executor_error_codes.py` |
| 测试基线 | `test_executor_error_codes.py` 3 passed；受影响回归 328 passed |
| 关联 spec | `S_08_event-stream-protocol.md`（chat.error 构造口径；事件字段 `code` 为增量） |

## 现象

外层 `interface_deep` 的 `chat.error` 帧已统一经 `stream_utils.build_chat_error_event`
兜底识别 `image_url unsafe` 并补 `code=image_url_unsafe`（B_09），但 skill_turbo 内部
`executor._make_error_chunk` / `_make_node_error_chunk` 仍直接构造
`{"event_type": "chat.error", "error": ...}`。若节点内 LLM 异常文本携带
`image_url ... unsafe` 浮出到这两条路径，帧上没有 `code`，前端分类器只能落到
`unknown` / `OA.05000090`。

## 根因

`chat.error` 的词法兜底（`_image_unsafe_error_code`）只下沉在 server 侧统一构造器里；
skill_turbo 自己维护了一套裸 dict 构造点，没有复用该构造器。两处错误出口形状因此漂移：
同一条异常经 task_failed 主路径带 `code`，经 skill_turbo 节点错误路径不带。

## 决策

两处出口改为复用 `build_chat_error_event({}, error)`，再按需补 `plan_name` / `task_id`：
结构化 code 的优先级与词法兜底完全由 server 侧单一实现决定，skill_turbo 不再重复维护
错误正文匹配逻辑；非 image 错误的 payload 形状不变（无 `code` 键）。

## 拒绝的方案

- **在 skill_turbo 内复制一份 `_image_unsafe_error_code`**：形成第二个词法真源，后续新增
  错误分型要改两处，容易漂移。
- **只在网关层补 code**：`chat.error` 帧在 skill_turbo 内已被构造并透传，网关侧拿不到
  原始异常对象的可靠分型上下文，且会扩大网关职责。

## 验证

- `test_make_error_chunk_adds_image_unsafe_code` / `test_make_node_error_chunk_adds_code_and_node_metadata`
  断言两条路径都带 `code=image_url_unsafe` 且保留 `plan_name` / `task_id`。
- `test_make_error_chunk_keeps_non_image_error_shape` 锁定非 image 错误形状不变。
- 回归：harness rails/multimodal、server `chat.error` 单测、adapter 出站帧共 328 passed。

## 已知遗留

- 其余非模型异常来源的裸 `chat.error`（如参数校验、命令错误）语义上没有 image_url 分型，
  不在本次扩展范围；若未来新增「模型异常文本」出口，应直接复用 `build_chat_error_event`。
