# 上下文窗口生效分母规约

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_06 / context-window-effective-denominator |
| 关联模块 | `jiuwenswarm/server/runtime/agent_adapter/deep/context_window.py`；`interface_deep.py`（Deep 主对话接线）；协议字段 `jiuwenswarm/common/schema/chat_send.py` |
| 最近一次修订日期 | 2026-11-17（同日修订：一元 reload 时序改回历史顺序） |
| 关联 feature | [[F_03_context-window-effective-denominator]] |
| 设计依据 | 仓库根目录 `方案_模型思考强度与上下文窗口.md` §3.2 / §3.4 |

## 范围 / 边界

本规约管 **Deep 主对话** 在 invoke 前如何把「卡片选用 / catalog 上限」合成生效上下文窗口，并写入本会话压缩/卸载分母。

不管：思考档位透传（common [[S_10]] N8）；圆环 / `get_context_usage` 展示口径；Team / Flash / Code 路径；agent-core 公开 API；前端夹紧 `selected ≤ max`。

## 不变量

- **N1 双字段、禁止进厂商请求**：catalog 上限字段为 `models.defaults[].context_window_tokens`（与 `model_config_obj` **同级**）；请求选用字段为 `chat.send.params.selected_context_window_tokens`。二者均 **禁止** 写入 `model_config_obj`（`ModelRequestConfig` extra=allow，会污染厂商请求）。
- **N2 生效合成顺序**：合法正 int 选用 → 否则 catalog 上限 → 否则本特性不覆盖（沿用今日内置表→200000）。`enable_routing is True`（Auto）时 **忽略选用**，仅用 catalog 上限（若有）。
- **N3 合法值**：正 `int`；排除 `bool`、非 int、`≤0`。非法选用视为缺省，回退 catalog。
- **N4 无粘滞**：本帧无生效值时清除本特性覆盖并还原快照前的槽位；不得沿用上一帧选用/上限。
- **N5 catalog 反查按请求身份**：经 `params.model_ref`（优先）→ 唯一 `model_name` 命中 →（无身份时）唯一 `is_default` 条目读取 `_model_entry_by_ref`。**禁止**用 `Model` 对象 `is` 身份匹配 cache（pending reload 后对象可能已换）。
- **N6 写入目标**：优先写会话 `context._context_window_tokens`；context 尚未 create 时播种 `context_engine._config.context_window_tokens`；两处皆无时记 `pending`，下轮再试。本期不改 agent-core。
- **N7 接线时机与范围**：Deep 主对话在「本请求已完成模型 resolve/apply，并已调用 `_maybe_apply_pending_reload`」之后、invoke **之前**写入窗口。一元保持历史顺序 `resolve → apply → reload → 写窗口`；流式为既有 `reload → resolve → apply → 写窗口`。本期不改一元 reload 时序。Team 等非 Deep 主路径不接线。

## 接口契约

| 符号 | 语义 |
|---|---|
| `ChatSendParams.selected_context_window_tokens` | 可选正 int；E2A 透传，TypedDict 仅文档契约 |
| `resolve_catalog_window_tokens(...)` | 按 N5 解析 catalog 上限；不读 `model_config_obj` |
| `resolve_effective_context_window(...)` | 按 N2/N3 合成 `(effective, source)` |
| `apply_context_window_to_session(...)` | 按 N4/N6 写/清槽位与快照 |
| `run_request_context_window_apply(...)` | 一次请求的合成 + 写入 + 日志 `[ModelWindow]` |
| `JiuWenSwarmDeepAdapter._apply_request_context_window` | Deep 主对话装配入口 |

## 执行路径

1. **P1 一元**（保持历史顺序）：`_resolve_model_for_request` → `_apply_model_to_react_agent` → `_maybe_apply_pending_reload` → `_apply_request_context_window` → invoke。
2. **P2 流式**（既有顺序，仅插入写窗口）：`_maybe_apply_pending_reload` → `_resolve_model_for_request` → `_apply_model_to_react_agent` → `_apply_request_context_window` → invoke。
3. **P3 catalog sync**：`sync_agents_configs` 写入的 defaults 条目整表进入 `_model_entry_by_ref`；建 `Model` 时只消费 client/config_obj/modalities，同级窗口键自然保留可读且不进厂商请求。

## 边界 case 清单

| 场景 | 现行为 | 测试 |
|---|---|---|
| 选用合法优先于 catalog | source=`selected` | `test_context_window.py` |
| Auto `enable_routing` | 忽略选用 | `test_apply_request_context_window.py` |
| 窗口键仅在 `model_config_obj` | catalog 视为无上限 | 同上 |
| `model_ref` / 唯一 `model_name` | 按 map 取值，不靠 Model `is` | 同上 |
| 歧义 `model_name` | catalog 上限 None | 同上 |
| context 与 engine 皆无 | snapshot.`pending`；日志 `pending=True` | 同上 |
| 本帧无生效值 | 清除覆盖，不粘滞 | `test_context_window.py` |

## 修改检查单

- 改生效合成或字段名 → 同步本 spec N1–N3、`ChatSendParams`、方案文档、单测字段字面量。
- 改 catalog 反查 → 禁止回到 Model `is`；不要为窗口特性改动一元 reload 时序。
- 改写入槽位 → 确认 compressor / overflow 仍读 `_context_window_tokens`；圆环口径若纳入范围另开条款。
- 扩展 Team/Flash → 本 spec 增路径或明确仍排除；不得静默只改 Deep。

## 与其它 spec 的关系

- server [[S_03]]：agent_adapter / Deep 运行时归属。
- common [[S_10]]：思考透传与「窗口键禁止进 `model_config_obj`」交叉约束（N9）；窗口生效写入在本规约。
