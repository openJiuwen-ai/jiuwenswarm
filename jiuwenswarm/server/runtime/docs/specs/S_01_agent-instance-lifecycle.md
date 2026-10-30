# S_01 · Agent 实例生命周期与租户隔离

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_01 / agent-instance-lifecycle |
| 关联模块 | `jiuwenswarm/server/runtime/agent_manager.py`、`tenant_agent_pool.py`、`tenant_catalog_registry.py`、`session_owner_routing.py`、`runtime_scope.py`、`tenant_context.py` |
| 最近一次修订日期 | 2026-10-21 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

Agent 实例从请求到缓存的全生命周期：**请求路由 → 租户池 → AgentManager →
JiuWenSwarm 门面实例的缓存 / 借用 / 退休 / 重建**。即：

- `TenantAgentPool`（进程单例）：`(agent_id, service_id, workspace_key)` 三元组
  LRU 缓存 `AgentManager`；租户 workspace 目录布局与播种；catalog 同步
  （`sync_agents_configs`）三分支；officeclaw 租户注册守卫；跨租户 HITL 续跑找回。
- `AgentManager`（每租户一个）：`agents[channel_key][cache_key]` 两级缓存；实例
  创建 / 借用（borrower）/ 钉住（pin）/ TUI 空闲退休；HITL 续跑 owner 排序；
  `recreate_agent`；会话 id 分配。
- 隔离原语：`RuntimeScopeKey`（进程级注册表隔离键）、`tenant_context.py`
  （workspace ContextVar 绑定）、`session_owner_routing.py`（owner 身份回绑）。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| 预热（warm pool / 启动三阶段预热 / 内存回收） | [[S_02]] |
| 配置 / env 热更新链路与指纹 | [[S_03]] |
| fork / rewind / restore / compact / warmup context | [[S_04]] |
| DeepAdapter 内部（事件流、rails、interrupt 状态机） | `agent_adapter/AGENTS.md` |
| 会话历史持久化 / session metadata | `session/`（会话与配置子模块） |

## 不变量

- **N1 缓存键唯一公式**：AgentManager 的 cache_key 恒为
  `f"{mode}:{sub_mode}:{project_dir}"`，其中 `project_dir` 经
  `os.path.normcase(os.path.abspath(os.path.expanduser(...)))` 归一（agent_manager.py
  `_normalize_project_dir` / `_make_agent_cache_key`）。改公式必须同步
  `get_agent` / `get_agent_nowait` / `_create_agent` 三处与 warm pool 的
  `WarmKey` 语义。
- **N2 plan 子模式并轨**：`collapse_plan_sub_mode` 把 `agent.plan → ""`、
  `code.plan → "normal"`（`_PLAN_SUB_MODE_ALIASES`，仅此两键）。plan 是会话运行期
  状态（`DeepAgentState.plan_mode`）而非装配形态——用户开关 Plan 必须命中同一
  实例，否则 context_engine 换实例即丢整段对话历史。新增 mode 想带 plan 子模式，
  先在这里登记别名，不许绕过。
- **N3 借用与钉住是两条独立引用通道**：`_agent_borrowers` 按 asyncio task 记借用
  （请求结束自动释放），`_agent_pins` 计数式持久后台所有权。TUI 空闲退休
  （`_retire_tui_agent_if_idle_locked`）要求 pins==0 且无活 borrower 才能销毁；
  退休失败回滚要恢复 agents 与 create_params 条目。
- **N4 HITL 续跑三要素判定**：`_is_interrupt_resume_request` =
  `source ∈ {permission_interrupt, confirm_interrupt, ask_user_interrupt}` **且**
  `params.answers` 是 list **且** `request_id` 非空，三者缺一不可
  （agent_manager.py 与 tenant_agent_pool.py 各有一份同构实现）。owner 排序
  `(2, last_used)` = 内存中挂着该 session 的暂停工具，`(1, last_used)` = 仅
  awaiting-permission 标记；仅持有 session runtime 的旧 cron owner 不是候选。
- **N5 会话 id 生成**：显式传入直接透传；自动生成
  `f"{prefix}_{int(time.time()*1000):x}_{uuid4().hex[:12]}"`；channel `__cron__`
  的目录前缀映射为 `cron`（白名单安全）。改格式必须核对 session-id 白名单。
- **N6 租户键与目录布局**：`TenantAgentPool._build_cache_key` 返回三元组 tuple
  （防分隔符碰撞）。个人版目录 `service_{sid}/agent_{aid}/`，企业版
  `workspace_{key}/`；企业版 AgentManager 的 `agent_id` 是 `f"{aid}_{sid}"`
  复合形式，而 env 命名空间仍用原始 `(env_service_id, env_agent_id)`——两套 id
  语义不可混用。
- **N7 LRU 不设容量与 TTL 上限**（默认 `cache_max_size=None`）：防止 ask_user
  长阻塞期间 AgentManager 被误淘汰。请求结束后 `_refresh_agent_manager_cache`
  touch 时间戳；给 cache 加上限前先想清楚 HITL 暂停会不会被中途逐出。
- **N8 catalog 同步三分支**：`sync_agents_configs` 以 `revision` 幂等去重、
  `content_hash` 判 unchanged、动作只允许 removed / added / updated；
  **added 失败必须回滚 `_evict_manager_cache`**。evict 连带清理
  `RailManagerPool` / `DeepResearchTaskManagerPool` / `AgentCronRegistry` 三个
  进程级池——新增租户级进程池必须挂进这个清理链。
- **N9 officeclaw 守卫**：`require_officeclaw_agent` 对命名租户 fail-closed
  （catalog 未注册 → `tenant_not_registered`），仅 `default/default` legacy 放行。
- **N10 跨租户续跑归属校验**：跨租户找回 interrupt owner 后，`cron_*` 会话在
  remote storage 模式下必须过 `_cron_resume_session_allowed`（查会话详情）
  fail-closed，防仅凭 session_id 操作他人任务。
- **N11 RuntimeScopeKey 对齐**：`RuntimeScopeKey.from_request` 内部调用
  `TenantAgentPool.extract_ids`（含 ACP 特例 → `("acp","global_acp","workspace_acp")`）。
  新增进程级注册表（Team / Rail / Ask / DeepResearch 同类）的隔离键一律走
  `RuntimeScopeKey`，不许自拼 tuple。
- **N12 workspace_key 禁 `__`**：`bind_workspace_key` 对含 `__` 的值抛
  ValueError（目录名安全）。

## 接口契约

| 入口 | 语义 |
|---|---|
| `TenantAgentPool.get_instance()` | 进程单例；`peek_instance()` 不触发构造 |
| `TenantAgentPool.process_message[_stream](request)` | 顶层入口：officeclaw guard → 跨租户 HITL 找回 → `extract_ids` → `_ensure_agent_manager` → 委托 AgentManager |
| `TenantAgentPool.sync_agents_configs(params)` | `_sync_lock` 串行；返回 per-agent 结果列表 |
| `AgentManager.get_agent(channel_id, mode, project_dir, sub_mode, request)` | 自动创建 + 双检锁（`_get_agent_create_lock`，WeakValueDictionary） |
| `AgentManager.get_agent_nowait(...)` | 同步、不创建；无参时按 `mode=="agent"` 优先回退任意实例 |
| `AgentManager.find_agent_for_interrupt_resume_nowait(session_id)` | N4 排序选优 |
| `AgentManager.recreate_agent(channel_id, immediate=True)` | 备份 create_params → cleanup → 立即按原参数重建（`/sandbox` 等换 SysOperationCard 场景） |
| `AgentManager.cleanup_session_runtime(channel_id, session_id)` | 聚合失败数，>0 时 raise RuntimeError；调用方要 best-effort 须自行 try/except |
| `bind_request_to_session_owner(request, owner)` | 仅回绑 implementation 身份（`resolved_adapter_mode/source`），业务 mode / plan 状态不动 |

## 数据结构

- `AgentManager.agents: dict[channel_key, dict[cache_key, JiuWenSwarm]]`——进程内
  两级缓存；`_agent_create_params` 镜像记录创建参数供 recreate。
- `AgentManager._session_create_tokens: dict[(channel_id, token), (create_signature, claim)]`
  ——`claim_prewarmed_session` 幂等表；同一 token 换参数立即 ValueError。
- `TenantAgentPool._agent_wrappers: AsyncLRUCache`——key 三元组（N6）。
- `TenantAgentPool._session_prewarm_tasks: ClassVar[dict[str, Task]]`——类级（跨
  单例）session.create 后台预热任务表；`wait_for_session_prewarm` 用
  `asyncio.wait` join（不传播预热异常，chat 走正常 get_agent 路径兜底）。

## 执行路径

- **P1 请求路由**：`TenantAgentPool.process_message_stream` → guard / 续跑找回 →
  `extract_ids`（tenant_agent_pool.py）→ `_ensure_agent_manager`（双检锁）→
  `AgentManager.process_message_stream` → `_get_agent_for_request` → 命中缓存
  `_borrow_agent`，否则 `_create_agent`（JiuWenSwarm 门面 + `create_instance`）。
- **P2 interrupt 续跑**：`_is_interrupt_resume_request` →
  `find_agent_for_interrupt_resume_nowait`（manager 内 → 跨租户 pool 内，两级）→
  `bind_request_to_session_owner`；无 owner 时 cron 回退 `__cron__` channel，
  其余回退 `request.channel_id`。
- **P3 mode 恢复**：`_resolve_request_mode`——续跑请求未携带 mode 时从 session
  metadata 读回原始 mode（防 `code.normal` 降级 `agent` 找错实例），并回写
  `params["mode"]` 供下游。
- **P4 TUI 退休**：`cleanup_session_runtime` / borrower 释放 →
  `_schedule_pending_tui_retirement` → `_retire_tui_agent_if_idle`（持 create_lock）
  → detach 后 `agent.cleanup()`；失败恢复条目并 raise。
- **P5 catalog 同步**：`sync_agents_configs`（pool）→ `validate_sync_payload`
  （sync_agents_configs.py）→ diff → removed: `_evict_manager_cache`；added/updated:
  `replace_active_env` → `_ensure_agent_manager` → `apply_sync_config`（[[S_03]]）→
  `registry.upsert`。

## 修改检查单

- 改 cache_key / mode 归一 → 同查 N1 列出的三处 + `WarmKey`（[[S_02]]）+
  `_agent_lookup_from_request` 与 handler 侧 `resolve_request_runtime_mode` 的一致性。
- 改续跑判定或 owner 排序 → agent_manager.py 与 tenant_agent_pool.py 的**两份同构
  判定一起改**，并核对 interface_deep 的 `guard_stale_interrupt_response` 终态卡拒绝。
- 给 TenantAgentPool 缓存加淘汰策略 → 先评估 N7（HITL 暂停存活）与
  `_refresh_agent_manager_cache` touch 路径。
- 新增进程级租户池 → 挂进 `_evict_manager_cache`（N8）并用 `RuntimeScopeKey`（N11）。

## 与其它 spec 的关系

[[S_02]] 预热从本 spec 的 AgentManager 借 `get_agent` / `pin_agent`；
[[S_03]] 热更新经本 spec 的 `reload_agents_config` 落到每个实例；
[[S_04]] 的 warmup context 在 adapter 重建（本 spec 的退休 / 重建路径）后触发。
