# Gateway A2A HTTP 接口文档

> 核验日期：2026-09-17。Gateway 基线：`8d578b702c75f8e215b37dbaec3c335a57ece90e`。Manager 等价映射对照 `agent-runtime` 基线 `d2dfcfb7309b0b4c0119e310ff9933ee37aa8bee`。
>
> 本文根据 `doc/A2A_Manager_统一设计与实现.md`、`doc/A2A_Manager_企业版Gateway接入计划.md` 和实际路由、DTO、处理器、服务层整理；设计计划不能代替已实现行为。以下示例为按代码结构构造的契约示例，不是线上请求实录，不证明线上镜像或部署已经验证。

## 1. 范围、入口和版本边界

本文覆盖 13 个公共出站 Web HTTP 路由和 6 个企业 Config Receiver 写操作。两套入口、权限、响应信封不同，不能混用。

| 入口 | 基础地址与配置 | 接口文档 | 用途与身份范围 |
| --- | --- | --- | --- |
| Gateway Web HTTP | `http(s)://{web-http-host}:{port}`；`GATEWAY_WEB_HTTP_PORT`，默认 `WEB_PORT + 2`，通常为 19002 | `/doc`、`/openapi.json`；`GET /api/v1/catalog` 查看注册清单 | 浏览器/BFF 用户入口；企业版按上游可信身份和业务资源限制 |
| Gateway Config Receiver | Manager 实例记录中的 `gateway_config_host`；监听 `GATEWAY_CONFIG_HTTP_HOST`（默认 `0.0.0.0`）和 `GATEWAY_CONFIG_HTTP_PORT`（默认 8775） | `/docs`、`/openapi.json` | Manager→当前 Gateway 的配置投影；无路径级租户或实例段 |

部署模板 `deploy/enterprise/templates/gateway.template.yaml` 已包含 Config Receiver 的 `config-http` ClusterIP 和 NodePort Service。其端口不是 Web HTTP 端口；应以部署值/Manager 实例配置为准，不能固定使用示例端口或仅凭 Web HTTP catalog 判定 Receiver 能力不存在。

企业版不启用 A2A ingress：不挂载 `/api/v1/a2a/ingress*`，也不注册对应 Web handler。个人版入站协议和接入方式见 [A2A 接入说明](A2A.md)，不纳入本次企业出站接口交付。

### 1.1 资源和 ID 对照

| 概念 | 个人版 | 企业版 |
| --- | --- | --- |
| 可派发注册项 `agent_id` | Registry 注册时生成 `agent_<uuidhex>` | 等于 Manager 下发的 `a2a_outbound_template.template_id` |
| Agent 名称 | `display_name` | 从 `template_name` 投影为 `display_name` |
| 策略 ID | 不使用 Manager 访问策略 | POST Body 使用 `policy_id`；Receiver PATCH/DELETE 路径变量虽名为 `{template_id}`，值仍为该 `policy_id` |
| 业务 Agent 资源 | 不以 Manager 资源过滤目录 | `X-Bot-Id` 对应 `instance_agent_resource.resource_id`；不是第三方 `agent_id` |
| 发现候选 | `discovery_id`，短期预览，不能派发 | 由 Manager 管理；不能用用户 Web HTTP 发现/注册改写投影 |
| 本地派发 | `dispatch_id` | 同样使用本地 ID；不能用远端 `remote_task_id` 替代 |

企业目录是 Manager 模板的运行态投影，不是 Manager 模板列表 API；运行态 `/a2a/outbound/agents` 不能冒充 `/a2a-outbound-templates` 查询。

## 2. Web HTTP 公共契约

### 2.1 身份与请求头

| 请求头 | 是否需要 | 代码行为 |
| --- | --- | --- |
| `Content-Type: application/json` | 写操作 | Body 是业务 JSON 对象，不传 WS 的 `type/method/params` 外层 |
| `X-Request-Id` | 可选 | 缺省生成 `uuid4().hex`；响应头和信封回显 |
| `X-Bot-Id` | 企业目录、启停、历史详情需要 | 写入路由身份；对应当前业务 Agent 资源 ID |
| `X-User-Id` | 企业目录、启停、历史列表和详情需要 | 用户开关和历史归属均使用连接/HTTP 可信身份，不使用 Body 中的 `user_id` 或 `source_user_id` |
| `X-Session-Id` | 企业历史详情需要原始派发会话 | 不传会生成临时 `webhttp_*` 会话，通常无法匹配记录归属 |
| `X-Group-Id` | 企业路由上下文可选 | 透传身份；A2A 目录策略解析实际以资源 ID 为入口，不能假定额外存在 group 过滤 |
| `Authorization` | 取决于上游认证 | 当前 Web HTTP 应用本身不校验 Bearer JWT，不能据此声称该入口已经鉴权 |

`web_http_dispatch.py` 中 `GATEWAY_WEB_HTTP_TRUST_CLIENT_HEADERS` 可显式设为真/假；未设置时企业版信任上游身份头，个人版只对回环客户端信任身份头。生产入口应由上游认证层清除/覆盖外部可伪造的 `X-*` 身份头。代码中的“可信身份”指入口构造的路由/连接身份，不意味着任意外网调用者可以自行声明用户身份。

Body/path/query 合并规则：只读取该路由声明的 query；path/query 覆盖 Body 同名值；通用头只补缺失的 params。企业目录/启停/详情 handler 接收的 `bot_id` 随后由 `metadata.routing` 覆盖，目录、启停及历史 handler 的 `user_id` 均由连接身份注入。不要把业务资源 ID 当作第三方 `agent_id`。

### 2.2 响应和 HTTP 状态

所有本文出站 Web HTTP 操作均为 Unary JSON，不是 SSE。成功注册为 HTTP 201，其余成功为 200，删除不是 204。

```json
{
  "request_id": "req-a2a-01",
  "ok": true,
  "data": {"items": [], "total": 0},
  "metadata": {"rpc_method": "a2a.outbound.list", "transport": "web-http"}
}
```

失败信封：

```json
{
  "request_id": "req-a2a-02",
  "ok": false,
  "error": {"code": "FORBIDDEN", "message": "企业版配置由管理面统一下发", "details": {}},
  "metadata": {"rpc_method": "a2a.outbound.discover", "transport": "web-http"}
}
```

响应头包括 `X-Request-Id`、`X-Web-RPC-Method`。通用错误映射为：`BAD_REQUEST→400`、`UNAUTHORIZED→401`、`FORBIDDEN→403`、`NOT_FOUND/METHOD_NOT_FOUND→404`、`CONFLICT→409`、`TIMEOUT→504`、`SERVICE_UNAVAILABLE→503`；其他码默认 HTTP 500。

**当前 `A2A_*` 领域码没有单独的 HTTP 状态映射**，例如 `A2A_DISPATCH_NOT_FOUND`、`A2A_AGENT_NOT_AUTHORIZED` 实际走 HTTP 500，不应在接入或测试中假设为 404/403。请同时判断 HTTP 和 `error.code`。FastAPI 请求体类型/JSON 校验失败可能先返回 422 `detail`，不经过上述业务信封。

## 3. 13 个 Web HTTP 出站操作清单

所有路径均为完整路径。企业 gate 在业务 handler 之前执行；表中“拒绝”表示路由返回 HTTP 403、`error.code=FORBIDDEN`。

| 方法 | 完整路径 | RPC method | 企业用户入口 | 个人版 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/a2a/outbound/settings` | `a2a.outbound.settings.get` | 拒绝 | 可用 |
| PATCH | `/api/v1/a2a/outbound/settings` | `a2a.outbound.settings.update` | 拒绝 | 可用 |
| POST | `/api/v1/a2a/outbound/discover` | `a2a.outbound.discover` | 拒绝 | 可用 |
| POST | `/api/v1/a2a/outbound/agents` | `a2a.outbound.register` | 拒绝 | 可用，成功 201 |
| GET | `/api/v1/a2a/outbound/agents` | `a2a.outbound.list` | 放行，资源策略过滤 | 可用 |
| GET | `/api/v1/a2a/outbound/agents/{agent_id}` | `a2a.outbound.get` | 拒绝 | 可用 |
| PATCH | `/api/v1/a2a/outbound/agents/{agent_id}/enabled` | `a2a.outbound.enabled.update` | 放行，只写本地启停 | 普通个人 Repository 不提供该 setter，见 §4.7 |
| PATCH | `/api/v1/a2a/outbound/agents/{agent_id}` | `a2a.outbound.update` | 拒绝 | 可用 |
| POST | `/api/v1/a2a/outbound/agents/{agent_id}:refresh` | `a2a.outbound.refresh` | 拒绝 | 可用 |
| POST | `/api/v1/a2a/outbound/agents/{agent_id}:confirm-revision` | `a2a.outbound.confirm_revision` | 拒绝 | 可用 |
| DELETE | `/api/v1/a2a/outbound/agents/{agent_id}` | `a2a.outbound.delete` | 拒绝 | 可用 |
| GET | `/api/v1/a2a/outbound/dispatches` | `a2a.outbound.dispatch.list` | 放行，用户过滤 | 可用 |
| GET | `/api/v1/a2a/outbound/dispatches/{dispatch_id}` | `a2a.outbound.dispatch.get` | 放行，user/session/resource 校验 | 可用，DTO 不同 |

企业 Registry 对 Manager-owned Repository 还拒绝目录变更，不能绕过 Web gate 修改投影。Agent 工具通过私有反向 RPC 使用 find/dispatch/get 能力。

## 4. Web HTTP 请求与行为

以下成功示例中的 `data` 为 §2.2 信封的业务部分；完整公共 Agent DTO 见 §5.1。未声明 query 的接口不接受本文未列出的筛选/分页参数。

### 4.1 GET `/api/v1/a2a/outbound/settings`

无 Body、无业务 query。个人版读取进程环境中的 `A2A_OUTBOUND_ALLOW_LOOPBACK`、`A2A_OUTBOUND_ALLOW_HTTP`，均默认 false。

```json
{"allow_loopback": false, "allow_http": false}
```

此设置属于当前个人版 Gateway，不是 Manager 全局网络策略，企业入口返回 FORBIDDEN。出站管理服务未装配时返回 `A2A_OUTBOUND_STORE_INVALID`。

### 4.2 PATCH `/api/v1/a2a/outbound/settings`

Body 两个字段都必须存在且为真正的 JSON boolean，不接受 `"true"`、`1`。虽为 PATCH，当前不是单字段部分更新。

```json
{"allow_loopback": true, "allow_http": false}
```

成功 `data` 同 §4.1 的两字段结构；持久化到个人 `.env`、更新 `os.environ`，并应用到发现与 Dispatcher。false 默认只允许公网 HTTPS；开启 loopback 不等于允许所有私网，HTTP 仍独立受 `allow_http` 限制。参数类型错误为 `A2A_CONFIG_INVALID`（当前 HTTP 500）；持久化/其他处理失败可能被 handler 归一为 `A2A_OUTBOUND_STORE_INVALID`。企业入口返回 FORBIDDEN。

### 4.3 POST `/api/v1/a2a/outbound/discover`

| Body 字段 | 类型 | 必填/默认 | 行为 |
| --- | --- | --- | --- |
| `url` | string | 必填 | HTTP(S)，最长 2048；拒绝 userinfo、query、fragment |
| `card_path` | string | 可选 | 不传时默认 `/.well-known/agent-card.json`；若 url 带非根路径且未传 card_path，直接按 Card URL 处理 |

```json
{"url": "https://weather.example.com", "card_path": "/.well-known/agent-card.json"}
```

成功 `data`：

```json
{
  "discovery_id": "disc_example",
  "expires_at": "2026-09-17T02:10:00Z",
  "source_url": "https://weather.example.com/",
  "card_path": "/.well-known/agent-card.json",
  "card_fingerprint": "sha256:example",
  "agent": {
    "name": "Weather Agent", "description": "天气服务", "version": "1.0.0",
    "skills": [{"id": "weather", "name": "Weather", "description": "天气查询", "tags": ["weather"]}],
    "compatible_interfaces": [{"protocol_binding": "JSONRPC", "protocol_version": "1.0", "url": "https://weather.example.com/a2a"}]
  },
  "agent_card": {"name": "Weather Agent"},
  "security_requirements": [],
  "warnings": []
}
```

`agent_card` 为解析后的完整规范化 Card；示例只展示 name，不是完整 Card 请求样板。`skills` 每项为 `id/name/description/tags`；`security_requirements` 为 Card 声明；当前 `warnings` 为空数组。Gateway 此 DTO **没有 `card_url` 或顶层 `selected_interface`**，这与 Manager 候选不同。

发现只缓存预览，不注册、不保存凭据、不派发。TTL 默认 600 秒，进程重启后缓存丢失；Card 最大 1 MiB，最多跟随 3 次重定向，每跳及接口地址均执行网络校验。只保留 JSONRPC 兼容接口，并经过 SDK 兼容性检查。错误：`A2A_DISCOVERY_URL_INVALID`、`A2A_DISCOVERY_BLOCKED`、`A2A_CARD_FETCH_FAILED`、`A2A_CARD_INVALID`；企业入口返回 FORBIDDEN。

### 4.4 POST `/api/v1/a2a/outbound/agents`

通过有效候选显式注册，不接受任意 endpoint 替代发现结果。

| Body 字段 | 类型 | 必填/默认 | 行为 |
| --- | --- | --- | --- |
| `discovery_id` | string | 必填 | 同进程未过期候选 |
| `display_name` | string | 可选，默认候选 name | 去首尾空白后必须非空 |
| `credential` | string | 可选，默认无凭据 | 非空存 SecretStore；不存到普通 DTO |
| `enabled` | boolean | 可选，默认 true | 当前代码仅显式 false 表示停用，调用方应使用 boolean |
| `timeouts` | object | 可选 | `connect_seconds` 默认 10；`sync_wait_seconds` 默认 300；均须大于 0 |

```json
{
  "discovery_id": "disc_example", "display_name": "Weather Agent",
  "enabled": true, "credential": "",
  "timeouts": {"connect_seconds": 10, "sync_wait_seconds": 120}
}
```

成功 HTTP 201、`data` 为 §5.1 公共 Agent DTO：新 ID、revision=1、availability=available，默认选第一个兼容接口。成功后消费候选。相同 source_url+card_path 或接口 URL 已注册时拒绝。凭据写入后注册持久化失败会删除新凭据。

错误：`A2A_DISCOVERY_NOT_FOUND`、`A2A_DISCOVERY_EXPIRED`、`A2A_AGENT_ALREADY_REGISTERED`、`A2A_CARD_INVALID`、`A2A_OUTBOUND_STORE_INVALID`；企业入口返回 FORBIDDEN。Gateway 此 Body 不是 Receiver 模板 DTO，也不是 Manager 创建模板 Body。

### 4.5 GET `/api/v1/a2a/outbound/agents`

无 Body，无分页/搜索 query。成功：

```json
{"items": [], "total": 0}
```

每项为 §5.1 DTO。个人版返回当前 Gateway 全部注册项；企业版按可信 `X-Bot-Id` 对应资源解析策略，并按可信 `X-User-Id` 读取当前用户的开关，`total` 是过滤后的目录数量。目录按策略允许集过滤，**不因 Manager/user 停用而隐藏**，因此可展示停用状态；availability 也不会用于该目录过滤。

资源不存在、禁用、过期，Agent 模板缺失/禁用，策略引用不是单个字面 ID，或策略缺失/禁用/非法模式时，授权集合为空，返回 `items=[]/total=0`，不回落全局目录。缺少可信用户身份返回 `A2A_USER_IDENTITY_REQUIRED`；服务不可用/投影非法为 `A2A_OUTBOUND_STORE_INVALID`。

企业授权入口为资源→Agent 模板→`template_ref.a2a_access_policy=["policy-id"]`→策略。allowlist 只允许成员，denylist 为当前已下发目录减成员；Agent 查找和派发的有效集还要求 Manager 与当前用户的启停均为 true，派发时另行执行 availability/凭据/网络校验。企业反向 RPC 的查找、派发使用 Gateway 当前会话的可信用户身份，不采信工具参数中的用户 ID；缺少身份返回 `A2A_USER_IDENTITY_REQUIRED`。

### 4.6 GET `/api/v1/a2a/outbound/agents/{agent_id}`

path `agent_id` 必填；无 Body/query。个人版成功 `data` 为 §5.1 DTO，不包含明文 credential 或 credential_ref。不存在返回 `A2A_AGENT_NOT_REGISTERED`。企业入口返回 FORBIDDEN；企业目录展示应使用列表已有公共字段，不能假定详情路由放行。

显式编辑凭据的 WS `a2a.outbound.edit` 存在，但当前路由表没有对应 HTTP `/edit`，不可当作 HTTP 交付。

### 4.7 PATCH `/api/v1/a2a/outbound/agents/{agent_id}/enabled`

企业需要可信 `X-Bot-Id` 和 `X-User-Id`；Body：

```json
{"user_enabled": false}
```

`user_enabled` 必填且必须为 boolean；不使用 `enabled` 替代。成功 `data` 为带三层启停字段的 §5.1 企业 DTO。仅当目标在当前资源策略允许集内才可修改，不创建模板、不改变 Manager enabled、Card、凭据或超时。

**实际存储范围**：`a2a_outbound_user_state` 按 `(template_id, user_id)` 唯一保存。同一 Gateway 内，用户 A 停用模板只影响 A，用户 B 的开关和后续调用不受影响；同一用户在不同会话或资源中使用同一模板时共用自己的开关。该用户无记录时默认 true，Manager 同步不覆盖个人开关；删除模板会清理该模板所有用户的开关。`effective_enabled=manager_enabled && user_enabled`，其中 `user_enabled` 属于当前用户；该字段不含策略或 availability，目录已按当前资源策略过滤。

**旧数据迁移**：Gateway 初始化企业表时，为旧表添加可空 `user_id`，将模板单列唯一索引替换为 `(template_id, user_id)` 联合唯一索引；迁移可重复执行。原共享开关保留为 `user_id=NULL`，不归属任何用户，也不用于个人开关计算。因此尚无个人记录的用户恢复默认 true，仍受 Manager 启停和资源策略限制。前端切换用户时清空旧目录和开关状态，忽略旧请求响应，等待重连后重新读取。

错误：非 boolean→`A2A_OUTBOUND_STORE_INVALID`；缺少可信用户身份→`A2A_USER_IDENTITY_REQUIRED`；越出策略→`A2A_AGENT_NOT_AUTHORIZED`；未下发对象→`A2A_AGENT_NOT_REGISTERED`。普通个人版 Repository 不实现 `set_user_enabled`，该路径虽注册但返回 `A2A_OUTBOUND_STORE_INVALID`；个人版启停使用 §4.8 的 `enabled`。

### 4.8 PATCH `/api/v1/a2a/outbound/agents/{agent_id}`

个人版只允许下列 Body 字段，未传字段保持原值：

| 字段 | 类型 | 行为 |
| --- | --- | --- |
| `display_name` | string | 去空白后必须非空 |
| `enabled` | boolean | 只有 true 表示启用；调用方应传 boolean |
| `connect_timeout_seconds` | number | 大于 0 |
| `sync_wait_seconds` | number | 大于 0 |
| `credential` | string | 非空替换；省略或空字符串保持旧凭据 |
| `clear_credential` | boolean | true 清除；当前同时有非空 credential 时替换分支优先，不采用 Manager 的互斥规则 |

```json
{"display_name": "Weather Agent", "enabled": false, "sync_wait_seconds": 120}
```

成功 `data` 为 §5.1 DTO。空 Body 可仅更新 updated_at；未知字段被拒绝。当前校验对象是 HTTP 合并后的 params，通用身份/会话补参不在允许集合内，因此个人版调用本接口不要附带不必要的 `X-User-Id/X-Group-Id/X-Bot-Id/X-Gateway-Id/X-Session-Id` 或同名 Body 字段。凭据变更后持久化失败会尝试恢复旧 SecretStore 状态。

错误：`A2A_AGENT_NOT_REGISTERED`、`A2A_OUTBOUND_STORE_INVALID`；企业入口返回 FORBIDDEN。不允许用此接口更换 source_url、接口、Card 或 revision。

### 4.9 POST `/api/v1/a2a/outbound/agents/{agent_id}:refresh`

Body 使用 `{}`；无业务 query。个人版重新获取注册时的 source_url/card_path，无任意 URL 覆盖参数。成功 `data` 为 §5.1 DTO。

- 非关键变化直接应用；指纹不同才将 card_revision 加 1。
- 关键身份变化：保留当前已确认 Card/revision，设置 `availability=review_required` 和 `pending_revision`；阻止后续派发，等待确认。
- 关键项：所选接口 URL、protocol_binding、安全要求、安全方案名称、provider URL、signature protected 值。不是对 Card 任意字段变化都要求人工确认。
- 已有 pending 时，刷新只更新待确认快照，不自动接受。
- 获取失败会写 `unreachable`，Card 无效写 `incompatible`；已有 pending 时仍保持 review_required。**这些失败可返回外层 ok=true/HTTP 200 的 Agent DTO**，应检查 `availability/last_error_code/last_error_summary`。

不存在为 `A2A_AGENT_NOT_REGISTERED`，其他未分类失败为 `A2A_OUTBOUND_STORE_INVALID`；企业入口返回 FORBIDDEN。

### 4.10 POST `/api/v1/a2a/outbound/agents/{agent_id}:confirm-revision`

```json
{"accept": true}
```

`accept` 必填且为 boolean。仅 review_required 且有 pending 时可确认。true 应用最新 pending、revision 加 1、清 pending、恢复 available；false 保留原确认 Card/revision、清 pending、恢复 available。成功 `data` 为 §5.1 DTO。

无待确认版本返回 `A2A_AGENT_REVIEW_REQUIRED`；不存在为 `A2A_AGENT_NOT_REGISTERED`；accept 非 boolean 为 `A2A_OUTBOUND_STORE_INVALID`；企业入口返回 FORBIDDEN。

### 4.11 DELETE `/api/v1/a2a/outbound/agents/{agent_id}`

无 Body/query。个人版成功 HTTP 200：

```json
{"agent_id": "agent_example", "deleted": true}
```

删除注册项及凭据，保留派发历史；不存在/重复删除返回 `A2A_AGENT_NOT_REGISTERED`，不是 Receiver 的幂等删除语义。企业入口返回 FORBIDDEN。

### 4.12 GET `/api/v1/a2a/outbound/dispatches`

唯一业务 query：`limit`，默认 200；handler 先转 int，再夹到 1..200，不是越界即报错。非法整数返回 `A2A_OUTBOUND_STORE_INVALID`。无 offset/page/status/agent_id 过滤契约。

企业必须提供可信 `X-User-Id`。按 `source_user_id` 先过滤、再按 `created_at DESC` 截取；`total` 是该用户全部可见记录数量，可能大于 items 数量。列表不按当前 session/resource 再过滤；旧 NULL owner 记录不会展示。个人版返回全局历史。

```json
{
  "items": [{
    "dispatch_id": "disp_example", "agent_id": "template-example", "agent_name": "Weather Agent",
    "mode": "sync", "status": "completed", "remote_task_id": "remote-task-example",
    "created_at": "2026-09-17T02:00:00Z", "updated_at": "2026-09-17T02:00:01Z",
    "accepted_at": "2026-09-17T02:00:00Z", "finished_at": "2026-09-17T02:00:01Z",
    "error_code": null, "error_summary": null, "source_resource_id": "resource-example"
  }],
  "total": 1
}
```

列表字段即示例中的 13 个字段，nullable 字段见 §5.2；不返回 result、输入正文、摘要哈希、source_user_id 或 source_session_id。`remote_task_id` 仅供展示，后续查询仍必须用本地 dispatch_id。当前没有派生 `duration_ms` 字段，客户端可用时间自行计算。缺少企业用户身份为 `A2A_USER_IDENTITY_REQUIRED`。

### 4.13 GET `/api/v1/a2a/outbound/dispatches/{dispatch_id}`

path 使用本地 dispatch_id。无 Body/query；企业需要 `X-User-Id`、`X-Bot-Id`、原派发 `X-Session-Id`。校验 user/session/resource；任一归属不匹配与不存在均返回 `A2A_DISPATCH_NOT_FOUND`，不泄露他人记录。

**企业详情**成功 `data` 是 Dispatcher 公共查询 DTO：

```json
{
  "ok": true, "dispatch_id": "disp_example", "agent_id": "template-example", "agent_name": "Weather Agent",
  "mode": "sync", "status": "completed", "created_at": "2026-09-17T02:00:00Z",
  "accepted_at": "2026-09-17T02:00:00Z", "finished_at": "2026-09-17T02:00:01Z",
  "result": {}, "error_code": null, "error_summary": null
}
```

结果示例仅展示部分业务内容，result 的实际结构见 §5.2。此 DTO 不暴露 remote_task_id/remote_context_id、归属身份、输入摘要或 updated_at。终态/无远端任务时直接读本地；其他状态可能执行远端 `get_task` 并更新本地记录。轮询间隔内会附带 `retry_after_seconds`；可继续查询的状态附带 `next_action`。远端查询异常可返回 `status=unknown/error_code=A2A_REMOTE_STATUS_UNKNOWN` 的成功外层信封，不一定是 HTTP 错误。

外层 `ok` 表示本次接口调用成功；企业 `data.ok` 只有 accepted/working/completed 为 true，不能混为一谈。

**个人版详情**直接返回持久化派发记录，完整字段见 §5.2，不经过企业 Dispatcher 的归属校验或远端刷新。不要把两种 DTO 混写成一个固定响应。

缺少企业用户身份→`A2A_USER_IDENTITY_REQUIRED`；缺少资源→`A2A_AGENT_NOT_AUTHORIZED`；归属不符/不存在→`A2A_DISPATCH_NOT_FOUND`；服务未装配/数据非法→`A2A_OUTBOUND_STORE_INVALID`。

## 5. Web HTTP 返回字段

### 5.1 公共 Agent DTO

所有公共输出移除 `credential_ref`，仅提供 `has_credential`；不包含明文 credential。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `agent_id`, `display_name` | string | ID/显示名；企业 ID 等于 template_id |
| `source_url`, `card_path`, `card_fingerprint` | string | 发现来源、Card 路径、规范化 Card 指纹 |
| `card_revision` | integer | 已确认版本，至少 1 |
| `agent_card` | object | 已确认 Card，敏感键递归脱敏 |
| `selected_interface` | object | `protocol_binding/protocol_version/url`，均为 string |
| `enabled` | boolean | 个人为注册启用；企业为 manager_enabled && user_enabled |
| `availability` | string | available / unreachable / incompatible / review_required |
| `has_credential` | boolean | 是否存在凭据引用，不证明凭据有效 |
| `connect_timeout_seconds`, `sync_wait_seconds` | number | 两类超时，大于 0 |
| `last_checked_at`, `last_success_at` | string/null | ISO-8601 时间 |
| `last_error_code`, `last_error_summary` | string/null | 最近运行错误 |
| `pending_revision` | object/null | 个人版待确认快照：source_url/card_path/card_fingerprint/agent_card/selected_interface/security_requirements；企业投影不带 Manager pending，通常为 null |
| `created_at`, `updated_at` | string | ISO-8601 时间 |
| `network_policy` | object/null | 个人注册默认 null；企业来自模板 data.network_policy |
| `manager_enabled`, `user_enabled`, `effective_enabled` | boolean | 仅企业输出；user_enabled 为当前用户的个人开关，effective_enabled 为 manager_enabled && user_enabled，不代表策略/网络/availability 已通过 |

企业运行态 DTO 不单独返回 Manager 的 `description/a2a_tags/data/policy_id` 字段；不要把 Receiver DTO 原样列为用户输出字段。

### 5.2 派发记录、状态和结果

个人版详情完整记录字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `dispatch_id`, `agent_id`, `request_message_id`, `source_session_id` | string | 本地派发、Agent、请求消息、来源会话 |
| `agent_revision` | integer | 派发时确认版本快照 |
| `mode`, `status` | string | sync/async；状态见下文 |
| `created_at`, `updated_at` | string | ISO-8601 时间 |
| `agent_name`, `source_resource_id`, `source_user_id` | string/null | 名称快照和来源归属 |
| `remote_task_id`, `remote_context_id` | string/null | 远端标识；不是后续 API path 参数 |
| `accepted_at`, `finished_at`, `last_polled_at` | string/null | 接受、终结、最近远端查询时间 |
| `result` | object/null | 归一化远端结果，敏感键脱敏；可能含 text/artifacts/parts，不能假定始终只有纯文本 |
| `error_code`, `error_summary` | string/null | 派发错误 |
| `input_length` | integer/null | 输入长度 |
| `input_content_type`, `input_digest` | string/null | 输入类型和摘要；不是输入正文 |

企业详情仅返回 §4.13 列出的公共字段；可选 next_action/retry_after_seconds。历史列表仅返回 §4.12 字段子集。

状态枚举：created、submitting、accepted、working、completed、failed、canceled、rejected、input_required、auth_required、unknown、timed_out、dispatch_failed。不可变终态为 completed/failed/canceled/rejected/dispatch_failed；timed_out、unknown 不是确认的远端终态，允许后续查询收敛。

Dispatcher 自动保留清理默认最多 1000 条终态记录、最长 30 天，仅清理终态记录。删除 Agent 模板/注册项保留派发历史。

## 6. Config Receiver：6 个模板/策略写接口

### 6.1 通用身份、Body 和响应

这些接口只管理**当前 Gateway 的企业投影**。配置权威在 Manager，不接受用户 Web 身份头作为管理员授权依据。

- Body 是直接业务 JSON，**没有 `business/sig/enc` 外层信封**；历史 sig/enc/jiuwenclaw_id 不承担验签、解密或鉴权功能。
- `JIUWENSWARM_LINK_MTLS_MODE=enforce` 时由 LinkMTLSConfig 检查 TLS peer 和部署绑定；off/observe 不执行同等请求拒绝。具体证书、绑定和部署配置见 [HTTP-SSE 链路 mTLS 部署配置](HTTP-SSE链路mTLS部署配置.md)。不能仅因路径名为管理接口就认为默认开启 mTLS。
- 链路拒绝为 HTTP 403：`{"ok":false,"error":{"code":"LINK_BINDING_MISMATCH","message":"..."}}`，不是成功业务信封。
- 正常成功均 HTTP 200：`{"code":200,"message":"success","data":...}`，POST 不是 201。
- schema/JSON 校验错误为 HTTP 422 `detail`；服务 ValueError 包含 not found 时为 404，其他为 400 `detail`。其他异常可能返回 500，不存在统一 `A2A_*` 错误信封。
- POST 按业务 ID upsert；PATCH 只保留显式字段，要求对象已存在、有效更新非空；DELETE Body 必须传 `{}`，对不存在对象收敛删除。
- DTO `extra=ignore`；credential 子模型 `extra=forbid`。不要依赖未知业务字段落库。NOT NULL 类型字段可省略于 PATCH，但显式 null 被 schema 拒绝；可空字段 description/a2a_tags/data/credential 可传 null，credential=null 表示保持。
- 写成功后调用 `trigger_runtime_config_update()`；通知不代表真实业务端已经联调验证。

| 方法 | 完整路径 | Body | 成功 data |
| --- | --- | --- | --- |
| POST | `/api/v1/a2a-outbound-templates` | §6.2 完整模板 | `{"template_id":"..."}` |
| PATCH | `/api/v1/a2a-outbound-templates/{template_id}` | §6.3 非空更新 | null |
| DELETE | `/api/v1/a2a-outbound-templates/{template_id}` | `{}` | null |
| POST | `/api/v1/a2a-access-policies` | §6.5 完整策略 | `{"policy_id":"..."}` |
| PATCH | `/api/v1/a2a-access-policies/{template_id}` | §6.6 非空更新；path 值为 policy_id | null |
| DELETE | `/api/v1/a2a-access-policies/{template_id}` | `{}`；path 值为 policy_id | null |

### 6.2 POST `/api/v1/a2a-outbound-templates`

| Body 字段 | 类型 | POST 必填/约束 |
| --- | --- | --- |
| `template_id` | string | 必填，1..100 |
| `template_name` | string | 必填，1..128 |
| `description` | string/null | 可选，最大 512 |
| `a2a_tags` | string[]/null | 可选 |
| `source_url` | string | 必填，HTTP(S) 且有主机，1..2048 |
| `card_path` | string | 必填，1..512，同源绝对路径；拒绝 //、反斜杠、query/fragment、解码后的 .. 路径段 |
| `agent_card` | object | 必填；name 必须非空，Receiver 不重新进行完整 SDK 发现 |
| `card_fingerprint` | string | 必填，1..128 |
| `card_revision` | integer | 必填，>=1 |
| `selected_interface` | object | 必填；protocol_binding/protocol_version/url 非空，url 为 HTTP(S) |
| `connect_timeout_seconds`, `sync_wait_seconds` | number | 必填，>0；无服务端缺省超时 |
| `enabled` | boolean | 必填 |
| `credential` | object | 必填；POST 只能 replace/clear，不能 keep |
| `data` | object/null | 可选；企业网络策略通过 data.network_policy 传递 |
| `updated_at` | ISO-8601 datetime | 必填 |

```json
{
  "template_id": "template-example", "template_name": "Weather Agent",
  "description": "天气服务", "a2a_tags": ["weather"],
  "source_url": "https://weather.example.com/", "card_path": "/.well-known/agent-card.json",
  "agent_card": {"name": "Weather Agent"}, "card_fingerprint": "sha256:example", "card_revision": 1,
  "selected_interface": {"protocol_binding": "JSONRPC", "protocol_version": "1.0", "url": "https://weather.example.com/a2a"},
  "connect_timeout_seconds": 10, "sync_wait_seconds": 120, "enabled": true,
  "credential": {"operation": "clear"},
  "data": {"network_policy": {"allow_http": false, "allow_private_network": false, "allow_public_http": false}},
  "updated_at": "2026-09-17T02:00:00Z"
}
```

成功：`{"code":200,"message":"success","data":{"template_id":"template-example"}}`。相同 ID 更新投影，不生成新 ID；created_at 为 Gateway 创建时间。Manager 本身的发现候选、pending_revision、数据库内部 id 不属于同步 DTO。

凭据操作：

| operation | value | 行为 |
| --- | --- | --- |
| keep | 不得带 value | PATCH 保持原 secret/ref；POST 拒绝 |
| replace | 必须非空 string，最大 4096 | 替换 SecretStore；`ENC:v1:` 前缀明确拒绝 |
| clear | 不得带 value | 删除 secret，投影 credential_ref=null |

仅 replace 会对 Receiver 请求自身 URL 执行网络目标 gate；使用请求 Body `data.network_policy`，缺省严格公网 HTTPS。通过可信代理提供 HTTPS 时只信任 `GATEWAY_CONFIG_FORWARDED_ALLOW_IPS` 中的来源。拦截为 HTTP 400、`detail="A2A credential sync blocked by network access settings"`。不是把 `X-Forwarded-Proto` 任意声明为 https 就可绕过。

企业网络策略：默认只允许全部 DNS 地址为公网的 HTTPS；`allow_private_network=true` 才可允许全部为 RFC1918 IPv4；HTTP 还需 allow_http，公网 HTTP 额外需 allow_public_http。loopback、link-local、混合解析等不放行；企业网络策略没有 loopback 开关。真实派发时重新执行目标校验，不因同步成功自动跳过。

Receiver 普通投影只保留 credential_ref；agent_card/selected_interface/data 敏感键递归脱敏。凭据写入后投影落库失败会尝试恢复旧凭据，不能把 HTTP 成功当作部署下所有事务/外部 secret 服务均已验证。

### 6.3 PATCH `/api/v1/a2a-outbound-templates/{template_id}`

path 为原 template_id。可更新字段为 §6.2 除 template_id 外的字段，均可省略；未传保持。不要将 template_id 放 Body 试图改 ID。更新为空/仅含被忽略字段返回 400；对象不存在返回 404。

```json
{"enabled": false, "credential": {"operation": "keep"}, "updated_at": "2026-09-17T02:05:00Z"}
```

成功：`{"code":200,"message":"success","data":null}`。不传 updated_at 时服务写当前 UTC 时间。credential 省略/null/keep 均保持旧凭据。Manager 同步客户端可发送完整可更新快照，但 Receiver 并不要求所有 PATCH 字段都必填。

### 6.4 DELETE `/api/v1/a2a-outbound-templates/{template_id}`

Body `{}`。成功：`{"code":200,"message":"success","data":null}`；已不存在仍返回成功。删除模板投影、SecretStore 凭据、该模板所有用户（含旧的无归属记录）的 user_state 及共享 runtime_state；派发历史保留，不伪造远端 canceled。删除失败会尝试恢复旧投影、本地状态和凭据。该语义与 §4.11 个人注册项删除不同。

### 6.5 POST `/api/v1/a2a-access-policies`

| Body 字段 | 类型 | POST 必填/默认/约束 |
| --- | --- | --- |
| `policy_id` | string | 必填，1..100 |
| `policy_name` | string | 必填，1..128 |
| `description` | string/null | 可选，最大 512 |
| `mode` | string | 必填，allowlist/denylist |
| `member_template_ids` | string[] | 可选，默认 []；成员 1..100，保持顺序去重 |
| `enabled` | boolean | 必填 |
| `revision` | integer | 必填，>=1 |
| `data` | object/null | 可选 |
| `updated_at` | ISO-8601 datetime | 必填 |

```json
{
  "policy_id": "policy-example", "policy_name": "Weather allowlist", "description": "允许天气服务",
  "mode": "allowlist", "member_template_ids": ["template-example"],
  "enabled": true, "revision": 1, "data": {}, "updated_at": "2026-09-17T02:00:00Z"
}
```

成功：`{"code":200,"message":"success","data":{"policy_id":"policy-example"}}`。同 ID upsert；Receiver 不自动加 revision，也不做 Manager 的成员存在性/引用计数校验。成员不存在不会凭空生成 Agent，授权计算只与当前投影目录相交。

空 allowlist 拒绝全部；空 denylist 允许当前投影目录进入策略允许集。策略缺失、禁用或 Agent 模板未绑定单个字面策略 ID 均 fail-closed。绑定结构是 `{"template_ref":{"a2a_access_policy":["policy-example"]}}`，不是字符串/多策略/表达式。

### 6.6 PATCH `/api/v1/a2a-access-policies/{template_id}`

path 值为 policy_id。Body 允许 §6.5 除 policy_id 外的字段，均可省略，更新必须非空；NOT NULL 字段不能传 null。例：

```json
{"member_template_ids": ["template-example"], "revision": 2, "updated_at": "2026-09-17T02:05:00Z"}
```

成功：`{"code":200,"message":"success","data":null}`。对象不存在 404；空更新 400；schema 422。不自动递增 revision；不传 updated_at 时写当前 UTC 时间。

### 6.7 DELETE `/api/v1/a2a-access-policies/{template_id}`

path 值为 policy_id，Body `{}`。成功：`{"code":200,"message":"success","data":null}`；已不存在也成功。仅删除该 Gateway 的策略投影，不删除 Agent 模板或出站 Agent。仍引用已删除策略的资源在后续授权解析时得到空允许集；Receiver 不采用 Manager 管理 API 的引用阻断规则。

## 7. Manager 能力映射

Manager 是另一服务；同路径在 Manager 存在不代表 Gateway 也注册。下表只解释能力关系，不新增接口契约。

| Manager 管理接口 | Gateway 已有入口 | 已确认的差异 |
| --- | --- | --- |
| GET/PUT `/api/v1/a2a-discovery-settings` | Web GET/PATCH `/api/v1/a2a/outbound/settings` | Manager 全局 allow_http/allow_private_network/allow_public_http；Gateway 个人设置 allow_loopback/allow_http；字段、作用域和企业权限不等价 |
| POST `/api/v1/a2a-outbound-discoveries` | Web POST `/api/v1/a2a/outbound/discover` | 都预览候选，但服务/缓存/返回 DTO 不同；企业发现必须走 Manager |
| POST `/api/v1/a2a-outbound-templates` | Web POST `/api/v1/a2a/outbound/agents`；Receiver POST `/api/v1/a2a-outbound-templates` | 分别为 Manager 候选注册、个人运行态注册、企业确认快照投影；Body 和凭据结构不同 |
| POST `/api/v1/a2a-outbound-templates/{template_id}:refresh`、`:confirm-revision` | Web 同动作但资源为 `/a2a/outbound/agents/{agent_id}` | 企业用户 Web 动作拒绝；Manager 确认快照再下发；不能用个人动作替代模板管理 |
## 8. Web A2A 错误码速查

Web 层错误使用 §2.2 信封，当前领域码通常 HTTP 500。Receiver 使用 §6.1 的 detail/链路响应，不能套用此表。

| 分类 | 已实现错误码 | 主要用途 |
| --- | --- | --- |
| 发现 | A2A_DISCOVERY_URL_INVALID、A2A_DISCOVERY_BLOCKED、A2A_CARD_FETCH_FAILED、A2A_CARD_INVALID | URL/网络/Card 获取与兼容性 |
| 候选/注册 | A2A_DISCOVERY_NOT_FOUND、A2A_DISCOVERY_EXPIRED、A2A_AGENT_ALREADY_REGISTERED、A2A_AGENT_NOT_REGISTERED | 候选生命周期/重复/对象不存在 |
| 启停/授权 | A2A_AGENT_NOT_AUTHORIZED、A2A_USER_IDENTITY_REQUIRED | 企业资源授权，以及目录、启停、历史的可信用户身份检查 |
| 版本/输入 | A2A_AGENT_REVIEW_REQUIRED、A2A_OUTBOUND_STORE_INVALID、A2A_CONFIG_INVALID | 无 pending、数据/参数/设置错误 |
| 派发查询 | A2A_DISPATCH_NOT_FOUND、A2A_REMOTE_STATUS_UNKNOWN | 归属不符/不存在；后者也可能位于成功 data 中 |
| 企业 gate | FORBIDDEN | 该用户入口不允许管理操作；HTTP 403 |

其他领域码（停用、认证、超时、繁忙等）可作为派发历史 error_code/result 中的信息：A2A_AGENT_DISABLED、A2A_AGENT_UNAVAILABLE、A2A_AGENT_USER_DISABLED、A2A_AGENT_MANAGER_DISABLED、A2A_AUTH_REQUIRED、A2A_AUTH_SCHEME_MISSING、A2A_DISPATCH_REJECTED、A2A_DISPATCH_TIMEOUT、A2A_DISPATCH_CONFLICT、A2A_OUTBOUND_BUSY、A2A_OUTBOUND_TASK_INVALID、A2A_OUTBOUND_MODE_INVALID、A2A_OUTBOUND_MANAGER_UNAVAILABLE。它们不代表每个 Web 路由都直接产生这些错误，也不代表存在对应 HTTP 派发方法。

## 9. 联调示例与验证范围

企业用户请求示例（基础地址和身份由部署/BFF 提供）：

```bash
curl "${WEB_HTTP_BASE}/api/v1/a2a/outbound/agents" \
  -H "X-User-Id: user-example" -H "X-Bot-Id: resource-example"

curl -X PATCH "${WEB_HTTP_BASE}/api/v1/a2a/outbound/agents/template-example/enabled" \
  -H "Content-Type: application/json" -H "X-User-Id: user-example" -H "X-Bot-Id: resource-example" \
  --data '{"user_enabled":false}'

curl "${WEB_HTTP_BASE}/api/v1/a2a/outbound/dispatches?limit=200" -H "X-User-Id: user-example"

curl "${WEB_HTTP_BASE}/api/v1/a2a/outbound/dispatches/disp_example" \
  -H "X-User-Id: user-example" -H "X-Bot-Id: resource-example" -H "X-Session-Id: original-session-id"
```

这些是 Bash/curl 示例，身份示意值不应直接用于生产。Receiver 写请求应由 Manager 配置推送客户端携带真实部署证书/绑定发送；不要通过用户身份头伪造管理链路。

测试应分别检查：

1. 公共路由 13 项均注册，但企业 gate 只允许 4 项；ingress 不挂载。
2. 发现候选与注册分离、TTL、重复注册、Card/网络安全边界；普通输出不泄露 secret。
3. refresh 失败可能 HTTP 成功但 availability 失败；关键变化保持原确认版本；接受/拒绝版本语义。
4. 目录按资源策略过滤；用户启停按 `(template_id, user_id)` 隔离，策略/Manager 停用不能被用户提升。
5. 历史先按用户过滤再截取/count；详情还校验原会话和资源；企业/个人 DTO 不同。
6. Receiver POST/PATCH/DELETE、exclude_unset、凭据 keep/replace/clear、链路/网络 gate、回滚与历史保留。

现有测试索引：`tests/unit_tests/test_web_http.py`、`tests/unit_tests/gateway/test_enterprise_a2a_web_gate.py`、`test_a2a_manager_web_handlers.py`、`test_a2a_outbound_management.py`、`test_a2a_outbound_dispatcher.py`、`test_a2a_outbound_repository.py`、`test_manager_config_receiver_a2a.py`。源码/单测核验不代替匹配部署版本的真实请求、mTLS、SecretStore 与重启恢复验证。

## 10. 实现索引

路径相对仓库根目录，冲突时以此基线实际实现为准：

- HTTP 路由：`jiuwenswarm/gateway/channel_manager/web/web_http_routes.py`（`_A2A_OUTBOUND_ROUTES`、`mapped_routes_for_edition`）。
- 企业 gate：`jiuwenswarm/gateway/channel_manager/web/invoke.py`（`_ENTERPRISE_A2A_ALLOWED`、`dispatch_web_request`）。
- Body/header 合并与 HTTP 信封：`jiuwenswarm/gateway/channel_manager/web/web_http_app.py`（`_params_from_mapped_route`、`_envelope_from_res`、`_unary`）。
- 入口身份：`jiuwenswarm/gateway/channel_manager/web/web_http_dispatch.py`、`outbound.py`、`web_rpc_host.py`。
- Web handler：`jiuwenswarm/gateway/channel_manager/web/app_web_handlers.py`（`_a2a_outbound_*`）。
- 门面/设置：`jiuwenswarm/gateway/a2a_manager/manager.py`、`config.py`。
- 发现/注册/DTO/派发：`jiuwenswarm/gateway/a2a_manager/outbound/discovery.py`、`registry.py`、`models.py`、`dispatcher.py`、`repository.py`、`errors.py`。
- 企业投影/授权：`jiuwenswarm/gateway/a2a_manager/outbound/enterprise.py`；A2A 状态表定义 `jiuwenswarm/gateway/config/enterprise/tables/a2a_models.py`，旧表迁移在同目录 `a2a_migration.py`。
- Receiver 前缀/链路：`packages/jiuwenclaw-ee/gateway/extensions/manager_config_receiver/http/app.py`、`infrastructure/config.py`。
- Receiver 路由/DTO：`packages/jiuwenclaw-ee/gateway/extensions/manager_config_receiver/routers/template_routers.py`、`routers/deps.py`、`schemas/template_schemas.py`、`schemas/sync_schemas.py`。
- Receiver 服务：`packages/jiuwenclaw-ee/gateway/extensions/manager_config_receiver/core/template/a2a_outbound_template.py`、`a2a_access_policy_template.py`。
- Manager 对照：`agent-runtime/applications/manager/manager_server/src/manager_server/routers/template_routers.py`、`schemas/template_schemas.py`。

相关文档：[Gateway Web HTTP 通用说明](Gateway%20Http接口文档.md)、[Manager→Gateway Config Receiver 通用说明](../../jiuwenswarm/gateway/docs/Gateway对接管理面接口文档.md)、[A2A 接入说明](A2A.md)。
