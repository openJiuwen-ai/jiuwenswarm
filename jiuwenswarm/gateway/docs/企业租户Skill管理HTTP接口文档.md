# 企业租户 Skill 管理 HTTP 接口文档

## 0. 概述

> **读者**：企业版（`gateway.edition = enterprise`）接入方——浏览器、BFF 或经 Ingress / 反向代理访问 Gateway 的 HTTP 客户端。
> **范围**：企业租户 Skill 管理的 HTTP 接口。本文描述企业版前端实际调用、且代码已实现落地的接口；个人版专属接口（marketplace 安装、本地导入、启停等）不在本文范围。
> **基线**：`dev-stable`，以当前已落地实现为准。

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

企业接口通过受信 `X-*` 头或 Query 参数定位 workspace；`group_id + bot_id + user_id` 为受信路由三元组，可由部署入口的认证代理 / BFF 注入。

| 字段 / HTTP 头 | 类型 | 说明 |
|----------------|------|------|
| `X-Bot-Id` / `bot_id` | string | 选中的机器人身份 |
| `X-User-Id` / `user_id` | string | 当前调用用户 |
| `X-Group-Id` / `group_id` | string | 当前组织 / 群组身份 |
| `service_id` | string | 租户 service 标识 |
| `agent_id` | string | 目标 Agent |
| `X-Request-Id` | string | 请求关联标识；缺省生成 UUID hex |

同一三元组的 workspace 目录与 `SkillManager` 实例由服务端按需创建并复用。

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

**失败**（HTTP 200，`ok=false` 或 HTTP 错误码）：

```json
{
  "request_id": "req-skill-002",
  "ok": false,
  "error": "skill not found",
  "data": { "success": false, "error_code": "skill_not_found" }
}
```

失败时读 `error`（文字说明）与 `data.error_code`（稳定业务错误码，见 Part C）。注意：业务失败可能返回 HTTP 200、`ok=true`，需检查 `data.success` / `data.error_code`。

### A4 WS 方法名与 HTTP 映射

| method | HTTP 方法 | HTTP 路径 |
| --- | --- | --- |
| skills.list | GET | /skills |
| skills.get | GET | /skills/{name} |
| skills.source.providers | GET | /skills/sources |
| skills.source.search | GET | /skills/sources/search |
| skills.source.install | POST | /skills/sources/install |
| skills.updates.check | GET | /skills/updates |
| skills.update | POST | /skills/actions/update |
| skills.enterprise.uninstall | POST | /skills/enterprise/actions/uninstall |

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

**含义**：列出当前已注册、已启用的技能源与能力声明。响应不返回 endpoint、token、密钥或公钥原文。

**入参**：无业务必填项。

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| providers | 是 | array | 技能源数组 |

**`providers` 元素**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| source_id | 是 | string | 稳定来源 ID |
| display_name | 否 | string | 展示名 |
| enabled | 否 | bool | 是否启用 |
| priority | 否 | int | 展示顺序 |
| capabilities | 是 | string[] | `search/check_updates/get_artifact` |
| verification_required | 是 | bool | 生产来源是否强制验签；不返回 key 内容 |
| signature_mode | 是 | string | 验签模式标识，取值由 Provider 定义 |
| allowed_algorithms | 是 | string[] | 来源允许的算法标识集合，取值由 Provider 定义 |

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

#### 2.3 `POST /skills/sources/install`（skills.source.install）

**含义**：按 `skill_id` 定位并安装指定来源的精确制品版本。安装时调用 Provider `get_artifact` 获取制品描述，再由平台公共管道完成下载、SHA-256 摘要校验与验签（默认 HMAC 契约，Provider 可覆写 `verify_artifact`），最后安全解压、实体与 JSON 原子提交。安装身份以下载后解析的 `SKILL.md.name` 为准，同名直接覆盖。

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

#### 4.1 `POST /skills/enterprise/actions/uninstall`（skills.enterprise.uninstall）

**含义**：卸载当前 workspace 中用户自装的 Skill。预置（prebuilt）技能不可卸载。

**Body**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| name | 是 | string | 技能名 |
| origin | 否 | string | 安装身份标识，消歧 |

**响应 `data` 字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| success | 是 | bool | 固定 `true` |
| detail | 否 | string | 结果说明 |

**请求示例**：

```json
{ "name": "meeting_book" }
```

---

## Part C — 安全与错误契约

### C1 验签契约

平台在安装事务中先校验原始 ZIP 的 SHA-256（与 Provider 上报的 `artifact_sha256` 比对），摘要不一致即拒绝。验签由 SPI 方法 `verify_artifact()` 承担，平台提供默认实现（`skillhub-artifact-v1` HMAC 契约），Provider 可覆写接入自定义验签方案。缺字段或验证失败一律 fail-closed。

**默认契约（skillhub-artifact-v1）**：签名原文为 `scope\nskill_id\nversion_id\nartifact_sha256` 四段拼接（`\n` 为换行符），算法 `HmacSHA256`，编码 `hex-lower`。

安装记录保存 `verification` 审计摘要，不保存 key 原文、签名原文或短期 URL。

**`verification` 审计摘要字段**

| key | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| verified | 是 | bool | 验签是否通过 |
| artifact_sha256 | 是 | string | 实际计算的 SHA-256 |
| algorithm | 是 | string | 实际通过校验的算法标识 |
| scope | 否 | string | 签名域标识 |
| key_id | 是 | string | 实际使用的 key 标识 |
| verified_at | 是 | string | 验签成功时间，ISO-8601 |

### C2 稳定错误码

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
