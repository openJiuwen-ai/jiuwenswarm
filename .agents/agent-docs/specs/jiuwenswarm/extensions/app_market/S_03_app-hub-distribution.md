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
  - jiuwenswarm/extensions/app_market/manager.py
  - jiuwenswarm/extensions/app_market/extension.py
  - jiuwenswarm/extensions/app_market/editor_sessions.py
  - jiuwenswarm/extensions/app_market/app_market/models.py
  - jiuwenswarm/extensions/app_market/app_market/market_service.py
  - jiuwenswarm/extensions/app_market/app_market/storage.py
  - jiuwenswarm/extensions/app_market/app_market/run_consent.py
  - jiuwenswarm/channels/web/frontend/src/components/AppMarketPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/AssetPublishDrawer/index.tsx
---

# Hub 应用分发与本地安装契约

本契约以本地单用户部署为验收范围：安装、启动、编辑、更新、卸载均在同一台机器、同一所有者下完成。个人版未登录连接采用空所有者标识，企业版安装需要已认证身份；共享部署的权限配额与远程页面访问不在本期范围。

## 包身份与边界

- Hub 应用以 `plugin_type=app`、`asset_type=app`、`runtime.type=app` 发布。分发包为 `<outer>/plugin.yaml` + 可选 `outer/icon.png` + 唯一内层 `<outer>/<name>/`；内层必须含 `index.html`，可含 `README.md`、`assets/`、`scripts/app_server.py`。
- 制品身份为 `hub_url + asset_id + version + checksum_sha256`，SHA-256 始终针对 Hub 保存的原始市场包装 zip。app 永不请求或生成 `raw.zip`。
- `plugin.yaml` 的 `version` 为字符串，使用 1 至 4 段数字，可加 `-` 或 `+` 引导的后缀。后缀由 1 至 16 个英文字母、数字、点号或连字符组成，版本总长最多 32 字符。Hub 网页校验并原样提交包内版本。
- Hub 发布 HTTP 请求体上限 512 MiB（含 multipart 开销）。Swarm 制品下载上限 600 MiB，zip 内非目录条目声明大小之和（源文件）上限 512 MiB。单文件 64 MiB、1000 条目、压缩比 50:1 沿用现有检查。
- 内层 `index.html` 若声明 `application-version`，必须等于包版本；Hub 发布和 Swarm 安装、更新均核对该版本。超过 64 KiB 的入口文件须在前 64 KiB 内结束 head 或开始 body。

## 从 Swarm 发布本地应用

- 工作目录可用的本地应用卡片提供「发布到 Hub」。发布抽屉复用 `assets.publish.*`、GitHub/GitCode 登录、制品预览和发布记录。服务端按 `app_id` 读取注册清单，校验当前 Web 连接所有者与 `source=local`。企业部署需要非空所有者。浏览器只提交资产标识和发布信息，应用入口、工作目录和权限从清单读取。
- 应用名、简介和版本预填，可设置可见范围及安装时是否创建项目。应用包名须以小写英文字母开头，其余字符可为小写英文字母、数字或连字符，最长 64 字符，避开系统保留名称。入口 HTML 已声明 `application-version` 时，显式本地登记版本和 Hub 提交版本均须与其一致。Hub 版本、续发和覆盖规则沿用其它资产的公共实现。
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
- 安装先下载并解压到暂存目录，再在目标盘提交目录和注册清单、写入安装凭据。暂存目录和新发布目录使用同一随机发布标记。包不得携带这个标记。
- 市场应用通过 `app_market.convert_to_local` 原地转为本地应用。转换按所有者校验并与安装、更新互斥，保留原目录、`app_id`、项目绑定和应用权限，清除 Hub 安装关联。编辑及再发布使用同一登记，Swarm 停止对该登记提供 Hub 更新和卸载；此时 `app_market.update` 与 `app_market.uninstall` 返回 `app_not_hub_installed`，本地维护走编辑和 `app_market.delete`。对于历史本地登记与安装目录重叠的情况，市场更新和卸载清理仍会保护该目录。
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
- 启动时注入 `WORKSWARM_APP_PROJECT_ID`、`WORKSWARM_APP_PROJECT_DIR`、`WORKSWARM_APP_WORK_MODE`；工作模式采用项目实际值。需要项目的应用优先读取这组字段，本地来源会话方式仍可回退。未绑定项目时为空。`requires_project=true` 且项目不可用时在拉起进程前失败。
- 首次启动需要项目的应用和打开本地应用的编辑会话共用项目解析顺序：清单 `project_id` → 来源会话项目 → 按应用 `work_dir` 复用项目或新建 `code` 项目。隐藏项目恢复，删除后重建。新建时遇到同名项目，在项目名称后附加应用 ID 短后缀，后缀仍重名时追加序号。Git 可用且未初始化时初始化 `main` 并写最小 `.gitignore`，不自动提交；Git 不可用时继续编辑。

## 生命周期 RPC

- 新增 `app_market.market_list/market_detail/install_dir_default/install/install_status/install_cancel/update/uninstall`；安装与更新共用 `job_id`、`state`（`queued/running/done/failed/cancelled`）与 `phase`（`resolving/downloading/verifying/extracting/committing/registering/done`）。`app_market.pin` 按所有者切换应用的 `pinned` 状态并持久化到清单。
- 同一应用存在进行中的安装/更新任务时，重复 `install/update` 返回现有 `job_id`；启动返回 `app_operation_conflict`。
- 所有写操作按连接所有者校验；任务查询、取消、更新、卸载均拒绝跨所有者访问。提交点之后的取消返回最终状态。
- `app_market.list` 为每个应用返回 `work_dir_missing` 和 `pinned`；列表先展示置顶应用，再按 `created_at` 从新到旧排列。目录缺失时前端禁用启动/编辑，并为 Hub 来源提供“重新安装”（删除失效登记后回到市场）。本地应用保留登记，恢复原目录后可继续使用。
- 更新与启动、编辑入口和卸载按应用互斥。更新先准备新版本，检查编辑会话是否忙碌，停止实例并复制 `data/` 和 `.git` 到新目录。原工作树（含未提交编辑）完整保存在安装根的 `.app-archive/`，成功响应返回 `backup_dir`。
- 卸载先停止实例并移除应用市场登记，默认保留本地程序文件、数据、项目与 Git 历史。用户选择清理时，市场验证应用目录归属、归档 `data/` 和 `.git`，再删除程序目录。归属核验、归档或删除失败时，卸载 RPC 仍成功，并通过 `cleanup_warning` 和 `work_dir` 提示检查本地文件。旧安装可在首次成功更新后获得发布标记。既有 `app_market.delete` 对 Hub 应用使用默认卸载行为。

## 兼容性

- 本地命令行、Python 桌面和 Electron 开发版/打包版启动共用 Swarm 的 Gateway 配置准备逻辑。Gateway 未指定独立配置且默认配置文件缺失时，使用 Swarm 实例的配置。Swarm 默认配置先扫描 `EXTENSION_DIRS` 中的自定义目录，再扫描包内 `jiuwenswarm/extensions`。旧的 Swarm 默认值随配置模板迁移。已有的 Gateway 默认配置若仍使用旧版内置目录表达式，Swarm 启动时会追加包内目录。显式的 Gateway 配置、扩展目录设置及 AgentOS 部署配置沿用各自规则。
- 本地应用 `app_market.publish/list/start/stop/delete/edit_session` 入口保持不变；个人版空所有者重复 `start` 复用同一运行实例（避免重复静态服务），同一应用按所有者各保留一份运行实例；`publish_app` 重新登记时保留来源、摘要、项目绑定字段，以及置顶状态与原始注册时间 `created_at`。
- 既有插件/MCP/Agent 资产的 Hub 客户端、下载器与安装器调用默认参数不变。
