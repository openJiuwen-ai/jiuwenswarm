# S_02 · 预热体系：就绪会话池与启动预热

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_02 / prewarm-and-warm-pool |
| 关联模块 | `jiuwenswarm/server/runtime/agent_warm_pool.py`、`prewarm.py`、`mem_reclaimer.py`、`agent_manager.py`（facade 段）、`tenant_agent_pool.py`（session prewarm 段） |
| 最近一次修订日期 | 2026-10-21 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

三类"提前把 DeepAgent 弄到可用"的机制及其资源回收：

1. **AgentWarmPool**（每 AgentManager 一个）：进程内就绪 session 槽位池
   （speculative 预建 DeepAgent + prepare_session，等 chat 来 claim）。
2. **启动三阶段预热**（prewarm.py）：进程启动时 import / checkpointer / 临时
   DeepAgent query 的端到端首因开销消除。
3. **session.create 后台预热**（TenantAgentPool.prewarm_session_in_background）
   与 **mem_reclaimer**（周期内存回收）。

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
  try/except 失败仅告警。
- **N9 mem_reclaimer 默认关**：env `JIUWENSWARM_GC_RECLAIM_INTERVAL`（秒，<=0
  或非数字即不启用）；后台 daemon 线程 `gc.collect()` + `malloc_trim(0)`，回收
  失败不终止线程。背景：会话 adapter 循环引用图需攒 ~70k 净存活才触发 gen2，
  高并发轮换下 RSS 只升不降。

## 接口契约

| 入口 | 语义 |
|---|---|
| `AgentWarmPool.sync(enabled_channels, *, config, env) -> stats` | 期望集对账：stale 槽位作废、failed 表按指纹清理、缺槽入 pending；旧 revision 并发到达只报快照 |
| `AgentManager.sync_prewarm_channels / claim_prewarmed_session / wait_for_session_prewarm / begin_foreground_chat / end_foreground_chat / activate_session_prewarm / release_session_prewarm_claim` | warm pool 的 AgentManager 门面；`claim_prewarmed_session` 的 `create_token` 幂等（同 token 异参数 ValueError） |
| `AgentWarmPool.claim(key) -> WarmClaim(session_id, prewarm_hit, prewarm_status)` | status ∈ {"ready", "warming", "bypassed"} |
| `warmup_import_and_checkpointer()` / `warmup_deep_agent_query(...)` | 启动预热阶段入口（prewarm.py）；`run_startup_warmup` 为向后兼容保留 |
| `prewarm_session_in_background(*, request, session_id, ...)` | session.create 后台预热；不写对话历史、不含 LLM 轮次 |
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
- **P4 启动预热**：`warmup_import_and_checkpointer`（阶段1/2）→ 独立
  `create_task(warmup_deep_agent_query)`（阶段3：建临时 agent → 发一条
  chat.send → cleanup）。
- **P5 session.create 预热**：`prewarm_session_in_background` → `extract_ids` →
  `manager.get_agent` → `agent.prepare_session`；失败仅 warning 不阻塞业务。

## 修改检查单

- 改 warm pool 并发/取消路径 → 对照 N2 互斥；新增退出路径对照 N6 清理完备性。
- 改 `WarmKey` 字段 → 同步 `AgentManager.make_key` 入参与 agent_manager 的
  cache_key 语义（[[S_01]] N1）。
- 动企业版预热开关（N1 层1）→ 必须先解决预热路径的请求 identity（metadata.routing）。
- 改启动预热阶段划分 → 保 N8 承载分离：兜底 await 只等快阶段。
