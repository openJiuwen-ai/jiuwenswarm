---
description: agent_adapter 适配器层模块说明书——给修改/检视本模块代码的 AI 助手与工程师：四层接口结构、27 文件地图、中断状态机与插话幂等等本地铁律、与 openjiuwen 的三层接缝、测试与提交约束。
---

# agent_adapter

适配器层：把 `jiuwenswarm` 运行时（AgentManager / TenantAgentPool 的装配请求、
AgentRequest 协议、HITL 中断/插话语义）翻译为 `openjiuwen` 的 DeepAgent 装配与
驱动。**本目录是两仓之间最宽的一条接缝**（27 个 .py = 26 实现 + `__init__`，
43,566 行），改这里的代码前先读 [../AGENTS.md](../AGENTS.md) 的铁律索引与相关 spec。

## 四层接口结构

```
AgentAdapter (Protocol, runtime_checkable)          ← agent_adapters.py:26
        ▲ create_adapter(sdk, mode, workspace_dir, agent_id, service_id)
        │                                            ← agent_adapters.py:150 工厂，仓内唯一装配入口
JiuWenSwarm (门面, interface.py:1156)               ← 组合 DeepAdapter，不是继承
        │ has-a
JiuWenSwarmDeepAdapter (基类, interface_deep.py:2539) ← 556 个方法、24,379 行
        ▲ 继承
JiuwenSwarmCodeAdapter (interface_code.py:459) / JiuwenSwarmFlashAdapter (interface_flash.py:166)
```

- **Protocol 而非基类约束门面**：调用方（agent_manager / tenant_agent_pool /
  gateway）只依赖 `AgentAdapter` 协议与 `create_adapter()` 工厂；`JiuWenSwarm`
  用组合包住 DeepAdapter 实例。加新模式 = 新子类 + 工厂分支，不要在门面上加 mode 判断。
- **模式三值**：`create_adapter(mode=...)` 只接受 `"agent"`（默认，Deep 通用）/
  `"code"` / `"flash"`；plan 不是装配形态而是 `DeepAgentState.plan_mode` 运行期
  状态（见 [[S_01]] N2，勿在此层新增 plan 分支）。

## 模块地图（26 个实现文件，按主题分组）

### 接口层

| 文件 | 行数 | 职责 |
|---|---|---|
| [interface.py](interface.py) | 4558 | `JiuWenSwarm` 门面：invoke/stream 生命周期、请求注入、team 审批 |
| [interface_deep.py](interface_deep.py) | 24379 | `JiuWenSwarmDeepAdapter` 基类：模型缓存、session 绑定、HITL 中断、reload、cron 收尾 |
| [interface_code.py](interface_code.py) | 2030 | code 模式子类（`CodingMemoryRail` 等代码域 rails） |
| [interface_flash.py](interface_flash.py) | 1440 | flash 模式子类 |
| [agent_adapters.py](agent_adapters.py) | 220 | `AgentAdapter` Protocol + `create_adapter()` 工厂 |

### 中断 / 续跑 / 插话

| 文件 | 行数 | 职责 |
|---|---|---|
| [interrupt_state_machine.py](interrupt_state_machine.py) | 153 | 相位机：`INTERRUPT_PHASE_SESSION_KEY = "jiuwenclaw_interrupt_phase"`（:42） |
| [interrupt_resume_helpers.py](interrupt_resume_helpers.py) | 171 | 中断/续跑辅助 |
| [plan_pause_helpers.py](plan_pause_helpers.py) | 565 | plan 暂停/恢复辅助 |
| [steering.py](steering.py) | 257 | 插话绑定与幂等回执 |

### 会话状态辅助

| 文件 | 行数 | 职责 |
|---|---|---|
| [session_flag_proxy.py](session_flag_proxy.py) | 110 | `SessionFlagProxy`（:33）会话标志双写 |
| [stale_todo_cleanup_helpers.py](stale_todo_cleanup_helpers.py) | 173 | 陈旧 todo 清理（配 generation_token） |
| [todo_close_text_hold.py](todo_close_text_hold.py) | 117 | todo 收口文案保持 |
| [recap_prompts.py](recap_prompts.py) | 85 | 回顾/摘要提示词 |
| [session_skill_dirs.py](session_skill_dirs.py) | 50 | 会话级技能目录解析 |

stale_todo_cleanup_helpers / todo_close_text_hold 两件与铁律 5（todo 代际隔离）的业务
契约归任务规划域：[agents/harness/AGENTS.md](../../../agents/harness/AGENTS.md)
（其 specs S_01~S_06，编号与本目录 specs 独立）。

### 演化（自我改进）

| 文件 | 行数 | 职责 |
|---|---|---|
| [evolution_helpers.py](evolution_helpers.py) | 1135 | 演化主流程辅助 |
| [evolution_version.py](evolution_version.py) | 454 | 演化版本管理 |
| [evolution_slash.py](evolution_slash.py) | 421 | 演化 slash 命令 |

### 团队模式

| 文件 | 行数 | 职责 |
|---|---|---|
| [team_helpers.py](team_helpers.py) | 3942 | team 装配/成员会话/快照刷新辅助 |

### 进程级 patch（幂等，`_PATCHED` 守卫）

| 文件 | 行数 | 职责 |
|---|---|---|
| [sysop_builder.py](sysop_builder.py) | 1160 | 系统提示词构建 |
| [llm_io_trace.py](llm_io_trace.py) | 547 | LLM IO 追踪（`_apply_llm_io_trace_patch` 于 interface_deep.py:1024） |
| [deepagent_task_plan_binding_patch.py](deepagent_task_plan_binding_patch.py) | 181 | 交互续轮 task id 绑定 TaskPlan |
| [workspace_host_init.py](workspace_host_init.py) | 198 | workspace host 初始化 |

### 流 / 上下文

| 文件 | 行数 | 职责 |
|---|---|---|
| [subagent_stream.py](subagent_stream.py) | 302 | 子代理流转发 |
| [compact_partial_prompts.py](compact_partial_prompts.py) | 201 | 部分压缩提示词（NO_TOOLS_PREAMBLE 等） |
| [code_agent_rail.py](code_agent_rail.py) | 478 | code agent 执行 rail |
| [statusline_setup_agent.py](statusline_setup_agent.py) | 129 | 状态栏 agent 装配 |

## 本地铁律（改代码前必读）

1. **`__init__` 的 5 个 patch 必须保持幂等**（interface_deep.py:2572-2590）：
   3 个无条件进程级（`apply_mcp_call_timeout_patch` /
   `apply_mcp_tool_list_refresh_patch` / `apply_deepagent_task_plan_binding_patch`）
   + 2 个仅企业版（`apply_memory_init_patch` / `apply_context_read_patch`，`is_enterprise()`
   分支内）。adapter 每会话重建，patch 只允许打一次——新增 patch 必须带
   `_PATCHED` 守卫并挂进 `__init__` 的同一编排。
2. **HITL 中断卡片双轨活性注册表**（interface_deep.py:2653 起 4 个结构）：
   `_hitl_card_instances`（final_id→base tcid）/ `_hitl_base_live_instance`
   （base→活卡 final_id）/ `_hitl_base_live_questions`（base→questions_key）/
   `_hitl_dead_card_ids`（死卡集）。同一 base tcid 同一时刻至多一张活卡；
   ask_user 卡片 request_id 以 `{id}#{n}` 区分代序——序号收口在
   `OuterCallIDDeduplicator`（复用 skill_turbo 域的
   `hitl_dedup.py`，interface_deep.py:647 导入；改它要同时看 skill_turbo 的
   AGENTS.md）。作答命中死卡 → `guard_stale_interrupt_response` 拒绝，
   切断"点旧卡 → resume → 再弹新卡"循环。内存态重启即失效（fail-open），
   磁盘相位守卫（`jiuwenclaw_interrupt_phase`）兜底。
3. **插话幂等键**：`steering.py` 以 `input_id` + `client_message_id` 判重
   （冲突回执 `IDEMPOTENCY_CONFLICT`），单 binding 输入上限 512
   （`len(binding.inputs) >= 512`，steering.py:230）。
4. **运行时状态写并发**：`_RUNTIME_STATE_WRITE_LIMIT = threading.BoundedSemaphore(2)`
   （interface_deep.py:2543）——DeepAgentState 落盘类操作至多 2 并发，勿绕开。
5. **todo 代际隔离**：todo 更新带 `generation_token`（interface_deep.py +
   stale_todo_cleanup_helpers.py），旧代程的 todo 写入不得覆盖新一代。
6. **workspace 多租户优先级**：调用方显式传入的 `workspace_dir`
   （企业 AgentManager / 个人版 TenantAgentPool）优先，均未传才回退
   `get_agent_workspace_dir()`；`agent_id` / `service_id` 保持企业语义，
   个人版租户身份经 env 命名空间传递，不走这两个字段。
7. **非流式 cron 必须补 commit**：`_persist_cron_checkpoint`
   （interface_deep.py:13954）——cron 走 `_inner_invoke` 时 commit 挂在
   `need_cleanup` 下会被跳过，此处对齐流式路径显式落盘，否则
   `cron_*` 会话 checkpointer 恒空、web 恢复读不到历史。
8. **模型缓存 key 的 `#N` 后缀**：多 entry 同名模型以 `#index` 区分 key；
   取默认模型优先 `is_default` 的纯 model_name key，否则回退第一个 `#index`
   key（interface_deep.py:6814 起）。request_id 注入函数
   `_inject_request_id_into_inputs` 见 :6071（调用点 :19222 / :20039）——
   skill_turbo 等工具从 metadata 直读 `request_id`/`channel_id`，缺失即注册失败。

## 与 session_ops 的接缝（业务归 [[S_04]]）

- interface_deep.py:3610 — 请求前 `warmup_session_context`（try/except 包裹，
  失败仅 warning，随后 `_touch_session_adapter`）；:13948 — invoke 收尾 warmup。
- :23755 — 部分压缩直读 history.json（`_read_history`），配
  `compact_partial_prompts.py` 的提示词。
- 调用语义边界（哪一层负责清 Session / 重建 context）见
  [../docs/specs/S_04_session-ops-and-context-restore.md](../docs/specs/S_04_session-ops-and-context-restore.md) N10。

## 与 openjiuwen（agent-core 仓）的接缝

- **三层消费**：DeepAgent 装配（`core.single_agent`）、session/harness 工厂
  （`harness.factory`）、rails 体系（`harness.rails`）。
- **进程级注册表互斥**：本仓 warm pool 与 openjiuwen 全局注册表共享互斥约束
  （[[S_02]] N2），adapter 侧不得另建平行的实例注册表。
- 改 openjiuwen 公共接口时，先 grep 本目录 556 个方法中的调用点再动手。

## 测试与提交

- 单测在 `tests/unit_tests/agentserver/`（deep_adapter / chat_interrupt /
  steering 系列等）；跑法与提交三连续约定见 [../docs/AGENTS.md](../docs/AGENTS.md)。
- 改中断/续跑路径必跑 `test_chat_interrupt_*`；改插话必跑
  `test_chat_steering_core_integration.py`。
