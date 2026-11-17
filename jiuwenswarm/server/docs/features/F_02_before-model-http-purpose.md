# before_model_http 扩展埋点暴露调用用途 purpose

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-05 |
| 范围 | `jiuwenswarm/extensions/hooks_context.py`、`jiuwenswarm/llm_sse_patch.py`、`jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`、`jiuwenswarm/symphony/llm.py`、`jiuwenswarm/agents/harness/common/tools/deepresearch/deepresearch_rewrite_fast_path.py`、`jiuwenswarm/server/runtime/skill_turbo/{executor,planner}.py` 及对应测试 |
| 测试基线 | 受影响模块全量 500 passed、1 skipped（hook / deepresearch 两组 / symphony / skill_turbo planner+executor / btw+evolution） |
| Refs | 暂无 |

## 背景

上游积分对账需要区分每一次模型 HTTP 调用的用途，但 `before_model_http` 扩展埋点的
`ModelHttpHookContext` 只有 `api_base` / `model_name` 两个只读字段，扩展在 handler 里
无法得知"这条消息在干什么"。

实际信号早已存在：agent-core 的 `Model.invoke/stream` 会把 `request_purpose` 作为
callback-only 内部 kwarg 透传到模型客户端的 `invoke` 出口（`compression_scope` 在压缩
作用域内写入 `compaction`），而 `before_model_http` 正好包装在客户端 `invoke` 方法上。
缺口只是没把这个值从 `kwargs` 里取出来暴露给扩展。

## 决策

1. **`ModelHttpHookContext` 新增只读字段 `purpose: str = "chat"`**，值域
   `chat` / `compaction` / `summary` / `recap` / `rewrite` / `deepresearch_rewrite` /
   `subagent` / `team_leader` / `team_member` / `skill_turbo` / `symphony` /
   `repair`。普通主对话用自己的值 `chat`，与 agent-core OTel 侧
   `openjiuwen.request.purpose` 的 `assistant`/`compaction` 白名单解耦——两侧各自读
   各自的，互不影响。

2. **复用 `request_purpose` 内部 kwarg 作为显式透传载体**，不新建 kwarg。理由：
   agent-core 已在 `base_model_client._build_params` 的 `internal_params` 里过滤它、
   `OpenAIAccountModelClient` 主动 `pop` 它——零跨仓、且不会污染真实模型 HTTP 参数。
   新建 kwarg 则需在 agent-core 补内部参数过滤，跨仓成本高。

3. **归一化优先级**（`llm_sse_patch._resolve_model_http_purpose`）：
   1. `request_purpose` 命中显式值（compaction / summary / recap / rewrite /
      deepresearch_rewrite / skill_turbo / symphony / repair）即用；
   2. 当前执行主体（`ExecutionSubject.kind`）命中主体映射 `subagent` / `team_leader` /
      `team_member` 即用；
   3. 兜底 `chat`。
   显式值优先于执行主体推导——子代理/团队成员内部触发的压缩仍应计作 `compaction`。

4. **执行主体用 agent-core 现成的 `ExecutionSubject.kind` 推导，不从本仓另造 ContextVar**：
   子代理运行时设 `"subagent"`，主代理 `"main_agent"`（回退 `chat`），team 模式由
   `team_agent.observability_execution_subject` 设 `"team_leader"` / `"team_member"`。

5. **jiuwenswarm 独立调用点显式打标**：
   `compact_partial` → `summary`、`_call_model_for_recap` → `recap`、
   `_execute_merge_version_rewrite` → `rewrite`、`repair_model_response` → `repair`、
   `symphony/llm.py` → `symphony`、deepresearch 重写快速通道 → `deepresearch_rewrite`、
   skill_turbo planner/executor → `skill_turbo`。

## 拒绝的方案

1. **为独立异步调用补开 OTel run span**：那是 `llm.call` span 的埋点（受 parent span
   限制，无 root span 时不产出），与 `before_model_http` 扩展埋点是两套独立机制。
   积分对账只需后者；不必为覆盖轨迹 span 而额外开 root span。

2. **新建 `call_purpose` kwarg 而非复用 `request_purpose`**：语义更干净，但需在
   agent-core 的 `internal_params` 追加过滤，否则会泄漏进模型 HTTP 参数；且压缩场景
   agent-core 已走 `request_purpose`，复用它是零成本路径。

3. **`compact_partial` 并入 `compaction` 而非单列 `summary`**：`/rewind summarize` 是
   "部分对话摘要"，与 context-engine 的上下文压缩是两条独立链路，计费口径分开更准。

4. **普通对话在 agent-core ReAct 主循环显式写 `request_purpose="chat"`**：跨仓、主循环
   调用点分散且当前不传 purpose；用触发端兜底 `chat` 更稳，且不阻断后续再加细。

5. **deepresearch 重写并入 `rewrite` 或单列笼统的 `deepresearch`**：最终单列
   `deepresearch_rewrite`——既要与 skill 自演进重写（`rewrite`）区分计费域，
   又要名字精确到"这是重写"而非笼统的"深度研究"（用户确认）。

6. **skill_turbo / symphony / repair 不单列、并进 `chat`**：最终单列——三者都是独立
   语义的模型调用面（技能加速引擎 / 图谱编排 / 响应修复），并进 `chat` 会让积分对账
   把业务调用量和对话调用量混在一起。

## 验证

- 受影响模块全量 **500 passed、1 skipped**（hook / deepresearch 两组 / `symphony/` /
  skill_turbo planner+executor / btw+evolution）。
- 既有断言的适配：deepresearch 三处 `await_args.kwargs` 断言与 planner 两个假 `stream`
  签名补 `**kwargs` / `request_purpose`，反映新契约（`request_purpose` 是本次调用的
  显式透传项）。
- 手动核对：`request_purpose` 已存在于 agent-core `internal_params` 与
  `OpenAIAccountModelClient` 的 `pop` 清单，不会外泄进模型请求。

## 已知遗留

1. **覆盖边界**：`before_model_http` 只覆盖被包装客户端的文本 `invoke`/`stream`
   （+部分 `generate_image`）；SiliconFlow / IntelliRouter / Ascend & InferenceAffinity
   及 DashScope/DeepSeek 的多媒体出口不在覆盖内——若上游计费需完全覆盖，另起任务。
2. **OTel span 侧细值不可见**：`summary`/`recap`/`rewrite`/`deepresearch_rewrite`/
   `skill_turbo`/`symphony`/`repair` 会被 agent-core `callback_handler` 归一成
   `assistant` 打进 `openjiuwen.request.purpose`，轨迹视图只区分压缩/非压缩，看不到更细值
   ——这是有意为之，不影响本特性的积分对账口径。
3. **子代理/team 推导依赖 `execution_subject_scope`**：若未来新增不套该 scope 的子代理或
   team 通道，需在触发端补识别；当前 agent-core 三条子代理通道（runtime/session spawn/
   task_tool）与 team_agent 均套用。
4. **一批平台内部模型调用未打标、会计入 `chat`**：以下调用点既未写 `request_purpose`、
   也不在 `ExecutionSubject` 作用域内，归一化后统一落 `chat`（与普通主对话同口径）——
   上游按 `chat` 对账时会把平台内部调用计入用户对话量，需注意：
   - 用户钩子审查 LLM（`jiuwenswarm/server/hooks/executor.py`）
   - 记忆 dreaming（`jiuwenswarm/agents/harness/common/memory/dreaming/sweeper.py`）
   - 后台 Agent（`jiuwenswarm/agents/harness/common/rails/eternal_conversation/background_agents.py`）
   - IM 改写/分类（`jiuwenswarm/gateway/im_pipeline/im_inbound.py`、`im_outbound.py`、
     `jiuwenswarm/server/im/im_hosting/gate.py`）
   - Agent 配置生成（`jiuwenswarm/server/handlers/agents.py`）
   - 深研选项生成（`jiuwenswarm/agents/harness/common/tools/deepresearch/execution.py`）
   - 图像生成出口（`OpenAIModelClient.generate_image`，值域无图像语义值）
   若需精确区分计费，应新增对应值（如 `hook` / `memory` / `im` / `deepresearch_options` /
   `image`）并按同一原则打标；本次刻意不覆盖，避免值域一次性膨胀。
5. **embedding / 语音等非 chat 模型出口未在本次审计范围内**，如需计费另起任务。