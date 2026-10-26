# plan_node fallback 一次性 guard 通道不对称 + 标志键 guard 拒绝路径泄漏

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_02 / fallback-guard-asymmetry |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/plan_node.py`（run / run_stream / execute_subplan_stream） |
| 归档日期 | 2026-10-20 |
| 关联 spec | S_04_node-contract.md（N4 + 模板方法语义 + 边界 case 18）；S_07_fallback-and-degradation.md（N18 + P3 + 边界 case 19 + 修改检查单） |
| 发现渠道 | PR 检视意见（阶段 2 + Path B 统一 PR） |

## 复现场景

两处独立的契约不一致，均以"fallback 产出再携带 `_needs_fallback=True`"为前提
（当前 fallback 子代理产出不含该标志，理论路径；先红后绿用例已证实可达）：

1. **异常通道不置位 guard（可达双触发）**：节点 `_execute_stream` 抛非
   AbortError/FallbackContractError 异常 → `run_stream` 异常通道进 fallback（未置位
   `_fallback_triggered`）→ fallback 末 chunk 携带 `_needs_fallback=True` → 父节点
   `execute_subplan_stream` 的 `subplan.try_begin_fallback()` 判定通过（guard 从未
   置位）→ 同一次 run 内同节点 fallback 二次触发（双层子代理），违反 S_07 N18
   "同节点一次 run 至多触发一次"。`run()` 异常通道同样不置位（与标志通道"先置位
   再进 fallback"不对称；单独看 run() 因异常通道直接 return 不可达，但组合父节点
   判定即成可达路径）。
   复现用例：`test_exception_fallback_guard_blocks_second_flag_trigger` 修复前红
   （`assert 2 == 1`：fallback_stream 被调两次）。

2. **标志键在 guard 拒绝路径不剥离（契约卫生）**：`execute_subplan_stream` 的检测
   条件 `last_chunk.get("_needs_fallback") and subplan.try_begin_fallback()` 在
   guard 返回 False 时整体短路，`last_chunk.pop("_needs_fallback")` 不执行——内部
   协议键随 after 回调 result（`_after_subplan_execute` 收到含标志的 dict）外泄；
   `run()` 路径同理（guard 已置位时 result 原样返回含标志键）。
   复现用例：`test_guard_reject_strips_flag_before_after_callback` 修复前红。

## 根因

F_02 引入标志通道时，guard 置位只写在标志通道（run 检测块内置位 /
execute_subplan_stream 经 `try_begin_fallback()` 原子置位），异常通道早于标志机制
存在、进 fallback 时未同步置位；标志剥离写在 guard 判定**之内**（短路路径跳过剥离），
剥离与判定耦合。

## 修复口径

1. **异常通道进入 fallback 前置位 guard**：`run()` 的 `except Exception` 分支与
   `run_stream()` 的 `except Exception` 分支，在调用 `_fallback[_stream]_callback`
   前置 `self._fallback_triggered = True`——两通道对称消费同一次性预算。
2. **标志剥离与 guard 判定解耦**：`run()` 对 dict 结果先无条件
   `result.pop("_needs_fallback", None)` 再以剥离值判定；`execute_subplan_stream`
   对 last_chunk 同理。guard 拒绝时同样剥离（内部协议键不泄入返回值 / after 回调）；
   未携带标志的节点行为逐位不变。
3. 既有断言同步：`test_flag_guard_prevents_second_trigger` /
   `test_flag_guard_second_run_no_trigger` 原锁定"guard 拒绝时原样透传（含标志键）"，
   修正为"剥离后透传"。

注意（已知边界收口）：剥离发生在 chunk 已 yield 之后（真实节点 last_chunk 与已
yield chunk 为同一对象），流式消费方在 yield 时刻仍可能见到标志键——"失败过程
原样流式转发"是 [[S_04]] 既有语义。本修复收口三个落盘面：返回值 / after 回调
（plan_node 剥离）+ **前端帧**（executor `_make_node_delta_chunk` 对 data 浅拷贝
剔除 `_needs_fallback`——生产者在技能侧仓 pptx-craft 已落地（P8.0 预处理失败 /
P8 qa 全败 / P9 页面恢复与 convert 失败），该剔除是活路径必需，见 [[F_02]]
已知边界 #7）。

## 测试

`tests/unit_tests/server/runtime/skill_turbo/test_plan_node_fallback_flag.py`：
新增 `test_exception_fallback_consumes_one_shot_guard`（run 异常通道经公开访问器
可见置位）/ `test_exception_fallback_guard_blocks_second_flag_trigger`（流式组合
双触发复现）/ `test_guard_reject_strips_flag_before_after_callback`（after 回调
不泄漏）；修正两处旧断言。12 用例，修复前 6 红（含两处旧断言）。
