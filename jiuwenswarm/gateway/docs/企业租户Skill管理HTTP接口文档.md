# 企业租户 Skill 管理 HTTP 接口文档

## 0. 概述

> **读者**：企业版（`gateway.edition = enterprise`）接入方——浏览器、BFF 或经 Ingress / 反向代理访问 Gateway 的 HTTP 客户端。
> **范围**：以 `/skills/enterprise` 开头的六个企业租户 HTTP 操作（§4、§5），并保留企业版可用的通用 Skill 接口作为补充（§1–§3）。企业接口与通用接口分别注册，不能互相替代；个人市场、本地导入、检索和演进不在本次补齐范围。

---

## Part A — 通用约定

### A1 Base URL 与传输

| 项 | 说明 |
|----|------|
| Base URL | 部署对外暴露的 Web HTTP 地址，例如 `https://gateway.example.com` |
| 路径前缀 | `/api/v1`，下文路径均不含此前缀 |
| 请求 / 响应 | JSON；POST 使用 `Content-Type: application/json` |
| 直连端口 | `GATEWAY_WEB_HTTP_PORT` 优先，否则取配置 `http_port`，未配置时按 `ws_port + 2` 计算 |
| 超时 | Gateway 单次响应等待默认 120 秒，由 `GATEWAY_WEB_HTTP_UNARY_TIMEOUT` 调整 |

### A2 身份与租户路由

六个企业接口使用同一个 Web HTTP 入口和身份规则；下文各节的 Query/Body 表只列业务字段及该路由的差异。

| 字段 / HTTP 头 | 类型 | 必填 / 默认值 | 说明 |
|----------------|------|----------------|------|
| `X-Bot-Id` | string | 企业请求应提供，无业务默认值 | 选中的机器人，参与运行时路由 |
| `X-User-Id` | string | 用户 workspace 请求应提供，无业务默认值 | 当前调用用户 |
| `X-Group-Id` | string | 按部署的组织/群组路由提供，无业务默认值 | 当前组织 / 群组 |
| Query `group_id` / `bot_id` / `user_id` | string | 可选，无默认值 | 六个企业路由均声明这些参数；它们是 handler 参数，不应替代受信头建立的连接级身份 |
| Query `service_id` / `agent_id` | string | 可选，无 HTTP 层业务默认值 | 六个企业路由均声明；对应运行时租户 / Agent。安装、卸载省略时由目标 adapter / SkillManager 上下文补齐；最终仍缺任一项返回 `missing_tenant` |
| `X-Session-Id` | string | 可选 | 关联已有会话；省略时 HTTP 层生成 `webhttp_<随机值>` 请求会话标识。它不替代租户身份，也不是新建聊天会话接口 |
| `X-Request-Id` | string | 可选，生成 UUID hex | 请求关联标识，响应回显 |

身份应由部署入口的认证代理 / BFF 校验并注入。当前企业版默认信任上游身份头；`GATEWAY_WEB_HTTP_TRUST_CLIENT_HEADERS` 可显式覆盖该行为。这些头是身份传递约定，不是客户端自行声明身份即可获得授权的凭据。入口所需登录态或认证方式由部署决定，本文不定义额外的业务 token。

HTTP 适配层先读取路由声明的 Query，再与 JSON Body 合并（同名 Query 优先）；`X-*` 头补齐缺失参数。分发时连接级 `user/group/bot` 身份另外从受信头建立，因此不要在头、Query 和 Body 中提供互相冲突的身份。POST 可在 Body 携带 `service_id/agent_id`，仍应与实际路由到的租户上下文一致。

企业安装状态读取和写入目标运行时的 workspace；`/skills/enterprise` 不是全租户管理数据库查询接口。本文 curl 示例中的 `gateway.example.com`、身份、来源 ID、技能 ID 和 URL 均须替换为实际部署值。

### A3 统一响应

**成功**（HTTP 200）：

```json
{
  "request_id": "req-skill-001",
  "ok": true,
  "data": {},
  "metadata": { "rpc_method": "skills.list", "transport": "web-http" }
}
```

**业务失败**：handler 正常返回失败结果时仍为 HTTP 200、外层 `ok=true`，检查 `data.success`、`data.error_code` 和 `data.error_message`。列表没有 `data.success` 字段，不能将其缺省解释为失败。

```json
{
  "request_id": "req-skill-002",
  "ok": true,
  "data": {
    "success": false,
    "error_code": "missing_params",
    "error_message": "source_id is required"
  },
  "metadata": { "rpc_method": "skills.enterprise.source.search", "transport": "web-http" }
}
```

**分发 / 执行失败**：外层 `ok=false`，错误是 `error` 对象，不是字符串，也不放在 `data` 中。例如 HTTP 504：

```json
{
  "request_id": "req-skill-003",
  "ok": false,
  "error": { "code": "TIMEOUT", "message": "request timed out", "details": {} },
  "metadata": { "rpc_method": "skills.list", "transport": "web-http" }
}
```

错误消息用于展示，具体文字以实际响应为准。RPC 错误码到 HTTP 状态的映射如下；不表示每个 handler 都主动产生所有错误码：

| `error.code` | HTTP 状态 |
| --- | --- |
| `BAD_REQUEST` | 400 |
| `UNAUTHORIZED` | 401 |
| `FORBIDDEN` | 403 |
| `NOT_FOUND` / `METHOD_NOT_FOUND` | 404 |
| `CONFLICT` | 409 |
| `SERVICE_UNAVAILABLE` | 503 |
| `TIMEOUT` | 504 |
| `INTERNAL_ERROR` / 未映射错误码 | 500 |

JSON Body 应为对象；不符合 FastAPI 参数类型的请求可能返回 HTTP 422 `detail` 校验响应，而不是上述业务信封。

### A4 WS 方法名与 HTTP 映射

| method | HTTP 方法 | HTTP 路径（加统一前缀 `/api/v1`） | 说明位置 |
| --- | --- | --- | --- |
| skills.list | GET | /skills | §1.1 |
| skills.get | GET | /skills/{name} | §1.2 |
| skills.source.providers | GET | /skills/sources | §2.1 |
| skills.source.search | GET | /skills/sources/search | §2.2 |
| skills.source.install | POST | /skills/sources/actions/install | §2.3 |
| skills.updates.check | GET | /skills/updates | §3.1 |
| skills.update | POST | /skills/actions/update | §3.2 |
| skills.enterprise.list | GET | /skills/enterprise | [§5.1](#enterprise-list) |
| skills.enterprise.install | POST | /skills/enterprise/actions/install | [§5.2](#enterprise-install) |
| skills.enterprise.uninstall | POST | /skills/enterprise/actions/uninstall | [§4.1](#enterprise-uninstall) |
| skills.enterprise.source.providers | GET | /skills/enterprise/sources | [§5.3](#enterprise-providers) |
| skills.enterprise.source.search | GET | /skills/enterprise/sources/search | [§5.4](#enterprise-search-get) |
| skills.enterprise.source.search | POST | /skills/enterprise/sources/actions/search | [§5.5](#enterprise-search-post) |

---

## Part B — 接口定义

### 1. 列表与详情

#### 1.1 `GET /skills`（skills.list）

**含义**：发现型只读列表。企业版从 workspace 安装状态合并结果，不返回个人版 marketplace 项，也不在请求主路径触发远端更新检查。

**Query 参数**

| key | 必填 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| refresh_marketplaces | 否 | bool | false | 先刷新 marketplace（个人版语义） |
| with_installed | 否 | bool | false | 同时返回 `plugins` |

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| skills | 是 | array | 技能数组，企业版只含已安装的 prebuilt/user，元素见下表 |
| plugins | 否 | array | 仅 `with_installed=true`；企业版为空 |

**`skills` 数组元素**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| name | 是 | string | 技能名（目录名 / frontmatter） |
| description | 否 | string | 描述，缺省 `""` |
| version | 否 | string | 版本，缺省 `""` |
| author | 否 | string | 作者，缺省 `""` |
| tags | 否 | string[] | 标签，缺省 `[]` |
| allowed_tools | 否 | string[] | 允许工具 |
| path | 否 | string | `SKILL.md` 路径 |
| source | 是 | string | 来源展示名；企业版以 `source_id` 为权威 |
| installed | 否 | bool | 企业版 list 结果恒为 `true` |
| display_name | 否 | string | 展示名（保留来源原始大小写） |
| origin | 否 | string | 安装来源追溯；企业版同名覆盖后无需消歧 |
| installed_at | 否 | string | 安装时间（ISO-8601），安装/覆盖时写入 |
| updated_at | 否 | string | 最近更新时间（ISO-8601），更新操作写入 |
| source_type | 否 | string | `prebuilt` / `user` |
| source_id | 否 | string | 来源定位参数（install/update 指定来源） |
| skill_id | 否 | string | 来源内 Skill ID（install/update 定位键） |
| version_id | 否 | string | 精确制品定位参数（update 用它做乐观并发） |
| removable | 否 | bool | 是否允许当前调用方卸载；`prebuilt` 固定 `false`，仅 `user` 可卸载 |
| remote_status | 否 | string | `available/no_access/not_downloadable/removed/unknown` |
| verification | 否 | object | 最近一次成功安装/更新的验签审计摘要，字段见 Part C；不包含 key 原文 |

**响应示例**：

```json
{
  "request_id": "req-001",
  "ok": true,
  "data": {
    "skills": [
      {
        "name": "meeting_book",
        "display_name": "meeting_book",
        "version": "1.0.0",
        "author": "",
        "source": "swarm-skillhub",
        "source_id": "swarm-skillhub",
        "skill_id": "42",
        "version_id": "1024",
        "source_type": "user",
        "removable": true,
        "installed": true,
        "installed_at": "2026-09-10T08:00:00+00:00",
        "updated_at": "2026-09-10T08:00:00+00:00"
      }
    ]
  }
}
```

#### 1.2 `GET /skills/{name}`（skills.get）

**含义**：按清单 `name` 取单个 Skill 详情；相对列表额外返回正文 `content` 与 `file_path`。

**路径参数**：`name`（必填，技能名）。

**Query 参数**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| origin | 否 | string | 安装身份标识，重名时消歧 |

**响应 `data`**：列表元素全部字段，外加：

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| content | 是 | string | `SKILL.md` 正文 |
| file_path | 否 | string | `SKILL.md` 路径 |

未找到返回 `skill_not_found`；同名多命中且未传 `origin` 返回 `skill_origin_ambiguous`。

---

### 2. 技能源

#### 2.1 `GET /skills/sources`（skills.source.providers）

**含义**：列出当前已注册的技能源与能力声明，包含禁用来源（由 `enabled` 区分）；按 `priority` 降序、`source_id` 升序排列。响应不返回 endpoint、token、密钥或公钥原文。

**入参**：无业务必填项。

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| success | 是 | bool | 成功时为 `true` |
| providers | 是 | array | 技能源数组；未配置时为 `[]` |

**`providers` 元素**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| source | 是 | string | `source_id` 的兼容别名 |
| source_id | 是 | string | 稳定来源 ID |
| provider_type | 是 | string | Provider 类型 |
| display_name | 是 | string | 展示名 |
| enabled | 是 | bool | 是否启用；`false` 的来源不能搜索/安装 |
| priority | 是 | int | 展示优先级 |
| capabilities | 是 | string[] | 能力列表，如 `search/check_updates/get_artifact` |

当前 `SourceDescriptor.to_dict()` 不返回 `verification_required/signature_mode/allowed_algorithms`，客户端不能将这些字段作为必填响应。

#### 2.2 `GET /skills/sources/search`（skills.source.search）

**含义**：按 `source_id` 调度 Provider 搜索可安装 Skill，支持关键词与分页。签名、HMAC key、Bearer token、预签名 URL 与服务器路径不进入响应。

**Query 参数**

| key | 必填 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| source_id | 是 | string | — | 已注册来源 ID |
| q | 否 | string | — | 搜索关键词 |
| page | 否 | int | 1 | 页码 |
| page_size | 否 | int | 20 | 每页数量，1–100 |

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| source_id | 是 | string | 来源回显 |
| items | 是 | array | 候选数组，元素见下表 |
| total | 否 | int | 总数 |
| page | 是 | int | 当前页 |
| page_size | 是 | int | 当前页大小 |
| count | 否 | int | 本页数量 |
| next_page | 否 | int | 下一页，无更多为空 |

**`items` 元素（SkillCandidate）**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| source_id | 是 | string | 稳定来源 ID |
| skill_id | 是 | string | 来源内 Skill ID，install/update 定位键 |
| version_id | 是 | string | 精确制品版本 ID |
| name | 是 | string | 技能名 |
| display_name | 否 | string | 展示名 |
| summary | 否 | string | 简介 |
| version | 否 | string | 展示版本 |
| fingerprint | 否 | string | 内容指纹 |
| namespace | 否 | string | 所属空间 |
| owner_display_name | 否 | string | 作者展示名 |
| labels | 否 | array | 标签 |
| rating_avg | 否 | number | 平均评分 |
| rating_count | 否 | int | 评分人数 |
| download_count | 否 | int | 下载量 |
| updated_at | 否 | string/number | 远端更新时间 |
| versions | 否 | array | 可选版本摘要 |
| accessible | 否 | bool | 是否可访问 |
| downloadable | 否 | bool | 是否可下载 |

#### 2.3 `POST /skills/sources/actions/install`（skills.source.install）

**含义**：按 `skill_id` 定位并安装指定来源的精确制品版本。安装时调用 Provider `get_artifact` 获取制品描述，再由平台公共管道完成下载、SHA-256 摘要校验与验签（默认 HMAC 契约，Provider 可覆写 `verify_artifact`），最后安全解压、实体与 JSON 原子提交。安装身份以下载后解析的 `SKILL.md.name` 为准；同名已安装时需 `force=true` 才能重装，预置 Skill 不可覆盖。此接口不是 §5.2 的企业 URL 安装接口。

**Body**

| key | 必填 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| source_id | 是 | string | — | 稳定来源 ID |
| skill_id | 是 | string | — | 来源内 Skill ID，安装定位键 |
| version_id | 是 | string | — | 精确制品版本 ID |
| force | 否 | bool | false | 同名技能是否覆盖重装；不带 `force` 且同名已安装返回 `skill_already_installed`；预置 Skill 不可覆盖 |

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| success | 是 | bool | 固定 `true` |
| skill | 是 | object | 安装后的 Skill，元素同 1.1，至少回显 `name/source_id/skill_id/version_id` |

**请求示例**：

```json
{
  "source_id": "swarm-skillhub",
  "skill_id": "42",
  "version_id": "1024"
}
```

---

### 3. 更新

#### 3.1 `GET /skills/updates`（skills.updates.check）

**含义**：按 workspace 安装记录批量检查更新。单一来源失败不影响其他来源，不删除本地安装。

**Query 参数**

| key | 必填 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| source_ids | 否 | string[] | 全部支持更新的来源 | 限定检查来源 |
| force_refresh | 否 | bool | false | 忽略缓存 |

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| items | 是 | array | 更新状态数组，元素见下表 |

**`items` 元素**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| source_id | 是 | string | 来源 ID |
| skill_id | 是 | string | 来源内 Skill ID，更新定位键 |
| name | 是 | string | 技能名 |
| current_version_id | 是 | string | 当前制品版本 ID |
| current_version | 否 | string | 当前展示版本 |
| has_update | 是 | bool | 是否存在更新 |
| fingerprint_matched | 否 | bool | 内容指纹是否一致 |
| accessible | 否 | bool | 是否可访问 |
| downloadable | 否 | bool | 是否可下载 |
| remote_status | 否 | string | `available/no_access/not_downloadable/removed/unknown` |
| error_code | 否 | string | 本条检查失败原因 |

#### 3.2 `POST /skills/actions/update`（skills.update）

**含义**：按 `skill_id` 定位已安装 Skill 并更新到指定精确版本。更新时重新获取短期下载描述，不复用搜索/检查结果。

**Body**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| source_id | 是 | string | 稳定来源 ID |
| skill_id | 是 | string | 来源内 Skill ID，更新定位键 |
| target_version_id | 是 | string | 目标精确制品版本 ID |
| expected_current_version_id | 否 | string | 乐观并发条件；不一致返回 `skill_version_conflict` |

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| success | 是 | bool | 固定 `true` |
| skill | 是 | object | 更新后的 Skill，元素同 1.1；保留 `name/installed_at`，更新 `version_id/version/updated_at` |

---

### 4. 卸载

<a id="enterprise-uninstall"></a>

#### 4.1 `POST /skills/enterprise/actions/uninstall`（skills.enterprise.uninstall）

**含义**：卸载目标 workspace 中 `source_type=user` 的安装，调用 `handle_skills_web_uninstall`；预置及其他非 user 类型不可卸载。身份与入口见 A1/A2。

**Query**：可选 `group_id/bot_id/user_id/service_id/agent_id`，取值与身份来源见 A2；`session_id` 可在 Body 或 `X-Session-Id` 中传递，该路由没有声明它的 Query 参数。

**Body**

| key | 必填 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| name | 是 | string | 无 | 非空技能名，建议直接取企业列表的 `name` |
| origin | 否 | string | 空 | 取企业列表的 `origin`。提供时按 origin 查找唯一安装记录，并以该记录的 name 卸载；零条或多条匹配均返回 `not_found` |
| service_id / agent_id | 否 | string | 目标运行时上下文 | 最终均须非空，见 A2 |
| session_id | 否 | string | HTTP 自动绑定值 | 关联会话，见 A2 |

**请求示例**：

```bash
curl -X POST 'https://gateway.example.com/api/v1/skills/enterprise/actions/uninstall' \
  -H 'Content-Type: application/json' \
  -H 'X-Bot-Id: bot-001' -H 'X-User-Id: user-001' -H 'X-Group-Id: group-001' \
  -d '{"name":"meeting_book"}'
```

**成功响应**（HTTP 200）：`data.success=true`，`data.name` 为实际卸载名称；此 handler 不返回成功 `detail` 字段。

```json
{
  "request_id": "req-uninstall-001",
  "ok": true,
  "data": { "success": true, "name": "meeting_book" },
  "metadata": { "rpc_method": "skills.enterprise.uninstall", "transport": "web-http" }
}
```

**失败响应示例**（HTTP 200，未安装）：

```json
{
  "request_id": "req-uninstall-002",
  "ok": true,
  "data": { "success": false, "error_code": "not_found", "error_message": "skill `meeting_book` is not installed" },
  "metadata": { "rpc_method": "skills.enterprise.uninstall", "transport": "web-http" }
}
```

业务错误包括 `missing_params`、`invalid_skill_name`、`missing_tenant`、`not_found`、`prebuilt_not_removable`、`uninstall_failed`；底层卸载返回的其他业务码原样透传。分发/执行失败采用 A3 的 `ok=false` 信封。

---

### 5. 企业列表、安装与来源搜索

本节补齐 `/skills/enterprise` 组的其余五个 HTTP 操作；所有完整 URL 均为 A1 Base URL + `/api/v1` + 节标题路径。

<a id="enterprise-list"></a>

#### 5.1 `GET /skills/enterprise`（skills.enterprise.list）

**含义与边界**：调用 `handle_skills_enterprise_list`，读取目标 workspace 的安装 DTO，并补充企业兼容字段。它不同于 §1.1 的发现型 `/skills`：不接收 `with_installed/refresh_marketplaces`，固定返回 `plugins/skills/service_id/agent_id`，且 `installed` 反映实体是否完整，不能假定永远为 true。

**身份与 Query**：A2 的受信身份头适用；可选 Query 为 `group_id/bot_id/user_id/service_id/agent_id`，类型均为 string。无必填业务 Query，无 Body。`service_id/agent_id` 优先使用传入参数，否则使用 SkillManager 上下文，仍缺失时返回空字符串。可用 `X-Session-Id` 关联会话；未声明 `session_id` Query。

```bash
curl 'https://gateway.example.com/api/v1/skills/enterprise' \
  -H 'X-Bot-Id: bot-001' -H 'X-User-Id: user-001' -H 'X-Group-Id: group-001'
```

**成功响应 `data`**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| skills | array | 安装账本中 `prebuilt/user/builtin` 类型的 Skill DTO；无记录为 `[]` |
| plugins | array | 兼容的 marketplace 插件记录数组；通常企业 workspace 为 `[]`，不受 `with_installed` 控制 |
| service_id / agent_id | string | 当前请求参数或管理器上下文的租户/Agent 标识 |

`skills` 元素保留安装记录字段；与 §1.1 可共用 `name/source_type/source/origin/version`、来源 ID、时间和可选 `verification` 的字段含义，但不保证返回扫描 `SKILL.md` 得到的 description/path/content。额外/差异字段如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| installation_id | string | 安装记录 ID |
| declared_name / entity_dir | string | 声明名称 / 实体目录名 |
| installed | bool | 实体可用才为 true，状态不一致时可为 false |
| enabled | bool | 安装启用状态 |
| removable | bool | 仅 `source_type=user` 为 true |
| consistency | string | `ok` / `inconsistent` |
| sync_status | string | prebuilt DTO 附带 `synced` / `failed`；其他类型不保证有此字段 |
| skill_name | string | `name` 的企业兼容别名 |
| skill_source | string | `origin` 的企业兼容别名，不等同于 `source_id` |
| skill_version | string | `version` 的企业兼容别名 |
| service_id / agent_id | string | 与外层 data 相同的标识 |

**成功示例**（HTTP 200；无安装记录）：

```json
{
  "request_id": "req-enterprise-list-001",
  "ok": true,
  "data": { "plugins": [], "skills": [], "service_id": "service-001", "agent_id": "agent-001" },
  "metadata": { "rpc_method": "skills.enterprise.list", "transport": "web-http" }
}
```

**失败**：该列表 handler 不定义 `data.success=false` 业务分支；异常、不可达和超时按 A3 返回。例如 HTTP 504：

```json
{
  "request_id": "req-enterprise-list-002",
  "ok": false,
  "error": { "code": "TIMEOUT", "message": "request timed out", "details": {} },
  "metadata": { "rpc_method": "skills.enterprise.list", "transport": "web-http" }
}
```

<a id="enterprise-install"></a>

#### 5.2 `POST /skills/enterprise/actions/install`（skills.enterprise.install）

**含义与边界**：调用 `handle_skills_web_install`，从 URL 下载归档，在目标 workspace 安装为 user Skill。**它不接收 §2.3 的三项制品 ID 作为安装契约**，也不使用其 `force` 参数。名称、版本来自归档内 `SKILL.md`，客户端不能用 Body 的 `name/version` 覆盖。

**身份与 Query**：A2 的受信头适用；Query 可选 `group_id/bot_id/user_id/service_id/agent_id`。`service_id/agent_id` 可由目标运行时上下文补齐，最终缺失返回 `missing_tenant`。

**JSON Body**：

| 字段 | 类型 | 必填 | 默认值 / 说明 |
| --- | --- | --- | --- |
| url | string | 是 | 非空归档下载 URL；必须 HTTPS 且主机命中服务端下载白名单，不跟随重定向 |
| signature | string | 否 | 空字符串。非空时校验归档原始字节的 HMAC-SHA256 hex，服务端密钥为 `SKILL_DOWNLOAD_HMAC_SECRET`；缺省/空值时当前实现跳过 HMAC 校验 |
| service_id / agent_id | string | 否 | 目标运行时上下文；Query 同名值优先，见 A2 |
| session_id | string | 否 | HTTP 自动绑定值，或通过 `X-Session-Id` 提供；不是此路由的 Query 参数 |

同名预置或不同来源安装返回 `skill_name_conflict`；同名 user、相同 URL、相同版本返回 `skill_already_installed`；同名 user、相同 URL、版本变化时允许替换。

```bash
curl -X POST 'https://gateway.example.com/api/v1/skills/enterprise/actions/install' \
  -H 'Content-Type: application/json' \
  -H 'X-Bot-Id: bot-001' -H 'X-User-Id: user-001' -H 'X-Group-Id: group-001' \
  -d '{"url":"https://artifacts.example.com/meeting_book.zip"}'
```

示例依赖目标服务已配置下载主机白名单，并由运行时提供租户上下文；如携带 signature，应使用制品发布方给出的真实摘要签名。

**成功响应**（HTTP 200）：`data.success` 为 true，`data.skill` 仅含 `name/version`，不承诺返回完整安装 DTO；版本缺失时为 null，完整状态通过 §5.1 查询。

```json
{
  "request_id": "req-enterprise-install-001",
  "ok": true,
  "data": { "success": true, "skill": { "name": "meeting_book", "version": "1.0.0" } },
  "metadata": { "rpc_method": "skills.enterprise.install", "transport": "web-http" }
}
```

**失败响应示例**（HTTP 200；空 Body）：

```json
{
  "request_id": "req-enterprise-install-002",
  "ok": true,
  "data": { "success": false, "error_code": "missing_params", "error_message": "url is required" },
  "metadata": { "rpc_method": "skills.enterprise.install", "transport": "web-http" }
}
```

业务错误：`missing_params`、`missing_tenant`、`download_failed`、`signature_invalid`、`extract_failed`、`invalid_package`、`invalid_skill_name`、`skill_name_conflict`、`skill_already_installed`、`install_failed`、`state_write_failed`。重复安装失败可能额外含 `installed=false/already_installed=true/name`；分发/执行失败见 A3。

<a id="enterprise-providers"></a>

#### 5.3 `GET /skills/enterprise/sources`（skills.enterprise.source.providers）

**含义与字段复用**：企业身份路由下调用 `handle_skills_source_providers`。与 §2.1 的通用来源列表使用同一 handler，所以 **data 和 providers 元素字段完全复用 §2.1**；HTTP 路径和 RPC 方法不同，Query 声明相同（见下表述）。来源配置由企业运行时准备，结果仅是当前上下文已注册的来源，不是全平台来源目录。

**身份与 Query**：适用 A2；可选 string 参数为 `group_id/bot_id/user_id/service_id/agent_id/session_id`。无必填业务字段、无 Body；`session_id` 缺省绑定 HTTP 请求会话，也可用 `X-Session-Id`。

```bash
curl 'https://gateway.example.com/api/v1/skills/enterprise/sources' \
  -H 'X-Bot-Id: bot-001' -H 'X-User-Id: user-001' -H 'X-Group-Id: group-001'
```

**成功响应**（HTTP 200；未配置来源时 providers 为 `[]`）：

```json
{
  "request_id": "req-enterprise-providers-001",
  "ok": true,
  "data": {
    "success": true,
    "providers": [{
      "source": "swarm-skillhub", "source_id": "swarm-skillhub",
      "provider_type": "skillhub", "display_name": "企业技能源",
      "enabled": true, "priority": 0, "capabilities": ["get_artifact", "search"]
    }]
  },
  "metadata": { "rpc_method": "skills.enterprise.source.providers", "transport": "web-http" }
}
```

来源 ID、Provider 类型及能力列表由部署配置决定。禁用来源仍列出 `enabled=false`。此列表不执行远端搜索，不保证列出的 Provider 远端服务当前可达。

**失败**：handler 没有独立业务失败分支；配置准备或执行异常走 A3 的执行失败信封。例如 HTTP 500：

```json
{
  "request_id": "req-enterprise-providers-002",
  "ok": false,
  "error": { "code": "INTERNAL_ERROR", "message": "request failed", "details": {} },
  "metadata": { "rpc_method": "skills.enterprise.source.providers", "transport": "web-http" }
}
```

<a id="enterprise-search-get"></a>

#### 5.4 `GET /skills/enterprise/sources/search`（skills.enterprise.source.search）

**含义**：企业身份下调用 `handle_skills_source_search`。GET 所有业务字段都在 Query 中，不读取 JSON Body。身份见 A2。

| Query 字段 | 类型 | 必填 | 默认值 / 约束 |
| --- | --- | --- | --- |
| source_id | string | 是 | 非空，使用 §5.3 返回的已启用、支持 search 的来源 ID |
| q | string | 否 | `""`，去除首尾空白 |
| page | int | 否 | `1`，必须 >= 1 |
| page_size | int | 否 | `20`，范围 1–100 |
| group_id / bot_id / user_id / service_id / agent_id | string | 否 | 无 HTTP 层业务默认值，身份与上下文规则见 A2 |
| session_id | string | 否 | HTTP 请求会话，也可用 `X-Session-Id` |

GET 没有声明 `filter` Query，不能用 JSON 编码的 Query filter 实现扩展筛选；需要筛选时使用 §5.5。

```bash
curl --get 'https://gateway.example.com/api/v1/skills/enterprise/sources/search' \
  -H 'X-Bot-Id: bot-001' -H 'X-User-Id: user-001' -H 'X-Group-Id: group-001' \
  --data-urlencode 'source_id=swarm-skillhub' --data-urlencode 'q=会议' \
  --data-urlencode 'page=1' --data-urlencode 'page_size=20'
```

**成功 `data` 字段**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| success | bool | true |
| source / source_id | string | 同一个来源 ID，source 为兼容别名 |
| items / skills | array | 同一候选列表，skills 为兼容别名；候选字段复用 §2.2 的 SkillCandidate |
| total | int / null | Provider 返回的总数；未知时为 null |
| page / page_size | int | 本次分页参数 |
| count | int | 本页候选数 |
| next_page | int / null | Provider 返回的下一页；无下一页时为 null |

与通用 `skills.source.search` 使用同一 handler，候选契约相同；可选字段为空时会省略，不承诺返回空数组的 labels/versions。候选不是已安装记录，使用 `source_id/skill_id/version_id` 安装应调用 §2.3 的 `POST /skills/sources/actions/install`，不能把这三个字段直接交给 §5.2。

**成功示例**（HTTP 200；未匹配到候选）：

```json
{
  "request_id": "req-enterprise-search-001",
  "ok": true,
  "data": {
    "success": true, "source": "swarm-skillhub", "source_id": "swarm-skillhub",
    "items": [], "skills": [], "total": 0, "page": 1, "page_size": 20, "count": 0, "next_page": null
  },
  "metadata": { "rpc_method": "skills.enterprise.source.search", "transport": "web-http" }
}
```

**失败示例**（HTTP 200；缺少 source_id）：

```json
{
  "request_id": "req-enterprise-search-002",
  "ok": true,
  "data": { "success": false, "error_code": "missing_params", "error_message": "source_id is required" },
  "metadata": { "rpc_method": "skills.enterprise.source.search", "transport": "web-http" }
}
```

业务错误：`missing_params`、`invalid_params`（分页或筛选非法）、`source_not_found`、`source_disabled`、`source_capability_unsupported`、`source_response_invalid`、`source_unavailable`；Provider 抛出的 `SourceRegistryError` 业务码会透传。分发/执行失败见 A3。

<a id="enterprise-search-post"></a>

#### 5.5 `POST /skills/enterprise/sources/actions/search`（skills.enterprise.source.search）

**含义与复用**：与 §5.4 的 GET 调用同一 handler，身份、分页语义、成功 data 字段、候选字段和错误码均相同；区别是本路由通过 JSON Body 接收搜索参数，并支持对象型 `filter`。

**Query**：仅声明 A2 的 `group_id/bot_id/user_id/service_id/agent_id`。`source_id/q/page/page_size/filter/session_id` 应放 Body；不要将 GET 的搜索 Query 原样附到此 POST 路径。

| Body 字段 | 类型 | 必填 | 默认值 / 约束 |
| --- | --- | --- | --- |
| source_id | string | 是 | 非空来源 ID |
| q | string | 否 | `""` |
| page | int | 否 | `1`，必须 >= 1 |
| page_size | int | 否 | `20`，范围 1–100 |
| filter | object / null | 否 | `{}`；null 同空对象，键值由具体 Provider 定义 |
| service_id / agent_id | string | 否 | 无 HTTP 层业务默认值，见 A2 |
| session_id | string | 否 | HTTP 自动绑定值，也可用 `X-Session-Id` |

使用单数键名 `filter`，不是 `filters`。对象顶层不得包含以下字段（大小写和首尾空格归一化后检查）：`token/authorization/password/secret/signature/download_url/user_id/group_id/bot_id/service_id/agent_id`；命中时返回 `invalid_params`。不得把身份或来源凭据藏在 filter 中。

```bash
curl -X POST 'https://gateway.example.com/api/v1/skills/enterprise/sources/actions/search' \
  -H 'Content-Type: application/json' \
  -H 'X-Bot-Id: bot-001' -H 'X-User-Id: user-001' -H 'X-Group-Id: group-001' \
  -d '{"source_id":"swarm-skillhub","q":"会议","page":1,"page_size":20,"filter":{}}'
```

**成功示例**（HTTP 200，与 §5.4 相同 data 契约）：

```json
{
  "request_id": "req-enterprise-search-post-001",
  "ok": true,
  "data": {
    "success": true, "source": "swarm-skillhub", "source_id": "swarm-skillhub",
    "items": [], "skills": [], "total": 0, "page": 1, "page_size": 20, "count": 0, "next_page": null
  },
  "metadata": { "rpc_method": "skills.enterprise.source.search", "transport": "web-http" }
}
```

**失败示例**（HTTP 200；Body 中 `filter` 为字符串）：

```json
{
  "request_id": "req-enterprise-search-post-002",
  "ok": true,
  "data": { "success": false, "error_code": "invalid_params", "error_message": "filter must be an object" },
  "metadata": { "rpc_method": "skills.enterprise.source.search", "transport": "web-http" }
}
```

其他业务错误复用 §5.4；分发/执行失败复用 A3。以上示例不代表执行过线上搜索请求。

---

## Part C — 安全与错误契约

### C1 验签契约

本节适用于 §2.3 的 Provider 制品安装和 §3.2 的更新；不适用于 §5.2 的 URL 安装。Provider 制品提供摘要时先进行 SHA-256 摘要校验，随后调用 `verify_artifact()`；Provider 可覆写验签方案。默认策略为 `if-present`：无签名时可不产生验签审计记录；存在签名或 Provider 要求验签时必须通过对应校验。不能将所有安装入口描述为统一强制验签。§5.2 的 URL 安装使用可选的原始归档字节 HMAC，详见该节。

**默认契约（skillhub-artifact-v1）**：签名原文为 `scope\nskill_id\nversion_id\nartifact_sha256` 四段拼接（`\n` 为换行符），算法 `HmacSHA256`，编码 `hex-lower`。

成功验签的 Provider 安装记录保存 `verification` 审计摘要，不保存 key 原文或签名原文。URL 兼容安装记录的 `origin` 为传入 URL，不能假定它具有同一套 Provider 验签审计字段。

**`verification` 审计摘要字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| verified | 是 | bool | 验签是否通过 |
| artifact_sha256 | 是 | string | 实际计算的 SHA-256 |
| algorithm | 是 | string | 实际通过校验的算法标识 |
| scope | 否 | string | 签名域标识 |
| key_id | 是 | string | 实际使用的 key 标识 |
| verified_at | 是 | string | 验签成功时间，ISO-8601 |

### C2 业务错误码

下表为通用 Provider 接口的业务码参考；企业 URL 安装、卸载及来源搜索的具体错误分支以 §4.1、§5.2、§5.4 为准。例如企业卸载未找到记录使用 `not_found`，不是通用 `skill_not_found`。业务码位于 `data.error_code`；与 A3 的外层 `error.code` 分开判断。

| error_code | 场景 |
| --- | --- |
| `workspace_unavailable` | workspace 无法解析或挂载 |
| `skill_not_found` | 本地安装不存在 |
| `skill_already_installed` | 同名 Skill 已安装且未带 `force` 重装 |
| `skill_name_conflict` | 同名但目标为不可覆盖的预置 Skill |
| `skill_version_conflict` | 更新乐观并发守卫失败 |
| `source_not_found` | 来源未注册 |
| `source_misconfigured` | 来源配置非法 |
| `source_disabled` | 来源已禁用 |
| `source_capability_unsupported` | 来源未声明所需能力 |
| `source_unavailable` | Provider 未启动/异常 |
| `source_response_invalid` | Provider 缺少稳定 ID 或关键字段 |
| `update_check_failed` | 更新检查失败（保留本地状态） |
| `artifact_descriptor_invalid` | 制品描述缺 URL/版本/安全字段 |
| `artifact_download_failed` | 下载失败或大小不匹配 |
| `artifact_checksum_mismatch` | 摘要不匹配 |
| `artifact_signature_invalid` | 来源验签失败 |
| `prebuilt_not_removable` | 卸载预置技能 |


### C3 源码核对入口

以下均为本仓库相对链接：

- [Gateway HTTP 路由表](../channel_manager/web/web_http_routes.py)：六个企业路由及各自 Query/Body 声明；通用来源安装为 `/skills/sources/actions/install`。
- [HTTP 参数与响应适配](../channel_manager/web/web_http_app.py)：`_params_from_mapped_route`、`_merge_header_params`、`_envelope_from_res`。
- [HTTP 身份分发](../channel_manager/web/web_http_dispatch.py)：`dispatch_http_request`、`_trust_client_tenant_headers`。
- [AgentServer handler 映射](../../server/runtime/agent_adapter/interface.py)：`_SKILL_ROUTES`、`_handle_skills_request`。
- [SkillManager 实现](../../server/runtime/skill/skill_manager.py)：`handle_skills_enterprise_list`、`handle_skills_web_install`、`handle_skills_web_uninstall`、`handle_skills_source_providers`、`handle_skills_source_search`。
- [来源 DTO](../../extensions/sdk/skill_source.py)：`SourceDescriptor.to_dict()`、`SkillCandidate.to_dict()`。

上述路由是 Gateway 对外 Web HTTP 契约，不能用 AgentServer 南向 REST 路径（例如 `/skills/enterprise/install`）替换。部署验收应核对该版本 Gateway 的 OpenAPI；本次文档修改不新增任何接口。
