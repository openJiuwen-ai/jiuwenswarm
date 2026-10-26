---
description: jiuwenswarm common 公共基础层模块说明书——给修改/检视本模块代码的 AI 助手与工程师：模块定位、统一术语、公开入口、模块地图、架构铁律、测试与提交约束，以及 14 份 spec 的索引。
---

# common 公共基础层

Gateway 与 AgentServer 双进程共用的公共基础层：对**外协议边缘**做统一信封转换（e2a），定义**核心数据模型**（schema：AgentRequest / Message / ReqMethod），承载**敏感信息安全体系**（secrets 四层）、**传输安全**（security：mTLS / Origin 白名单）、**thinking 参数适配**、**工作区安全与配额**（workspace），以及配置与环境、MCP 集成、模型与推理参数、文件传输、审计日志、请求上下文、会话生命周期工具和自升级链路。全仓 `jiuwenswarm.server` / `jiuwenswarm.gateway` / `jiuwenswarm.agents` / `jiuwenswarm.channels` / `jiuwenswarm.extensions` 都向下依赖本包；本包**不反向依赖**它们。

本文件是模块入口索引。**修改任何代码前先读对应 spec**（见「设计文档归档与双向同步」）；深入细节跳转：

- `docs/specs/S_01_overview-and-boundaries.md` — 模块总览与包边界（分层依赖方向 / 空 `__init__` 约定 / utils.py 路径与日志中枢）
- `docs/specs/S_02_e2a-protocol.md` — E2A 统一信封协议（信封模型 / ACP·A2A 适配 / 网关归一唯一正门 / wire 三层兜底）
- `docs/specs/S_03_schema-data-models.md` — 核心数据模型（ReqMethod ~260 成员 / Mode 归一 / AgentRequest·Message / ask_user / ChatSendParams）
- `docs/specs/S_04_secrets.md` — 敏感信息安全体系（L1 门面 → L2 注册表 → L3 变换 → L4 持久化 + 算法提供方）
- `docs/specs/S_05_security-transport.md` — 传输安全（link_mtls 三态 / WS Origin 白名单 / CryptoProvider 协议 / WS 消息限额与诊断）
- `docs/specs/S_06_thinking-adaptation.md` — thinking 适配管线（冻结 profile / 厂商白名单 / 永不抛错降级 / rail 注入）
- `docs/specs/S_07_workspace-safety-and-quota.md` — 工作区安全与配额（九分区 zone / 删除四层安全链 / 配额门禁——**破坏力最高的领域**）
- `docs/specs/S_08_config-and-environment.md` — 配置与环境（config.yaml 合并缓存 / 双轨环境变量 / provider SPI / 模式归一）
- `docs/specs/S_09_mcp-integration.md` — MCP 集成（安全准入 / 请求级与全局 worker 池 / 两个上游 monkeypatch）
- `docs/specs/S_10_model-and-reasoning.md` — 模型与推理参数（外部 client 动态装载 / 模型身份指纹 / OpenRouter 归因 / reasoning 注入白名单）
- `docs/specs/S_11_file-transfer.md` — 文件分块传输（配置开关 / 共享类型 / 安全文件名）
- `docs/specs/S_12_observability-and-logging.md` — 审计与日志（audit 三件 / 日志会话绑定 / 源头脱敏 / USR1 活体快照 / SDK 兼容）
- `docs/specs/S_13_request-context-and-tools.md` — 请求上下文与工具支持（路由身份四字段 / ext 白名单透传 / chat.final 注解 / 工具归属注册）
- `docs/specs/S_14_session-lifecycle-and-upgrade.md` — 会话生命周期与自升级（todo 快照 / 后台清理 / cron 收轮状态机 / 三源自升级链路）

面向**理解系统**（而非改代码）的叙事文档在 `docs/designs/`：

- `docs/designs/common_overview.md` — 特性文档：common 的功能、原理与能力边界（概念层面，含核心优势与术语表）
- `docs/designs/architecture_cn.md` — 架构文档：架构总览图 / 分层模块结构 / 核心机制 mermaid 图 / 端到端时序 / 关键设计决策表 / 公共 API 面

## 统一术语

```
外部协议（ACP / A2A / Channel Message）
    └── E2AEnvelope（统一信封）        ← adapters / gateway_normalize 在系统边缘完成
        └── AgentRequest（内部请求）    ← agent_compat.e2a_to_agent_request
            └── AgentResponse / AgentResponseChunk（内部响应）
                └── E2AResponse（统一响应）→ 各协议投影（projection）
```

| 术语 | 含义 |
|---|---|
| **E2A / 信封（envelope）** | Everything-to-Agent 统一信封协议，版本恒 `"1.0"`；`E2AEnvelope`（请求向）与 `E2AResponse`（响应向，含 `response_kind`/`status`/`sequence`/`is_final`/`projections`） |
| **投影（projection）** | 把 `E2AResponse` 翻译回某外协议形状（ACP JSON-RPC / A2A payload / agent chunk）的过程；不适用时返回 `None` |
| **内部上下文键** | `_jiuwenswarm`（`E2A_INTERNAL_CONTEXT_KEY`）：E2A 在 wire 上携带内部上下文的保留键，出入站一律过滤 |
| **Track A / Track B** | 环境变量双轨：A = spawn 键驻留 `os.environ`；B = 业务键仅存进程内 `(sid,aid)` tip bag，永不进 `os.environ`（见 [[S_08]]） |
| **tip / overlay** | tip = 某 `(session_id, agent_id)` 命名空间的生效环境快照（active ∪ staged，staged 胜）；overlay = 任务级密封覆盖（bind 即 seal，miss 即 unset） |
| **zone（工作区分区）** | `WorkspaceZone` 九分区（SESSION_WORKSPACE / SESSION_TODO / SESSION_META / SKILLS / ROLE_MD / MEMORY / RUNTIME / ARTIFACT / OTHER）；删除许可仅 `DELETABLE_ZONES` 白名单 |
| **配额门禁（quota gate）** | `check_workspace_write` 在写入前拦截，抛 `WorkspaceQuotaExceeded`；默认关闭、最终一致（用量异步对账） |
| **worker 池** | MCP 会话的进程内复用单元：请求级（`acquire_request_scoped_mcp_session`）与全局（`GlobalMcpWorkerPool`，registry 扫描驱动） |
| **patch（上游补丁）** | 对 openjiuwen SDK 的进程启动 monkeypatch（MCP 调用超时 / 工具列表刷新），均带 `_PATCHED` 幂等标记，唯一装配点 `interface_deep` |
| **`_jws_call_timeout` 印章** | 自建 remote MCP client 必须补打的属性；timeout patch 靠它区分"外部已设超时"与"未设"（契约见 [[S_09]]） |
| **亲和（affinity）** | Ascend KV cache 亲和：开启 ⇒ release 必 false 且默认 provider 必为 `AscendAffinity`（fail-closed 不变量） |
| **canonical mode** | `agent.work.normal` 式三段模式；Web 组合（`mode`+`work_mode`）与历史完整串都归一到 8 种 canonical |
| **身份引用（model identity reference）** | `model-identity-v1:<sha256>`：跨组件指代模型配置而不外传密钥的无凭证指纹 |
| **final_mode** | `chat.final` 事件上的协议注解（`patch_segment` / `plain` 等），前端免字符串启发式折叠 |
| **ext（请求扩展）** | 经白名单从 Web query/header → `metadata['ext']` → ContextVar → Agent rail 的透传链；内部头为 Base64URL(JSON) |
| **路由身份（routing identity）** | Web 请求四字段 `user_id` / `group_id` / `bot_id` / `gateway_id`：`user_id` 只在 metadata 顶层，路由三字段只在 `metadata.routing` |
| **发卡 / HITL** | 人机交互中断；E2A 响应分派中 HITL 特判 `is_final=False` + `in_progress`（见 [[S_02]]） |
| **fail-open / fail-closed** | 本模块的核心分界：观测（审计/日志/诊断/脱敏）一律 fail-open 不弄坏请求路径；安全边界（zone 删除 / enforce mTLS / MCP 白名单 / affinity 不变量）一律 fail-closed |

## 公开入口（public API）

**`common/__init__.py` 是空文件——本包没有聚合导出。** 消费者一律走
`from jiuwenswarm.common.<module> import ...` 全路径导入；`import jiuwenswarm.common`
不带来任何符号。子包 `__init__` 也只导出极窄的门面：

| 子包 | `__init__` 导出 | 说明 |
|---|---|---|
| `schema/` | `Message` / `AgentRequest` / `AgentResponse` / `AgentResponseChunk` | 四件核心模型是唯一聚合出口；`ReqMethod`/`EventType`/`Mode`/`ChatSendParams` 等走子模块导入 |
| `secrets/` | `SecretStore` | 唯一门面；注册表/变换/持久化细节不外露 |
| `e2a/` `security/` `thinking/` `workspace/` | 无门面导出 | 全部按子模块导入（如 `jiuwenswarm.common.e2a.models`） |

**两个进程级单例**（get_instance 风格）：`SecretStore.get_instance()`（[[S_04]]）、
`get_mcp_server_registry()`（[[S_09]]）。**两个进程级 SPI**：`config_provider` /
`path_provider`（register / get / reset，见 [[S_08]]）。

**新增符号的规则**：先做子模块；只有当符号成为跨层稳定契约（如 `AgentRequest`）且消费方
超过三个目录时，才考虑进子包 `__init__` 的门面导出——并同步更新 [[S_01]]。**不要**在
`common/__init__.py` 里加导出：顶层聚合会让"import 即全量初始化"的副作用面失控。

## 模块地图

```
common/
├── __init__.py                     # 空：无聚合导出（约定见 S_01）
│
├── e2a/                            # E2A 统一信封协议（零 openjiuwen 依赖）
│   ├── constants.py                # 词法表根：25 常量（协议版本状态/kind/ACP 方法表/内部键 frozenset）
│   ├── models.py                   # E2AEnvelope/E2AResponse/E2AProvenance/IdentityOrigin + from_dict 宽容迁移
│   ├── adapters.py                 # ACP/A2A ↔ E2A 双向适配（边缘转换，惰性 import Message）
│   ├── gateway_normalize.py        # 系统边缘唯一正门：message_to_e2a_or_fallback（失败也产合法 E2A）
│   ├── wire_codec.py               # 线编解码三层兜底：E2A → legacy blob → 旧形状；encode 永不抛错
│   ├── agent_compat.py             # e2a_to_agent_request（fallback 信封硬拒绝 RuntimeError）
│   └── acp/                        # ACP 协议投影
│       ├── protocol.py             # initialize/session/prompt result 构造（能力自述保守）
│       ├── acp_tool_updates.py     # 工具名 9 别名归一 / 描述符 / tool-call·result 更新
│       └── session_updates.py      # 会话更新主分派器（增量去重/usage/messageId 稳定）
│
├── schema/                         # 核心数据模型
│   ├── message.py                  # ReqMethod(~260)/EventType(43)/Mode 归一/Message dataclass
│   ├── agent.py                    # PermissionContext/AgentRequest/AgentResponse/AgentResponseChunk
│   ├── ask_user.py                 # AskUserAnswer/Response(frozen)/JSON Schema/双重编码解码
│   ├── chat_send.py                # ChatSendParams TypedDict（chat.send 入参契约）
│   ├── event_base.py               # HookEventBase（__init_subclass__ 自动加 scope 前缀）
│   └── swarmflow_reply.py          # SwarmflowReplyParams TypedDict（纯契约）
│
├── secrets/                        # 敏感信息安全体系（四层）
│   ├── store.py                    # L1 门面：SecretStore（get/set 明文进出）+ 单例 + 装配链
│   ├── registry.py                 # L2 路由：secret_registry.yaml（bundled+用户覆盖）+ 路径解析
│   ├── transform.py                # L3 变换：encode/decode 决策树（信封/legacy/明文）
│   ├── envelope.py                 # 信封格式 ENC:v1:<algorithm>:<wrap_b64>:<payload_b64>
│   ├── legacy.py                   # legacy 敏感名判定（委托 local_env_config）
│   ├── providers/__init__.py       # 算法提供方：AES-256-GCM（主密钥）/ X25519 DEK 双层
│   └── persistence/                # L4 介质适配：env/file/default_file/db(stub) + gateway 分发
│
├── security/                       # 传输安全
│   ├── base_crypto.py              # CryptoProvider Protocol（encrypt/decrypt 字符串进出）
│   ├── link_mtls.py                # LinkMTLSConfig：off/observe/enforce 三态（enforce 启动预检 fail-closed）
│   └── ws_origin.py                # WS Origin 白名单（严格 =="1" 才启用/企业版短路/无 Origin 哨兵 "none"）
│
├── thinking/                       # thinking 参数适配管线
│   ├── types.py                    # ThinkingProfile(frozen)/normalize_thinking/kwargs_digest
│   ├── vendor_map.py               # 厂商白名单：GLM-5.x 窄匹配 + DeepSeek → extra_body 风格
│   ├── adapter.py                  # adapt_thinking 永不抛错（三种降级 reason）
│   ├── rail.py                     # ThinkingInjectRail(priority=15) before_model_call 注入
│   └── register_hook.py            # 幂等注册进 openjiuwen TaskTool（ImportError 降级）
│
├── workspace/                      # 工作区安全与配额
│   ├── zones.py                    # WorkspaceZone 九枚举 + DELETABLE_ZONES + 12 前缀规则 + 词法校验
│   ├── service.py                  # WorkspaceService：删除四层安全链/preview/download 配额
│   └── quota.py                    # 配额门禁（默认关/最终一致/-1 无限/soft 80 hard 100）
│
├── config.py                       # config.yaml 读/合并/${VAR} 解析/带锁写回 + 各配置段 API（~2500 行）
├── config_provider.py              # 配置源 SPI：ConfigProvider 协议 + 企业注入（fail-open 回退默认）
├── path_provider.py                # 路径解析 SPI：PathCategory ~20 类目录落点（进程级单例）
├── local_env_config.py             # 双轨环境变量：Track A/B + tip/overlay + 加解密导出（~1340 行）
├── hooks_config.py                 # hooks 段 schema：17 HookEvent × matcher（正则/OR/*/总开关）
├── http_proxy_config.py            # 出站代理/SSL 解析 + trust_env=False 的 requests 封装
├── kv_cache_affinity_config.py     # Ascend KV cache 亲和纯规则（不变量校验 fail-closed）
├── mode_matrix.py                  # 模式归一：组合表 → 新三段直解 → legacy 回调（8 种 canonical）
├── work_mode.py                    # work/code 工作模式共享基础（正交于 mode；虚拟默认项目 ID）
├── context_keys.py                 # 共享键常量（JIUWENSWARM_CHANNEL_CONTEXT_KEY）
│
├── mcp_config.py                   # MCP 配置→SDK 转换 + 安全准入 + 请求级 worker 池（~2720 行）
├── mcp_server_registry.py          # 进程内 MCP 注册表 + 全局 worker 池 + remote 周期扫描
├── mcp_call_timeout_patch.py       # 上游补丁：调用超时 + 死会话作废重连（wait_for 不变量）
├── mcp_tool_list_refresh_patch.py  # 上游补丁：工具列表中途刷新（修 ToolMgr/ResourceMgr 三缺陷）
│
├── model_client_extensions.py      # 外部模型 client 动态装载（env 白名单 + EXTENSION_DIRS）
├── model_config_validation.py      # 占位符 api_base 识别（example.com 黑名单）
├── model_identity.py               # 模型身份引用/凭证指纹（sha256，无凭证跨组件指代）
├── openrouter_attribution.py       # OpenRouter 归因头注入（setdefault 不覆盖用户值）
├── reasoning_config.py             # reasoning 判定纯函数（provider/host/模型三层白名单）
├── reasoning_injector.py           # reasoning_level → 厂商参数（deepseek extra_body/百炼布尔）
│
├── file_transfer_config.py         # 分块传输配置（默认关；chunk 64KB/max 100MB/超时 300s）
├── file_transfer_types.py          # 传输共享类型 + safe_filename 白名单 + mime 猜测
│
├── audit_emit.py                   # 审计打点统一入口（telemetry 优先，foundation 回退，fail-open）
├── audit_gate.py                   # 审计全局布尔闸门（默认 False；Gateway 库配置切换）
├── audit_net.py                    # 审计 SRCIP/DSTIP 采集（POD_IP 优先，失败占位 "-"）
├── log_context.py                  # 请求级 session id ContextVar（日志过滤器消费）
├── log_preview.py                  # 单行日志用户内容有界预览（200 字符 + 可关脱敏）
├── openjiuwen_logging.py           # SDK 文件日志引导（钉住 <root>/agent/.logs/openjiuwen）
├── debug_dump.py                   # kill -USR1 活体快照（全线程栈 + pending 协程）
├── openjiuwen_rail_compat.py       # SDK 版本兼容：构造器缺 kwargs 按签名静默丢弃
├── stage_timer.py                  # 热路径分段计时（单行 name=ms 渲染）
├── ws_diagnostics.py               # WS 异常诊断字段（鸭子探测，不依赖具体 ws 库）
├── ws_limits.py                    # WS 消息限额常量（内部链 8MiB/浏览器链 100MiB）
│
├── request_ext.py                  # 请求扩展白名单透传链（query/header→metadata→ContextVar→rail）
├── request_identity.py             # 路由身份四字段归一化 + metadata 落点（写一次读多处）
├── chat_final.py                   # chat.final 的 final_mode 注解 + reasoning-only 空回复兜底
├── tool_display.py                 # 工具中文展示名（call_goal 优先，规则兜底；绝不碰 display_name）
├── tool_ownership.py               # 工具实例归属注册（agent 独占 vs 共享，id 防 并发覆盖）
├── coding_memory_paths.py          # Coding Memory 目录解析 + legacy Markdown 一次性迁移
│
├── todo_generation.py              # todo 代际 token 过滤（无 token 旧条目一律保留）
├── todo_snapshot.py                # 会话 todo 快照 → 前端恢复格式（session_id 白名单正则）
├── cleanup.py                      # 后台周期清理（30 天过期会话目录 + 孤儿 file_ops）
├── cron_team_completion.py         # cron 团队收轮状态机（leader 非 placeholder final 才算完成）
├── git_safe_directory.py           # git dubious ownership 检测与提示（不代执行配置）
│
├── updater.py                      # 自升级编排（安装模式二分：desktop 下载 / pip 安装）
├── updater_restart_helper.py       # 独立重启守护（python -m 运行，等父进程退出再重启）
├── upgrade_executor.py             # 升级执行器抽象（Desktop/Pip 两实现 + restart 标记）
├── version.py                      # __version__ = "0.2.3"（全仓唯一版本真源）
├── version_source.py               # 三源最新版本获取（GitHub/GitCode/PyPI + pre-release 总序）
│
├── utils.py                        # 路径管理中枢 + 日志装配 + 杂项工具（~4040 行；import 即 setup_logger）
└── docs/                           # 设计文档归档（specs/ 14 份 + designs/ 2 份 + 归档规约 AGENTS.md）
```

## 架构铁律

以下 12 条从各 spec 不变量中提炼（全量不变量以 spec 为准，条目可被 review 单条引用）：

1. **依赖方向单向**（[[S_01]] N1）。common 不 import `jiuwenswarm.gateway` / `server` /
   `agents` / `channels` / `extensions` 任何模块；跨进程共享逻辑下沉到 common，而不是让
   common 反向感知消费者。e2a 子包更进一步：零 openjiuwen 依赖（纯标准库 + pydantic），
   保证信封模型可被任意边缘独立复用。
2. **空 `__init__` 约定**（[[S_01]] N2）。`common/__init__.py` 保持为空；子包 `__init__`
   只做窄门面导出（schema 四件、secrets 一件）。新增公开符号默认走子模块全路径导入；
   禁止在顶层 `__init__` 聚合——那会让"import 即全量初始化"的副作用面失控。
3. **边缘转换唯一正门 + wire 永不抛错**（[[S_02]]）。外协议进系统只有
   `gateway_normalize.message_to_e2a_or_fallback` 一条路（失败也产出合法 E2A 形状，从不
   把裸异常抛出边界）；`wire_codec.encode_agent_response_for_wire` 永不抛错；解码三层
   兜底顺序固定：E2A → metadata legacy blob → 旧形状直读，不得增删乱序。
4. **敏感信息只经 SecretStore 明文进出**（[[S_04]]）。明文只存在于内存与 get/set 边界；
   落盘前必经 transform 决策树（显式 algorithm → 信封 / legacy 敏感 + custom crypto →
   Provider / 其余明文）。新增敏感配置项必须同步 `secret_registry.yaml`（bundled 约 60
   条 + 用户覆盖同 key 优先）。secrets 的初始化 API（configure_aes256gcm 等）当前
   **未接线**——勿假设默认装配链已启用自定义加密。
5. **工作区删除 fail-closed 四层链**（[[S_07]] N1-N4）。zone 白名单
   （仅 SESSION_WORKSPACE / SESSION_TODO / ARTIFACT）→ 词法校验（绝对路径/穿越/NUL 拒绝）
   → symlink 只 unlink → resolve 后二次校验 + 深度 32 / 条目 5000 预算。任何一层不过即
   拒；**扩删除面 = 改 DELETABLE_ZONES，必须先过 spec 修订**。
6. **观测一律 fail-open，安全边界一律 fail-closed**（[[S_12]] / [[S_05]] / [[S_07]]）。
   审计打点（audit_emit）、日志预览（log_preview）、诊断（ws_diagnostics/debug_dump）、
   脱敏装配的任何失败只 warning，不得弄坏请求路径；反之 enforce 态 mTLS 预检失败、zone
   删除越权、MCP stdio 命令白名单外、affinity 不变量冲突，直接抛错拒绝——**不要把安全
   分支改成告警降级**。
7. **thinking / reasoning 注入永不抛错、白名单窄匹配**（[[S_06]] / [[S_10]]）。
   `adapt_thinking` 对非法值/不支持模型/适配器异常三种情况统一降级（reason 可追溯）；
   厂商匹配是**有序窄白名单**（GLM-5/5.1/5.2 精确、DeepSeek 关键字），新增厂商 = 扩表 +
   回归测试，禁止前缀宽匹配。
8. **上游 monkeypatch 唯一装配点 + 幂等**（[[S_09]]）。两个 MCP patch 只在
   `server/runtime/agent_adapter/interface_deep.py` 装配；均带 `_PATCHED` 标记防重复；
   patch 目标属性名集中为常量（上游改名一处可改）。自建 remote MCP client 必须补打
   `_jws_call_timeout` 印章——漏打的后果是超时 patch 的内层 300s 兜底先掐断长调用。
9. **配置缓存共享引用禁改 + 联动失效**（[[S_08]]）。`get_config()` 返回的 dict 是缓存
   共享引用，消费方禁止原地修改；`local_env_config` 的 tip 写入必须联动失效 config 的
   ns 缓存（已内置，绕过 `set_os_environ` 直写 os.environ 会破坏该联动）。
10. **Track B 业务键永不驻留 os.environ**（[[S_08]] N5）。`export_agent_environ` 是子进程
    环境的唯一合法出口（含加解密）；overlay 绑定即 seal（miss 即 unset，不回退 tip）。
    `JIUWENCLAW_*` ↔ `JIUWENSWARM_*` 别名互换只在出口处做。
11. **模式与身份归一的原样铁律**（[[S_08]] / [[S_13]]）。`deprecate_mode` 对 None / 空串
    原样返回（禁降级为默认值）；路由身份四字段只写一次（Gateway 入口）、读多处，
    `user_id` 只在 metadata 顶层、路由三字段只在 `metadata.routing`，不摊平不进业务
    params；`tool_display` 绝不触碰 `display_name`（team 工具业务字段，撞名会误剥参数）。
12. **版本唯一真源**（[[S_14]]）。`version.__version__` 是全仓唯一版本号；UA / 握手 /
    升级判定 / ACP initialize 全部引用它，禁止在任何消费点硬编码版本串。pre-release
    比较必须走 `version_source.release_sort_key` 总序（基版本 → stable>pre →
    dev<alpha<beta<rc<pre → 序号），不得用 `packaging.version` 之外的自造比较。

## 生命周期 / 配置消费

- **进程启动序**（谁在哪儿装配）：`app_gateway` / `app_agentserver` 启动时安装
  `debug_dump`、`openjiuwen_logging.bootstrap_openjiuwen_logging`、`load_extra_model_clients`、
  `start_background_cleanup`；agentserver 另注册 `thinking_hook`、启停 MCP registry
  runtime；两个 MCP patch 在 `interface_deep` 的 DeepAdapter 构造时装配。
- **请求级**：E2A 信封在 Gateway 边缘生成，`AgentRequest` 贯穿全程；路由身份、ext、
  quota identity、tip overlay 均为请求/任务级 ContextVar，绑定必须成对（bind/reset）。
- **跨请求**：config 解析缓存按 `(path, mtime_ns)`、agent 级配置按 `(sid,aid)` + overlay
  hash 缓存；MCP 全局 worker 池按 `config_fingerprint` 判等 retire；HITL/E2A sequence
  随会话推进单调。
- **环境变量重定位**：`JIUWENSWARM_CONFIG_DIR`（配置目录）、`JIUWENSWARM_DATA_DIR`
  （数据根，默认 `~/.jiuwenswarm`）——测试里改这两个 + `clear_config_cache` 是隔离标配。

## 测试

pytest.ini 的 addopts 含全仓 coverage，日常跑请清掉（`-o addopts=""`）：

```powershell
# 模块主测试目录（31 个文件：config/secrets/workspace/mcp/thinking/audit/...）
.venv\Scripts\python.exe -m pytest tests/unit_tests/common/ -o addopts=""

# E2A 协议（独立目录）
.venv\Scripts\python.exe -m pytest tests/unit_tests/e2a/ -o addopts=""

# 自升级链路（updater/upgrade_executor/version_source）
.venv\Scripts\python.exe -m pytest tests/unit_tests/test_updater_install_mode.py -o addopts=""

# todo 快照（历史路径在 server/ 下——迁移遗留，勿再新增）
.venv\Scripts\python.exe -m pytest tests/unit_tests/server/test_todo_snapshot.py tests/unit_tests/server/test_history_todo_restore.py -o addopts=""
```

- 单测路径镜像源码：`common/<module>.py` → `tests/unit_tests/common/test_<module>.py`；
  e2a 例外（`tests/unit_tests/e2a/`），新增 e2a 测试进该目录。
- **改 workspace 删除链 / quota 门禁时，除单测外必须对照 [[S_07]] 修改检查单跑全场景
  矩阵**：合法 zone 删除 / 越权 zone 拒绝 / symlink / 穿越 / 配额边界（soft 80 / hard
  100 / -1 / 0）/ 多租户隔离。
- 修复类改动必须附**能复现原故障的测试**（先红后绿）；修 MCP patch 类问题必须列出全部
  同构装配点逐个确认（[[S_09]] 修改检查单）。

## 代码风格

- 类型注解：PEP 585 内置泛型（`list[X]`、`dict[K, V]`）+ PEP 604 联合类型（`X | None`）；
  禁止从 `typing` 导入已被内置语法替代的别名。
- 注释 / 日志 / docstring 中文，代码符号英文；关键机制处注释写"为什么"而非"是什么"
  （mcp_config / local_env_config / quota 内大量此类注释是契约的一部分，清理时勿顺手删除）。
- 日志禁止 `print`；用户内容进日志一律过 `log_preview.preview_text`（脱敏 + 截断）。

## 设计文档归档与双向同步（本模块强制）

本模块设计文档**全部**落在 `docs/` 下，命名与结构规约见 [`docs/AGENTS.md`](docs/AGENTS.md)。
把 **`common/` 代码 + `docs/specs/` + 本 AGENTS.md** 当作一个一致性单元维护——本模块是
双进程共用的地基（协议信封 / 敏感信息 / 工作区删除 / 配置缓存），"只改一半消费方"或
"绕过门面直捣内部"是缺陷的头号来源。

**前置铁律——文档先是设计的输入，才是提交的产出。** 动手改任何代码**之前**，先读相关
`docs/specs/S_NN_*`（现有契约 / 不变量 / 修改检查单），拿它们校准方案；方案定了**当场**
同步修订对应 spec。先码后档、拖到提交前才补文档是把流程做反了。

三条强制约束，提交时必查：

1. **特性更新归档 feature 且三连续提交**：`docs/features/` 新增 `F_NN_<slug>.md`；特性代码
   （`feat(common)`）、测试（`test(common)`）、文档（`docs(common)`）拆成三个紧邻提交、
   同一 PR 落地。bugfix 触及 spec 描述的路径/不变量时同样三连（`fix`/`test`/`docs`）；
   纯缺陷修复归档 `docs/bugfix/B_NN_<slug>.md` 时合并为**一个** `fix(common)` 提交。
2. **规约变动必更新 specs**：契约 / 执行路径 / 不变量 / 信封格式 / 注册表结构 / 配额语义
   变化时，同步修订对应 `S_NN`（元信息"最近一次修订日期"填当天）；新规约主题 = 新 spec 文件。
3. **读到不一致当场修文档**：本文件 / specs 里与代码不符的接口名、枚举值、常量数、路径，
   先 grep 代码、以代码为准刷新文档，同一次改动落地；不把过时表述当新约束执行。点名
   数量的句子（"ReqMethod 约 260 成员"、"17 个 HookEvent"、"三层兜底"）都是契约的一部分。

## 提交约定

本模块改动的 commit message scope 固定用 `common`（如 `feat(common): ...`）。

footer 用 `Refs: #<issue>` 格式关联 issue。issue 号若无法从当前上下文明确，必须先询问用户，不要臆造或留空。

涉及 `docs/features/F_*` / `docs/specs/S_*` 文档更新的特性改动，**特性代码、测试代码、文档
拆成三个连续提交**（提交 1 特性代码 `feat(common)`，提交 2 单测 `test(common)`，提交 3 文档
`docs(common)`）；归档 `docs/bugfix/B_*` 的缺陷修复例外，代码、测试、文档合并为一个
`fix(common)` 提交。细则见上文「设计文档归档与双向同步」与 [`docs/AGENTS.md`](docs/AGENTS.md)。
