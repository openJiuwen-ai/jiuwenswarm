# relay-claw 定时触发未标记为 cron，TTSE 每次多跑一轮且成本方差大

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_02 / scheduler-trigger-cron-run-kind |
| 关联模块 | jiuwenswarm/server/runtime/agent_adapter/interface.py（`_build_inputs`）；agent-core `openjiuwen/harness/rails/evolution/ttse_rail.py` |
| 最近一次修订日期 | 2026-11-24 |
| 关联 feature | 暂无 |

## 复现场景

OfficeAce 1.8.0，同一个每小时触发的提醒类定时任务，单次消耗 0.81～5.22 积分。日志
（11-16 21:19～11-17 10:19，14 次触发）显示每次输入约 52～60k token，其中约 50.5k 是固定前缀
（system 28.7k + tools 13.9k + skills 8k）。按夜间单价拟合，计费与 token 的误差在 ±0.03 积分以内，
说明账单没有算错，差异来自真实 token 当量：

- 多数触发跑两轮：第一轮模型按 TTSE 提示先调用 `ttse_consult`（对提醒任务返回的是无关的
  `write_file` 经验），第二轮才产出正文；
- 第二轮在第一轮结束约 0.2s 后发起，前缀不变（`stable_prefix_changed=False`）、`sessionId`
  相同，但 17 次中有 5 次服务端缓存未命中，这一轮按全价计费，夜间多约 1.6 积分、日间多约 2.4 积分；
- 每次触发结束还会跑一次 `is_feedback` 反思判定（约 2.7k 输入）。

## 根因

relay-claw 的定时触发以普通 `chat.send` 下发，session_id 为 `officeclaw_*`。OfficeAce 1.8.0 的
`metadata` 只有 `officeace_scen` / `officeace_client`；relay-claw dev-stable 自 `f79d6bead5`（F164）起
由服务端调度入口（reminder / web-digest / TaskRunnerV2 敏感重试）在信封 `metadata` 写入
`officeace_scheduler = {source: "scheduler", task_id, template_id}`，但 jiuwenswarm 未消费。
`_build_inputs` 只认 `params.run` / `params.cron`，因此这类触发的 run kind 是 `normal`：

- agent-core `TTSERail._is_background_run` 判不出后台运行，演进（含 `is_feedback`）照跑；
- `TTSERail.before_model_call` 注入"新任务首个工具调用必须是 `ttse_consult`"的提示与目录时，
  本就不检查后台运行，即使标了 cron 也会强制多一轮（agent-core 侧同步修复）。

## 修复口径

- `_build_inputs`：调用方未显式传 `params.run` / `params.cron`，且 `metadata.officeace_scheduler.source
  == "scheduler"` 时，写入 `inputs["run"] = {"kind": "cron"}`；显式传入的 `run` / `cron` 仍然优先。
  - 只认结构化字段、不认 query 前缀 `[定时任务]`：前缀可由用户在普通聊天中自行输入；relay 也明确
    "用户点击重试"属于普通对话、不带该字段。
  - HITL 审批后的续跑帧 `params.query` 为空，但仍携带该字段，续跑轮同样按 cron 处理。
- agent-core `TTSERail.before_model_call`：后台运行（cron / heartbeat）移除 TTSE 指引段，
  从本次调用的工具列表中去掉 `ttse_consult`，并清掉目录附件；`after_model_call` 再把指引段与目录附件
  加回，共享 builder / 会话附件在调用结束后回到基线。

修复后提醒类触发固定为单轮，不再有"第二轮是否命中缓存"的二值成本，也不再跑反思判定。

标记为 cron 的其他既有效果与 jiuwenswarm 自有定时任务一致：记忆轨只读（`MemoryRail`
不写入长期记忆）、`ExternalMemoryRail` / `CodingMemoryRail` / `SkillEvolutionRail` /
`SkillCreateRail` 跳过后台运行。

不在本次修复：
- 服务端第二轮缓存未命中（云端）；
- 日夜分时费率口径（产品 / 云端）；
- `[防重复]` 与 `[会话背景]` 块逐次增长（relay-claw）；
- auto 档 `ModelRoutingRail` 对定时任务按全文打分、可能升档 glm-5.3（单独跟踪）。

不带 `officeace_scheduler` 的旧版 relay（如 OfficeAce 1.8.0）不在覆盖范围内，行为与修复前一致。

## 验证

- `tests/unit_tests/agentserver/test_agentserver_modes.py`：
  - 带 `officeace_scheduler`（含空 query 续跑帧）标为 cron；
  - 无 metadata、只有 `officeace_scen`、`source` 非 scheduler、字段非 dict 时，即使 query 以
    `[定时任务]` 开头也保持 normal；
  - 显式 `params.run` 优先。
- agent-core `tests/unit_tests/harness/rails/evolution/test_ttse_rail.py`：后台运行（`ctx.extra`
  中的 `RunKind.CRON` / `"heartbeat"`，以及 `inputs.run_kind`）移除指引、工具与目录附件；
  普通运行保留 `ttse_consult`；后台调用结束后指引与目录附件恢复，紧接的普通调用照常拿到 `ttse_consult`。
