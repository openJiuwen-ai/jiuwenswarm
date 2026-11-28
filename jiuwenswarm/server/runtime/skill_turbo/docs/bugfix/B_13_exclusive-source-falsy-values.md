# 互斥来源继承误拒绝合法假值

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-28 |
| 类型 | bugfix |
| 范围 | segment_inputs.py 的互斥来源继承；输入及批次回归 |
| 关联 spec | S_10_segment-state-and-inputs.md N10 |
| 状态 | 修复及回归完成 |

## 现象与影响

当 input_schema.oneOf 仅包含多个单字段 required 分支，且没有显式选择来源时，
输入装配继承第一个声明来源的已验证事实。旧判断将 0、False、空字符串均当作缺失，
即使参数规则允许这些值也返回 blocked/exclusive_source_missing_or_unverified。
同值显式传入可以通过，批次则在后段输入装配时中止，后续节点未执行。

## 根因与修复决策

原 not inputs.get(selected) 混淆了缺失和真假性。改为键不存在或值为 None 时判缺失；
来源仍须为 ledger/upstream 且已验证。保留原路径、前置条件及 Schema 校验，
不改变显式键选择、互斥投影、默认第一来源、台账结构或恢复重放规则。
空文件路径、低于最小值、错误类型等输入仍拒绝，不通过扩大空值豁免修复。

## 回归证据与适用范围

候选为 PR2 分支 aa8a53dde 加本次未提交差异。Python 3.13.14，使用锁定的
agent-core 228bd7da2 源码和原虚拟环境其他依赖。新增 20 项参数化场景。

- 修复前：13 failed、7 passed、60 deselected（4.15s）。台账和上游继承合法
  0/False/空字符串均失败，而显式值通过；三种真实批次也返回 blocked。
  无效值用例原先提前返回来源缺失，尚未到达预期的路径或 Schema 校验。
- 修复后：互斥输入、通用输入和批次定向回归 104 passed（10.16s）。
  验证值与类型保留、来源数据不被改写；缺失/None/未验证/defaults 仍 blocked；
  空路径和违规假值由原校验拒绝；三个批次均实际执行完三段、revision=3。
- 完整 SkillTurbo 模块、Flash 披露、stream source、旧工具及 HITL/恢复回归：
  1045 passed（47.72s），日志为同目录 regression.log。

日志位于本机临时目录 pr2-exclusive-falsy-20261128-a9fwnm0_ 下的 red.log、
targeted.log；定向命令使用 pytest 运行 test_segment_exclusive_inputs.py、
test_segment_inputs.py 和 test_batch_execution.py，覆盖 addopts 为 --asyncio-mode=auto。
真实 SDK Session、隔离内存存储、临时文件与 PlanNode 执行；模型和外部工具边界使用替身。

完整命令（PR2 工作区，PYTHONPATH 追加锁定 SDK 源码目录）：

```powershell
$env:PYTHONPATH=(Get-Location).Path+';E:/Jiuwenswarm-zqh/agent-core-pr1-sdk-20261127'
& E:/Jiuwenswarm-zqh/jiuwenswarm/.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_flash_turbo_discovery.py tests/unit_tests/agentserver/test_foreign_stream_source.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_resume_usage.py tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py -o addopts="--asyncio-mode=auto" -q -o log_cli=false --tb=short --show-capture=no
```

## 资料同步与限制

S_10 N10 明确合法假值与缺失的区别。批次静态检查未使用该真假值判断，仍由每段调用
唯一输入装配执行最终校验，因此无需另写批次修复或新增抽象。
未运行格式化、静态检查、真实模型或产品 UI 验收；未修改其他检视意见涉及的代码。
