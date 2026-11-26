---
id: S_03_app-hub-distribution
title: Hub 应用分发与本地安装契约
type: S
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/extensions/app_market
  - jiuwenswarm/server/runtime/marketplace
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/runtime/marketplace/hub_asset_port.py
  - jiuwenswarm/server/runtime/marketplace/hub_asset_type_adapter.py
  - jiuwenswarm/server/runtime/marketplace/hub_models.py
  - jiuwenswarm/server/runtime/marketplace/hub_client.py
  - jiuwenswarm/server/runtime/marketplace/hub_package_downloader.py
  - jiuwenswarm/server/runtime/marketplace/hub_asset_installer.py
  - jiuwenswarm/server/runtime/marketplace/asset_publish_api.py
  - jiuwenswarm/server/runtime/marketplace/asset_publish_models.py
  - jiuwenswarm/server/runtime/marketplace/asset_publish_adapters.py
  - jiuwenswarm/server/runtime/marketplace/asset_package_builder.py
  - jiuwenswarm/server/runtime/marketplace/hub_publish_client.py
  - jiuwenswarm/extensions/app_market/hub_install.py
  - jiuwenswarm/extensions/app_market/app_ai_roles.py
  - jiuwenswarm/extensions/app_market/manager.py
  - jiuwenswarm/extensions/app_market/extension.py
  - jiuwenswarm/extensions/app_market/editor_sessions.py
  - jiuwenswarm/extensions/app_market/app_market/models.py
  - jiuwenswarm/extensions/app_market/app_market/market_service.py
  - jiuwenswarm/extensions/app_market/app_market/storage.py
  - jiuwenswarm/extensions/app_market/app_market/run_consent.py
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/AppMarketAiRoles.tsx
  - jiuwenswarm/channels/web/frontend/src/components/AssetPublishDrawer/index.tsx
---

# Hub 应用分发与本地安装契约

最近修订日期为 2026-11-24。

本契约以本地单用户部署为验收范围：安装、启动、编辑、更新、卸载均在同一台机器、同一所有者下完成。个人版未登录连接采用空所有者标识，企业版安装需要已认证身份；共享部署的权限配额与远程页面访问不在本期范围。

## 包身份与边界

- Hub 应用以 `plugin_type=app`、`asset_type=app`、`runtime.type=app` 发布。分发包为 `<outer>/plugin.yaml` + 可选 `outer/icon.png` + 唯一内层 `<outer>/<name>/`；内层必须含 `index.html`，可含 `README.md`、`assets/`、`scripts/app_server.py`。
- 制品身份为 `hub_url + asset_id + version + checksum_sha256`，SHA-256 始终针对 Hub 保存的原始市场包装 zip。app 永不请求或生成 `raw.zip`。
- `plugin.yaml` 的 `version` 为字符串，使用 1 至 4 段数字，可加 `-` 或 `+` 引导的后缀。后缀由 1 至 16 个英文字母、数字、点号或连字符组成，版本总长最多 32 字符。Hub 网页校验并原样提交包内版本。
- Hub 发布 HTTP 请求体上限 512 MiB（含 multipart 开销）。Swarm 制品下载上限 600 MiB，zip 内非目录条目声明大小之和（源文件）上限 512 MiB。单文件 64 MiB、1000 条目、压缩比 50:1 沿用现有检查。
- 内层 `index.html` 若声明 `application-version`，必须等于包版本；Hub 发布和 Swarm 安装、更新均核对该版本。超过 64 KiB 的入口文件须在前 64 KiB 内结束 head 或开始 body。

## AI 角色声明与展示

- 新开发的运行时 AI 应用将 UTF-8 的 `app-ai-roles.json` 放在入口 HTML 同目录。版本 1 声明至少一个会话组，包含应用内唯一的组 ID、`agent` 或 `team`、会话简介及角色列表。单角色组有一个角色，团队组声明 leader 和 1–5 名成员。角色填写组内唯一的 ID、名称和简介。头像可省略，声明时必须指向应用目录内的 PNG、JPEG、WebP 或 GIF。字段和示例由 [AI 角色清单规范](../../../../../../jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/references/ai-roles.md)维护。
- 本地 `app_market.publish` 和 Swarm 的 Hub 发布预检校验已提供的清单。有效清单将发布权限标记为 `ai`。Hub 根据预检快照重算并携带清单和打包范围内的头像。没有清单的登记兼容旧 HTML 标记，Hub 打包沿用本地登记中的权限。能力声明用于展示，运行授权仍按制品摘要确认。
- `app_market.ai_roles` 按当前连接所有者与 `app_id` 读取本地或已安装应用的清单，返回公开的 `session_groups`。头像以受限大小的图像数据返回，前端缺图时使用角色名生成占位头像。详情加载时发起请求，列表、运行和 Hub 安装前详情无需读取角色文件。无清单返回空列表，清单有误时返回 `app_ai_roles_invalid`。

## 从 Swarm 发布本地应用

- 工作目录可用的本地应用卡片提供「发布到 Hub」。发布抽屉复用 `assets.publish.*`、GitHub/GitCode 登录、制品预览和发布记录。服务端按 `app_id` 读取注册清单，校验当前 Web 连接所有者与 `source=local`。企业部署需要非空所有者。浏览器只提交资产标识和发布信息，应用入口、工作目录和权限从清单读取。
- 应用名、简介和版本预填，可设置可见范围；发布页不提供运行项目开关，包统一声明 `requires_project: true`。应用包名须以小写英文字母开头，其余字符可为小写英文字母、数字或连字符，最长 64 字符，避开系统保留名称。入口 HTML 已声明 `application-version` 时，显式本地登记版本和 Hub 提交版本均须与其一致。Hub 版本、续发和覆盖规则沿用其它资产的公共实现。
- 打包在独立快照完成，生成 `runtime.type=app` 的 `plugin.yaml`。根目录下旧的 HTML 入口在快照内提供 `index.html`，若与已有入口冲突则提示报错。工作目录中的 `data/`、Git 元数据、真实环境变量文件、已知凭据、日志文件和市场生成的启停脚本会从待上传制品排除。发布确认页展示包内文件及排除项。
- 提交使用预览对应的快照和 SHA-256，上传沿用 `POST /api/v1/plugins`。App 的完整 multipart 请求体校验 512 MiB 上限，包使用 ZIP 存储模式以符合 Hub 压缩比限制。

## Hub 客户端与下载

- `HubAssetKind` 含 `app`；`HubAssetQuery.version` 可固定版本，详情响应透传 `file_size`、`source_size`、`checksum_sha256`、`requires_project`、`permissions`、`has_server_script`。详情摘要来自 Hub 版本详情，不调用 artifact 接口，避免下载计量。
- `resolve_download` 对 app 使用 `is_cli_download=true` 获取原包，要求 `file_size > 0`，并校验 artifact 的资产、类型与版本一致。
- 下载器支持按类型传入下载/解压限制；app 使用 600 MiB 下载、512 MiB 解包总量、64 MiB 单文件，并跳过重复 CRC 全量读取。下载字节先做 SHA-256 校验，再解压。
- 安装器新增可选 `version`、`expected_sha256` 与包根定位器；app 通过定位唯一 `<outer>/<package_name>/index.html` 找到内层目录，`state_store` 可为空，由 app_market 自行持久化 `install.json`。

## 安装与本地注册

- 安装根解析顺序：RPC `install_dir`（必须是服务端绝对路径）→ 环境变量 `APP_MARKET_INSTALL_ROOT` → `get_user_workspace_dir()/apps`。最终目录为 `<install_root>/<package_name>`；Windows 下安装路径长度超过 240 字符时预检失败。
- `app_id = hub-<asset_id 安全化>-<sha256(hub_url, asset_id, owner)[:24]>`；同一 Hub 资产与所有者只允许一份安装。同版本摘要发生变化时可更新。更新前校验当前 Hub 与安装凭据的来源一致。
- 安装先下载并解压到暂存目录，再提交目标目录、注册清单并写入安装凭据，提交前的失败回滚本次目录和登记。注册清单不继承旧副本的 `project_id`、项目目录、来源会话和编辑会话绑定；随后市场尽力通过 Core 按当前应用目录创建或复用项目并把归属写回清单；项目解析失败不丢弃已完成的安装，应用保持已安装未绑定，由启动前补齐绑定。暂存目录和新发布目录使用同一随机发布标记。包不得携带这个标记。
- 市场应用通过 `app_market.convert_to_local` 原地转为本地应用。转换按所有者校验，与安装、更新、删除、启动和打开编辑会话按应用互斥。转换保留原目录、`app_id` 和应用权限，清除 Hub 安装关联。项目目录与当前应用目录一致时，保留项目、来源会话及编辑会话绑定。跨目录绑定一并解除，尝试为应用目录创建或复用独立项目。项目上下文变化时，先停止旧实例，再保存转换后的登记。编辑及再发布使用同一登记，Swarm 停止对该登记提供 Hub 更新和卸载；此时 `app_market.update` 与 `app_market.uninstall` 返回 `app_not_hub_installed`，本地维护走编辑和 `app_market.delete`。对于历史本地登记与安装目录重叠的情况，市场更新和卸载清理仍会保护该目录。
- 安装和更新在数据根的 `app_market/install_ops/<job_id>.json` 记录事务。只有目录归属、清单和凭据的版本及摘要相符时才确认完成。部分写入回滚，更新回滚同时恢复原目录、原清单和原凭据。恢复失败保留事务记录。无发布归属的旧记录、用户目录和备份保留。
- `install.json` 记录 `install_id`、`release_id`、所有者、安装根、工作目录、Hub 资产及制品身份、安装时间和运行能力。注册表位于数据根的 `app_market/apps/<app_id>/`。
- 包结构校验拒绝市场生成的启停脚本、`data/`、`.git` 和 `.workswarm-app-release`。包名最长 64 字符，符号链接和不安全路径由解包入口拒绝。

包条目最多 32 层。路径按大小写折叠检查整个目录树，包括文件与目录的冲突。Web 与桌面市场共用分页列表和安装目录输入框。

## 运行、授权与项目

- Hub 应用清单使用包声明的权限。HTML 内容推断仅用于本地来源。
- Hub 应用首次启动（以及每次更新后）需要按 `owner_id + app_id + artifact_sha256` 授权；未授权返回 `app_run_consent_required` 并携带当前摘要，客户端确认后以 `run_consent.artifact_sha256` 重试。摘要不匹配返回 `app_artifact_changed`。
- Hub 进程仅继承启动所需的系统环境变量，由市场另外注入运行上下文。随包服务脚本默认仅在 WebChannel 绑定回环地址时可启动。共享部署可设置 `APP_MARKET_ALLOW_HUB_CODE=0` 显式关闭，受信部署可设为 `1` 启用。
- 启停脚本与 `/_workswarm/shutdown`、端口、进程回收、`*.localhost` 来源隔离沿用本地应用模型。
- 清单新增可选字段 `source`（`local`/`hub`）、`hub_asset_id`、`hub_url`、`artifact_sha256`、`requires_project`、`project_id`、`project_dir`、`project_work_mode`；旧清单缺省 `source=local`。
- 所有市场应用在启动前都须绑定可用项目。Hub 应用安装时按当前应用目录尽力创建或绑定项目，绑定失败或本地登记、旧安装缺少绑定时在启动前补齐。启动时注入 `WORKSWARM_APP_PROJECT_ID`、`WORKSWARM_APP_PROJECT_DIR`、`WORKSWARM_APP_WORK_MODE`，工作模式采用项目实际值。绑定不可用时返回 `app_project_unavailable`。转为本地应用只沿用与当前目录一致的项目绑定，跨目录绑定在转换时解除并为当前目录重建。
- 发布页不提供运行项目开关。Swarm 打包统一写入 `metadata.app.requires_project: true`，旧发布请求仍可传入该布尔字段。旧 Hub 包与安装记录保留原字段用于兼容，Swarm 启动规则对所有应用生效。
- 安装、启动补绑和打开编辑会话共用项目解析。清单已有 `project_id` 时，通过 `project.info` 验证该项目并跳过会话统计。清单没有绑定时优先查找来源会话项目。已绑定的项目被删除或找不到来源会话项目时，按应用 `work_dir` 复用项目或新建 `code` 项目。隐藏项目会恢复。新建时遇到同名项目，在项目名称后附加应用 ID 短后缀，后缀仍重名时追加序号。打开编辑会话时，Git 可用且未初始化则初始化 `main` 并写最小 `.gitignore`，不自动提交。Git 不可用时继续编辑。

## 生命周期 RPC

- 新增 `app_market.market_list/market_detail/install_dir_default/install/install_status/install_cancel/update/uninstall`；安装与更新共用 `job_id`、`state`（`queued/running/done/failed/cancelled`）与 `phase`（`resolving/downloading/verifying/extracting/committing/registering/done`）。`app_market.pin` 按所有者切换应用的 `pinned` 状态并持久化到清单。
- 同一应用存在进行中的安装/更新任务时，重复 `install/update` 返回现有 `job_id`；启动返回 `app_operation_conflict`。
- 所有写操作按连接所有者校验；任务查询、取消、更新、卸载均拒绝跨所有者访问。提交点之后不再接受安装取消请求。安装任务在后续项目绑定期间被取消时，状态仍为 `done`，结果保留已提交的安装信息。
- `app_market.list` 为每个应用返回 `work_dir_missing` 和 `pinned`；列表先展示置顶应用，再按 `created_at` 从新到旧排列。目录缺失时前端禁用启动/编辑，并为 Hub 来源提供“重新安装”（删除失效登记后回到市场）。本地应用保留登记，恢复原目录后可继续使用。
- 更新与启动、编辑入口和卸载按应用互斥。更新只替换程序文件和版本、名称、简介、图标等描述字段，保留 `app_id`、市场登记、`pinned`、创建时间、项目绑定和编辑会话绑定。更新检查应用绑定的全部编辑会话，任一会话忙碌时拒绝替换目录。更新会停止实例并复制 `data/` 和 `.git` 到新目录。原工作树（含未提交编辑）完整保存在安装根的 `.app-archive/`，成功响应返回 `backup_dir`。
- 移除分为两种显式操作，二者都不修改应用项目或编辑会话记录。**下架并保留文件**停止实例并移除市场登记，保留程序目录、数据与 Git 历史。**删除并清理文件**确认登记移除成功后，验证目录归属，归档 `data/` 和 `.git`，再删除程序目录。Hub 应用通过 `app_market.uninstall` 的 `remove_files` 选择模式，本地应用通过 `app_market.delete` 的 `remove_files` 选择。本地清理只处理市场安装根的子目录，或能用转换时保存的原安装发布 ID 核验发布标记的目录。市场安装根及其上级、归档目录、与其他应用登记重叠的目录均保留。归属核验、归档或清理失败时，登记移除仍成功，并通过 `cleanup_warning` 和 `work_dir` 提示检查文件。旧 Hub 安装可在首次成功更新后获得发布标记。已经转为本地的历史登记如果缺少可核验凭据且位于安装根外，自动清理会保留其目录。

## 兼容性

- 本地命令行、Python 桌面和 Electron 开发版/打包版启动共用 Swarm 的 Gateway 配置准备逻辑。本地 Gateway 默认读取实例的 `config/config-gateway.yaml`。该文件缺失时使用 Swarm 实例的配置目录与扩展环境设置。初始化生成的 Gateway 默认配置先扫描 `EXTENSION_DIRS` 中的自定义目录，再扫描包内 `jiuwenswarm/extensions`。启动时在 Gateway 实际读取的配置中补齐旧默认值遗漏的包内目录。显式的 Gateway 配置、扩展目录设置及 AgentOS 部署配置沿用各自规则。
- 本地应用 `app_market.publish/list/start/stop/delete/edit_session` 入口保持不变；个人版空所有者重复 `start` 复用同一运行实例（避免重复静态服务），同一应用按所有者各保留一份运行实例；`publish_app` 重新登记本地应用时保留来源、摘要、项目绑定字段，以及置顶状态与原始注册时间 `created_at`，Hub 来源登记不继承旧副本的项目和编辑会话绑定。打包到 Hub 的 `plugin.yaml` 不包含本地项目或编辑会话信息。
- 个人版允许空身份启动已有所有者的本地应用，清单中已记录的所有者在启动成功或失败时均保留。企业版和 Hub 应用继续按原所有者校验。
- 既有插件/MCP/Agent 资产的 Hub 客户端、下载器与安装器调用默认参数不变。
