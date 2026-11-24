# 年份 / 届次事实题的虚假前提（2028 年世界杯）

| 项 | 值 |
| --- | --- |
| 类型 | bugfix |
| 日期 | 2026-11-23（2026-11-24 修订判据） |
| 问题 | 线上会话：用户问「2028 年世界杯冠军是谁」，Flash 用 2026 年世界杯的主办地 / 扩军 48 队 / 赛期（2026-06-11 ~ 07-19）作答 |
| 关联规约 | [S_26 事实前提守卫](../specs/S_26_fact-premise-guard.md) |

## 复现场景

同一会话里用户先问 2026 年世界杯，再问 2028 年世界杯冠军。第二轮：

- 0 次工具调用（`web_flash` 在最终 14 个 eager 工具里且权限为 `allow`，模型没有选它）
- 整轮 4.81 ~ 4.97 s，仅 192 reasoning tokens——没有检索、没有展开推理，直接生成
- 回答把 2026 届的真实参数（主办国、48 队、赛期）套到 2028 上
- 下一轮用户质疑时，模型自评 40 分并承认「There is NO 2028 men's World Cup!」

结论：不是知识缺失，而是**生成时没有校验前提**——2028 年男足世界杯并不存在（2022 → 2026 → 2030），模型把年份当成弱槽位，用相邻届次的属性填空。

## 根因

三层都缺「先确认前提」这一句，且三层都没有强制力：

1. **系统提示词**：`time` 段只写「问最新/今年/近期时，搜索 query 优先带当前年份」，没有任何「先判断该年份/届次是否存在」的规则。`request_system_prompt`（relay，priority 110）也不管这类校验。
2. **工具选择**：Flash 的选型总则写着「默认一个问题一个工具」，把联网检索从默认动作压成了例外；`web_flash` 的描述里「通常先搜索」是建议语气，不是年份事实题的必要条件。
3. **运行时**：没有任何机制检查「这一轮是该搜的题」——模型不搜就直接答，链路不会拦、不会重问、也不会留下可观测的痕迹。

## 拒绝的方案

- **事后校验再重试**：`consume_retry_request()` 只在异常路径消费，一次「成功但答错」的响应无法被重跑。要实现得改 agent-core 的 ReAct 主循环语义，超出本次修复面。
- **`tool_choice` 直接强推（不改 agent-core）**：react agent 对 per-call kwargs 走 `_ALLOWED_LLM_CALL_KWARG_KEYS` 白名单，`tool_choice` 不在其中会被丢弃——没有确定性通道。
- **改 `flash/prompt/content/tools.py` 的选型总则**：该文件参与静态提示词哈希审计，改动面大于收益；守卫改由用户消息子句提高核实优先级，不动静态提示词。

## 修复口径

单一机制（`FactPremiseRail`）。**不改** `runtime_prompt_rail` 的 `time` 段，也**不改** `web_flash._description()`：它们与 rail 同时生效时无法单独归因，且提示词层改动面更大；本次只保证「命中即注入 + 可选强搜 + 漏检可观测」。

1. **消息层（默认生效）**：新增 `FactPremiseRail.on_user_message`，命中「时间锚点 + 疑问形态 / 事件话题，且非编辑指令」且来源是用户 query 时，在 parts 首位插入 `[事实核对]` 子句，原话保留。子句只声明「核实优先于工具调用次数控制」，不引用 Flash 专属的「默认一个问题一个工具」——deep / team 提示词里没有那条规则。子句按级别两档（见下节「判据修订」）。
2. **请求层（默认关）**：同一 rail 的 `before_model_call` 可在首轮把 `tool_choice` 指向已挂载的检索工具（`web_flash` / `web_search`）。开关 `execution_guard.fact_premise_guard.force_first_retrieval` 默认 `false`：provider 不支持时整轮 400，需先验证再开。配置值走严格布尔解析，`"false"` 不会被 `bool()` 误开。
3. **观测**：命中、最终答案已产出（无 `tool_calls` 且内容非空）却整轮零检索 → 一条 warning（`after_model_call`；deep 适配器只把该事件桥接到内层 ReActAgent，`after_invoke` 在外层读不到命中标记）。query 预览受 `logging.preview_user_content` 约束，用于统计「该搜没搜」。
4. **装配**：`rail_factory._build_fact_premise_rail`（fail-open，坏配置返回 `None`）+ deep `_build_agent_rails` + flash `_FLASH_RAIL_KEEP`。flash 是 relay-claw 办公单专家的默认聊天路径，**也是本缺陷的线上复现渠道**（见「复现场景」），所以必须登记；白名单变更会改变 `_profile_fingerprint`，既有 warm 实例重建一次属预期（见 S_26「装配」P4）。

agent-core 侧只加一件能力：`tool_choice` 进入 per-call kwargs 白名单（形态校验 + 请求前按最终 tools 剪枝）。旧版 core 丢弃它时，消息层仍然生效。

## 判据修订（2026-11-24，MR 评审期）

初版判据把「事实名词表」当成**必要条件**，两个后果在冒烟复盘中暴露：

1. **闭集漏检**：「峰会」命中，但「华为云 2025 Inspire 大会」「AWS re:Invent 2026」「WAIC」「CES」这类未收录的专名整体静默——不注入、不强制、不留 warning。名词表永远追不上新事件名，等于每来一个场景补一次词。
2. **信封把年份窗口废掉**：真实链路 `on_user_message` 收到的是 `你收到一条消息：{..."timestamp": "当天"...}`，`timestamp` 恒为当年，使「年份不早于当前年 - 1」恒真；实测「2024 年华为云峰会」裸文本不命中、加信封后命中，`_PAST_YEAR_SLACK` 形同不存在。

修法（同一 rail，不新增机制）：

- **判据改形态**：时间锚点（窗口内年份 / 届次词 / 相对年份）× 疑问形态（中文疑问词或英文 wh- / 助动词）× 非编辑指令。`_FACT_NOUNS` 降级为**分级信号**：命中 → level 2（完整子句 + 允许强搜），未命中 → level 1（一行短句，仍然守）。名词表从此只能让守卫说少，不能让它消失。
- **先解包信封**：`_unwrap_office_envelope` 只取 `content`；前缀 / JSON / `content` 任一不符即回退原文。
- **强搜只给 level 2**：`tool_choice` 是最侵入的杠杆，不为形态级命中启用。
- **新增届次锚点**：`本届 / 这届 / 上届 / 上一届 / 下一届 / 最近一届 / 前年`——「上届冠军是谁」不写年份也该守。

取舍：level 1 会把部分内部数据问题（「2026 年 Q1 目标是什么」「四季度营收是多少」）也算进来，代价是一行短句；换来的是新事件名不再需要改代码。实测 23 条未收录事件题：旧判据命中 6 条，新判据 23 条全命中；30 条日常办公消息：旧判据 4 条命中（其中 2 条是信封 P2 造成的假阳），新判据 9 条（其中 3 条「春节是哪天 / 放假安排 / 校招什么时候」其实是真事实题，真正多出的是 6 条内部数据问题）。

## 输出侧兜底（2026-11-24，二轮评审）

判据改成形态后，还剩一个**按定义**覆盖不到的口子：输入侧必须有时间锚点。

| 输入 | 输入侧级别 | 实际风险 |
|---|---|---|
| `华为云 Inspire 大会有什么发布？`（无年份） | 0 | 模型仍可把上一届参数套上去 |
| `re:Invent 有什么新发布？` | 0 | 同上 |
| `写一段 2025 年集团年会讲话稿` | 0（生产任务） | 若同时被问「去年发了什么」，仍可能编 |

继续往输入侧加词只会重建闭集，加「无锚点也算」又会把大量普通任务拖进来。所以改成看**输出**：`after_model_call` 对未命中轮次检查最终答案——出现窗口内年份（复用 `_has_temporal_anchor`）∧ 一个具体断言（日期 / 第 N 届 / 数量+单位 / 举办·发布·夺冠等）∧ 整轮零检索 → 一条 `unclassified answer` warning。零词表，且拒答（无法确认 / 查不到 / 不存在 / cannot confirm…）显式排除——拒答正是期望行为。

两处配套：

- `after_tool_call` 不再要求命中标记（`_fact_premise_hit`）：否则未命中的轮次永远看不到「其实搜过了」，输出侧会稳定误报。副作用是被守卫跳过的轮次也会写一个 `_retrieved` 布尔，无感知成本。
- `log_unclassified_claims`（默认 true）控制这条 warning；它只是观测，不注入、不强搜、不改答。

取舍：这是**观测**而非拦截。它能回答「有多少无锚点年份事实是零检索答出来的」，但要把它升级成「重问一次」需要 core 的 retry seam（见 S_07：重试只在异常路径消费），不在本提交范围。误报代价是一行日志；断言正则故意保守（宁少勿多），拒答排除也故意窄（`抱歉` / `不确定` 不算拒答，避免把真断言放过）。

## 回归

- `tests/unit_tests/agents/harness/common/rails/test_fact_premise_rail.py`：判据正反例（不存在届次 / 未来届次 / 相对年份 / 英文题 / 英文无问号 / 届次词 / 未收录事件名 / 旧届次 / 编辑指令）、分级（level 1 vs 2）、信封解包与损坏回退、子句两档形态、注入位置与精确幂等（含用户引用 `[事实核对]` 字面量、level 1 重复投递）、fail-open、强制检索六重前置（含 level 1 不强搜）、观测（最终答案未检索告警 / 工具调用轮与空响应轮不告警 / 观测钩子必须挂在桥接事件上）、输出侧兜底（`answer_asserts_year_fact` 正反例 / 未命中轮次告警 / 检索过不告警 / 开关可关 / 未命中轮次也记录检索）。
- `tests/unit_tests/agentserver/test_fact_premise_rail_factory.py`：缺省启用、显式关闭、严格布尔（`"false"` 不开强搜）、坏配置 fail-open。
- `tests/unit_tests/agentserver/test_flash_adapter_rails.py`：断言 `_fact_premise_rail` **在** flash 白名单内、且不在 protected 集（flash 是默认聊天路径、本缺陷的线上渠道；删条目会让守卫在默认路径静默失效）。
- agent-core `tests/unit_tests/agent/react_agent/test_react_agent_llm_call_kwargs.py::TestLlmCallKwargsToolChoice`：合法 / 非法 `tool_choice` 与剪枝。

## 验证

- **flash（本缺陷的线上渠道）**：`_FLASH_RAIL_KEEP` 登记本 rail 后，在本地引擎上跑过 8 个独立会话（4 + 4）。level 2「2028 年世界杯冠军是谁」注入完整子句并触发 5 次检索，终答先否定 2028 前提；level 0 的编辑指令与本地整理任务全程静默；level 1「2026 年会发生什么大事」注入一行短子句后自行检索；两条「凭记忆说 + 无锚点年份事实」命中输出侧 warning；2019 旧届次（窗口外）静默。日志锚点：`[fact-premise] guard applied level=`、`[JiuwenSwarmFlashAdapter] FactPremiseRail create success`。
- **deep / agent**：同一 rail、同一套钩子，装配入口是 `interface_deep._build_agent_rails`，无独立实现；行为口径与 flash 一致。

产品侧仍需实测（flash 与 deep 各一组）：同一会话连续问 2026 / 2028 两届，确认第二轮先检索且不再复用 2026 参数；不开启 `force_first_retrieval` 时，检查 warning 是否出现——出现即说明仅靠注入子句没压住。
