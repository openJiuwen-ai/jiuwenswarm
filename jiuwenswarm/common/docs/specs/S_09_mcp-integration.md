# MCP 集成

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_09 / mcp-integration |
| 关联模块 | jiuwenswarm/common/{mcp_config, mcp_server_registry, mcp_call_timeout_patch, mcp_tool_list_refresh_patch}.py |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | F_01_deepresearch-mcp-delegation.md |

## 范围 / 边界

本规约管 **MCP（Model Context Protocol）的配置转换、安全准入、worker 池与上游 SDK
补丁**。MCP 配置段在 config.yaml 的位置与加载归 [[S_08]]；MCP 协议本身（E2A 投影里的
`mcp.server.*` 方法）归 [[S_02]] / [[S_03]]。

## 装配 / 分层关系

```
mcp_config.py（基础层，~2720 行）
  配置条目 → SDK McpServerConfig 转换 + 安全准入（白名单/SSRF/鉴权）
  请求级 worker 池 + OfficeClaw 工具发现/白名单绑定
        ▲ 复用 _PooledMcpWorker / create_mcp_tool / list_request_mcp_server_tools
mcp_server_registry.py（注册表层）
  chat.send 新路径 mcp_server_list 的进程内注册表：CRUD / 工具缓存 / remote 周期扫描
  全局 worker 池（RequestScopedOfficeClawMcpTool 经 use_global_pool 反向回环取用）

mcp_call_timeout_patch.py ─┐ 两个互相独立的 openjiuwen monkeypatch，
mcp_tool_list_refresh_patch.py ┘ 唯一装配点：interface_deep.DeepAdapter.__init__

契约耦合：自建 remote client 必须补打 _jws_call_timeout 印章——
漏打则 timeout patch 的进程级默认 30s 兜底会先掐断长调用。
```

## 不变量

- **N1 transport 归一**：`http` / `streamable_http` → `streamable-http`；缺省 `stdio`。
  归一发生在 `build_mcp_server_config`，下游只见归一后的值。
- **N2 安全准入三闸**（fail-closed）：企业版禁本地 stdio；stdio 命令白名单
  node / python / npx / uvx + 危险参数（`-e` / `-c`）拦截；SSRF 屏蔽内网 / 元数据地址
  （逃生 env `JIUWENSWARM_ALLOW_LOOPBACK_MCP`）。鉴权必须进 `auth_headers`（兼容
  `headers` 别名）。
- **N3 超时与缓存**：调用 / 发现超时默认 300s；schema 缓存 env
  `JIUWENSWARM_MCP_SCHEMA_CACHE`（bundle 指纹作 key）。
- **N4 OfficeClaw 身份 pin**：启动身份经 `OFFICE_CLAW_MCP_COMMAND / ARGS_JSON / CWD`
  + env key 白名单锁定；allowlist 经 ContextVar + 共享 ability_manager 双通道跨 task
  传递。
- **N5 注册表权威与缓存分工**：registry 是权威，tool_cache 是扫描产物；对话路径只读
  缓存、**不在线 list_tools**；仅 sse / streamable-http 参与扫描（超时 30s、间隔默认
  600s，env `MCP_REGISTRY_SCAN_INTERVAL_S / SCAN_CONCURRENCY / WORKER_IDLE_TTL_S /
  SCAN_FAIL_THRESHOLD` 可调）。
- **N6 worker 池判等与 retire**：`config_fingerprint`（sha256）判等，变更即 retire；
  池 key = server_name（env 含 `OFFICE_CLAW_INVOCATION_ID / CALLBACK_TOKEN` 时附 env
  指纹）。list 接口凭据一律脱敏（`redact_mcp_config`）。
- **N7 remote 生命周期归属**：connect、退出上下文、失败清理和重连必须由同一任务
  执行。发现用完整生命周期协程；池化连接复用 worker；ToolMgr 的 Streamable HTTP
  连接由补丁的专用 owner 管理，SSE 保留 SDK owner，建连取消时等待该 owner 的
  finally 清理。SSE public disconnect 的等待者取消时仍等待完整停止流程；
  等待清理期间的后续取消不得再次传给 owner，完成清理后继续传播原取消。
  建连/调用用 `asyncio.timeout`
  保持任务归属；`wait_for` 仅可用于完整生命周期或不持有上下文的单次 I/O。
  超时/死会话先 await disconnect 再重置状态，禁止延迟跨任务 aclose；False 必须报错。
  owner 结束后若仍有未清资源，禁止换新任务复用该 client。scanner 停止等待发现清理，
  无论 scanner 是否启动都关闭全局 worker。
  `_jws_call_timeout` 印章、SDK read timeout、AbilityManager 截止桥、共享 client
  串行和死会话重试一次（at-least-once）保持；外部取消必须传播。
- **N8 refresh patch 修三缺陷**：`ToolMgr._inner_refresh_mcp_tools`（先 list 后删旧、
  失败保留旧快照）、`ToolMgr.refresh_tool_server`（`None` = TTL 跳过 vs `[]` = 空刷新）、
  `ResourceMgr.refresh_mcp_server`（真实现）+ `get_mcp_tool_infos`（刷新后
  `_sync_id_to_card`）。TTL 优先级 env `MCP_TOOL_LIST_TTL_S` → config
  `mcp.tool_list_ttl_s` → 60s（0 = 每轮强刷）；上游保护属性名集中为常量（G.CLS.11）；
  `_PATCHED` 幂等。
- **N9 patch 装配点唯一**：两个 patch 只在
  `server/runtime/agent_adapter/interface_deep.py` 的 DeepAdapter 构造时装配（[[S_01]] N6）。
- **N10 DeepResearch 只读委派快照**：`snapshot_for_deepresearch(name)` 在原注册表锁内
  一次读取来源配置、已缓存工具和当前连接参数，复用调用路径的存在/启用检查；返回配置和
  工具的深拷贝，不发现工具、不启动 worker。返回值包含未脱敏连接凭据，仅供进程内授权
  检查，不得写日志、持久化或直接传给 SDK。研究专用端点调用原请求级工具，每次 list/call
  重验当前请求、来源身份、工具集合和权限；恢复仅保留首次来源与当前授权的交集。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `mcp_config.py` | `create_mcp_tool` / `build_mcp_server_config` / `build_enabled_mcp_server_configs` / `acquire_request_scoped_mcp_session` / `RequestScopedOfficeClawMcpTool` / `list_request_mcp_server_tools` / `validate_office_claw_mcp_config` / `bind_active_office_claw_mcp_tools` / `ensure_request_scoped_mcp_tool_allowed` / `preflight_mcp_server_reachable` | N1–N4 |
| `mcp_config.py` | `RequestScopedOfficeClawMcpTool.server_name` | 只读返回原 `_server_name`；供来源匹配使用，不改变 worker key、请求绑定或工具权限 |
| `mcp_server_registry.py` | `McpServerRegistry` / `GlobalMcpWorkerPool` / `get_mcp_server_registry` / `start_mcp_registry_runtime` / `snapshot_for_chat` / `acquire_worker` / `redact_mcp_config` / `extract_mcp_server_list` / `McpRegistrySettings` | N5 / N6 |
| `mcp_server_registry.py` | `snapshot_for_deepresearch(name) -> tuple[dict, list, dict]` | N10；缺失抛 `UnknownMcpServerError`，停用抛 `DisabledMcpServerError` |
| `mcp_call_timeout_patch.py` | `apply_mcp_call_timeout_patch` / `force_invalidate_mcp_client` / `is_retryable_mcp_dead_session_error` / `DEFAULT_CALL_TIMEOUT = 30.0` | N7 |
| `mcp_tool_list_refresh_patch.py` | `apply_mcp_tool_list_refresh_patch` / `refresh_registered_mcp_tool_lists` / `resolve_mcp_tool_list_ttl_s` / `DEFAULT_MCP_TOOL_LIST_TTL_S = 60` | N8 |

## 执行路径 / 消费者

- 基础层消费：`agents/swarm/assembly`、`agents/harness/common`（mcp_toolkits、
  progressive_tool_rail）、`agents/harness/team/rails/request_scoped_mcp_tools_rail`、
  `server/handlers/mcp`、`server/runtime/tenant_agent_pool`、`interface_deep`。
- 注册表消费：`server/handlers/mcp_servers`、`app_agentserver`（runtime 启停）、
  `interface_deep`。
- DeepResearch 委派：`interface_deep._get_deepresearch_mcp_context` → 专用模块
  `agents/harness/common/tools/deepresearch/mcp_search.py` → `snapshot_for_deepresearch`；
  原生 SDK 只连接本任务临时端点，端点通过 `RequestScopedOfficeClawMcpTool` 复用原 worker。
- patch 消费：`interface_deep`（唯一装配）+ `tests/unit_tests/common/test_mcp_call_timeout_patch.py`
  / `test_mcp_call_timeout_recovery.py` / `test_mcp_tool_list_refresh_patch.py`。

## 边界 case 清单

| 场景 | 现行为 | 风险 / 测试 |
|---|---|---|
| stdio 命令带 `-e` / `-c` | 白名单外拦截 | 准入测试 |
| remote URL 指向内网 / 元数据地址 | SSRF 屏蔽拒绝 | 准入测试（逃生 env 显式开） |
| 企业版配置 stdio MCP | 拒绝（N2） | 企业分支测试 |
| remote 调用挂死 | 保留超时，在 owner 清理；下次调用重连。死会话错误当轮重试一次 | `test_mcp_call_timeout_recovery.py` |
| remote 生命周期取消/退出 | 进入和退出同任务，清理失败报错且禁止换任务复用残留上下文 | `test_mcp_remote_lifecycle.py`（真实 AnyIO TaskGroup + 内存传输） |
| 共享 client 并发调用 | `_client_io_lock` 串行 | 同上 |
| 扫描期工具列表失败 | 保留旧快照（N8） | `test_mcp_tool_list_refresh_patch.py` |
| 配置变更（指纹变） | worker retire 重建 | registry 测试 |
| list 接口回显配置 | 凭据脱敏 | `redact_mcp_config` 测试 |
| 研究期间来源停用、删除或变更 | 专用端点拒绝继续委派 | `test_deepresearch_mcp_review_regressions.py` |
| 研究恢复时新增连接或更换来源 | 不扩张首次来源集合 | 同上及 `test_deepresearch_mcp_search.py` |

## 修改检查单

- 改 patch 目标（上游 SDK 升级）→ 属性名常量表（N8 G.CLS.11）同步 +
  `inspect` 确认新签名 + 两个 patch 测试文件回归 + 列出全部同构包装点逐个确认。
- 自建 remote MCP client → **必须补 `_jws_call_timeout` 印章**（N7 契约耦合）。
- 改扫描节奏 / 池 TTL → env 覆盖路径回归（N5/N6 的四个 env）。
- 扩 stdio 白名单 → 安全评审（N2 是 fail-closed 边界，[[S_01]] 铁律 6）。
- 改 OfficeClaw 启动身份 → pin 字段（N4）+ env 白名单同步。
- 改研究快照/委派 → 核验撤权、凭据轮换、工具集合冻结、恢复隔离及原 MCP 回归；
  不得将进程内明文快照混入对外脱敏列表接口（N10）。

## 与其它 spec 的关系

- [[S_08]]：mcp 配置段加载、企业 fail-closed 语义（N9）、代理出站。
- [[S_02]]：`mcp.server.*` 方法域与 ACP 工具投影。
- [[S_10]]：`model_client_extensions` 同属"进程启动装配"，但互不依赖。
