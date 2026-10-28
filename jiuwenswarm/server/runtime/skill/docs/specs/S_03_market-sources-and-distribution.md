# 技能源体系与市场分发

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_03 / market-sources-and-distribution |
| 关联模块 | jiuwenswarm/server/runtime/skill/{source_registry.py, artifact_security.py} + skill_manager.py 市场渠道段 |
| 最近一次修订日期 | 2026-10-16 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

技能**从哪来**：六个内置安装渠道（SkillNet / ClawHub / TeamSkillsHub / builtin / web / import_local）+ Source SPI 扩展面（SourceRegistry 注册表 + Source Provider 协议）、产物完整性验证（artifact_security）、下载白名单与安全解压。账本与事务见 [[S_02]]；企业预置（管理面模板硬同步）见 [[S_04]]；面向模型的工具封装（search_skill / install_skill / uninstall_skill）见 [[S_06]]。

## 技能源全景

```
                    ┌─ SourceRegistry（SPI 注册表，扩展包按 provider_type 提供）──┐
                    │  扩展经 bind_extension(config, factory) 注册 SkillSourceProvider │
AgentRequest ──────►├─ 内置渠道（SkillManager 自带，不走 SPI）：                     │
 skills.* RPC       │  SkillNet（github 文件夹，异步）/ ClawHub（token+slug）       │
                    │  TeamSkillsHub（asset_id）/ builtin（仓库内置目录）    │
                    │  web（企业 URL 直装）/ import_local（本地路径或 URL 导入）     │
                    └──────────────────────────────────────────────────────────────┘
```

**新渠道优先走 Source SPI**（扩展包 + SourceRegistry），不要继续往 skill_manager.py 堆渠道 handler——该文件已过大。

## SourceRegistry（source_registry.py，SPI 核心）

| 机制 | 契约 |
|---|---|
| ID 规则 | `source_id` / `provider_type` 必须匹配 `_ID_RE = ^[a-z0-9][a-z0-9-]{0,63}$`；`source_id` 去重（重复 → `SourceRegistryError("source_misconfigured")`） |
| 能力校验 | config 声明的 `capabilities`（search / get_artifact / check_updates 等）必须是 provider 实际实现能力的**子集**，缺口报错 |
| 懒启动 | `get(source_id, capability)` 三重校验（存在 → enabled → capability）+ `start_lock` 保证并发下 `provider.start()` 只执行一次；启动失败 → `source_unavailable` |
| 白名单合并 | `_merge_provider_download_hosts` **fail-closed**：管理面显式 `allowed_hosts` 优先 → Provider 声明的 `download_allowed_hosts` 默认 → 两者皆空 = **保持为空 = 拒绝下载** |
| 默认源 | `SkillManager._register_default_sources` 问 `ExtensionRegistry.list_skill_source_extensions()` 要 `default_source_config()`（返回 None 即不注册）；扩展系统未运行（单测直构 manager）时无源 |
| 热更新 | `apply_skill_source_configs`（SkillManager 方法）原子应用 `custom_config.skill_sources`：构建候选 SourceRegistry（默认源 + 配置源同表绑定，配置源 source_id 跳过默认源）→ 原子切换 `self._source_registry` → 关闭旧表。**没有整表替换的独立 API** |

SPI 协议（`jiuwenswarm/extensions/sdk/skill_source.py`）：`SkillSourceProvider` + `SourceConfig` + `SkillSourceExtension` 工厂 + `ArtifactDescriptor` / `DownloadPolicy` / `InstalledArtifact` / `SkillRef` / `SkillSearchRequest` / `ProviderInvocationContext`。`verify_artifact` 的默认实现委托 `artifact_security.verify_skillhub_artifact`（`TrustPolicy(verification="if-present")` + `EnvironmentSecretResolver`），Provider 可覆写。

## 产物完整性验证（artifact_security.py）

**定位：完整性与来源验证，不是恶意内容扫描**（不查病毒 / 恶意模式）。

冻结常量：`_HMAC_ALGORITHM="HmacSHA256"` / `_HMAC_ENCODING="hex-lower"` / `_HMAC_SCOPE="skillhub-artifact-v1"`。

`verify_skillhub_artifact(descriptor, body, *, trust_policy, secret_resolver)` 全程 fail-closed，失败抛 `ArtifactVerificationError(code, message)`：

1. body 实际 SHA-256 计算；declared sha256 格式非法 → `artifact_descriptor_invalid`
2. 摘要比对（`hmac.compare_digest` 防时序）失败 → `artifact_checksum_mismatch`
3. 无签名：`trust_policy.verification=="required"` → 拒绝（`artifact_signature_invalid`）；`"if-present"` → 容忍返回 `verified=False`
4. 有签名：算法 / encoding / scope 必须全匹配、签名为 64 位小写 hex、`key_id` 必须能从 `trust_policy.hmac_key_refs` 解析出密钥
5. 签名输入 = **四行拼接** `"{scope}\n{skill_id}\n{version_id}\n{declared_sha256}"`，重算 HMAC 比对

密钥解析器：`MappingSecretResolver`（字典）/ `EnvironmentSecretResolver`（只认 `env://NAME`，变量名须匹配 `[A-Z][A-Z0-9_]{0,127}`）。成功返回 `VerificationResult`（frozen），仅 `verified=True` 时上层落审计账（`verification.checksum_sha256` 等）。

## 下载白名单与安全解压

### 白名单三断言

| 断言 | 白名单来源 | 用途 |
|---|---|---|
| `_assert_skill_download_url_allowed` | 通用（含 source provider 路径） | 所有远程下载的前置断言 |
| `_assert_team_skills_hub_download_url_allowed` | `_get_team_skills_hub_allowed_download_hosts()` | TeamSkillsHub 资产下载 |
| `_assert_import_local_download_url_allowed` | `_get_import_local_allowed_download_hosts()` | import_local 的 URL 形态 |

host 匹配走 `_team_skills_hub_host_matches_rule`（支持规则式匹配）；`_is_http_download_target` 判定是否走 HTTP 下载；`_is_github_skill_folder_url` 识别 github 文件夹 URL（转 SkillNet 链路）。**绕过断言直接 `_download_http_archive_bytes_sync` 是违规**。

### 安全解压（zip-slip 防护）

`_safe_extract_zip_members_into` / `_safe_extract_zip_bytes_to_dir` / `_safe_extract_tar_to_dir` / `_detect_archive_format`（zip / tar 识别）+ `_extract_archive_bytes_to_dir` 统一分发。所有解压必须走这组函数——成员路径先归一再验证落在 dest 内，禁止直接 `zf.extractall`。

## 内置渠道契约

### SkillNet（github 文件夹技能，**异步安装**）

- `handle_skills_skillnet_search`：默认走 vector 模式（工具层 [[S_06]] 传 `params["mode"]="vector"`，多关键词召回更稳）。
- `handle_skills_skillnet_install` **立即返回 `install_id`**，后台 `_skillnet_install_background` 执行 `_skillnet_download_sync`（github api / mirror 下载，`SkillNetInstallError` / `SkillNetEmptyDownloadError` 分型，`_map_github_api_error` 映射错误上下文）。
- 任务表 `_SKILLNET_INSTALL_JOBS` 是**模块级进程共享**（多 manager 实例互通）；`handle_skills_skillnet_install_status` 查询进度；完成后 `set_skillnet_install_complete_hook` 回调（重载 Agent）。
- 代理与 TLS：`_skillnet_proxy_mapping` / `_configure_skillnet_requests_session` / `_get_free_search_proxy_url`；github token 经 `_get_github_token()`。

### ClawHub

token 三件套（`handle_skills_clawhub_get_token / set_token`，token 存账本侧并 `_mask_clawhub_token` 脱敏）+ `search` + `download`（owner/slug 拆分）。**目录名 = slug**（重名技能 name 撞车时按 slug 判存活，见 [[S_02]] 对账）。

### TeamSkillsHub

`info / init / validate / pack / search / install / publish / delete`。发布侧 `_prepare_teamskills_publish_zip`（技能目录 → zip）；认证 `_resolve_teamskills_hub_auth`；HTTP 错误 `_normalize_teamskills_hub_http_error`。安装以 `asset_id` 为标识。

### builtin / web / import_local

- **builtin**：仓库内置技能目录（`get_builtin_skills_dir()`，`_scan_builtin_skills` 扫描）；`handle_skills_install_builtin` 把 builtin 技能复制安装到工作区。企业版不提供 builtin（[[S_02]] N5）。
- **web（企业）**：`skills.enterprise.install` RPC 路由到 `handle_skills_web_install` → `_download_web_skill_bytes` → `_install_web_skill_dir` → `_commit_web_skill_install`（失败回滚删目录）；`protected_source_types={user}` 保护用户技能身份。
- **import_local**：`_import_local_from_path(src, *, force, origin)` 支持本地目录 / zip / URL（URL 走 import_local 白名单断言 + 下载）。

### marketplace（git 仓库市场）

`_marketplace/` 目录下的 git clone 集：`handle_skills_marketplace_add` → `_git_clone`；`_sync_marketplace_repos` → `_git_pull` + `_git_get_commit`；`_scan_marketplace_skills` 扫描市场内技能。mirror 目录支持 `_get_mirror_skills_dirs`。

## 不变量

- **N1（fail-closed 白名单）**：下载白名单空的 source 不可下载；管理面未配置时 Provider 必须显式声明默认白名单，否则该源只可搜索不可安装。
- **N2（验证先于落盘）**：带声明的产物（checksum / signature）必须先验证后解压提交；`verification` 结果落账（`record_skill_installation` 的审计字段）。
- **N3（解压必走安全函数）**：任何渠道的 zip/tar 解压必须经 `_safe_extract_*` 家族；新增渠道不得自写解压。
- **N4（异步任务进程共享）**：SkillNet 任务表模块级共享；`install_status` 轮询与后台任务跨 manager 实例可见。改任务结构需兼容多实例并发写。
- **N5（渠道错误码稳定）**：`source_misconfigured` / `source_unavailable` / `artifact_descriptor_invalid` / `artifact_checksum_mismatch` / `artifact_signature_invalid` 是对外契约（前端 / 工具层展示依赖），重命名 = 破坏性变更。

## 修改检查单

- 新增 Source Provider → 走扩展包 + SPI（`extensions/sdk/skill_source.py`），核对 capabilities 声明 ⊆ 实现、白名单合并语义（N1）。
- 改 HMAC 契约（算法 / scope / 签名输入格式）→ 属**破坏性变更**：`_HMAC_SCOPE` 是冻结作用域，发布侧与验证侧必须同步升级。
- 改下载逻辑 → 核对三个白名单断言点未被绕过；新增下载入口必须先加断言。
- 改 SkillNet 异步链路 → 确认 install_status 轮询（工具层 0.5s 间隔）、complete_hook、多实例任务表共享三件事都未破坏。
- 改 zip/tar 解压 → 跑 zip-slip 用例（含 `..` 穿越、绝对路径成员、Windows 盘符成员）。

## 与其它 spec 的关系

- 账本落账与事务见 [[S_02]]；企业预置的 provider/url 双路径消费本 spec 的 `install_prebuilt_from_provider` / `install_skill_sync`（见 [[S_04]]）。
- 工具层 `search_skill` / `install_skill` / `uninstall_skill` 是本 spec 渠道的模型侧封装（[[S_06]]）。
