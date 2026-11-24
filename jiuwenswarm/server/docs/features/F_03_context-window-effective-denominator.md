# 上下文窗口生效分母（catalog 上限 + chat.send 选用）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-17（Deep）；2026-11-24（Team 扩展） |
| 范围 | `common/schema/chat_send.py`；`deep/context_window.py`；`interface_deep.py`（Deep）；`agents/swarm/{config_specs,assembly}.py`；`team_helpers.py`（Team）；单测 `test_context_window*.py`、`test_team_context_config.py` |
| 关联 spec | [[S_06_context-window-effective-denominator]]；交叉 common [[S_10]] N9 |
| 关联设计叙事 | 仓库根 `方案_Team模式_模型思考强度与上下文窗口.md` |
| 测试基线 | 见验证节 |
| Refs | [#5227](https://gitcode.com/openJiuwen/jiuwenswarm/issues/5227)；思考侧 [[F_02_thinking-passthrough-no-rewrite]] |

## 关联依据

- spec：server `S_06`（双字段、合成顺序、无粘滞、请求身份读 catalog、Deep 接线、Team N8）。
- 实施：纯函数模块 + Deep 一元/流式接线 + Team 装配分母；未接 Flash/Code；未改 agent-core。

## 决策归属与关系

新特性。与思考透传（F_02 / S_10 N8）正交；窗口键禁止写入 `model_config_obj`（S_10 N9）。

## 实际实现与开发决策

1. **上限**：`models.defaults[].context_window_tokens`（条目同级）。
2. **选用**：`chat.send.params.selected_context_window_tokens`。
3. **生效**：合法选用 → catalog 上限 → 不覆盖（内置表→200000）。
4. **catalog 反查**：按 `model_ref` / 唯一 `model_name` / 唯一 `is_default` 读 `_model_entry_by_ref`，不用 `Model is`。
5. **接线**：一元 `resolve → apply → reload → 写窗口`；流式在既有顺序末尾插入写窗口；不改 reload 时序。
6. **写入**：会话 `_context_window_tokens`；无 context 时播种 engine config；皆无则 `pending`。
7. **Auto**：`enable_routing is True` 时忽略选用。
8. **Team（2026-11-24）**：`request_metadata` 透传 selected / enable_routing；`_team_context_engine_config` 有合法 selected 则直接写分母（可破 200K）；否则 catalog 进既有 min；复用 Deep 纯函数。分母写在成员 `context_engine_config`，随 **建团/首轮 runner** 生效。
9. **Team 中途改档（有意不做）**：不仿 Deep 每帧写多成员 `_context_window_tokens`（方案 B）。follow-up 仍 enrich，但不把新 spec 交给既有 runtime。

## 与设计的偏差

- Team 方案初稿曾写「每帧 enrich 可覆盖分母」；实现后确认 enrich 结果在 follow-up 不进入 live runner。**已按产品决定保持方案 A，不做 B**；契约与联调改为「建团帧生效 / 中途改档不保证」。见 `S_06` N8。
- 圆环 / agent-core / Flash·Code / 成员多模型分档仍按范围有意不做。`model_ref` 统一选型为主路径假设，非硬门闩。

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 | 实际结果 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| 2026-11-17 纯函数 + apply | 本地工作区 | Deep 两文件 pytest | 通过 | compressor 端到端、relay |
| 2026-11-24 Team 分母 | 本地 `feat/model-thinking-context-window` | 见下 | Deep 31 + Team 56 passed | Team 实机压缩联调 |

```text
.venv\Scripts\python.exe -m pytest ^
  tests/unit_tests/server/runtime/agent_adapter/test_context_window.py ^
  tests/unit_tests/server/runtime/agent_adapter/test_apply_request_context_window.py ^
  tests/agents/swarm/test_team_context_config.py ^
  -o addopts="--asyncio-mode=auto" -q
```

### 可见行为、副作用与证据有效范围

已证明：Deep 合成/写槽（含中途改档无粘滞）；Team 合成函数 selected 破硬顶、catalog 进 min、Auto、非法回退。  
未证明：Team 实机压缩阈值；relay Team 实发；**Team follow-up 改档**（本期明确不保证）。

## 验证结论

- 已证明：S_06 Deep + Team N8 合成（单测）。
- 未覆盖：relay / Team 实机压缩；Team 多轮改档（非目标）。
- 交付判断：可开联调（**建团/首轮**场景）；观察 `[ModelWindow] team applied …` / `Team context window capped`。

## 已知遗留

| 遗留项 | 未处理原因 / 性质 | 影响与现有处置 | 后续入口 |
| --- | --- | --- | --- |
| Team 同会话中途改窗即时生效 | 方案 B 暂缓（2026-11-24） | 改档需重建团才吃到新分母 | 产品要一致时开 B |
| relay 联调（含 Team） | 范围外待验 | 契约字段已定 | 联调验收 |
| Auto 路由后按实际模型重算上限 | 有意取舍 | 路由前按 resolve 模型 catalog，且忽略选用 | 若需要另开 |
| 圆环 / `get_context_usage` | 本期不做 | 展示可能与压缩分母不一致 | 后续专项 |
| Flash / Code | 方案范围 | 仍用今日分母 | 扩展时改 S_06 |
| 成员多模型分档 | 本期不做 | 无 ref 时按各成员模型名读 catalog | 产品另开 |
