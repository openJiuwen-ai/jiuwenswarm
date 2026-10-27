# Agent 侧技能 Rails 运行时

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_05 / agent-skill-rails |
| 关联模块 | jiuwenswarm/agents/harness/common/rails/skill_*.py + subagent_skill_authorization_rail.py + rails/permissions/skill_authorization/（file_store + runtime） |
| 最近一次修订日期 | 2026-10-21 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

swarm 侧 7 个技能 rail 的**运行时行为契约**：priority、挂载条件、状态读写、与其他组件的交互。core 侧原语（`SkillUseRail` 目录扫描 / skills 段注入 / `skill_tool` 工具、`SkillAuthorizationRail` 裁决骨架、Grant / Manifest / 审批卡数据模型）在 **agent-core 仓**，本 spec 只描述协作边界与 swarm 侧差异；装配入口（interface_deep / flash / code / swarm providers）的完整顺序表也在此收敛。工具（`SkillToolkit` / `SkillRetrievalToolkit`）见 [[S_06]]。

## 装配全景

rail 回调框架按 **priority 数值大者先执行**。技能链装配点与优先级：

| Rail | priority | 挂载条件 / 装配点（interface_deep.py） |
|---|---|---|
| `SkillUseRail`（core） | 95 | `_build_skill_rail`；skill_mode 检索开启时强制 `auto_list` |
| `SkillAuthorizationRail`（swarm 子类） | 95（继承 core） | `_build_skill_authorization_rail`，**须在 `_permission_rail` 之后构建**（复用其 engine） |
| `SkillRetrievalPromptRail` | `SkillUseRail.priority - 1`（= 94，**动态推导**） | `_build_skill_retrieval_prompt_rail`；`is_skill_retrieval_enabled()` 门控 |
| `SkillSleepRail` | 26 | `_build_skill_sleep_rail`，**始终挂载**（是否跑休眠周期由 per-skill 配置在 runner 内裁决） |
| `SkillActiveStateRail` | 25 | `_build_skill_active_state_rail` |
| `SkillProtocolPromptRail` | 8 | `_build_skill_protocol_prompt_rail`，**仅 `mode.startswith("agent")`** |
| `SkillCredentialInjectionRail` | 5 | `_build_skill_credential_injection_rail`（`react.skill_envs`） |

衍生装配：flash 模式 `_FLASH_RAIL_KEEP` 白名单保留 `SkillUseRail`（钉 `SKILL_MODE_ALL`，仅注册 skill_tool）/ credential / active_state / authorization；swarm 成员经 `agents/swarm/providers/`（`swarm.skill_retrieval_prompt` / `swarm.skill_retrieval` / `swarm.member_skill_toolkit`）；团队技能工作区经 `MemberSkillToolkitRail`（team/rails/）。

**热重载原位更新约定**：`SkillUseRail`（skill_mode / disabled_skills 原位改，不重建——保住动态加载技能）、`SkillCredentialInjectionRail.update_skill_envs`（**空 overlay 不清空已有凭据目录**）、`SkillActiveStateRail.update_stale_invoke_limit`、`_sync_skill_retrieval_prompt_rail_for_runtime`（按开关动态注册 / 注销）、`_reconcile_skill_sleep_rail`（惰性补挂）。

## SkillActiveStateRail — 会话级活跃技能状态

**职责**：跨 invoke 跟踪"当前会话激活了哪个技能"，为凭据注入提供状态源。**会话级生命周期，不是 invoke 级**——新任务、HITL 恢复轮都不清空。

状态载体：`_SkillSessionState`（active_skill / source_session / invokes_since_skill_tool），持久化在 `FileSkillGrantStore` 会话文件的 `doc["active_skill"]`（portalocker + 原子写，**无内存缓存**——每次读写走文件，多适配器 / 多进程一致）。

会话 id 解析（`resolve_skill_session_id`）：preset → `inputs.conversation_id` → `ctx.session` → extra（`__jiuwenswarm_session_id__`，与 StreamEventRail 共享）→ contextvar → `"default"`。**真实状态绝不落 `default` 名下**（`_shared_store` 对 default 会话返回 None；凭据注入读取时"只读查询、不收养 default 孤儿"——防跨会话凭据泄漏）。

写入（`after_tool_call`）：
- `skill_tool` 成功且**非目录列举**（tool_msg metadata `is_directory_listing` 为假）→ `active_skill = skill_name`，计数清零；与旧值不同则记录 switch。
- `skill_complete` → **仅当名字匹配**当前 active 时 reset；不匹配只告警不清空（防状态挂死无从排查）。

清空四路径：① `skill_complete`（名字匹配）② 切换到另一技能 ③ 会话删除 / teardown（`clear_session_skill_state`）④ **过期兜底**：连续 `stale_invoke_limit`（`react.skill_stale_invoke_limit`，默认 5）轮 invoke 无 skill_tool 调用 → 下一轮 `before_invoke` 清空。兜底存在的原因：模型对 skill_complete 遵从性不可靠，防止"遗忘的技能占着凭据注入窗口一整场会话"。**HITL 恢复轮**（permission / confirm / ask_user interrupt 来源，经 `_CHAT_SEND_SOURCE_EXTRA_KEY` 识别）不递增计数。

**main 熔断与延迟复活**（任务取消防护）：

任务终止路径（interrupt cancel/abort 经 core 骨架 `invalidate_scope`；合规自动停用经 `rails/permissions/skill_authorization/runtime.py` 的 `revoke_deactivated_main_skill`）把 `main` 写入会话文件的 invalidated 列表——**熔断后迟到回调不能再恢复凭据窗口**。同会话中全新的显式 `skill_tool` 激活是新的用户意图、可以解除熔断，但解除被刻意**延迟合并进激活持久化的同一事务**：

- `before_tool_call`：对 skill_tool 调用**只读探测** `store.is_scope_invalidated(session_id, "main")`（探测失败按无熔断处理，fail-safe）；熔断则在 `ctx.extra[_SKILL_REVIVE_PENDING_KEY]` 打上**本次 tool_call_id** 的标记。此处绝不提前解熔断——提前解会让被取消轮的迟到回调滑过 operate 的 pop 守卫，且 listing / 被拒调用会空解除。
- `after_tool_call`：激活校验（非目录列举、有 skill_name）通过后，**复活标记匹配同一 tool_call_id** 才置 `revive_main=True`，经 `update_active_skill_state(..., revive_main=True)` 在**同一事务内**持久化激活并 `revive_scope` 解除 main 熔断——不存在"熔断已解除而激活未落盘"或"激活已落盘而熔断未解除"的中间态。完成类更新（active_skill=None）**永不复活**；标记值绑定 tool_call_id，防 Agent 循环复用 ctx.extra 时残留标记误伤后续调用。
- 已知残留（代码注释声明）：本调用成功完成后、after 派发前的微窗口内发生第二次 cancel（revoke 重写熔断），标记匹配仍会持久化本技能。

失败语义：会话目录不存在时拒绝写（迟到回调不能复活已删除会话）；存储异常抛 `AbortError` 中止本轮（**不带旧状态继续跑**）。

## SkillCredentialInjectionRail — 凭据注入 + SKILL_GATE

**职责**：把 per-skill 凭据环境变量（`react.skill_envs`，`{skill_name: {ENV_KEY: value}}`）注入 shell 类工具调用的 `tool_args["env"]`，同时拦截"直接运行未激活技能的脚本"。

作用工具集：`SHELL_PERMISSION_TOOLS = {"bash", "shell", "mcp_exec_command", "create_terminal", "exec_command"}`。注入规则：已有 key 不覆盖、空值跳过；附带 PowerShell `'&'` 调用前缀剥离（仅 bash 工具 + 默认 shell 类型；LLM 显式指定 powershell 则保留）。

**SKILL_GATE 三分支**（`before_tool_call`，priority=5——最后执行，先让权限 rail 跑完）：

命令经**执行位判定**（见下）后：

1. 执行位命中**当前激活技能**（或命令不可解析：只读引用 / cd 相对路径 / 裸文件名 / 无凭据技能 / 非脚本形态）→ 按 active 注入（fail-open 到旧行为，因为不构成跨技能泄漏）。
2. 执行位命中**另一个有凭据技能** → **拒绝**（`ctx.extra["_skip_tool"] = True` 短路工具执行），写回指引文本（先 `skill_tool(skill_name=...)` 激活再重跑）。**逃生口**：`tool_args["env"]` 已显式携带该技能任一凭据 key → 放行原样执行，且**停注 active 技能凭据**（防跨技能泄漏）。
3. 执行位命中**多个技能** → 指引拆分为多条命令分别激活执行（单激活语义下"激活 X 后重跑"会乒乓循环）。

**执行位判定**（纯函数层，与 rail 实例无关）：

| 函数 | 职责 |
|---|---|
| `_split_subcommands` | 按引号外 `&& / \|\| / ; / \| / & / 换行` 拆子命令（引号内不拆；`2>&1` 的 `&` 不拆；多行复合命令逐行识别） |
| `_tokenize` / `_strip_quotes` / `_basename_lower` | 分词、去引号、basename 归一（兼容反斜杠 / `.exe` / 大小写） |
| `_iter_executed_scripts` | **只判执行位**：① 首位解释器（python / python3 / py / node）→ 跳过 flag 后首个脚本 token；② 首 token 自身是脚本（`./skill/run.py` 直执行）；③ 其他命令（cat / grep / ls…）→ 无执行位，参数位路径不扫。跳过 `VAR=val` 赋值前缀与 `nohup / timeout / sudo` wrapper 前缀 |
| `match_skills_in_command` | 执行位脚本路径**逐段**（`/` 分割）精确匹配已知技能名（整段边界，`mx-a-x` 不会误匹配 `mx-a`；大小写不敏感），按出现顺序去重 |

已知漏拦面（模块注释声明，安全面与旧行为持平）：命令替换 `$(python3 x.py)`、xargs 等罕见形态走回退注入，不产生误拦。

## SkillAuthorizationRail — 授权门禁的宿主适配（薄子类）

**继承关系**：core `SkillAuthorizationRail`（agent-core，裁决骨架：DENY 预判 → Manifest 解析 → scope 解析 → 权限 overlay 差分 → `_claim_gate` 短路标记 → BUILTIN/低风险复用或静默批准 → 审批卡中断流；`skill_complete` 回收 Grant、根 SKILL.md 加载成功激活 pending、Manifest 漂移 drop + retire）。

swarm 子类只做四件事：
- `inherit_to_subagents = False`（类属性）：子代理收到**自己的委托门禁**（绑定父会话，见下节），不继承主门禁——授权不可继承的代码强制点。
- `resolve_scope(ctx)`：把 core 的 `_resolve_scope` 公开为 `(session_id, agent_scope_id)`，供宿主 permission rail 集成。
- `set_sessions_root(path)`：装配层每请求传入**可信租户会话根**（supervisor / checkpoint 任务间携带租户身份）。
- `_bind_session_files(ctx)`：每次工具调用前后 `bind_session_file_context(scope[0], root)`，使 core 骨架的 Manifest / Grant 落到正确的会话文件（`FileSkillGrantStore`）；`sync_feature_state` 在策略中途被关闭时退休持久化 Grant；文件锁 / IO 异常抛 `AbortError`。

装配要点：engine 复用 `_permission_rail._engine`；`skill_resolver` 用 core `build_skill_registry_resolver` **延迟**读最新 `SkillUseRail.skills_meta`（防热更新拿到旧实例）+ `_skill_installation_snapshot()`（SkillManager 安装账本校准 source / version 身份）；`prebuilt_dirs_provider` / `builtin_dirs_provider` 注入 trust 判定目录快照（prebuilt/builtin 目录 → `SkillTrustLevel.BUILTIN`）。

**`FileSkillGrantStore`**（agents/harness/common/rails/permissions/skill_authorization/file_store.py）：Grant / 审批上下文 / 活跃技能状态 / **scope 熔断标记（invalidated 列表）**四合一落 session 目录；`operate` + portalocker 文件锁、原子替换。是 core 默认 `SkillGrantStore` 的文件化实现。熔断面 API：`invalidate_scope`（写入端 = core 骨架任务终止路径 + swarm `revoke_deactivated_main_skill`）、`is_scope_invalidated`（只读探测）、`revive_scope` / `update_active_skill_state(revive_main=)`（同事务解熔断，见上文活跃状态节）。

## SubagentSkillAuthorizationRail — 子代理委托授权

**职责**：子 Agent（`task_tool` 经 `create_subagent` 惰性创建）加载技能时的授权门禁：复用主 rail 的裁决骨架，**交互步骤委托给主会话**——审批卡经父会话 `session.write_stream` 下发 `chat.ask_user_question`（带 `SKILL_APPROVAL_CARD_EXTENSION_KEY`；**必须带 `index` 的 OutputSchema**，裸 dict 会被规范化成 message 导致前端看不到卡片），子 Agent 侧 Future 等待（**不能 checkpoint interrupt**）。

相对父类的模板钩子差异：

| 钩子 | 子类行为 |
|---|---|
| `_claim_gate` | 恒 True（子 Agent 无原权限 rail 兜底，无需短路标记） |
| `_proceed_tool_call` | 放行必须**显式 approve** |
| `_on_policy_evaluation_failed` / `_on_gate_stale` | 直接 reject（`[PERMISSION_DENIED]`），fail-closed |
| `_try_reuse_session_approval` | 恒 False——子 Agent 生命周期短，审批**始终落 local Grant**，不回写父或会话缓存 |
| `_run_approval_flow` | 审批卡剔除 `APPROVE_SESSION` 选项；`SubagentApprovalRegistry.request(kind=SKILL_LOAD)` Future 等待；超时 / 取消 / 通道异常（如 WS 断开）→ `_proceed_tool_call`（**不击穿子 Agent**）；答案到达后二次校验 feature flag / generation / manifest 身份未变（变了按 stale 放行重走） |

**作用域恒为「主会话 session_id + 子 Agent scope id」**——ContextVar 未命中时回落 rail 注入的主会话 id，**绝不回落子 Agent 复合 session**（防 Grant 命名空间错位）。

装配：`subagent_executor/authorization.py` 的 `install_subagent_authorization_wiring` **包装父 Agent 的 `create_subagent`**（不改 agent-core）：仅当父授权 Context 为 main scope（杜绝授权继承）且开关开启时，`build_subagent_authorization_rails` 挂 `(SubagentSkillAuthorizationRail, SubagentPermissionRail)` 对；子 Agent 生命周期 finally 上 `cleanup_skill_authorization_scope`（清 scope 下全部 Grant 与待决审批）。engine 未注入时按策略评估失败处理（fail-closed）。

## SkillSleepRail — 使用计数与休眠训练触发

**职责**：统计 per-skill 的**使用量**（usage，非加载量）：一个用户任务内某技能经**工作工具**被成功使用过 → 计 1（任务内去重）。累计超过 `call_threshold`（默认 20，`get_skill_sleep_call_threshold`）时触发 `SkillSleepRunner.try_start` 离线"休眠"训练（自我进化）。

**usage ≠ load**：`skill_tool` 加载只绑定归属（`_remember_skill` 记 last_skill）；`_NON_USAGE_TOOLS`（skill_tool / skill_complete / todo 三件套 / ask_user / 生命周期工具 search·install·uninstall / 检索三件套 index_build·branch_explore·branch_peek）与失败 / 中断的调用一律不计。

任务窗口：`before_invoke` 非 HITL 恢复轮 → 新任务；上一任务遗留未冲账的 used 集合**先补计数**（任务确实发生过）。`after_invoke` 未停在 interrupt → `_end_task`（每技能 `counter.increment`；**count > threshold** 的增量时刻触发 `runner.try_start`）。

归属判定链：SkillUseRail 会话绑定（core `get_current_skill_name`）→ `get_session_active_skill`（本模块 active state）→ last_skill 兜底。

持久化：`SkillCallCounter`（`workspace/.skill_sleep/call_counts.json`，线程锁 + portalocker）；`last_skills.json`（`{session_id: {skill, ts}}`，只携带"中途暂停任务"的归属）——**多个 rail（每会话适配器各一个）共享该文件**，写入按 dirty-session 合并（读-合并-剪枝-临时文件-`os.replace` + portalocker）；`forget_session` 在会话 teardown 冲账并删除条目。

`SkillSleepRunner`（skill_sleep/runner.py）：后台非阻塞训练启动器；每 runner / 每 state_dir / 每 skills 目录三重并发互斥（失败回滚计数、指数退避 600s→24h）；注入轨迹目录（OTel `.trace`）、skills 目录与模型规格。是否真正跑由 per-skill selfEvolution 配置裁决。

## SkillProtocolPromptRail — 技能执行规范注入

**职责**：每次模型调用前刷新系统提示的 `skill_protocol` 段（`PromptPriority.SKILL_PROTOCOL = 45`），注入**技能执行过程的强制规范**（`_CN_PROTOCOL` / `_EN_PROTOCOL`，语言由 builder 的 `language` 决定）。仅 agent 模式挂载。

段内容三部分：
- **SKILL.md 加载规范**：必须且只能用 `skill_tool(skill_name=…, relative_file_path="SKILL.md")` 加载（禁 read_file / 拼凑）；嵌套子技能 `skill_name` 填顶层名、子技能走 `relative_file_path`；无 skill_tool 时告知用户而非替代；**历史加载内容不跨任务复用**（技能可能已卸载 / 更新）。
- **9 条执行规范**：① 声明步骤 `[当前步骤: …]`；② todo 与工作工具**同轮搭便车**（禁独占一轮，唯一例外是纯文本最终回复的前一轮；**加速通道例外**：单份交付物走 `skill_acceleration_exec` 时**不建 todo**，仅单轮多交付物才先建外层待办清单——见 `skill_turbo_guide` 段）；③ 严格顺序不跳过 / 合并 / 重排；④ 闸门等待禁止自行假设同意；⑤ 不确定时只能重调 skill_tool；⑥ 内容忠实（选项 / 参数 / 标签原样用）；⑦ 错误禁自 skip；⑧ 工具缺失先告知征得同意再替代；⑨ 用户打断按**原话**判定（"继续"类→续跑，其余视为放弃：todo 全标 cancelled）。
- **覆盖语义警告**：新消息默认覆盖旧任务（禁"两个都做"）。

实现要点：每次从 `ctx.agent` 现取最新 `system_prompt_builder`（防热重载后指向旧 builder）；`uninit` 先同步到当前生效 builder 再 `remove_section`。

与相邻 prompt 段的分工：技能**清单与加载约定**归 core `SkillUseRail` 的 `skills` 段（priority 40）；**加速通道指引**归 `SkillTurboPromptRail` 的 `skill_turbo_guide` 段（skill_turbo 模块）；本段只管**执行过程规范**，互不重复。

## SkillRetrievalPromptRail — Agentic 树检索提示

**职责**：agentic 技能检索（大规模已安装技能场景）启用时，注入轻量**技能树检索指引段**（`skill_retrieval`，priority 41），**同时隐藏**原生技能清单注入与 `list_skill` 工具，把模型引向 `skill_branch_explore` / `skill_branch_peek` 渐进浏览（工具见 [[S_06]]）。

关键设计：
- `priority = SkillUseRail.priority - 1`（**动态推导而非硬编码**）——core 曾把 SkillUseRail 从 100 调到 95，硬编码会静默反转执行顺序。
- `before_model_call`：门控 `is_agentic_retrieval_enabled()`；渲染在 `asyncio.to_thread` 中执行（`render_skill_retrieval_prompt_for_visible_skills`，内容为第一层分类清单 + 探索策略）；非空时**三连隐藏**——摘除 `list_skill` 能力卡（缓存到 `_hidden_legacy_abilities`）+ 从本轮模型输入 `inputs.tools` 滤除 + 摘除 `SectionName.SKILLS` 段。
- `after_model_call` / `on_model_exception`：恢复 list_skill 能力与原生 skills 段——**隐藏只作用于单次模型调用窗口**，之后立即还原（防状态泄漏）。
- `visible_skill_names` 支持集合或 `Callable[[], set | frozenset | None]`（惰性 provider，装配点传 `interface_deep._visible_skill_names_for_list_skill`，实时扫描技能目录并应用 enabled / disabled / 企业预置过滤）。
- 启用检索时 `_resolve_skill_mode` **强制 SkillUseRail 进入 `auto_list` 模式**（保留 skill_tool / read_file）。

## 不变量

- **N1（单激活凭据语义）**：同一时刻只注入一个技能的凭据；跨技能执行硬拦（除非显式自带凭据的逃生口，且此时停注 active 凭据）；多技能复合命令指引拆分。
- **N2（状态即文件 + AbortError）**：active 状态与 Grant 走 `FileSkillGrantStore` 会话文件，无进程内缓存；存储不可用抛 `AbortError` 中止本轮——**任何"带旧状态继续"的兜底都是违规**。
- **N3（default 会话隔离）**：真实状态绝不落 `default` 名下；default 会话读取一律"只读 / 不收养"。
- **N4（授权不可继承）**：子代理授权仅 main scope 父 Context 可装配；scope 恒绑主会话 + 子 scope；子 Agent 审批恒落 local Grant；生命周期 finally 清 scope。
- **N5（usage ≠ load）**：只有工作工具成功调用才计数；任务内去重；HITL 暂停跨 invoke 存活；放弃任务下轮补计。
- **N6（热重载原位更新）**：技能链 rail 热重载一律原位更新（update_* 方法），**不重建实例**——重建会脱离 agent rail 注册链并丢失动态加载技能；`skill_envs` 空占位不清空已有凭据。
- **N7（检索隐藏单窗口）**：list_skill / skills 段的隐藏只在单次模型调用窗口内生效，调用结束立即恢复。
- **N8（可见性统一来源）**：`_visible_skill_names_for_list_skill` 实时扫描 + enabled / disabled / 企业过滤，同时喂检索 prompt rail 与检索 toolkit——prompt、工具、SkillUseRail 三方必须看到同一技能视图。
- **N9（熔断-复活原子性）**：main scope 熔断后的复活只能由**全新的显式 skill_tool 激活**触发，且解熔断必须与激活持久化在**同一事务**内完成（`update_active_skill_state(revive_main=True)`）；完成类更新永不复活；复活标记按 tool_call_id 绑定。任何"提前解熔断 / 空解除（listing、被拒调用）/ 事后补解除"的实现都违反原子性并重新打开迟到回调窗口。

## 修改检查单

- 改 SKILL_GATE / 执行位判定 → 必须跑：跨技能执行拦截、多技能拆分指引、逃生口停注、引号内分隔符不拆、`2>&1` 不拆、解释器 flag 跳过、`..` 穿越不误判。
- 改活跃状态生命周期 → 核对四条清空路径 + HITL 恢复轮不计数 + default 会话隔离。
- 改熔断 / 复活路径（N9）→ 必须覆盖：迟到回调无标记仍被 operate 的 pop 守卫拦截、listing / 被拒调用不解熔断、复活标记 tool_call_id 匹配（含 Agent 循环复用 ctx.extra 的残留场景）、成功后微窗口二次 cancel 的已知残留。
- 改授权链 → core 骨架 / swarm 子类 / subagent 三层的模板钩子语义不得漂移；审批卡必须带 index 的 OutputSchema。
- 改热重载 → 保持 N6 原位更新；新增 rail 需实现 update_* 方法或在 `_reconcile_*` 注册。
- 改 prompt 段 → 核对与 skills 段 / skill_turbo_guide 段的分工边界（互不重复）；`SkillRetrievalPromptRail.priority` 保持动态推导。

## 与其它 spec 的关系

- 工具层契约（skill_tool 元数据 `skill_name` / `is_directory_listing` 的生产方在 core，消费方在本 spec 的 active_state / sleep rail）见 [[S_06]] 与 agent-core 文档。
- SkillManager 启停名单（`list_execution_disabled_skills`）经装配层喂给 `SkillUseRail.disabled_skills`，见 [[S_02]]。
- skill_turbo 的 ask_user / 权限 rail 与本链的交互见 skill_turbo 文档体系。
