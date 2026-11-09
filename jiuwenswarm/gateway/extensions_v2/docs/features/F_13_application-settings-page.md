# 应用级设置页设计方案

## 1. 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | feature |
| 编号 / slug | `F_13 / application-settings-page` |
| 建档日期 | 2026-11-05 |
| 后续修订 | 2026-11-09：移除宿主固定扩展菜单与开场设置页（D-12）；2026-11-07（第二轮检视）：实例键规则统一、导入凭据并发与下载前再次核对激活（第 8.2 节 V-12）；2026-11-07：统一到 S_18 设置贡献模型（D-11）；2026-11-05：导入与导出对远程浏览器同样开放（D-07）；2026-11-06：每个插件的设置页在设置导航中单独一项，“扩展”模块只放宿主功能（D-08）；与 AgentServer 插件设置并存（D-09）；2026-11-07：标题改取 manifest.json 的 `display.name` 并按界面语言显示（D-10） |
| 计划条目 | [EXTV2-GW-12](../designs/extensions-v2-development-plan.md) |
| 整体设计 | [应用级界面能力设计概览](../designs/application-surfaces-overview.md) 第 1.1、3.3 节 |
| 方案成熟度 | 已确认，已实现 |
| 关联规约 | [S_10 应用级设置页与文件交接](../specs/S_10_application-settings.md)、[S_09 启动门控与开场](../specs/S_09_startup-splash.md)、[S_17 组件属性](../../../../extensions_v2/docs/specs/S_17_webui-component-props.md) §4.25 |
| 验证摘要 | 单元验证与 Chromium 157 无头模式验证通过（2026-11-05）；桌面打包版与真实 WebChannel 联调随壁纸插件进行 |

## 2. 背景、目标与范围

### 2.1 背景与问题

外观类插件需要一个与聊天会话无关的设置位置：切换主题、导入用户主题、导出与备份、恢复默认。
现有 `application.settings` 由 AgentServer 渲染，需要会话标识，Gateway 包无法使用。
GW-11 的开场队列按发布顺序播放全部提供者，用户无法调整顺序或停用某个开场；`ui.closed` 的
`disabled`、`removed` 两个原因已在契约中预留但没有触发点。导入主题需要用户选择 ZIP 或目录，
导出需要把插件生成的文件交给用户保存，这两件事 V2 都没有通道。

### 2.2 目标与关键场景

| 场景 | 预期行为 |
| --- | --- |
| 安装了带设置页的插件 | 设置导航按插件的 S_18 声明显示菜单、分类和配置项，内容由插件渲染 |
| 安装了登录插件 | 登录前设置帧被拒绝；登录后正常 |
| 多个开场插件 | 播放沿用已有开场协议与保存的偏好；宿主不提供开场顺序和停用的设置页 |
| 未调整过顺序 | 保持“外观提供者优先，其余按安装顺序” |
| 停用或卸载正在播放的开场 | 宿主关闭该段并以 `disabled` / `removed` 通知插件，队列继续 |
| 导入主题 | 用户选择 ZIP 或目录；内容写入插件数据目录，插件收到相对路径 |
| 导出与备份 | 插件生成文件后，桌面端弹出保存对话框，浏览器中下载 |
| 从另一台设备的浏览器打开 | 导入与导出同样可用，访问权来自一次性凭据 |

### 2.3 本次范围与非目标

范围：Gateway 包的设置贡献（按 AgentServer S_18 声明，见 D-11、D-12）；宿主方法；开场偏好的保存与生效；
`disabled` / `removed`；`FileImport` 组件、导入凭据与上传路由；`ops.ui.download` 与下载路由；`app_web` 转发。

非目标：导入进度的断点续传；设置页搜索；
`FileImport` 之外的通用远程上传（概览第 1.3 节仍推迟）。

## 3. 设计约束、假设与待决问题

- 登录示例在登录前拒绝除自身入口外的全部 `webui.dispatch`；设置页只在门控开放并登录之后可达，
  因此设置相关的帧经请求策略即可，不需要平台帧。
- `gateway.request` 链的总时限为 5 秒，导入完成事件的 Handler 不能做长时间解压；
  Handler 内把目录移走（改名）后再在后台处理。
- 远程浏览器经 `app_web` 或直接连接 Gateway；凭据出现在请求地址中，远程部署应使用 HTTPS。
- 待决问题：无。远程浏览器上传此前在概览中列为推迟；2026-11-05 用户决定插件设置页的导入与导出同样对远程浏览器开放（D-07）。

## 4. 总体方案与设计取舍

### 4.1 方案概览

插件按 S_18 在 `application.settings` 上声明菜单、分类与配置项，经共享目录合成到设置树。宿主不预置“扩展”菜单和“启动开场”设置页（D-12）。
Gateway 仍把已有开场偏好保存在 `<data_dir>/host/splash.json`，`webui.splash.providers` 给出生效顺序。
导入由宿主组件 `FileImport` 发起：前端先取得导入凭据，把用户选中的文件流式上传到 Gateway，
再以 `ui.change` 把凭据交给插件；Gateway 把凭据换成数据目录中的相对路径。导出由插件调用 `ops.ui.download`，
宿主签发一次性下载地址并通知浏览器。

### 4.2 关键决策

#### D-01 新增 Gateway 应用级设置槽位（已被 D-11 替代）

不复用 `application.settings`：它属于 AgentServer 的会话级渲染链路，改成双宿主会让同一槽位的作用域取决于包的
`domain`。新槽位与 `application.entry` 等同属 Gateway，按连接寻址，插件不需要会话。

#### D-02 宿主方法在 USER 请求策略之后应答

读写开场偏好与开始导入都是用户操作，不应在登录前可用。宿主方法由 BUILTIN 处理器应答，USER 请求策略先判定：
要求登录的插件拒绝未认证的帧时，宿主方法同样不可用。

2026-11-06 修订：合入 `releases/0.2.9.beta3` 时目标分支已移除 BUILTIN 认证门禁（见 F_02），原方案“注册在门禁之后、
由门禁要求认证”不再适用，改为只依赖 USER 请求策略；处理器注册位置与应答语义不变。
没有任何 USER Handler 时，运行时同样让这些方法进入链路，避免误转发到 AgentServer。

#### D-03 开场偏好由 Gateway 保存，“外观提供者优先”只在未调整时生效

顺序是跨插件的设置，属于宿主数据，不放在任何一个包的存储里。安装顺序单独记录，使“恢复默认”能回到安装顺序。
外观提供者只有浏览器知道（S_08 按连接），所以由前端在 `customized` 为 false 时把它排到首位；
用户一旦调整顺序，以保存的顺序为准。停用只影响开场，不影响背景与外观。

#### D-04 导入传内容，不传路径

浏览器发来的路径无法证明来自用户的选择；按路径复制等于允许读取本机任意文件。前端读取用户选中的文件内容，
带凭据上传；Gateway 只写入调用包的数据目录。Electron 的文件输入可以直接使用，不需要新增 IPC，
目录用 `webkitdirectory` 多文件上传。

#### D-05 三段式导入：开始、传输、完成

开始时校验节点、文件清单与大小并签发凭据；传输走凭据访问的 HTTP 路由以便流式写盘，不经 WebSocket 帧预算；
完成仍走 `webui.dispatch` 的 `ui.change`，复用实例、版本与动作校验，插件按普通交互处理。

#### D-06 导出由宿主签发一次性地址

插件只能指定自己数据目录中的文件；地址只发给调用所在的连接、只用一次。桌面版复用已有的 `downloadFile`
（保存对话框与“在文件夹中显示”），浏览器用下载链接。不复用素材路由，因为素材按页面内联显示并带沙箱脚本策略。

#### D-07 导入与导出不区分本机与远程浏览器

访问权只来自一次性凭据：导入凭据由 USER 请求策略继续后的宿主方法签发，绑定连接、包、激活代次、实例与节点，
完成时必须由同一连接提交；下载地址只发给调用所在的连接。Gateway 与 `app_web` 不判断对端地址。
凭据出现在请求地址中，远程部署应使用 HTTPS；单次导入的总大小受 `uploads.max_bytes` 限制。

历史：首版实现（同日）两处路由都只接受回环地址，因为概览把远程浏览器上传列为推迟，而这一点当时没有得到用户的明确答复。
用户随后决定都放开，回环判断与相应的前端提示一并移除。

#### D-08 每个插件在设置导航中单独一项，“扩展”只放宿主功能（2026-11-06；实现方式已被 D-11 替代，导航效果保留）

首版把所有插件的设置页放在“扩展”模块里，每个插件一个页签。用户试用后要求按插件分开，并保留“扩展”放宿主自己的功能：

- 每个声明 `application.settings.page` 的包在设置导航中单独一项，排在“扩展”之后，顺序同 `webui.settings.pages`（安装顺序）。
- 名称取插件显示名（与 `webui.settings.pages` 的 `title` 相同），图标统一为拼图图标；选中时只挂载该包的设置页。
- “扩展”模块只含“启动开场”分组（顺序、停用、恢复默认），说明文字随之改为宿主功能。
- 插件项在设置页打开且连接可用时读取；读取失败或尚未返回时不显示插件项，连接恢复后重新读取。
- 宿主访问策略对 `extensions` 模块的结论同样作用于插件项：隐藏“扩展”时不显示插件项，也不发出 `webui.settings.pages`。

放弃的做法：在 manifest 的设置页贡献中另设标题或图标字段（新增契约；用户选择直接用插件名与统一图标）；
把启动开场移到“常规”或单独成项（用户选择保留“扩展”放宿主功能）。

#### D-09 与 AgentServer 插件设置并存（2026-11-06；已被 D-11 替代）

`releases/0.2.9.beta3` 引入了 AgentServer 插件设置：`webui.settings.catalog` 返回 `kind: menu|section|item` 贡献，
按 location 规则合成进已有设置模块（[S_18](../../../../extensions_v2/docs/specs/S_18_settings-location.md)）；
S_18 说明 Gateway 域插件目录未接入该接口。合入时两套机制并存、不合并：

| 机制 | 数据来源 | 呈现 |
| --- | --- | --- |
| AgentServer 插件设置（S_18） | `webui.settings.catalog`，插件按 location 声明 | 合成进常规等已有模块的分区与设置项 |
| Gateway 插件设置页（本特性） | 宿主方法 `webui.settings.pages`，包声明 `application.settings.page` | “扩展”之后每个插件单独一项，挂载该包的设置页 |

`SettingsPageLayout` 先由 `usePluginSettings` 合成 AgentServer 贡献，再在“扩展”模块之后插入 Gateway 插件项；
目标分支的侧栏收起与悬浮、初始模块、行内保存错误和插件目录错误提示同样作用于插件项。目标分支已移除布局底部通用的
`application.settings` 宿主，本特性不恢复。统一两套机制（例如让 Gateway 包也按 location 声明设置位置）留待后续评估。

#### D-10 标题取 manifest.json 的 `display.name`（2026-11-07；设置菜单标题改由 D-11 的声明提供，本条只用于启动开场列表）

`releases/0.2.9.beta3` 把系统插件包统一为 `manifest.json`：包归属写在 `package.domain`，展示信息写在 `display`，
所有文案为 `{zh, en}`（[系统插件包指南](../../../../extensions_v2/docs/designs/system-plugin-package-guide.md)）。
原方案读取的 `package.displayName` 不再存在，插件项会退回包标识。现在 Gateway 加载时按包目录的同一规则读取
`display.name`：单个字符串按双语同值，缺一种语言时取另一种，为空或超过 200 字节时忽略；`webui.settings.pages`
与 `webui.splash.settings` 的 `title` 改为 `{zh, en}`，前端设置导航、页头与启动开场列表按界面语言显示，切换语言即时生效。
前端仍接受字符串标题，以兼容旧的应答。放弃的做法：由 Gateway 选定一种语言（Gateway 不知道浏览器的界面语言）。

#### D-11 统一到 S_18 的设置贡献模型（2026-11-07，PR #10763 检视）

检视指出：已有 `webui.settings.catalog` 与 S_18，本特性又加了 `application.settings.page`、`webui.settings.pages`
和设置导航的单独拼接，目录、刷新、错误提示与国际化出现两套语义；S_18 明文规定“没有另一个设置承载槽位”；
新目录读取失败时直接清空列表；D-01 以“原设置属于会话级”为理由，但 S_18 已支持 application 范围
（真正的原因是 S_18 写明 Gateway 域插件目录未接入）。现在：

- 两侧保留各自的插件归属：AgentServer 包仍由 AgentServer 列出，Gateway 包由 Gateway 列出；声明方式相同（S_18），
  Gateway 配置项须显式为 application 范围。校验规则在 Gateway 侧另实现一份（模块边界不允许互相 import），
  两侧共用 `tests/fixtures/settings_contribution_cases.json`。
- `webui.settings.catalog` 由 Gateway 取 AgentServer 的目录后追加自己包的声明，`revision` 覆盖两者；前端只有一套目录、
  刷新、失败提示与国际化（`usePluginSettings`），失败时保留上次目录，不再清空。
- `application.settings` 由两侧共用，Gateway 只认领其包已声明的配置项实例，其余转发给 AgentServer。
- 删除 `application.settings.page`、`webui.settings.pages`、`pluginPages.tsx` 与 `SettingsPageLayout` 中的拼接，
  `SettingsPageLayout` 恢复为目标分支原样；“扩展”模块只含宿主的开场设置。插件菜单写 `location.after: "extensions"`
  即可得到 D-08 的导航效果。
- 代价：插件设置页从“整页”变为放在设置行内容中的组件树，布局需随插件调整；主题背景插件需改写声明。

#### D-12 移除宿主固定的扩展菜单与开场设置页（2026-11-09）

用户确认设置页的扩展内容由插件声明，不保留宿主固定的“扩展”菜单及其“启动开场”设置，也不迁移到“常规”。
删除该菜单的注册、专用组件、样式、前端开场设置读写工具及中英文文案；设置目录查询、位置合成、WebUI 渲染和交互继续复用 S_18。
插件菜单可使用空 `location` 追加，或定位到有效的同级节点。旧插件中的 `after: "extensions"` 沿用 S_18 缺失锚点追加规则，
不为旧锚点保留隐藏菜单或新增特殊分支。开场播放、预览、Gateway 方法与既有偏好沿用原契约；本次不删除整个 `application.splash` 能力。

验证使用“主题扩展开发及视频录制”交付的 `workswarm-theme 1.0.2` 原始 ZIP，在独立数据目录安装，核对无固定菜单、
插件菜单与内容、配置交互、外观切换、预览和刷新恢复。实施及验证记录见第 8 节；D-08、D-11 的固定菜单描述为历史方案。

### 4.3 替代方案与排除理由

| 方案 | 排除理由 |
| --- | --- |
| R-01 `application.settings` 双宿主 | 同一槽位在两条渲染链路中语义不同，前端无法只凭槽位判断该发往哪里 |
| R-02 开场偏好存浏览器本地 | 多个窗口与浏览器之间不一致；Gateway 无法据此剔除停用的提供者 |
| R-03 路径交给插件或由 Gateway 按路径复制 | 无法证明路径来自用户选择；插件可借此读取任意本机文件 |
| R-04 文件内容经 WebSocket 分块 | 受帧预算限制，需要自行实现分块重组；HTTP 可直接流式写盘 |
| R-05 导出复用 `/extension-assets/` | 需要把导出文件放进可公开的目录，且响应会被当作页面内联显示 |

## 5. 详细设计

### 5.1 模块责任

| 模块 | 修改 |
| --- | --- |
| Gateway `webui/constants.py`、`bridge.py` | 新槽位；设置页包列表；开场提供者按偏好排序；`close_splashes`；`FileImport` 的 `ui.change` 换为可信数据 |
| Gateway `webui/host_settings.py`（新） | 开场偏好的读写与保存；宿主方法处理器 |
| Gateway `webui/transfers.py`（新） | 导入凭据、上传接收、下载凭据、清理 |
| Gateway `webui/http.py` | 上传与下载路由 |
| Gateway `webui/component_rules.py` | `FileImport` |
| Gateway `webui/registry.py` | 单个实例的退役（清除帧并预留版本号） |
| Gateway `runtime.py`、`bootstrap.py`、`config.py`、`loader.py`、`handler_ops/ui.py` | 宿主方法进入链路；注册处理器；`uploads.max_bytes`；`display.name` 标题（D-10）；`ops.ui.download` |
| Gateway `management/registry.py` | 释放活动包前以 `removed` 关闭开场；卸载时清除偏好记录 |
| `channels/web/app_web.py` | 转发 `/extension-uploads/`（流式请求体）与 `/extension-downloads/` |
| 前端 `catalog/`、`renderer/adapters/webuiFileImport/` | `FileImport` 规则与适配器 |
| 前端 `transfers/` | 导入服务（凭据、上传、完成）与下载处理 |
| 前端 `splash/` | 读取 `customized` 合并顺序 |
| 前端 `features/settings/registry/` | 插件设置经 S_18 共享目录与位置声明合成，不注册固定“扩展”模块（D-12） |
| 前端 `features/settings/SettingsPageLayout.tsx` | 2026-11-07 起不再修改，恢复目标分支原样（D-11） |
| 前端 `react/WebUIAppRoot.tsx` | 提供导入服务，订阅 `webui.download` |

### 5.2 状态与生命周期

开场偏好的生命周期见 S_10 第 4 节；导入凭据：开始 → 传输中 → 全部接收 → 完成（作废并在 Handler 后删除目录），
任一阶段过期、断线或包释放都直接作废并删除目录。下载凭据：签发 → 第一次 `GET` 后作废，或过期、断线、包释放时作废。

## 6. 影响面与实施设计

同步 S_17（`FileImport`、设置槽位）、S_09（顺序与原因）、S_06（宿主方法与新路由）、S_01（配置、`ops.ui.download`、宿主文件）、
S_02 槽位表、概览、计划与模块 `AGENTS.md`；新增中英文界面文字。

## 7. 兼容与迁移

`webui.splash.providers` 只增加字段，旧前端忽略 `customized`；没有偏好文件时顺序与 GW-11 一致。
既有插件不受影响；`application.settings` 不变。

## 8. 验证设计、执行记录与结论

### 8.1 验证设计与通过条件

| 编号 | 场景 | 通过条件 |
| --- | --- | --- |
| F_13-V-01 | 设置槽位与页面列表 | 只列出声明该槽位的活动包；标题取 `display.name`（`{zh, en}`）；定向 `ui.ready` 受请求策略约束 |
| F_13-V-02 | 宿主方法与请求策略 | 要求登录的 USER 策略拒绝未认证的帧，认证后应答；无 USER Handler 时仍由 Gateway 应答 |
| F_13-V-03 | 开场偏好 | 安装顺序、用户顺序、恢复默认、停用、非法参数；重启后保留；卸载后删除 |
| F_13-V-04 | `disabled` / `removed` | 正在播放的实例被清除并通知一次；版本号不复用 |
| F_13-V-05 | 导入 | 清单校验、大小上限、凭据绑定、远程浏览器可上传、超出与不足、完成后插件收到相对路径、目录删除、过期与断线清理 |
| F_13-V-06 | 导出 | 路径越界与符号链接拒绝、帧内容、一次性（不论来源）、响应头、远程浏览器可下载 |
| F_13-V-07 | `app_web` | 两条路由转发、请求体流式、远程客户端同样转发 |
| F_13-V-08 | 前端 | 组件契约两端一致；导入服务、下载处理、开场排序、设置模块 |
| F_13-V-09 | 回归与构建 | Gateway 扩展用例、Web 渠道用例、前端 WebUI 各套件通过；`npm run build` 成功 |
| F_13-V-10 | 浏览器 | Chromium 中设置模块、开场排序、导入与导出的完整往返 |

### 8.2 实际执行与证据

环境：macOS（arm64），Python 3.11.17（独立虚拟环境，agent-core 31fee3a），Node 22.23.4，
Chrome for Testing `chrome-headless-shell` 157.0.8103.38。

| 编号 | 用例或命令 | 结果 |
| --- | --- | --- |
| F_13-V-01～V-04 | `test_gateway_application_settings.py` 7 项（页面列表与标题、定向 `ui.ready`；无 USER Handler 时应答；要求登录的 USER 策略拒绝未认证的帧、认证后应答；安装顺序、用户顺序、停用、非法参数、重启保留与恢复默认；偏好文件损坏；停用正在播放的开场只通知一次且版本号预留；重载、升级与卸载以 `removed` 关闭并在卸载后删除偏好记录） | 通过 |
| F_13-V-05、V-06 | `test_gateway_file_transfer.py` 37 项（ZIP 与目录导入往返、15 种清单拒绝、节点与版本、远程对端上传与错误请求体、过期与连接数上限与断线清理、启动清理、远程对端下载后一次性作废与响应头、7 种越界导出、包释放、7 种 `FileImport` 事件数据） | 通过 |
| F_13-V-07 | `test_app_web_extension_transfers.py` 3 项（请求体在收齐前已开始转发、下载转发、远程客户端的上传与下载同样转发） | 通过 |
| F_13-V-08 | `webuiGatewayCases.json` 新增 13 条（Gateway `test_gateway_ui_render.py` 与前端 `test:webui-components` 同一份）；`test:webui-components` 新增 9 项（应答解析与顺序计算、已保存顺序不前置外观提供者、导入服务、`FileImport` 选择上传与派发、无导入服务时禁用、下载处理、开场设置的排序停用与恢复、空态与断线、设置页签只挂载选中的包）；`test:webui-contracts` 新增 1 项并更新组件与槽位清单 | 通过 |
| 改动前对照 | 暂存源码改动、只保留测试后运行 | Gateway 两份新用例收集失败（缺少 `host_settings`、`transfers`）；去掉新模块导入后 44 项中 38 项失败，其余 6 项是非法事件数据的反例（旧代码不认识该组件，同样拒绝）；`app_web` 3 项失败；共享用例 3 项、开场提供者 1 项失败；`webui-contracts` 2 项失败；`webui-components` 因缺少新模块无法打包 |
| 修订对照（远程开放） | 先把用例改为期望远程可用，在只服务本机的实现上运行 | Gateway 2 项（远程上传、远程下载）、`app_web` 1 项、前端 1 项（403 不再提示“只能在本机导入”）失败；移除回环判断后全部通过 |
| F_13-V-09 | `tests/unit_tests/gateway/extensions_v2/` 483 项；`tests/unit_tests/channels/web/` 与 `test_trajectory_http.py` 150 项（1 项既有跳过）；前端 contracts 151、protocol 14、components 264、renderer 17、runtime 21、client 22、events 93、transport 9；`test:i18n-locales` 4；`tsc` 与 `npm run build` | 全部通过；构建仅有既有的分包与体积提示 |

F_13-V-10（2026-11-05，D-08 之前的页签布局）：验证页（本地验证脚本，未随仓库提交）中，验证服务启动真实的 `GatewayRuntime` 与 Gateway 路由，
`/harness-ws` 按 WebChannel 的帧格式把每个请求交给 `GatewayRuntime.intercept`；页面打包真实的 `SettingsPageLayout`
（“常规”与“扩展”两个模块）、`createWebClientTransport`、导入服务与 `FileImport`，加载 `npm run build` 产出的样式表。
三个测试包：两个声明设置页与开场，一个只声明开场。

| 场景 | 结果 |
| --- | --- |
| 打开“扩展”模块 | 依次发出 `webui.splash.settings`、`webui.settings.pages` 与设置页 `ui.ready`；三个提供者按安装顺序，标为默认顺序；两个页签标题取插件显示名，插件页面渲染 |
| 下移第一个提供者 | 界面与 `webui.splash.providers` 都变为新顺序，`customized` 为 true |
| 停用一个提供者 | 开关关闭；`webui.splash.providers` 不再包含它；`host/splash.json` 记录用户顺序与停用项 |
| 恢复默认并重新启用 | 回到安装顺序，`customized` 为 false |
| 切换页签 | 只挂载选中的包，第二个包收到 `ui.ready` 并显示自己的页面 |
| 导入 ZIP（300 KiB） | 经 `/extension-uploads/` 写入后插件收到 `.uploads/<凭据>` 与清单，移出后磁盘大小一致；宿主随后删除临时目录，未完成凭据为 0 |
| 导入目录（两层、两个文件） | 清单保留目录内相对路径，插件收到的两份文件大小一致 |
| 导出备份 | 插件调用 `ops.ui.download` 后页面收到 `webui.download`；浏览器保存为“WorkSwarm 主题备份.zip”，内容与插件写入一致；同一地址再次请求返回 404 |
| 与“常规”模块对照 | 行标题、说明文字与内容左边距的计算样式一致；开场分组为一张带分隔线的卡片 |
| 560 px 宽 | 两个模块都有 24 px 横向溢出（设置页布局本身，来自导航与页头），扩展模块没有额外溢出 |

验证中发现并修正：开场行原先各自包在一层 `div` 里，分组样式选不到，每行显示成单独的卡片；改为行直接位于设置项下，测试标识移到行标题。

远程开放的修订之后，以上场景在 Chromium 中重跑，结果一致。浏览器与验证服务都在本机，远程对端只在单元用例中以非回环地址模拟，
两台机器之间尚未实测。

F_13-V-11（2026-11-06，D-08）：

| 范围 | 环境与命令 | 结果 |
| --- | --- | --- |
| 前端单元 | `test:webui-components` 270 项，新增 4 项：插件项排在“扩展”之后、读取条件（访问结果、连接、失败）、只挂载所选包、“扩展”只含宿主功能；在改动前的代码上因 `pluginPages.tsx` 不存在而无法构建。`test:settings-panel` 62 项（修正 GW-12 遗留的模块顺序断言：当时把 `extensionsModule` 加入定义却未更新该断言）；`test:i18n-locales` 4；contracts 151、protocol 14、renderer 17、runtime 21、client 22、events 93、transport 9；`tsc`、`npm run build` | 通过 |
| 验证页 | 本地 CDP 验证脚本（未随仓库提交），三个测试包，Chromium 157 | 导航依次为常规、扩展、WorkSwarm Theme Studio、Release Notes；“扩展”只有开场分组、没有导入控件；插件项的标题为插件名，切换时只挂载所选包；导入、导出（地址一次性）、预览与 V-10 一致；“扩展”的行样式与“常规”一致 |
| 真实应用 | 隔离实例：开发分支的 Gateway、AgentServer、`app_web` 与构建产物，安装主题背景 2.0.0 与登录示例 | 设置导航为“常规……已归档的任务、扩展、主题背景、实验功能”；“扩展”的开场提供者显示“主题背景”；登录前后都能打开插件项 |

验证中发现并修正：插件名较长时按钮随名称撑宽导航列（导航列表为 grid，子项默认最小宽度取内容宽度）；改为按钮可收缩、名称单行省略，完整名称放在悬停提示中。

F_13-V-12（2026-11-07，PR #10763 第二轮检视）：

| 问题 | 修订 | 用例（修复前失败） |
| --- | --- | --- |
| 实例键规则不一致：S_18 声明接受 `settings:main`，Gateway 的 `webui.dispatch`、开场生命周期帧与 `webui.upload.begin` 却只接受 `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`，声明成功的设置页连 `ui.ready` 都被拒绝 | `webui/models.py` 的 `valid_instance_key()` 作为唯一规则（与渲染目标、AgentServer 相同），各处共用；共享用例补充冒号、中间空格与 200 字节边界 | `test_every_declarable_instance_key_renders_dispatches_and_imports`（3 种键）；`test_instance_keys_outside_the_shared_rule_are_refused_everywhere` 修复前后都通过 |
| 并发清理过期导入时 `KeyError`：先取过期列表，等待删除目录后才逐个移除记录 | 先同步移除记录，再删除目录；开始导入时作废最早凭据也改为先移除记录 | `test_concurrent_expiry_removes_each_ticket_once` |
| 同一连接并发开始导入可越过 4 个凭据的上限：新凭据在创建目录之后才登记 | 凭据在任何等待之前登记；准备期间被挤出或包被释放时返回 `TARGET_INACTIVE` 并删除目录 | `test_concurrent_imports_of_one_connection_respect_the_open_ticket_limit`（修复前留下 7 个） |
| 下载准备期间包被释放，仍向浏览器发送无法取回的 `webui.download` | 清理过期凭据之后、生成凭据之前再次核对激活 | `test_a_release_while_the_download_is_prepared_sends_no_offer` |
| 开始上传在清理过期凭据期间遇到重载，按旧节点校验的文件取得新代次凭据；同代次重渲染也可能更改文件规则 | 始终绑定最初的激活代次、实例与渲染版本，在清理之后和目录创建之后分别核对；变化时返回 `TARGET_INACTIVE` 并清除准备中的目录 | `test_import_preparation_refuses_a_target_changed_while_waiting`（两个等待点 × 重载、重渲染、断线共 6 项；修复前 4 项失败） |

F_13-V-13（2026-11-07，上传取消与卸载的资源边界）：

宿主导入复用 Core 的 `run_file_operation` 等待被取消调用的文件线程结束；打开文件的线程先把句柄登记到
接收任务持有的 `ExitStack`，即使取消导致调用方取不到返回值，清理仍能关闭实际句柄。
准备、接收与清理都持有包的文件操作计数，卸载在其结束前返回 `EXTENSION_PACKAGE_BUSY`；
取消目录准备还会作废已登记凭据并删除实际创建的目录。传输中凭据被撤销时，最后一个接收任务关闭文件后重试目录清理。
没有增加另一个文件线程等待机制，也不把插件取得路径后自行读写的操作纳入宿主计数。

新增 3 项回归：文件打开线程被阻塞时取消一次或两次、目录创建线程被阻塞时取消。
用例检查卸载先返回忙，线程结束后句柄关闭、临时文件或目录消失、接收状态清空，重试卸载成功且数据目录不再出现。
单次取消的旧实现可复现句柄泄漏。最终相关后端 913 通过、1 跳过，前端相关 6 个套件共 605 项通过；
命令和环境见开发计划本轮台账，未重做真实桌面或跨机器传输验证。

F_13-V-13（2026-11-07，PR #10763 第四轮检视）：上一轮在关闭开场前只调用 `validate_package()`，它只检查浏览器声明；新版本若注册不存在的事件，
仍会先关闭旧开场、注销旧版本，随后注册失败，旧版本不再运行。现在切换分为准备（校验并注册不运行的新 Handler）与提交两步，
准备成功后才关闭旧开场（S_01 第 3.2 节、不变量 14）。回归用例 `test_an_upgrade_whose_handlers_cannot_register_keeps_the_running_version`、
`test_a_reload_whose_handlers_cannot_register_keeps_the_running_version` 修复前失败（旧开场已以 `removed` 关闭），修复后旧版本继续运行并照常应答。

F_13-V-14（2026-11-07，PR #10763 第五轮检视，2 项均复现）：
- 等待旧包 `ui.closed` 时取消安装：准备中的 Handler 被撤销，但外层只捕获 `Exception`，被取消的安装没有删除新副本；旧开场已消失，
  重新创建运行时后启用了这个没有提交的版本。现在以开始关闭旧开场为提交点：之前的步骤都不等待；之后操作运行到结束，取消在完成后才抛出；
  安装只在新版本真正提交后保留副本。卸载使用同样的提交点。
- 等待期间停机后，提交仍然成功，留下有激活记录、没有 Handler 的状态。现在准备项由运行时按运行周期登记，停机终止它们，之后的提交失败。
- 回归用例：`test_a_cancelled_upgrade_finishes_once_the_running_splash_is_closing`（修复前取消后仍运行旧版本、旧开场已关、新副本留存）、
  `test_a_switch_prepared_before_shutdown_cannot_commit_after_it`（修复前报告成功）、`test_a_cancelled_uninstall_finishes_once_the_running_splash_is_closing`。
- 本条的“按运行周期登记”与“不运行的新 Handler”已由 V-15 替代。

F_13-V-15（2026-11-07，PR #10763 第六轮架构检视，2 项均复现）：
- 收尾期间调用方被再次取消时，上一轮的等待被打断，切换脱离调用方继续运行：安装看到尚未提交就删除新副本，切换随后照样提交，
  1.1.0 在运行而目录已不存在，管理锁也提前释放。现在沿用 agent-core `run_file_operation` 的算法，持续等待子任务真正结束，
  再抛出第一次取消，并读取子任务结果写入日志；复用的是算法，切换仍在事件循环中运行。
- 新版本不运行的 Handler 在关闭旧开场期间一直挂在总线上：旧 Handler 在提交前进入、提交后才传递事件时，事件链快照里的新 Handler 已经生效，
  同一帧被两个版本处理（复现得到 `['old', 'new']`）。现在准备阶段只用不执行插件代码的占位 Handler 试注册并立即注销，
  提交时在同一同步步骤中先注册新批次、后注销旧批次（agent-core `replace_published`）；准备中的生效开关删除，
  已释放激活的 Handler 屏障保留。提交时注册失败也放弃准备项并释放模块，旧版本照常运行（S_01 第 3.2 节、不变量 14）。
- 运行周期计数已被准备项登记完全覆盖，删除；停机后不再新建准备项。卸载“运行到结束”改写为保证收尾，并写明第二次忙检查仍可能拒绝；
  取消结果与 AgentServer 的差异记入 S_01 第 3.2 节。
- 回归用例：`test_an_upgrade_cancelled_twice_still_waits_for_its_switch`（修复前调用方提前返回）、
  `test_a_chain_running_across_a_switch_reaches_one_version_only`（修复前 `['1.0.0', '1.1.0']`）、
  `test_a_commit_whose_registration_fails_keeps_the_running_version`、`test_nothing_is_prepared_once_the_runtime_has_shut_down`，修复前均失败。

### 8.3 验证结论

第 2.2 节场景在单元范围与 Chromium 中成立。浏览器验证中 Gateway 运行时与路由是真实的，WebChannel 以验证服务中的最小实现代替；
真实 WebChannel 往返与登录插件同时存在的流程已在 EXTV2-GW-14 的隔离实例中验证（V-11 与开发计划台账）；桌面打包版中的保存对话框（`downloadFile`）尚未验证。暗色主题未单独检查（本条目没有改动主题变量）。

F_13-V-16（2026-11-09，D-12）：

- 删除宿主固定“扩展”菜单与“启动开场”设置页，未迁移到其他宿主菜单；清理专用前端组件、样式、设置读写工具、测试导出与中英文文案。
- 单元验证：`npm run test:webui-components` 368 项、`node --test tests/pluginSettings.test.mjs` 17 项、
  `npm run test:i18n-locales` 4 项通过。`npm run test:settings-panel` 62 通过、2 失败；删除前后失败用例相同，
  分别为既有视觉契约与 Harness 翻译键断言。`git diff --check` 与 `git diff --cached --check` 通过。
- 完整构建：`npm run build` 未通过，当前工作区原有 `App.tsx` 的 `conversationTeamId` 调用不符合参数类型（TS2353）；
  该文件未在本次修改范围内，未据此宣称构建通过。
- 真实联调：当前工作区的 Gateway、AgentServer、Web Client 源码，独立数据目录与 19500 / 19501 / 19502 / 19510 端口，
  Chrome 157.0.8103.38 无头浏览器。通过运行实例的正式本机包管理入口安装录屏交付的 `workswarm-theme 1.0.2` 原始 ZIP，
  SHA-256 为 `ff8ac168af522de1f4715f3a01a5c7ac86a962af12d34c0eb60bc6ba1af6dc86`，安装后的 45 个文件与 ZIP 逐一一致。
- 插件保留原声明 `after: "extensions"`，设置导航没有宿主 `extensions` 项，插件“主题背景”菜单正常追加，目录无位置或加载错误。
  四套主题切换、滑杆 / 开关 / 文本配置、刷新后的配置与主题恢复、流光极昼开场预览、恢复默认外观与重新启用、
  1600 / 1000 像素宽度下的设置访问均通过；页面无运行错误，插件素材无失败请求，也未发出已删除页面的开场设置读写请求。
- 配置联调逐项等待正式交互回执后再操作；首次快速连续修改控件时曾观察到文本回写，未单独归因，不能将本次结果扩大为并发配置验证。
  本轮未重做英文 / 暗色模式、桌面打包版、跨机器传输与导入导出验证；脚本、原始结果与截图保存在本地交付目录，未入库。

## 9. 风险与已知遗留

- 远程浏览器使用导入与导出时，凭据随请求地址经网络传输；部署方应使用 HTTPS。两台机器之间的导入与导出尚未实测。
- 桌面打包版与真实 Gateway 的联调随壁纸插件进行。
- 插件设置页放在设置行内容中，复杂页面（如主题图库）需随插件调整布局（D-11）。
- 设置页在 560 px 宽时的既有横向溢出不属于本条目，未处理。
- `FileImport` 的“完成”状态在插件重新渲染后不保留（节点随新版本重建）；插件应在页面中自行反映导入结果。
