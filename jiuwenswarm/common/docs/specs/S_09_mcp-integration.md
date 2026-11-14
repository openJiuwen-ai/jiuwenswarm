# MCP 集成

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_09 / mcp-integration |
| 关联模块 | jiuwenswarm/common/{mcp_config, mcp_server_registry, mcp_call_timeout_patch, mcp_tool_list_refresh_patch}.py |
| 最近一次修订日期 | 2026-11-11 |
| 关联 feature | F_01_deepresearch-mcp-delegation.md、F_03_mcp-registration-decoupled-from-chat.md |

## 范围 / 边界

本规约管 **MCP（Model Context Protocol）的配置转换、安全准入、worker 池与上游 SDK
补丁**。MCP 配置段在 config.yaml 的位置与加载归 [[S_08]]；MCP 协议本身（E2A 投影里的
`mcp.server.*` 方法）归 [[S_02]] / [[S_03]]。

## 装配 / 分层关系

```
mcp_config.py（基础层，~2640 行）
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
  node / python / npx / uvx（python 解释器名精确匹配：`python`、`pythonw`，可带版本号及 `.exe` / `.cmd` / `.bat` 后缀）
  + 危险参数（`-e` / `-c`）拦截；SSRF 屏蔽内网 / 元数据地址
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
  finally 清理。SSE public disconnect 的等待者取消时，在 10 秒清理预算内等待完整
  停止流程；预算内的后续取消不得再次传给 owner。预算耗尽后取消目标任务，并最多
  再等待 1 秒收尾；仍未结束则报告失败、保留不可复用状态，不无限屏蔽取消。
  ToolMgr HTTP 建连/断开失败后的 owner 等待同样屏蔽调用者取消；取消后使用
  进入清理等待时的截止时间继续有界收尾，不再次取消正在清理的 owner。
  SSE 清理任务自身取消按清理失败报告，只有等待者真实取消才继续传播原取消。
  worker 初始化取消向排队调用者映射为 McpWorkerStoppedError，worker 本身继续传播
  原取消；发现阶段的内部取消仅跳过当前连接器，真实外部取消继续传播。
  call_tool/list_tools 的内部取消（等待者无 cancel 请求）在所属任务清理成功后
  最多重连重试一次；再次内部取消映射为普通工具错误。清理失败不得当作成功重试，
  调用或清理期间的真实外部取消继续传播。
  建连/调用用 `asyncio.timeout`
  保持任务归属；`wait_for` 仅可用于完整生命周期或不持有上下文的单次 I/O。
  超时/死会话先 await disconnect 再重置状态，禁止延迟跨任务 aclose；False 必须报错。
  owner 结束后若仍有未清资源，禁止换新任务复用该 client。scanner 停止等待发现清理，
  无论 scanner 是否启动都关闭全局 worker。
  `_jws_call_timeout` 印章、SDK read timeout、AbilityManager 截止桥、共享 client
  串行和死会话重试一次（at-least-once）保持；外部取消必须传播。
  包装幂等性以当前方法/factory 的显式标记为准；即使 `_PATCHED` 与注册集合被重置，
  也不得重复包装或叠加 ToolMgr owner。旧 SDK 建连吞掉新收到的取消并返回 False 时，
  补丁须恢复该取消，让外层超时及用户取消保持原语义。
  SSE 仍使用 SDK owner，在该任务内直接关闭 SDK 的 AsyncExitStack，避免旧 SDK 的
  wait_for(__aexit__) 跨任务退出；失败保留不可复用状态，不重新关闭已消费的 stack。
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
- **N11 请求级连接器加载结果对模型可见**：`list_request_mcp_server_tools` 以空
  connect_params 表示发现失败，非空 params + 零工具表示合法零工具（如 tool_filter 全关）。
  `interface_deep._append_request_mcp_server_tools` 按此区分并记录本请求未加载出工具的连接器
  （名称 → 原因），在发布前写到本请求的宿主 MCP 工具实例上；宿主的两个连接器目录工具
  （列出连接器 / 查询单个连接器的工具）结果末尾追加"本轮不可调用"说明
  （`annotate_connector_catalog_result`）。状态只挂在本请求工具实例上，不跨请求、不影响其他连接器。
  目录工具名与查询参数名以宿主 MCP 服务的工具定义为准；查询单个连接器时取不到查询名，
  按列出连接器的语义全量标注，避免参数名变化后静默失效。
- **N12 注册声明与连接配置分离**：注册项的 `runtime_params` / `scan_params` /
  `connection_scope` / `system` / `version` 5 个字段不属于连接配置，准入与工具发现只看
  去掉它们之后的部分（`_transport_config`）。除 `scan_params` 外都随配置入库、计入
  `config_fingerprint`、在 list 中回显；`scan_params` 只用于本次扫描，不入库、不计指纹、
  不回显、不进日志。声明校验失败返回 `INVALID_RUNTIME_PARAMS`：`runtime_params` 至多
  16 项，同一注册项只用一种 `via`，且 `via` 与传输类型匹配（meta / env 仅 stdio，header
  仅 sse / streamable-http）；`group_key` 至多一个且必须是必填的 meta 参数；
  `connection_scope` 只能配 header 方式；`scan_params` 的键必须是已声明的非 meta 参数。
- **N13 能力标识只报已实现**：`mcp_registry_capabilities()` 随 `mcp.server.list` 返回，
  由模块内的支持集合常量生成。未放开的 `via`、`scan_params`、`connection_scope` 在注册时
  拒绝，不允许「接受但不生效」。新增一种能力时，实现与支持集合必须同一次改动放开。
- **N14 系统条目归属**：`system: true` 的条目记录注册它的连接（`owner_id`，取
  `RequestContext.connection_id`）。属主连接在线时，其它连接对该条目的改配置与删除返回
  `SYSTEM_ENTRY_OWNER_CONFLICT`；配置相同的重复注册按指纹跳过，不算争用。WS 连接断开时
  `release_owner` 清空属主，条目与工具缓存保留，之后的连接可以接管。系统条目名不能含
  `.`。用户连接器没有属主，行为不变。
- **N15 office-claw 条目的启动身份校验**：名为 `office-claw` /
  `office-claw-single-agent` 的注册项在通用准入之后再过
  `validate_office_claw_mcp_config`（N4 的同一函数、同一份 env 白名单，只多放行
  `OFFICE_CLAW_MCP_SHARED`），失败返回 `SYSTEM_ENTRY_IDENTITY_MISMATCH`。未配置启动身份
  的部署里这两个名字无法注册（fail-closed）。`via: env` 的执行时参数同样进子进程环境，
  参数名必须在这份白名单内，否则返回 `INVALID_RUNTIME_PARAMS`。
- **N16 对话字段只在本轮生效**：`mcp_runtime_params`（`{注册名: {参数名: 字符串}}`）与
  `mcp_tool_scope`（`{注册名: [原始工具名]}`）只在请求带非空 `mcp_server_list` 时读取，
  不在列表里的注册名忽略。字段整体或 `mcp_tool_scope` 的值形状非法 → 抛
  `McpRegistryChatError`，对话请求失败。按服务的参数校验由 `resolve_runtime_params` 做：
  未声明的参数丢弃并记告警；缺必填参数、值不是非空字符串、单值超过 32768 或单服务合计
  超过 65536 → **只跳过该服务**，写入本轮不可用连接器（N11）。参数值只绑定在本轮的
  `RequestScopedOfficeClawMcpTool` 上，不进注册表、不落盘；跳过原因与日志只含参数名。
  `mcp_tool_scope` 只能收窄：未出现的服务装全部工具，空列表或全部名字不在缓存里的服务本轮
  不装工具。请求头的值含换行或 NUL 视为不合法。
- **N17 调用身份的读取口径**：调用 ID 与投递线程一律经
  `office_claw_tool_identity_value(tool, key)` 读取，先看工具绑定的执行时参数，再看连接
  参数的 `env`。保留参数名 `OFFICE_CLAW_INVOCATION_ID` / `OFFICE_CLAW_THREAD_ID` 与旧通道的
  环境变量同名。本轮注册的调用 ID 取自 `system: true` 条目的执行时参数；同一请求还带
  `office_claw_mcp` 时以注册面为准。
- **N18 office-claw 条目严格装载**：名为 `office-claw` / `office-claw-single-agent` 的注册
  条目先于其它条目占名，工具不改名、不逐个跳过；任一工具名为空、组内重复、已被本轮占用
  或遮蔽内置工具时，该条目本轮整组不装并记 error，其它条目照常。目录结果注解（N11）的
  承载工具是旧通道的 `office-claw` 与所有 `system: true` 条目的工具，不再只认条目名。

- **N19 meta 方式的分组 worker 池**：声明了 `via: meta` 的 stdio 服务，进程跨请求共享，
  本轮参数随每次 `tools/call` 的 `_meta` 下发（键为参数名，值为字符串，平铺）；worker 只
  转发，不把参数存到进程或连接上。池键为 `<服务名>#g<分组摘要>#<序号>`，分组摘要是
  `group_key` 参数值的 sha256 前 16 位，未声明 `group_key` 时整个服务一组。每组最多
  `worker_group_size` 个 worker（默认 2，env `MCP_REGISTRY_WORKER_GROUP_SIZE`）：先用空闲
  的，不满员就新建，都忙时排到 inflight 最少的那个，组内每个 worker 串行。
  `acquire_grouped` 在选中的同一段无 await 代码里替调用方占一个 inflight 名额，调用方必须
  以 `call_tool(..., reserved=True)` 消费或自行归还；占位泄漏会让 worker 永远不被空闲回收。
  条目删除、停用、改配置时沿用 N6 的 retire 语义（前缀匹配覆盖全部分组）。调用失败换掉
  出错的那个 worker 重试一次，不动同组其它 worker。
- **N20 header / env 方式按参数值区分连接**：`apply_runtime_params` 把本轮参数值并入连接
  参数的**副本**（header 并入 `auth_headers`，头名不区分大小写；env 并入 `env`；同名以执行
  时参数为准），只交给这一个 worker，不写回工具缓存。池键为 `<服务名>#v<全部参数值指纹>`，
  值相同才共用连接或进程，空闲回收沿用 N6。声明了 `connection_scope: request` 时池键为
  `<服务名>#r<请求标识与参数值的指纹>`，连接归本请求独占，
  `cleanup_request_scoped_office_claw_mcp` 在请求结束（含取消、报错、审批中断）时
  `retire_variant`。每次请求都变的凭证（如带时间戳的预签名头）应声明
  `connection_scope: request`，否则每个请求留下一条等空闲回收的连接。
- **N21 扫描参数用完即弃**：`_discover` 把 `scan_params` 并入本次扫描用的配置副本（配置里
  的 `headers` 别名先归一成 `auth_headers`），扫描返回的连接参数经 `_strip_scan_params`
  去掉这些值、同名的固定配置值放回后才入缓存。周期扫描跳过声明了必填执行时参数的远程
  服务的直连扫描：没有固定凭证可扫，不计失败（有活动连接时的处理见 N23）。
- **N22 DeepResearch 委托判定计入执行时请求头**：委托调用经本轮装载的工具对象执行，自动带
  本轮参数与工具范围。来源判定用 `deepresearch_connect_params`：声明的请求头全部计入，
  装载当轮用真实值（检查头名白名单与签名型凭证），`snapshot_for_deepresearch` 复核时用占位
  值，两处的头名集合一致，来源身份才能对上（N10）。
- **N23 复扫**：`rescan_servers(names, scan_params)`（`mcp.server.rescan`）在配置不变的前提下
  重新列工具，逐项返回 `name / ok / changed / tools_count / version`。工具有变化才把版本加
  一；失败返回 `SCAN_FAILED` 且原缓存、原版本不动；扫描期间条目被改或被删返回
  `conflict: config changed concurrently`。同一服务并发的复扫合并为一次（`_rescans` 按名字
  记在途任务，等待者用 `asyncio.shield`，一个等待者被取消不打断共用的扫描），后到的请求拿到
  同一份结果，它自带的扫描参数不生效。三种自动复扫都经活动 worker 的 `list_tools()` 执行
  （与调用同一个 owner task，不另起进程或连接），失败（含提交缓存时的异常）只记日志、
  不计扫描失败、不动缓存，排队的补扫照常执行：
  stdio 服务发来 `tools/list_changed`；worker 是换掉旧的重建出来的；周期扫描遇到必填凭证随
  请求下发的远程服务且它有活动连接。同一服务的自动复扫在途时再次触发只补扫一次，补扫用最后一次触发它的那个 worker。worker 内列工具的上限是 30s，不按调用超时占着串行队列。远程
  服务的 `tools/list_changed` 不处理，靠周期扫描收敛。扫描结果统一经 `_commit_scan_locked`
  写入缓存。
- **N24 工具对象构造收口**：本轮 MCP 工具对象一律经 `create_request_scoped_mcp_tool` 构造
  （标识 `<前缀><会话与请求的摘要>.<服务名>.<工具名>`、超时属性、执行时参数与连接模型）。
  对话装载（`_stage_request_mcp_tools`）与进程内扩展的按需装载
  （`build_registered_mcp_tools`）共用它，两条路径产出的对象逐字段相同。
  `build_registered_mcp_tools` 只读缓存、不做 IO、不安装工具；参数缺失或不合法时抛
  `McpRegistryChatError` 而不是跳过。调用方在请求结束时须调
  `retire_request_owned_mcp_workers`，否则随请求独占的连接要等空闲回收（N20）。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `mcp_config.py` | `create_mcp_tool` / `build_mcp_server_config` / `build_enabled_mcp_server_configs` / `acquire_request_scoped_mcp_session` / `RequestScopedOfficeClawMcpTool` / `list_request_mcp_server_tools` / `validate_office_claw_mcp_config` / `bind_active_office_claw_mcp_tools` / `ensure_request_scoped_mcp_tool_allowed` / `preflight_mcp_server_reachable` | N1–N4 |
| `mcp_config.py` | `RequestScopedOfficeClawMcpTool.server_name` | 只读返回原 `_server_name`；供来源匹配使用，不改变 worker key、请求绑定或工具权限 |
| `mcp_config.py` | `annotate_connector_catalog_result` / `set_unavailable_connectors`（请求级 MCP 工具实例） / `CONNECTOR_DISCOVERY_FAILED` / `CONNECTOR_NO_TOOLS` | N11；只改写两个目录工具的结果，无不可用连接器时原样返回 |
| `mcp_server_registry.py` | `McpServerRegistry` / `GlobalMcpWorkerPool` / `get_mcp_server_registry` / `start_mcp_registry_runtime` / `snapshot_for_chat` / `acquire_worker` / `redact_mcp_config` / `extract_mcp_server_list` / `McpRegistrySettings` | N5 / N6 |
| `mcp_server_registry.py` | `add_servers` / `update_servers` / `remove_servers(..., owner_id="")` / `release_owner(owner_id)` / `mcp_registry_capabilities()` / `McpRegistrationError(code, message)` / `McpServerDeclaration` | N12–N15；逐项结果在 `name / ok / error` 之外可带 `code`：`INVALID_RUNTIME_PARAMS` / `SYSTEM_ENTRY_OWNER_CONFLICT` / `SYSTEM_ENTRY_IDENTITY_MISMATCH` / `SCAN_FAILED`（扫描失败或超时） |
| `mcp_config.py` | `validate_office_claw_mcp_config(config, *, environ=None, extra_env_keys=())` | N4 / N15；`extra_env_keys` 只由注册面传入，旧通道不放宽 |
| `mcp_server_registry.py` | `extract_mcp_runtime_params(params)` / `extract_mcp_tool_scope(params)` / `resolve_runtime_params(declaration, raw) -> (values, skip_reason, dropped)` / `McpServerRegistry.chat_declarations(names)` / `is_office_claw_entry_name(name)` | N16 / N18；`chat_declarations` 同步、无 IO，返回 frozen 声明 |
| `mcp_config.py` | `RequestScopedOfficeClawMcpTool(..., runtime_params=None, catalog_host=False)` / `.catalog_host` / `office_claw_tool_identity_value(tool, key)` / `CONNECTOR_RUNTIME_PARAMS_INVALID` | N16 / N17；执行时参数此处只绑定，按 `via` 下发见后续能力（N13） |
| `mcp_server_registry.py` | `runtime_delivery(declaration, values) -> (via, group)` / `GlobalMcpWorkerPool.acquire_grouped(server_name, params, group, max_workers, *, replace=None)` / `McpServerRegistry.acquire_group_worker(server_name, group, *, replace=None)` / `McpRegistrySettings.worker_group_size` | N19；返回的 worker 已占位 |
| `mcp_config.py` | `_PooledMcpWorker.call_tool(name, arguments, *, meta=None, reserved=False)` / `RequestScopedOfficeClawMcpTool(..., runtime_via="", runtime_group="")` | N19；`meta` 只对 stdio 会话生效 |
| `mcp_server_registry.py` | `runtime_delivery(declaration, values, request_key="") -> (via, 池键后缀, 是否随请求关闭)` / `apply_runtime_params(params, via, values)` / `acquire_worker(name, *, force_rebuild=False, via="", values=None, variant="")` / `GlobalMcpWorkerPool.retire_variant(server_name, variant, *, reason="")` / `deepresearch_connect_params(params, declaration, values=None)` | N20–N22 |
| `mcp_config.py` | `RequestScopedOfficeClawMcpTool(..., request_owned_worker=False)` / `.request_owned_worker` | N20；返回随请求关闭的连接的池键后缀，否则为空 |
| `mcp_server_registry.py` | `rescan_servers(names, scan_params=None)` / `GlobalMcpWorkerPool.active_worker(server_name)` / `GlobalMcpWorkerPool.on_worker_event` | N23 |
| `mcp_config.py` | `_PooledMcpWorker.list_tools()` / `_PooledMcpWorker.on_tools_changed` | N23；回调必须同步且不阻塞 |
| `server/handlers/mcp_servers.py` | `handle_mcp_server_rescan`（`ReqMethod.MCP_SERVER_RESCAN`，HTTP `POST /mcp/servers/rescan`） | N23；`names` 须为列表，`scan_params` 须为 `{注册名: {参数名: 值}}` |
| `mcp_config.py` | `create_request_scoped_mcp_tool(request_id, session_id, server_name, tool_def, params, *, staged_name="", use_global_pool=False, strict=False, runtime_params=None, runtime_delivery=("", "", False), catalog_host=False)` | N24 |
| `mcp_server_registry.py` | `build_registered_mcp_tools(request, server_name, runtime_params=None, tool_scope=None)` / `retire_request_owned_mcp_workers(tools, *, request_id="")` | N24；供进程内扩展使用 |
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
| 请求级连接器发现失败 / 零工具，而宿主连接器目录仍显示已连接 | 目录工具结果追加本轮不可用说明（N11） | `test_failed_connector_discovery_is_reported_on_catalog_lookup` 等 |
| 注册项声明了尚未放开的能力 | 该项拒绝（`INVALID_RUNTIME_PARAMS`），不扫描、不入库 | `test_declarations_are_rejected_until_supported` |
| 另一连接改或删在线属主的系统条目 | 拒绝（`SYSTEM_ENTRY_OWNER_CONFLICT`）；属主断开后可接管 | `test_system_entry_is_guarded_while_its_owner_is_connected` |
| office-claw 条目的命令、参数、工作目录或 env 键与启动身份不符 | 拒绝（`SYSTEM_ENTRY_IDENTITY_MISMATCH`） | `test_office_claw_entries_keep_startup_identity_check` |
| 对话请求缺某服务的必填执行时参数，或参数值不合法 | 只跳过该服务，目录结果注明本轮不可调用；其它服务与对话照常（N16） | `test_missing_required_param_skips_only_that_server` |
| `mcp_runtime_params` / `mcp_tool_scope` 形状非法 | 对话请求失败（`McpRegistryChatError`） | `test_malformed_chat_fields_fail_the_request` |
| `mcp_tool_scope` 给空列表或只列不存在的工具 | 该服务本轮不装工具，目录注明未提供可用工具 | `test_tool_scope_narrows_per_server` |
| office-claw 条目的工具名遮蔽内置工具或与已占用的名字冲突 | 该条目整组不装，其它条目照常（N18） | `test_office_claw_entry_is_dropped_whole_on_name_conflict` |
| 同一分组并发调用超过 `worker_group_size` | 前几个各占一个 worker，其余排到最闲的 worker 上；不同分组不共用进程（N19） | `test_group_fills_idle_workers_then_queues_on_least_busy`、`test_stdio_server_receives_each_calls_own_meta` |
| 两个请求带不同的 header / env 参数值 | 各用各的连接或进程；值相同则共用（N20） | `test_workers_are_separated_by_runtime_values`、`test_env_values_start_separate_processes` |
| `connection_scope: request` 的服务请求结束 | 只退役本请求的连接，其它请求的连接不动 | `test_request_owned_connection_is_closed_with_the_request` |
| 注册或更新带 `scan_params` | 只用于本次扫描，缓存、list、get 中都不出现；同名固定头放回（N21） | `test_scan_params_are_used_once_and_never_cached` |
| 请求头参数值含换行 | 对话请求里跳过该服务；注册扫描参数里拒绝该项 | `test_header_values_cannot_inject_extra_headers` |
| 周期扫描遇到必填凭证随请求下发的远程服务（无活动连接） | 跳过，不计失败 | `test_periodic_scan_skips_servers_without_fixed_credentials` |
| 复扫时服务不可达 | 返回 `SCAN_FAILED`，原缓存与版本不变（N23） | `test_rescan_reports_changes_and_keeps_cache_on_failure` |
| 同一服务并发复扫 | 只扫一次，结果共用；其中一个请求被取消不影响其它 | `test_concurrent_rescans_share_one_scan` |
| stdio 服务中途增减工具并发通知 / worker 重建 | 经该 worker 复扫，版本加一，下一条对话生效 | `test_stdio_list_changed_and_worker_rebuild_trigger_rescan` |
| 周期扫描遇到必填凭证随请求下发的远程服务 | 有活动连接经它复扫；没有则跳过，不计失败 | `test_periodic_scan_uses_an_active_connection_for_credentialed_servers` |
| 审批后恢复（同一会话的新请求带新的调用 ID） | 短名映射、工具对象、注册的调用 ID 都指向恢复请求；与清理的先后次序无关 | `test_approval_resume_replays_with_the_resume_requests_params` |
| 扩展在对话中途按需装载已注册服务 | 得到的工具对象与对话装载路径逐字段相同（N24） | `test_public_builder_matches_the_chat_load_path` |
| 分组 worker 调用失败 | 只换掉出错的 worker 并重试一次 | `test_replace_and_fingerprint_change_retire_old_workers` |
| 并发请求各带各的调用 ID | 参数绑定在各自的工具对象上，互不可见（N16 / N17） | `test_concurrent_requests_keep_their_own_params` |

## 修改检查单

- 改 patch 目标（上游 SDK 升级）→ 属性名常量表（N8 G.CLS.11）同步 +
  `inspect` 确认新签名 + 两个 patch 测试文件回归 + 列出全部同构包装点逐个确认。
- 自建 remote MCP client → **必须补 `_jws_call_timeout` 印章**（N7 契约耦合）。
- 改扫描节奏 / 池 TTL → env 覆盖路径回归（N5/N6 的四个 env）。
- 扩 stdio 白名单 → 安全评审（N2 是 fail-closed 边界，[[S_01]] 铁律 6）。
- 改 OfficeClaw 启动身份 → pin 字段（N4）+ env 白名单同步；注册面多放行的键在
  `_OFFICE_CLAW_ENTRY_EXTRA_ENV_KEYS`（N15），两处一起核对。
- 新增注册字段 → 判断它属于连接配置还是注册声明；后者必须加入
  `_REGISTRATION_ONLY_KEYS`，否则会被带进准入和工具发现（N12）。
- 放开一种能力（`via` / `scan_params` / `connection_scope` / 复扫 / 工具范围）→ 实现、
  支持集合常量、`mcp_registry_capabilities` 的测试同一次改动（N13）。
- 新增读取调用身份的代码 → 走 `office_claw_tool_identity_value`，不要直接读
  `_params["env"]`：注册面条目的身份不在进程 env 里（N17）。
- 新增把参数值写进连接参数的路径 → 必须写副本；工具缓存的 `connect_params`、注册表、
  list / get、日志里都不能出现执行时参数或扫描参数的值（N20 / N21）。
- 新增写工具缓存的路径 → 走 `_commit_scan_locked`，并在写入前确认条目对象没被替换；
  新建 worker 一律经 `GlobalMcpWorkerPool._start`，否则收不到工具变更通知（N23）。
- 新增 `mcp.server.*` 方法 → `ReqMethod`、`dispatch.HANDLERS`、`agent_http_routes`、Gateway 的
  `agent_rest_map` 与 Web / TUI 转发名单、`tests/transport/test_dispatch_matrix.py` 一起改。
- 改工具对象的标识、超时属性或构造参数 → 只改 `create_request_scoped_mcp_tool`，不要在
  调用方另拼一份；进程内扩展依赖两条路径一致（N24）。
- 改分组池的选取或调用路径 → 核对每条提前返回、异常、取消路径都归还了占位（N19）；
  扫描循环的空闲回收靠 `inflight == 0` 判定。
- 改注册面装载顺序或重名处理 → 核对 office-claw 条目仍先占名、冲突时整组不装（N18）。
- 改研究快照/委派 → 核验撤权、凭据轮换、工具集合冻结、恢复隔离及原 MCP 回归；
  不得将进程内明文快照混入对外脱敏列表接口（N10）。

## 与其它 spec 的关系

- [[S_08]]：mcp 配置段加载、企业 fail-closed 语义（N9）、代理出站。
- [[S_02]]：`mcp.server.*` 方法域与 ACP 工具投影。
- [[S_10]]：`model_client_extensions` 同属"进程启动装配"，但互不依赖。
