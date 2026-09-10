# Gateway中和Manager交互的接口文档

> 范围：Manager（`applications/manager`）通过 HTTP 调用 Gateway **Config Receiver**（`jiuwenswarm/.../manager_config_receiver`）的全部接口。  


---

## 1. 总览

| 项 | 说明 |
|----|------|
| Gateway 模块 | `packages/jiuwenclaw-ee/gateway/extensions/manager_config_receiver` |
| Base URL | 实例的 `gateway_config_host`（如 `http://gateway:8080`） |
| 路径前缀 | `/api/v1` |
| Manager 客户端 | `manager_server.manager_config_push.client.gateway_request` |
| 探活客户端 | `manager_server.core.instance.config_host_probe`（直连 GET） |

**交互原则**

- 配置权威在 Manager；Gateway 侧为下发副本（GDB）。
- 多数写操作：**先推 Gateway 成功，再写 Manager DB**。

---

## 2. 公共约定

### 2.1 请求 Body

写接口 Body 即为业务 JSON 字段。探活接口无 Body。

DELETE 且无业务字段时，Body 为 `{}`。

PATCH 更新须至少包含一个可更新业务字段；空 Body / 无可更新字段会返回 400。

### 2.2 统一响应

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | 业务码，成功为 `200` |
| `message` | string | 文案，成功为 `success` |
| `data` | object \| null | 业务结果；更新/删除多为 `null`；创建类接口多为 `{ "template_id": "..." }` / `{ "resource_id": "..." }` / `{ "rule_id": "..." }` 等 |

| HTTP | 含义 |
|------|------|
| 200 | 成功 |
| 400 | 业务校验失败（`detail`） |
| 404 | 资源不存在 |

### 2.3 Manager 调用形态

```text
POST/PUT/PATCH/DELETE  {gateway_config_host}{path}
Content-Type: application/json
Body = { **business } 或 {}
```

---

## 3. 探活 / 系统

无落库表。

### 3.1 健康检查

- **接口名称**：健康检查
- **请求方法**：`GET`
- **请求路径**：`/api/health`
- **请求参数**：无
- **返回参数**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `ok` |

- **请求示例**：无 Body
- **返回示例**：

```json
{ "status": "ok" }
```

### 3.2 就绪检查

- **接口名称**：就绪检查
- **请求方法**：`GET`
- **请求路径**：`/api/v1/ready`
- **请求参数**：无
- **返回参数**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `ready` |

- **请求示例**：无 Body
- **返回示例**：

```json
{ "status": "ready" }
```

说明：供 K8s `readinessProbe`（`gateway.template.yaml` → `config-http`）使用；当前不做依赖探测，固定返回 ready。

---

## 4. 模型模板（`model_template` / `model-templates`）

### 4.0 表结构（`model_template`）

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | BIGINT 自增 PK | 是 | 数据库主键；插入时可省略。 |
| `template_id` | VARCHAR(100) UNIQUE | 是 | 对外稳定标识（通常 UUID v4）；API 路径与引用均用本字段。 |
| `template_name` | VARCHAR(128) | 是 | 用户自定义模板名称。 |
| `description` | VARCHAR(512) | 否 | 模型描述。 |
| `model_type` | JSON | 是 | 模型类型列表；允许值仅 `default` / `video` / `audio` / `vision`；创建未传时落库为 `[]`。 |
| `model_tags` | JSON | 否 | 标签，如 `["chat","vision"]`。 |
| `api_base` | VARCHAR(512) | 是 | 模型 API 基地址。 |
| `api_key` | VARCHAR(4096) | 是 | API 密钥。 |
| `model_id` | VARCHAR(128) | 是 | 上游模型标识（如 `gpt-4o-mini`）。 |
| `model_provider` | VARCHAR(64) | 是 | 提供商标识，如 `openai`。 |
| `parameters` | JSON | 否 | 推理参数，如 `temperature`、`max_tokens`。 |
| `timeout` | INT DEFAULT 60 | 否 | 单次请求超时（秒）。 |
| `retry_count` | INT DEFAULT 3 | 否 | 失败重试次数。 |
| `enable_streaming` | BOOLEAN DEFAULT true | 否 | 是否启用流式输出。 |
| `enable_function_calling` | BOOLEAN DEFAULT true | 否 | 是否启用函数调用。 |
| `verify_ssl` | BOOLEAN DEFAULT false | 否 | 是否校验 HTTPS 证书。 |
| `enabled` | BOOLEAN DEFAULT true | 是 | 是否启用。 |
| `data` | JSON | 否 | 扩展字段。 |
| `created_at` | DATETIME(3) | 是 | 创建时间。 |
| `updated_at` | DATETIME(3) | 是 | 更新时间。 |

**Manager 触发**：Agent 资源 `template_ref` 引用 / 全量 bootstrap / 模型模板变更推送（`push_template_to_gateway`）。

### 4.1 创建 / Upsert 模型模板

- **接口名称**：创建模型模板
- **请求方法**：`POST`
- **请求路径**：`/api/v1/model-templates`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `template_id` | string | 是 | 业务 ID |
| `template_name` | string | 是 | 名称 |
| `api_base` | string | 是 | API 基地址 |
| `api_key` | string | 是 | API 密钥 |
| `model_id` | string | 是 | 上游模型 ID |
| `model_provider` | string | 是 | 提供商 |
| `description` | string | 否 | 描述 |
| `model_type` | string[] | 否 | 类型列表；仅允许 `default` / `video` / `audio` / `vision`；省略时落库 `[]` |
| `model_tags` | string[] | 否 | 标签 |
| `parameters` | object | 否 | 推理参数 |
| `timeout` | int | 否 | 超时秒；默认 `60`（`>=1`） |
| `retry_count` | int | 否 | 重试次数；默认 `3`（`>=0`） |
| `enable_streaming` | bool | 否 | 流式；默认 `true` |
| `enable_function_calling` | bool | 否 | 函数调用；默认 `true` |
| `verify_ssl` | bool | 否 | SSL 校验；默认 `false` |
| `enabled` | bool | 否 | 启用；默认 `true` |
| `data` | object | 否 | 扩展 |

- **返回参数**：

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `data.template_id` | string | 写入的 `template_id` |

- **请求示例**（演示数据 M1「兜底-经济型」）：

```json
{
  "template_id": "a1000001-0000-4000-8000-0000000000m1",
  "template_name": "兜底-经济型",
  "description": "Fallback Agent 使用",
  "model_type": ["default"],
  "model_tags": ["chat"],
  "api_base": "https://api.openai.com/v1",
  "api_key": "sk-demo-global",
  "model_id": "gpt-4o-mini",
  "model_provider": "openai",
  "parameters": { "temperature": 0.7, "max_tokens": 4096 },
  "enabled": true,
  "data": {}
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "template_id": "a1000001-0000-4000-8000-0000000000m1"
  }
}
```

### 4.2 更新模型模板

- **接口名称**：更新模型模板
- **请求方法**：`PATCH`
- **请求路径**：`/api/v1/model-templates/{template_id}`
- **路径参数**：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `template_id` | string | 是 | 要更新的模板 ID |

- **请求参数**（Body）：业务字段均可选（partial）。
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{
  "template_name": "兜底-经济型",
  "parameters": { "temperature": 0.5, "max_tokens": 2048 },
  "enabled": true
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### 4.3 删除模型模板

- **接口名称**：删除模型模板
- **请求方法**：`DELETE`
- **请求路径**：`/api/v1/model-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|

- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

---

## 5. Embedding 模板（`embedding_template` / `embedding-templates`）

### 5.0 表结构（`embedding_template`）

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | BIGINT 自增 PK | 是 | 数据库主键。 |
| `template_id` | VARCHAR(100) UNIQUE | 是 | 对外稳定标识。 |
| `template_name` | VARCHAR(128) | 是 | 模板名称。 |
| `description` | VARCHAR(512) | 否 | 用途说明。 |
| `embed_tags` | JSON | 否 | 标签，如 `["memory","fallback"]`。 |
| `api_base` | VARCHAR(512) | 是 | Embedding API 基地址。 |
| `api_key` | VARCHAR(4096) | 是 | API 密钥。 |
| `model_id` | VARCHAR(128) | 是 | 上游 Embedding 模型标识。 |
| `model_provider` | VARCHAR(64) | 是 | 提供商标识。 |
| `parameters` | JSON | 否 | 对齐 OpenAI Embeddings 可选参数（如 `encoding_format`、`dimensions`）。 |
| `client_config` | JSON | 否 | HTTP 客户端参数（如 `timeout`、`retry_count`、`verify_ssl`）。 |
| `enabled` | BOOLEAN DEFAULT true | 是 | 是否启用。 |
| `data` | JSON | 否 | 扩展字段。 |
| `created_at` | DATETIME(3) | 是 | 创建时间。 |
| `updated_at` | DATETIME(3) | 是 | 更新时间。 |

### 5.1 创建 / Upsert Embedding 模板

- **接口名称**：创建 Embedding 模板
- **请求方法**：`POST`
- **请求路径**：`/api/v1/embedding-templates`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `template_id` | string | 是 | 业务 ID |
| `template_name` | string | 是 | 名称（亦接受别名 `name`） |
| `api_base` | string | 是 | API 基地址 |
| `api_key` | string | 是 | API 密钥 |
| `model_id` | string | 是 | 模型 ID |
| `model_provider` | string | 是 | 提供商 |
| `description` | string | 否 | 描述 |
| `embed_tags` | string[] | 否 | 标签 |
| `parameters` | object | 否 | Embeddings 可选参数 |
| `client_config` | object | 否 | HTTP 客户端配置 |
| `enabled` | bool | 否 | 启用 |
| `data` | object | 否 | 扩展 |

- **返回参数**：`data.template_id`
- **请求示例**（演示数据 B1「兜底向量模型」）：

```json
{
  "template_id": "b1000001-0000-4000-8000-0000000000b1",
  "template_name": "兜底向量模型",
  "description": "Fallback Agent 记忆检索",
  "embed_tags": ["memory", "fallback"],
  "api_base": "https://api.openai.com/v1",
  "api_key": "sk-demo-embed-global",
  "model_id": "text-embedding-3-small",
  "model_provider": "openai",
  "parameters": { "encoding_format": "float" },
  "client_config": { "timeout": 60, "retry_count": 3, "verify_ssl": true },
  "enabled": true,
  "data": { "demo": "b1" }
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": { "template_id": "b1000001-0000-4000-8000-0000000000b1" }
}
```

### 5.2 更新 Embedding 模板

- **接口名称**：更新 Embedding 模板
- **请求方法**：`PATCH`
- **请求路径**：`/api/v1/embedding-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**：可选业务字段（partial）
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{
  "parameters": { "encoding_format": "float", "dimensions": 1536 },
  "enabled": true
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### 5.3 删除 Embedding 模板

- **接口名称**：删除 Embedding 模板
- **请求方法**：`DELETE`
- **请求路径**：`/api/v1/embedding-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**：`{}`
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

---

## 6. 扩展配置模板（`extension_config_template` / `extension-config-templates`）

### 6.0 表结构（`extension_config_template`）

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | BIGINT 自增 PK | 是 | 数据库主键。 |
| `template_id` | VARCHAR(100) UNIQUE | 是 | 对外稳定标识。 |
| `template_name` | VARCHAR(128) | 是 | 模板名称。 |
| `description` | VARCHAR(512) | 否 | 用途说明。 |
| `component` | VARCHAR(32) | 是 | 下发目标：`gateway` / `agent_server`。 |
| `hook_type` | VARCHAR(32) | 是 | `pre_request` / `post_request` / `error` / `schedule`。 |
| `hook_config` | JSON | 是 | 钩子配置，见下表。 |
| `custom_config` | JSON | 否 | 自定义配置；默认 `{}`。 |
| `enabled` | BOOLEAN DEFAULT true | 是 | 是否启用。 |
| `data` | JSON | 否 | 扩展字段。 |
| `created_at` | DATETIME(3) | 是 | 创建时间。 |
| `updated_at` | DATETIME(3) | 是 | 更新时间。 |

**`hook_config` 字段说明**：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `handler` | string | 是 | 钩子实现路径，如 `hooks.auth.pre_request`。 |
| `params` | object | 否 | 静态参数。 |
| `schedule` | string | 否 | 仅 `hook_type=schedule` 时必填；cron（5/6/7 段）。 |
| `data` | object | 否 | 单条钩子扩展。 |

### 6.1 创建 / Upsert 扩展配置模板

- **接口名称**：创建扩展配置模板
- **请求方法**：`POST`
- **请求路径**：`/api/v1/extension-config-templates`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `template_id` | string | 是 | 业务 ID |
| `template_name` | string | 是 | 名称 |
| `component` | string | 是 | 目标组件；仅 `gateway` / `agent_server` |
| `hook_type` | string | 是 | 钩子类型；仅 `pre_request` / `post_request` / `error` / `schedule` |
| `description` | string | 否 | 描述 |
| `hook_config` | object | 是 | 钩子配置（创建落库必填；`hook_type=schedule` 时须含合法 `schedule` cron） |
| `custom_config` | object | 否 | 自定义配置；省略时落库 `{}` |
| `enabled` | bool | 否 | 启用；默认 `true` |
| `data` | object | 否 | 扩展 |

- **返回参数**：`data.template_id`
- **请求示例**（演示数据 E1「Gateway 请求前鉴权」）：

```json
{
  "template_id": "e1000001-0000-4000-8000-0000000000e1",
  "template_name": "Gateway 请求前鉴权",
  "description": "请求前参数校验与权限检查（gateway）",
  "component": "gateway",
  "hook_type": "pre_request",
  "hook_config": {
    "handler": "hooks.auth.pre_request",
    "params": { "require_token": true, "allowed_roles": ["user", "admin"] }
  },
  "custom_config": { "auth_header": "Authorization" },
  "enabled": true,
  "data": { "demo": "e1" }
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": { "template_id": "e1000001-0000-4000-8000-0000000000e1" }
}
```

### 6.2 更新扩展配置模板

- **接口名称**：更新扩展配置模板
- **请求方法**：`PATCH`
- **请求路径**：`/api/v1/extension-config-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**：可选业务字段
- **返回参数**：`data` 为 `null`
- **请求示例**（演示数据 E4 定时清理）：

```json
{
  "hook_type": "schedule",
  "hook_config": {
    "handler": "hooks.maintenance.cleanup",
    "schedule": "0 */5 * * *",
    "params": { "ttl_seconds": 3600 }
  },
  "enabled": true
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### 6.3 删除扩展配置模板

- **接口名称**：删除扩展配置模板
- **请求方法**：`DELETE`
- **请求路径**：`/api/v1/extension-config-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**：`{}`
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

---

## 7. 预置Skill模板（`skill_prebuilt_template` / `skill-prebuilt-templates`）

企业预置技能安装为**双模式**（按字段推断，不另传 `mode`）：

| 模式 | 定位 | 说明 |
|------|------|------|
| `provider`（推荐） | 齐 `source_id` + `skill_id` + `version_id` | AgentServer 走 SPI：`get_artifact` → `download_artifact` → `verify_artifact`。摘要以 Provider `ArtifactDescriptor` 为准，**不读**模板 `data.sha256`。若同时带 `package_url`，URL **不作**安装入口。 |
| `url`（兼容） | 合法 `package_url`（且走不成 provider） | AgentServer `install_skill_sync(package_url, checksum=data.sha256?)`。可选 `data.sha256`（64 位小写 hex）。 |

Gateway **只存期望态**，不下载、不验签、不写 workspace。写接口 `200` 只表示模板已保存；真正安装在 AgentServer reconcile。账本 `source_type=prebuilt`；用户不可卸载。

`source_id` 未在本机 SourceRegistry 绑定 → reconcile `source_not_found`，**不得**因模板另有 URL 而回退到 `url`。两者字段都不满足 → 该项失败（`failed[]`），不得静默跳过。

HMAC 密钥、Bearer token、长期明文下载凭证不得出现在模板或普通日志中。

**Manager 触发**：预置 Skill 模板变更推送（`push_template_to_gateway`，kind=`skill_prebuilt_templates`）/ 全量 bootstrap。须先于引用它的 Agent 模板推送。

### 7.0 表结构（`skill_prebuilt_template`）

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | BIGINT 自增 PK | 是 | 数据库主键。 |
| `template_id` | VARCHAR(100) UNIQUE | 是 | 对外稳定标识；策略 / Agent `template_ref.skill_prebuilt` 引用本字段。 |
| `template_name` | VARCHAR(128) | 是 | 模板名称。 |
| `description` | VARCHAR(512) | 否 | 用途说明。 |
| `skill_id` | VARCHAR(512) | 是 | 远端/本地定位 ID；两种模式均使用。 |
| `package_url` | VARCHAR(2048) | 条件 | **仅 url 路径**：合法 `http(s)://` 且含主机。 |
| `source_id` | VARCHAR(64) | 条件 | **仅 provider 路径**：已绑定来源 ID，建议 `^[a-z0-9][a-z0-9-]{0,63}$`。 |
| `version_id` | VARCHAR(128) | 条件 | **仅 provider 路径**：精确制品版本 ID。 |
| `enabled` | BOOLEAN DEFAULT true | 是 | `false` 时不下发到租户预置清单。 |
| `data` | JSON | 否 | 扩展袋。url 路径可用 `data.sha256`；**禁止**在 `data` 内放 `source_id` / `skill_id` / `version_id` / `package_url`。 |
| `created_at` | DATETIME(3) | 是 | 创建时间。 |
| `updated_at` | DATETIME(3) | 是 | 更新时间。 |

落库校验：合并后必须能推断 provider 或 url 之一。`data.sha256` 若提供须为 64 位小写 hex。

### 7.1 创建 / Upsert 预置Skill模板

- **接口名称**：创建预置Skill模板
- **请求方法**：`POST`
- **请求路径**：`/api/v1/skill-prebuilt-templates`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `template_id` | string | 是 | 业务 ID，≤100 |
| `template_name` | string | 是 | 名称，≤128 |
| `skill_id` | string | 是 | Skill ID，≤512 |
| `source_id` | string | 条件 | provider：来源 ID，≤64 |
| `version_id` | string | 条件 | provider：制品版本 ID，≤128 |
| `package_url` | string | 条件 | url：合法 http(s) URL，≤2048 |
| `description` | string | 否 | 描述，≤512 |
| `enabled` | bool | 否 | 启用；默认 `true` |
| `data` | object | 否 | 扩展；url 可用 `sha256` |

- **返回参数**：`data.template_id`

- **请求示例（provider）**：

```json
{
  "template_id": "sp100001-0000-4000-8000-00000000pdf",
  "template_name": "预置 PDF 解析",
  "description": "销售组强制预置",
  "enabled": true,
  "skill_id": "skill_pdf_parser",
  "source_id": "customer-hub",
  "version_id": "ver-abc123"
}
```

- **请求示例（url）**：

```json
{
  "template_id": "sp100001-0000-4000-8000-0000000wthr",
  "template_name": "天气 Skill（URL）",
  "enabled": true,
  "skill_id": "search/weather",
  "package_url": "https://artifacts.example.com/skills/weather-1.0.0.zip",
  "data": {
    "sha256": "f3a1b2c4d5e6f7890abcdef1234567890abcdef1234567890abcdef123456789"
  }
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": { "template_id": "sp100001-0000-4000-8000-00000000pdf" }
}
```

无法推断安装路径时 HTTP `400`，`detail` 形如 `invalid_template: cannot infer install path from fields`。provider 缺 `source_id` / `version_id`，或 url 的 `package_url` 非法，同为 400。

### 7.2 更新预置Skill模板

- **接口名称**：更新预置Skill模板
- **请求方法**：`PATCH`
- **请求路径**：`/api/v1/skill-prebuilt-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**：可选业务字段。Gateway 与库内行**合并后再校验**；合并后必须仍能推断 provider 或 url，否则 400，不得写入残缺期望态。
  - 更换 SPI 制品版本：至少更新 `version_id`。
  - 从 url 切到 provider：带齐 `source_id` + `version_id`。
  - 从 provider 切回 url：提供合法 `package_url`，并清空 `source_id` / `version_id`。
- **返回参数**：`data` 为 `null`
- **请求示例**（更换 SPI 版本）：

```json
{
  "version_id": "ver-def456"
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

`template_id` 不存在 → HTTP `404`。

### 7.3 删除预置Skill模板

- **接口名称**：删除预置Skill模板
- **请求方法**：`DELETE`
- **请求路径**：`/api/v1/skill-prebuilt-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**：`{}`
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

删除后，依赖该模板的策略下次生效不再下发该项；AgentServer reconcile **只移除不再期望的 prebuilt**，不删 user 技能。

---

## 8. 安全护栏模板（`permissions_template` / `permissions-templates`）

判定只在 **AgentServer**。Gateway 只存期望态：写接口 `200` 表示模板已落库，不代表节点已按该策略拦截。

未绑定、模板行 `enabled=false`、或 `body` 无效时，运行时回落 Gateway 本地 `config.yaml::permissions`。

`template_ref.permissions` 为**单值槽位**：绑了多条时，AgentServer 只取第一个**启用**模板的 `body`。

**Manager 触发**：安全护栏模板变更推送（`push_template_to_gateway`，kind=`permissions_templates`）/ 全量 bootstrap。须先于引用它的 Agent 模板推送。

### 8.0 表结构（`permissions_template`）

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | BIGINT 自增 PK | 是 | 数据库主键。 |
| `template_id` | VARCHAR(100) UNIQUE | 是 | 对外稳定标识；策略 / Agent `template_ref.permissions` 引用本字段。 |
| `template_name` | VARCHAR(128) | 是 | 模板名称。 |
| `description` | VARCHAR(512) | 否 | 说明，不参与判定。 |
| `enabled` | BOOLEAN DEFAULT true | 是 | 模板行开关；`false` 时不下发，等价未引用。 |
| `body` | JSON | 是 | 完整权限策略，结构与 `config.yaml::permissions` 一致；见下表。 |
| `data` | JSON | 否 | 扩展袋，运行时不参与判定。 |
| `created_at` | DATETIME(3) | 是 | 创建时间。 |
| `updated_at` | DATETIME(3) | 是 | 更新时间。 |

两个开关不要混：行上 `enabled` 控制是否下发；`body.enabled` 是权限引擎总开关（`false` 时工具调用基本放行，内置高危规则仍可能 DENY）。

**`body` 字段**（Gateway **不**校验内部语义，只要求是 JSON object；无效 body 运行时回落 yaml）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `enabled` | bool | 否 | 权限引擎总开关。Manager 新建多为 `true`。 |
| `permission_mode` | string | 否 | `normal`（默认）或 `strict`；把 `rules[].severity` 映射为 allow / ask / deny。 |
| `defaults` | object | 否 | 未列明工具的默认动作；UI 通常只编 `defaults["*"]`（`allow` / `ask` / `deny`）。无 `defaults` 时引擎回落 `ask`。 |
| `tools` | object | 否 | 键为工具名，值为 `allow` / `ask` / `deny`。`deny` 立即拒绝，不再看 rules。 |
| `rules` | array | 否 | 参数级规则，见 **rule 对象**。 |
| `file_guard` | object | 否 | 路径防护，见 **file_guard 对象**。 |

**`rule` 对象**：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | string | 否 | 规则 ID。 |
| `tools` | string[] | 是 | 适用工具；须同一类（shell / path），空或跨类则运行时跳过该条。 |
| `pattern` | string | 是 | 命令模式。通配如 `ls *`；`re:` 开头为正则。 |
| `severity` | string | 否 | `LOW` / `MEDIUM` / `HIGH` / `CRITICAL`，经 `permission_mode` 映射。 |
| `action` | string | 否 | 显式 `allow` / `ask` / `deny`；有则优先于 severity 映射。 |
| `description` | string | 否 | 说明。 |

**`file_guard` 对象**：

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `enabled` | bool | `false` 时整层路径防护关，不影响 tools/rules。 |
| `defaults` | object | 未命中 workspace / paths 时的 `read` / `write` / `exec`（`allow` / `ask` / `deny`）。 |
| `workspace` | object | Agent workspace **内**三轴；配置里不写死路径。 |
| `paths` | array | 额外路径规则：`path`、`match`（`prefix` 缺省 / `glob`）、以及该路径三轴。 |

`permission_mode` 映射：`normal` 下 LOW/MEDIUM→allow、HIGH/CRITICAL→ask；`strict` 下 MEDIUM→ask、CRITICAL→deny。未知 severity 按 HIGH。

### 8.1 创建 / Upsert 安全护栏模板

- **接口名称**：创建安全护栏模板
- **请求方法**：`POST`
- **请求路径**：`/api/v1/permissions-templates`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `template_id` | string | 是 | 业务 ID，≤100 |
| `template_name` | string | 是 | 名称，≤128 |
| `body` | object | 是 | 完整 permissions 段；缺省或非 object → 400 |
| `description` | string | 否 | 描述，≤512 |
| `enabled` | bool | 否 | 模板行开关；默认 `true` |
| `data` | object | 否 | 扩展 |

- **返回参数**：`data.template_id`
- **请求示例**：

```json
{
  "template_id": "p1000001-0000-4000-8000-00000000perm",
  "template_name": "默认安全护栏",
  "description": "销售组工具权限",
  "enabled": true,
  "body": {
    "enabled": true,
    "permission_mode": "normal",
    "defaults": { "*": "allow" },
    "tools": {
      "bash": "ask",
      "write_file": "allow"
    },
    "rules": [
      {
        "id": "shell_allow_ls",
        "tools": ["bash", "mcp_exec_command", "create_terminal"],
        "pattern": "ls *",
        "severity": "LOW"
      }
    ],
    "file_guard": {
      "enabled": true,
      "defaults": { "read": "ask", "write": "ask", "exec": "ask" },
      "workspace": { "read": "allow", "write": "allow", "exec": "allow" },
      "paths": []
    }
  }
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": { "template_id": "p1000001-0000-4000-8000-00000000perm" }
}
```

创建缺 `body` 时 HTTP `400`，`detail` 为 `permissions_templates.create requires body object`。

### 8.2 更新安全护栏模板

- **接口名称**：更新安全护栏模板
- **请求方法**：`PATCH`
- **请求路径**：`/api/v1/permissions-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**：可选业务字段（`template_name` / `description` / `enabled` / `body` / `data`）；至少一项。未传 `body` 则保留库内原 body。
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{
  "enabled": true,
  "body": {
    "enabled": true,
    "permission_mode": "strict",
    "defaults": { "*": "ask" },
    "tools": { "bash": "ask" },
    "rules": [],
    "file_guard": {
      "enabled": true,
      "defaults": { "read": "ask", "write": "ask", "exec": "ask" },
      "workspace": { "read": "allow", "write": "allow", "exec": "allow" },
      "paths": []
    }
  }
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

`template_id` 不存在 → HTTP `404`。

### 8.3 删除安全护栏模板

- **接口名称**：删除安全护栏模板
- **请求方法**：`DELETE`
- **请求路径**：`/api/v1/permissions-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**：`{}`
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

删除后，依赖该模板的策略下次生效不再下发该项。

---

## 9. Agent 模板（`agent_template` / `agent-templates`）

### 9.0 表结构（`agent_template`）

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | BIGINT 自增 PK | 是 | 数据库主键。 |
| `template_id` | VARCHAR(100) UNIQUE | 是 | UUIDv4；`instance_agent_resource.ref_template_id` 引用本字段。 |
| `template_name` | VARCHAR(128) | 是 | 模板名称。 |
| `description` | VARCHAR(512) | 否 | 描述。 |
| `agent_tags` | JSON | 否 | 标签，如 `["vip","demo"]`。 |
| `template_ref` | JSON | 否 | 对各配置模板的槽位引用；结构见下。未配置可为 `{}`。 |
| `enabled` | BOOLEAN DEFAULT true | 是 | 是否启用。 |
| `data` | JSON | 否 | 扩展（如 `workspace_dir`）。 |
| `created_at` | DATETIME(3) | 是 | 创建时间。 |
| `updated_at` | DATETIME(3) | 是 | 更新时间。 |

**`template_ref` 槽位**：

| 槽位键 | 实体表 | 说明 |
|--------|--------|------|
| `default_model` | `model_template` | 默认模型 |
| `video_model` | `model_template` | 视频模型 |
| `audio_model` | `model_template` | 音频模型 |
| `vision_model` | `model_template` | 视觉模型 |
| `embedding_model` | `embedding_template` | Embedding |
| `skill_prebuilt` | `skill_prebuilt_template` | 预置Skill（可多条；见 §7） |
| `extension_config` | `extension_config_template` | 扩展配置 |
| `permissions` | `permissions_template` | 安全护栏 / Permissions（**单值**；见 §8） |

值为 `template_id` 字符串数组。空槽位键在规范化时省略。Manager 推送 Agent 模板前会先推送其引用的子模板。`service_config` 仅下发 Runtime，不出现在本 Gateway 接口中。

### 9.1 创建 / Upsert Agent 模板

- **接口名称**：创建 Agent 模板
- **请求方法**：`POST`
- **请求路径**：`/api/v1/agent-templates`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `template_id` | string | 是 | 业务 ID |
| `template_name` | string | 是 | 名称 |
| `description` | string | 否 | 描述 |
| `agent_tags` | string[] | 否 | 标签 |
| `template_ref` | object | 否 | 默认 `{}` |
| `enabled` | bool | 否 | 启用 |
| `data` | object | 否 | 扩展 |

- **返回参数**：`data.template_id`
- **请求示例**（演示数据「销售组 Agent 模板」；`m2/b2/...` 为已创建子模板的 `template_id`）：

```json
{
  "template_id": "aa000001-0000-4000-8000-00000000sale",
  "template_name": "销售组 Agent 模板",
  "description": "销售通道：M2/B2/预置Skill/E1+E2",
  "agent_tags": ["sales", "demo"],
  "template_ref": {
    "default_model": ["a1000001-0000-4000-8000-0000000000m2"],
    "vision_model": ["a1000001-0000-4000-8000-0000000000m2"],
    "video_model": ["a1000001-0000-4000-8000-0000000000m1"],
    "audio_model": ["a1000001-0000-4000-8000-0000000000m1"],
    "embedding_model": ["b1000001-0000-4000-8000-0000000000b2"],
    "skill_prebuilt": [
      "sp100001-0000-4000-8000-00000000pdf",
      "sp100001-0000-4000-8000-0000000wthr"
    ],
    "permissions": [
      "p1000001-0000-4000-8000-00000000perm"
    ],
    "extension_config": [
      "e1000001-0000-4000-8000-0000000000e1",
      "e1000001-0000-4000-8000-0000000000e2"
    ]
  },
  "enabled": true,
  "data": {}
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": { "template_id": "aa000001-0000-4000-8000-00000000sale" }
}
```

### 9.2 更新 Agent 模板

- **接口名称**：更新 Agent 模板
- **请求方法**：`PATCH`
- **请求路径**：`/api/v1/agent-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**：可选业务字段
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{
  "agent_tags": ["sales", "demo", "prod"],
  "enabled": true
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

### 9.3 删除 Agent 模板

- **接口名称**：删除 Agent 模板
- **请求方法**：`DELETE`
- **请求路径**：`/api/v1/agent-templates/{template_id}`
- **路径参数**：`template_id`
- **请求参数**：`{}`
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

---

## 10. 实例 Agent 资源（`instance_agent_resource` / `instance-agent-resources`）

### 10.0 表结构（Gateway：`instance_agent_resource`）

与 Manager 表字段对齐。`resource_id` 唯一，一行一个实例 Agent；多条件 OR 写在 `match_expr` JSON 中。

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | BIGINT 自增 PK | 是 | 数据库主键。 |
| `resource_id` | VARCHAR(100) UNIQUE | 是 | 实例化 Agent ID（运行时 / 会话标识）。 |
| `resource_name` | VARCHAR(128) | 是 | 展示名称。 |
| `resource_desc` | VARCHAR(512) | 否 | 描述。 |
| `ref_template_id` | VARCHAR(100) | 是 | 关联 `agent_template.template_id`。 |
| `match_expr` | JSON | 否 | 命中表达式；`[]` / `""` / null = 全匹配；`str` 单条件；`[str, ...]` 多条件 OR。 |
| `granted_by` | VARCHAR(64) | 否 | 授权人。 |
| `expires_at` | DATETIME(3) | 否 | 过期时间。 |
| `enabled` | BOOLEAN DEFAULT true | 是 | 是否启用。 |
| `data` | JSON | 否 | 扩展。 |
| `created_at` | DATETIME(3) | 是 | 创建时间。 |
| `updated_at` | DATETIME(3) | 是 | 更新时间。 |

### 10.1 Upsert 实例 Agent 资源

- **接口名称**：Upsert 实例 Agent 资源
- **请求方法**：`POST`
- **请求路径**：`/api/v1/instance-agent-resources`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `resource_id` | string | 是 | 资源 ID |
| `ref_template_id` | string | 是 | Agent 模板 ID |
| `resource_name` | string | 是 | 名称 |
| `resource_desc` | string | 否 | 描述 |
| `match_expr` | string \| string[] \| null | 否 | 命中表达式；默认 `[]` |
| `granted_by` | string | 否 | 授权人 |
| `enabled` | bool | 否 | 默认 `true` |
| `expires_at` | datetime | 否 | 过期时间 |
| `data` | object | 否 | 扩展 |

- **返回参数**：`data.resource_id`
- **请求示例**（演示数据 R_VIP / R_SALES 形态）：

```json
{
  "resource_id": "r1000001-0000-4000-8000-00000000rvip",
  "ref_template_id": "aa000001-0000-4000-8000-000000000vip",
  "resource_name": "VIP Agent（alice）",
  "resource_desc": "仅 alice 可用；聊天时 bot_id=本 resource_id",
  "match_expr": "user_id in ('alice')",
  "granted_by": null,
  "enabled": true,
  "expires_at": null,
  "data": { "demo": "r_vip" }
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "resource_id": "r1000001-0000-4000-8000-00000000rvip"
  }
}
```

### 10.2 删除实例 Agent 资源

- **接口名称**：删除实例 Agent 资源
- **请求方法**：`DELETE`
- **请求路径**：`/api/v1/instance-agent-resources/{resource_id}`
- **路径参数**：`resource_id`
- **请求参数**：`{}`
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

---

## 11. 应用配置 — Logging（`logging_config`）

### 11.0 表结构（`logging_config`）

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | BIGINT 自增 PK | 是 | 主键（单行配置）。 |
| `level` | VARCHAR(16) | 是 | 默认级别，如 `INFO`。 |
| `console_level` | VARCHAR(16) | 否 | 控制台级别。 |
| `gateway` | VARCHAR(16) | 否 | Gateway 日志级别。 |
| `channel` | VARCHAR(16) | 否 | Channel 日志级别。 |
| `agent_server` | VARCHAR(16) | 否 | AgentServer 日志级别。 |
| `full` | VARCHAR(16) | 否 | 全量覆盖级别。 |
| `created_at` | DATETIME(3) | 是 | 创建时间。 |
| `updated_at` | DATETIME(3) | 是 | 更新时间。 |

### 11.1 Upsert Logging 配置

- **接口名称**：Upsert Logging 配置
- **请求方法**：`PUT`
- **请求路径**：`/api/v1/logging`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `level` | string | 否 | 默认 `INFO` |
| `console_level` | string | 否 | — |
| `gateway` | string | 否 | — |
| `channel` | string | 否 | — |
| `agent_server` | string | 否 | — |
| `full` | string | 否 | — |

- **返回参数**：`data` 为落库后的级别字段对象
- **请求示例**：

```json
{
  "level": "INFO",
  "console_level": "WARNING",
  "gateway": "INFO",
  "channel": null,
  "agent_server": "INFO",
  "full": null
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": {
      "level": "INFO",
      "console_level": "WARNING",
      "gateway": "INFO",
      "channel": null,
      "agent_server": "INFO",
      "full": null
    }
}
```

### 11.2 删除 Logging 配置

- **接口名称**：删除 Logging 配置
- **请求方法**：`DELETE`
- **请求路径**：`/api/v1/logging`
- **请求参数**：`{}`
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

---

## 12. 应用配置 — 日志脱敏规则（`log_masking_rule`）

### 12.0 表结构（`log_masking_rule`）

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `id` | BIGINT 自增 PK | 是 | 数据库主键。 |
| `rule_id` | VARCHAR(64) UNIQUE | 是 | 规则业务 ID。 |
| `rule_name` | VARCHAR(128) | 是 | 规则名称。 |
| `description` | VARCHAR(512) | 否 | 描述。 |
| `pattern` | VARCHAR(512) | 是 | 正则。 |
| `replacement` | VARCHAR(64) | 是 | 替换串，默认 `******`。 |
| `priority` | INT | 是 | 优先级。 |
| `with_fingerprint` | BOOLEAN DEFAULT false | 是 | 是否在脱敏替换结果中附带指纹。 |
| `source` | VARCHAR(16) | 是 | 如 `custom` / `builtin`。 |
| `enabled` | BOOLEAN DEFAULT true | 是 | 是否启用。 |
| `data` | JSON | 否 | 扩展。 |
| `created_at` | DATETIME(3) | 是 | 创建时间。 |
| `updated_at` | DATETIME(3) | 是 | 更新时间。 |

### 12.1 创建日志脱敏规则

- **接口名称**：创建日志脱敏规则
- **请求方法**：`POST`
- **请求路径**：`/api/v1/log-masking-rules`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `rule_id` | string | 是 | 规则 ID |
| `rule_name` | string | 是 | 名称 |
| `pattern` | string | 是 | 正则 |
| `description` | string | 否 | 描述 |
| `replacement` | string | 否 | 替换串；默认 `******` |
| `priority` | int | 否 | 默认 `0` |
| `with_fingerprint` | bool | 否 | 默认 `false` |
| `source` | string | 否 | 默认 `custom` |
| `enabled` | bool | 否 | 默认 `true` |
| `data` | object | 否 | 扩展 |

- **返回参数**：`data.rule_id`
- **请求示例**：

```json
{
  "rule_id": "mask-phone",
  "rule_name": "手机号脱敏",
  "description": "屏蔽中国大陆手机号",
  "pattern": "1[3-9]\\d{9}",
  "replacement": "******",
  "priority": 10,
  "with_fingerprint": false,
  "source": "custom",
  "enabled": true,
  "data": null
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "rule_id": "mask-phone"
  }
}
```

### 12.2 更新日志脱敏规则

- **接口名称**：更新日志脱敏规则
- **请求方法**：`PATCH`
- **请求路径**：`/api/v1/log-masking-rules/{rule_id}`
- **路径参数**：`rule_id`
- **请求参数**：可选 `rule_name` / `description` / `pattern` / `replacement` / `priority` / `with_fingerprint` / `source` / `enabled` / `data`
- **返回参数**：`data.rule_id`
- **请求示例**：

```json
{
  "priority": 20,
  "enabled": true
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "rule_id": "mask-phone"
  }
}
```

### 12.3 删除日志脱敏规则

- **接口名称**：删除日志脱敏规则
- **请求方法**：`DELETE`
- **请求路径**：`/api/v1/log-masking-rules/{rule_id}`
- **路径参数**：`rule_id`
- **请求参数**：`{}`
- **返回参数**：`data` 为 `null`
- **请求示例**：

```json
{}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": null
}
```

---

## 13. 实例数据生命周期

无独立业务表；`purge` 会清理本 Gateway 上已同步的模板 / 资源 / 应用配置等表（含 channel / cron / Manager 公钥等；不可逆）。

### 13.1 清理实例配置数据

- **接口名称**：实例数据生命周期（purge）
- **请求方法**：`POST`
- **请求路径**：`/api/v1/instance-data-lifecycle`
- **请求参数**（Body）：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `op` | string | 否 | 默认 `purge`；目前仅支持 `purge` |

- **返回参数**：`data.purged` 为各表删除计数（仅含实际删到行的表）
- **请求示例**：

```json
{
  "op": "purge"
}
```

- **返回示例**：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "purged": {
      "model_template": 3,
      "embedding_template": 3,
      "extension_config_template": 2,
      "skill_prebuilt_template": 2,
      "permissions_template": 1,
      "agent_template": 3,
      "instance_agent_resource": 3,
      "log_masking_rule": 1,
      "logging_config": 1
    }
  }
}
```

---

## 14. Manager 侧调用映射（速查）

| Gateway 能力 | Manager 代码位置 |
|--------------|------------------|
| HTTP 客户端 | `manager_config_push/client.py` → `gateway_request` |
| Endpoint 解析 | `manager_config_push/endpoint.py` |
| 探活 | `core/instance/config_host_probe.py`、`schedulers/heartbeat_scanner.py` |
| 模板推送 | `core/template/push_template_to_gateway.py` |
| 预置 Skill 模板 | `core/template/skill_prebuilt_template.py` → `/api/v1/skill-prebuilt-templates` |
| 安全护栏模板 | `core/template/permissions_template.py` → `/api/v1/permissions-templates` |
| Agent 模板/资源推送 | `core/template/push_agent_template_to_gateway.py` |
| Agent 资源业务 | `core/instance_resource/instance_agent_resource_service.py` |
| 应用配置 | `core/application_config/*.py` |
| 上线全量同步 | `core/instance/instance_data_lifecycle.py` |
| 删实例清理 | `purge_gateway_instance_data` |

---

## 15. 源码索引

| 侧 | 路径 |
|----|------|
| Gateway 路由 | `manager_config_receiver/http/app.py` |
| 模板路由 / Schema | `routers/template_routers.py`、`schemas/template_schemas.py` |
| 表定义 | `jiuwenswarm/gateway/config/enterprise/tables/template_models.py`、`instance_resource_models.py`、`application_config_models.py` |
| 应用配置路由 | `routers/application_config_routers.py` |
| Agent 资源路由 | `routers/instance_resource_routers.py` |
| 生命周期 | `routers/instance_routers.py`、`core/instance/instance_data_lifecycle.py` |
| 探活 | `http/app.py` → `GET /api/health`、`GET /api/v1/ready` |

Gateway OpenAPI：`{gateway_config_host}/docs`。

---

## 16. 与旧 Gateway 接口的差异

> 旧实现：`jiuwenswarm/.../packages/jiuwenclaw-ee/gateway/extensions/manager_ws_client`  
> 新实现：`manager_config_receiver`（本文档 §1–§15 所描述的 HTTP Config Receiver）

旧链路是 **Gateway 作为 WebSocket 客户端连上 Manager**，由 Manager 下发 `config.push` 帧；新链路是 **Manager 作为 HTTP 客户端主动调用** Gateway 的 `gateway_config_host`。业务落库语义大体对齐，但传输、寻址、操作编码与能力边界均已切换。

### 16.1 架构与连接模型

| 维度 | 旧（`manager_ws_client`） | 新（本文档 HTTP） |
|------|---------------------------|-------------------|
| 传输 | WebSocket 长连接 | HTTP 请求/响应 |
| 谁主动连谁 | Gateway → Manager（`ws_url`，如 `ws://…:8766`） | Manager → Gateway（`gateway_config_host`） |
| 会话建立 | `register` / `register.ack`，分配或复用 `jiuwenclaw_id` | 实例在 Manager 侧已创建；推送前读 `instance_info.gateway_config_host` |
| 存活判定 | Gateway 发 `heartbeat`，Manager 回 `heartbeat.ack`；另有 `pod_status.report` | Manager 主动 `GET /api/health`（及就绪 `GET /api/v1/ready`） |
| 配置下发 | Manager → Gateway：`config.push` | Manager → Gateway：`POST/PUT/PATCH/DELETE /api/v1/...` |
| 结果回传 | Gateway → Manager：`config.ack`（含 `revision` / `success_flag` / `result`） | 同一次 HTTP 响应的统一包络 `{ code, message, data }` |
| Manager 客户端 | 旧 `manager_ws_server` 推帧 | `manager_config_push.client.gateway_request` |

### 16.2 报文形态对比

**旧：`config.push` 帧（示意）**

```json
{
  "type": "config.push",
  "payload": {
    "revision": "2026-06-01T10:00:00Z",
    "jiuwenclaw_id": "gw-1",
    "config": {
      "model_templates": {
        "op": "create",
        "template": { "template_id": "...", "api_key": "sk-..." }
      }
    },
    "sig": { "alg": "Ed25519", "key_id": "v1", "ts": "...", "nonce": "...", "value": "..." },
    "enc": { "scheme": "hybrid", "gw_key_fp": "...", "epk": "...", "wrapped_dek": "..." }
  }
}
```

- 一次 push 的 `config` 里按 **业务 key** 选一段（`model_templates` / `logging_config` / …），段内用 **`op`** 区分 create / update / delete / upsert / sync。
- 可带 **Ed25519 验签**（`sig`）与 **混合加密信封**（`enc` + 字段级 ENC）。
- Gateway 处理后回 `config.ack`。

**新：REST 调用（示意）**

```http
POST {gateway_config_host}/api/v1/model-templates
Content-Type: application/json

{ "template_id": "...", "api_key": "sk-...", ... }
```

```json
{ "code": 200, "message": "success", "data": { "template_id": "..." } }
```

- **HTTP 方法 + 路径** 代替 `op` + config key；Body 直接是业务字段（不再包一层 `template` / `updates`，应用配置类也不再要求 `op`）。
- 当前 Manager HTTP 客户端按明文 JSON 推送；旧链路的帧签名 / DEK 信封不在本 HTTP 协议内复用。

### 16.3 操作编码：`op` → HTTP 方法

| 旧 `op`（payload 内） | 新 HTTP | 说明 |
|----------------------|---------|------|
| `create` | `POST /api/v1/...` | 模板 / 脱敏规则等；Body 为资源本体（旧版常为 `{ op, template }`） |
| `update` | `PATCH /api/v1/.../{id}` | 路径带业务 ID；Body 为变更字段（旧版常为 `{ op, template_id, updates }`） |
| `upsert` | `PUT /api/v1/...` | logging 等单文档配置 |
| `delete` | `DELETE /api/v1/...` 或 `.../{id}` | 无业务字段时 Body `{}` |
| `sync` | **无对等单接口** | 旧版全量对账（upsert 全集 + 删差集）；新版由 Manager 上线引导时多次 REST 推送，或 `POST /api/v1/instance-data-lifecycle`（`purge`）清理 |
| `activate` / `deactivate`（仅 channel） | **本 Receiver 未提供** | 见 §16.5 |

### 16.4 能力映射速查

| 旧 `config` key | 旧入口 | 新 HTTP 路径（本文档） |
|-----------------|--------|------------------------|
| （无，靠 WS heartbeat） | `heartbeat` / `heartbeat.ack` | `GET /api/health`、`GET /api/v1/ready` |
| `model_templates` | `apply_model_template` | `/api/v1/model-templates` |
| `embedding_templates` | `apply_embedding_template` | `/api/v1/embedding-templates` |
| `extension_config_templates` | `apply_extension_config_template` | `/api/v1/extension-config-templates` |
| `skill_whitelist_templates` | `apply_skill_whitelist_template` | `/api/v1/skill-prebuilt-templates`（**改为** `skill_prebuilt_template` CRUD，见 §7） |
| `permissions_config`（旧实例级） | 旧 WS 应用配置 | `/api/v1/permissions-templates`（**改为** `permissions_template` CRUD，见 §8） |
| `logging_config` | `apply_logging_config` | `/api/v1/logging` |
| `log_masking_rule` | `apply_log_masking_rule` | `/api/v1/log-masking-rules` |
| `instance_data_lifecycle` | `apply_instance_data_lifecycle` | `/api/v1/instance-data-lifecycle` |
| — | 无 | `/api/v1/agent-templates`（**新增**） |
| — | 无 | `/api/v1/instance-agent-resources`（**新增**） |

### 16.5 旧有、本文档未覆盖的能力

下列 key 仍存在于旧 `manager_ws_client` 路由中，**不在**本 Config Receiver HTTP 文档范围内（去向以当前产品设计为准：策略/映射可能仍在 Manager 侧编排，或改由 Runtime 通道下发）：

| 旧 `config` key | 旧行为摘要 |
|-----------------|------------|
| `task_memory_config` | Task Memory 应用配置（前端未交付，暂未纳入本文档） |
| `memory_config` | Memory 应用配置（前端未交付，暂未纳入本文档） |
| `channel_config` | Channel 配置 create / activate / deactivate / delete |
| `service_config_templates` | 服务配置模板 CRUD / sync（Agent 模板说明中：`service_config` 仅下发 Runtime） |
| `config_default_template_mappings` | 默认模板映射 CRUD / sync |
| `config_effective_global_policies` | 全局生效策略 |
| `config_effective_service_policies` | 服务级生效策略 |
| `config_effective_agent_policies` | Agent 级生效策略 |

> Manager 推送代码里仍可能出现 `service_config_templates` → `/api/v1/service-config-templates` 的路径常量；若 Gateway Receiver 未实现该路由，则不属于本文档已交付接口。

### 16.6 数据模型与实例隔离

| 维度 | 旧 | 新 |
|------|----|----|
| `jiuwenclaw_id` 列 | 各业务表普遍带 `jiuwenclaw_id`，与 `(jiuwenclaw_id, template_id)` 等联合唯一 | 本文档表结构以单实例 Gateway 库为准，**无** `jiuwenclaw_id` 列；实例身份由「连到哪台 `gateway_config_host`」表达 |
| push 校验 | `assert_jiuwenclaw_id_matches`：帧内 id 须与已注册 id 一致 | HTTP 无帧内 id；Manager 用 `jiuwenclaw_id` 查 endpoint 后再请求 |
| 全量清理 | `instance_data_lifecycle.op=purge`，按 id 扫多表（含策略/channel 等） | `POST /api/v1/instance-data-lifecycle`，`op` 默认 `purge`；清理本机已同步表（模板/资源/应用配置/channel/cron/公钥等），响应 `data.purged` 仅含删到行的表 |

### 16.7 安全与副作用

| 项 | 旧 | 新 |
|----|----|----|
| 完整性 | 可选/强制 Ed25519 验签 + nonce 防重放 | 依赖传输层（建议内网 / TLS）；HTTP 层无 `sig` |
| 机密性 | 可选 hybrid 解 DEK + 敏感字段 ENC | Body 明文 JSON（密钥等仍可能出现在模板字段中，需靠网络隔离或后续加固） |
| Runtime 热更新 | 部分写成功后触发 `enterprise_config_update`（`runtime_management`） | 由 Receiver / Runtime 侧实现决定；**不**再走 WS `config.push` 后的统一钩子 |

### 16.8 迁移时注意点

1. **不要把旧 `op` JSON 原样 POST**：须改成对应 REST 方法与路径；模板 create 的 Body 是资源字段本身，不是 `{ "op":"create", "template":{...} }`。
2. **无 `sync` 单接口**：上线全量对齐改为 Manager 按资源逐条（或批量多次）HTTP 推送；删实例仍用 lifecycle `purge`。
3. **探活方向反转**：运维/排障看 Manager 对 `gateway_config_host` 的 health，而不是 Gateway 是否连上 Manager WS。
4. **Agent 模板与实例 Agent 资源** 为 HTTP 阶段新增能力，旧 WS 路由中无对应 key。
5. **安全护栏** 不再走旧 `permissions_config` 应用配置；改为 `POST/PATCH/DELETE /api/v1/permissions-templates`，由 Agent `template_ref.permissions` 引用（§8）。
6. **预置 Skill** 不再走 `/skill-whitelist-templates`（`skill_id` + `skill_version` + `skill_source`）；改为 `/skill-prebuilt-templates` 双模式（provider / url），槽位 `skill_prebuilt`（§7）。
7. **策略 / Channel / 部分 service 模板** 若业务仍需要，需确认是否改走 Runtime 配置通道或其他接口，不能假设旧 `config.push` key 在本 Receiver 上仍可用。
