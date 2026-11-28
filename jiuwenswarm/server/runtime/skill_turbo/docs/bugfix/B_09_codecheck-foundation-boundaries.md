# 基础执行内核 CodeCheck 问题修复

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 日期 | 2026-11-27 |
| 候选基线 | PR 11161，437609c1b |
| 关联规约 | S_05、S_06、S_10、S_11 |

## 问题与依据

用户提供 CodeCheck 未解决导出，83 条问题分布于 10 个模块文件。行宽 36 条、空行 2 条、
受保护成员访问 22 条、条件过多 10 条、初始化外声明属性 5 条、字典取值 3 条、复杂推导式 2 条，
以及严格类型检查、忽略异常、父子异常重复捕获各 1 条。此次不扩展业务功能或引入屏蔽规则。

## 修复口径

- 手工换行、补空行并按输入类型、身份、断点内容等阶段拆开复合判断。检查仍先拒绝错误类型，
  再访问字段和计算指纹，保留原拒绝原因；字典读取在已有结构校验后使用 get，不补伪造默认值。
- 整数检查排除 bool，清单 enabled 继续只接受布尔值；UnicodeError 已被 ValueError 覆盖，
  去除重复捕获。目录恢复的二次异常补日志，仍清空描述并返回空目录。
- 单段编排显式接收 Environment、Executor 与 SegmentRunState。门面拥有状态对象，编排只写
  本次结果及台账引用；公开结果仍深拷贝，持久化结构和公开工具 schema 不变。后续 PR2 迁移
  应衔接该执行状态，不恢复对门面私有属性的反向读写。
- 外部扫描与执行前复核共用 validate_external_skill_code，保持专属 validator 和目录安全校验。
  external_scan_ok 在构造时声明；复杂事实和覆盖推导式改为显式循环。
- resume_context 集中适配 SDK 运行时 agent_id。锁定 SDK 的 get_agent_id 返回原始 card ID，
  不可用于判断隔离命名空间；新增真实 SDK 测试验证同 card 的主会话与 Turbo 状态不串写。
- Executor 提供 hash_plan_code，算法保持原 MD5 前八位，不改变存量产物/断点哈希。模块与旧
  中断恢复测试消费方一起迁移，不保留仅用于消除检查告警的重复包装。

## 验证

Python 3.13.14，复用原 venv 的依赖，PYTHONPATH 优先加载本 worktree 与 uv.lock 指定的
agent-core 228bd7da239d3919a0598d1ac5fc49ae9c0fb7cb 源码；未修改原 venv 或其他仓库源码。

新增目录恢复诊断测试先红：1 failed、18 deselected（2.94s），原因为二次错误无日志。
修复后的定向回归 99 passed（21.22s）。补充布尔/浮点清单版本、整数 enabled、损坏历史答案和
子状态、真实 SDK 隔离同步等回归后，完整命令如下：

```powershell
$env:PYTHONPATH = (Get-Location).Path + ';E:/Jiuwenswarm-zqh/agent-core-pr1-sdk-20261127'
& E:/Jiuwenswarm-zqh/jiuwenswarm/.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agentserver/test_skill_turbo_resume_usage.py -q -o addopts="--asyncio-mode=auto" -o log_cli=false --tb=short --show-capture=no
```

最终 728 passed（16.90s）。最后核对并保留原有短路求值顺序后完整重跑。首次扩展回归收集时发现旧 agentserver 测试还引用 _hash_code，
迁移该真实消费方后完整重跑；不将收集失败视为通过。

用户已要求修复 CodeCheck，因此本轮针对附件对应规则运行局部验证，不运行全仓格式化：
Pylint 选择行宽、条件数量、受保护访问、初始化外属性、类型判断和重复异常；Ruff 选择
E301/E302/E501。以 PR 基线 99dcf6114 到候选的新增行过滤后，所选规则告警为零。
未修改基线已有的 executor.py 与 skill_turbo_tools.py 长行；它们不在附件或本 PR 新增行范围。
复杂推导式、父子异常覆盖和字典前置校验另按源码逐项核对。没有增加 noqa 或禁用项目规则。

本地工具不是 GitCode 专有 CodeCheck 引擎，不能代替远端流水线通过结论。未做真实模型或 UI
验收，未进行独立代码检视。日志保存在本机临时目录 pr11161-codecheck-fix-yyz3ii14 中。
