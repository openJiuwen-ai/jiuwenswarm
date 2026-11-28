# 执行模式参数冲突与能力查询提示丢失

| 项 | 值 |
|---|---|
| 日期 | 2026-11-27 |
| 类型 | bugfix |
| 分支 | fix/ppt-segment-file-handoff |
| 关联规约 | S_11_explicit-segment-execution.md；S_12_model-disclosure-and-delivery.md |

## 现象与证据

任务 212c802c30e2 的公开调用先传 __full__ 和 params，得到 invalid_execution_mode；
随后仅传 skill 和 fresh_start=true，模型看到 tool failed without details；最终改用普通阶段自动组批。
Relay 会话事件 45a354c4a03a77ab65062684c0ef1a1bbd655303eb950680ea2012773eac4a2d/1-534.json
的 seq=26/27、45/46、70/438 分别记录这些调用及结果。
该轨迹只能证明非法调用与后续模式切换，不能证明完整技能根节点内部执行失败。

## 根因与修复

共用工具暴露多种模式，模型指南未集中解释 __full__ 与阶段 params 的边界。
仅传 skill 的能力查询返回 needs_accelerator、success=false 和 candidates，但缺少错误说明；
通用工具归一化按失败渲染，模型只收到默认错误文本，无法得知尚未选择入口及候选内容。

- 中英文公共指南、工具参数说明和根技能加载附录按接口契约区分完整执行、阶段、批次与能力查询。
- __full__ 的任务要求经 query 传入，省略 params（包括空对象）、accelerators 和 auto_batch=true；
  用通用技能名给出完整执行调用示例。用户限定执行方式时，参数重试保留该约束，无法满足则如实说明。
- full 参数冲突仍在业务前拒绝，error 给出具体冲突和纠正方法，error_code 保留原机器标识。
  不静默丢弃参数，不从用户自然语言关键词推断或切换模式。
- 能力查询保留 needs_accelerator、success=false、delivery_confirmed=false 与 candidates，补充
  可经既有归一化展示的说明及准确候选；fresh_start 不选择入口。
- 不修改通用结果归一化器、工具签名、技能流水线、模式路由、持久化结构及发送机制。

## 验证

环境：当前分支 HEAD 903e1976a 加本次及此前已有工作区差异，Python 3.13.14 与本地安装 SDK。
通过注册工具的真实 invoke 进入；完整根节点及发送等外部边界使用测试替身，不调用真实模型。

1. 定向先红：14 failed、75 deselected（2.91s），覆盖 full 的空/非空 params、批次列表、
   auto_batch=true 冲突；能力查询的默认/null/false 及 fresh_start 两态；Deep/Flash × 中英文指南载体。
2. 实现后相同定向用例：14 passed、75 deselected（2.32s）。错误说明通过真实 normalize_tool_result
   和 render_tool_result_for_llm 验证，候选与纠正方法未退化为默认错误；非法 full 零实例、零业务节点。
3. 完整模块及旧公开工具回归：893 passed（36.33s）。命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_skill_turbo_tools.py -q -o addopts='--asyncio-mode=auto' -o log_cli=false --tb=short --show-capture=no
```

日志为 `%TEMP%/turbo-mode-contract-red.log`、`turbo-mode-contract-green.log`、
`turbo-mode-contract-regression.log`。完整根节点模式覆盖省略/null/false，保持零 planner；
既有自动组批、HITL 与发送保护回归通过。

本轮仅修改 JiuwenSwarm；Relay 源码、技能、配置和业务数据未改动，未重启、提交或推送。
按 jw-dev 规范未运行格式化或静态检查；未执行真实模型请求，不能据此保证模型将来始终选对模式。
后续若修改工具说明载体、结果归一化或模式路由，应重新验证这些对应路径。
