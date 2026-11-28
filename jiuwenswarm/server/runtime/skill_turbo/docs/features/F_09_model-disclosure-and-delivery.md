# 请求级模型披露与交付归属

| 项 | 值 |
|---|---|
| 类型 / 编号 | feature / F_09 |
| 日期 | 2026-11-16 |
| 基线 | fix/ppt-segment-file-handoff；HEAD 0d8f41bc8d7d2af67f919c8164ff7612ec5a1a38，加上开始任务时已存在的前五步未提交改动 |
| 状态 | 授权范围实现与本地行为验证完成；Relay packages/api 消费适配经用户要求暂不应用；未提交、发布或真实模型/UI验收 |
| 关联规约 | [[S_12]]；[[S_02]]、[[S_08]]、[[S_09]]、[[S_11]] |

## 依据与范围

依据用户提供的《Skill多入口-第六步-模型描述与Relay结果消费执行方案》实施。
附件是设计参考，源码实施权限来自用户本轮请求。保留两仓前五步与其他既有未提交工作，
不恢复stash，不改动agent-core仓、不改运行配置。

用户明确：jiuwenswarm可改；Relay超出turbo的代码需审核。准备结果消费补丁后，用户要求
“待任务完成后细致讲解，暂不修改这部分代码”。因此当前上游实现与技能侧改动已落地，
Relay packages/api 保留原状，补丁、候选关联模块及隔离测试在其turbo/step6-review目录。

## 实际实现与决策

1. accelerator_renderer复用已解析AcceleratorSpec；SkillAcceleratorRail与请求可见SkillUseRail技能取交集。
   ToolInfo描述使用模型输入副本；共享模板和Flash懒加载导航不动态保存请求清单。
   目录按order、review、96字符/行、8条/技能分别渲染。隐藏/禁用清旧section。
2. 仅成功根skill_tool加载追加一次完整参数及进度附录；保留data元信息并同步模型ToolMessage。
   实际SDK普通技能加载可能没有data.content，使用其已渲染ToolMessage继续追加；只读current路径
   不open、不save。台账缺失与读失败分别披露，不把未知进度变成零进度。
3. 段技能取消关键词门槛；普通工具与段可交替，保留业务审阅、producer与文件前提。
   旧完整流程规则限定无声明legacy，PPT局部编辑不转generate，合法单页可export。
4. 交付总结由当前真实发送owner驱动，pending保留正文和任务/调用/文件依据。
   executor原产物投影只保留info/files/summary，新增delivery_evidence必须同步进入holder；
   真实四段测试检出了这个消费边界，补齐后验证摘要可进入上层工具包装通道。
5. export声明把外层todo文字提示从next_steps移到continue_hint，空next_steps表示四段结束。
   outer_work_remaining另行约束Flash，不能因有交付总结就结束未完成的外层待办。
6. 当前调用清pending，暂停、失败、中间段、already_done和历史去重不复用旧成功骨架。
   已发送后常规保存失败或后处理失败保留交付事实，失败业务状态不会提升为成功总结。
   已完成export的历史确认与当前文件路径/hash比较；合法改稿后保留原发送记录、当前确认false，零自动重发。
7. 使用已有事件的可选turbo_context/turbo_delivery传播归属；adapter重建工具/文本帧时保留。
   Flash finish_request保持请求级schema注入和执行前剥离，历史追加、待办与steering检查后才允许收尾。

## 与方案的工程差异

实际运行依赖是 `.venv/Lib/site-packages/openjiuwen`，版本0.1.16；与本地agent-core源码不同，
该版本尚不支持callback_priority。目录rail priority93同时使加载附录早于StreamEvent80；
新SDK可用hook级85。总结rail没有初始化副作用，直接设79以兼容实际包，SDK回调链测试验证
tool_result先于llm_output，不能用手工依次调用冒充顺序证据。

未开放第七步批量schema、force重跑或自动首段选择。不改SKILL.md正文和前端组件。

## 验证与限制

命令与完整日志最终归档在 `.agents/testing/skill-step6/`（按仓库规范忽略，不提交）。

- 开始实施前turbo模块：636 passed。
- 模型披露和交付主体回归：704 passed；后来扩展只读进度、控制异常和真实发送边界后再运行最终回归，见下方最终记录。
- 实际SDK与可控模型替身：Deep/Flash、懒加载开关、真实skill_tool根加载、模型实际tools/历史消息、callback排序。
- PPT与声明门禁的组合回归曾为130 passed；最终候选增加改稿历史确认测试后再次验证，见最终记录。
- Relay原provider PPT回归：2 passed。待审关联模块：17 passed，定向Biome检查通过。
- pnpm check执行记录：Biome检查9881文件、23356错误，第一项为未改动biome.json的CRLF差异；后续串行检查未执行。
- pnpm lint执行记录：green-package/web的Window.desktopPet与chatStore remote-origin类型错误；相关业务源码本次未修改。
- git diff --check通过；未运行jiuwenswarm格式化或静态检查。

上述是SDK、文件和受控外部边界验证；真实模型选择、产品UI、完整服务重启和Relay新消费者集成未运行。
当前不能宣称第六步产品全链路已通过。Relay缺证据不替换的门槛仍待用户审核后实际应用。

## 提交与交接

没有提交或创建PR。提交级gate未执行：HEAD尚未包含本地实现，不用base=head或零changed files作为验证。
如后续需要提交，先由用户确认范围，再按模块规定拆为特性代码、测试、文档三个连续提交，
将前五步既有未提交变更单独核对，不自动混入本步提交。

待审Relay解释文档：`E:/relay-claw-zqh/relay-claw/office-claw-skills/pptx-craft/turbo/step6-review/README.md`。

## 最终候选验证记录

| 验证 | 实际结果 | 日志 |
|---|---|---|
| turbo全模块、Flash真实模型输入、原工具、HITL与恢复、stream-source回归 | 787 passed，26.71s | .agents/testing/skill-step6/step6-final-confirmed.log |
| 声明门禁、PPT单段真实文件交接/回执、文件校验、注册门禁及历史改稿边界 | 131 passed，453.09s | .agents/testing/skill-step6/step6-ppt-final.log |
| 真实owner→holder→段结果→包装pending，以及改稿后已完成export零重发 | 包含于上表；定向20 passed | .agents/testing/skill-step6/step6-boundary.log |
| Relay待审关联候选 | 17 passed；定向Biome通过 | Relay turbo/step6-review/relay-candidate-tests.log |
| 原Relay provider PPT | 2 passed；消费者源码未应用补丁 | Relay turbo/step6-review/relay-legacy-tests.log |
| 两仓git diff --check / 待审patch适用性 | 通过 / git apply --check通过，仅检查未应用 | 命令结果；packages/api工作区无变动 |

最终引擎命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_flash_turbo_discovery.py tests/unit_tests/agentserver/test_foreign_stream_source.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_resume_usage.py tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py -o addopts="--asyncio-mode=auto" -q -o log_cli=false --tb=short
```

最终PPT组合命令在同一引擎工作目录运行，以当前仓库的测试环境引导和安装SDK验证：

```powershell
.venv\Scripts\python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/test_accelerator_manifest.py E:/relay-claw-zqh/relay-claw/office-claw-skills/pptx-craft/turbo/tests/test_segment_single_entries.py E:/relay-claw-zqh/relay-claw/office-claw-skills/pptx-craft/turbo/tests/test_segment_files.py E:/relay-claw-zqh/relay-claw/office-claw-skills/pptx-craft/turbo/tests/test_turbo_registration_gate.py E:/relay-claw-zqh/relay-claw/office-claw-skills/pptx-craft/turbo/tests/test_model_delivery_boundary.py -c pytest.ini -o addopts="--asyncio-mode=auto" -q --tb=short -o log_cli=false
```

保留的较早日志包含调试失败或被中断的尝试，不能代替本表最终候选结果。
未进行独立代码检视，不将实现者的检查称作交叉审核通过。
