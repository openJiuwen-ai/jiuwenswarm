# 同技能批量执行与原段断点恢复

## 元信息

| 项 | 值 |
|---|---|
| 类型 / 编号 | feature / F_10 |
| 日期 | 2026-11-16 |
| 范围 | jiuwenswarm SkillTurbo 批入口、静态输入校验、逐段Ledger、模型披露；Relay PPT turbo测试 |
| 当前规约 | [[S_13]]；关联 [[S_06]]、[[S_10]]、[[S_11]]、[[S_12]] |
| 设计依据 | E:/ZQH/11.16-11.27/skill多入口执行方案/Skill多入口-第七步-批量执行与断点恢复执行方案.md |
| 状态 | 当前分支本地实现；未提交、未独立检视、未产品端到端验收 |

## 背景与承接范围

前六步已有显式单段、台账、PPT四段文件交接和模型披露。本步将确定的同技能阶段序列放入一次工具调用，
复用各段原执行与恢复owner，减少外层模型在固定阶段间的往返。
F_09的历史测试记录继续保留；本记录新增批编排，不回写旧验证结果。
用户已排除Relay结果消费个例，因此此前packages/api候选补丁不应用，也不再作为本任务待办。

实施前核对两仓均在 `fix/ppt-segment-file-handoff`，jiuwenswarm HEAD为
`0d8f41bc8d7d2af67f919c8164ff7612ec5a1a38`，Relay HEAD为
`f4a75a3624627d8cdc22478ecde16f7ae4877115`。两仓已有大量前六步未提交修改，
Relay整个turbo目录仍未跟踪，不能用相对HEAD的全部差异冒充本步改动。
本步前文件及status备份保存在 `C:/Users/13653/AppData/Local/Temp/skill-step7-baseline-q6cuprb_/`，
用于分离本步差异；不恢复或删除既有用户文件。

## 实际实现与取舍

1. `batch.py`增加BatchStep/BatchPlan、只读build_batch_plan及run_batch_stream；工具签名末尾追加accelerators。
   保留query-only、__full__和原单段路径，不增加顶层force或跨技能调度。
2. 静态检查整批shape、order、独占、共享值及步骤参数、schema、前提和当前源码。
   缺失required仅能由合法前序声明解释；实际段前仍由原严格装配检查真实facts和文件。
   不采用伪造未来文件路径填required的方式，避免静态检查掩盖运行时缺失。
3. 父批持原会话/card租约，每段新SkillTurbo/Executor，所有段共享唯一Ledger任务。
   逐段record/save；工作快照保留常规保存失败时真实产生的事实和来源，不靠整批末尾一次保存。
   首次任务身份及重跑前失效必须可靠确认，失败则本段零业务。
4. latest_runs检查已完成步骤，当前产物不足时blocked并提示修复/force，不偷偷重跑。
   force位于步骤内，先确认失效；记录consumed_facts和params，后续跳过旧下游时披露revision变更，
   不自动重算下游或重发文件。独占段按原列表长度判断，允许独占单元素force。
5. 断点沿用resume_ctx.segment.batch纯数据控制，存原调用、cursor、前缀、工作Ledger及保存风险。
   恢复前核对整个技能内容快照和声明，原段inputs/task_states仍由原恢复链持有。
   新Session及持久Checkpointer重建后可恢复原游标，不重复完成前缀或当前已完成叶子。
6. review四种组合仅双条件允许才继续；clarify恢复后遵循continue_after_resume。
   failed/blocked/not_applicable保留前缀停批；partial按逐事实verified继续并保持partial。
7. 子执行器相同工具参数的TCID在原request哈希域加入batch_id/cursor，防止不同段调用碰撞；
   保留原格式、解析器和外层request路由，不创建新卡片/事件协议。
8. renderer与请求工具副本同步批量指南、独占/force/审阅规则；batch_enabled=false时实际模型schema移除accelerators。
   Deep/Flash和懒加载开关均从真实SDK模型入口捕获消息及tools验证，不仅比对中间字符串。
9. 聚合保留实际发送依据，最终delivery_confirmed只属于本批新增、正常完成且无余段的交付。
   全跳过或历史回执不产生新总结；原步骤后续技能提示仍保留。

## Relay范围及测试适配

相对本步前备份，Relay只修改turbo/tests/conftest.py及test_segment_batch_entries.py。
PPT四段生产代码沿用前五/六步实现；packages/api、packages/web和此前step6-review内容均未修改。

既有潜在批量测试使用的fixture需要按实际宿主todo定位任务，批内创建新子实例并在外部工具边界注入替身。
对照四次单段的测试改用新的宿主todo，而非误把fresh_start当作换任务信号。
原有两个批量用例被实际运行，并补充已完成去重、上游force不自动重算下游、不重复投递、
发送结果未知时force不得重发、缺页/合并失败不部分发送、显式页面来源不污染历史manifest等用例。
保留既有测试文件名，避免为了匹配方案示例名而移动测试。

## 开发中定位与修正

最终回归曾暴露新类型校验与已有断点格式不兼容：ResumeContextManager.save显式写
resume_in_flight=None，批入口误要求必须是bool，导致11个恢复测试和1个PPT审阅恢复用例失败。
按原owner契约接受None/缺失/false为未占用，true返回busy，拒绝字符串/数字，
并在正常恢复用例中明确断言原断点None，在损坏控制用例中加入字符串false负例。
修正后恢复/批量定向80项通过，再执行完整引擎与PPT组合回归。
较早日志还保留fixture注入、断言格式和schema预期同步过程中的失败，不将其计为最终通过证据。

## 验证与适用范围

使用引擎既有`.venv/Scripts/python.exe`及安装的openjiuwen 0.1.16，未修改SDK或相邻agent-core。
外部模型、发送和转换边界为受控替身；PlanNode/SDK Session/持久化路径、临时文件、声明校验和产物检查真实运行。
文件及持久Checkpointer均使用测试临时目录，未读取或清理业务持久存储。

| 验证 | 结果 | 证据（相对引擎仓） |
|---|---|---|
| 完整turbo模块、Deep/Flash披露、工具/HITL/恢复及stream-source | 872 passed，80.00s | .agents/testing/skill-step7/engine-verified.log |
| PPT单段/批量、文件、声明、注册、真实交付边界组合 | 138 passed，615.76s | .agents/testing/skill-step7/ppt-verified.log |
| Relay pnpm lint | 失败：未修改的green-package/web缺virtual:site-scenarios、Window.desktopPet及chatStore remote-origin类型 | .agents/testing/skill-step7/relay-lint.log |
| Relay格式基线复核 | 未修改biome.json仍报CRLF格式差异；未重复第六步约800秒的全仓pnpm check | .agents/testing/skill-step7/relay-format-baseline.log |
| git diff --check | 两仓通过；uv.lock仅有既有LF/CRLF提示 | 命令输出 |

完整引擎命令（在引擎仓）：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_flash_turbo_discovery.py tests/unit_tests/agentserver/test_foreign_stream_source.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_resume_usage.py tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py -o addopts="--asyncio-mode=auto" -q -o log_cli=false --tb=short
```

PPT组合命令（同一引擎仓，日志置于技能根之外，避免验证日志改变执行定义快照）：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/test_accelerator_manifest.py E:/relay-claw-zqh/relay-claw/office-claw-skills/pptx-craft/turbo/tests/test_segment_single_entries.py E:/relay-claw-zqh/relay-claw/office-claw-skills/pptx-craft/turbo/tests/test_segment_files.py E:/relay-claw-zqh/relay-claw/office-claw-skills/pptx-craft/turbo/tests/test_turbo_registration_gate.py E:/relay-claw-zqh/relay-claw/office-claw-skills/pptx-craft/turbo/tests/test_model_delivery_boundary.py E:/relay-claw-zqh/relay-claw/office-claw-skills/pptx-craft/turbo/tests/test_segment_batch_entries.py -c pytest.ini -o addopts="--asyncio-mode=auto" -q -o log_cli=false --tb=short
```

第六步完整pnpm check已有9881文件、23356错误的基线记录，详见F_09；本步仅复核未修改根配置的格式失败。
因此不宣称Relay整仓check/lint通过，未为修复既有错误越界修改业务源码。
提交级gate仅支持commit base/head；当前本地实现未进入HEAD，不使用base=head的零差异结果冒充本步验证。
未运行引擎格式化或静态检查；未自动提交、推送、建PR或启动业务服务。

## 交接与重验条件

实现者已核对源码路径及实际行为证据，尚未进行独立代码检视，不称为交叉审核通过。
真实模型选择、产品UI卡片展示、完整服务进程重启、生产发送及token/耗时收益留待第八步验收。
持久Checkpointer重建测试仅证明SDK存储状态可加载，不能替代产品进程重启验收。
Relay被排除的消费门槛不计入本步完成范围，也不再请求用户批准。

批执行、声明、输入装配、resume格式、SDK版本或PPT fixture/源文件发生变化时，应重验受影响矩阵。
后续若需要提交，先核对既有六步未提交差异，再按模块约定组织特性、测试、文档连续提交并运行实际提交范围gate。


## PR2 拆分与集成验证（2026-11-28）

PR1（11161）负责声明解析、台账、输入装配和内部单段执行；本次 PR2 承接公开工具的
skill/accelerator/params/accelerators/auto_batch 模式、请求级模型披露、同技能串行批次、
审阅恢复、交付归属和内部阶段行隐藏。query/fresh_start 的 legacy 调用仍保留。
原工作区的已提交与未提交实现一并迁移，原工作区内容不变。

新分支从上游最新 dev-stable 创建，并合入 PR1 原提交作为依赖；PR1 尚未进入目标分支时，
直接比较 dev-stable 会包含两部分。须先合入 PR1，再创建面向 dev-stable 的 PR2；若 PR1
以 squash 方式合入，应先调整依赖历史并重新核算实际 diff，不把累计差异当作 PR2 的规模。
按仓库约定，将 PR2 实现、测试、文档组织为三个连续提交，不创建远端 PR。

迁移保留 PR1 的 CodeCheck 修复、公开环境校验接口、段状态对象、产物 holder 和
会话 scope 自动回收。批次入口显式接收环境与段状态，避免重新引入旧的私有结果字段。
PR1 的自动回收实现及 12 项生命周期测试未改动。旧稿 B_08/B_09 故障记录与上游、PR1
已有编号冲突，迁入时顺延为 B_11/B_12，并更新引用，不覆盖已有记录。

集成验证使用 Python 3.13.14、锁文件指定的 agent-core 228bd7da2 源码和原虚拟环境其他依赖。
完整 SkillTurbo 模块、Flash 披露、stream source、旧工具和 HITL/恢复回归共 1025 passed
（55.13s），日志为本机临时目录 skill-turbo-pr2-20261128-f9k8jy56/regression-final.log。
命令沿用上文完整引擎命令，额外指定源码 PYTHONPATH 和 --show-capture=no。
测试使用临时文件及隔离存储，模型、发送等外部边界使用替身；未做真实模型、产品 UI、
完整服务重启或长期性能压测，未运行格式化、静态检查或独立代码检视。

PPT 消费方回归：真实本地技能注册门禁和自动组批共 13 passed（202.65s）。通过公开工具
执行自动四段、显式单段、条件审阅暂停/恢复、六页产物及单次发送断言；真实执行 PowerShell、
SVG 转换和 PPTX 文件读取，模型和发送边界使用替身。命令为同一引擎环境的 pytest 加载
Relay 技能 turbo/tests/test_turbo_registration_gate.py 和 test_segment_auto_batches.py，
指定 -c pytest.ini -o addopts="--asyncio-mode=auto" -q -o log_cli=false --tb=short --show-capture=no。
日志为同一临时目录的 ppt-consumer.log；该结果依赖当前本地技能源码，不代表其已在上游发布。
