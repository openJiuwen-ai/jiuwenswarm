# S_04 — 计划审批：exit_plan_mode 审批门与 Web/TUI 动作分叉

| | |
|---|---|
| Spec ID | S_04 |
| 状态 | 当前实现的真实描述（含未装配遗留机制的登记） |
| 修订日期 | 2026-10-28 |
| 关联模块 | `jiuwenswarm/agents/harness/code/rails/code_plan_approval_interrupt_rail.py`（p78 现行审批门，357 行）<br>`jiuwenswarm/agents/harness/code/prompt/plan_approval.py`（常量、Web 三动作描述符、意图分类，368 行）<br>`jiuwenswarm/agents/harness/code/rails/code_agent_mode_rail.py`（批准后的模式恢复 / 正文压缩 / 延迟收尾消费面，325 行）<br>`jiuwenswarm/agents/harness/work/rails/work_agent_mode_rail.py`（76 行）+ `work/prompt/work_plan_prompts.py` 的 `WORK_PLAN_ALLOWED_TOOLS`（work profile plan 白名单）<br>`jiuwenswarm/agents/harness/code/rails/code_plan_approval_rail.py`（p76 文本审批，**未装配遗留**，226 行） |
| 装配点 | 三处，均返回同一个 `PlanApprovalInterruptRail()` 实例类：① `server/runtime/agent_adapter/interface_deep.py` `_build_work_plan_approval_rail()`（~10580，`_RailBuildInfo("_work_plan_approval_rail")` ~10800，deep 与 work profile 共用）；② `server/runtime/agent_adapter/interface_code.py` `_build_plan_approval_rail()`（~1339），并在 `_get_current_agent_rails`（~1353-1369）纳入热重载覆盖；③ `agents/swarm/providers/code_rails.py` `build_team_plan_approval`（~375，`_is_team_plan_leader` 门控，team.plan leader 复用） |
| 消费点 | `server/runtime/agent_adapter/interface.py`（~2256-2285 Web 动作 → confirm 载荷映射；~97/253 reminder 原文还原）<br>`agents/harness/common/rails/interrupt/interrupt_helpers.py`（~1078 ask_user 载荷增强）<br>`server/handlers/_shared.py`（~645 进 plan 轮记录 original_query）<br>`server/handlers/_default.py`（~201 消费 `PLAN_MODE_EXITED_EVENT_TYPE`） |

## 1. 范围与边界

本 spec 管 **"exit_plan_mode 的用户审批门，以及批准 / 拒绝 / 跳过 / 执行四态在 Web 与 TUI 上的语义分叉"**——为什么与怎么做：

- 约束链：plan 模式只读（[[S_04-N13]] 白名单 + bash 写正则拦截）→ 模型完成规划后调 `exit_plan_mode` 提交 → **工具体执行前**弹审批（对齐 Claude Code 的即时交互）→ 按用户选择恢复或保持 plan
- 现行机制是 p78 `PlanApprovalInterruptRail`（中断弹窗）：审批、模式恢复、Web「执行」的延迟收尾全部在一次中断 + 恢复内闭环
- Web 的三个按钮把"批准"拆成两条路径：执行（结束本轮 + 前端补发新消息）与跳过（留在 plan + 结束本轮），避免 Web 流式界面里"批准后同轮狂奔"
- TUI 行为刻意不动：approve 同轮继续实现
- work profile 的 plan 白名单是本域的安全边界（task_tool 刻意排除）

**不管**（指向其它 spec / 域）：

- plan 模式的进出提示注入、会话状态同步（`_ensure_code_mode_state`）、plan_sub_mode 路由与 agent 选择 → server 编排域（`test_plan_mode_orchestration.py` 覆盖面，不在任务规划 spec 集内）
- `server/runtime/agent_adapter/plan_pause_helpers.py`（名字含 plan 但实为 **cancel 后"恢复 vs 新任务"的路由**）→ 中断交互域（未归档）
- todo 收口 → [[S_03]]
- `openjiuwen` 的 `AgentModeRail` 父类通用机械（`DEFAULT_PLAN_MODE_ALLOWED_TOOLS` 基线、switch_mode 解析）→ agent-core 仓文档；本 spec 只登记 jiuwenswarm 侧的定制接缝

## 2. 核心不变量

**N1 审批门在工具体执行前，优先级三明治**
`PlanApprovalInterruptRail(ConfirmInterruptRail)`，`priority = 78`，`tool_names=["exit_plan_mode"]`——先于通用确认门 `CodeConfirmInterruptRail(80)` 抢占、后于 `AgentModeRail(85)`（plan 约束仍激活）。首次 `resolve_interrupt`（`user_input is None`）读计划文件构造审批消息并 `interrupt(InterruptRequest(..., auto_confirm_key="exit_plan_mode"))`；`init()` 绑定 `self._agent` 供读计划文件，`uninit()` 释放。

**N2 模式恢复发生在批准后的 resumed 调用内，而非下一轮**
批准 → 中断恢复 → `ExitPlanModeTool.invoke()` 真正执行并在体内调 `restore_mode_after_plan_exit()`。这是对齐 Claude Code 的关键设计：不依赖"下一条用户消息"来恢复模式（p76 文本机制的做法，见 N15）。拒绝 → 工具体被 `_skip_tool` 拦截、模型收到反馈错误、留在 plan 继续修订。

**N3 Web「执行」= 批准 + 本轮收尾 + 前端补发**
`plan_execute` 取值映射为 `{approved: True, plan_execute: True}`——`plan_execute` 是 ConfirmPayload 的**额外键**（N6）。rail 在决策非 RejectResult 后置 `ctx.extra[PLAN_EXECUTE_CTX_KEY] = True`；`CodeAgentModeRail.after_tool_call` Segment 4 检测到该标记即 `ctx.request_force_finish({"output": "", "result_type": "answer"})` 结束本轮。**真正的执行由前端紧接着补发的一条普通非 plan 消息开启全新一轮**——若同轮继续，本轮与前端补发轮会并发。

**N4 Web「跳过」= reject 通道 + 强制结束本轮 + 留在 plan**
`plan_skip` 映射为 `{approved: False, feedback: custom_input or plan_skip_feedback(lang), plan_skip: True}`。rail 走 RejectResult 分支：置 `ctx.extra["_plan_rejected"]`（N9）与 `"_plan_skipped"`，随即 `ctx.request_force_finish(PLAN_SKIP_TURN_OUTPUT[lang])`（result_type="answer"，中文文案"已跳过本次计划审批。计划已保存，当前仍处于计划模式…"）。不强制结束的话，模型收到普通 reject 会继续思考并**再次调用 exit_plan_mode**，弹窗循环。

**N5 Web「修订」与 TUI 的 approve/reject 都同轮继续**
Web revise：`reject` + `custom_input` → `approved=False, feedback=修改意见`，无任何额外键。TUI：approve（纯 `approved=True`，无 plan_execute 键，同轮继续实现）与 reject（`feedback="用户拒绝"`）行为与引入 Web 分叉前完全一致。

**N6 额外键机制：rail 在父类校验前从原始 dict 读**
`plan_execute` / `plan_skip` 是 ConfirmPayload 不认识的键（`ConfirmPayload` 反序列化会忽略它们）；真正承载它们的是 `interface.py` 选项映射处放进 confirm dict 的布尔值 + rail 在调用 `super().resolve_interrupt()` **之前**从原始 `user_input` dict 提取的局部变量。TUI 与其它确认流不产生这两个键，路径完全不受影响。

**N7 `plan_execute` 标记设在拿到决策之后**
`if plan_execute and not isinstance(decision, RejectResult)` 才置 `PLAN_EXECUTE_CTX_KEY`——父类可能把这次恢复判成拒绝（如 payload 校验失败），那时 exit_plan_mode 不会执行，本轮不该被"执行"标记提前结束。

**N8 `PLAN_SKIP_OPTION_VALUES` 只认精确值**
`frozenset({"plan_skip"})`，不放 "skip"/"跳过" 这类通用词。消费它的 if/elif 链是**所有确认类中断共用**的选项映射，别的确认流若出现"跳过"按钮会被误判成计划跳过。`PLAN_EXECUTE_OPTION_VALUES` 同理（`frozenset({"plan_execute"})`）。

**N9 `_plan_rejected` / `_plan_skipped` 是持久标记**
拒绝时 `_skip_tool()` 置的 `ctx.extra["_skip_tool"]` 会被 `ability_manager._railed_execute_single_tool_call` 在 `after_tool_call` 运行前 **pop** 掉——`CodeAgentModeRail` 改读 rail 设置的 `_plan_rejected`（贯穿 pop 存活）。skipped 是它的伴生标记（当前无独立消费方，语义备查）。

**N10 挂起等待审批时不拆 plan 模式**
`@rail` 装饰器从 `finally` 块触发 AFTER_TOOL_CALL——中断挂起时该钩子也会跑，但工具体未执行、用户未回答。`CodeAgentModeRail._is_pending_user_interrupt(ctx)`（`extract_tool_interrupt(ctx.exception)` 非 None）判定挂起即提前 return，不做任何注销 / 恢复 / 压缩。（`ToolCallResilienceRail` 会先给 tool_result 填失败占位，`tool_result is not None` 无法区分挂起与执行，必须看 ctx.exception。）

**N11 执行判据从上游模板推导 + 空计划兜底恢复**
`_derive_exit_plan_markers()` 从 openjiuwen `agent_mode_tools._EXIT_PLAN_EMPTY_MSG` / `_EXIT_PLAN_WITH_CONTENT_PREFIX` 模板推导"结果开头"与"计划正文标题"两组判据（措辞变了自动跟随）；常量丢失则 warning + 退回捆绑兜底值。残余盲区：上游改掉文案生成方式（不经由这两个常量）时推导仍"成功"且无 warning，线上表象是 Web「执行」轮不结束、task_tool 不注销。`_exit_plan_tool_executed` 用"结果开头"判定工具真执行；执行了但 state 仍 plan（空计划早退不恢复）→ 兜底 `restore_mode_after_plan_exit()`。

**N12 计划正文压缩：echo 只留 header 和 reminder**
`_compact_exit_plan_result` 把 `exit_plan_mode` 结果里 "## Plan:" / "## 计划：" 起到 `<system-reminder>` 之前的正文抽掉。模型写计划时已有全文在上下文，用户刚在 UI 里批准过，重读一遍只会诱导模型"再总结一遍计划并询问是否开始"而非直接执行。

**N13 work profile plan 白名单：白名单制 + 刻意排除**
`WORK_PLAN_ALLOWED_TOOLS`（15 项）：`enter_plan_mode` / `exit_plan_mode` / `ask_user` / `read_file` / `grep` / `list_files` / `glob` / `bash` / `write_file` / `edit_file` / `web_search` / `web_free_search` / `web_paid_search` / `web_fetch` / `web_fetch_webpage`。设计要点：① 白名单而非黑名单——工具集随配置 / 技能 / MCP 动态增长，黑名单会漏；② **排除 task_tool**——子代理有独立 session 与 plan_mode 状态，父 agent 的 plan 约束不传播，委派即可绕开只读约束（评审定论；待上游给 TaskTool 下发 plan 标记后再评估恢复）；③ 排除 `send_file_to_user` / `cron_create` / `switch_mode`（work 的 plan 开关由服务端控制）；④ 与 openjiuwen 基线 `DEFAULT_PLAN_MODE_ALLOWED_TOOLS`（12 项，**含 task_tool 与 switch_mode**，无 web 五件）的差异即 work 的定制面。`WorkAgentModeRail` 直接继承 `CodeAgentModeRail`，构造时只换 `allowed_tools` 与 `exit_plan_notification`；`before_model_call` 在**非 plan 态**追加隐藏 `switch_mode`（plan 态本就被 N14 的 switch_mode 拦截覆盖）。

**N14 plan 态双闸：switch_mode 退出拦截 + bash 写操作正则**
`CodeAgentModeRail.before_tool_call`：plan 态下 `switch_mode` 目标为 normal/auto 即拒绝（"请先调用 exit_plan_mode 提交计划等待审批"）——模式退出必须过审批门，模型不能单方面走；bash 命令命中 `_NON_GIT_WRITE_RE`（mkdir/touch/mv/cp/chmod/chown/dd/tee/wget/curl -O/rm -rf/压缩解压/`>`/`>>` 重定向）即拒绝。`write_file`/`edit_file` 不在拦截之列（计划文件本身需要写）。

**N15 p76 文本审批是未装配的遗留实现（如实登记）**
`code_plan_approval_rail.py` 的 `PlanApprovalRail(DeepAgentRail)`（priority=76，after_tool_call 检测 exit_plan_mode 成功执行 → 抽取计划正文 → 若模式已被提前恢复则 `switch_mode(mode="plan")` 强制回 plan → 存 `agent._plan_approval_state` → tool_result 追加 `PENDING_APPROVAL_MARKER` 等下一条用户消息裁决）——**全仓无任何 import 消费**。其配套面同样无生产消费：`PENDING_APPROVAL_MARKER`（仅该遗留 rail 引用）、`APPROVED_NOTIFICATION` / `FEEDBACK_INJECTION` / `PLAN_USER_APPROVED_FLAG` / `PLAN_APPROVAL_EVENT_TYPE` / `APPROVE_CMD_PREFIX` / `REJECT_CMD_PREFIX` / `classify_plan_user_intent` / `is_user_approving` / `is_direct_plan_implement_request` / `extract_feedback_from_reject`（后五个仅测试引用，见 §7）。**现行机制只有 p78**。修改检查单建议清理（§9-1）。

**N16 UI 接缝三则**
① **预览截断**：审批消息中计划预览超 `_MAX_PLAN_PREVIEW_CHARS = 3000` 字截断加 "… (truncated)"——只影响弹窗显示，不影响计划文件本体；② **载荷增强四键**：`interrupt_helpers` 在 `source=="confirm_interrupt" and tool_name=="exit_plan_mode" and is_plan_approval_message` 时给 ask_user 载荷注入 `plan_content` / `plan_language` / `plan_approval_kind="plan_approval"` / `plan_actions=build_plan_approval_actions(lang)`（Web 据此渲染三按钮，TUI 不读 plan_actions 仍用 questions[].options）；③ **reminder 原文还原**：进 plan 轮注入的 `<system-reminder>` 拼在 query 前，`_shared.py` 记录 `PLAN_REMINDER_ORIGINAL_QUERY_KEY`，写会话历史时还原用户原文——否则刷新页面会把提示词当用户提问显示。

## 3. 接口契约

### 3.1 PlanApprovalInterruptRail（p78，code_plan_approval_interrupt_rail.py）

| 入口 | 签名要点 | 行为 |
|---|---|---|
| `resolve_interrupt(ctx, tool_call, user_input, auto_confirm_config)` | → 中断 / 决策 | 首调（N1）读文件构造消息；resume：先从原始 dict 提 plan_skip/plan_execute 两布尔（N6）→ 委托父类 → 决策后按 N3/N4/N7/N9 置标记 |
| `_read_plan_content(ctx)` | → `str` | `agent.get_plan_file_path(session)`；agent/session/路径缺失或 OSError 一律返回 ""（空模板兜底） |
| `_build_approval_message(plan_content, language)` | → `str` | 有内容用审批模板、空用空计划模板；预览截断（N16-①） |
| `_detect_language()` | → `"cn"`/`"en"` | 读 `agent.system_prompt_builder.language`，异常一律 "cn" |

模块级四个公开函数（服务端消费面）：`is_plan_approval_message`（标题判定 `**计划审批**` / `**Plan Approval**`）、`strip_inline_plan_approval_choices`（按 `_PLAN_CHOICE_MARKERS` 去掉文本按钮清单）、`extract_plan_approval_content` → `(plan_content, language)`（解析审批消息回抽计划正文）、`build_plan_approval_options_from_message` → approve/reject 结构化选项（TUI 侧）。

### 3.2 plan_approval.py 常量与函数（现行消费面）

| 符号 | 值 / 签名 | 消费方 |
|---|---|---|
| `PLAN_SKIP_PAYLOAD_KEY` | `"plan_skip"` | rail（N6 提取）+ interface.py（N4 载荷） |
| `PLAN_EXECUTE_PAYLOAD_KEY` | `"plan_execute"` | 同上（N3） |
| `PLAN_SKIP_OPTION_VALUES` | `frozenset({"plan_skip"})` | interface.py 选项映射（N8） |
| `PLAN_EXECUTE_OPTION_VALUES` | `frozenset({"plan_execute"})` | 同上 |
| `PLAN_EXECUTE_CTX_KEY` | `"_plan_execute_deferred"` | rail 置 → CodeAgentModeRail 消费（N3） |
| `PLAN_SKIP_TURN_OUTPUT` | `{"cn": ..., "en": ...}` | rail force_finish 输出（N4） |
| `PLAN_SKIP_FEEDBACK` / `plan_skip_feedback(lang)` | 中英双语文案 / 取值函数 | interface.py（skip 无 custom_input 时给模型的反馈） |
| `PLAN_REMINDER_ORIGINAL_QUERY_KEY` | `"_plan_reminder_original_query"` | _shared.py 记录 / interface.py 还原（N16-③；interface 侧 getattr 防御式导入） |
| `PLAN_MODE_EXITED_EVENT_TYPE` | `"plan.mode_exited"` | _default.py:201（批准退出后推给客户端的事件） |
| `build_plan_approval_actions(lang)` | → 三动作描述符列表 | interrupt_helpers 载荷增强（N16-②） |
| `_PLAN_APPROVAL_ACTIONS` | execute(`requires_input="no"`)/skip(`"empty"`)/revise→reject(`"yes"`) | 结构与语言无关，label 才分语言 |

### 3.3 CodeAgentModeRail 审批侧四段（after_tool_call）

| 段 | 行为 |
|---|---|
| 挂起守门 | `_is_pending_user_interrupt` → 提前 return（N10） |
| Segment 1 | `enter_plan_mode` 成功 → `_register_task_tool`；`exit_plan_mode` 未拒绝且真执行 → `_unregister_task_tool`（判定 N9 + N11） |
| Segment 2 | exit 成功执行但 state 仍 plan → 兜底 `restore_mode_after_plan_exit`（N11） |
| Segment 3 | `_compact_exit_plan_result`（N12） |
| Segment 4 | `PLAN_EXECUTE_CTX_KEY` → `request_force_finish({"output": "", "result_type": "answer"})`（N3） |

### 3.4 Web 动作 → confirm 载荷映射（interface.py 选项 if/elif 链）

| 回传 selected | 载荷 | 后续 |
|---|---|---|
| `plan_execute` | `{approved: True, plan_execute: True}` | N3 延迟执行 |
| `plan_skip` | `{approved: False, feedback: custom_input or plan_skip_feedback(lang), plan_skip: True}` | N4 跳过 |
| `reject`（含 Web revise） | `{approved: False, feedback: ...}` | 同轮继续修订 |
| `approve`（TUI） | `{approved: True}` | 同轮继续实现 |

### 3.5 p76 遗留面（登记，不鼓励使用）

`PlanApprovalRail`（priority=76）+ `PlanApprovalState(pending, plan_slug, plan_content, plan_path)` + `agent._plan_approval_state` + N15 列出的常量与函数族。任何新代码**不得**建立对它们的依赖。

## 4. 数据结构

**ctx.extra 键**（审批域生命周期 = 单次 tool_call 恢复链）：

| 键 | 置者 | 读者 | 含义 |
|---|---|---|---|
| `PLAN_EXECUTE_CTX_KEY`（`_plan_execute_deferred`） | p78 rail（N7 后置） | CodeAgentModeRail Segment 4 | Web 执行：本轮收尾 |
| `_plan_rejected` | p78 rail | CodeAgentModeRail（贯穿 `_skip_tool` pop） | 拒绝：跳过注销/恢复/压缩 |
| `_plan_skipped` | p78 rail | （暂无） | 跳过伴生标记 |

**Web 三动作描述符**：`{"kind": "execute", "value": "plan_execute", "requires_input": "no"} / {"kind": "skip", "value": "plan_skip", "requires_input": "empty"} / {"kind": "revise", "value": "reject", "requires_input": "yes"}` + 按语言的 label（cn：执行/跳过/下一步；en：Execute/Skip/Next）。`requires_input` 语义：no=无需输入框，empty=框空时才显示该按钮，yes=必须带输入。

**审批消息模板**：四份（cn/en × 有内容/空计划）；标题常量 `_PLAN_APPROVAL_TITLE_CN/EN` 是 `is_plan_approval_message` 的判定依据——**改标题即破坏载荷增强门控**（N16-②），需同步。

## 5. 执行路径全景

**P1 Web「执行」（deferred 路径）**：模型调 exit_plan_mode → p78 拦截 → 读计划文件 → 审批消息（截断预览）→ interrupt 挂起 → Web 渲染三按钮（载荷增强）→ 用户点「执行」→ interface 映射 `{approved: True, plan_execute: True}` → rail resume：提 plan_execute=True → 父类决策 approve → `ctx.extra[PLAN_EXECUTE_CTX_KEY]=True` → exit_plan_mode 真执行（体内恢复模式）→ AFTER_TOOL_CALL：非挂起 / 未拒绝 / 已执行 → 注销 task_tool → 兜底恢复（如需）→ 压缩正文 → **force_finish 结束本轮** → 前端补发普通消息 → 新一轮执行计划。

**P2 Web「跳过」**：同前置 → 用户点「跳过」（框空）→ `{approved: False, feedback: plan_skip_feedback(lang), plan_skip: True}` → rail：RejectResult 分支 → `_plan_rejected` + `_plan_skipped` → force_finish(PLAN_SKIP_TURN_OUTPUT) → exit_plan_mode 未执行、模式未动 → 本轮结束，仍在 plan。

**P3 Web「修订」/ TUI「拒绝」**：`{approved: False, feedback: 修改意见}` → RejectResult → `_plan_rejected` → 工具体被拦，模型收到 feedback 错误 → 同轮继续修订计划 → 再次 exit_plan_mode 回到 P1 前置。

**P4 TUI「批准」（同轮继续）**：`{approved: True}`（无 plan_execute 键）→ 决策 approve → 不置任何标记 → exit_plan_mode 真执行 → 注销 task_tool → 恢复 / 压缩 → **无 force_finish** → ReAct 同轮继续实现计划。

**P5 空计划退出**：计划文件不存在/读失败 → `_read_plan_content` 返回 "" → 空计划模板（"批准：退出 plan 模式（无计划内容）"）→ 批准后工具执行但早退不恢复 → Segment 2 兜底 `restore_mode_after_plan_exit`。

**P6 enter_plan_mode（进入面）**：成功执行 → `_register_task_tool`（openjiuwen 基线行为：plan 态注册子代理工具供规划期调研）→ 服务端注入 reminder + 记录 original_query（N16-③）。

**P7 team.plan leader**：`build_team_plan_approval` 以 `_is_team_plan_leader` 门控，leader 复用同一个 p78 rail 类；非 leader 不装配（团队计划只有 leader 提交审批）。

**P8 code 侧热重载**：`_get_current_agent_rails` 把 `_code_plan_approval_rail` 纳入覆盖集——rails 配置变更后无需重建 agent 即可生效。

## 6. 异常分支

| 分支 | 触发 | 行为 |
|---|---|---|
| E1 计划文件读失败 | 路径缺失 / OSError / agent 未绑定 | 返回 ""，走空计划模板；warning 日志 |
| E2 上游模板推导失败 | openjiuwen 常量改名/删除 | `_derive_exit_plan_markers` 退回捆绑兜底值 + warning（N11 残余盲区：生成方式改变无 warning） |
| E3 payload 校验失败 | 父类把恢复判成 RejectResult | 不置 `PLAN_EXECUTE_CTX_KEY`（N7）；走拒绝路径 |
| E4 拒绝后的 AFTER_TOOL_CALL | `_plan_rejected=True` | Segment 1/2/3/4 全跳过：不注销 task_tool、不恢复模式、不压缩 |
| E5 挂起期用户离开/取消 | 中断未 resume 即请求结束 | 模式未动（N10）；后续恢复路由属 plan_pause（域外，§1） |

## 7. 边界 case 清单（测试锚点）

| case | 锚点 |
|---|---|
| Web 执行：批准 + 延迟标记 | `tests/unit_tests/test_web_plan_mode.py::test_execute_action_approves_and_defers_execution` |
| TUI approve 不带 plan_execute | 同文件 `::test_tui_approve_is_unchanged` |
| Web 跳过：拒绝 + 标记 | 同文件 `::test_skip_action_rejects_and_flags_force_finish` |
| Web 修订：feedback 透传、无 plan_skip | 同文件 `::test_revise_action_rejects_and_carries_feedback` |
| TUI reject 不带 plan_skip | 同文件 `::test_tui_reject_is_unchanged` |
| 三按钮结构与取值 | 同文件 `::test_plan_approval_actions_describe_three_web_buttons` / `::test_plan_approval_actions_differ_only_by_label` |
| 跳过取值不吃通用词 | 同文件 `::test_skip_option_values_do_not_capture_generic_words` |
| skip feedback 按语言 | 同文件 `::test_plan_skip_feedback_follows_language` |
| reminder 不进历史 / 无 reminder 回退 | 同文件 `::test_plan_activation_reminder_is_hidden_from_history` / `::test_history_falls_back_to_query_without_plan_reminder` |
| work 白名单排副作用工具 | 同文件 `::test_work_plan_whitelist_excludes_side_effect_tools`（断言 task_tool/send_file_to_user/cron_create/switch_mode 不在） |
| work rail 非 plan 态隐藏计划工具 / plan 态保留 | 同文件 `::test_work_rail_hides_plan_tools_outside_plan_mode` / `::test_work_rail_keeps_plan_tools_in_plan_mode` |
| 执行判据追踪上游模板 / 常量消失退兜底 | 同文件 `::test_exit_plan_markers_track_openjiuwen_templates` / `::test_exit_plan_markers_fall_back_when_upstream_constants_vanish` |
| plan 态 switch_mode 退出拦截 | `tests/unit_tests/agentserver/test_code_agent_mode_rail.py::test_before_tool_call_blocks_switch_mode_exit_in_plan_mode` |
| plan 态 bash 写拦截 | 同文件 `::test_before_tool_call_blocks_non_git_write_in_plan_mode` |
| 挂起期保持 plan（两种路径） | 同文件 `::test_after_tool_call_keeps_plan_mode_while_approval_is_pending` / `::test_after_tool_call_skips_teardown_while_awaiting_approval` |
| 拒绝后保持 plan | 同文件 `::test_after_tool_call_keeps_plan_mode_when_user_rejected` |
| Web 延迟执行收尾 / TUI 批准同轮继续 | 同文件 `::test_after_tool_call_finishes_turn_for_web_deferred_execution` / `::test_after_tool_call_keeps_tui_turn_running_after_approve` |
| 正文压缩 / 空计划结果保持 / 兜底恢复（两条） | 同文件 `::test_after_tool_call_drops_echoed_plan_body` / `::test_after_tool_call_keeps_empty_plan_result_intact` / `::test_after_tool_call_restores_mode_when_tool_executed` / `::test_after_tool_call_restores_mode_when_tool_left_it_in_plan` |
| init 不再 patch exit_plan_mode | 同文件 `::test_init_no_longer_patches_exit_plan_mode_invoke` |
| 意图分类四则（p76 遗留函数，防回归保留） | `tests/unit_tests/agentserver/test_plan_approval.py`（`test_direct_plan_implement_requires_strong_signal` / `test_classify_implement_intent_as_approve` / `test_classify_revision_intent` / `test_classify_mixed_revision_overrides_short_approval` / `test_skills_list_does_not_sync_code_mode`） |
| 编排接缝：批准退出 resume 识别 / 中断恢复跳过审批准备 | `tests/unit_tests/agentserver/test_plan_mode_orchestration.py::test_approved_plan_exit_resume_is_detected` / `::test_prepare_chat_turn_skips_approval_for_interrupt_resume` |
| 装配：work/deep 与 team | `tests/unit_tests/test_web_plan_mode.py`（adapter 选择族）+ `tests/agents/swarm/test_swarm_assembly.py` |

## 8. 修改检查单（改动本域必查）

1. **改审批消息标题**（`_PLAN_APPROVAL_TITLE_*`）：同步 `is_plan_approval_message` / `extract_plan_approval_content` / 载荷增强门控（N16-②）三处判据；有测试锚定吗？——标题判定无直接单测，改前先补。
2. **改 Web 按钮取值**（`plan_execute`/`plan_skip` 字符串）：同步 `PLAN_*_OPTION_VALUES`、interface.py 选项映射、前端 `InlineQuestionCard` / `useWebSocket` 三处。
3. **动 exit_plan_mode 的工具语义**（返回文案 / 恢复时机）：重跑 `test_exit_plan_markers_*` 两测；注意 N11 残余盲区。
4. **扩 work 白名单**：先过 N13 的排除论证（task_tool 恢复需上游 plan 标记）；同步 `test_work_plan_whitelist_excludes_side_effect_tools`。
5. **改 force_finish 输出**（PLAN_SKIP_TURN_OUTPUT / deferred 空 output）：确认 result_type 与前端对结束帧的渲染约定。
6. **清理 p76 遗留**（建议项）：删除 `code_plan_approval_rail.py` 与 N15 列出的无消费常量/函数时，同步删 `test_plan_approval.py` 中仅服务它们的测试；`PLAN_MODE_EXITED_EVENT_TYPE` 与 `PLAN_REMINDER_ORIGINAL_QUERY_KEY` 有真实消费，**不可**一并删。
7. **改 AFTER_TOOL_CALL 段序**：Segment 1 的 task_tool 注销依赖"真执行"判定，顺序不可提前到挂起守门之前。
8. **新增确认类中断选项**：检查取值不与 `PLAN_SKIP_OPTION_VALUES` / `PLAN_EXECUTE_OPTION_VALUES` 撞车（N8）。

## 9. 与其它 spec 的关系

- **[[S_01]]**：计划文件本体是 `get_plan_file_path` 指向的会话产物；审批预览只读不写。
- **[[S_02]]**：`exit_plan_mode` 批准后进入执行轮，执行轮的 todo 生命周期由 task_execution_rail 管；本 spec 的 force_finish 输出（answer 类型）会触发其收口判定（若 plan 执行轮有开放任务）。
- **[[S_03]]**：Web「执行」补发的新一轮若纯文本收尾且有未收口 todo，走 [[S_03]] 的 nudge 闭环；P1 的 force_finish 空 output 不含正文，不触发 hold/drop。
- **[[S_05]] / [[S_06]]**：各 profile 的 todo 变体与 deepresearch 落盘同属任务规划域，编号连续。
- **中断交互域**（未归档）：`ConfirmInterruptRail` 父类的中断/恢复协议、`plan_pause_helpers` 的 cancel 恢复路由、auto_confirm 机制。
- **server 编排域**（未归档）：plan_sub_mode 路由、`_ensure_code_mode_state` 状态同步、`plan.mode_exited` 事件推送链。
