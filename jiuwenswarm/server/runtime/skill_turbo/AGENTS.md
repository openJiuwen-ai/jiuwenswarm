---
description: SkillTurbo 技能加速引擎模块说明书——给修改/检视本模块代码的 AI 助手与工程师：公开入口、模块地图、架构铁律、执行路径与边界 case 的 spec 索引、测试与提交约束。
---

# SkillTurbo

技能加速引擎：把"技能执行"从外层 LLM 的多轮 ReAct 循环，替换为**预编排的 PlanNode 节点树流式执行**。外层 DeepAgent LLM 调 `skill_acceleration_exec` 工具进入加速通道 → planner 路由到已注册技能（如 pptx-craft）→ executor 在受限命名空间加载技能节点树 → 节点经回调使用工具/LLM/ask_user → 流式 chunk 转发回父会话直至交付。技能侧加速代码（turbo code）由独立的**技能侧仓**（skills sidecar）持有，经共享技能目录注入运行时，引擎零技能名硬编码。

本文件是模块入口索引。**修改任何代码前先读对应 spec**（见「设计文档归档与双向同步」）；深入细节跳转：

- `docs/specs/S_01_execution-pipeline.md` — 主链路与执行管线（执行循环 / task_states / 产物 / 退出路径 E1-E13 全景）
- `docs/specs/S_02_skill-routing-and-registration.md` — 技能路由与注册（发现 / 扫描 / meta.json / 入口链 / planner 路由 / 入口工具 / 工具装载）
- `docs/specs/S_03_security-model.md` — 安全模型（validator 三 profile / 运行期沙箱 / 审批收口）
- `docs/specs/S_04_node-contract.md` — PlanNode 节点契约（11 条契约 / 12 回调 / IO 约定 / 异常分流 / LLM 调用）
- `docs/specs/S_05_dynamic-package-and-facade.md` — 动态包与门面（包注册 / skill_turbo_runtime / tool_utils 工具契约）
- `docs/specs/S_06_hitl-interrupt-resume.md` — HITL 中断-恢复状态机（tcid / 发卡 / 恢复 / 取消 / 持久化——**缺陷密度最高领域**）
- `docs/specs/S_07_fallback-and-degradation.md` — 降级体系（4 层决策 / fallback 子代理 / 契约判定 / 回写）
- `docs/specs/S_08_event-stream-protocol.md` — 事件流前端协议（事件全集 / 顺序约束 / 缓冲 / 转发 / 交付总结时序）
- `docs/specs/S_09_outer-integration.md` — 外层集成面（adapter 装配 / ContextVar 桥 / WS-gateway / 双仓部署）

## 统一术语

```
外层（outer）  外层 DeepAgent ReAct 循环（interface_deep adapter 所在层）
└── 加速通道    skill_acceleration_exec 工具调用进入的 SkillTurbo 执行世界
    └── 内层（inner）  turbo 节点树的执行上下文（executor / 内层 rails）
```

| 术语 | 含义 |
|---|---|
| **skill_code / turbo code** | 技能侧加速代码（技能侧仓，`<shared_skills_root>/<skill>/turbo/turbo_codes/<skill_name>/`），PlanNode 子类组成，自包含（零 `jiuwenswarm.*` import） |
| **技能侧仓（skills sidecar）** | 持有外部 turbo 技能源码的独立仓库 / 部署单元；其技能目录经共享技能根目录送达引擎运行时（当前部署形态对照见 [[S_09]] 附录，非正典契约） |
| **共享技能根目录（shared skills root）** | `JIUWENSWARM_SHARED_SKILLS_DIRS` 指向的目录，部署侧注入；`<shared_skills_root>/<skill>/turbo/...` 即技能加速面的发现路径 |
| **上层前端 / 上层应用** | 事件流与作答路由的下游消费方（Gateway 之后的链路：气泡 / taskProgress 槽位 / taskStack / ask_user 卡片路由） |
| **plan_code** | `from {module} import root` 形态的可执行入口字符串；随 HITL 断点持久化，resume 时重新加载 |
| **PlanNode / Stage** | 节点 = skill_code 的执行单元（group 编排 / leaf 执行）；**只有 depth==1 的 subplan 对应前端 Stage**（参与 task 事件与 resume 跳过判定），root 为 depth 0 |
| **plan_name / display_name** | plan_name = skill 内稳定标识（日志/trace/产物 key）；display_name = root 的 `display_names` 类属性映射出的界面名（task/llm 事件用） |
| **tcid（tool_call_id）** | `skill_turbo-tc-{tool_name}-{args_hash}-{idx}`；args_hash = `sha1` 对 `request_id` 与 canonical_args 以竖线拼接后取前 8 位 hex；HITL 重放的对齐锚（全公式见 [[S_06]]） |
| **断点 / resume_ctx** | `__skill_turbo_resume_ctx__`（checkpointer 隔离键 `{card.id}__skill_turbo` 下）：plan_code + inputs + pending_tool_call_id + task_states 快照 |
| **HITL / TIE / AbortError** | HITL = ask_user/权限类人机中断；TIE = ToolInterruptException；AbortError = 中断信号异常（**全链不吞**，承载 TIE 时触发暂停） |
| **发卡** | 向前端发 `chat.ask_user_question` 卡片；单路 owner（harness `__interaction__` 转换 / resume 内 `_emit_skill_turbo_hitl_chunks`），request_id 恒为外层 call id 域 |
| **门面（skill_turbo_runtime）** | 外部 turbo code 与引擎之间的唯一接口边界（sys.modules 内存注册，磁盘不存在）；契约符号纯 re-export |
| **动态包（skill_turbo_codes_{skill}）** | turbo_codes 目录的 sys.modules 注册形态（`__path__` 锚点，不进 sys.path） |
| **fallback / 降级** | 三级概念勿混：**fallback** = 节点级子代理兜底（可恢复）；**FallbackContractError** = 终止性降级信号（不进节点 fallback，直达降级出口）；**SkillTurboNotHandled** = 对外层 LLM 的降级信号（tool 返回 success=false，由 LLM 转 skill_tool 标准流程） |
| **E1-E13** | 退出路径统一编号（定义见 [[S_01]]），跨 spec 引用 |
| **adapter** | interface_deep.py 的 DeepAdapter——skill_turbo 与外层会话/事件流的集成层 |

## 公开入口（public API）

公开符号仅限 `__init__.py` 的 lazy export（13 个）：

| 入口 | 用途 |
|---|---|
| `SkillTurbo(config)` | 组装门面：Environment → Planner → Executor；`run_stream` / `resume_stream` 是运行入口，`SkillTurboNotHandled` 是降级信号 |
| `SkillTurboExecutor` | 执行引擎（一般不直接使用，经 SkillTurbo） |
| `SkillTurboPlanner` / `SkillTurboEnvironment` / `PlanNode` / `PlanCodeValidator` | 规划 / 环境 / 节点基类 / 校验器 |
| `SkillTurboEvolver` / `PlanExecution` / `ExecutionTrace` | 执行轨迹记录——**预留未接线**（`_send_trace` 空壳，生产零调用；勿假设其工作） |
| `PlanCodeValidationError` / `PlanCodeLoadError` / `PlanGenerationError` | 异常（`PlanGenerationError` 当前无 raise 点，plan 失败统一返回 None） |
| `Skill` | 技能数据模型（environment.py） |

**真正的生产入口是工具**：`skill_turbo_tools.skill_turbo`（`@tool(name="skill_acceleration_exec")`），由
interface_deep 在 `config.react.skill_turbo.enabled=true` 时注册进外层 ability_manager。配置项
消费关系见 [[S_09]]（含 `react.disabled_tools`、`tool_lazy_load.enable_for_models`、
`execution_guard.llm_retry_rail`、`JIUWENSWARM_SHARED_SKILLS_DIRS`）。

**新增配置项走 config dict + adapter `build_skill_turbo_config` 注入**，不在模块内读全局配置。

## 模块地图

```
skill_turbo/
├── __init__.py             # 公开 API lazy export（13 符号）
├── agent.py                # SkillTurbo 门面：planner→executor 组装、run/run_stream/resume_stream、异常→NotHandled 分流
├── planner.py              # LLM 流式路由（置信度 0.6、解析失败重试 1 次）→ plan_code 组装（入口 5 级链）
├── executor.py             # 执行引擎核心（~3800 行）：执行循环 / 事件发射 / task_states / HITL 断点 / use_tool / LLM 调用 / 降级入口
├── plan_node.py            # PlanNode 基类（11 条契约）+ AbortError/FallbackContractError re-export（全链唯一符号源）
├── environment.py          # 技能发现/注册（三路径，外部覆盖内置）/ 工具装载 / Skill 模型 / skill_root·路径解析
├── skill_turbo_tools.py    # skill_acceleration_exec 封装：入口拦截 / resume / 事件转发映射 / 收尾诚实性 / 模块级 ContextVar 协议
├── fallback_handler.py     # 节点级 fallback 子代理（spawn 隔离/权限挂起）+ 契约双层判定 + FallbackContractError
├── permission_bridge.py    # SkillTurbo↔权限/中断 rail 纯函数胶水 + resume_ctx 薄委托（单一 owner：resume_context）
├── resume_context.py       # ResumeContextManager：断点 save/load/clear/mark（隔离键、merge 语义、isolated 通道）
├── interactive_ask.py      # 引导模式（interactive_ask）判定与 resume 恢复
├── node_artifact_store.py  # 节点产物 session 持久化（只存摘要/路径；plan_code_hash 门禁继承）
├── context_binding.py      # 7 通道 ContextVar 统一 bind/reset（新增通道=bind 一段+RESETTERS 一行）
├── evolver.py              # 执行轨迹（预留未接线）
├── json_utils.py           # extract_llm_json 四级提取（无 LLM 级重试，重试归调用方/rail）
├── markdown_stream.py      # 流式 markdown 围栏粘连/未闭合保护
├── region_edit.py          # 选区/编辑关键词（临时模块；PromptRail 与入口拦截同一词源）
├── skill_meta.py           # meta.json 解析 + find_skill_root_file（入口发现唯一实现，三消费方共用）
├── thinking_seam.py        # thinking 参数 fail-open 注入 + 参数被拒启发式判定（裸参重试一次）
├── tools_loader.py         # 10 分组工具装载（异常隔离）+ send_file 每请求刷新
├── turbo_package_loader.py # 外部 turbo 发现 + 动态包注册/清洗碰撞/rebind + resume 自愈重注册
├── validator.py            # PlanCodeValidator 三 profile（import 白黑名单/危险函数/require_abort_reraise 静态强制）
├── rails/                  # ask_user_rail（HITL）/ artifact_rail（产物检测+事件）/ delivery_summary_rail（交付骨架时序）/ permission_rail（审批卡）/ skill_prompt_rail（指南注入）
├── runtime/                # skill_turbo_runtime 门面（契约符号 re-export + CONTRACT_VERSION）+ tool_utils（通用工具单副本）
└── docs/                   # 设计文档归档（specs/ 9 份 + 归档规约 AGENTS.md）
```

## 架构铁律

以下 12 条从各 spec 不变量中提炼（全量不变量以 spec 为准，条目可被 review 单条引用）：

1. **AbortError 全链不吞**（[[S_06]] N2/N3）。引擎侧 **14 处守卫点**一律透传——plan_node 4
   （run / run_stream / execute_subplan / execute_subplan_stream）、executor 7
   （execute_plan_stream / _run_rail_hook / use_tool 两层 / call_llm / stream_llm /
   _execute_node_stream 消费循环）、agent 3（run / run_stream / resume_stream），穷尽全表见
   [[S_06]] P1 步 6；validator `require_abort_reraise` 静态强制技能代码宽泛 except 前置
   守卫、门面纯 re-export 保证类型同一——三道防线缺一即 HITL 失效（中断被吞 / 用户中断被
   降级成契约失败）。**新增任何宽泛 except 必须先答"AbortError 怎么过"**。
2. **AbortError / FallbackContractError 单一符号源**（[[S_05]]）。两者仅从 `plan_node` re-export
   （源头 openjiuwen / fallback_handler），禁止任何环节二次定义或绕过 import——isinstance/except
   判定依赖同一类对象。
3. **HITL 断点清理完备性**（[[S_06]] N1）。任何退出路径（approve / resume 成功 / 断点不可复用 /
   cancel / supplement）必须清理持久化断点；**瞬时失败是唯一合法保留场景**（且不 post_run）；
   cancel 必须**先终止执行、后清状态**（反序会被 executor 退出路径的落盘覆盖）。
4. **发卡单路 owner + 外层 call id 口径**（[[S_06]] N8）。ask_user 卡片只有两个合法产出源
   （harness `__interaction__` 转换 / resume 内 `_emit_skill_turbo_hitl_chunks`），request_id 恒为
   外层 skill_acceleration_exec call id（或其 `#n` 变体），必经 `_dedupe_ask_user_card`。
5. **tcid 确定性**（[[S_06]] N4）。`skill_turbo-tc-{name}-{args_hash}-{idx}`（args_hash =
   `sha1` 对 `request_id` 与 canonical_args 以竖线拼接后取前 8 位 hex）；
   request_id 混入使 **resume 精确匹配必然失效，name+idx 回退命中是预期路径**。改 tcid 格式必须
   同步两个解析函数并重新评估回退命中链。
6. **退出路径事件终态收口对称**（[[S_01]] E1-E13 / [[S_08]]）。改任何异常分支前对照 E 表：
   暂停态禁 chat.final（AbortError 分支零终态帧）；异常分支先 flush/drain 全部 task 生命周期事件
   再 raise（CancelledError 是唯一例外）；新增退出路径必须明确归属哪条 E 的语义。
7. **降级判定 fail-closed**（[[S_07]]）。fallback 成功判定 = 子代理契约自证 + 节点级
   `validate_fallback_success` 双层，空产出/无契约块/validator 拒绝/validator 自身异常一律失败，
   唯一白名单是 AbortError 重抛；契约结果必须回写共享 inputs；降级终止路径不发 chat.error
   （防前端抢跑终结，LLM 来不及转标准流程）。
8. **工具失败信号双轨兼容**（[[S_05]]）。工具层失败两轨并存（抛异常 / `success=False` 结构化
   结果），消费点必须双轨判定；禁止 `str(ToolOutput)` 兜底进业务流；读失败与合法空值必须可区分。
9. **持久化状态写入规则**（[[S_06]] / [[S_01]]）。skill_turbo 持久化一律走 `{card.id}__skill_turbo`
   隔离键 + ResumeContextManager / node_artifact_store 单一 owner（禁手写字面量键）；save 显式写
   全量键（merge 语义下删键=显式写 None）；clear 走 isolated session 通道 + 同步清调用方内存快照。
10. **并发安全三则**（[[S_01]]）。每请求新建 SkillTurbo/Executor 实例；`_prepare_root_node` 对
    模块级 root 单例 **deepcopy 后再绑回调**；工具按全局 tool_id 从 `Runner.resource_mgr` 现取
    （后注册覆盖前者是已知行为），带 session 路由的工具（send_file）必须走请求级 ContextVar。
    async 协程内同步 IO/CPU 一律 to_thread + 信号量限流（禁裸同步调用阻塞事件循环）。
11. **路径唯一 owner**（[[S_09]]）。`inputs["workspace_base"]` 只来自 `request_metadata["output_dir"]`
    （非空才设置），禁止任何 agent workspace 兜底——工具层不得对路径类入参"猜值"。
12. **技能侧与引擎侧互不越界**（[[S_05]] / 技能侧仓 TURBO_CODE_SPEC）。turbo code 零
    `jiuwenswarm.*` import，一切引擎能力经 `skill_turbo_runtime` 门面；引擎侧新增门面符号属契约
    变更（递增 CONTRACT_VERSION + 同步 validator 白名单）；改 turbo code 后必跑技能侧仓注册
    门禁测试（compileall 不能发现安全策略拒绝）。

## 生命周期 / 配置消费

- **启用**：`config.yaml react.skill_turbo.enabled`（false 时工具不注册、rail 不挂载）；
  `react.disabled_tools` 含 `skill_acceleration_exec` 时 rail 仍建但不注入指南。
- **请求级**：每次 `skill_acceleration_exec` 调用新建实例；请求上下文经模块级 ContextVar 传递
  （`_current_request_metadata` / `_skill_turbo_resume_answers` 等，见 [[S_09]] 7 通道表）。
- **跨请求**：HITL 断点 / 节点产物经 checkpointer 持久化（隔离键）；类级扫描缓存
  （`_scan_cache` / `_external_scan_cache`）跨实例共享、按 mtime 失效。
- **部署顺序**：① agent-core → ② 技能源（技能侧仓）→ ③ jiuwenswarm（回滚逆序；错序后果见 [[S_09]]）。

## 测试

pytest.ini 的 addopts 含全仓 coverage，日常跑请清掉（`-o addopts=""`）：

```powershell
# 模块单测（322 个：validator/包加载/tool_utils/planner/fallback/HITL 对齐/交付门禁/退出路径帧级/AbortError 守卫/契约解析/ask_user 门控/ContextVar 通道/入口拦截/task_states 快照…）
.venv\Scripts\python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ -o addopts=""

# HITL / 工具面 / 中断恢复（历史路径在 agentserver/ 下——skill_turbo 曾位于该处，迁移遗留，勿再新增）
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_resume_usage.py tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py -o addopts=""

# 技能侧仓（改 turbo code 后必跑的注册门禁；在技能侧仓执行，路径为共享技能根目录下的技能目录）
python -m unittest discover -s <shared_skills_root>/pptx-craft/turbo/tests -p "test_turbo_registration_gate.py"
```

- **改动 HITL / 事件流 / 降级路径时，除单测外必须对照 [[S_06]] 修改检查单跑全场景矩阵**：
  单卡 / 连续多卡 / 暂停后取消 / 暂停后重发纯文本 / 嵌套外层中断 / 重复作答 / resume 内二次
  中断 / 跨会话并发同参数。
- 修复类改动必须附**能复现原故障的测试**（先红后绿）；修契约类问题（重试 / 异常处理 / flush /
  透传）必须列出全部同构调用点逐个确认。

## 代码风格

- 类型注解：PEP 585 内置泛型（`list[X]`、`dict[K, V]`）+ PEP 604 联合类型（`X | None`）；
  禁止从 `typing` 导入已被内置语法替代的别名。
- 注释 / 日志 / docstring 中文，代码符号英文；关键机制处注释写"为什么"而非"是什么"
  （executor 内大量此类注释是契约的一部分，清理时勿顺手删除）。

## 设计文档归档与双向同步（本模块强制）

本模块设计文档**全部**落在 `docs/` 下，命名与结构规约见 [`docs/AGENTS.md`](docs/AGENTS.md)。
把 **`skill_turbo/` 代码 + `docs/specs/` + 本 AGENTS.md** 当作一个一致性单元维护——本模块
跨层接缝多（HITL 状态机 / 事件流终态 / 失败信号传播），"只改部分执行路径"是缺陷的头号来源，
specs 的执行路径全景与修改检查单就是为根治此问题而设。

**前置铁律——文档先是设计的输入，才是提交的产出。** 动手改任何代码**之前**，先读相关
`docs/specs/S_NN_*`（现有契约 / 执行路径 / 修改检查单），拿它们校准方案；方案定了**当场**同步
修订对应 spec。先码后档、拖到提交前才补文档是把流程做反了。

三条强制约束，提交时必查：

1. **特性更新归档 feature 且三连续提交**：`docs/features/` 新增 `F_NN_<slug>.md`；特性代码
   （`feat(skill_turbo)`）、测试（`test(skill_turbo)`）、文档（`docs(skill_turbo)`）拆成三个
   紧邻提交、同一 PR 落地。bugfix 触及 spec 描述的路径/不变量时同样三连（`fix`/`test`/`docs`）。
2. **规约变动必更新 specs**：契约 / 执行路径 / 不变量 / 事件协议 / 持久化结构变化时，同步修订
   对应 `S_NN`（元信息"最近一次修订日期"填当天）；新规约主题 = 新 spec 文件。
3. **读到不一致当场修文档**：本文件 / specs 里与代码不符的接口名、枚举值、路数、路径，先 grep
   代码、以代码为准刷新文档，同一次改动落地；不把过时表述当新约束执行。点名数量的句子
   （"14 处 AbortError 守卫点"、"13 条退出路径"、"7 个 ContextVar 通道"）都是契约的一部分。
