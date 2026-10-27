# Runtime Agent 管理

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_03 / runtime-agent-management |
| 关联模块 | jiuwenswarm/server/runtime/{agent_manager.py, tenant_agent_pool.py, agent_warm_pool.py, agent_adapter/, a2ui/, debug_trace/, session/, skill/, workspace/, agent_config_service.py, prewarm.py, proactive_adapter.py, team_binding_store.py, team_entity_store.py, team_snapshot_refresh.py} |
| 最近一次修订日期 | 2026-10-27 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

- AgentManager（单租户）与 TenantAgentPool（企业多租户）两种形态的边界：默认 AgentServer 进程内只构造裸 AgentManager（workspace_key="default"）；企业版 `TenantAgentPool.get_instance()` 按 TenantContext → per-tenant AgentManager 池。
- AgentWarmPool：按 `WarmKey(channel, project, work_mode)` 维度缓存已创建的 agent 实例；`sync()` 按期望 key 集合重建；排除 `acp` / `a2a` 通道。
- prewarm.py 三阶段：`warmup_import_and_checkpointer()`（阶段 1+2）与 `warmup_deep_agent_query()`（阶段 3，独立 fire-and-forget，mock_model 默认 True）。
- agent_adapter 三路：`interface_deep`（默认原生 DeepAgent）/ `interface_code` / `interface_flash`；`AgentWebSocketServer._resolve_adapter` 按 req_method / channel 选。
- A2UI 协议（Agentic UI）：runtime/a2ui/protocol.py 定义 + runtime/stream.py + runtime/formatter.py + runtime/response_finalization.py + runtime/finalizer.py。
- debug_trace：/debug 模式调试追踪（stream_logger + directives + subagent_capture + task_tool_patch）。
- session/ 域：session_manager（任务队列 + 优先级 + 一次性 session 判定）+ session_history + session_metadata + project_store + git_diff_watcher + kv_cache_affinity + permission_interrupt_cancel / permission_response_ledger + work_mode。
- skill/ 域：skill_manager + source_registry + artifact_security + enterprise_state_lock + skilldev 自演进流水线（init → plan → generate → desc_optimize → test_design → test_run → evaluate → improve → package → validate）。
- workspace/ 域：fs_quota_guard + policy_reload + usage_reconciler。
- AgentWebSocketServer 在企业版冷加载期间调用的 `reload_task_memory_config_from_gateway_db` / `reload_memory_config_from_gateway_db` / `reload_logging_levels` / `reload_audit_log_config_from_db` / `reload_permissions_from_gateway_db` / `reload_quota_policies_from_gateway_db` 序列。

### 本规约**不**管什么

| 子主题 | 归属 spec |
|---|---|
| HANDLERS 注册表 / 默认路径 fallback / Sink 契约 | [[S_02]] request-pipeline-dispatch |
| sandbox auto-start 条件链、JiuwenBoxRunner | [[S_04]] sandbox-lifecycle |
| im_connector Plugin、im_hosting 托管 | [[S_05]] im-channel-integration |

## 不变量

1. **AgentWarmPool 排除 acp/a2a**：WarmKey.channel ∈ {acp, a2a} 的请求不走预热池，每次按需创建（外部 agent control plane 自身管理生命周期）。
2. **prewarm 阶段 3 独立 fire-and-forget**：阶段 3（临时 DeepAgent + query mock 默认 True）不被阶段 1/2 的兜底 await 阻塞，避免首请求被 LLM 真实调用阻塞。
3. **TenantAgentPool 仅企业版启用**：非 enterprise 进程内 `AgentWebSocketServer._agent_manager` 是裸 AgentManager，workspace_key="default"；tenant_pool 属性为 None。
4. **agent_adapter 三路正交**：interface_deep（默认）/ interface_code（编码模式）/ interface_flash（轻量）；`_resolve_adapter` 按 req_method / channel / 配置选，**新增接口必须确认归哪一路**，不要让三路重叠。
5. **A2UI / A2A / ACP 三条出站通道正交**：A2UI（面向 UI 的结构化 agent 输出）、A2A（agent-to-agent 协议）、ACP（外部 agent control plane）互不替代。
6. **debug_trace 只在 /debug 模式生效**：非 debug 或 include_subagent_flow=False 时走原始 invoke，零回归。
7. **session/ 只管会话生命周期**：不碰 agent 装配（归 agent_manager）、不碰 sandbox 启动（归 AgentWebSocketServer.start()）、不碰 IM 托管（归 im/）。跨域调用必须显式过门面。
8. **skilldev 流水线 11 阶段顺序固定**：init → plan → generate → desc_optimize → test_design → test_run → evaluate → improve → package → validate。pipeline.py 是编排真源，不要在 service.py 里硬改顺序。
9. **AgentManager 单进程单默认实例**：非 enterprise 进程内至多一个裸 AgentManager（由 AgentWebSocketServer 构造），不要在其他模块私自 AgentManager()。
10. **企业版冷加载 best-effort**：每个 `reload_*_from_gateway_db` 调用失败只记 warning，不阻断 AgentServer 启动——用户配置 PVC 缺表或缺字段不应让进程起不来。
11. **code.normal/plan 首建与重载共享工具懒加载策略**：首次创建的 None/空白 sub_mode 默认 normal；后续 `create_instance(sub_mode=None)` 沿用保存的 profile，并把有效 sub_mode 传入 rail 装配及后续 session 子 adapter，显式 normal/空白可切回 normal。首建按入参规范化后的 sub_mode 排队并显式传给 builder，配置热重载未传该参数时使用 `_session_instance_sub_mode`；仅 normal/plan 装配 `ProgressiveToolRail`。code.team 对齐普通 team，首建和完整/部分重载均不新增该 rail；历史误挂实例在 configure 成功后注销。单 agent 复用 `react.tool_lazy_load` 的开关、模型白名单及请求级 MCP allowlist，配置缺省重载保持既有 rail；编码记忆、LSP、模式切换与计划审批工具保持 eager，最终可见性仍受模式、技能门控及 `disabled_tools` 约束。一次 code 挂载仅记录一条带 `[JiuwenSwarmCodeAdapter]` 和规范化 sub_mode 的适配器日志。普通 agent/team 的装配、日志及重载策略不因此改变。

## 接口契约

### 普通工作请求的 Flash 默认实现

`resolve_request_runtime_mode()` 对 `officeclaw` 普通工作请求采用与其它渠道相同的
`flash.enabled` 默认选型。该配置沿用配置加载层对
`JIUWENSWARM_FLASH_ENABLED` 的解析；环境变量未设置时保留配置回退。
显式 `mode=flash` 不受默认开关关闭影响。历史办公参数 `agent.plan` 未满足
真正的 Plan 准入条件时，先归一为普通 Agent，再应用默认选型。
Code、Team 和真正的 Plan 保留既有路由。

环境配置选中 Flash 时，普通请求保留 `agent` / `agent.work.normal` 业务模式，
通过 `adapter_mode=flash` 选择实现；会话创建将同一实现选择传给预热。
已有 Flash 亲和、暂停实例查找及审批恢复保护继续复用；OfficeClaw 的审批、确认和
问答中断续跑不应用默认开关，沿用恢复出的模式。本次默认选型调整不新增
Deep 会话锁定：没有暂停实例保护的普通 Deep 续聊可能随当前开关改选 Flash。
技能检索仍由 `flash.skill_selection.enabled` 独立控制。

### AgentManager（简化）

```python
class AgentWebSocketServer:
    def __init__(self, *, user_workspace_dir: str | None = None,
                 workspace_key: str = "default"): ...
    # 按 req_method / channel / config 选 agent_adapter 接口
    def _resolve_adapter(self, ...) -> InterfaceBase: ...
    # 预热池支持
    agent_warm_pool: AgentWarmPool | None
    # SkillManager 单例持有
    skill_manager: SkillManager
```

### AgentWarmPool

```python
@dataclass(frozen=True)
class WarmKey:
    channel: str
    project: str | None
    work_mode: str | None

class AgentWarmPool:
    def sync(self, expected_keys: list[WarmKey]) -> None:
        # 按期望 key 集合重建预热池
        # 排除 channel ∈ {acp, a2a}
```

### prewarm

```python
async def warmup_import_and_checkpointer() -> None: ...   # 阶段 1+2（阻塞 listen 前必须 await）
async def warmup_deep_agent_query(query, channel_id, mode, timeout_s, mock_model=True) -> None: ...
```

### agent_adapter 接口三路

```python
# 基类（jiuwenswarm.server.runtime.agent_adapter.interface）
class AgentAdapterBase:
    async def invoke(self, ctx, request) -> None: ...
    async def invoke_stream(self, ctx, request) -> AsyncIterator: ...

# 三路实现
interface_deep  # 默认原生 DeepAgent（default）
interface_code  # 编码模式
interface_flash # 轻量模式
```

### A2UI 协议

```python
# 核心结构（runtime/a2ui/protocol.py）
class A2UIMessage:
    type: str
    content: Any
    metadata: dict[str, Any] | None

# runtime 层处理链
response_finalization → stream_guard → validator → a2ui 输出
```

## 执行路径 / 装配关系

### P1：Agent 装配（默认路径 fallback 里的 _handle_stream / _handle_unary）

```
dispatch_parsed_request → 未命中 HANDLERS
  → _handle_stream(ctx, request) / _handle_unary
    → AgentWebSocketServer._resolve_adapter(req_method, channel, config)
      → interface_deep / interface_code / interface_flash（选一路）
    → adapter.invoke(ctx, request) → openjiuwen Runner.invoke
      → AgentWarmPool 复用（warm_key 存在 → 复用；不存在 → 创建新 agent）
      → 流式 → A2UI runtime 链（response_finalization → stream_guard → validator）
      → debug_trace（/debug 模式下 task_tool_patch 捕获 subagent 流）
      → ctx.sink.send_wire（E2A wire 编码）
```

### P2：AgentWarmPool.sync（预热池同步）

```
AgentWebSocketServer / AgentManager 启动时
  → AgentWarmPool.sync(expected_keys)
     → 计算期望 WarmKey 集合（排除 acp/a2a）
     → 存在且 config 指纹未变 → 复用（只刷新活跃时间）
     → 不存在 → 创建新 agent 实例（AgentManager 底层调）
     → 存在但 config 指纹变 → 销毁旧实例 + 创建新实例
```

### P3：skilldev 自演进流水线

```
SkillDevPipeline.run(context)
  → stages/base.py 统一接口（init / generate / desc_optimize / ...）
  → 11 阶段固定顺序编排
  → 每阶段输入/输出 state_utils.py 统一管理
  → store.py 持久化中间态
  → workspace.py 技能构建工作区
  → 完成 → artifact_security.py 校验 + SourceRegistry 注册
```

### P4：企业版冷加载序列（AgentWebSocketServer 构造后、start 前）

```
TaskMemoryConfig:   reload_task_memory_config_from_gateway_db
MemoryConfig:       reload_memory_config_from_gateway_db
LoggingLevels:      reload_logging_levels
AuditLog:           reload_audit_log_config_from_db(service=SERVICE_AGENTSERVER)
Permissions:        reload_permissions_from_gateway_db
WorkspaceQuota:     reload_quota_policies_from_gateway_db(force=True)
UsageReconciler:    start_usage_reconciler()（启动周期回收）
```

## 修改检查单

- **改 Flash 默认选型** → 覆盖 OfficeClaw 环境开关双向取值、显式 Flash、普通请求业务模式、会话创建到预热的实现传递、流式与非流式聊天及审批 owner 恢复；确认 Code、Team 和真正的 Plan 不被默认开关覆盖。
- **新增 agent_adapter 接口** → 确认归哪一路（deep/code/flash）→ AgentWebSocketServer._resolve_adapter 里加分支 → 默认路径 fallback 里跑双接口一致性测试。
- **改 code 工具装配或懒加载策略** → 验证首次模型调用已过滤大量 MCP schema、编码工具仍可见、deferred 工具可搜索调用；确认关闭开关、模型白名单、禁用工具及热重载替换行为。
- **改 AgentWarmPool WarmKey 结构** → 同步 sync() 里的重建条件 → 排除 acp/a2a 分支不被意外打破 → 跑预热池一致性测试。
- **改 prewarm 阶段** → 确认阶段 3 仍是独立 fire-and-forget，不被阶段 1/2 兜底 await 阻塞 → mock_model 默认 True 不消耗 token。
- **增删 skilldev 阶段** → pipeline.py 编排顺序同步 → service.py 的 stage 实例化清单同步 → 阶段 base.py 接口签名不变。
- **改企业版冷加载顺序** → 每个 reload 调用失败只记 warning → 不阻断 AgentServer 启动（best-effort）→ PVC 缺表场景下进程仍能起来。
- **改 A2UI 协议** → runtime/a2ui/protocol.py 同步 → runtime 三层处理链（response_finalization / stream_guard / validator）同步 → 前端消费方不崩。

## 与其它 spec 的关系

- [[S_02]] 默认路径 fallback（_handle_stream / _handle_unary）是 AgentManager 的调用入口
- [[S_04]] sandbox 由 AgentWebSocketServer.start() 按条件 auto-start，runtime 不碰
