# B_04 — 排队晚达的「继续」被误读为采纳备选方案，stopped 终态文案三重防御补强

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_04 / stopped-copy-triple-defense |
| 关联模块 | `jiuwenswarm/agents/harness/common/tools/deepresearch/execution.py`、`common/rails/deepresearch_execution_rail.py` |
| 修复日期 | 2026-11-05 |
| 关联 spec | 无（面向模型的终态文案补强，未触及 S_01~S_21 已归档路径 / 不变量） |
| 关联测试 | `tests/unit_tests/agentserver/test_deepresearch_execute_tool.py` |
| 配套修复 | skill_tool 口径统一线 fix-c（stopped 双指引统一）的线上验证补强；fix-e 见 [[B_03]]，error 相位续跑见 [[B_02]]，线路全貌见仓库根 `HANDOVER_deepsearch_skill_tool口径统一.md` |

## 复现场景

officeClaw 部署环境（full.log，2026-11-05，conversation `9973ed6e`）——fix-c 新版
stopped 文案**已部署且被模型收到**的条件下，续跑指令仍被误读：

1. 10:42:44 全新 DeepResearch 启动（修复后链路正常，claim probe=none）。
2. 10:44:26 outline HITL 中断 → 10:44:29 用户答复续跑（interrupt 认领链验证通过）。
3. 10:45:38 用户停止；**10:45:41 用户发「继续」**（停止动作后 3 秒）。
4. 10:46:08 graceful 30s 超时 → 10:46:10 stopped 转译（fix-c 文案生效）。
5. 10:46:36.9 停止当轮 assistant 回复——**完全遵守文案**：未自主重启，汇报停止
   状态并提供两方案（1 恢复专用引擎 / 2 模型直接研究）。
6. **10:46:37 排队 55s 的「继续」才投递**——追加在两方案回复之后（消息信封
   timestamp 仍为 10:45:41）。
7. Turn 3 模型 reasoning：「[助手]提供了两个方案，用户回复"继续"」→ 解读为
   采纳方案 → 转走模型自主研究路径，重新规划研究任务。

停止快照完好（skill 侧 `checkpointer.db` 该会话 8 个 key 全部保留，agent-core /
deepsearch 停止保留修复在位）——**非链路故障，是模型对续跑指令的解读错误**。

## 根因

**主因（adapter 消息排队时序颠倒，不在本仓控制）**：officeclaw adapter 在
in-flight turn 期间把到达的用户消息排队，当轮 assistant 回复生成后才投递——
真实因果序（用户发送时刻）与呈现序（模型看到的顺序）被破坏。「继续」
（10:45:41）比两方案回复（10:46:36.9）早 55 秒，但呈现顺序相反，模型按呈现
顺序把「继续」当成了对两方案的答复。

**文案失守模式（本仓可修，三个）**：

1. **F1 停止来源误判**：模型 reasoning 出现「This is confusing... system timeout /
   pre-existing state」——文案未定性停止来源，模型自行猜测为系统故障或会话残留。
2. **F2 「继续」被备选方案劫持**：文案只约束「仅当用户明确要求继续」，未定义
   「继续」「继续研究」这类短语即属明确要求，也未声明其优先级高于模型自拟的
   备选方案——模型提供了两方案后，任何后续用户短语都倾向被解读为方案选择。
3. **F3 历史失败记忆绕过**：模型引用记忆中的引擎失败错误码（211901 / 212000）
   认定引擎不稳定而绕过——文案未禁止凭历史记忆降级。

## 修复口径

stopped 终态文案两处落点（`execution.py` `_stopped_result` content 与
`deepresearch_execution_rail.py` stopped 改写 content——fix-c 已确立两处同步
契约）同步新增三重防御；原契约条款（禁止自主重启 / skill_tool 重载指引 /
content 不含 conversation_id）不变：

1. **停止定性（封 F1 + F3）**：「停止是用户的主动操作，不代表系统故障、超时或
   会话残留，也不要因记忆中该主题曾有失败记录而绕过本工具」。
2. **「继续」优先级（封 F2）**：「用户回复"继续""继续研究"即属明确要求继续：
   即使你曾向用户提供过备选方案，也应优先按上述方式续跑，而非将其理解为采纳
   备选方案或改走模型自主研究路径」。
3. **timestamp 时序意识（针对主因的通用防御）**：「另注意用户消息信封自带
   timestamp，晚于你最近一条回复到达的消息未必是对该回复的回应，解读用户意图
   前先核对消息时序」。消息信封构建方在 adapter 仓（全仓 grep 实证），在排队
   消息的 supplementary_info 附时序说明超出本仓控制，只能以提示词侧补偿。

## 验证

- **定向回归**：`test_deepresearch_execute_tool.py` → **141 passed**（.venv）。
- **新增断言（两处）**：
  - `test_cancelled_during_sdk_window_translates_to_stopped_without_error_overwrite`：
    result content 断言含「停止是用户的主动操作」「失败记录」「备选方案」
    「timestamp」。
  - `test_execution_rail_rewrites_stopped_result_to_block_auto_restart`：rail
    改写 content 同步断言三重防御关键词——两处口径一致性被测试锁定。
- **既有断言不变**：无 conversation_id、无邀请式旧文案、「请勿」「明确要求继续」
  在位。

## 防错教训

- 面向模型的终态文案不仅要约束「能否重调」，还要约束「如何解读停止之后的用户
  消息」——用户在停止后的下一个短语（继续 / 重启 / 算了）是意图路由的关键输入；
  文案不定义其归属时，模型会按消息呈现顺序自行猜测。
- 消息系统的因果序（发送时刻）与呈现序（模型看到的顺序）可能不一致：排队 /
  重试 / 多端同步都会颠倒两者；提示词侧要求模型核对信封 timestamp 是低成本
  通用补偿。
- 修复文案的线上验证要看「模型收到后的**下一轮**行为」，不能只看当轮是否遵守
  ——本次当轮完全遵守（未自主重启、汇报 + 给方案），下一轮仍因误读「继续」失守。

## 遗留与后续

- adapter 排队投递机制（时序颠倒主因）不在本仓控制；长期方向为收窄版 A——停止
  终态专用窗口的 pending 消息查询（通用消息注入方案已评估消息丢失 / HITL 路由
  冲突 / 跨仓改造 / 注入竞态 / 意图绑定五大风险后否决）。
- 线上部署同步本补强后，需在「停止 + 立即『继续』」场景复验：模型应直接
  skill_tool 重载 + `deepresearch_execute` 认领快照续跑，而非解读备选方案。
- 本次 9973ed6e 会话未触发 tools_search（模型凭 reasoning 直接决策），
  [[B_03]]（fix-e）生效性仍待后续线上验证。
