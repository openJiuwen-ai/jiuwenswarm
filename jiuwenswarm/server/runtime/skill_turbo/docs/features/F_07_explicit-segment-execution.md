# 单段执行内核与恢复

| 项 | 值 |
|---|---|
| 类型 | feature |
| 日期 | 2026-11-27 |
| 范围 | 多入口拆分 PR1：内部单段执行、结果聚合、HITL 与恢复 |
| 状态 | 已迁移；本分支验证结果见下文 |
| 关联规约 | [[S_01]]、[[S_02]]、[[S_04]]、[[S_05]]、[[S_06]]、[[S_07]]、[[S_10]]、[[S_11]] |

## 范围与拆分依据

从 fix/ppt-segment-file-handoff 的 HEAD 903e1976a 及工作区候选拆分，基于上游 dev-stable 99dcf6114 迁移。原始候选与未提交文件独立备份，未改变原工作区。每个 PR 最多 50 文件、6000 行新增，因此按执行内核与公开集成分层；实现、测试与对应契约一起落地，不按已提交和未提交切分。

本 PR 提供声明、注册、台账、隔离状态、产物命名空间、输入输出检查及内部单段门面。公开 skill_acceleration_exec 仍保留 query/fresh_start，目录快照用于原描述刷新；公开 skill/accelerator/params、批次、自动组批、条件审阅、Deep/Flash 披露、阶段行隐藏和执行模式错误提示留给 PR2。保留内部批次控制数据接缝，避免改变后续恢复结构，但不引入批次模块或开放批量执行。

## 实现与决定

- run_accelerator_stream 精确选择技能和段，跳过 planner；run_skill_stream 精确构建旧根入口。目录缓存不能授权执行，执行前重新验证声明及源码。
- 首次任务身份、重跑失效和暂停断点须确认持久化；普通完成保存失败保留真实结果并报告告警。台账与产物桶使用同一隔离状态 owner，保留其他任务及 legacy 数据。
- SegmentContext 在序列化前捕获根返回并剥离内部 handoff，子节点产物保留真实 owner；root 成功不能覆盖未修复子失败。fallback 通过结果 holder 交回契约结果，再统一做后置验收。
- 恢复核对任务身份、原输入、调用 ID、task_states 和技能文件原始内容。已完成阶段跳过，连续 ask_user 保留原答案及序号；源码漂移拒绝恢复。
- can_pause=false 在段边界终止，禁止发卡和保留断点；取消继续透传。finally 按租约清理并回收注册工具，不用陈旧 Session 回写覆盖状态。
- 输入路径只取显式 metadata.output_dir；宿主 project_dir 补齐属于下一 PR。

## 验证与限制

2026-11-27，在本分支候选上使用 Python 3.13.14。原 venv 安装的 agent-core 为 2e2b6c7b，与新基线不匹配，首轮 4 个收集错误均为缺失 openjiuwen.rsi.harness_rsi.auto_harness；未为此修改业务代码或原 venv。随后从 uv.lock 精确提取 agent-core 228bd7da239d3919a0598d1ac5fc49ae9c0fb7cb，在独立只读源码 worktree 通过 PYTHONPATH 加载 SDK，当前 PR worktree 优先于原 editable 安装。其余依赖复用原 venv；不宣称重建了全部锁定依赖。

回归命令：

```powershell
$env:PYTHONPATH = (Get-Location).Path + ';E:/Jiuwenswarm-zqh/agent-core-pr1-sdk-20261127'
& E:/Jiuwenswarm-zqh/jiuwenswarm/.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agentserver/test_skill_turbo_resume_usage.py -q -o addopts="--asyncio-mode=auto" -o log_cli=false --tb=short --show-capture=no
```

结果：715 passed（29.56s）。之后补充拆分边界用例，对 test_accelerator_catalog.py 与 test_segment_execution.py 定向重验，29 passed（4.25s），包括真实 SDK 公开 schema 仍为 query/fresh_start，以及指定技能根入口不经过 planner。没有修改生产代码后沿用旧通过结果。

uv lock --check --offline 通过，328 个包解析一致。未运行格式化或静态检查；未进行独立代码检视。

日志位于本机临时目录 skill-turbo-pr1-20261127-trbzqzhe 下的 pr1-regression.log、pr1-locked-sdk-regression.log、pr1-boundary-tests.log 和 pr1-lock-check.log。历史候选的测试结果不作为本分支通过证据。

测试使用临时技能、临时文件及隔离内存或临时 SQLite 存储；外部模型、发送和 fallback 边界按各测试说明替换。未做真实模型、产品 UI、完整服务重启或技能侧四段流程验收。独立代码检视与用户验收不由本次行为测试替代。

## CodeCheck 后续修订

2026-11-27，按用户要求整理 PR 11161 的规范与模块边界问题，公开工具签名、恢复和存储协议保持。
本轮修复、验证及本地检查适用范围见 [B_09](../bugfix/B_09_codecheck-foundation-boundaries.md)。
