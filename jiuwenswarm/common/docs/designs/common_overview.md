# common 公共基础层特性文档

## 概述

`jiuwenswarm.common` 是 Gateway 与 AgentServer 双进程共用的公共基础层，核心目标是让
jiuwenswarm 的两个核心进程——面向连接与路由的 **Gateway**、面向 Agent 执行的
**AgentServer**——在协议、数据、配置、安全与运维五个维度上共享同一套实现，而不是各自
维护一份平行代码。

与业务模块不同，common 本身不实现任何端到端功能；它是**被两层进程同时向下依赖的地基**：
全仓 `server` / `gateway` / `agents` / `channels` / `extensions` 五个层级都 import 它，而它
不反向依赖其中任何一个。这个单向依赖关系是整个 jiuwenswarm 工程结构的锚点。

本文从概念层面介绍 common 的功能、原理、特性与优势，帮助读者理解它管什么、为什么这样
设计、能力边界在哪里。修改代码前必读的规约性内容（不变量 / 修改检查单）在
[`docs/specs/`](../AGENTS.md) 与模块 [AGENTS.md](../../AGENTS.md)，本文不重复。

---

## 核心概念

### 分层结构：六个子包 + 顶层工具集

common 内部按"自成体系的领域"划分为六个子包，其余约 50 个顶层文件按主题归组：

| 单元 | 领域 | 一句话定位 |
|------|------|-----------|
| `e2a/` | 协议 | Everything-to-Agent 统一信封：外协议 ↔ 内部模型的翻译层 |
| `schema/` | 数据 | AgentRequest / Message 等贯穿全仓的核心数据模型 |
| `secrets/` | 安全（静态） | 敏感配置项的注册、加解密与多介质存储 |
| `security/` | 安全（链路） | 服务间 mTLS、WS Origin 白名单、加密提供方协议 |
| `thinking/` | 模型参数 | thinking 开关的厂商适配管线 |
| `workspace/` | 文件 | 工作区分区、删除安全链、存储配额 |
| 顶层 ~50 文件 | 横切 | 配置与环境、MCP 集成、模型与推理、文件传输、审计日志、请求上下文、会话生命周期、自升级 |

### 双进程数据流中的位置

common 的每个子域都同时服务两个进程：

```
外部协议客户端（ACP / A2A / Web / TUI / Channel）
        │
        ▼
┌────────────────────┐   Agent WS / HTTP   ┌────────────────────┐
│      Gateway       │ ──────────────────► │    AgentServer     │
│  （连接 / 路由）    │ ◄────────────────── │  （Agent 执行）     │
└────────────────────┘      E2A wire       └────────────────────┘
        │                                       │
        └───────────────┬───────────────────────┘
                        ▼
              jiuwenswarm.common（本层）
   e2a · schema · secrets · security · thinking
   workspace · config · mcp · model · audit · ...
```

- **入站**：Gateway 在系统边缘把外协议消息归一为 E2A 信封，再转成 AgentRequest 下行；
- **出站**：AgentServer 把 AgentResponse / 流式 chunk 归一为 E2AResponse，投影回各协议；
- **中间**：配置、环境变量、工作区、配额、审计、日志在两侧使用同一套实现。

### 依赖方向与空 `__init__` 约定

common 有两条结构性纪律：

1. **依赖方向单向**——common 永不 import gateway / server / agents / channels /
   extensions；跨进程共享逻辑必须下沉到 common，而不是让 common 反向感知消费者。
   其中 `e2a/` 更进一步做到**零 openjiuwen SDK 依赖**（纯标准库 + pydantic），保证信封
   模型可被任意边缘（含独立部署的协议网关）复用。
2. **无聚合导出**——`common/__init__.py` 是空文件；子包 `__init__` 只做极窄的门面导出
   （schema 四件核心模型、secrets 的 SecretStore）。消费一律走
   `jiuwenswarm.common.<module>` 全路径导入。这避免了"import 一个符号却触发全量初始化
   副作用"的失控场面。

### fail-open 与 fail-closed 的分界

common 内所有子域的容错策略都遵守同一条分界线：

- **观测类一律 fail-open**：审计打点、日志预览、诊断、脱敏装配的任何失败只记
  warning，绝不弄坏请求路径——丢一条日志是可接受的损失，挂掉一个用户请求是事故。
- **安全边界一律 fail-closed**：工作区越权删除、enforce 态 mTLS 预检失败、MCP stdio
  命令白名单外、KV cache 亲和不变量冲突，直接抛错拒绝——宁可拒绝服务也不越过边界。

---

## 协议边缘：E2A 统一信封

jiuwenswarm 对外同时说多种协议（ACP JSON-RPC、A2A、Web/TUI Channel Message）。E2A
（Everything-to-Agent）是这些协议的**统一中间表示**：

```
ACP JSON-RPC ─┐                                  ┌─ ACP 响应投影
A2A ──────────┼─► E2AEnvelope ─► AgentRequest ─► │
Channel Msg ──┘   （统一信封）    （内部请求）      ├─ A2A 流 payload
                                                    └─ Agent chunk
```

三个关键设计：

- **边缘归一唯一正门**：外协议消息进系统只有 `message_to_e2a_or_fallback` 一条路，即使
  归一失败也产出"合法的 fallback 信封"（可识别、可拒绝），从不把裸异常抛出系统边界。
- **wire 三层兜底**：双进程之间的线上解码按"E2A → legacy blob → 旧形状直读"三层回退，
  让 Gateway 与 AgentServer **版本错位部署**时仍能互通；出站编码永不抛错。
- **响应投影**：`E2AResponse` 携带 `response_kind`（14 种）/ `status` / `sequence` /
  `is_final`，一次性表达状态、流序与终态，再由各协议适配器投影为具体形状。人机交互
  中断（HITL）被特判为 `is_final=False`——它不是终态，是等待。

契约细节见 [S_02_e2a-protocol.md](../specs/S_02_e2a-protocol.md)。

---

## 核心数据模型：AgentRequest 与 Message

`schema/` 定义贯穿全仓的四件核心模型（`Message` / `AgentRequest` / `AgentResponse` /
`AgentResponseChunk`）与两个巨型枚举：

- **ReqMethod（约 260 成员）**：系统全部方法域的唯一枚举——`chat.*`、`command.*`、
  `session.*`、`team.*`、`workspace.*`、`updater.*` 等 14 个域。它是方法路由的词法根，
  禁止用裸字符串当 method 传递。
- **EventType（43 个）**：事件名的唯一来源。
- **Mode 归一**：外部传入的 `agent` / `agent.plan` / `agent.fast` 等历史模式归一为
  canonical 三段值（如 `agent.work.normal`、`code.team`），配合 work_mode（work 办公 /
  code 工程）正交组合成 8 种运行形态。

会话身份的派生公式固定：`session_id = sha256(thread_id)`、`workspace_key = MD5 hex`——
这保证了同一外部线程在两个进程中定位到同一份会话数据。

契约细节见 [S_03_schema-data-models.md](../specs/S_03_schema-data-models.md)。

---

## 敏感信息安全：secrets 四层体系

API key、token、密码等敏感配置项的存储与加解密分为四层：

| 层 | 模块 | 职责 |
|----|------|------|
| L1 | `SecretStore` | 门面：get / delete / set 明文进出，进程级单例 |
| L2 | `SecretRegistry` | 路由：按 `secret_registry.yaml`（bundled 约 60 条 + 用户同 key 覆盖）决定每个敏感项存哪、怎么存 |
| L3 | `transform` | 变换：encode 决策树（显式算法 → 加密信封 / legacy 敏感名 + 自定义 crypto → Provider / 其余明文） |
| L4 | `persistence/*` | 介质适配：.env / yaml / json / 默认 secrets_store.json / DB（预留） |

加密值统一走信封格式 `ENC:v1:<algorithm>:<wrap_b64>:<payload_b64>`；算法提供方内置
AES-256-GCM（32 字节主密钥）与 X25519 DEK 双层。

一个值得注意的设计权衡：**解密链全部 fail-open 返回原文**——用户手填的明文密钥是合法
输入，解不开就当明文用；但 encode 侧决策树严格分级。当前自定义加密的初始化 API 尚未
接线（默认装配链实际是明文直通），这被如实记录在 spec 而非隐藏。

契约细节见 [S_04_secrets.md](../specs/S_04_secrets.md)。

---

## 传输安全：mTLS 三态与 Origin 白名单

- **link_mtls 三态**：`off`（默认零影响）→ `observe`（预检失败仅告警，用于灰度观察）→
  `enforce`（启动预检失败直接拒绝启动，全链 fail-closed）。三态设计让 mTLS 可以先
  observe 再 enforce 地渐进部署。
- **WS Origin 白名单**：默认关闭；开启后无 Origin 的请求需要白名单显式含哨兵 `"none"`
  才放行——"缺省放行"是恶意客户端最容易钻的洞。
- **WS 消息限额双轨**：内部链（Gateway ↔ AgentServer）8 MiB，浏览器链 100 MiB——两条
  链路的载荷形态完全不同（流式事件 vs base64 文档），刻意不统一。

契约细节见 [S_05_security-transport.md](../specs/S_05_security-transport.md)。

---

## 模型参数适配：thinking 与 reasoning 双管线

common 内有两条**相互独立**的模型参数适配管线，职责分界清晰：

| 管线 | 输入 | 输出 | 特点 |
|------|------|------|------|
| `thinking/`（开关） | `""` / `default` / `off` / `on` | 厂商风格 kwargs（GLM-5.x、DeepSeek） | 永不抛错，非法值降级 default |
| `reasoning_*`（力度） | `off` / `low` / `medium` / `high` | deepseek `extra_body.thinking` / 百炼 `enable_thinking` | 三层白名单（provider → host → 模型）全过才注入 |

两者共享同一条哲学：**厂商白名单必须窄匹配**（GLM-5/5.1/5.2 精确版本号），因为前缀宽
匹配曾让 GLM-4 误吃 GLM-5 的参数；注入失败一律降级而非报错——thinking 是增强项，失败
不得影响主调用。

契约细节见 [S_06_thinking-adaptation.md](../specs/S_06_thinking-adaptation.md) 与
[S_10_model-and-reasoning.md](../specs/S_10_model-and-reasoning.md)。

---

## 工作区安全与配额：破坏力最高的领域

Agent 会在用户的工作区里读写删文件，common 的 `workspace/` 子包是这把"删除权"的缰绳：

- **九分区 zone 模型**：会话工作区、todo、元数据、技能、角色文档、记忆、运行时、产物、
  其它——每个路径归属唯一分区，由 12 条最长前缀规则判定。
- **删除许可是白名单**：九个分区里只有三个（SESSION_WORKSPACE / SESSION_TODO / ARTIFACT）
  允许删除。角色文档、记忆、技能永不可删——Agent 可以重写自己的工作产出，但抹不掉自己
  的"人格"。
- **四层安全链**：zone 白名单 → 词法校验（拒绝绝对路径 / 穿越 / NUL）→ symlink 只摘链接
  不递归 → resolve 后二次校验 + 深度 / 条目预算。任何一层不过即拒。
- **配额门禁**：默认关闭、最终一致（用量异步对账），但写入拦截本身同步生效；
  `-1` 无限 / `0` 零配额 / 缺失策略 fallback 10 TiB；soft 80% 观测、hard 100% 拦截。

契约细节见 [S_07_workspace-safety-and-quota.md](../specs/S_07_workspace-safety-and-quota.md)。

---

## 配置与环境：双轨环境变量

common 的配置体系回答一个难题：**同一台机器上多个 agent 并存时，每个 agent 有自己的
API key 与环境配置，怎么办？**

答案是双轨制：

- **Track A（spawn 键）**：进程启动必需的键，驻留 `os.environ`。
- **Track B（业务键）**：按 `(session_id, agent_id)` 命名空间存放的进程内 tip bag，
  **永不进入 `os.environ`**。子进程环境的唯一合法出口是 `export_agent_environ`（出口处
  做加解密与 `JIUWENCLAW_*` ↔ `JIUWENSWARM_*` 别名互换）。

在此之上叠加任务级 **overlay**：绑定即密封（seal）——overlay 里没有的键视为未设置，
不回退 tip，防止任务 A 的配置渗透进任务 B。config.yaml 解析（`${VAR}` 替换）从双轨取值，
解析结果按 `(path, mtime)` 与 `(sid, aid, overlay hash)` 双重缓存，tip 写入联动失效缓存。

模式归一则由 mode_matrix 承担：Web 组合（`mode` + `work_mode`）→ 新三段直解 → legacy
回调三级解析，旧模式串经 DEPRECATION_MAP 静默升级。

契约细节见 [S_08_config-and-environment.md](../specs/S_08_config-and-environment.md)。

---

## MCP 集成：准入、池化与上游补丁

MCP（Model Context Protocol）集成是 common 内体量最大的主题（`mcp_config.py` 约 2720
行），包含三块能力：

- **安全准入**：transport 归一（http/streamable_http → streamable-http，缺省 stdio）、
  企业版禁本地 stdio、stdio 命令白名单（node/python/npx/uvx + 危险参数拦截）、SSRF
  屏蔽内网与元数据地址。
- **两级 worker 池**：请求级（随请求生命周期）与进程级（registry 驱动，remote server
  周期扫描，按配置指纹判等重建）。对话路径只读缓存、不在线 list_tools。
- **两个上游 monkeypatch**：openjiuwen SDK 的 MCP 客户端缺少调用超时与工具列表中途刷新
  能力，common 用进程启动补丁修复——所有补丁幂等（`_PATCHED` 标记）、单一装配点
  （interface_deep）、目标属性名集中为常量。自建 remote client 必须补打
  `_jws_call_timeout` 印章，这是 patch 与业务代码之间的隐形契约。

契约细节见 [S_09_mcp-integration.md](../specs/S_09_mcp-integration.md)。

---

## 观测体系：审计与日志

- **审计三件**：`audit_gate`（全局开关，默认关）+ `audit_net`（补 SRCIP/DSTIP）+
  `audit_emit`（统一入口：企业版与开关两道闸 → 字段归一 → telemetry 优先、foundation
  回退，任何失败仅 warning）。
- **日志体系**：请求级 session id 经 ContextVar 绑定进日志过滤器；用户内容进日志一律
  过 200 字符有界预览（可关闭）；第三方 logger（openjiuwen/openai/httpx）的脱敏装在
  `LogRecordFactory` 层——源头脱敏，漏网最小。
- **活体诊断**：`kill -USR1` 输出全线程栈 + pending 协程快照（gc 全堆扫描，不依赖运行
  中 loop）；Windows 上自动降级为 no-op。

契约细节见 [S_12_observability-and-logging.md](../specs/S_12_observability-and-logging.md)。

---

## 自升级链路：代际重启

common 内置一条完整的自升级闭环：

```
version（唯一版本真源）
  → UpdaterService（检查 + 状态机）
      → version_source（GitHub / GitCode / PyPI 三源，pre-release 总序比较）
      → upgrade_executor（desktop 只下载 / pip 安装，editable 拒绝升级）
          → 写 .restart_pending.json → 拉起独立重启守护
              → 等父进程退出 → 等端口释放 → 分离进程重启 → 删一次性标记
```

安装模式二分（`sys.frozen` / `JIUWENSWARM_DESKTOP=1` 判定 desktop vs pip）；pip 模式
强制走 PyPI 源。版本比较有严格总序（基版本 → stable 恒高于 pre-release →
dev<alpha<beta<rc<pre → 序号），杜绝字符串比较的坑。

契约细节见 [S_14_session-lifecycle-and-upgrade.md](../specs/S_14_session-lifecycle-and-upgrade.md)。

---

## 核心优势

### 1. 单向依赖的清晰地基

common 不感知任何上层，上层全部向下依赖它。这条纪律让"这段逻辑放哪"有了唯一答案：
跨进程共享的进 common，单层私有的留在单层。循环依赖在这个仓库里结构性不存在。

### 2. 统一信封消解协议分叉

新增一种外协议只需写一个适配器（协议形状 ↔ E2A），不需要碰任何业务逻辑。ACP / A2A /
Channel 的差异被压缩在边缘约 600 行适配代码里，内部世界只见 AgentRequest。

### 3. 版本错位容忍

wire 三层兜底 + E2A from_dict 宽容迁移，让 Gateway 与 AgentServer 可以独立升级部署，
旧版本进程发出的消息新版本总能解出——这在双进程滚动升级场景下是刚需。

### 4. fail-open / fail-closed 的全局一致性

观测与安全的容错策略不是各模块各自为政，而是全包统一的分界线。读代码的人不需要逐个
确认"这里挂了会怎样"——看它属于观测还是安全边界，答案就确定了。

### 5. 删除权最小化

工作区九分区 + 三分区删除白名单 + 四层校验链，把 Agent 的破坏力压缩到"只能删自己的
工作产物"。扩删除面必须走白名单修改 + spec 评审，没有旁门。

### 6. 环境隔离的多租户能力

Track A/B 双轨 + overlay 密封 + 配额 ContextVar 身份绑定，让同一进程内的多个 agent /
多个租户互不可见——这是 jiuwenswarm 多 agent 产品的安全底座。

### 7. 上游补丁的工程化收口

对 openjiuwen SDK 的 monkeypatch 不散落各处，而是集中为两个带幂等标记的补丁模块，
单一装配点、常量化目标名。SDK 升级时属性名改一处即可。

### 8. SPI 扩展而非侵入

配置源（ConfigProvider）、路径解析（PathProvider）、加密提供方（CryptoProvider）、
持久化介质（PersistenceGateway）全部走协议 + 注册表。企业版注入配置源不需要改 common
一行代码。

---

## 能力边界

### 当前提供

- 外协议 ↔ 内部模型的统一信封转换（ACP / A2A / Channel）
- 贯穿全仓的核心数据模型与方法 / 事件 / 模式枚举
- 敏感配置的注册、信封加解密（AES-256-GCM / X25519 DEK）与多介质存储
- 服务间 mTLS（三态渐进）与 WS Origin 白名单
- thinking / reasoning 参数的厂商适配注入
- 工作区分区、删除安全链、存储配额
- 双轨环境变量 + config.yaml 合并解析 + 两个进程级 SPI
- MCP 配置准入、两级 worker 池、上游补丁
- 审计打点链、日志会话绑定与源头脱敏、USR1 活体快照
- 请求身份与扩展透传、todo 快照恢复、后台清理、cron 收轮判定
- 三源版本检查与自升级（desktop 下载 / pip 安装 + 代际重启）

### 暂不提供

- **不承载业务编排**——team 协作、任务看板等在 `agents/` 与 `server/runtime/`，common
  只提供它们需要的公共件（如 cron 收轮判定状态机）
- **不做 HTTP 服务框架**——WS / HTTP 的服务端实现在 gateway / server，common 只提供
  限额常量、Origin 白名单与诊断工具
- **自定义加密默认未接线**——secrets 的初始化 API（configure_aes256gcm 等）已就绪但
  无生产调用点，默认装配链是明文直通
- **DB 持久化介质是 stub**——secrets 的 db backend 抛 NotImplementedError，为后续扩展
  预留
- **A2A 入站未接线**——`envelope_from_a2a_send_message` 已实现并测试，生产路径走
  gateway 侧自有通道
- **不提供跨进程的分布式协调**——common 内的"进程级"均指单进程内；跨进程状态一致性
  由 gateway ↔ agentserver 的消息协议（经 E2A wire）保证

---

## 术语表

| 术语 | 含义 |
|------|------|
| E2A / 信封 | Everything-to-Agent 统一信封协议，版本恒 `"1.0"`，外协议与内部模型的中间表示 |
| 投影（projection） | E2AResponse 翻译回具体协议形状的过程 |
| AgentRequest | 内部统一请求模型，由 E2A 信封转换而来，贯穿 Gateway 与 AgentServer |
| ReqMethod | 约 260 成员的方法域枚举，方法路由的词法根 |
| canonical mode | `agent.work.normal` 式三段运行模式，共 8 种 |
| Track A / Track B | 环境变量双轨：spawn 键驻留 os.environ；业务键存进程内 (sid,aid) 命名空间 |
| tip / overlay | agent 级生效环境快照 / 任务级密封覆盖（bind 即 seal，miss 即 unset） |
| zone | 工作区九分区；删除许可仅 DELETABLE_ZONES 白名单三分区 |
| 配额门禁 | check_workspace_write 写前拦截；默认关闭、最终一致 |
| worker 池 | MCP 会话复用单元，分请求级与全局（registry 扫描驱动） |
| 上游补丁（patch） | 对 openjiuwen SDK 的进程启动 monkeypatch，幂等、单一装配点 |
| `_jws_call_timeout` 印章 | 自建 remote MCP client 必须补打的超时标记，patch 与业务代码的契约 |
| 亲和（affinity） | Ascend KV cache 亲和；开启 ⇒ release 必 false 且 provider 必为 AscendAffinity |
| 身份引用 | `model-identity-v1:<sha256>`，跨组件指代模型配置的无凭证指纹 |
| 路由身份 | Web 请求四字段（user_id / group_id / bot_id / gateway_id）的归一化落点 |
| final_mode | chat.final 事件的协议注解，前端免字符串启发式 |
| fail-open / fail-closed | 观测类失败不阻断业务 / 安全边界失败直接拒绝的全局分界 |
| HITL | Human-in-the-Loop 人机交互中断；E2A 分派中特判为非终态 |
| 代际 token | todo 快照的版本标记，无 token 的旧条目一律保留 |
