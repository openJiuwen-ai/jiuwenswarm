---
id: F_20261028_1135_cron-snapshot-retirement
title: cron 快照退役与权威收口实施
type: F
author: Yoggaboomin
author_email: yutao131@huawei.com
modules:
  - gateway/message_handler
  - gateway/cron
  - jiuwenswarm/agents/harness/common/tools/cron
  - jiuwenswarm/server
files:
  - gateway/message_handler/message_handler.py
  - gateway/cron/controller.py
  - jiuwenswarm/agents/harness/common/tools/cron/cron_tools.py
  - jiuwenswarm/server/agent_ws_server.py
  - tests/unit_tests/agentserver/test_cron_runtime.py
  - tests/unit_tests/agentserver/test_a4p_split_process.py
  - tests/unit_tests/gateway/test_cron_controller_project_binding.py
  - tests/unit_tests/gateway/test_message_handler_cron_push.py
  - tests/unit_tests/gateway/test_cron_snapshot_sync_scope.py
designs:
  - D-20261028-1105-cron-snapshot-retirement
design_note: null
---

# cron 快照退役与权威收口实施

## 关联依据

- 设计：D-20261028-1105-cron-snapshot-retirement（2026-10-28 用户批准，状态 approved）。
- 基线：upstream develop `2b9cee84f6`（2026-10-27 10:05），worktree `jiuwenswarm-gwlat` 分支 `fix/cron-snapshot-retirement`。
- 日期：2026-10-28。
- 实际实施范围：设计 D-01～D-06 全部落地（数据面摘除、快照机制退役、update 前置读权威收口、ack 回投补 session_id、toggle 服务端守卫、CRON_JOBS_SYNC tombstone）；V-01 E2E/性能复测未执行，留转测阶段。

## 决策归属与关系

本次为 D-20261028-1105 的首次实施记录，无更早维护记录。设计中的取舍理由（broadcaster 否决、authoritative get 用于 pre-read 的选择、fire-and-forget 权衡）由设计文档承接，本文只记录落地事实与开发中新产生的细节决策。

## 上下文与跨边界影响

- 诊断输入：gateway-latency-20261027.tar.gz——`process_stream` 在每次 chat.send / command.goal 前串行 `await _sync_agentos_cron_jobs(env)`（跨进程 cron.jobs.sync RPC 往返），实测 p50 158-191ms、单发 425ms、50 并发 p50 3.4s；web 渠道网关侧无超时上限（agent_request_timeout.py 仅覆盖 TUI）。
- 生产模式事实（推翻早期误判）：`CronTools()` 无参构造时 `gateway_push is None` → `_uses_gateway_command_ack()` 返回 True（cron_tools.py docstring "Production AgentServer uses the host-services path"），即生产 ack 回环模式本已启用；list/get/preview/create/run_now 在生产从不读快照，快照仅剩 update/delete/toggle 的 `existing` 预读消费者。
- 跨端契约：E2A 帧级别新增/变化——
  - `CRON_JOBS_SYNC`：由"安装快照"变为 tombstone（AgentServer 侧识别方法并应答 ok、不安装），保留一个发布周期防旧网关帧落入泛化 chat 执行链伪造空 agent turn。
  - `CRON_COMMAND_ACK` / `CRON_RUN_NOW_ACK`：回投信封补 `session_id` 字段（原先缺失导致 per-session key 部署路由失败、全部 cron 工具 15s 超时）。
- 协议安全面：Gateway 入口 `_handle_cron_push_payload` 对 marker `_agentos_project_binding_verified` 是无条件盖章，因此裸删除快照会造成静默回归（project_dir 重绑定丢失、standalone work_mode 通过、伪造 project_id 落库）——这正是必须配套"权威收口 pre-read"而非单纯删除的原因，详见设计 §5。

## 实际实现与开发决策

1. **数据面摘除（D-01）**：删除 `message_handler.py` 中 `_CRON_SNAPSHOT_STREAM_METHODS` 常量、`process_stream` try 块首部的 `await self._sync_agentos_cron_jobs(env)` 以及整个 `_sync_agentos_cron_jobs` 方法。chat.send 数据面不再有任何跨进程 cron RPC。
2. **快照机制退役（D-02）**：删除 cron_tools.py 模块级 `_gateway_jobs_snapshots` 缓存、`install_gateway_jobs_snapshot()`、`_snapshot_for_route()`、`_agentos_snapshot_unavailable()`；`_view_job` / `_view_jobs` 收敛为"本地 store + 本请求 pending 投影"两层。AgentServer 侧 `_handle_gateway_cron_callback` 不再安装快照。`_agentos_snapshot_unavailable` 的非 ack 分支按用户决策直接删除（无替代告警）。
3. **update 前置读权威收口（D-03）**：新增 `_get_existing_via_gateway()`（普通 get，not-found 与错误归一 None）；`update_job` 在 ack 直连且 `route().user_id` 非空时 pending 短路 → 权威 get，not-found 由服务端 mutation 的 KeyError 呈现；ack 分支 update 不回写 pending（返回 `gateway_result["data"]`，pending 仅由 create 写入，模拟同轮 create→update）。delete/toggle 的 ack 分支改为先行提交并返回服务端裁决结果（deleted 布尔 / data dict）；非 ack 分支 existing=None 时维持"提交 + pending 记账 + 乐观 True"。
4. **ack 回投补 session_id（D-04 / R7 前置）**：`_push_cron_command_ack` / `_push_cron_run_now_ack` 签名增加 `session_id: str | None = None`，`_handle_cron_push_payload` 两个调用点透传 payload 的 session_id，`e2a_from_agent_fields` 构造信封时传入。
5. **toggle 服务端守卫（D-05）**：`CronController.toggle_job` 在 KeyError 检查后拒绝 `mode == "proactive.tick"`，错误文案与 delete 路径 `ensure_proactive_deletable` 对称；堵住 TUI 与 E2A 路径原先缺失的服务端校验（web 已有 handler 级拒绝）。
6. **tombstone（D-06）**：`agent_ws_server.py` 的 CRON_JOBS_SYNC 分支改为 `pass`（应答 ok、不安装），方法识别保留一个发布周期；英文注释说明兼容窗口与移除条件。

开发中新产生的选择（设计未逐字规定）：直接读的合取条件取 `remote_gateway and user_id 非空`——ack-only 会让 legacy 传输多付一次往返，user_id-only 会在非 ack+user 组合下复活静默回归，两者缺一不可。

## 与设计的偏差

无实质偏差。一处处置从设计候选中选定：非 ack 分支 `_agentos_snapshot_unavailable` 的替代告警未实现，按用户 2026-10-28 确认"直接删除"，其后果（非 ack 模式 delete/toggle 在无本地视图时 fire-and-forget）已作为设计 §9.2 已知限制记录。

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 / 记录链接 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-10-28 / 受影响单元测试面（8 文件：cron_runtime、a4p_split_process、cron_controller_project_binding、message_handler_cron_push、message_handler_evolution、gateway_normalize、adx_gateway_identity、session_input_forwarding） | worktree fix/cron-snapshot-retirement（基线 2b9cee84f6）+ uv 环境 | `PYTHONPATH=. uv run pytest <8 文件> -q --no-cov` | 通过：239 passed / 6.42s | 无 |
| 2026-10-28 / message_handler 广面回归 | 同上 | `PYTHONPATH=. uv run pytest tests/unit_tests/gateway/test_message_handler*.py -q --no-cov` | 通过：218 passed | 无 |
| 2026-10-28 / AgentServer cron 关联面（agent_ws_connection_close、deep_adapter_cron_tool_registration、cron_dow_schema_fix） | 同上 | `PYTHONPATH=. uv run pytest <3 文件> -q --no-cov` | 通过：63 passed | 无 |
| 2026-10-28 / steering 集成回归 | 同上 | `uv run pytest tests/integration/test_session_steering_sdk.py` | 1 例失败：`test_goal_steering_uses_real_adapter_stream_and_closes_after_clear[clear]` | 基线既有：`git stash` 后在 2b9cee84f6 上同样失败，与本次改动无关 |
| 2026-10-28 / V-01 E2E 与性能复测 | — | 计划：实例环境 mock LLM，验证 chat.send 无 cron-jobs-sync 出站帧、50 并发时延对比 | 未执行 | 留转测阶段（jw-testing 资产） |

### 可见行为、副作用与证据有效范围

- 数据面：chat.send / command.goal 不再发出 CRON_JOBS_SYNC 出站帧（代码路径删除 + tombstone 测试断言 ok 应答且仅 1 帧）。
- Agent 工具行为：ack 直连下 update/delete/toggle 返回服务端权威结果（update 返回 data dict、delete 返回 deleted 布尔、toggle 返回 data dict）；同轮 create→update 经 pending 短路不发 get（测试断言 get_calls 为空）。
- ack 信封：CRON_COMMAND_ACK / CRON_RUN_NOW_ACK 携带 session_id（gateway_normalize 载体测试从旧 carrier 切至 ReqMethod.CRON_COMMAND_ACK 断言透传）。
- 证据范围：全部为单元/集成级（mock gateway push、多进程 a4p 真实 store 查询）；未含真实部署的 E2E 时延测量。mock 形状变化（`supports_cron_command_ack` 缺失即非 ack 模式）与 pending TTL 回收行为均有专项测试。

## 验证结论

- 已证明范围：快照同步从数据面移除后，cron 工具面（读/写/删/启停/run_now/pending 语义）、Gateway 推送处理（marker 盖章路径、ack 投递）、AgentServer 回调（tombstone）、controller 守卫（proactive.tick 拒绝）在单测/集成级全部保持或加强；ack 信封 session_id 透传有 wire 级断言。
- 未覆盖或未通过的承诺：V-01（E2E 无出站帧验证 + 50 并发性能对比）未执行，属转测阶段承诺；steering 集成 1 例失败经基线对比确认为既有问题，不构成本次交付阻塞。
- 可支持的交付判断：满足设计约定的代码级验收范围；生产时延收益（每次 chat.send 少一次跨进程 RPC 往返）需 V-01 数字化确认后才能对外主张。

### 覆盖与资料同步

- 已盘点/理解/检视/验证范围：gateway 数据面转发、cron 三链路（AgentTool ack 直连 / 非 ack 提交 / web RPC）、AgentServer 回调、协议信封；证据为上表 4 组测试运行。
- 本次更新资料：D-20261028-1105（设计）、本 F 文档；sync_documents.py 已执行并刷新索引。
- 待同步部分：转测资料由 jw-testing 独立产出，不进本索引。

## 已知遗留

| 遗留项 | 未处理原因 / 性质 | 影响与现有处置 | 后续入口 / 条件及所需验证 |
| --- | --- | --- | --- |
| 非 ack 模式 delete/toggle 在无本地视图时 fire-and-forget（不报 not-found） | 用户确认的有意取舍（设计 §9.2） | 仅影响非 ack 传输（测试 mock / legacy 路径）；服务端仍会拒绝无效 id 并记日志 | 若 legacy 路径仍有真实部署，转测阶段观察误报率 |
| `_get_existing_via_gateway` 返回 None 且 patch 含 project 字段时，以未盖章 resolution 提交 | 边界情形 | 自洽：同渠道 mutation 亦会失败，错误由服务端呈现 | 无需动作；设计 §5 已论证 |
| steering 集成 1 例既有失败 | 非本次引入（基线复现确认） | 不影响本特性 | 独立 issue 处理 |
| CRON_JOBS_SYNC tombstone 需在下一个发布周期移除 | 有意兼容窗口（D-06） | 旧网关帧仍被识别并 ok 应答 | 升级窗口结束后删除方法识别与 pass 分支 |
| V-01 E2E / 性能复测未执行 | 属转测阶段 | 时延收益主张暂限于机制层（RPC 次数 1→0） | jw-testing 转测方案含 50 并发对比 |
