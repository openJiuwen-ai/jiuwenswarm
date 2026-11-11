# 后端包管理协议

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 / 编号 | spec / S_02 |
| 最近修订 | 2026-11-10 |
| 所属模块 | `extensions_v2/management/adapter.py` |
| 特性依据 | [F_09](../features/F_09_backend-management-requests.md)、[F_10](../features/F_10_management-failure-recovery.md)、[F_35](../features/F_35_system-plugin-recovery.md) |

门面契约沿用 [S_01](S_01_extension-management-facade.md)。

## 1. 请求

沿用 `AgentRequest` / `AgentResponse`，由已有认证、用户路由和 E2A 链路传递。

| 方法 | params | 行为 |
| --- | --- | --- |
| `extensions.v2.query` | 可选 `package_id: str` | 查询全部或一个包 |
| `extensions.v2.catalog` | `scope: "catalog" \| "mine"` | 列出预设包或已生效包 |
| `extensions.v2.show` | `package_id: str`，可选 `version: str` | 返回一个包的详情投影；指定版本时优先返回该版本的 Hub 详情 |
| `extensions.v2.install` | `paths: list[str]`，非空，每项为现有 ZIP 文件的绝对路径 | 安装 ZIP 扩展包，拒绝目录；同标识的新版本保留旧版本并切换生效，已存在的同版本返回 `PACKAGE_EXISTS` |
| `extensions.v2.install` | `package_ids: list[str]`，可选 `version: str` | 按包 ID 从本地预设或 Hub 安装；目标版本已生效时幂等，换版本时保留旧副本并切换生效 |
| `extensions.v2.import` | `path: str`，现有目录或归档的绝对路径 | 按 `package.domain` 分流安装（Gateway 入口） |
| `extensions.v2.uninstall` | `package_id: str`，可选 `version: str` | 不带 `version` 时卸载所有版本；带 `version` 时只删除该版本副本，其他版本保留 |
| `extensions.v2.reload` | `package_id: str` | 从安装副本热重载，不先卸载 |
| `extensions.v2.set_enabled` | `package_id: str`、`enable: bool` | 保留安装副本，启用或禁用系统插件 |
| `extensions.v2.file.list` | `package_id: str`，可选 `version: str` | 列出包内可预览文件树；已安装版本读本地副本 |
| `extensions.v2.file.read` | `package_id: str`、`path: str`，可选 `version: str` | 读取一个可预览文件；已安装版本读本地副本 |

包标识沿用 Core 约束：1～128 个字符，首位为英文字母或数字，其余可包含英文字母、数字、点、
下划线和连字符；不含空格与 `..`，不以点结尾，也不使用 Windows 保留名。未知参数拒绝，不接受自建
Plan 或修改安装目录的参数。
外部安装请求先校验绝对路径、`.zip` 后缀和文件类型，再交给 Core 校验归档、manifest 与依赖。
此限制只在外部请求入口生效；启动通过 `load_installed()` 加载安装目录内的解压副本，不受影响。

`extensions.v2.import` 面向未定域的上传入口，由 Gateway 读取包根 `manifest.json` 后分流：
`package_type` 为 `extension` 的包按 `package.domain` 归属，`gateway` 在 Gateway 安装，`agentserver` 转为
`extensions.v2.install`；其余包（如 `package_type: plugin`）转为 `plugin_packages.import_local`。
响应附 `kind`（`extension` 或 `plugin`）供前端区分刷新目标。

### 1.1 详情与文件投影

展示信息统一取 manifest 的 `display`，见 1.3。`readme` 分别取包根 `README.md` 与 `README_EN.md`；
两者都编码为 `{zh, en}`，单一语言缺失时回落到另一语言。
`preview` 取包内固定的 `assets/preview.png`，文件缺失或超过 5 MB 时为 `null`；
`display.events` 每项为 `{name, description: {zh, en}}`，只用于详情页展示，不参与加载校验。

文件树和文件读取复用 AgentServer 的包预览能力：只列非隐藏文件和目录，`previewable` 覆盖
`.md`、`.mdx`、`.json`、`.py`、`.pdf`，单文件上限 1 MB，拒绝绝对路径、`..`、隐藏段和符号链接。
Gateway 不实现本地读取，`files` 与 `file.read` 始终转发给 AgentServer，两域共用同一包源解析入口。
已安装副本（含 Gateway 域）优先于 Hub 预览；只有未安装的版本才回落到 Hub 资源。

### 1.2 manifest 字段

| 字段 | 必填 | 消费方 | 说明 |
| --- | --- | --- | --- |
| `schemaVersion` | 是 | Core | 固定为 1 |
| `package_type` | 是 | Core | 固定为 `extension` |
| `package.id`、`package.version` | 是 | Core | 包标识与版本 |
| `extensions[]` | 是 | Core | `id`、`runtime=backend`、`module`；模块文件须位于包内 |
| `package.domain` | 是 | Core / 宿主 | `gateway` 归 Gateway，`agentserver` 归 AgentServer；缺失或其它值拒绝安装 |
| `enable` | 否 | Core / 宿主 | 是否登记该包 Handler。缺失按 `true`；受管副本会补写为 `true`，`false` 时保留文件但不生效 |
| `expose` | 否 | 宿主 | 本地预设扫描用；为 `true` 才出现在本地插件广场 |
| `display` | 否 | 宿主 | 展示信息，字段见 1.3 |
| `ui` | 否 | 宿主 | 槽位贡献，见 [F_07](../features/F_07_ui-contributions.md) |
| `client` | 否 | Gateway | `requestBindings`，安装激活后随 client.declarations 下发 |

Core 解析并校验 `package.domain`；`expose`、`display`、`ui`、`client` 等宿主字段由宿主从生效记录的包根读取原始文件。

包作者的填写示例、README 写法与自检项见 [系统插件包开发指南](../designs/system-plugin-package-guide.md)。

### 1.3 display 字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 否 | `{zh, en}`，卡片与详情页标题；缺省回退包 ID |
| `description` | 否 | `{zh, en}`，卡片简介 |
| `tags` | 否 | 双语文本数组，渲染为卡片标签 |
| `avatar` | 否 | 包内相对路径，不超过 1 MB |
| `details` | 否 | `{zh, en}` Markdown，详情页“内容详情” |
| `events` | 否 | `[{name, description}]`，详情页“依赖事件” |

界面文案统一使用 `{zh, en}` 对象，单语字符串按双语同值处理；声明与消费不再有其它文案来源。

## 2. 响应

保留原 `request_id`、`channel_id`、`metadata` 和 `agent_ref`；路径编码为字符串，元组编码为数组。

| 场景 | ok | payload |
| --- | --- | --- |
| 查询完成 | true | `state`：active、installed、installed_packages、inflight、published、packages、package_ids |
| 列表完成 | true | `packages`：卡片数组 |
| 详情完成 | true | `package`：卡片字段加 `details`、`readme`、`preview`、`events` |
| 文件树 | true | `tree`：`{path, type, size?, previewable?, children?}` |
| 文件内容 | true | `{path, content, download_url?}` |
| 取得写操作结果 | true | `reports`：Core ActivationReport 数组 |
| 方法或参数错误 | false | `code=INVALID_PARAMS`、`error` |
| 门面尚不可用 | false | `code=EXTENSION_MANAGER_NOT_INITIALIZED`、`error` |
| 调用抛错 | false | `code`、`error`，不得伪造成功 Report |

Report 字段沿用 Core，`error` 编码为 `{code, message, params}`，不发送 StatusCode 枚举对象。
正常失败 Report 不补查、不重试。写操作抛错且未取得 Report 时返回 `outcome=unknown`；
核对成功附 `state`，核对失败附 `query_error`，原始 `code` 和 `error` 保留。
查询到当前状态不表示原操作成功，不补发 Report。查询请求失败不递归核对，取消继续传播。

安装响应另含 `installed_packages` 数组，各项为
`{package_id: str, version: str, install_path: str, generation: int}`。
只收录本次成功 Report 对应的生效记录，包 ID 和 generation 均须匹配。
`install_path` 是目标 AgentServer 上的实际安装绝对路径，来自记录的 `root`；不是最初的来源路径。
未取得匹配记录时不伪造目录，原始 Report 仍保留。失败 Report 不产生安装成功提示。

AgentServer 在包进入可重新就绪状态后，将本次 `SUCCESS` 包 ID 通知曾上报 `ui.ready` 的连接。
安装和成功启用由公共管理适配器发送；热重载由旧代际的视图清理回调在下发 `webui.render.clear` 后发送，避免异步 clear 擦掉新视图。
网页 `/extension install`、本机 CLI 和其它调用该适配器的入口共用安装通知，CLI 不重复通知。
前端收到 `extensions.v2.installed` 后，为仍挂载且允许 ready 的槽位重新发送 `ui.ready`，无需手动刷新页面。
安装完成、成功启用和热重载清理完成后，`extensions.v2.installed` 都表示指定包需要重新发送 `ui.ready`。
Gateway 下发该事件时保留完整结构，包括 `package_ids` 和 `session_id`（应用级通知为空字符串），
不得按聊天消息只提取 `content`；通知继续按 `metadata.ws_id` 定向发送。
安装和成功启用响应的 `ui_notified` 表示通知是否送达；未连接或通知失败时为 false，不改变成功 Report 和 `ok`，不回滚已成功的操作。
热重载的代际释放与通知在成功 Report 返回后异步执行，因此 reload 响应不承诺该通知已经送达。
失败包不加入通知；全部失败时不发送通知。通知送达不等于插件自身渲染或业务验收通过。

查询的 `state` 另含 `installed_packages`，各项为 `{package_id, version, install_path}`。
它从安装目录的固定布局取得 ID、版本和实际路径，不执行包代码；因此未生效的副本也可以展示 ID。
generation 保留为内部校验字段，不出现在命令结果中。目录和详情投影还提供 `enabled: bool`：
已安装但禁用的系统插件仍出现在“我的扩展”，并显示开关；只对系统插件展示该开关。

### 2.1 系统插件还原记录

还原记录由当前程序启动的独立本机恢复进程维护。每次启动在加载插件前追加一条 `startup` 记录；启动加载不是
逐包记录。安装、卸载、启用、禁用和还原成功后各追加一条记录，所有启动之间共最多保留最近 50 条。每条记录包括
`id`、`timestamp`、`action`、本次操作的 `package_ids` 及该时刻的系统插件启用状态。独立页以存档点时间作为
记录主标题，并以操作内容作为副标题；初始时不展开记录，一次只展开一条，再次点击已展开的记录会收起详情。展开详情时，
独立页通过本机预览接口按当前受管安装目录计算执行还原后的包 ID、版本和启用状态：快照中已有但当前目录已删除的包不展示，
快照中不存在但当前仍安装的包展示为禁用。每条记录左侧是单选还原目标；页面右上角的还原入口在未选择时禁用，选择后启用。
预览状态与实际还原结果一致。

还原页不使用扩展管理 RPC。主应用通过受保护的本机静态服务取得带随机令牌的回环地址，并在
`/system-plugin-recovery` 路由嵌入独立页面；令牌不出现在主应用地址。独立页读取记录并执行还原。还原只更新当前
仍在受管安装目录内、且 `package_type: extension` 的 manifest：记录中存在的包版本按记录写入 `enable`，记录中
不存在但当前仍安装的包写入 `false`。已经删除的安装目录不会重新下载或恢复。还原本身会追加一条 `restore` 记录。

还原不回滚插件已经执行过的外部副作用。恢复进程不导入插件模块，只读写受管 `manifest.json`，并且只监听
`127.0.0.1`。恢复完成后会写入一次性重启请求；父启动器消费请求后自动重启 AgentServer 和 Gateway，使新的
插件状态生效，正在处理的任务会中断。

### 2.2 安装来源与版本

安装来源记录在安装目录同级的 `extension/system_plugin_install_state.json`（默认 `~/.jiuwenswarm/extension/`），
与安装目录分离存放，不写入包内。
每条记录包含 `package_id`、`version`、`origin`（`hub` / `user` / `preset`）、`asset_id`、`install_path`、
`active`、`enabled`；Gateway 与 AgentServer 共用同一文件。

目录卡片与详情页字段：

| 字段 | 说明 |
| --- | --- |
| `packageId` | 落地后的包目录名，卸载、启用、禁用均使用该值 |
| `assetId` | Hub 资源 ID；本地上传与预设为空 |
| `installedVersion` | 当前安装版本；未安装为空 |
| `versions` | Hub 已发布版本；本地包只有自身版本 |
| `origin` / `source` | 安装来源，`source` 为界面使用的 `hub` / `local` 二分 |

按包 ID 安装时：目标版本已生效直接返回成功；目标版本尚未安装时从 Hub 下载或使用本地副本，
先停用当前版本再启用目标版本，旧版本文件保留在工作区。用户上传与预设包记录 `origin=user` / `origin=preset`，
Hub 下载记录 `origin=hub` 与 `asset_id`。用户上传的包可在详情页发布，发布类型为 `system_plugin`。

## 3. Web 命令

Web 在单 Agent 模式中解析 `/extension`，新建对话与已有会话均可使用，直接发起上述请求，不进入普通对话。
命令管理当前路由到的 AgentServer 进程内的扩展包，不只影响当前会话。

| 输入 | 请求与参数 |
| --- | --- |
| `/extension` | 显示帮助，不发请求 |
| `/extension install <ZIP 文件的绝对路径>` | `extensions.v2.install`，`paths` 为一个 ZIP 路径组成的数组 |
| `/extension query [package_id]` | `extensions.v2.query`，可选包 ID |
| `/extension uninstall <package_id>` | `extensions.v2.uninstall`，必填包 ID |
| `/extension reload <package_id>` | `extensions.v2.reload`，必填包 ID |

子命令不区分大小写，包 ID 和路径保留原文。单次命令安装一个 ZIP；install 后的完整内容作为一个路径，
允许直接粘贴含空格的路径，也兼容路径外的一对引号。反斜杠原样保留，不执行 shell 命令。
路径必须是运行服务可读取的绝对路径，最终校验由后端负责。

已有会话时 Web 携带 `session_id` 供 Gateway 路由；新建对话不携带前端占位标识 `new`，
不要求先创建聊天会话。两种情况都沿用连接认证和用户路由，不能因没有聊天会话绕过认证。
Gateway 将路由使用的会话标识移入 E2A 会话字段，不传入管理器业务参数。
Gateway 保留失败 payload。Web 按 Report.status 判断成功或失败；超时、断连或 `outcome=unknown`
显示结果未确认和查询用法，不自动重试。查询失败不展示为空列表。
安装、查询、卸载和重载结果明确标注 `package_id`。安装成功展示版本和安装目录，
提示修改安装副本后执行热重载；无参数查询按包 ID 升序显示共享报告中的 package_id、domain 和生效版本，
并将当前进程所属包的运行状态和目录合并在同一包下；指定 ID 的查询显示该包详情，不向用户显示 generation 或内部进程 ID。

## 4. 不变量与边界

1. 只使用已装配门面，不创建第二个 Bus 或 Registry。
2. 网络方法名与 Core 事件名分开，不接收候选对象或管理事件输入。
3. 查询不触发安装、回滚或重载；失败报告与没有报告的异常分开处理。
4. Core 共享报告提供同主机跨进程管理保护；Web 仅提供命令入口，不提供管理页面。

验证入口：`tests/unit_tests/extensions_v2/management/test_management_adapter.py`。

## 5. 本机 CLI

特性设计见 [F_31](../features/F_31_extension-cli.md)。源码、Wheel 使用 `jiuwenswarm extension`；
EXE、DMG 使用现有后端程序的 `extension` 分发，参数与结果使用同一实现。

| 命令参数 | 语义 |
| --- | --- |
| `install <ZIP>` | 读取最终 ZIP 的 ID、版本与 `package.domain`，再交给对应的运行宿主安装 |
| `query [package_id]` | 无包 ID 时合并两个宿主的状态；有包 ID 时按包归属查询 |
| `reload <package_id>` | 按包归属从安装副本重载 |
| `uninstall <package_id>` | 按包归属卸载包或未生效副本 |
| `--domain agentserver/gateway` | install 与 ZIP 声明必须一致；其它操作可显式指定宿主 |
| `--json` | 结构化输出；当前默认也输出 JSON |
| `--timeout <seconds>` | 每次本机请求的超时，缺省为配置的依赖等待上限加 30 秒，必须为正的有限数 |
| `--server-path <ZIP>` | 宿主已可读取的绝对路径；不执行上传 |

### 5.1 连接与授权

每个宿主在现有装配完成后创建回环监听，生成新的随机密钥和实例标识。
内部终端通过 `JIUWENSWARM_EXTENSION_CLI_AGENT` 或 `JIUWENSWARM_EXTENSION_CLI_GATEWAY` 继承连接记录；
Gateway 与 AgentServer 分别保存自己的记录，位置为数据目录下的
`run/extension-cli-<domain>-<实际服务端口>.json`；CLI 根据当前运行环境选择对应记录。
记录仅包含本机入口，CLI 不扫描端口，不读取浏览器登录信息。缺少当前实例的连接信息时返回 `HOST_UNAVAILABLE`。
记录在 POSIX 使用 0600 权限，Windows 沿用当前账号数据目录的访问权限。

本机入口只接受四种管理操作，校验随机密钥、实例标识与包的 domain 后，调用已装配的管理器。
该入口供当前操作系统账号使用，不提供远程用户授权；Web 请求继续使用原登录和路由规则。
关闭入口时撤销连接记录，旧连接信息不能用于重新启动的宿主。

未传 `--domain` 时，本机 CLI 与 Web `/extension` 使用相同的路由语义：install 按 ZIP 的
`package.domain` 选择宿主；无参数 query 汇总 Gateway 和 AgentServer 状态；带包 ID 的 query、reload 和
uninstall 先检查 Gateway 是否拥有安装副本，否则由 AgentServer 处理。显式 `--domain` 保留为调用方固定
选择宿主的兼容入口。

### 5.2 结果与失败

CLI 安装前查询同 ID 状态；已有副本时不自动覆盖、卸载或重装。成功 Report 后再次核对版本、generation 与目录。
输出保留 `reports`、`before_state`、`state`，并分别记录 `operation_status`、`verification_status` 与总体 `status`。
`business_verification` 固定为 `not_run`；有页面通知时附 `ui_notified`，不将通知送达解释为页面已验收。

| 退出码 | 语义 |
| --- | --- |
| 0 | 查询成功，或写操作与后续核对均成功 |
| 1 | 明确失败报告，或成功报告与当前状态不符 |
| 2 | 输入、运行环境或写入前检查失败 |
| 3 | 写操作结果未知，或成功报告后的状态无法确认 |

超时、断线和无法识别的写操作结果仅查询一次当前状态，不重试写请求，不补造成功 Report。
安装成功后通知已有 WebUI 连接重新发送 ui.ready；未连接或通知失败不回滚安装。
未实际触发业务或检查浏览器时，CLI 结果不能单独证明用户效果已经完成。
