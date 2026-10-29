# S_13 子代理授权与审批

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_13 / subagent-authorization-and-approval |
| 关联模块（agent-core 仓） | `openjiuwen/harness/security/skill_authorization/subagent_approval_registry.py`（`SubagentApprovalRegistry` 单例 / Future 审批）、`openjiuwen/harness/security/skill_authorization/`（Grant store / generation / `is_skill_authorization_enabled`）、`openjiuwen/harness/rails/skills/skill_authorization_rail.py`（模板钩子 / `MAIN_AGENT_SCOPE_ID` / `_resolve_session_id`）、`openjiuwen/harness/rails/skills/skill_lifecycle_events.py`、`openjiuwen/harness/rails/interrupt/confirm_rail.py`（`ConfirmInterruptRail` 基类）、`openjiuwen/harness/security/core.py`（`PermissionEngine`） |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/agents/harness/common/tools/subagent_executor/authorization.py`（wiring 装配层）、`context_vars.py`（`_subagent_parent_session`）、`common/rails/subagent_skill_authorization_rail.py`（委托门禁）、`common/rails/subagent_permission_rail.py`（执行期权限裁决）、`common/rails/permissions/config_loader.py`、`owner_scopes.py`；装配点 `interface_deep.py`（`_bind_subagent_authorization_wiring` @7193） |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**子代理的动态授权与审批链**——Skill 委托门禁与工具权限裁决在子代理上的投射：

- **wiring 装配层**（jiuwenswarm `subagent_executor/authorization.py`）：包装父 Agent `create_subagent`，子代理创建时按需装配委托 rail 对（`SubagentSkillAuthorizationRail` + `SubagentPermissionRail`）并接管生命周期清理
- **scope 判定**：子代理授权作用域恒定绑定主会话 session_id + 子 agent scope id；仅父 Context 为 main scope 才装配
- **审批注册表**（agent-core）：per-scope 单 pending、per-session 容量、Future 机制、超时 / 取消 / 容量三类异常
- **执行期权限裁决**（`SubagentPermissionRail`）：手工合成 overlay + 临时引擎、ASK 走 Future 委托（不抛 checkpoint）、被拒指纹、owner_scopes 收紧、EXCLUDED_TOOLS_SPAWN 深防御
- **父会话审批下发**：全部经 `chat.ask_user_question` 到主会话（`emit_subagent_approval`），sender 发送时取 `_subagent_parent_session` ContextVar

不管：

- 子代理创建 / 分发 → [[S_11]]（wiring 只是 create_subagent 的包装层）
- 运行时状态机 → [[S_12]]
- 主 Agent 侧的 `SkillAuthorizationRail` / `PermissionInterruptRail` 本体契约——本 spec 只管其**子代理投射版**；主侧见 `common/rails/` 各自文件
- Grant store 存储格式 / 会话 overlay 合并算法（`config_loader`）——契约同主侧
- 中断交互（`chat.ask_user_question` 事件流本身）→ 中断交互域

## 不变量

- **N1（wiring 幂等且零侵入）**：`install_subagent_authorization_wiring` 以实例属性 `_jiuwenswarm_skill_authz_wiring_installed` 标记幂等；包装器内**任何**异常（scope 解析 / attach）只 warning——装配失败不得击穿子 Agent 创建。开关关闭或父 Context 非 main scope 时零侵入（原 `create_subagent` 直通）。
- **N2（main scope 唯一准入）**：`resolve_main_authorization_session` 仅当功能开启**且**父授权 Context `agent_scope_id == MAIN_AGENT_SCOPE_ID` 时返回主会话 id。子 Agent 任务可能继承父请求的授权 Context——只有 main scope 允许装配委托授权链，**杜绝任何形式的授权继承**。
- **N3（scope 恒定绑定主会话）**：`_resolve_subagent_scope`（subagent_skill_authorization_rail.py @63）——ContextVar 命中且 scope 匹配直接返回；否则回落 rail 注入的主会话 id，**不回落子 Agent 复合 session**（防 Grant 命名空间错位）。子 scope id 取 `create_subagent` 的 `subsession_id`（每调用唯一语义）。
- **N4（rail 对原子回退）**：`build_subagent_authorization_rails` 返回 `(gate_rail, permission_rail)` 对；开关关闭 / 参数缺失 / 任一构建异常 → 空元组（整体回退 legacy 路径），**不允许只挂半对**。
- **N5（生命周期清理三保证）**：`_wrap_child_lifecycle` 把 `cleanup_skill_authorization_scope`（`clear_scope` + `cancel_scope`）挂在子 Agent `invoke` / `stream` 的 **finally**——覆盖正常完成与异常路径。清理 best-effort 且幂等（空 scope / 重复调用 no-op）。`attach_subagent_authorization` 以 `_jiuwenswarm_skill_authz_scope` 标记幂等；子已初始化（pending rails 不再生效）则跳过并告警，**不**强挂。
- **N6（审批容量五重约束）**（agent-core registry，进程级单例）：
  1. per-(session_id, agent_scope_id) **单 pending**——同 scope 第二个未决审批直接 `SubagentApprovalCapacityError`（防重复弹窗）
  2. per-session 总量 ≤ `max_pending_per_session = 5`（构造 ≤0 抛 ValueError）
  3. per-session `asyncio.Lock` 串行化 request 检查-登记临界区（防竞态突破上限）
  4. sender 与 future 用 `asyncio.wait(..., FIRST_COMPLETED)` 竞速——审批通道故障**不等同**于超时（各自独立路径）
  5. 超时路径顺序固定：先 `_discard_pending`（释放容量）→ `_notify_expiry`（通知 UI）→ 抛 `SubagentApprovalTimeout`
- **N7（审批 id 与审计）**：`approval_id = f"subagent_{kind.value}_{uuid.uuid4().hex}"`——kind 分类（TOOL_PERMISSION / Skill 委托）可审计。`resolve` 跨线程用 `loop.call_soon_threadsafe` 完成 future；scope 不匹配仅 warning 不抛（不误杀他人 future）。
- **N8（UI 终态通知不反阻塞）**：`_notify_expiry` 内部 1s 超时（`_EXPIRY_NOTIFY_TIMEOUT_SECONDS = 1.0`）+ 异常吞掉——UI 卡死不得拖住超时收口。
- **N9（审批统一主会话下发）**：子代理审批（委托门禁 + 工具权限）全部经 `emit_subagent_approval` → 父 session `write_stream` 下发 `chat.ask_user_question`（payload 带 `source: subagent_{kind}` / request_id=approval_id）。默认 sender（`build_context_approval_sender` / `build_context_expiry_sender`）**发送时**从 `_subagent_parent_session` ContextVar 取父 session（该 ContextVar 由父 `StreamEventRail.before_tool_call` 在 task_tool 调用期间绑定）——不在构造期捕获。父 session 缺失：委托 sender 抛错 → rail 按审批通道异常 fail-closed。
- **N10（执行期裁决手工合成引擎）**：agent-core `PermissionEngine` 无 session/overlay 钩子——`SubagentPermissionRail._adjudicate_config` 手工合成：生效配置（全局 + 会话 overlay）+ 当前 ACTIVE Grant overlay_snapshot → `compose_skill_permissions` → 以**临时引擎**裁决（`PermissionEngine(config=effective, workspace_root=模板)`）。注入的 `engine` 仅作模板（复用 workspace_root / trusted_dirs），本身不感知 Grant。配置读取失败按空配置处理（全 ASK）。
- **N11（owner_scopes 只收紧）**：owner scope 在引擎结果（含 Skill overlay）**之后**应用且只升压不降压（`_tighten_permission_level` 按 ALLOW<ASK<DENY 序）。owner 上下文存在但评估异常 → **DENY**（fail-closed，matched_rule=`owner_scopes:evaluation_error`）。
- **N12（EXCLUDED_TOOLS_SPAWN 深防御）**：主 Agent 专属工具兜底集合（26 项：spawn_subagent / send_file_to_user / office_claw_* 调度 / 消息 / 计划任务 / 记忆 / 会话链 / 技能管理）命中即**无条件拒绝**——不受动态授权开关影响。dev-stable 装配层已从子 Agent 工具列表剔除这些工具；该集合仅防异常路径（继承异常 / 直接注入）。
- **N13（拒绝指纹与强制收口）**：同一调用（skill_name + tool_name + 归一化 tool_args 的 sha256 指纹）被拒绝 / 审批超时后入 `_rejected_call_fingerprints`——重试同调用直接拒绝（`[PERMISSION_DENIED] 相同的子 Agent 工具调用已被拒绝或审批超时`）。before hook 不能直接 `request_force_finish`（agent-core 会跳过 railed 方法体返回 None 破坏工具结果二元组）——置 `ctx.extra["_subagent_force_finish"]`，**after hook** 再 `request_force_finish`。指纹集在 root skill load / skill complete 时清空。
- **N14（审批答案复核三条件）**：`_resolve_ask` 拿到答案后复核——①功能仍开启；②`authorization_generation` 未变（配置已变更则拒绝）；③`get_active_skill_execution` 仍等于发起时 skill_name（scope 已结束 / 变化则拒绝）。答案判定：`selected_options` 含 "本次允许"/"允许" → approve；含 "拒绝" → 记指纹 + reject。审批超时 → 记指纹 + reject。通道异常（Cancelled / Capacity / 通道 Exception）→ reject 但**不**记指纹。
- **N15（数字分身豁免）**：`TOOL_PERMISSION_CONTEXT` 为 `group_digital_avatar` scene 时 rail 直接放行（数字分身使用既有专用裁决，动态授权不得接管）。
- **N16（审批卡预览有界）**：`_format_tool_args_preview` 命令类工具取 command/cmd/script/input 首个非空；否则 JSON 序列化；超 `_APPROVAL_PREVIEW_MAX_CHARS = 2000` 截断加省略号——审批卡不允许无界膨胀。

## 接口契约

### wiring 层（jiuwenswarm subagent_executor/authorization.py）

| 函数 | 契约 |
|---|---|
| `install_subagent_authorization_wiring(agent, config_provider=None) -> bool` | N1 幂等；包装 create_subagent |
| `attach_subagent_authorization(child, agent_scope_id, session_id, config_provider=None) -> bool` | N5 幂等 + 已初始化跳过 |
| `build_subagent_authorization_rails(...) -> tuple` | N4 原子对或空元组 |
| `cleanup_skill_authorization_scope(agent_scope_id, session_id)` | N5 best-effort 幂等 |
| `resolve_main_authorization_session(config_provider=None) -> str \| None` | N2 main scope 唯一准入 |
| `skill_authorization_enabled(config_provider=None) -> bool` | 总开关；读取异常按关（保 legacy 路径） |

装配点：`interface_deep._bind_subagent_authorization_wiring` @7193（config_provider 用 `_resolve_skill_authorization_base_config`）。装配异常只 warning。

### `SubagentApprovalRegistry`（agent-core）

| 成员 | 契约 |
|---|---|
| `get_instance()` / `peek_instance()` / `reset_instance_for_tests()` | 进程级单例三件套 |
| `request(kind, session_id, agent_scope_id, tool_call_id, payload, sender, expiry_sender, timeout)` | N6 五重约束；返回审批答案 |
| `resolve(...)` | 跨线程 `call_soon_threadsafe` 完成 future；scope 不匹配仅 warning |
| 异常族 | `SubagentApprovalTimeout` / `SubagentApprovalCancelled` / `SubagentApprovalCapacityError` |

### `SubagentPermissionRail`（jiuwenswarm，`priority = 90`）

构造：`agent_scope_id` / `engine`（模板）/ `grant_store` / `approval_registry` / `approval_sender` / `approval_expiry_sender` / `approval_timeout=120.0` / `config_provider` / `session_id` / `workspace_root`。`set_sessions_root` 只传 trusted parent 身份（跨 child callback task）。

挂点：`before_tool_call`（N12 深防御 → N15 豁免 → scope 解析 → ACTIVE skill 判定 → gate call 豁免 → N10 合成裁决 → ALLOW/DENY/ASK 三分）；`after_tool_call`（指纹清理 + N13 强制收口）。

### `SubagentSkillAuthorizationRail`（jiuwenswarm）

委托门禁（Skill 加载授权）——骨架由 agent-core `SkillAuthorizationRail` 模板钩子传导；scope 判定 N3；审批下发 N9。scope 与 sender 共享 `_resolve_subagent_scope` / `build_context_*_sender`。

## 数据结构

- 审批请求：`SubagentApprovalRequest(kind, session_id, agent_scope_id, tool_call_id, payload, approval_id)`——payload 含 tool_name / tool_args / skill_name / reason
- 调用指纹：`sha256({skill_name, tool_name(TOOL_NAME_ALIASES 归一), tool_args})`——不保留敏感原始参数
- 装配标记：父 `_jiuwenswarm_skill_authz_wiring_installed` / 子 `_jiuwenswarm_skill_authz_scope`

## 执行路径

1. `interface_deep` 装配 wiring（@7193）→ 父 create_subagent 被包装
2. TaskTool 调用 → 父 `StreamEventRail.before_tool_call` 绑定 `_subagent_parent_session`
3. create_subagent 触发 → main scope 判定（N2）→ attach（N5）→ rail 对挂 pending（N4）
4. 子执行 Skill 加载 → gate rail 拦截 → `emit_subagent_approval` 委托审批（N9）
5. 子执行工具 → permission rail 裁决（N10-N12）→ ASK → registry.request（N6）→ 用户答案 → 复核（N14）
6. 子 invoke/stream finally → scope 清理（N5）

## 异常分支

- 审批超时：记指纹 + reject（N13/N14）
- 审批通道异常：reject 不记指纹（N14）
- 审批容量满：`SubagentApprovalCapacityError` → reject
- 配置 / overlay 读取失败：空配置全 ASK（N10）；owner 评估异常 DENY（N11）
- wiring 装配失败：warning 吞掉，子代理照常创建（N1）

## 边界 case

- 子 Agent 已初始化后才 attach：跳过 + 告警（N5）
- 父 session ContextVar 缺失（非 task_tool 路径）：sender 抛错 → fail-closed（N9）
- KV cache 亲和下失败 sticky 子代理：SDK 清理驱逐缓存而非复用（execution_timeout 契约，[[S_14]]）
- 同 skill 同工具同参数重试：指纹命中直接拒绝（N13）
- 审批挂起期间配置变更 / skill 结束：答案到达也拒绝（N14 三条件）

## 修改检查单

改 authorization.py / 两个 rail / registry 前：

- [ ] N2 main scope 准入未被放宽（防授权继承）？
- [ ] N6 容量约束是否仍五重齐备（尤其 asyncio.Lock 临界区）？
- [ ] 超时路径顺序（释放容量 → 通知 → 抛错）未颠倒（N6.5）？
- [ ] before hook 仍不直接 force_finish（N13 二元组保护）？
- [ ] 指纹集清理时机（skill load/complete）未漏？
- [ ] EXCLUDED_TOOLS_SPAWN 与装配层工具剔除清单同步（N12）？
- [ ] 新增审批 kind 是否更新 approval_id 审计格式（N7）？
- [ ] 复核三条件（N14）在答案延迟到达场景仍全部执行？

## spec 关系

- 上游：[[S_11]]（wiring 包装 create_subagent）
- 下游：[[S_14]]（task_tool 通道绑定父 session ContextVar 的时序前提）
- 跨域：主 Agent 侧 skill 授权与权限体系同源（agent-core skill_authorization / security）；中断交互（chat.ask_user_question 下发与回复）归中断交互域；harness 域 [[S_08]] 错误检测器的 `[ERROR]:` 前缀与本域 `[PERMISSION_DENIED]` 前缀互不消费
