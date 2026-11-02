# S_03 · 配置与环境变量热更新链路

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_03 / config-hot-reload |
| 关联模块 | `jiuwenswarm/server/runtime/agent_manager.py`（reload 段）、`reload_result.py`、`tenant_agent_pool.py`（reload/sync 段）、`sync_agents_configs.py`、`tenant_catalog_registry.py` |
| 最近一次修订日期 | 2026-10-21 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

配置（config.yaml 语义）与环境变量（env）变更**不重启进程**地生效的完整链路：

```
触发（config.set / sync_agents_configs / MCP 批量变更）
  → TenantAgentPool.reload_agents_config（广播）/ reload_tenant_config（单租户）/ apply_sync_config（直通）
  → AgentManager.reload_agents_config（串行锁 + 指纹去重 + 范围决策）
  → agent.reload_agent_config（adapter 侧逐实例应用，含 deferred）
  → 环境变量 staged tip（在途请求读旧值，idle 时 promote）
  → 模型连接增量关闭 + warm pool refresh
```

含 `reload_result.py` 的结果类型、日志红线（只记 key 不记 value）、env 影响域
分类（memory / task memory / shared skills）。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| adapter 侧 `reload_agent_config` 如何重建 rails/模型 | `agent_adapter/AGENTS.md` |
| 实例缓存与租户目录 | [[S_01]] |
| warm pool refresh 的槽位语义 | [[S_02]] |

## 不变量

- **N1 reload 串行锁**：`AgentManager.reload_agents_config` 全程持
  `_reload_lock`——高频触发（批量 MCP 增删）下多个 reload 并发叠加重建大量
  实例会 OOM。任何新 reload 入口都必须经过这把锁（或证明显然无需）。
- **N2 指纹去重**：`_reload_fingerprint` = json(config + env + agent_topology +
  target_channel + target_session + reload_scopes)。指纹与上次相同直接跳过
  （返回空 aggregate，不更新 `_last_reload_fingerprint`）。**拓扑进指纹**：
  即使配置不变，缓存里新增/删除了实例也会重新走一遍 reload。
- **N3 model scope 全局 fan-out**：`reload_scopes` 含 `"model"` 时忽略
  `target_channel_id` 窄化、广播全部 channel——model 是全局共享配置段，否则
  web 保存模型后 IM 长连接通道（xiaoyi 等）的 session adapter 会一直用旧模型
  直到手动 /new_session。新增全局性配置段时评估是否也要进这个 scope 集。
- **N4 env 两段提交**：reload 只 `stage_env_overrides`（tip 袋暂存），在途请求
  继续读旧 tip（或 sealed overlay）；**仅当 `is_working()` 为 False 时**
  `promote_staged_env()`。env 命名空间按 `(service_id, agent_id)` 袋分租户。
  多模态 omission 推断（`infer_multimodal_env_removals`）在 stage 前跑一遍
  reconcile。
- **N5 LLM 连接增量关闭**：`_evict_stale_llm_clients` 只对
  **上次默认集 - 本次默认集** 做差量 `aclose_connections`（OpenAI + Anthropic
  两类 client），绝不碰其它组件仍在用的连接；进程启动后**首次**模型变更无历史
  快照、只登记不关闭。diff key = (client_provider, api_key, api_base, verify_ssl,
  ssl_cert)。
- **N6 日志红线**：hot-reload 日志一律走 `log_agent_config_hot_reload` /
  `log_reload_config_changes`（marker `[agent_config] hot-reload`），**只记
  key/path 名，永不记 value**；错误串先过 `redact_reload_error_message`
  （命中 api_key/secret/token/password 模式 → `[redacted]`，超 120 字截断）。
- **N7 env 影响域分类是判定表**：`reload_result.py` 中四个 frozenset
  （`MEMORY_ENV_KEYS` / `EXTERNAL_MEMORY_ENV_KEYS` / `TASK_MEMORY_ENV_KEYS` /
  `SHARED_SKILLS_ENV_KEYS`）+ `reload_touches_memory`（engine+embed 指纹比对）
  驱动"要不要重建 memory 缓存"。新增会影响 memory / 技能目录的 env 键必须登记
  进对应集合，否则热更新后读到旧引擎。
- **N8 sync 直通通道**：`apply_sync_config`（catalog 同步路径）不走指纹 / 不走
  staged tip，直接 `replace_active_env(clear_staged=True)` 并对在途
  （`is_working`）实例 `queue_pending_reload` 记 deferred。两条通道并存是设计：
  用户界面保存 = 热更新（N2/N4），控制面 catalog 下发 = 直通同步。
- **N9 空缓存也要落 catalog**：`TenantAgentPool.reload_agents_config` 在没有任何
  AgentManager 时仍要 stage env + `_upsert_reload_catalog`——租户尚未创建的
  配置等冷启动创建时生效（`_ensure_agent_manager` 从 registry 回读）。

## 接口契约

| 入口 | 语义 |
|---|---|
| `AgentManager.reload_agents_config(config, env, *, target_channel_id, target_session_id, reload_scopes, reload_trace_id) -> ReloadAggregateResult` | 唯一合法热更新入口；`reload_scopes` 是 set[str]（"model" 等语义标签） |
| `TenantAgentPool.reload_agents_config / reload_tenant_config` | 广播 / 单租户；先 reconcile env 再逐 manager 委托 |
| `AgentManager.apply_sync_config(config, env)` | catalog 直通（N8） |
| `ReloadResult(applied/deferred/error)` → `ReloadAggregateResult(applied:int, deferred:int, failed:list)` | 聚合按 session_key 记失败；`to_payload()` 给上层回包 |
| `ReloadAggregateResult.merge(result, session_key)` | error > deferred > applied 单值归类 |

## 数据结构

- `AgentManager._last_reload_fingerprint`：上次成功应用的指纹；失败
  （reload_completed=False）**不更新**，下次触发重试。
- `AgentManager._last_model_conn_configs: dict[diff_key, ModelClientConfig]`：N5
  的历史快照。
- `TenantCatalogRegistry`：进程内 `(service_id, agent_id) → TenantAgentSpec`
  单例；spec 含 config / env / runtime / revision / content_hash。
- `_last_sync_revision: dict[service_id, revision]`：sync 幂等水位。

## 执行路径

- **P1 热更新**（config.set）：`AgentManager.reload_agents_config` → 指纹 →
  逐 channel 逐 agent `agent.reload_agent_config` → team evolution config 更新 →
  idle 时 `promote_staged_env` → 成功则记指纹 + 异步 `warm_pool.refresh` →
  model scope 时 `_evict_stale_llm_clients`。
- **P2 catalog 同步**：见 [[S_01]] P5；其中每 agent 的 `apply_sync_config`
  走 N8 直通。
- **P3 冷启动回放**：`_ensure_agent_manager`（[[S_01]]）从 registry / tip 袋取
  spec 回放配置；`log_agent_config_hot_reload_replay` 记录回放（计数不记值）。

## 修改检查单

- 改指纹构成 / scope 集合 → 想清楚旧指纹的兼容（会全量 reload 一次，无害但
  别频繁）；N3 的 fan-out 语义不得丢失。
- 新增 env 键 → 若影响 memory / 技能目录 / 模型，登记 N7 判定表。
- 新增 reload 触发源 → 走 `reload_trace_id` 贯穿日志；热更新日志必须经 N6 的
  helper，不许直接 logger.info 打 value。
- 改 env 提交流程 → 保 N4 两段提交；在途请求读旧 tip 的语义不得破坏。
