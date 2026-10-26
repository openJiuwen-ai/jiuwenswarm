# 企业租户 Skill 管理 HTTP 接口文档

## 0. 概述

> **读者**：企业版（`gateway.edition = enterprise`）接入方——浏览器、BFF 或经 Ingress / 反向代理访问 Gateway 的 HTTP 客户端。
> **范围**：企业租户 Skill 管理的 HTTP 接口。本文描述企业版前端实际调用、且代码已实现落地的接口；个人版专属接口（marketplace 安装、本地导入、启停等）不在本文范围。
> **基线**：`dev-stable`，以当前已落地实现为准。
> **证据原则**：标注「代码行为」的描述均可回溯至源码；未实现项写「代码未定义」，不当作已冻结承诺。产品排期、网关转发规则、部署鉴权方式等以运行中 `/openapi.json` 与部署约定为准，本文不承诺未落地行为。

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

### A2 身份与请求头

| 请求头 | 是否需要 | 说明 |
| --- | --- | --- |
| `Content-Type: application/json` | 写操作 | Body 为业务 JSON 对象 |
| `X-Bot-Id` | 企业版需要 | 写入路由身份（选中的机器人） |
| `X-User-Id` | 企业版需要 | 当前调用用户身份 |
| `X-Group-Id` | 企业版需要 | 当前组织 / 群组身份 |
| `X-Session-Id` | 可选 | 请求会话标识；不传时生成临时 `webhttp_*` 会话。调用方应复用当前会话标识，避免每次请求创建独立会话、占用服务端会话资源 |
| `X-Request-Id` | 可选 | 缺省生成 `uuid4().hex`；响应头和信封回显 |
| `Authorization` | 取决于上游认证 | 当前 Web HTTP 应用层不校验 Bearer；鉴权由 Ingress / 壳层 / 反向代理完成 |

`group_id + bot_id + user_id` 为受信路由三元组，同一三元组的 workspace 目录与 `SkillManager` 实例由服务端按需创建并复用。`X-User-Id` / `X-Group-Id` / `X-Bot-Id` / `X-Session-Id` 也可通过同名 Query 参数传递；同名时 Query 优先，头只补齐未提供的值。`GATEWAY_WEB_HTTP_TRUST_CLIENT_HEADERS` 可显式开关租户头信任，未设置时企业版信任上游身份头。

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

失败信封（HTTP 4xx / 5xx，`error` 为对象）：

```json
{
  "request_id": "req-skill-002",
  "ok": false,
  "error": { "code": "NOT_FOUND", "message": "skill not found", "details": {} },
  "metadata": { "rpc_method": "skills.list", "transport": "web-http" }
}
```

响应头包括 `X-Request-Id`、`X-Web-RPC-Method`。通用错误映射：`BAD_REQUEST→400`、`UNAUTHORIZED→401`、`FORBIDDEN→403`、`NOT_FOUND/METHOD_NOT_FOUND→404`、`CONFLICT→409`、`TIMEOUT→504`、`SERVICE_UNAVAILABLE→503`；其他码默认 HTTP 500。FastAPI 请求体类型 / JSON 校验失败可能先返回 422 `detail`，不经过上述业务信封。

**业务失败**（Skill handler 特有）：仍返回 HTTP 200、`ok=true`，失败信息在 `data` 内，需检查 `data.success` / `data.error_code`（稳定业务错误码见 Part C）：

```json
{
  "request_id": "req-skill-003",
  "ok": true,
  "data": { "success": false, "error_code": "skill_not_found", "error_message": "skill not found" },
  "metadata": { "rpc_method": "skills.list", "transport": "web-http" }
}
```

### A4 WS 方法名与 HTTP 映射

| method | HTTP 方法 | HTTP 路径 |
| --- | --- | --- |
| skills.list | GET | /skills |
| skills.get | GET | /skills/{name} |
| skills.source.providers | GET | /skills/sources |
| skills.source.search | GET | /skills/sources/search |
| skills.source.install | POST | /skills/sources/actions/install |
| skills.updates.check | GET | /skills/updates |
| skills.update | POST | /skills/actions/update |
| skills.enterprise.uninstall | POST | /skills/enterprise/actions/uninstall |

---

## Part B — 接口定义

### 1. 列表与详情

#### 1.1 `GET /skills`（skills.list）

**含义**：发现型只读列表。企业版扫描 workspace 本地技能目录并补充安装状态，不返回个人版 marketplace 项，也不在请求主路径触发远端更新检查。

**Query 参数**

| key | 必填 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| refresh_marketplaces | 否 | bool | false | 企业版不生效（个人版市场刷新语义），无需传 |
| with_installed | 否 | bool | false | 同时返回 `plugins` |
| session_id | 否 | string | 自动生成 `webhttp_*` 临时会话 | 请求会话标识，建议复用当前会话；见 A2 |

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| skills | 是 | array | 技能数组，企业版只含已安装的 prebuilt/user，元素见下表 |
| plugins | 否 | array | 仅 `with_installed=true` 时返回；由安装记录生成，企业 workspace 通常为空 |

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
| source | 是 | string | 来源展示名；有 `source_id` 时用后者定位 Provider |
| installed | 否 | bool | 技能实体状态；有安装记录时可能反映实体不一致 |
| enabled | 否 | bool | 启用状态；两版均返回，启停控制仅个人版前端使用，企业版前端不消费 |
| is_builtin | 否 | bool | 是否按内置技能处理；企业版不提供仓库内置技能，恒为 `false` |
| is_builtin_source | 否 | bool | 内置源目录是否存在同名技能；个人版内置浏览用，企业版恒为 `false` |
| has_evolutions | 否 | bool | 是否存在 `evolutions.json`；个人版"查看演进"入口使用，企业版前端不消费 |
| display_name | 否 | string | 展示名（保留来源原始大小写） |
| market_display_name | 否 | string | 市场展示名（安装记录回填） |
| origin | 否 | string | 安装来源追溯；有安装记录时返回，同名时可用于详情优先定位 |
| installation_id | 否 | string | 安装记录 ID；有安装记录时返回 |
| source_type | 否 | string | 安装记录中的类型：`prebuilt` / `user`；有安装记录时返回 |
| source_id | 否 | string | 来源定位参数（install/update 指定来源）；有安装记录时返回 |
| skill_id | 否 | string | 来源内 Skill ID（install/update 定位键）；有安装记录时返回 |
| version_id | 否 | string | 精确制品定位参数（update 用它做乐观并发）；有安装记录时返回 |
| installed_at | 否 | string | 安装时间（ISO-8601），安装/覆盖时写入 |
| updated_at | 否 | string | 最近更新时间（ISO-8601），更新操作写入 |
| removable | 否 | bool | 是否允许当前调用方卸载；`prebuilt` 固定 `false`，仅 `user` 可卸载 |
| consistency | 否 | string | 安装一致性：`ok` / `inconsistent`；有安装记录时返回 |
| sync_status | 否 | string | prebuilt 同步状态：`synced` / `failed`；仅 prebuilt 记录出现 |

元素字段不限于上表：SKILL.md frontmatter 中的键会**全量透传**进列表项（仅 `name` 缺省取目录名，`description` / `version` / `author` 缺省补空串，`tags` / `allowed_tools` 规整为数组）。frontmatter 最少可只写 `name` 和 `description`，此时列表项就是这两个字段加上表中的运行时回填字段。

**响应示例**：

```json
{
  "request_id": "req-001",
  "ok": true,
  "data": {
    "skills": [
      {
        "name": "meeting_book",
        "description": "预定会议。当用户说“预定会议”“创建会议”“安排会议”时调用。",
        "version": "1.0.0",
        "author": "",
        "tags": [],
        "allowed_tools": [],
        "path": "/home/app/.jiuwenswarm/workspace_xxx/agent/workspace/skills/meeting_book/SKILL.md",
        "source": "swarmskillhub",
        "display_name": "meeting_book",
        "installed": true,
        "enabled": true,
        "installation_id": "4aae2feb-661f-433e-9a65-abc3b2844cfa",
        "source_type": "user",
        "source_id": "swarmskillhub",
        "skill_id": "42",
        "version_id": "1024",
        "origin": "swarmskillhub:42",
        "installed_at": "2026-09-10T08:00:00+00:00",
        "updated_at": "2026-09-10T08:00:00+00:00",
        "removable": true,
        "consistency": "ok",
        "market_display_name": "会议预定",
        "is_builtin": false,
        "is_builtin_source": false,
        "has_evolutions": false
      }
    ],
    "plugins": []
  },
  "metadata": { "rpc_method": "skills.list", "transport": "web-http" }
}
```

#### 1.2 `GET /skills/{name}`（skills.get）

**含义**：按清单 `name` 取单个 Skill 详情；相对列表额外返回正文 `content` 与 `file_path`。

**路径参数**：`name`（必填，技能名）。

**Query 参数**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| origin | 否 | string | 安装身份标识；匹配时优先定位，未匹配时回退到第一个同名技能 |
| session_id | 否 | string | 请求会话标识；省略时生成临时 `webhttp_*` 会话，见 A2 |

**响应 `data` 字段**：包含 §1.1 列表元素的全部字段（frontmatter 透传规则相同，见 §1.1 表尾说明），路径字段在详情中改名为 `file_path`（列表中为 `path`），另有详情专属字段：

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| content | 是 | string | `SKILL.md` 正文 |
| file_path | 是 | string | `SKILL.md` 路径 |
| config | 是 | object | 启停配置回写，形如 `{ "enabled": bool }`；启停功能仅个人版提供，企业版可忽略 |

未找到时 handler 抛出异常，由分发层按 A3 的 `ok=false` 信封返回；同名多命中且未传 `origin` 时取第一个匹配项，不返回专用的模糊匹配错误。

---

### 2. 技能源

#### 2.1 `GET /skills/sources`（skills.source.providers）

**含义**：列出当前已注册的技能源与能力声明，包含禁用来源（由 `enabled` 区分）。响应不返回 endpoint、token、密钥或公钥原文。

**入参**：无业务必填项；可选 Query `session_id`（请求会话标识，见 A2）。

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
| enabled | 是 | bool | 是否启用；禁用来源仍列出，但不能搜索或安装 |
| priority | 是 | int | 展示优先级 |
| capabilities | 是 | string[] | `search/check_updates/get_artifact` |

#### 2.2 `GET /skills/sources/search`（skills.source.search）

**含义**：按 `source_id` 调度 Provider 搜索可安装 Skill，支持关键词与分页。签名、HMAC key、Bearer token、预签名 URL 与服务器路径不属于标准候选字段；Provider 不应在扩展 `metadata` 中放入凭据。

**Query 参数**

| key | 必填 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| source_id | 是 | string | — | 已注册来源 ID |
| q | 否 | string | `""` | 搜索关键词 |
| page | 否 | int | 1 | 页码 |
| page_size | 否 | int | 20 | 每页数量，1–100 |
| session_id | 否 | string | — | 请求会话标识，见 A2 |

**成功响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| success | 是 | bool | 成功时为 `true` |
| source | 是 | string | `source_id` 的兼容别名 |
| source_id | 是 | string | 来源回显 |
| items | 是 | array | 候选数组，元素见下表 |
| skills | 是 | array | 与 `items` 相同的兼容别名 |
| total | 是 | int/null | Provider 返回的总数；未知时为 `null` |
| page | 是 | int | 当前页 |
| page_size | 是 | int | 当前页大小 |
| count | 是 | int | 本页数量 |
| next_page | 是 | int/null | 下一页；无更多时为 `null` |

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
| slug | 否 | string | 来源提供的短标识 |
| canonical_slug | 否 | string | 来源提供的规范标识 |
| owner_display_name | 否 | string | 作者展示名 |
| labels | 否 | array | 标签 |
| homepage | 否 | string | 来源提供的主页地址 |
| rating_avg | 否 | number | 平均评分 |
| rating_count | 否 | int | 评分人数 |
| download_count | 否 | int | 下载量 |
| updated_at | 否 | string/number | 远端更新时间 |
| versions | 否 | array | 可选版本摘要 |
| previous_version | 否 | string | 来源提供的上一版本 |
| accessible | 否 | bool | 是否可访问 |
| downloadable | 否 | bool | 是否可下载 |
| metadata | 否 | object | Provider 返回的扩展元数据；空对象时省略 |

#### 2.3 `POST /skills/sources/actions/install`（skills.source.install）

**含义**：安装 Skill 到 workspace。

**Body**

| key | 必填 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| source_id | 是 | string | — | 稳定来源 ID |
| skill_id | 是 | string | — | 来源内 Skill ID，安装定位键 |
| version_id | 是 | string | — | 精确制品版本 ID |
| force | 否 | bool | false | 同名技能是否覆盖重装；不带 `force` 且同名已安装返回 `skill_already_installed`；预置 Skill 不可覆盖 |
| display_name | 否 | string | `""` | 市场展示名，写入安装记录 |
| version | 否 | string | `""` | 展示版本；制品描述未提供时作为版本回退值 |
| author | 否 | string | `""` | 作者展示名；制品描述未提供时作为回退值 |
| session_id | 否 | string | — | 请求会话标识（该路由未声明 `session_id` Query 参数，可在 Body 或 `X-Session-Id` 头传递），见 A2 |

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| success | 是 | bool | 成功时为 `true`；业务失败时为 `false` |
| skill | 成功时 | object | 安装记录的字段子集（值为 null 的键省略），固定为：`installation_id` / `name` / `declared_name` / `source_type` / `source_id` / `skill_id` / `version_id` / `version` / `enabled` / `installed` / `removable` / `consistency` / `author` |

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

**含义**：按 workspace 安装记录批量检查更新。

**Query 参数**

| key | 必填 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| source_id | 否 | string | 全部具备来源及版本定位信息的安装记录 | 限定检查单个来源 |
| session_id | 否 | string | — | 请求会话标识，见 A2 |

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| success | 是 | bool | 更新检查请求成功时为 `true`；单个来源检查失败仍会体现在对应 `items` 条目中 |
| items | 是 | array | 更新状态数组，元素见下表 |
| count | 是 | int | `items` 的数量 |

**`items` 元素**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| source_id | 是 | string | 来源 ID |
| skill_id | 是 | string | 来源内 Skill ID，更新定位键 |
| installation_id | 否 | string/null | workspace 安装记录 ID；无值时成功分支省略，来源检查失败分支可能为 `null` |
| name | 否 | string/null | 技能名；记录未提供该字段时可能省略或为 `null` |
| current_version_id | 是 | string | 当前制品版本 ID |
| current_version | 否 | string | 当前展示版本 |
| latest_version_id | 否 | string | 来源端最新制品版本 ID；未提供或来源检查失败时省略 |
| latest_version | 否 | string | 来源端最新展示版本；未提供或来源检查失败时省略 |
| has_update | 是 | bool | 是否存在更新 |
| fingerprint_matched | 否 | bool | 内容指纹是否一致 |
| accessible | 否 | bool | 是否可访问 |
| downloadable | 否 | bool | 是否可下载 |
| remote_status | 否 | string | `available/no_access/not_downloadable/removed/unknown` |
| error_code | 否 | string | 本条检查失败原因 |
| checked_at | 是 | string | 本次检查时间，UTC ISO-8601 |

`current_*` 表示本地当前安装版本，`latest_*` 表示来源端最新版本；两组字段可以同时出现，HTTP 不会将其互相改名。

#### 3.2 `POST /skills/actions/update`（skills.update）

**含义**：按 `source_id` + `skill_id` 定位已安装 Skill 并更新到指定精确版本。更新时重新获取短期下载描述，不复用搜索/检查结果。

**Body**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| source_id | 是 | string | 稳定来源 ID |
| skill_id | 是 | string | 来源内 Skill ID，更新定位键 |
| target_version_id | 是 | string | 目标精确制品版本 ID；缺省时回退读取 `version_id` 参数 |
| expected_current_version_id | 否 | string | 乐观并发条件；与当前安装版本不一致返回 `skill_version_conflict` |
| session_id | 否 | string | 请求会话标识（该路由未声明 `session_id` Query 参数，可在 Body 或 `X-Session-Id` 头传递），见 A2 |

更新内部委托安装流程执行，因此也接受 §2.3 的 `display_name` / `version` / `author` 展示字段，随更新写进安装记录。

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| success | 是 | bool | 成功时为 `true`；业务失败时为 `false` |
| skill | 成功时 | object | 见下方两分支说明 |

`skill` 按是否实际需要安装分两种形态：

- **目标版本与当前已装版本相同**（`target_version_id` 等于账本中的当前 `version_id`）：不重新下载安装，幂等返回当前安装 DTO——账本记录全字段（`installation_id` / `name` / `declared_name` / `entity_dir` / `source_type` / `source` / `origin` / `version` / `installed_at` / `updated_at` / `enabled`，及可选的 `source_id` / `skill_id` / `version_id` / `fingerprint` / `market_display_name` / `author` / `verification`）外加运行时字段 `installed` / `removable` / `consistency`（prebuilt 记录另有 `sync_status`）；
- **目标版本不同**：走 §2.3 安装流程强制覆盖安装，返回其固定 13 键字段子集（`installation_id` / `name` / `declared_name` / `source_type` / `source_id` / `skill_id` / `version_id` / `version` / `enabled` / `installed` / `removable` / `consistency` / `author`），`updated_at` 随之刷新。

**成功示例**（HTTP 200，从 1.0.0 更新到 2.0.0）：

```json
{
  "request_id": "req-update-001",
  "ok": true,
  "data": {
    "success": true,
    "skill": {
      "installation_id": "a1cfe797-a463-4649-81a5-24b595aa45b0",
      "name": "perm-probe-diff",
      "declared_name": "perm-probe-diff",
      "source_type": "user",
      "source_id": "swarmskillhub",
      "skill_id": "f640dc443079411eb899706622cf1189",
      "version_id": "2.0.0",
      "version": "2.0.0",
      "enabled": true,
      "installed": true,
      "removable": true,
      "consistency": "ok",
      "author": "huben"
    }
  },
  "metadata": { "rpc_method": "skills.update", "transport": "web-http" }
}
```

**失败示例**（HTTP 200；乐观并发冲突）：

```json
{
  "request_id": "req-update-002",
  "ok": true,
  "data": { "success": false, "error_code": "skill_version_conflict", "error_message": "expected 1.0.0, current version is 2.0.0" },
  "metadata": { "rpc_method": "skills.update", "transport": "web-http" }
}
```

---

### 4. 卸载

#### 4.1 `POST /skills/enterprise/actions/uninstall`（skills.enterprise.uninstall）

**含义**：卸载当前 workspace 中用户自装的 Skill。预置（prebuilt）技能不可卸载。

**Body**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| name | 是 | string | 技能名 |
| origin | 否 | string | 安装身份标识；提供时按 origin 在账本中精确匹配，需恰好一条记录，否则返回 `not_found` |
| session_id | 否 | string | 请求会话标识（该路由未声明 `session_id` Query 参数，可在 Body 或 `X-Session-Id` 头传递），见 A2 |

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| success | 是 | bool | 成功时为 `true` |
| name | 成功时 | string | 实际卸载的技能名 |

失败时按 A3 业务失败形态返回 `data.error_code` / `data.error_message`（如 `missing_params` / `invalid_skill_name` / `missing_tenant` / `not_found` / `prebuilt_not_removable`）。

**请求示例**：

```json
{ "name": "meeting_book" }
```

---

## Part C — 错误码

### C1 稳定错误码

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
