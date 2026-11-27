# S_02 · 预热体系：就绪会话池与启动预热

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_02 / prewarm-and-warm-pool |
| 关联模块 | `jiuwenswarm/server/runtime/agent_warm_pool.py`、`prewarm.py`、`mem_reclaimer.py`、`agent_manager.py`（facade 段）、`tenant_agent_pool.py`（session prewarm 段） |
| 最近一次修订日期 | 2026-11-27 |
| 关联 feature | F_02_session-prepare.md |

## 范围 / 边界

### 本规约管什么

三类"提前把 DeepAgent 弄到可用"的机制及其资源回收：

1. **AgentWarmPool**（每 AgentManager 一个）：进程内就绪 session 槽位池
   （speculative 预建 DeepAgent + prepare_session，等 chat 来 claim）。
2. **启动三阶段预热**（prewarm.py）：进程启动时 import / checkpointer / 临时
   DeepAgent query 的端到端首因开销消除。
3. **session.create 后台预热**（TenantAgentPool.prewarm_session_in_background）、
   **session.prepare 会话准备**（TenantAgentPool.prepare_session_in_background，
   Relay 在首条消息路由后按 chat.send 的身份提前构建）与 **mem_reclaimer**（周期内存回收）。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| 实例缓存 / 借用 / pin（pin_agent 只是被本模块消费） | [[S_01]] |
| 配置热更新触发的 warm pool refresh | [[S_03]] |
| prepare_session 内部（session adapter 构建） | `agent_adapter/AGENTS.md` |

## 不变量

- **N1 三层开关分层**，谁也不要越层判定：
  - 层1 `AgentWarmPool`：env `JIUWENSWARM_AGENT_PREWARM`，**默认关、仅显式 opt-in**
    （`prewarm_enabled_by_env`）；**企业版恒 False**（预热路径无请求 identity，
    模型依赖 `metadata.routing`，会落到空 config 打 ERROR）。
  - 层2 session.create 预热：env `JIUWENSWARM_SESSION_CREATE_PREWARM`，默认开，
    `=0` 关闭（tenant_agent_pool.prewarm_session_in_background）。
  - 层3 启动预热：`app_agentserver` 启动即跑，无开关（企业版跳过阶段3）。
  - session.prepare：env `JIUWENSWARM_SESSION_PREPARE`，默认开，`=0` 时应答
    `disabled`（`TenantAgentPool.session_prepare_enabled`），与层2 互不影响。
- **N2 openjiuwen 注册表互斥**：工具 / 资源注册在进程全局 manager，speculative
  与前台 DeepAgent 构建绝不并发触碰——AgentWarmPool 用 `_initialization_lock` +
  `begin_foreground` 取消全部后台任务后才让前台构建（`_prepare` 内先
  `await asyncio.sleep(0)` 让取消送达）。改并发路径必须保住这条互斥。
- **N3 排除通道**：`EXCLUDED_CHANNELS = frozenset({"acp", "a2a"})` 不参与 warm
  pool；`is_swarm=True` 的 key claim 恒 bypassed（新建 session id 直接返回）。
- **N4 claim 三分支**：ready 槽位命中（`WarmClaim(sid, True, "ready")`）/
  warming 中 **promote 正在跑的 task**（复用其最终 session id，绝不并发建第二个
  DeepAgent）/ 都没有则前台调度 `keep_as_slot=False`。claim 成功即登记
  `_claimed_pins` + 300s 超时自动释放 pin。
- **N5 revision 淘汰**：WarmRevision = (boot_id, config_fingerprint, sequence)。
  config 指纹变化即作废全部槽位与失败表；desired 集合 = enabled_channels ×
  (可见 projects + `DEFAULT_PROJECT_ID_WORK` + `DEFAULT_PROJECT_ID_CODE`)。
  默认并发上限 `max_concurrency=1`、`max_ready_slots=1`，后台批间 cooldown 0.25s，
  且**仅在前台聊天计数为 0 时泵任务**（`_pump_background_locked`）。
- **N6 marker 文件生命周期**：`.prewarm/{session_id}.json` 携带 boot_id；构造时
  清扫异 boot marker 且**只删 session 目录下无 metadata.json 的未初始化目录**
  （路径校验 `session_dir.parent == sessions_root`）。claim 落地（成为正式会话）
  后 `clear_marker`。任何新退出路径都要保证 marker 与 runtime 一起清
  （`_dispose_runtime` finally 段）。
- **N7 WarmupModelClient 零 monkeypatch 注入**：类定义即经
  `BaseModelClient.__init_subclass__` 注册进进程级 ClientRegistry
  （provider="warmup"），`_build_warmup_config_base` 覆盖 defaults 列表 / legacy
  单条 / react 段三条路径把 client_provider 指到它。预热走通全链路构建、只短路
  最终 HTTP 调用，不消耗 token。预热 agent **独立构造、不进 AgentManager 缓存、
  用完 cleanup**。
- **N8 阶段承载分离**：阶段1（interface_deep import）+ 阶段2（checkpointer）由
  `_startup_warmup_task` 承载、可被兜底 await；阶段3（临时 DeepAgent query，真实
  LLM 最长 120s）独立 task——**不得**让兜底 await 被阶段3 拖住。三阶段全部
  try/except 失败仅告警。启用启动预热且部署配置 `flash.enabled=true` 时，进程级
  checkpointer 初始化与配置、模块预热并行。请求仍通过已有入口等待就绪或重试。
  创建 Flash 适配器时，实例 checkpointer 的异步 I/O 可与 Flash 基础配置补缺和覆盖重叠。
  两项完成后才继续装配或复用该实例。会话状态恢复仍使用真实会话身份。
- **N9 mem_reclaimer 默认关**：env `JIUWENSWARM_GC_RECLAIM_INTERVAL`（秒，<=0
  或非数字即不启用）；后台 daemon 线程 `gc.collect()` + `malloc_trim(0)`，回收
  失败不终止线程。背景：会话 adapter 循环引用图需攒 ~70k 净存活才触发 gen2，
  高并发轮换下 RSS 只升不降。
- **N10 session.prepare 与 chat 同路由、只读**：模式解析与工作目录只走
  `handlers/_default.py` 的 `resolve_chat_turn_routing` / `resolve_readonly_project_dir`
  （chat.send 的同一函数，不写 metadata.json；未带工作目录时构建实例仍按现有逻辑建默认工作区
  `projects/<session_id>`）；`_uses_explicit_officeclaw_mode`
  必须认 `SESSION_PREPARE`，否则准备按全局 Flash 开关解析、与 chat 静默不一致。
  只接受解析结果为 agent / flash 且无子模式的请求。工作目录与 chat 同口径：请求值按该模式
  是否退回旧字段（`cwd` / `workspace_dir` / `trusted_dirs`）解析，会话已锁定的值优先
  （`prefer_locked=True`）；只读 RPC 复用同一函数但保持请求值优先，且沿用调用方已算好的
  请求值，不重复解析。解析出的生效目录回写到本次请求的 `params` / `metadata`（只改内存里的
  请求对象，不落盘），与 chat 轮一致：后台构建拿到的请求值和传入的 `project_dir` 是同一个。
- **N11 准备按请求身份构建**：`prepare_session(request=...)` 在门面层绑定
  `_bind_tenant_request_context(request)`，并把 request 传进
  `_get_or_create_session_adapter`（线程 ID、企业请求身份），与 chat 建出的实例一致。
  session.create 预热不传 request，行为不变。
- **N12 准备与预热共用任务表**：session.prepare 与 session.create 预热同用
  `_session_prewarm_tasks`，同一会话至多一个在途任务（排队中或构建中再来一个应答
  `already_running`）；chat 经现有 `wait_for_session_prewarm` 加入，不重复构建。在途任务
  覆盖 `prepare_session` 全程（含 `configure_session_runtime`），消息处理的入口都先等它结束
  再取实例，所以准备的配置与对话轮的 `_update_runtime_config` 不会并发作用在同一实例上。
  反过来对话先到（已过等待、正在建实例）时，对话建完会取走追踪器里该会话的记录：还没开始的
  准备任务整个跳过，已在等会话锁的准备拿到实例后不再 `configure_session_runtime`、不登记回收。
- **N13 准备队列**：session.prepare 全进程同时构建 1 个（单 worker
  `_drain_session_prepare_queue`），排队上限 `SESSION_PREPARE_MAX_QUEUED = 8`，超出应答
  `queue_full`。worker 等一个构建最多 `SESSION_PREPARE_BUILD_WAIT_SEC = 60` 秒，超时只记 warning、
  转去启动下一个，不取消该构建（卡住的构建不能让后面的准备永远轮不到）。`wait_for_session_prewarm` 遇到该会话还在排队的准备就移出队列（请求自己构建，
  不排在别的准备后面），之后照常检查并加入同会话在途的构建。session.create 预热不进队列。
- **N14 未使用回收**：准备新建的会话实例一建好（`configure_session_runtime` 之前）就登记在根
  适配器 `_prepared_unused_session_timers`，`PREPARED_SESSION_UNUSED_TTL_SEC = 600` 秒后按
  `_evict_idle_session_adapters` 同款守卫（不在等待工具审批、会话锁未持有且无人等待、无存活
  子代理）调用 `cleanup_session_adapter`。登记保留到回收成功：守卫不满足或清理未成功时每
  `PREPARED_SESSION_RECLAIM_RETRY_SEC = 60` 秒重试，期间被对话取用仍按首次取用记命中结果。
  非对话请求先取用时只作废回收，命中结果留给第一条对话（按追踪器里的 building / ready 判定）。
  被 `_get_or_create_session_adapter` 取用一次即撤销登记，转为普通会话实例。
  `_drop_session_adapter_cache_entry` 与根 `cleanup` 同步撤销计时器。
- **N15 配置同步后后台热更新**：`_mark_session_adapters_stale_for_reload` 之后，对仍未使用
  的准备实例在后台持 `_session_adapter_locks[sid]` 执行 `_reload_session_adapter_if_stale`；
  chat 到达时在同一把锁上等它完成。后台失败不登记重试冷却，chat 取用时照常懒重载。
- **N16 命中日志**：`session_prepare_tracker`（进程内，上限 1024）记录准备各步状态与 chat 的
  加入等待；officeclaw chat.send 第一次取得会话实例时，`_get_or_create_session_adapter` 记一条
  `[session.prepare] chat result=hit|joined|hit_reloaded wait_ms=` 或 `result=miss reason=`，
  同一会话只记一次（准备实例靠作废回收计时器判定首次取用，新建实例只建一次）。

## 接口契约

| 入口 | 语义 |
|---|---|
| `AgentWarmPool.sync(enabled_channels, *, config, env) -> stats` | 期望集对账：stale 槽位作废、failed 表按指纹清理、缺槽入 pending；旧 revision 并发到达只报快照 |
| `AgentManager.sync_prewarm_channels / claim_prewarmed_session / wait_for_session_prewarm / begin_foreground_chat / end_foreground_chat / activate_session_prewarm / release_session_prewarm_claim` | warm pool 的 AgentManager 门面；`claim_prewarmed_session` 的 `create_token` 幂等（同 token 异参数 ValueError） |
| `AgentWarmPool.claim(key) -> WarmClaim(session_id, prewarm_hit, prewarm_status)` | status ∈ {"ready", "warming", "bypassed"} |
| `warmup_import_and_checkpointer()` / `warmup_deep_agent_query(...)` | 启动预热阶段入口（prewarm.py）；`run_startup_warmup` 为向后兼容保留 |
| `prewarm_session_in_background(*, request, session_id, ...)` | session.create 后台预热；不写对话历史、不含 LLM 轮次 |
| `prepare_session_in_background(*, request, mode, adapter_mode, project_dir, before_build) -> str` | session.prepare 入队；返回 `scheduled` / `already_running` / `queue_full`（N13） |
| `JiuWenSwarmDeepAdapter.is_prepared_session_unused(session_id)` | 准备实例是否尚未被对话取用（N14） |
| `session_prewarm_running(session_id) -> bool` / `session_prepare_enabled()` | 在途判定 / 准备开关 |
| `handlers/session_prepare.handle_session_prepare(ctx)` | 立即应答 `{session_id, accepted, status}`；status ∈ scheduled / already_running（accepted）、already_ready / queue_full / mode_not_supported / unknown_agent / disabled（不受理）；`INVALID_PARAMS` / `invalid_tenant_id` 为错误应答 |
| `mem_reclaimer.setup()` | app_agentserver 启动时调用，幂等无害 |

## 数据结构

- `WarmKey`（frozen）：(channel_id, project_id, project_dir, work_mode, is_swarm,
  adapter_mode)；`agent_sub_mode` 派生规则：work_mode=="code" → "normal"。
- `WarmSlot`：key + session_id + revision + agent + ready_at。
- `TenantAgentPool._session_prewarm_tasks`：跨单例类级 dict；`wait_for_session_prewarm`
  用 `asyncio.wait`（不传播异常）join 后 chat 才走 get_agent，避免与后台预热
  竞争建 adapter。

## 执行路径

- **P1 槽位预热**：`sync` → `asyncio.to_thread(_desired_keys)`（项目发现移出事件
  循环）→ 锁内对账 → `_pump_background_locked` → `_prepare` →
  `manager.get_agent` → `agent.prepare_session` → 发布 WarmSlot + pin。
- **P2 claim**：`AgentManager.claim_prewarmed_session` → `warm_pool.claim` →
  N4 三分支 → 调用方 `wait_for_session_prewarm` join → `activate_session_prewarm`
  清 marker。
- **P3 前台抢占**：`begin_foreground`（取消后台任务）→ chat 正常建 agent →
  `end_foreground` → cooldown 后恢复泵。
- **P4 启动预热**：`warmup_import_and_checkpointer`（阶段 0/0.5/1/2；阶段 0.5 在后台
  线程预热 tiktoken 词表并把 `TIKTOKEN_CACHE_DIR` 钉到 `{root}/cache/tiktoken`，
  仅在 `react.context_engine_config.enable_tiktoken_counter` 开启时生效）→ 独立
  `create_task(warmup_deep_agent_query)`（阶段3：建临时 agent → 发一条
  chat.send → cleanup）。
- **P5 session.create 预热**：`prewarm_session_in_background` → `extract_ids` →
  `manager.get_agent` → `agent.prepare_session`；失败仅 warning 不阻塞业务。
- **P6 session.prepare**：`handle_session_prepare` → 参数校验 → 开关 →
  `require_officeclaw_agent`（未登记 → unknown_agent）→ `resolve_chat_turn_routing`
  （N10）→ 在途 / `find_agent_for_session_nowait` 已有 → 入队（N13）→ 应答 → worker
  逐个 `_schedule_session_prewarm`：`ensure_interface_deep_and_checkpointer` → P5 同一
  get_agent → `prepare_session(request=...)`（N11）→ 新建的实例登记未使用回收（N14）。
- **P7 配置同步**：根 `reload_agent_config` → `_fan_out_reload_to_session_adapters` →
  `_mark_session_adapters_stale_for_reload` → `_reload_prepared_sessions_in_background`（N15）。

## 修改检查单

- 改 warm pool 并发/取消路径 → 对照 N2 互斥；新增退出路径对照 N6 清理完备性。
- 改 `WarmKey` 字段 → 同步 `AgentManager.make_key` 入参与 agent_manager 的
  cache_key 语义（[[S_01]] N1）。
- 动企业版预热开关（N1 层1）→ 必须先解决预热路径的请求 identity（metadata.routing）。
- 改启动预热阶段划分 → 保 N8 承载分离：兜底 await 只等快阶段。
- 改 chat 的模式 / 工作目录解析（`_prepare_code_mode_chat_turn`）→ 改动须落在
  `resolve_chat_turn_routing` 内，并跑 session.prepare 与 chat 的选型等价用例（N10）。
- 改 chat 建会话实例时绑定的上下文（门面 `process_message_stream`）→ 同步
  `JiuWenSwarm.prepare_session` 的 request 分支（N11）。
- 新增会话实例的移除路径 → 确认经过 `_drop_session_adapter_cache_entry`，否则
  未使用回收计时器残留（N14）。改空闲淘汰守卫 → 同步 `_reclaim_unused_prepared_session`。
- 新增调用 `wait_for_session_prewarm` 的请求类型 → 它会把同会话排队中的准备移出队列（N13）。
