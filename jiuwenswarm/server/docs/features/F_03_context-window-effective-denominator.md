# 上下文窗口生效分母（catalog 上限 + chat.send 选用）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-17 |
| 范围 | `common/schema/chat_send.py`；`server/runtime/agent_adapter/deep/context_window.py`；`interface_deep.py`（Deep 主对话）；单测 `test_context_window.py`、`test_apply_request_context_window.py` |
| 关联 spec | [[S_06_context-window-effective-denominator]]；交叉 common [[S_10]] N9 |
| 关联设计叙事 | 无 |
| 测试基线 | 上述两文件 pytest 通过（见验证节） |
| Refs | [#5227](https://gitcode.com/openJiuwen/jiuwenswarm/issues/5227)；思考侧 [[F_02_thinking-passthrough-no-rewrite]] |

## 关联依据

- spec：server `S_06`（双字段、合成顺序、无粘滞、请求身份读 catalog、Deep 接线）。
- 实施：纯函数模块 + Deep 一元/流式接线；未接 Team/Flash/Code；未改 agent-core。

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

## 与设计的偏差

无目标方案偏差。圆环 / agent-core 公开 API 按范围有意不做。

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 | 实际结果 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| 2026-11-17 纯函数 + apply | 本地工作区 | 见下 | 通过 | 完整 `interface_deep` 装配、compressor 端到端、relay 联调 |

```text
.venv\Scripts\python.exe -m pytest ^
  tests/unit_tests/server/runtime/agent_adapter/test_context_window.py ^
  tests/unit_tests/server/runtime/agent_adapter/test_apply_request_context_window.py ^
  -o addopts="--asyncio-mode=auto" -q
```

### 可见行为、副作用与证据有效范围

已证明：合成优先级、非法值、Auto 忽略选用、不读 `model_config_obj`、按 ref/name 解析 catalog、写/清槽位、engine 播种、`pending`。  
未证明：真实 Deep 装配下压缩阈值变化；relay 实发字段。

## 验证结论

- 已证明：S_06 合成与写槽（单测）。
- 未覆盖：relay 联调与压缩闭环观测。
- 交付判断：可开联调；观察 `[ModelWindow]`（含 `pending=`）。

## 已知遗留

| 遗留项 | 未处理原因 / 性质 | 影响与现有处置 | 后续入口 |
| --- | --- | --- | --- |
| relay 联调 | 范围外待验 | 契约字段已定 | 联调验收 |
| Auto 路由后按实际模型重算上限 | 有意取舍 | 路由前按 resolve 模型 catalog，且忽略选用 | 若需要另开 |
| 圆环 / `get_context_usage` | 本期不做 | 展示可能与压缩分母不一致 | 后续专项 |
| Team 等非 Deep | 方案范围 | 仍用今日分母 | 扩展时改 S_06 |
