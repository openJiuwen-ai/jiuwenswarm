# S_17 宿主装配与交互转换

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_17 / host-assembly-and-interaction-conversion |
| 关联模块（agent-core 仓） | `openjiuwen/harness/security/host.py`（`ToolPermissionHost` 回调接口——本 spec 管宿主侧实现）、`openjiuwen/harness/security/models.py`（`PermissionConfirmResponse`）、`openjiuwen/harness/security/skill_authorization`（`SKILL_APPROVAL_CARD_EXTENSION_KEY` / 子代理审批注册表） |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/agents/harness/common/rails/interrupt/interrupt_helpers.py`（1529 行：`build_permission_rail` / `convert_interactions_to_ask_user_question` / scene hook / hosted confirm 闭包族）、`common/rails/permissions/config_loader.py`（`get_effective_permissions_config` / `merge_session_permissions_overlay` / `persist_permissions_mutate`）、`common/rails/permissions/owner_scopes.py`（`TOOL_PERMISSION_CONTEXT` / `check_avatar_permission`）、`common/rails/permissions/tool_permission_context.py`（`TOOL_PERMISSION_CHANNEL_ID`）、`common/rails/subagent_skill_authorization_rail.py`（`emit_subagent_approval`）、`openjiuwen_streaming_tool_patch.py`（`streaming_tool_wait_timeout_paused`）、`code/rails/code_plan_approval_interrupt_rail.py`（plan approval 提取） |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | issue #1976（ask_user 豁免）、#2331（非数组 options 防字符迭代） |

## 范围 / 边界

本规约管 **jiuwenswarm 宿主对 agent-core 中断体系的装配与前后端交互转换**——`ToolPermissionHost` 全部回调的实现、权限 rail 的构建与三级变体选择、interrupt 结果到 `chat.ask_user_question` 前端卡片的转换：

- **rail 构建**（`build_permission_rail`）：配置来源二分、enabled 门、tool_names 标签收集、host 闭包族组装、三级 rail 变体选择
- **hosted confirm 通道分流**（`_request_permission_confirmation`）：hosted parent-card（web / officeclaw / tui）/ interrupt 弹卡 / ACP 原生 JSON-RPC 三路
- **场景豁免链**（`_permission_scene_hook`）：ask_user 豁免、deepresearch workflow 状态、数字分身、owner_scopes
- **持久化回调**（`_persist_allow_rule`）：只 mutate 三键的磁盘写
- **交互转换**（`convert_interactions_to_ask_user_question`）：三源分类（ask_user_interrupt / permission_interrupt / confirm_interrupt）+ plan approval 附加字段 + Skill 审批卡透传
- **HITL 序号后缀**（`strip_hitl_seq_suffix`）：作答对齐用剥离语义
- **子代理 hosted 审批**（`request_subagent_hosted_permission_confirmation`）：父会话卡审批

不管：

- rail 本体判定序与双 key 算法 → [[S_16]]
- 三态决策协议与基类 → [[S_15]]
- 中断相位机 / 权限响应台账 / 续跑注入 → [[S_18]]
- 取消结算与 `{id}#{n}` 序号代际的产生侧（`hitl_dedup` 的 adapter 实例计数）→ [[S_19]]（本 spec 只管消费侧剥离）
- SkillAuthorizationPermissionRail / SkillTurboPermissionRail 变体**内部**行为 → skill_turbo 域 [[S_06]] / [[S_09]]（本 spec 只管选择序）
- 子代理审批注册表本体（容量 / Future / 超时）→ [[S_13]]
- plan approval 的 rail 本体与 todo 语义 → [[S_04]]
- 事件流下发格式（chat.ask_user_question 事件协议）→ 事件流域

## 不变量

- **N1（配置来源二分）**：`build_permission_rail` 收到 dict 型 `permission_config`（Agent 模板 body）→ 企业版叠加当前会话 overlay（`merge_session_permissions_overlay`）、非企业版 deepcopy，`config_source="agent_template"`；未收到 → `get_effective_permissions_config()`，`config_source="effective"`。`enabled` 为假 → 返回 None（不装 rail）。
- **N2（tool_names 仅标签）**：`_collect_optional_tool_tags` 从 tools 键 + rules[].tools 收集，仅作展示 / 日志——拦截范围由 rail 本体决定（[[S_16]] N1）。
- **N3（persist 只 mutate 三键）**：`_persist_allow_rule` 经 `persist_permissions_mutate` 只把 `approval_overrides` / `file_guard` / `external_directory` 三键 deepcopy 进持久化目标 body（yaml `agents[id]` 命中时，否则 global）——**不得整盘替换**（会复活已删工具或丢 `agents` 表）。persist_scope：企业版 `session`、否则 `base`。失败返回 False（触发 [[S_16]] N8 回滚）。
- **N4（hosted 通道白名单）**：`_HOSTED_PERMISSION_CHANNELS = {web, officeclaw, tui}`——hosted 卡片需 `chat.ask_user_question` 渲染；**ACP 保留原生 `session/request_permission` JSON-RPC，不得劫持**。parent-card 路径仅当 `resolve_subagent_permission_parent_session` 返回非 None（嵌套代理 + 会话 id 不同 + parent 可 write_stream）且 channel ∈ 白名单。
- **N5（非 ACP 回落 interrupt）**：channel 非 acp 且无 parent-card → 返回 `"interrupt"`（走 [[S_15]] 弹卡路径）；ACP → JSON-RPC 请求（选项 allow-once / allow-always / reject-once）。
- **N6（ACP 预算保护）**：ACP 权限 HITL 与子代理 hosted 审批都包在 `streaming_tool_wait_timeout_paused()` 上下文中——**不得消耗 StreamingToolExecutor.wait_all 预算**。
- **N7（ACP 响应三分类）**：error dict → reject（`[PERMISSION_DENIED] {message}`）；`outcome=="selected"` → allow-once（approved, 无记忆）/ allow-always（approved + auto_confirm）/ 其它 reject；`outcome=="cancelled"` → reject（`[PERMISSION_REJECTED] ... cancelled`）；请求异常或非 dict → None（[[S_16]] N10 按宿主失败拒绝）。
- **N8（ask_user 豁免，issue #1976）**：`_permission_scene_hook` 对 `normalized_tool_name == "ask_user"` 且非 `group_digital_avatar` 场景直接 `("approve",)`——权限 rail 拦截一切工具，resume 时会把 ask_user 答案误当 ConfirmPayload 解析失败再弹权限卡死循环；豁免让 ask_user rail 的答案直达模型。数字分身场景例外（仍拦截）。
- **N9（deepresearch workflow 状态豁免）**：`deepresearch_execute` 的 user_input 为 dict 且 status ∈ {answered, skipped, cancelled, error} 且 answers 为 list → approve——结构化卡答案须直达 DeepResearchExecutionRail，不被权限 rail 再解析。首查（user_input None）与显式权限响应仍走正常权限路径。
- **N10（数字分身场景）**：`TOOL_PERMISSION_CONTEXT.scene == "group_digital_avatar"`——user_input 非 None（交互审批）→ reject（不支持交互审批）；否则 `check_avatar_permission`：allow → approve，否则 reject。
- **N11（owner_scopes allow 须无 ACTIVE Skill overlay）**：owner_level == "allow" 时若 `_has_active_skill_overlay` 为真 → 返回 None 回落 engine（防 owner allow 绕过 overlay 收紧）；**判定失败按存在处理**（返回 True，不放宽）。owner_level 非 allow → reject（`[PERMISSION_DENIED] 该工具未被授权 (owner_scopes: ...)`）。principal_user_id / channel_id / owner_scopes 配置缺失 → None（回落主序）。
- **N12（agent_template 快照返回 None）**：`_get_permissions_snapshot` 在 `config_source=="agent_template"` 时返回 None——工具校验跑在 DeepAgent supervisor Task（不继承请求 Task 的 PERMISSIONS_AGENT_BASE），走 get_effective 会回落 yaml（常 enabled:false）覆盖模板配置；返回 None 保 `_static_config`（请求开头 `_update_permission_rail` 与 persist 的 update_config 会刷新它）。
- **N13（三级 rail 选择逐级回退）**：`rail_cls` 选择序——`PermissionInterruptRail` →（import 成功）`SkillAuthorizationPermissionRail`（skill_tool / skill_complete 专属裁决避免重复弹卡）→（`react.skill_turbo.enabled`）`SkillTurboPermissionRail`（定制 skill_acceleration_exec 审批消息，两种定制叠加）。任一级 import / 配置读取异常 → debug 日志回退上一级，**不得让构建失败**。整体构建异常 → warning + 返回 None。
- **N14（三源分类优先序）**：`convert_interactions_to_ask_user_question` 三轮扫描——① **structured ask_user 优先**（value 有 questions → `ask_user_interrupt`；防权限 shell 空卡让前端拿不到 request_id 恢复等待中的工具调用）② plain ask_user（tool_name==ask_user 或 payload_schema+questions 特征，无 questions 字段时从 tool_args.query / message / question 取自由文本）③ permission / confirm（`_resolve_interrupt_source` 启发式：message 含 `_PERMISSION_INTERRUPT_MARKERS`（需要授权才能执行 / requires permission / Permission denied / 安全风险评估）或 `**工具 \`` 前缀或 tool_name ∉ `_CONFIRM_INTERRUPT_TOOLS`（switch_mode / exit_plan_mode）→ permission_interrupt，否则 confirm_interrupt）。
- **N15（plan approval 附加字段）**：`confirm_interrupt` + `exit_plan_mode` + `is_plan_approval_message` → 附加 `plan_content` / `plan_language`（en 归一）/ `plan_approval_kind="plan_approval"` / `plan_actions`（Web 用；**TUI 忽略该字段继续用 questions[].options 的 approve/reject，两端互不影响**）/ `plan_path` / `plan_slug`。
- **N16（Skill 审批卡透传）**：payload_schema 含 `SKILL_APPROVAL_CARD_EXTENSION_KEY` 时把 card dict 顶层透传给前端结构化渲染（契约键冻结）；`_classify_structured_approval` 按 metadata.source / interrupt_kind / tool_name（evolve_skill_experiences / simplify_skill_experiences）识别演进审批并附加 `source=evolution_interrupt` + `approval_kind`。
- **N17（企业版永久记住降级）**：企业版三处一致降级——`_default_interrupt_options` 不含「永久记住」（依赖写回本地 config 与 Agent permissions 模板权威源不一致且 supervisor Task 下 session_id 常丢失）；`parse_hosted_permission_answer` 永久标签（永久记住 / 总是允许 / Always Allow / allow_always）降级为 session 记忆（persist_allow=False）；ui_options 过滤永久选项（仅 permission_interrupt，技能演进保留「总是允许」）。企业版 permission 弹窗文案剥离「永久记住」提示（`_strip_permanent_remember_hint`）。
- **N18（hosted 答案标签映射）**：`parse_hosted_permission_answer` 按 selected_options 标签映射——reject 标签优先（拒绝语义压倒一切）→ 永久标签（N17 降级）→ 会话记住（auto_confirm=True, persist=False）→ 本次允许（无记忆）；空标签 → None。
- **N19（子代理 hosted 审批永不 interrupt）**：`request_subagent_hosted_permission_confirmation` 经子代理审批注册表（[[S_13]] N6 容量约束）等父会话卡答案，**永不返回 `"interrupt"`**；超时 → reject（`[PERMISSION_DENIED] 子 Agent 权限审批超时`）；取消 / 容量满 → reject；通道异常 → None；答案解析失败 → reject（`[PERMISSION_REJECTED]`）。session remember 存子 rail 会话（选项描述明示「该子 Agent 委托内」），不得暗示父会话作用域。
- **N20（序号后缀剥离语义）**：`strip_hitl_seq_suffix` 剥 `{id}#{n}` 恢复 harness 原始 tool_call id，仅用于**作答对齐**与「base 当前代卡」判定；**权限账本去重 key 不做此归一化**——`#{n}` 是顺序门的不同代次（不同权限内容），归一化会把下一个合法门误判为重复（CR-1）。未命中后缀原样返回。
- **N21（resume 源判定）**：`INTERRUPT_RESUME_SOURCES = {permission_interrupt, confirm_interrupt, ask_user_interrupt, evolution_interrupt}`；`is_interrupt_resume_source` **有意排除** skill-evolution 源（保持 active skill / skill_envs 注入跨安全 HITL）；legacy `skill_evolution_approval` 源须 `evolution_meta.approval_transport == "interrupt"` 才算 resume 载荷。
- **N22（选项构造防呆）**：`_build_multi_questions`——有选项的问题末尾追加自定义输入选项，后端只追加 `{"label": "Other", "description": "Custom input"}`（**无 value 键**）；哨兵值 `__other__` 是**前端** `InlineQuestionCard` 的识别标记（`OTHER_VALUE`，另兼容仅 label 的旧形态 `!value && label === 'Other'`）；无选项不追加（前端直接自由输入）；**非数组 options（如 "a,b"）不得按字符迭代**（#2331，置空处理）。

## 接口契约

### rail 构建

| 函数 | 契约 |
|---|---|
| `build_permission_rail(config, options) -> rail \| None` | N1 配置二分 + enabled 门；N13 三级选择；N22 整体失败 None |
| `PermissionRailBuildOptions` | llm / model_name / permission_config / resolve_workspace_dir / persist_target_agent_id_provider / rail_factory（测试注入） |

### host 闭包族（装配进 `ToolPermissionHost`）

| 闭包 | 契约 |
|---|---|
| `_persist_allow_rule(permissions) -> bool` | N3 只 mutate 三键；scope 企业 session / 否则 base |
| `_request_permission_confirmation(req)` | N4 通道分流：parent-card（hosted 白名单）→ N19；非 acp → `"interrupt"`；acp → N5/N6/N7 JSON-RPC |
| `_permission_scene_hook(inp) -> tuple \| None` | N8 ask_user 豁免 → N9 workflow 状态 → N10 数字分身 → N11 owner_scopes；None 回落主序 |
| `_get_permissions_snapshot() -> dict \| None` | N12 agent_template 返回 None；effective 源返回 get_effective |
| `resolve_workspace_dir` / `permission_yaml_path` | 默认 `get_workspace_dir` / `get_config_file()` |

### 交互转换

| 函数 | 契约 |
|---|---|
| `convert_interactions_to_ask_user_question(state_outputs) -> dict \| None` | N14 三轮扫描；返回 `{"event_type": "chat.ask_user_question", "request_id", "questions", "source"}` + N15/N16 附加字段；无交互返回 None |
| `extract_question_from_interaction(payload) -> dict \| None` | 单交互提取：confirm 空文案补 `build_confirm_interrupt_message`；header 三分（Exit Plan and Execute / 操作确认 / 权限审批）；企业版 permission 剥永久提示 |
| `_question_options_from_ui_options(...)` | ui_options 优先（企业版 permission 过滤永久项）→ plan approval 选项 → `_default_interrupt_options` 兜底 |
| `strip_hitl_seq_suffix(request_id) -> str` | N20 剥后缀；账本 key 不归一化 |
| `is_interrupt_resume_source(source) / is_interrupt_resume_payload(params)` | N21 源判定 |
| `parse_hosted_permission_answer(answer) -> PermissionConfirmResponse \| None` | N18 标签映射 |

### 子代理 hosted 审批

| 函数 | 契约 |
|---|---|
| `request_subagent_hosted_permission_confirmation(req, parent_session, timeout=120.0)` | N19 永不 interrupt；注册表容量约束见 [[S_13]] N6 |
| `resolve_subagent_permission_parent_session(ctx)` | 嵌套代理 + 会话 id 不同 + parent 可 write_stream 才返回 parent；同会话绑定不得走 hosted 路径 |

## 数据结构

- 前端卡片 payload：`event_type` / `request_id` / `questions[]`（question / header / options[] / multi_select / preview?）/ `source` + 可选 `plan_*` / `SKILL_APPROVAL_CARD_EXTENSION_KEY` / `approval_kind` / `evolution_context`
- 选项标签集合：`_HOSTED_ALLOW_ONCE_LABELS`（本次允许 / 允许 / Allow once / allow_once / allow-once）、`_HOSTED_SESSION_REMEMBER_LABELS`（本会话内允许 / 会话内记住 / Session remember / allow_always_session）、`_HOSTED_REJECT_LABELS`（拒绝 / Reject / reject / reject-once / reject_once）、`_PERMANENT_REMEMBER_LABELS`（永久记住 / 总是允许 / Always Allow / always_allow / allow_always）
- ACP 请求：`toolCall`（descriptor + title 含 reason）+ options（allow-once / allow-always / reject-once，各带 kind）

## 执行路径

1. **P1 rail 构建**：装配点调 `build_permission_rail` → N1 配置二分 → enabled 门 → tool_names 标签 → 闭包族（persist / confirm / scene_hook / snapshot）→ host 组装 → N13 三级 rail 选择 → 实例化（失败 None）
2. **P2 hosted confirm**：rail ASK → `_request_permission_confirmation` → channel 读取（`TOOL_PERMISSION_CHANNEL_ID`，默认 web）→ parent_session 解析 → N4 分流：hosted parent-card（N19 注册表审批）/ 非 acp `"interrupt"` / acp JSON-RPC（N6 预算保护 + N7 响应分类）
3. **P3 scene hook 豁免**：rail 首查先过 hook → N8 ask_user → N9 workflow → N10 数字分身 → N11 owner_scopes → None 回落 [[S_16]] N3 主序
4. **P4 交互转换**：agent 返回 interrupt dict → `convert_interactions_to_ask_user_question` → N14 三轮扫描 → structured / plain / permission-confirm 分类 → N15 plan 附加 / N16 Skill 卡透传 → 前端 `chat.ask_user_question` 事件
5. **P5 用户作答回流**：前端回 source + request_id + answers → `is_interrupt_resume_payload` 判定 → resume 路径（[[S_15]] P2 / [[S_18]]）

## 异常分支与退出路径

- rail 构建整体异常：warning + None（不装 rail，权限退化为无拦截）
- 三级 rail import 失败：debug + 回退上一级
- ACP 请求异常 / 响应非 dict：None（宿主失败按拒绝）
- 子代理 hosted 审批超时 / 取消 / 容量：reject；通道异常：None
- scene_hook 异常：warning + None 回落主序（[[S_16]] N3）
- persist 失败：False → [[S_16]] N8 内存回滚
- 交互转换无匹配：None（不产生卡片事件）

## 边界 case

| 场景 | 行为 | 依据 |
|---|---|---|
| 企业版 Agent 模板 + supervisor Task 工具校验 | snapshot 返回 None 保 _static_config | N12 |
| OfficeClaw worker 嵌套代理 ASK | parent-card hosted 审批（web/officeclaw/tui） | N4 |
| ACP 客户端权限请求 | 原生 JSON-RPC，不劫持为 ask_user 卡 | N4/N5 |
| ask_user 答案回流被权限 rail 截获 | scene hook 豁免 approve | N8 |
| deepresearch_execute 结构化答案 | 状态豁免直达 workflow rail | N9 |
| 数字分身场景 ask_user | 不豁免，仍拦截 | N8/N10 |
| owner allow + ACTIVE Skill overlay | 回落 engine 不放宽 | N11 |
| owner_scopes 判定异常 | 按存在 overlay 处理 | N11 |
| 权限 shell 内含真 ask_user 卡 | structured ask_user 优先提取 | N14 |
| exit_plan_mode 计划审批卡 | plan 附加字段，TUI 用 options 不受影响 | N15 |
| 企业版用户点「永久记住」 | 降级 session 记忆 | N17 |
| 同外层 call id 第 n 代权限门 | 账本 key 保留 `#{n}` 不归一化 | N20 |
| options 传成 "a,b" 字符串 | 置空不按字符迭代 | N22 |

## 修改检查单

改 interrupt_helpers.py / 装配相关文件前：

- [ ] 配置来源二分未被合并成单一路径（N1）？
- [ ] persist 仍只 mutate 三键、scope 分流正确（N3）？
- [ ] ACP 通道未被劫持为 hosted 卡（N4/N5）？
- [ ] ACP / hosted 审批仍包 wait_timeout_paused（N6）？
- [ ] ask_user 豁免未误伤数字分身场景（N8）？
- [ ] owner allow 回落条件（ACTIVE overlay 判定失败按存在）未放宽（N11）？
- [ ] agent_template 快照仍返回 None（N12）？
- [ ] 三级 rail 回退链完整（N13）？
- [ ] 三源分类优先序（structured ask_user 最先）未调换（N14）？
- [ ] plan_actions 与 TUI options 双轨并存未被合并（N15）？
- [ ] 企业版永久记住三处降级一致（N17）？
- [ ] 子代理 hosted 审批仍永不返回 "interrupt"（N19）？
- [ ] 序号后缀剥离未用于账本去重 key（N20）？
- [ ] 新增 resume source 时 `is_interrupt_resume_source` 的排除语义复核（N21）？

## spec 关系

- 上游：[[S_15]]（弹卡路径与 InteractiveInput 回流）、[[S_16]]（host 回调契约与判定序——本 spec 是其宿主实现侧）
- 下游：[[S_18]]（resume 请求进入相位机与台账）、[[S_19]]（`{id}#{n}` 序号的产生与取消结算）
- 跨域：[[S_13]]（子代理审批注册表与 emit_subagent_approval）；[[S_04]]（plan approval rail 本体）；skill_turbo [[S_06]] / [[S_09]]（rail 变体内部行为）；agent_adapter/AGENTS.md 铁律 2（HITL 卡片双轨注册表消费 request_id）；事件流域（chat.ask_user_question 事件协议）
