# 上下文窗口生效分母（catalog 上限 + chat.send 选用）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-17 |
| 范围 | `common/schema/chat_send.py`；`server/runtime/agent_adapter/deep/context_window.py`；`interface_deep.py`（Deep 主对话）；单测 `test_context_window.py`、`test_apply_request_context_window.py` |
| 关联 spec | [[S_06_context-window-effective-denominator]]；交叉 common [[S_10]] N9 |
| 关联设计叙事 | 无（方案在仓库根目录 `方案_模型思考强度与上下文窗口.md`） |
| 测试基线 | 上述两文件 pytest：**通过**（见验证节） |
| Refs | 方案同文件；思考侧 [[F_02_thinking-passthrough-no-rewrite]]（common） |

## 关联依据

- spec：server `S_06` 定义双字段、合成顺序、无粘滞、按请求身份读 catalog、Deep 接线时机。
- 实际实施：纯函数模块 + Deep 一元/流式接线；未接 Team/Flash/Code；未改 agent-core。

## 决策归属与关系

新特性归档。思考档位（F_02 / S_10 N8）继续有效且正交；窗口禁止走 `model_config_obj` 透传。

## 实际实现与开发决策

1. **上限**：`models.defaults[].context_window_tokens`（条目同级）。
2. **选用**：`chat.send.params.selected_context_window_tokens`。
3. **生效**：合法选用 → catalog 上限 → 不覆盖。
4. **catalog 反查**：`resolve_catalog_window_tokens` 按 `model_ref` / 唯一 `model_name` / 唯一 `is_default`，**不用** `Model is`（避免 pending reload 后 cache 换对象导致上限静默丢失）。
5. **接线**：仅插入写窗口；一元保持 `resolve → apply → reload → 写窗口`；流式保持既有 `reload → resolve → apply → 写窗口`（不改 reload 时序）。
6. **写入**：会话 `_context_window_tokens`；无 context 时播种 engine config；皆无则 `pending`。
7. **Auto**：`enable_routing is True` 忽略选用。

## 与设计的偏差

| 项 | 说明 |
|---|---|
| 初版用 `Model is` 反查 catalog | 已改为请求身份反查（二次审查修复） |
| 曾将一元 reload 前移以对齐流式 | **已改回历史顺序**；窗口不依赖该时序（catalog 按请求身份读取） |
| 圆环 / agent-core | 按方案有意不做 |

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 | 实际结果 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| 2026-11-17 纯函数 + apply | 本地工作区 | 见下 | 通过 | 真实 `interface_deep` 入口、compressor 端到端、relay 联调 |

```text
.venv\Scripts\python.exe -m pytest ^
  tests/unit_tests/server/runtime/agent_adapter/test_context_window.py ^
  tests/unit_tests/server/runtime/agent_adapter/test_apply_request_context_window.py ^
  -o addopts="--asyncio-mode=auto" -q
```

### 可见行为、副作用与证据有效范围

已证明：合成优先级、非法值、Auto 忽略选用、不读 `model_config_obj`、按 ref/name 解析 catalog、写/清槽位、engine 播种、`pending` 重试意图。  
未证明：经完整 Deep adapter 装配的压缩触发阈值变化；relay 实发字段。

## 验证结论

- 已证明范围：方案 §3.2/§3.4 的后端合成与写槽逻辑（单测）。
- 未覆盖：联调与压缩闭环观测。
- 可支持交付判断：**可开 relay 联调**；联调需盯 `[ModelWindow]`（含 `pending=`）与首轮/hot-reload 场景。

## 已知遗留

| 遗留项 | 未处理原因 / 性质 | 影响与现有处置 | 后续入口 |
| --- | --- | --- | --- |
| relay 联调 | 范围外待验 | 契约字段已定 | 联调 checklist |
| Auto 路由后按实际模型重算上限 | 有意取舍 | 路由前按 resolve 模型 catalog，且忽略选用 | 路由后钩子若需要另开 |
| 圆环 / `get_context_usage` | 方案本期不做 | 展示可能与压缩分母不一致 | 后续 agent-core / 圆环专项 |
| Team 等非 Deep | 方案 P6 | 这些路径仍用今日分母 | 扩展时改 S_06 |

## 实施演进与修订

### 2026-11-17 二次审查修复

- 原结论：catalog 用 `Model is`；无独立 S_NN；注释偏中文。
- 现结论：请求身份反查；新增 `S_06`；server 侧新增代码英文注释；补 pending / catalog 单测。
- 曾误将一元 reload 前移：已改回，保持 PR 仅含方案必要面。
