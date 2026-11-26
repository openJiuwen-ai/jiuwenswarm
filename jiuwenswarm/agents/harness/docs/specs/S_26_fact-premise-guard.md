# S_26 事实前提守卫（年份 / 届次类事实题）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_26 / fact-premise-guard |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/agents/harness/common/rails/fact_premise_rail.py`（`FactPremiseRail` + 纯函数）；`jiuwenswarm/server/runtime/agent_adapter/deep/rail_factory.py`（`_build_fact_premise_rail` + `_config_bool`）；`jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`（`_build_agent_rails` 的 `_RailBuildInfo`）；`jiuwenswarm/server/runtime/agent_adapter/interface_flash.py`（`_FLASH_RAIL_KEEP` 白名单登记）；`jiuwenswarm/resources/config.yaml`（`execution_guard.fact_premise_guard`） |
| 关联模块（agent-core 仓，跨仓契约） | `openjiuwen/core/single_agent/agents/react_agent.py`（`_ALLOWED_LLM_CALL_KWARG_KEYS` / `_is_valid_tool_choice` / `_prune_unusable_tool_choice` / `_apply_llm_call_kwargs` 调用点）；`openjiuwen/core/foundation/llm/model_clients/base_model_client.py`（`_build_request_params` 中 `params.update(filtered_kwargs)` 覆盖 `tool_choice`）；`openjiuwen/core/single_agent/rail/base.py`（`UserMessageInputs.parts` 可变、`ModelCallInputs.react_iteration` 1-based、`AgentRail.priority` 降序执行、`inherit_to_subagents` 默认 True） |
| 最近一次修订日期 | 2026-11-25 |
| 关联 feature | 暂无（缺陷修复，见 [B_09](../bugfix/B_09_fact-premise-guard.md)） |

## 范围 / 边界

本规约管**用户提出年份 / 届次类事实问题时，回答前必须确认前提存在并检索核实**这一条链路：

- 用户消息侧：命中判据（形态 + 分级）+ `[事实核对]` 子句注入（`on_user_message`）
- 模型请求侧：可选的「首轮强制一次检索」`tool_choice`（`before_model_call`）+ 请求前剪枝（agent-core）
- 观测侧：① 命中但整轮没有检索、且本轮已产出最终答案 → warning；② **未命中判据**（无时间锚点 / 生产任务）但最终答案本身写出「窗口内年份 + 具体断言」且整轮零检索 → 另一条 warning（输出侧兜底）

不管：

- 工具调用边界的参数 / 结果守卫（路径修复、读文件校验、配额门禁）→ [[S_10]]
- LLM 调用失败后的重试与上下文溢出恢复 → [[S_07]]；`tool_choice` 的注入**不**经重试通道（见「异常分支」）
- 工具循环熔断 → [[S_08]]
- `web_flash` / `web_search` 的工具本体（检索实现、provider 选路）→ 各自模块文档
- 静态提示词 / 工具描述：本守卫不改 `runtime_prompt_rail` 的 `time` 段，也不改 `web_flash._description()`——单一机制，便于归因
- **flash 路径的覆盖**：`_fact_premise_rail` **已登记**在 `_FLASH_RAIL_KEEP`，deep / agent 与 flash 行为一致（注入 + 两侧 warning）。flash 是 relay-claw 办公单专家的默认聊天路径，也是 B_09 的线上复现渠道，因此不作为「deep 先行」的排除项；白名单变更会改变 `_profile_fingerprint`，既有 warm 实例重建一次属预期（见 P4）
- 模型的知识正确性本身：本守卫只提高「先核实」的概率并让漏检可观测，**不保证**答案正确

**范围排除甄别**：

1. 这不是 [[S_10]] 的输入侧守卫：S_10 的「输入侧」指**工具调用边界**（before 改参 / 拒绝 / after 归一），本条作用于**用户请求本体**（用户消息 + 首轮请求参数）。两者唯一的共同点是都挂在 `before_*` 钩子上。
2. 不改写用户原话：子句是**追加**到 parts 首位的新条目，原 query 原样保留（用户可见历史不变）。
3. 不碰 `flash/prompt/content/tools.py` 的「默认一个问题一个工具」总则：该文件参与静态提示词哈希审计；子句只说「核实优先于工具调用次数控制」，不引用任何 Flash 专属规则（deep / team 提示词里没有那条规则）。
4. `force_first_retrieval` 默认**关**：它让请求带 `tool_choice`，provider 不支持时整轮 400。开它是部署决策，不是代码默认；配置值按严格布尔解析，`"false"` 不会被当成真。

## 不变量

- **N1（判据是形态，不是话题表）**：命中需同时满足——(a) 出现时间锚点：`(19|20)\d{2}` 且不早于 `当前年 - 1`，或届次 / 相对年份词（今年 / 本年 / 明年 / 去年 / 后年 / 前年 / 本年度 / 本届 / 这届 / 上届 / 上一届 / 下一届 / 最近一届）；(b) 疑问形态：中文疑问词 / 语气词，或英文 wh- / 助动词（`What did AWS announce at re:Invent 2026` 没有 `?` 也算）。已定论的旧届次（如 2022 年世界杯）不命中——那是常识题，强搜只增延迟。
- **N1a（名词表只定级，不做开关）**：`_FACT_NOUNS` 命中 → level 2（完整子句，且是唯一可触发强搜的级别）；未命中但 N1 成立 → level 1（一行短句）。名词表是**分级信号**而非必要条件——闭集判据无法覆盖新事件名（「华为云 2025 Inspire 大会」「AWS re:Invent 2026」「WAIC」「CES」都曾整体静默），所以新场景不需要改代码，最坏也只是拿到短句。
- **N1b（先解包信封再判年份）**：OfficeClaw 把每轮用户消息包成 `你收到一条消息：{json}`，其中 `timestamp` 恒为当天 → 直接判整段文本会让「年份不早于当前年-1」恒真、`_PAST_YEAR_SLACK` 失效。只对 `content` 字段判定；前缀缺失 / JSON 坏 / 无 `content` → 回退原文（不猜）。
- **N2（指令不误伤）**：含编辑 / 生产动词（改成 / 改为 / 修改 / 删除 / 替换 / 重写 / 翻译 / 排版 / 写一份 / 实现 / 修复 / change / rewrite / edit / …）且非疑问形态 → 不命中，即使同时带年份与事件名词。`把 2028 年世界杯冠军那页改成 2026` 属于文档任务。
- **N3（只注 query 来源 + 精确幂等）**：`UserMessageInputs.source != "query"` 直接返回（steering / resume 批次是框架生成的，注入会改写内部指令）。仅当 `parts[0]` 与「cn / en × level 1 / 2」四种渲染结果之一**精确相等**时视为已注入、不重复；不做子串扫描——用户正文里援引 `[事实核对]` 字面量不应让守卫失效。
- **N4（原话保留）**：子句 `insert(0, ...)`，原 parts 逐条保持原对象与原位置（原 query 落在 `parts[1]`）；空 parts / 非 list 直接返回。
- **N5（fail-open）**：每个钩子各自整体 `try/except`，异常只 `logger.warning`（观测钩子静默），绝不上抛。守卫让一轮对话失败，比它要防的错误更糟。
- **N6（强制检索六重前置）**：仅当「命中 ∧ level 2 ∧ `force_first_retrieval` ∧ `force_tool_names` 非空 ∧ `react_iteration <= 1` ∧ 目标工具名出现在本次 `ctx.inputs.tools`」才写 `llm_call_kwargs["tool_choice"]`；写入用 `setdefault`（已有 choice 优先），并置 `_fact_premise_forced` 防重复。强搜是最侵入的杠杆，只给完整子句级别。
- **N7（priority=10 + 合并写）**：钩子链按 priority **降序**执行，`ThinkingInjectRail`(15) 先写 `extra["llm_call_kwargs"]`（覆盖式）。本 rail 排在它之后，必须**读旧 dict 再合并**，不得整体替换。
- **N8（请求前剪枝是最后防线）**：`before_model_call` 看到的工具列表并非最终列表——`disabled_tools_rail`(1) 等更低 priority 的 rail 之后还会删工具。agent-core 在真正发请求前用 `_prune_unusable_tool_choice` 兜底：无 tools 或强制函数不在 `tools` 中 → 删除 `tool_choice`，不让 provider 400。
- **N9（不继承子代理）**：`inherit_to_subagents = False`。子代理的 turn 是模型写的任务指令，不是用户提问。
- **N10（单一机制，可归因）**：本守卫只由 rail 承担，不改系统提示词与工具描述。旧版 core 丢弃 `tool_choice` 时降级为「用户消息注入」一层：模型仍会收到子句，只是没有确定性强制。
- **N11（可观测量）**：命中后整轮没有任何检索类工具调用（`web_*` / `*_search` / `fetch_webpage` / `browser_agent`），且本轮已产出最终答案（无 `tool_calls`、内容非空）→ `after_model_call` 打一条 warning（query 经 `preview_text` 截断、受 `logging.preview_user_content` 开关约束）。不用 `after_invoke`：deep 适配器只把本 rail 的事件桥接到内层 ReActAgent，`after_invoke` 留在外层 DeepAgent，`extra` 不是同一个 dict。也不用 `after_react_iteration`：core 只在**工具调用迭代**结束时触发它，最终无工具答案反而不会触发。
- **N12（输出侧兜底，零词表）**：输入侧按定义只覆盖「有时间锚点」的轮次；`re:Invent 有什么新发布`（无锚点）与 `写一段 2025 年大会发布稿`（生产任务）永远判不到，扩大词表只会重建闭集。因此 `after_model_call` 对**未命中**轮次再判一次最终答案：出现窗口内年份（复用 `_has_temporal_anchor` 的年份窗口）∧ 一个具体断言（日期 / 第 N 届 / 数量+单位 / 举办·发布·夺冠等）∧ 整轮零检索 → 一条 warning。命中判据的轮次走 N11，两条互斥（先判 `_fact_premise_hit`）。`_RETRIEVAL_TOOL_NAMES` 命中即置 `_fact_premise_retrieved`，`after_tool_call` 因此**不再要求命中标记**——否则未命中的轮次永远看不到「搜过了」。拒答形态（无法确认 / 查不到 / 不存在 / cannot confirm…）显式排除：那正是守卫想要的行为。误报代价是一行日志（`log_unclassified_claims` 可关）。

## 接口契约

### fact_premise_rail（纯函数）

| 成员 | 契约 |
|---|---|
| `fact_premise_level(text, *, today=None)` | N1/N1a/N1b/N2；返回 `0` 忽略 / `1` 短句 / `2` 完整子句；`today` 可注入以便边界用例稳定；非 str / 空串 → 0；只看 `content` 前 4000 字符 |
| `is_fact_premise_question(text, *, today=None)` | `fact_premise_level(...) > 0` 的布尔视图，保留给旧调用方 |
| `build_fact_check_clause(language="cn", *, level=2)` | 前缀 `[事实核对]` / `[Fact-check]`；level 1 为一行提醒（确认年份 / 届次存在、不套用相邻届次、无法确认就直说），level 2 另含「每 4 年一届」「不要编造」；两者都要求先确认前提再回答，完整子句含中性的「核实优先于工具调用次数控制」，不引用 Flash 专属规则 |
| `answer_asserts_year_fact(text, *, today=None)` | N12；非 str / 空 / 命中 `_REFUSAL_RE` / 无窗口内年份 / 无 `_ASSERTION_RE` 断言 → False；只看前 4000 字符 |

### fact_premise_rail（rail）

| 成员 | 契约 |
|---|---|
| `FactPremiseRail(language, force_tool_names, force_first_retrieval, log_when_unverified, log_unclassified_claims)` | 继承 `DeepAgentRail`；`priority = 10`；`inherit_to_subagents = False` |
| `on_user_message(ctx)` | N3/N4；按 `fact_premise_level` 定级，置 `extra["_fact_premise_hit"]=True`、`extra["_fact_premise_level"]=level`、`extra["_fact_premise_query"]=text[:400]`；`info` 日志带 `level=` 与 query 预览 |
| `before_model_call(ctx)` | N6/N7；未命中 / level 1 / 未开启 / 已强制 / iteration>1 / 工具缺失 → 直接返回 |
| `after_tool_call(ctx)` | 不看命中标记（N12 的未命中轮次也要知道搜过）：`inputs.tool_name` 属检索类 → 置 `extra["_fact_premise_retrieved"]=True` |
| `after_model_call(ctx)` | N11 / N12；已检索 / 非最终答案 → 返回；命中 → 未检索则 warning（受 `log_when_unverified`）；未命中 → `answer_asserts_year_fact` 为真且 `log_unclassified_claims` 开 → 另一条 warning |
| `_first_available_tool(tools)` | 按 `force_tool_names` 顺序取第一个出现在 `tools` 中的名字（dict / ToolInfo 双形态） |

### 装配

| 位置 | 契约 |
|---|---|
| `deep/rail_factory.py::_build_fact_premise_rail(config_base, *, language)` | 读 `execution_guard.fact_premise_guard`；`enabled` 缺省视为 **true**；三个布尔项都走 `_config_bool` 严格解析（`bool("false") == True` 会把强搜开关误开）；任何异常 → warning + `None`（`_instantiate_rails` **不捕获** builder 异常，builder 必须自己兜住） |
| `interface_deep.py::_build_agent_rails` | 追加 `_RailBuildInfo("_fact_premise_rail", _build_fact_premise_rail, {"config_base": config_base, "language": self._resolve_runtime_language()})` |
| `interface_flash.py::_FLASH_RAIL_KEEP` | **含** `"_fact_premise_rail"`——flash 先跑 `super()._build_agent_rails` 再按白名单裁剪，未登记即整条丢弃。flash 是 relay-claw 默认聊天路径、B_09 的线上复现渠道，故与 deep 同挂；`_profile_fingerprint` 含该白名单，变更后 warm 实例重建（预期行为） |

## 数据结构

- 配置块（`jiuwenswarm/resources/config.yaml`，`execution_guard.fact_premise_guard`，全部经 `_config_bool` 严格解析）：
  - `enabled`（默认 true）：false → 不挂载 rail
  - `force_first_retrieval`（默认 **false**）：true → 首轮带 `tool_choice`
  - `force_tool_names`（默认 `[web_flash, web_search]`）：按序取第一个已挂载者；也可写成单个字符串
  - `log_when_unverified`（默认 true）：命中但未检索 → warning
  - `log_unclassified_claims`（默认 true）：未命中判据但最终答案写出年份事实且未检索 → warning
- `ctx.extra` 键（**invoke 作用域**，非实例属性——一个 rail 实例被并发会话共享）：
  - `_fact_premise_hit`：命中标记（`on_user_message` 写，其余钩子读）
  - `_fact_premise_level`：本轮级别（1 / 2），决定子句长度与是否允许强搜
  - `_fact_premise_query`：query 前 400 字符，仅用于日志
  - `_fact_premise_forced`：本轮已强搜（防同一轮重复写 `tool_choice`）
  - `_fact_premise_retrieved`：本轮已发生过检索（`after_tool_call` 写，命中与否都写；`after_model_call` 读）
- 常量：`_FACT_NOUNS`（事件 / 统计**类型**名词表，只定级）、`_QUESTION_MARKERS`、`_EN_INTERROGATIVE_RE`、`_EDIT_COMMAND_RE`、`_RETRIEVAL_TOOL_NAMES`、`_RELATIVE_YEAR_RE`（含届次词）、`_PAST_YEAR_SLACK = 1`、`_ENVELOPE_PREFIX`、`_ASSERTION_RE`（N12 的具体断言）、`_REFUSAL_RE`（N12 排除拒答）

## 执行路径全景

- **P1 用户消息层（`_admit_user_message` 内）**：react agent 组好本轮 parts → `ctx.fire(ON_USER_MESSAGE)` → 本 rail 先解包信封再定级 → `parts[0]` 不是四种渲染结果之一 → `parts.insert(0, clause(level))` + 置 `_fact_premise_hit` / `_fact_premise_level` → 框架 `"\n".join(parts)` 写入 `UserMessage`。
- **P2 请求参数层（首次模型调用）**：`ctx.fire(BEFORE_MODEL_CALL)` → 命中且 level 2 且开关开 → 取已挂载检索工具 → 合并写 `extra["llm_call_kwargs"]["tool_choice"]` → react agent `_apply_llm_call_kwargs` 过白名单 → `_prune_unusable_tool_choice` 对最终 tools 剪枝 → `base_model_client._build_request_params` 里 `params.update(filtered_kwargs)` 覆盖默认 `tool_choice="auto"`。
- **P3 观测层（每轮模型响应后）**：`after_tool_call` 见检索类工具 → 置 `_fact_premise_retrieved`（不看命中标记）；`after_model_call` 在「最终答案（无 tool_calls 且内容非空）∧ 未见检索」时：命中 → 一条 `year-sensitive … level=` warning（N11）；未命中 → `answer_asserts_year_fact(答案)` 为真才打 `unclassified answer stated a year-scoped fact` warning（N12）。
- **P4 flash 装配（本版覆盖）**：`JiuwenSwarmFlashAdapter._instantiate_rails` → `_filter_rail_infos_by_keep` 按 `_FLASH_RAIL_KEEP` 裁剪；白名单**已登记** `_fact_premise_rail`，故 flash 会话与 deep 一样注入 `[事实核对]` 并产生两侧 warning。`_profile_fingerprint` 含该白名单——从「未登记」升级到「已登记」会让既有 warm 实例重建一次，属预期行为，不是异常。

## 异常分支与退出路径

| 分型 | 触发 | 行为 |
|---|---|---|
| 判据不命中 | 普通办公指令（无时间锚点 / 编辑动词且非疑问） | 不注入、不强制、不告警 |
| 判据只能定到 level 1 | 时间锚点 + 疑问，但话题不在名词表（新事件名 / 内部数据问题） | 只注入一行短句；不强搜；warning 日志带 `level=1` |
| 注入失败 | parts 非 list、ctx 只读、拼接异常 | warning，本轮按无守卫继续（N5） |
| 强制检索失败 | 未开启 / 工具未挂载 / iteration>1 / 写入异常 | warning 或静默返回；请求照常发出 |
| `tool_choice` 被丢弃 | 旧版 agent-core（不在白名单）| core 记录 dropped 键后继续；用户消息子句仍在（N10） |
| `tool_choice` 无效 | 无 tools / 强制函数未 advertise | agent-core 请求前剪枝（N8），provider 不会 400 |
| provider 不支持 `tool_choice` | 请求 400 | 只有显式开启 `force_first_retrieval` 才会遇到；默认配置不触发 |
| 检索类工具不存在 | 环境关网 / 工具被禁用 | 不强制；子句要求模型明说无法确认 |
| 子代理轮次 | `inherit_to_subagents=False` | 不挂载到子代理（N9） |
| 整轮未检索 | 命中但零检索且已出最终答案 | 一条 warning（受日志预览开关约束） |
| 未命中判据但答案写出年份事实 | 无时间锚点 / 生产任务，最终答案含窗口内年份 + 具体断言，整轮零检索 | 一条 `unclassified answer` warning（N12）；拒答与「含年份但无断言」不告警 |

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| 「2028 年世界杯冠军是谁？」 | 命中（不存在的届次）→ 注入 + 可选强搜 | —— | `test_nonexistent_year_event_triggers` |
| 「2030 年世界杯在哪些国家举办？」 | 命中 | —— | `test_future_edition_triggers` |
| 「2022 年世界杯冠军是谁？」 | 不命中（已定论旧届次） | 多一次检索的延迟成本 | `test_settled_past_edition_does_not_trigger` |
| 「今年的总决赛冠军是谁」 | 命中（相对年份） | —— | `test_relative_year_triggers` |
| 「Who won the 2028 World Cup?」 | 命中 | —— | `test_english_question_triggers` |
| 「华为云 2025 Inspire 大会在哪举办？」 | 命中 level 2（事件类型名词「大会」） | —— | `test_unlisted_event_name_still_triggers` |
| 「2025 re:Invent 有哪些新发布？」 | 命中 level 1（名词表未覆盖，但仍守） | 子句更短、不强搜 | `test_shape_only_hit_is_level_one` |
| 「What did AWS announce at re:Invent 2026」（无 `?`） | 命中（英文疑问词） | —— | `test_english_question_without_question_mark_triggers` |
| 「上届冠军是谁」 | 命中（届次词即时间锚点） | —— | `test_deictic_edition_is_a_temporal_anchor` |
| 信封 `timestamp` 为当年 + `content` 是 2024 年峰会 | 不命中（只判 `content`，年份窗口生效） | 误注入 / 误强搜 | `test_envelope_metadata_does_not_widen_the_year_window` |
| 信封 JSON 损坏 / `content` 为空 | 回退原文后正常判定，不抛异常 | 一轮对话被打断 | `test_malformed_envelope_falls_back_to_raw_text` |
| 「把 2028 年世界杯冠军那页改成 2026」 | 不命中（编辑动词 + 非疑问） | —— | `test_year_mentioned_in_editing_request_does_not_trigger` |
| 「把预算表里的 2028 改成 2029」 | 不命中（编辑指令） | —— | `test_year_only_edit_does_not_trigger` |
| 原 query 位置 | 落在 `parts[1]`，子句在 `parts[0]` | 用户历史被改写 | `test_on_user_message_prepends_clause_and_keeps_original` |
| 重复投递同一 parts | 只注入一次（`parts[0]` 精确等于四种渲染之一） | 提示词膨胀 | `test_on_user_message_does_not_inject_twice`、`test_short_clause_is_not_injected_twice` |
| level 1 命中 | 只注入一行短句，`extra["_fact_premise_level"] == 1` | 提示词膨胀 | `test_on_user_message_injects_short_clause_for_shape_only_hit` |
| 未收录事件名 + 端到端注入 | 仍注入完整子句 | 守卫静默消失 | `test_on_user_message_guards_an_unlisted_event_name` |
| 用户正文引用 `[事实核对]` 字面量 | 仍注入（不做子串扫描） | 守卫被话术绕过 | `test_user_quoting_the_marker_still_gets_guarded` |
| steering 批次含年份事实 | 不注入 | 改写内部指令 | `test_on_user_message_ignores_steering_batches` |
| `parts` 形态异常 | 不抛异常 | 一轮对话被打断 | `test_on_user_message_is_fail_open_on_bad_inputs` |
| 首轮 + 工具已挂载 + level 2 + 开关开 | 写 `tool_choice`（命名函数） | —— | `test_before_model_call_forces_advertised_retrieval_tool` |
| level 1 + 开关开 | 不写 `tool_choice`（强搜只给完整子句） | —— | `test_before_model_call_does_not_force_on_level_one_hit` |
| thinking kwargs 已在 `extra` 里 | 合并保留 `extra_body` | 关掉模型思考 | `test_before_model_call_merges_existing_llm_kwargs` |
| 目标工具未挂载 | 不写 `tool_choice` | provider 400 | `test_before_model_call_does_not_force_when_tool_absent` |
| 第二轮及以后 | 不写 | 卡死循环 | `test_before_model_call_does_not_force_on_later_iterations` |
| 未命中 / 开关关 | 不写 | —— | `test_before_model_call_does_nothing_when_not_matched`、`test_before_model_call_is_off_by_default` |
| 命中但未检索 | 最终答案落地时 warning | 漏检不可见 | `test_after_model_call_warns_when_final_answer_used_no_retrieval` |
| 命中且检索过 | 无 warning | 日志噪声 | `test_after_tool_call_marks_retrieval_and_silences_warning` |
| 命中但本轮只是工具调用 | 不 warning（循环继续） | 误报 | `test_after_model_call_ignores_tool_call_turns` |
| 命中但响应为空 | 不 warning（core 会重试） | 误报 | `test_after_model_call_ignores_empty_responses` |
| 未命中判据但答案写出年份事实 | `answer_asserts_year_fact` 为真且未检索 → 一条 `unclassified answer` warning | 漏检不可见（无锚点题无法在输入侧判） | `test_unclassified_answer_that_states_a_year_fact_warns` |
| 未命中判据但本轮检索过 | 无 warning | 日志噪声 | `test_unclassified_answer_with_retrieval_does_not_warn` |
| 未命中判据 + 拒答 / 无断言 | 不告警（拒答正是期望行为） | 误报 | `test_answer_asserts_year_fact_negative` |
| `log_unclassified_claims=false` | 不告警 | —— | `test_unclassified_claim_logging_can_be_disabled` |
| 未命中轮次调了检索工具 | `after_tool_call` 仍置 `_retrieved`（不要求命中标记） | 输出侧兜底失效（误报） | `test_after_tool_call_records_retrieval_without_a_hit` |
| 观测钩子事件路由 | 必须挂在桥接到内层 ReActAgent 的事件上 | 告警整体丢失 | `test_observability_hooks_are_bridged_to_the_inner_react_agent` |
| flash 白名单 | **含** `_fact_premise_rail`（与 deep 一致） | 漏登记会让 OfficeClaw 默认聊天路径静默无守卫（B_09 的原始线上场景） | `test_fact_premise_rail_is_kept_in_flash` |
| agent-core `tool_choice` | 合法形态放行、非法形态丢弃、无 tools 剪枝 | provider 400 / 注入失效 | `test_react_agent_llm_call_kwargs.py::TestLlmCallKwargsToolChoice` |

## 修改检查单

- 改 `_FACT_NOUNS` / `_QUESTION_MARKERS` / `_EN_INTERROGATIVE_RE` / `_EDIT_COMMAND_RE` → 名词表**只定级**：加词只会让命中从短句升级为完整子句，删词不会让守卫消失；真正决定「守不守」的是时间锚点 × 疑问形态 × 非编辑指令。任何时候都不要把名词表改回必要条件（那正是「华为云 2025 Inspire 大会」整体静默的原因）。改判据要过两侧：目标题（赛事 / 大选 / 榜单 / 英文题 / 新事件名）必须命中，文档编辑指令必须不命中
- 改 `_unwrap_office_envelope` → 只对 `content` 判年份，回退必须是无损的（前缀 / JSON / `content` 任一不符就返回原文）；跳过解包会让 `timestamp` 的当年把 `_PAST_YEAR_SLACK` 变成恒真
- 改 `_PAST_YEAR_SLACK` → 「旧届次不搜」与「掐点年份搜」是权衡项，改大意味着更少检索
- 改 `priority = 10` → 必须重新核对与 `ThinkingInjectRail`(15) 的先后：本 rail 必须在它**之后**执行且**合并**写 `llm_call_kwargs`，否则思考参数被覆盖丢失
- 改 `on_user_message` 的插入位置 → `insert(0, ...)` 与 `_admit_user_message` 的 `"\n".join(parts)` 成对；改成追加或改写原话会破坏「原话保留」（N4）
- 改幂等判定 → 必须比对 `parts[0]` 与全部渲染结果（cn / en × level 1 / 2），不能用前缀 / 子串包含（用户会引用该字面量）
- 改子句文案 → level 1 与 level 2 都要保留 `[事实核对]` / `[Fact-check]` 前缀；level 2 的「每 4 年一届」「核实优先于工具调用次数控制」「不要编造」是既有测试的锚点，改动要同步 `test_clause_is_bilingual_and_names_the_cycle_rule`
- 改 `inherit_to_subagents` → 置 True 会把子句注入模型自写的任务指令
- 改 `_build_fact_premise_rail` → builder 必须自己 `try/except` 返 `None`（`_instantiate_rails` 不兜异常）；`enabled` 缺省语义是 **true**；布尔项必须走 `_config_bool`，不要用 `bool(...)`
- 改 flash 白名单 → `_fact_premise_rail` 必须留在 `_FLASH_RAIL_KEEP`（flash 是 relay-claw 默认聊天路径，也是 B_09 的线上复现渠道）；删掉条目等于让默认路径回到「不注入、不告警」，必须同步把 `test_fact_premise_rail_is_kept_in_flash` 反过来并说明为何放弃该渠道
- 改观测钩子 → 必须留在被桥接到内层 ReActAgent 的事件上（`after_model_call` / `after_tool_call`）；`after_invoke` 在外层 DeepAgent，读不到内层 `ctx.extra`
- 改 `_ASSERTION_RE` / `_REFUSAL_RE` → 这两条只服务输出侧兜底（N12），**不参与输入侧判据**；放宽断言会让普通回答也告警（一行日志的噪声），放宽拒答排除会让「正确拒答」被误报。任何时候都不要为了「少误报」把它们接回输入侧开关
- 改 agent-core `tool_choice` 白名单 / 校验 → 必须回归 `test_react_agent_llm_call_kwargs.py`：既有「非白名单键被丢弃」断言不能改动，新增仅覆盖 `tool_choice` 的合法 / 非法形态与剪枝
- 打开 `force_first_retrieval` 前 → 先用部署模型发一次带 `{"type":"function","function":{"name":"web_flash"}}` 的请求确认不 400；这是配置项，不是代码默认

## 与其它 spec 的关系

- [[S_10]]（输入侧守卫）：同属可靠性域但作用面不同——S_10 管工具调用的参数与结果，本 spec 管用户请求的前提与首轮工具选择；两者可以同时在同一轮生效
- [[S_07]]（LLM 调用层失败恢复）：重试是**异常路径**（`consume_retry_request()` 只在异常时消费），因此「先答错再重问一次」在当前 seam 不可实现——本 spec 明确不做 post-hoc 重试，只在请求发出前干预
- [[S_08]]（工具循环熔断）：强制检索只发生一次（首轮），不会触发循环检测；若模型拿到结果仍重复检索，由 S_08 接管
- 任务规划 / 事件流域：本 rail 不发射前端事件、不写 session；warning 只进服务日志
- 跨仓接缝：agent-core 的 `tool_choice` 白名单是**可选增强**——jiuwenswarm 在它缺席时降级为「用户消息注入」一层，不改变任何既有行为
