# 显式单段执行与恢复

| 项 | 值 |
| --- | --- |
| 类型 | feature |
| 日期 | 2026-11-15 |
| 范围 | 第四步：工具模式、真实 PlanNode 单段执行、结果聚合、段 HITL 与恢复 |
| 状态 | 第四步本地实现及相关行为回归完成，未提交或发布 |
| 关联规约 | [[S_01]]、[[S_02]]、[[S_04]]、[[S_05]]、[[S_06]]、[[S_07]]、[[S_08]]、[[S_09]]、[[S_10]]、[[S_11]] |

## 依据与改动范围

按用户指定的 `E:/ZQH/11.16-11.27/skill多入口执行方案/Skill多入口-第四步-显式单段执行与恢复执行方案.md` 实施，文档作为设计依据，用户的仓库授权优先：jiuwenswarm 可修改，relay 仅允许 PPT turbo。

从当前 `fix/skill-accelerator-ledger` 创建 `fix/skill-accelerator-segment-execution`，保留前三步未提交内容；基线 HEAD 为 `0d8f41bc8d7d2af67f919c8164ff7612ec5a1a38`。实施前相关前三步测试 100 passed；原始工作区副本在 `%TEMP%/skill-segment-baseline-9d3hq9v4`。不把前三步的 manifest、ledger、产物存储、依赖/锁文件改动计为本步新增。

本步修改仅在 jiuwenswarm：SkillTurbo 及 executor、工具入口、resume/permission bridge、动态加载器、输入恢复校验、adapter 执行配置透传、行为测试和规约。relay、agent-core、运行配置均未修改；未读取或恢复 stash。PPT 四段业务、relay 宿主收尾、模型目录/加载态 rail、批次及 force 留给后续步骤。

## 实现与决定

- 工具保留 query/fresh_start 位置，新增 skill/accelerator/params；实际 SDK schema 拒绝顶层 force/accelerators。准确普通段无 planner；skill-only 返回候选；__full__ 对指定技能 build_plan_code，沿用整流程收尾。
- segment_execution 只编排精确选择、租约、实际执行与保存；任务身份/重跑失效、输入/事实/文件验证仍由第三步唯一 owner 完成。当前声明及源码无缓存复核；首次身份和重试失效须确认保存后才能执行，常规结果保存失败保留真实结果并报告 persisted=false。
- SegmentContext 为本次实例状态，序列化前捕获根结果并剥离 handoff；子树流式产物按真实节点收集。root ok 不能覆盖未修复子失败/partial。fallback handler 通过可选 holder 交回已通过原契约检查的 completed 结果，段上下文解除已修复子树标记，再验收文件；不重写 PlanNode 模板。
- 普通段不附整任务停止提示；交付仅从实际 send_file_status=sent 的 owner 派生，不硬编码 p5/p10。已完成且文件/事实仍可信时跳过，不产生新 run 或声称重新发送。
- 恢复先核对身份、调用参数、入口、原 inputs/task_states/tcid 完整性及当前内容快照。原输入只重新验证，不重新装配；沿用原 tcid 公式及 name+idx 回退，复用已完成阶段与同 namespace 产物。连续卡片记录已回答 ask_user 的原参数、节点、序号和结果，先重放旧答案再消费当前 pending。
- 快照覆盖技能根中声明及技能文件的相对路径/原始字节，排除 `.git/__pycache__/.pytest_cache/.venv/node_modules/skill_runs`；新增、删除、重命名、文案、注释及同 stat 修改均会失效。不可读来源和越界/目录链接拒绝。未采集技能根之外或排除的第三方依赖，不宣称覆盖这些依赖。正常工作产物位于宿主工作区，不纳入执行定义。
- SDK 将字典键中的 `.` 解释为状态路径，因此文件路径和节点状态以列表记录的值保存。段断点经隔离通道整体替换并确认；重复读取也使用独立快照，避免 Session 重复 pre_run 保留旧状态。legacy 保存显式清 segment；in-flight 清理遵循真实 merge 语义。暂停前先确认本 namespace 产物保存，再保存断点；未确认不得发恢复卡片。原模型身份无法恢复时清本次匹配断点并返回失败。
- can_pause 默认 true，正常 AbortError 沿原 marker/card 路径；false 在 use_tool 保存和事件转发前分流，返回固定失败信息，清本次下层已写断点，禁止 fallback 继续。正常终态/取消清本次断点；只有定义与身份均验证通过、尚未执行业务的瞬时工具注册失败可保留可复用断点。源码/加载读取失败明确排除此豁免。
- Deep/Flash build_skill_turbo_config 共用执行配置深拷贝；授权仍在原外层统一审批，未增加内层重复审批，也未移除业务 ask_user。
- 拒绝并发作答后的清理同样取得执行租约，避免 owner 尚未标 in-flight 时被重复请求误清。存储故障导致终态清理失败时回带 resume_cleanup_failed、保留真实业务结果并继续工具回收；残留 in-flight 继续挡重复业务，不伪称已清。

## 行为证据

使用临时真实外部技能、真实 PlanNode/Executor/ToolCard.invoke、SDK InMemoryCheckpointer 与真实临时磁盘。模型/fallback spawn、文件/发送工具作为外部边界替换；额外使用真实 Runner.resource_mgr 验证工具注册和回收。没有连接生产存储或外部发送服务。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_resume_usage.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py tests/unit_tests/agentserver/test_flash_turbo_discovery.py -o addopts="--asyncio-mode=auto" -q -o log_cli=false --tb=short --show-capture=no
git diff --check
```

最终结果：**742 passed in 73.96s**；完整日志 `%TEMP%/skill-step4-full-tests.log`，即 `C:/Users/13653/AppData/Local/Temp/skill-step4-full-tests.log`。`git diff --check` 通过。按 jw-dev 未运行格式化或静态检查。

此前边界测试确实暴露并推动修复了 SDK 点号键序列化、重复 Session 读旧断点、连续 ask_user 错配旧答案、fallback completed 结果识别，以及原恢复尚未标 in-flight 时被 busy 请求误清的问题。并非仅运行成功路径。最终版本额外覆盖结果保存及断点清理同时失败：实际结果仍返回，明确 persisted=false 和清理 warning，残留 busy 槽拒绝重复业务。

| 设计矩阵 | 证据入口 |
| --- | --- |
| E01–E03、E05 | test_segment_execution、test_segment_execution_boundaries：准确选择、root/子状态、无 handoff/无 status、文件后置、真实 fallback 契约通过/失败、校验/加载失败、真实发送 owner |
| E04、E07–E09 | test_segment_inputs、test_segment_state、test_segment_tool_boundary、test_skill_run_ledger：唯一装配/producer、稳定身份、SDK 未实现参数、已完成跳过、两类启动前保存失败、常规保存 fail-open、禁用/当前源码复核 |
| E06、C01 | test_segment_tool_boundary、原 agentserver 工具回归、真实资源管理器用例：外层 todo 抑制、legacy/__full__、配置深拷贝、成功/失败/暂停/取消回收 |
| H01–H03、H05、H07–H10 | test_segment_hitl、test_segment_execution_boundaries：单卡/连续卡/二次中断、原阶段短路、同会话并发与重复作答、独立会话、原输入/任务状态/tcid 损坏、merge、版本/内容集合漂移、同 stat 旧 pyc、工作文件修改、namespace/hash、不可靠源码拒绝 |
| H04、H06 | 暂停后取消并独立读文件/台账；工具注册瞬时失败保留原答案重试；新调用不能覆盖 pending；can_pause=false 真实 ask_user、下层断点和流式卡片事件被拒绝/清理 |
| H11 与既有完整矩阵 | 原 permission/ask_user、agentserver HITL resume、TIE rewrite、interrupt recovery、resume usage、prompt disabled、Flash discovery 用例一并运行；产品授权/卡片 UI 不由 invoke 级测试替代 |

跨会话测试使用宿主各自的输出目录；同工作区、同 query/sequence 的任务身份按第三步公式可相同，本步不增加跨会话共享磁盘写锁或更改身份公式。

## 限制与后续

本地测试证明 SDK 工具边界、事件、临时文件及隔离状态的行为；未运行真实模型、浏览器/产品 UI、真实发送服务或完整服务重启，也未将进程内 checkpointer 结果冒充重启恢复证据。现有 adapter/harness 的取消、补充、嵌套中断路径通过原回归测试；未执行产品中人工取消/补充的端到端操作。

relay 保持原 `dev-stable` HEAD `f4a75a3624627d8cdc22478ecde16f7ae4877115` 及已有未跟踪 PPT turbo。第五步可基于本步接 PPT 业务；第六步再做描述/宿主与最终收尾，第七步开放批次和步骤内 force。代码尚未 commit/push/发布。
