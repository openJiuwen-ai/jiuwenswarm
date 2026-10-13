# 工具护栏与 A4P SDK 对接方案

## 1. 概述

本方案引入 A4P SDK （OpenJiuwen Agent Protocol仓库），在 JiuwenSwarm 中实现动态的预授权。当 Agent 执行跨多个工具的复杂任务时，或用户不在场 Agent 执行定时任务时，A4P SDK 可为工具调用提供意图授权与 token 校验能力。

当前依赖固定为 Agent Protocol 仓库的 `agent-protocol-V0.2.0` tag（`A4P` 子目录）。

A4P 的核心增量价值：
1. 当 Agent 执行跨多个工具的复杂任务时，可以申请 A4P 意图授权，用户一次批准明确 scope 后，后续匹配的工具调用可以通过可验证的 intent token 自动放行，避免连续重复审批，影响用户体验。
2. 当用户不在场 Agent 执行定时任务时，可以预先申请 A4P 意图授权，在定时任务执行过程中可自动放行匹配的工具调用，无需将该工具持久化设为allow，引入安全风险。

A4P 的核心流程：
- **Intent 授权**：Agent 调用 `request_a4p_intent_authorization` 申请覆盖一组 action/params 的 intent token。
- **工具护栏校验**：进入权限确认回调后，交互式 Web 调用查当前 session 的 A4P intent token；cron 执行时按 `cron.job_id` 查本地绑定的 A4P intent token；命中并通过 SDK 校验则放行。Web 下未申请 A4P 时，未命中 token 回退现有工具护栏审批。该回调并不是所有工具调用的必经入口，权限层的适用边界见 4.2。

**通道隔离**：A4P 仅介入 Web 通道和 Cron 定时任务通道的权限校验；ACP/TUI/CLI 行为不变。

---

## 2. 关键流程

### 2.1 Intent 授权申请流程

```mermaid
sequenceDiagram
    participant Agent as LLM
    participant Tool as 意图授权工具
    participant Runtime as JiuwenSwarm A4P Runtime
    participant Server as A4P Server
    participant Front as Web 前端
    participant User as 用户

    Agent->>Tool: actions=[{name, params}]
    Tool->>Runtime: request_intent_authorization(session_id, identity, actions)
    Runtime->>Server: prepare_intent_authorization(...)
    Server-->>Runtime: mandate + signingOptions
    Runtime->>Runtime: 校验 mandate 的 Server 签名及信任链
    Runtime->>Front: push a4p.authorization_request
    Front->>User: 展示 A4P Intent 授权卡片
    User->>Front: 授权 / 拒绝
    Front->>Runtime: complete(requestId, session_id) / reject
    alt 用户批准
        Runtime->>Runtime: SDK approve_user_mandate 构造本地用户同意对象
        Runtime->>Server: complete_intent_authorization(signedMandate)
        Server-->>Runtime: intent token
        Runtime->>Runtime: 存入 session token 池或 cron token store
        Runtime-->>Agent: {ok, approved, token 摘要}
    else 用户拒绝或超时
        Runtime-->>Agent: {ok, approved=false, rejectReason}
    end
```

说明：
- 用户在 Web 卡片中确认授权，前端提交 `requestId` 和 `session_id`。后端按请求 ID 和逻辑路由定位待授权 mandate，使用 SDK `approve_user_mandate()` 构造本地用户同意对象，通过 `signedMandate` 协议字段交给 A4P Server 完成授权并签发 intent token。
- Runtime 使用 SDK `verify_local_user_authorization_request()` 和服务器信任配置校验 mandate，校验成功后才推送前端；权限范围调整后生成的 mandate 使用同样的校验流程。
- token 由 A4P Server 签发并验证，绑定内部固定 `userId`、当前执行上下文解析出的 `agentId`、action scope、params scope 和有效期；`agentId` 优先取 metadata，其次取当前 Agent 标识，并非固定常量。
- `params` 解释为安全关键字段的子集约束；JiuwenSwarm 校验关键字段后，在调用 SDK 时统一补充协议字段 `allowExtraParams: true`。因此真实工具调用可携带未列入 scope 的普通参数，但 scope 中列出的字段必须匹配；授权工具返回给 Agent 的 token 摘要会隐藏该内部字段。
- session token 存入当前 AgentServer 进程的内存池，重启或关闭 A4P 后不保留；cron token 则按已存在的 `cronJobId` 持久化。Agent 后续调用工具时不需要手动携带 token。

### 2.2 工具调用时序

以下时序以权限层已要求 host 确认为前提，省略之前的权限规则和智能权限决策。

```mermaid
sequenceDiagram
    participant Agent as LLM Agent
    participant Rail as PermissionInterruptRail
    participant Runtime as A4P Runtime
    participant Server as A4P Server
    participant Front as Web 前端
    participant User as 用户

    Agent->>Rail: 调用 write_file(file_path, content)
    Note over Agent,Rail: token 对 Agent 透明，由 session_id 关联
    Rail->>Runtime: 查找匹配 intent token(session_id, action, params)
    Runtime->>Server: verify_intent_token(token, expected)
    alt token 命中且 valid=True
        Server-->>Runtime: valid=True
        Runtime-->>Rail: approved=True
        Rail-->>Agent: 工具正常执行
    else 无有效 token且本轮未申请 A4P
        Runtime-->>Rail: 无匹配 token
        Rail->>Front: 走现有工具护栏 permission interrupt
        Front->>User: 展示普通工具审批卡片
        User-->>Front: 批准/拒绝
        Front-->>Rail: 返回结果
        Rail-->>Agent: 放行或拒绝
    else 无有效 token且本轮 A4P 已拒绝/失败或 scope 不匹配
        Runtime-->>Rail: 授权状态与失败原因
        Rail-->>Agent: 直接拒绝并返回 A4P 错误
    end
```

### 2.3 Cron 定时任务授权流程

cron 通道的授权分两个阶段：创建 job 时申请并绑定 intent token（在场），未来触发时按 `cron.job_id` 查 token 校验（无人值守）。

#### 阶段一：创建 cron job 并申请 intent 授权

```mermaid
sequenceDiagram
    participant Agent as LLM Agent
    participant Cron as cron_create_job
    participant Tool as 意图授权工具
    participant Runtime as A4P Runtime
    participant Server as A4P Server
    participant Front as Web 前端
    participant User as 用户
    participant Store as cron_intent_tokens.sqlite3

    Agent->>Cron: 创建 disabled cron job(enabled=false)
    Cron-->>Agent: job.id
    Agent->>Tool: request_a4p_intent_authorization(cronJobId=job.id, actions=...)
    Tool->>Runtime: request_intent_authorization(session_id, identity, actions, cron_job_id)
    Runtime->>Server: prepare_intent_authorization(...)
    Server-->>Runtime: mandate + signingOptions
    Runtime->>Front: push a4p.authorization_request
    Front->>User: 展示 A4P Intent 授权卡片
    User->>Front: Approve / Reject
    Front->>Runtime: a4p.authorization.complete / reject
    alt 用户批准
        Runtime->>Server: complete_intent_authorization(signedMandate)
        Server-->>Runtime: intent token
        Runtime->>Store: 持久化 job.id -> intentToken
        Runtime-->>Agent: {ok, approved, token 摘要}
    else 用户拒绝或超时
        Runtime-->>Agent: {ok, approved=false, rejectReason}
    end
    alt 授权成功
        Agent->>Cron: cron_toggle_job(job.id, enabled=true)
        Cron-->>Agent: job enabled
    else 授权拒绝/失败/超时
        Note over Agent,Cron: job 保持 disabled，不得启用
    end
```

#### 阶段二：cron 触发时校验 intent token

```mermaid
sequenceDiagram
    participant Cron as Cron Scheduler
    participant Rail as PermissionInterruptRail
    participant Store as cron_intent_tokens.sqlite3
    participant Runtime as A4P Runtime
    participant Server as A4P Server
    participant Agent as LLM Agent

    Cron->>Agent: 触发 cron job(metadata.cron.job_id=job.id)
    Agent->>Rail: 调用需要 host 确认的工具
    Rail->>Runtime: find_valid_cron_intent_token(job.id, identity, action, params)
    Runtime->>Store: 查询 job.id 对应 token
    Store-->>Runtime: intent token / 无 token
    alt cron token 命中且 SDK 校验通过
        Runtime->>Server: verify_intent_token(token, expected)
        Server-->>Runtime: valid=True（含 action/params/agent/expireAt 校验）
        Runtime-->>Rail: approved=True
        Rail-->>Agent: 工具正常执行
    else 无有效 token 或校验失败
        Runtime-->>Rail: 无有效匹配 token / 校验异常
        Rail-->>Agent: 直接拒绝并返回 A4P_CRON_SCOPE_DENIED
        Note over Rail,Agent: 返回 job id、tool 和 scope/过期/身份排查方向，不生成审批卡片
    end
```

说明：
- 阶段一先以 `enabled=false` 创建 job；授权工具拒绝绑定 enabled job。Runtime 拿到 token 后通过 SQLite store 原子写入 `a4p/cron_intent_tokens.sqlite3`（以 `cronJobId` 为键），Agent 仅在授权工具返回 `ok=true` 后调用 `cron_toggle_job(..., enabled=true)`。授权拒绝、失败或超时时 job 保持 disabled。
- 授权工具会先确认 `cronJobId` 对应的 job 存在且已禁用，再由 Runtime 把 job id、名称、任务内容、schedule 和持久化属性放入 `uiContext.authorizationTarget`；授权卡片通过“定时任务”区块展示 job id、任务内容、schedule 和时区。
- `cronJobId` 来自 `cron_create_job` 返回的 `job.id`，用于本地绑定和授权展示，不注入被授权工具的参数；A4P token 的校验约束为 action/params scope、agent/user 身份和有效期，不包含本地 job 归属校验。
- 阶段二与 2.2 基本一致，差异仅在于 Runtime 通过 `find_valid_cron_intent_token()` 按 `metadata.cron.job_id` 查 token，而非按 `session_id` 查 session token 池。
- 每次 cron 模型调用前，`RuntimePromptRail` 按 `metadata.cron.job_id` 自动注入该 job 的授权 actions、params 和有效期；不向 Agent 暴露原始 token 或 SDK 内部协议字段。Prompt 明确要求只执行 scope 内调用，并提示 scope 外、过期或身份不匹配的调用无法完成 cron 场景下的交互式审批，因而可能失败。
- A4P 启用时，进入权限 host 确认回调的 cron 调用遇到 scope miss、缺失 job id 或校验失败，采用 fail-closed：直接向 Agent 返回 `A4P_CRON_SCOPE_DENIED`，不回落 session token，也不创建普通 permission interrupt。该限制不覆盖此前已由权限层直接处理的调用（见 4.2）。Agent 可根据反馈调整后续步骤或报告失败。
- 删除 cron job 不主动删除对应 token；cron token 默认有效 30 天，可由授权工具传入的 `validitySeconds` 覆盖。过期记录在 A4P runtime 初始化或现有存储读取清理路径中删除。

### 2.4 Token 匹配规则

```mermaid
flowchart TD
    A[工具调用<br/>action + actual_params] --> B[遍历当前 session tokens]
    B -->|取下一枚| C{SDK 验签、身份和有效期通过?}
    B -->|已遍历完| J[无有效匹配 token]
    C -->|否| B
    C -->|是| D{存在同名 action 的 params 全部匹配?}
    D -->|否| B
    D -->|是| F[返回有效 token]
```

授权在进入 SDK 前统一使用子集匹配语义，实际调用中未列入 `params` 的普通字段不会导致失败。

SDK 要求约束 key 均存在：字符串按区分大小写的 `fnmatchcase` glob 匹配，约束值 `*` 可匹配任意值，其他值按相等比较。同名 action 中任意一项完整匹配即可，不会因第一项不匹配就终止查找。Shell 命令在申请和校验时都会移除包裹安全绝对路径字面量的引号；glob 不理解 Shell 语法或路径目录边界，窄范围约束仍需按第 5 节申请规则表达。

---

## 3. 配置与开关

`config.yaml::a4p` 当前包含配置字段：

```yaml
a4p:
  enabled: false
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | A4P intent 授权开关 |

有效期和审批等待超时使用代码默认值：授权工具的 `validitySeconds` 为可选参数，未传入时，session token 有效期为 `3600` 秒（1 小时），cron token 有效期为 `2592000` 秒（30 天）；显式传入时采用该有效期。等待用户批准/拒绝的超时固定为 `300` 秒（5 分钟）。授权工具仅要求 `actions` 必填；绑定定时任务时还需提供 `cronJobId`。

JiuwenSwarm Web 当前按单用户个人助理处理，因此 A4P SDK 所需的 `userId` 由 runtime 内部使用固定常量填充。

### 3.1 Web 授权设置

用户在“设置 → 实验功能 → A4P 意图授权”中控制 A4P 开关。配置由 AgentServer 校验、持久化并应用到运行时，界面展示加载、保存和失败状态。配置管理要求交互式 Web route，A4P 关闭时也可访问。

授权采用用户点击确认的流程。审批卡片按“授权原因”“授权范围”展示申请信息，用户可勾选需要批准的动作后点击“授权”，或点击“拒绝”结束本次申请。

---

## 4. 代码对接点

### 4.1 Runtime

`A4PRuntime` 实现 intent 授权与 token 校验：

- 使用内嵌的 `A4PServer`，不使用 A4P SDK 的本地 `A4PHTTPServer`。
- `A4PServer` 使用直接确认授权模式（SDK 参数 `require_user_signature=False`）；Runtime 持有 `StaticA4PServerTrustStore`，用于校验待确认 mandate 的服务端签名。
- A4P 配置变化时终止旧 runtime 的 pending 授权并重建 runtime；启用状态下重建 runtime 会保留已签发的 session token，cron token 继续从持久化 store 读取。
- `request_intent_authorization()` 显式调用 `server.prepare_intent_authorization()` 生成 mandate 和 `signingOptions`，在 Web 用户批准后以 `signedMandate` 调用 `server.complete_intent_authorization()` 签发 token。
- `dispatch_a4p_request()` 将批准/拒绝 RPC 分发给 `runtime.authorizer.complete()` / `reject()`（`WebAuthorizerBroker`）；pending 绑定逻辑路由 `session_id + app_id + agent_ref`，`ws_id` 仅用于当前连接投递。
- `runtime.authorizer.pending_for_route()` 按逻辑路由返回尚未结束的授权请求，供 Web 重连或切回 session 后恢复卡片。
- session token 在查找、导出和导入时过滤过期项并删除空会话条目；session/cron 共用与 SDK 一致的过期判断（UTC 秒格式，当前时间严格晚于 expireAt 才过期），过期、缺失或非法 expireAt 的新 token 不入库。闲置 session 不做定时扫描或主动释放。
- `find_valid_intent_token()` 通过 SDK `verify_intent_token()` 校验 session token。
- `a4p/cron_intent_tokens.sqlite3` 持久化保存 `cronJobId -> token` 绑定，由 AgentServer 读写，Gateway 不访问 token 数据库。
- `SQLiteCronIntentTokenStore` 保存任务 ID 与 token 的绑定。
- `find_valid_cron_intent_token()` 按当前 `cron.job_id` 查询绑定，通过 SDK 校验 action、params、agent 和有效期，不远程检查任务是否存在。
- `prune_expired()` 全量清理过期或无效记录，在 A4P runtime 初始化时调用；保留 `get()` 对当前任务 token 的按需清理；移除无业务调用的 `status()`、`cron_intent_token_status()` 和 `summaries()`。孤立 token 到期后需等上述清理路径触发从数据库删除。
- A4P 设置通过 `a4p.config.get` / `a4p.config.update` 由 AgentServer 校验、持久化并更新运行时；接口返回 `enabled` 布尔值，关闭 A4P 时仍可管理配置。
- 授权 broker 使用宿主注入的 `send_runtime_push`，不引用 AgentServer 单例。Gateway 保留协议转发和可信路由校验，不 import A4P 实现。

### 4.2 权限护栏

A4P 接入 `interrupt_helpers.py` 构建的 `ToolPermissionHost.request_permission_confirmation` 回调。基础 `PermissionInterruptRail` 先处理场景规则、权限 `allow` / `deny` 和已有自动确认；只有仍需确认时才进入该回调。启用智能权限模式时，外层 `AutoPermissionInterruptRail` 还会先执行自身的确定性规则和 reviewer 流程；这些分支可能直接放行、拒绝或要求人工确认，只有实际转入基础 rail 的 host 回调时才使用 A4P。

该回调内，除静默 `skills.rebuild` 和授权申请工具自身的直接放行特例外，A4P 的执行顺序如下：

1. 如果 A4P 未启用，直接走现有权限确认逻辑。
2. 仅 `__cron__` 或具有有效 AuthorizerRoute 的交互式 Web 上下文进入 A4P 分支；如果 metadata 存在 `cron.job_id`，先从 `a4p/cron_intent_tokens.sqlite3` 查该 job id 对应 token。
3. cron token 命中且 SDK 校验通过，则返回 `PermissionConfirmResponse(approved=True)`。
4. `__cron__` 通道的 job id/token 缺失、过期、scope 不匹配或校验异常时直接返回 `A4P_CRON_SCOPE_DENIED`，不生成普通审批；交互式 Web 若未命中 cron token，则继续检查 session token。
5. 普通 Web round 解析统一 Host 执行上下文中的 `session_id`、`agentId`、工具名和参数，查当前 session 的有效 intent token。
6. session token 命中则返回 `PermissionConfirmResponse(approved=True)`。
7. 当前 round 未申请 A4P 时，token 未命中返回 `"interrupt"`，交给现有工具护栏 ask。
8. 未命中有效 session token，且当前 round 已申请 A4P，但授权被拒绝、超时、内部失败、验签失败或实际调用不匹配已批准 scope 时，直接拒绝并返回 `A4P_AUTHORIZATION_UNAVAILABLE` 或 `A4P_SCOPE_DENIED`。先前已批准且仍匹配的 token 优先于本轮失败状态。

工具、权限 host 和 prompt 可见性使用同一个 session 级 `AuthorizationExecutionContextStore`。上下文在取得输出 lease 并开始 round 时激活，结束、取消或初始化失败时释放。


### 4.3 Web / API

Web RPC：

| RPC | 说明 |
|---|---|
| `a4p.config.get` | 读取 A4P 开关配置 |
| `a4p.config.update` | 校验并持久化 A4P 配置，更新运行时 |
| `a4p.authorization.reprepare` | 在原始动作范围内重新选择并生成待确认 mandate |
| `a4p.authorization.complete` | 用户批准当前 intent mandate |
| `a4p.authorization.reject` | 用户拒绝当前 intent mandate |
| `a4p.authorization.pending` | 查询当前逻辑会话尚未完成的授权，用于重连恢复 |

Web 审批卡片保留标题、授权／拒绝按钮，正文按“授权原因”“授权范围”排列。原因来自 `uiContext.reason`，为空时采用默认说明；范围展示工具名称、完整参数、勾选框和已选数量。session 与 cron 使用同一展示方式，加载、空选及失败状态提供必要提示。

批准请求只提交 `requestId` 和 `session_id`。`requestId` 取自 `mandateId`，用于关联卡片与等待中的请求。Gateway 从当前 WebSocket 的 RoutingKey 构造调用方路由；AgentServer 校验 `session_id + app_id + agent_ref` 后完成或拒绝 pending，同一逻辑会话重连后通过 pending RPC 恢复。

mandate 的有效时间、cron 任务绑定、`signingOptions` 和 `uiContext` 由协议与运行时管理。`displayText` 是协议必填字段（参与服务端签名），由 A4P SDK 生成默认一句话文本；JiuwenSwarm 不再自定义渲染其内容，Web 卡片直接结构化渲染 `intent.actions` 等字段，不展示 `displayText`。设置页提供 A4P 开关。

---

## 5. Agent 引导

Agent 仍通过 `request_a4p_intent_authorization` 主动申请 intent token。该工具和完整的申请规则只在 A4P 启用、具有有效 AuthorizerRoute 的顶层交互式 Web round 中暴露；cron、TUI、ACP、后台 goal 和 team member/subagent 均不注入 A4P 申请 prompt，Team leader 统一负责申请。误调用会立即返回 Web 会话错误而不会等待授权超时：

| 机制 | 说明 |
|---|---|
| ToolCard description | 描述工具用途、Web 分阶段授权及 session/cron token 存储方式 |
| ToolCard input schema | 描述 action 安全关键字段、shell 精确命令和文件路径约束；输入由 `_ACTION_SCOPE_POLICY` 及相关校验处理 |
| RuntimePromptRail 动态段 | 描述何时授权、范围约束、规划与动态参数、Web 分阶段授权和 cron 生命周期 |

建议 Agent 申请规则：
- 当前 Web prompt 要求：涉及支持的受保护工具时，在第一个受保护调用前申请 A4P 授权，通过这些工具执行只读操作也不例外。实现仍保留本轮未申请 A4P 时的普通审批回退（见 4.2）。
- 可以先使用受保护列表之外的工具读取 Skill、定位文件和发现参数。
- 后续受保护调用依赖前一阶段的动态结果时，可先授权并执行范围已明确的阶段；只有后续调用不在已批准范围内时才申请新 token。运行时值变为已知本身不要求重新授权。不同阶段的 session token 会共存，已签名 token 不会被修改或扩展。
- 不使用 `<KEY>`、`<URL>` 等占位符猜测动态值；每个受保护调用执行前都必须命中某个已批准 token。
- Cron 不采用分阶段 token：先明确完整未来 scope，动态参数的构成方式和允许范围可预先表达时，无需提前取得运行时值；严格执行“`cron_create_job(enabled=false)` → 对完整未来 scope 申请 A4P intent 授权 → 授权成功后 `cron_toggle_job(enabled=true)`”；失败时不得启用。仅创建定时任务时，不要求先试运行任务。
- `actions[].name` 使用真实工具名，例如 `write_file`、`edit_file`、`bash`。
- `actions[].params` 必填且不能为空，只填写必须匹配的安全关键字段；真实调用中的其他普通参数由系统自动允许。
- `bash`、`mcp_exec_command` 必须约束 `command`，`create_terminal` 必须约束 `cmd`，`write_file`、`edit_file` 必须约束 `file_path`，`acp_chat` 必须约束目标 `agent`。参数名必须与真实工具 schema 完全一致；未登记安全关键字段策略的工具不签发 intent token。提交不支持的 action 会把本轮申请记为失败，因此不能保证随后自动回落普通审批（见 4.2）。
- shell 默认分别授权完整精确命令；只有单个动态参数无法提前确定，且通配不会覆盖空白、Shell 运算符、重定向或额外参数时，才使用窄范围通配。文件路径使用具体文件名或固定目录下的文件名 glob，不以目录路径代替文件范围，不使用裸 `*`。这些通配与占位符限制包含 prompt/schema 对 Agent 的要求，当前输入校验并未完整强制检查 Shell 通配和未解析占位符。
- 不要为了减少 action 数量合并计划中独立执行的 shell 步骤；创建目录和最终 `ls` 验证应分别授权、分别调用。
- cron 授权的 `validitySeconds` 应覆盖用户要求的任务持续时间；省略时默认有效 30 天。

---


## 6. 设计取舍

- A4P 的操作授权模式与普通工具 ask 本质重复，在JiuwenSwarm 中暂不采用。
- 用户同意由交互式 Web 会话中的显式确认表达；服务端验证请求归属并构造本地同意对象。mandate 的服务端签名、intent token 的签发与校验由 SDK 负责。
- A4P token 由 session / cronJobId 级自动匹配附带，Agent 无需手动传 token。
- JiuwenSwarm 隐藏 SDK 的 `allowExtraParams` 协议细节，并通过 action scope policy 负责 Agent 输入校验；SDK 继续负责 token 签发及 action、params、agent 和有效期验证。


## 部分授权与范围修改

Web 授权卡片保留首次请求的全部动作，用户可在最终确认前反复选择子集。
勾选变化只更新本地选择。用户点击“授权”时，若所选范围与当前 mandate 不同，前端一次性调用重新生成接口，并在成功后自动完成最新 mandate 的授权；范围相同时直接完成授权。提交期间锁定范围选择和审批操作；空选时提示至少选择一项权限或拒绝。
重新生成失败时终止提交；完成授权失败时保留当前请求供重试。取消选择的动作仍保留，
可在后续修改中重新选中；新增原始请求之外的动作需要重新发起授权。

`a4p.authorization.reprepare` 接收 `session_id`、`requestId` 和 `selectedActionIndexes`，
索引始终对应首次请求的 `originalActions`，不是当前缩减后的数组。
pending 查询及推送包含 `originalActions`、`preparedActionIndexes`，替换推送包含
`replacesRequestId`；查询中的 `repreparing` 表示正在修改范围。
整条交互共用 300 秒超时。修改失败、取消或超时不会恢复旧请求。

授权工具结果的 `partial` 表示最终授权是否为原始请求的真子集，`authorizedActions`
和 `unselectedActions` 分别描述已授权与未授权范围。部分成功仍返回 `approved=true`。
普通会话保留其他有效 token；定时任务保存最终范围的 token，未覆盖动作在自动执行时拒绝，
授权后仍需单独启用任务。未选动作不构成显式禁止规则。

此实现只在 JiuwenSwarm 入口封锁旧 requestId，不删除底层 SDK 的旧 pending，
不提供直接调用 SDK 时的协议级撤销保证。
