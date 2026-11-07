# Gateway Extensions V2 特性开发计划

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | 开发计划 |
| 编号作用域 | `EXTV2-GW-NN` |
| 所属模块 | `jiuwenswarm/gateway/extensions_v2/` |
| 最近一次修订日期 | 2026-11-05 |
| 维护责任 | Gateway Extensions V2 模块维护者 |

## 1. 计划说明

本计划与 AgentServer 的 `EXTV2-NN` 分别编号，互不占用。每个条目对应一个可独立提交、可独立验证的能力单元；条目与其 feature 文档、spec 文档、测试位置一一对应。

进度记为：`未开始` / `进行中` / `已实现（单测通过）` / `已验证（联调用例通过）`。负责人一栏登记认领人；本栏建立前的条目未登记负责人。

## 2. 条目清单

| 编号 | 条目 | 交付内容 | Feature | Spec | 测试 | 负责人 | 进度 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| EXTV2-GW-01 | 装配与运行时骨架 | `assembly` / `bootstrap` / `runtime` / `config` / `loader` / `registry` / 渠道钩子 | [F_01](../features/F_01_assembly-and-runtime.md) | [S_01](../specs/S_01_gateway-extension-runtime.md) | `test_gateway_request.py` | 未登记 | 已实现（单测通过） |
| EXTV2-GW-02 | `gateway.request` 与本地路由 | 事件类型、CORE Handler、USER 鉴权职责、宿主本地方法表 | [F_02](../features/F_02_gateway-request-and-auth-gate.md) | [S_01](../specs/S_01_gateway-extension-runtime.md) | `test_gateway_request.py`、`test_gateway_package_management.py` | 未登记 | 已实现（单测通过） |
| EXTV2-GW-03 | `ui.render` | 渲染协议、校验、登记、传输、`ops.ui.render` | [F_03](../features/F_03_render-service.md) | [S_02](../specs/S_02_ui-render.md) | `test_gateway_ui_render.py` | 未登记 | 已实现（单测通过） |
| EXTV2-GW-04 | `ui.store` | 作用域存储、写即下发、`ops.ui.store` | `F_04_ui-store.md` | `S_03_ui-store.md` | `test_gateway_ui_store.py` | 未登记 | 未开始 |
| EXTV2-GW-05 | `ui.click` | entry/footer 上行分派与动作校验 | [F_07](../features/F_07_local-login-integration.md) | [S_06](../specs/S_06_local-browser-bridge.md) | `test_gateway_browser_bridge.py` | 未登记 | 已实现（单测通过） |
| EXTV2-GW-06 | `ops.http.fetch` 与示例包 | 出站 HTTP、白名单策略、[auth_login_demo](../../../../extensions_v2/examples/auth_login_demo/) | [F_06](../features/F_06_http-fetch.md) | [S_05](../specs/S_05_http-fetch.md) | `test_gateway_http_fetch.py` | 未登记 | 已实现（单测通过） |
| EXTV2-GW-07 | 应用级 UI 包与素材交付 | 应用级槽位贡献包统一取得激活代次、交互分派、`ops.ui.asset_url` 与素材；素材路由加沙箱策略；`app_web` 流式转发素材 | [F_08](../features/F_08_application-packages-and-assets.md) | [S_06](../specs/S_06_local-browser-bridge.md)、[S_01](../specs/S_01_gateway-extension-runtime.md) | `test_gateway_browser_bridge.py`、`test_gateway_application_assets.py`、`tests/unit_tests/channels/web/test_app_web_extension_assets.py` | Song Haoran | 已实现（单测通过） |
| EXTV2-GW-08 | Gateway 存储与包数据目录 | 接入 Core `JsonStore` 与 `ops.store.*`；`ops.data.root()`；数据素材；卸载清理 | [F_09](../features/F_09_package-store-and-data.md) | [S_01](../specs/S_01_gateway-extension-runtime.md)、[S_06](../specs/S_06_local-browser-bridge.md) | `test_gateway_package_data.py` | Song Haoran | 已实现（单测通过） |
| EXTV2-GW-09 | 媒体容器与应用背景 | `MediaSurface` 根组件（图片、视频、沙箱页面）、令牌与来源校验的消息协议、`application.background` 槽位与前端背景宿主 | [F_10](../features/F_10_media-surface-and-background.md) | [S_07](../specs/S_07_media-surface.md)、[S_17](../../../../extensions_v2/docs/specs/S_17_webui-component-props.md)、[S_02](../specs/S_02_ui-render.md) | `test_gateway_ui_render.py`、`test_gateway_application_assets.py`；前端 `test:webui-components`、`test:webui-protocol`、`test:webui-contracts` | Song Haoran | 已实现（单测与浏览器验证通过；桌面打包版联调随 GW-11） |
| EXTV2-GW-10 | 受控外观 | `ops.appearance.apply`、宿主派生的强调色与半透明模式、按连接的提供者、包释放时撤销外观并清除实例、重载后重新通知 | [F_11](../features/F_11_controlled-appearance.md) | [S_08](../specs/S_08_appearance.md) | `test_gateway_appearance.py`；前端 `test:webui-runtime`、`test:webui-components`、`test:webui-transport` | Song Haoran | 已实现（单测与浏览器验证通过） |
| EXTV2-GW-11 | 启动门控与开场队列 | 中性层、候选提示、启动标识、开场声明与提供者、显示型槽位的平台帧、定向 `ui.ready`、`application.splash`、时限与心跳、长按跳过、`ui.closed`、登录入口协调 | [F_12](../features/F_12_startup-gate-and-splash.md) | [S_09](../specs/S_09_startup-splash.md)、[S_07](../specs/S_07_media-surface.md) | `test_gateway_splash.py`、`test_app_web_splash_hint.py`；前端 `test:webui-components` | Song Haoran | 已实现（单测与浏览器验证通过；桌面打包版联调待真实插件） |
| EXTV2-GW-12 | 应用级设置页 | `application.settings.page`、“扩展”设置模块（宿主功能）与每个插件单独的设置项（F_13 D-08）；宿主方法；开场顺序与停用及其保存；`ui.closed` 的 `disabled` / `removed`；`FileImport` 与导入凭据、上传路由；`ops.ui.download` 与下载路由；`app_web` 转发（本机与远程浏览器均可用） | [F_13](../features/F_13_application-settings-page.md) | [S_10](../specs/S_10_application-settings.md)、[S_09](../specs/S_09_startup-splash.md)、[S_17](../../../../extensions_v2/docs/specs/S_17_webui-component-props.md)、[S_02](../specs/S_02_ui-render.md) | `test_gateway_application_settings.py`、`test_gateway_file_transfer.py`、`test_app_web_extension_transfers.py`；前端 `test:webui-components`、`test:webui-contracts` | Song Haoran | 已实现（单测与浏览器验证通过；桌面打包版联调待真实插件） |
| EXTV2-GW-13 | 开场预览 | `ops.splash.preview()`、下行帧 `webui.splash.play`、开场 `ui.ready` 的 `preview` 字段、门控开放后的单次预览播放 | [F_14](../features/F_14_splash-preview.md) | [S_09](../specs/S_09_startup-splash.md) | `test_gateway_splash.py`；前端 `test:webui-components` | Song Haoran | 已实现（单测与浏览器验证通过；桌面打包版联调待真实插件） |
| EXTV2-GW-14 | 真实环境联调 | 在隔离实例中运行开发分支的 Gateway、AgentServer、`app_web` 与前端构建产物，安装主题背景插件与登录示例，用无头 Chromium 走完首次启动、设置、导入导出、登录前后与断线；联调中发现的缺陷就地修复 | [F_13](../features/F_13_application-settings-page.md)（D-08）、[F_12](../features/F_12_startup-gate-and-splash.md)（V-10） | [S_01](../specs/S_01_gateway-extension-runtime.md)、[S_10](../specs/S_10_application-settings.md) | `test_gateway_request.py`、`test_gateway_package_management.py`；前端 `test:webui-components`、`test:settings-panel` | Song Haoran | 已完成（A 档真实 Gateway 与 Web、B 档 Electron 开发模式通过；打包版与 Windows 由打包团队验证） |

## 3. 依赖与开工条件

```mermaid
flowchart LR
    G1["GW-01 装配骨架"] --> G2["GW-02 request 与本地路由"]
    G2 --> G3["GW-03 ui.render"]
    G3 --> G4["GW-04 ui.store"]
    G3 --> G5["GW-05 ui.click"]
    G5 --> G6["GW-06 http.fetch / 独立 Mock 示例"]
    G5 --> G7["GW-07 应用级 UI 包与素材"]
    G7 --> G8["GW-08 存储与数据目录"]
    G7 --> G9["GW-09 媒体容器与背景"]
    G9 --> G10["GW-10 受控外观"]
    G9 --> G11["GW-11 启动门控与开场"]
    G7 --> G12["GW-12 应用级设置页"]
    G11 --> G13["GW-13 开场预览"]
    G12 --> G13
```

- GW-01 是其余条目的前置：事件类型必须在装配阶段先注册。
- GW-03 必须在 GW-04/GW-05 之前：`ui.store` 的下行帧与 `ui.click` 的实例判断都依赖渲染登记。
- GW-02 的鉴权由 USER 包通过 gateway.request 完成，包通过 ui.render 提交自身视图；宿主只负责协议接入。
- 当前 Mock 登录示例依赖渲染与浏览器桥接，安装激活后无需额外浏览器能力授权，不依赖未实现的 ui.store 或出站 HTTP。
- GW-13 复用 GW-11 的开场层与时限；预览入口在 GW-12 的插件设置页中。
- GW-07～GW-13 的整体设计见[应用级界面能力设计概览](application-surfaces-overview.md)；GW-07 是其余条目的前置，使不带浏览器请求绑定的应用级 UI 包也能取得交互、素材与激活代次。

## 4. 验证要求

| 编号 | 验证重点 |
| --- | --- |
| GW-01 | 单例装配、重复装配、`enabled=false`、装配失败降级、停机注销、零扩展快路径 |
| GW-02 | forward/consume/deny/local 四路由、未知路由降级、普通请求无内置登录要求、USER 鉴权拒绝、`ROUTE_UNSUPPORTED` 错误 |
| GW-03 | 渲染树校验边界、revision 递增、槽位白名单、传输失败返回 `delivery_failed`、包渲染登录树 |
| GW-04 | 三种作用域隔离、TTL 过期、`value=None` 撤销、写即下发帧内容、断线只清 connection |
| GW-05 | 槽位/包/实例不匹配拒绝、payload 越界拒绝、实例清除后回执 `inactive` |
| GW-06 | 白名单空拒绝一切、`host:port` 匹配、超时与响应体上限、示例包登录成功/失败两条路径 |
| GW-07 | 无请求绑定的应用级 UI 包可收到 ready/click 与素材；请求绑定与客户端操作仍限于声明 client 的包；素材越界与失活代次被拒绝，USER 请求策略可拒绝或接管素材；沙箱策略头；`app_web` 转发、Range 与流式输出 |
| GW-08 | 存储按包隔离并跨重载与重启保留；数据目录与数据素材越界、符号链接拒绝；卸载删除且失败可重试；重载保留 |
| GW-09 | 两端字段、根节点与槽位结论一致（共享用例）；图片、视频、页面渲染与宿主策略；消息的来源、令牌与事件校验；就绪超时、错误与减少动态回退；背景层不接收输入、根元素标记；声明背景槽位的包收到 `ui.ready`；真实浏览器显示 |
| GW-10 | 外观对象两端结论一致；提供者切换、非提供者撤销、断开连接；卸载与重载时撤销外观并清除实例且版本号不复用；重载后广播；前端帧排序与重连核对；强调色对比度与半透明预设；Chromium 中派生变量与 antd 取色 |
| GW-11 | 声明校验与提供者顺序；请求策略拒绝一切时平台帧仍到达包；`ui.closed` 一次性；候选提示；门控四种判定路径；准备、媒体就绪、心跳与上限时限；长按跳过；登录入口推迟；Chromium 中从首帧到开放的完整流程 |
| GW-12 | 页面列表与定向 `ui.ready`；宿主方法在请求策略与认证门禁之后、无 USER Handler 时仍应答；开场顺序、停用、恢复默认与重启保留、卸载清除；`disabled` / `removed` 只通知一次；导入清单、大小、凭据绑定、远程浏览器可用、过期与断线清理；导出路径边界、一次性与响应头；`app_web` 流式转发；两端组件与槽位结论一致；Chromium 中设置模块、排序、导入与导出往返 |
| GW-13 | 预览帧只发往调用连接并带声明值；未声明开场、失效激活、无连接时拒绝；停用后仍可预览；`ui.ready` 的 `preview` 必填；前端只在门控开放且空闲时播放一次、不改写启动记录；Chromium 中从设置页点击到播放结束 |
| GW-14 | 真实 WebChannel 往返（平台帧、外观、背景、开场、设置页、导入导出经 `app_web`）；登录插件存在时登录前后的完整流程；断线后连接状态被清理；同版本卸载重装后执行新代码；与正在使用的 WorkSwarm 隔离（数据、家目录、worker 套接字、端口） |

单元测试统一位于 `tests/unit_tests/gateway/extensions_v2/`。

## 5. 进度台账

| 日期 | 条目 | 变更 |
| --- | --- | --- |
| 2026-11-01 | GW-01、GW-02 | 装配骨架、`gateway.request`、认证门禁、本地路由与对应单测落地 |
| 2026-11-01 | GW-03～GW-05 | 待实现 |
| 2026-11-01 | GW-06 | `ops.http.fetch`、出站白名单、超时与体积预算落地；`auth_login_demo` 示例包待实现 |

| 2026-11-04 | GW-03、GW-05、GW-06 | 渲染、浏览器桥接及可安装 Mock 示例落地；验证记录见 F_03、F_07 |
| 2026-11-04 | GW-05、GW-06 | 移除额外授权、无消费者协议字段及同链路重复判断；Gateway 138 项、前端客户端/组件/上下文 163 项测试通过，范围与限制见 F_07 |

| 2026-11-04 | GW-02 | 移除旧 BUILTIN 认证门禁及 auth.status，统一由 USER handler 完成鉴权；更新回归后 Gateway 130 项测试通过，范围与迁移见 F_02 |
| 2026-11-05 | GW-07～GW-12 | 建立应用级界面能力条目，负责人 Song Haoran；GW-07 开工，方案见 F_08 |
| 2026-11-05 | GW-07 | 应用级 UI 包与素材交付实现，单测通过（Gateway 扩展 325 项、Web 渠道相关 155 项）；打包版联调待 GW-09，证据见 F_08 |
| 2026-11-05 | GW-08 | 包存储与数据目录实现，单测通过（Gateway 扩展 331 项），证据见 F_09 |
| 2026-11-05 | GW-09～GW-11 | 调整拆分：`application.background` 随媒体容器在 GW-09 交付，GW-10 只做受控外观，开场槽位归 GW-11；GW-09 开工，方案见 F_10 |
| 2026-11-05 | GW-09 | 媒体容器与应用背景实现，单测通过（Gateway 扩展 370 项、前端 WebUI 各套件全部通过、构建成功）；内置浏览器验证图片、视频、超时回退与素材响应头，页面消息往返的浏览器验证待补，证据见 F_10 |
| 2026-11-05 | GW-09 | Chromium 157 无头模式补充验证：页面双向消息、错误回退、减少动态、就绪超时与清除通过；隐藏 iframe 被暂停渲染的推测未得到证实，证据见 F_10 |
| 2026-11-05 | GW-10 | 受控外观实现：Gateway 扩展 413 项、前端 WebUI 各套件全部通过、构建成功；Chromium 157 中验证强调色派生、antd 取色、半透明预设与撤销；同时补正包释放时不通知浏览器的缺口，证据见 F_11 |
| 2026-11-05 | GW-11 | 启动门控与开场队列实现：Gateway 扩展 426 项、Web 渠道 147 项、前端 WebUI 各套件全部通过、构建成功；Chromium 157 中验证首帧中性层、播放、登录入口推迟、刷新不重播与长按跳过；背景与开场的生命周期帧改为平台帧，证据见 F_12 |
| 2026-11-05 | GW-12 | 应用级设置页实现：Gateway 扩展 483 项、Web 渠道 150 项、前端 WebUI 各套件全部通过、构建成功；Chromium 157 中验证“扩展”设置模块、开场排序与停用、ZIP 与目录导入、导出下载；导入与导出本期只服务本机连接，证据见 F_13 |
| 2026-11-05 | GW-12 | 按用户决定，导入与导出对远程浏览器同样开放：移除 Gateway 与 `app_web` 的回环地址判断及前端“只能在本机导入”提示，访问权只来自一次性凭据；Gateway 扩展 483 项、Web 渠道 150 项、前端 WebUI 各套件通过，Chromium 中重跑一致；两台机器间未实测，证据见 F_13 |
| 2026-11-05 | GW-13 | 建立开场预览条目（壁纸插件设置页的“预览开场”需要），负责人 Song Haoran；开工，方案见 F_14 |
| 2026-11-05 | GW-13 | 开场预览实现：Gateway 扩展 485 项、Web 渠道 150 项、前端 WebUI 各套件全部通过、构建成功；Chromium 157 中验证从设置页点击到预览播放、结束并恢复界面，证据见 F_14 |
| 2026-11-06 | GW-12 | 按用户要求修订设置页（F_13 D-08）：“扩展”只放宿主的开场设置，每个插件在设置导航中单独一项；前端 components 270、settings-panel 62（同时修正 GW-12 遗留的模块顺序断言）、WebUI 其余套件、`tsc` 与构建通过，Chromium 与真实应用验证见 F_13-V-11 |
| 2026-11-06 | GW-14 | 建立真实环境联调条目，负责人 Song Haoran。A 档完成：隔离实例（独立数据目录与家目录、`/tmp` 下的 worker 套接字、第 2 组端口）中，首次启动开场、背景与外观、设置导航与主题背景页、切换、预览、经 `app_web` 的导入（204）与导出、刷新不重播、登录示例存在时登录前的开场与背景及登录后的设置页均通过，控制台无插件相关错误。联调发现并修复两处上游缺陷：渠道断线钩子多传一个参数导致 `notify_gateway_disconnect` 从未执行（S_01 签名）；加载器模块名固定，同版本卸载重装复用缓存的子模块（S_01 不变量 11）。Gateway 扩展 487 项、Web 渠道 150 项通过。首次运行未隔离 worker 套接字，测试实例连上了已安装 WorkSwarm 的常驻 worker（未关闭它，并向其日志目录追加了一行），随即改为独立家目录与套接字 |
| 2026-11-06 | GW-14 | B 档完成（Electron 43.5.1，安装包校验通过）：开发分支的 Electron 外壳以独立应用名、配置目录、家目录、数据目录与 worker 套接字运行，冷启动时早期导航、中性层、门控覆盖工作界面、开场播放、刷新不重播、重新启动按新启动标识重播均符合设计；无用户操作时 WebAudio 与 `<audio>` 开场音乐都能自动播放；已安装 WorkSwarm 的配置与数据目录无变化。证据见 F_12-V-10 |
| 2026-11-06 | GW-07～GW-14 | 合入 `releases/0.2.9.beta3`：素材请求改为与其他 HTTP 请求一样经 `gateway.request`（采用目标分支契约，F_08 D-03），导入与导出仍凭一次性凭据访问；目标分支已移除 BUILTIN 认证门禁与 `client_grants`，宿主方法改为只依赖 USER 请求策略（F_13 D-02）；素材 CSP 沙箱指令与前端 Iframe 对齐（`allow-scripts allow-forms allow-popups allow-modals allow-downloads`）并保留目标分支的 CORS；外观共享用例改名 `gatewayAppearanceCases.json`，避免与目标分支同名夹具冲突；断线钩子签名采用目标分支修复；Gateway 插件设置页与 AgentServer 插件设置并存（F_13 D-09）。复验（agent-core `31fee3a`，Python 3.11.17，Node 22.23.4）：Gateway 扩展与 Web 渠道相关 771 通过、1 跳过、13 失败（12 项在未修改的目标分支上同样失败，另 1 项为同类 CLI 安装用例，均因本地 agent-core 早于锁定版本）；前端 test:webui-components 358、test:webui-contracts 167、test:webui-protocol 14、test:webui-runtime 21、test:webui-transport 9、test:webui-client 25、test:plugin-settings 16、test:i18n-locales 4 均 0 失败，test:settings-panel 62 通过、2 失败（与目标分支相同）；`npm run build` 通过。未在目标分支上重做浏览器与桌面联调 |
| 2026-11-07 | GW-07～GW-14 | 变基到 `releases/0.2.9.beta3` 的 `9780a04`（系统插件包统一为 `manifest.json`，包归属改为 `package.domain`）：插件标题改取 `display.name` 并以 `{zh, en}` 下发、前端按界面语言显示（F_13 D-10）；`app_web` 的开场提示改为扫描 `manifest.json`；用例中的 Gateway 包改为 `manifest.json`。提交作者与提交者统一为 Song Haoran |
| 2026-11-07 | GW-07～GW-14 | PR #10763 检视修改：外观在途帧随包释放撤销（S_08 不变量 7）；释放后清理包模块（S_01 不变量 11）；开场设置写盘失败返回 `SETTINGS_WRITE_FAILED` 且保持原状态；Vite 开发代理补齐 `/extension-uploads`、`/extension-downloads`；背景 HTML 不委派 autoplay（浏览器中静音；桌面端维持现状并写明限制，背景不得发声由插件遵守，见 S_07 不变量 4 与 F_10）；`ops.appearance.apply`、`ops.ui.download`、`ops.splash.preview` 改为 `ui.appearance`、`ui.download`、`ui.splash.preview` 事件链；开场提示按 Gateway 规则计算且“无候选”仍向 Gateway 复核；设置统一到 S_18 共享目录（F_13 D-11）；半透明外观只覆盖主题的角色变量（先行提交 `refactor(webui)` 提供变量，F_11）。复验（agent-core `c48b5bb`）：Gateway 扩展与 Web 渠道 845 通过、1 跳过；AgentServer WebUI、代理与包管理 869 通过；Electron 42 通过；前端 WebUI 各套件与插件设置全部通过，`test:settings-panel`、`test:agent-management`、`test:trajectory-host` 的 5 项失败与目标分支相同；构建通过；Chromium 中验证了设置导航、导入、开发代理传输与背景静音 |
| 2026-11-07 | GW-07～GW-14 | PR #10763 第二轮检视（5 项均核实属实，修复前用例可复现）：卸载在 Handler 或数据调用在途时返回 `EXTENSION_PACKAGE_BUSY`，存储与 `ops.data.root()` 只接受包当前激活的 Ops，已释放激活不再开始新的 Handler 调用（S_01 不变量 12、F_09 D-05）；外观调用取得连接锁后再次核对激活，下载在生成凭据前同样再核对（S_08 不变量 7、S_10 第 7.1 节）；实例键统一为 `valid_instance_key()`，声明、渲染、交互与文件导入同一规则（S_02 第 4 节）；开场完成记录与启动标识放在同一作用域，浏览器标签页互不覆盖（S_09 第 4 节、F_12 D-04）；导入凭据先同步移除记录再删除目录，并发开始导入不越过每连接 4 个的上限（S_10 第 6.2 节、F_13 V-12）。复验（agent-core `c48b5bb`）：Gateway 扩展与 Web 渠道 867 通过、1 跳过；AgentServer WebUI、代理与包管理 872 通过；Electron 42 通过；前端 WebUI 各套件与插件设置全部通过（`test:webui-components` 360），`test:settings-panel`、`test:agent-management`、`test:trajectory-host` 的 5 项失败与目标分支相同；`tsc` 与构建通过 |
| 2026-11-07 | GW-07～GW-14 | PR #10763 第三轮（架构检视收敛，两名独立检视员与用户转来的 GPT 意见交叉核实后执行）：激活身份只有一个来源，桥接层代次由激活记录派生，`_CALLER`、渲染守卫与 `_RENDER_CHECK` 合并为带外传递的调用方，宿主动作输入不再携带代次、改写输入不能冒用其他包（S_01 不变量 13）；Handler 与数据调用合并为按包计数，重载后仍在运行的旧激活也使卸载返回忙；过期凭据同步移除、目录交给受管的后台任务删除，卸载删除数据前等待；新版本先校验再关闭正在播放的开场；背景跨帧保持挂载改为槽位策略（只用于背景），海报失败状态按地址区分，地址即内容身份（S_07 不变量 6），模糊改用 `backdrop-filter`（不变量 7 只写行为）；“无候选”提示后不再补播开场；首次访问记录只在看完或跳过后写入；工具函数去重。评估后保留：外观的连接锁、锁内复核、在途与已提交状态区分以及发送后代次比较（停机不撤销在途记录），前端开场时长上限，`index.html` 首帧中性层样式。复验（agent-core `c48b5bb`）：Gateway 扩展与 Web 渠道 886 通过、1 跳过；AgentServer WebUI、代理与包管理 872 通过；Electron 42 通过；主题背景插件 46 通过；前端 WebUI 各套件与插件设置全部通过（`test:webui-components` 367），`test:settings-panel`、`test:agent-management`、`test:trajectory-host` 的 5 项失败与目标分支相同；`tsc` 与构建通过；隔离实例（重启后端、主题背景 2.0.4）在 Chromium 中验证开场与跳过、背景与外观、设置入口，拖动暗化时背景不重载、不重提交外观 |
| 2026-11-07 | GW-07～GW-14 | PR #10763 第四轮检视（2 项均复现）：重载与安装改为准备—提交两步切换，新版本 Handler 全部注册成功后才关闭旧开场并替换，失败时旧版本保持运行（S_01 第 3.2 节、不变量 14）；卸载删除数据前只等待本包导入目录的后台删除，不再等待全部浏览器通知。3 个回归用例修复前失败（两处旧开场被关闭、卸载超时）。复验（agent-core `c48b5bb`）：Gateway 扩展与 Web 渠道 889 通过、1 跳过；AgentServer WebUI、代理与包管理 872 通过；主题背景插件 46 通过；本轮未改前端 |
| 2026-11-07 | GW-07～GW-14 | PR #10763 第五轮检视（2 项均复现）：切换以开始关闭旧开场为提交点，之后即使被取消也运行到结束，安装副本只在提交后保留；卸载同样处理；准备项按运行周期登记，停机后不能提交（S_01 第 3.2 节、不变量 14）。另修复一个因后台删除导入目录而偶发失败的用例。复验（agent-core `c48b5bb`）：Gateway 扩展与 Web 渠道 892 通过、1 跳过（连续两次）；AgentServer WebUI、代理与包管理 872 通过；主题背景插件 46 通过 |
| 2026-11-07 | GW-07～GW-14 | PR #10763 第六轮架构检视（两名独立检视员，3 项缺陷均复现）：管理操作收尾期间重复取消不再让切换脱离调用方，沿用 agent-core `run_file_operation` 的等待算法；准备阶段只用占位 Handler 试注册并立即注销，提交时同步先注册新批次、后注销旧批次，一条事件链只经过一个版本，删除准备中的生效开关与运行周期计数，停机后不再新建准备项（S_01 第 3.2 节、不变量 14，F_13-V-15）；背景媒体失败后在连接恢复时重新加载一次，只有显示画面时才标记 `active`（S_07 第 1 节、不变量 6，F_10）；卸载“运行到结束”改为保证收尾并写明第二次忙检查仍可能拒绝，取消结果与 AgentServer 的差异写入 S_01。7 个新增或改写的用例修复前失败。其余检视项（调用方改用 `trigger_context`、激活校验收敛与小清理）另行提交 |
| 2026-11-07 | GW-07～GW-14 | PR #10763 第六轮收敛（独立提交）：小清理（海象运算符、开场排序规则前端共用一处、令牌按实例创建等）；宿主操作的调用方改由 `trigger_context` 传给 CORE，嵌套事件链与后台任务不再继承调用方，渲染 CORE 显式交给传输层调用方的代次（S_01 第 4 节、不变量 13，F_09）；激活状态收敛为激活记录一个来源，激活有效性统一为一个判断，凭据持有激活，宿主动作在链开始前核对调用方。修复本 PR 早先改动遗留的集成用例 `tests/integration/test_webui_resource_browser.py`（目标分支通过、本分支失败：桥接层新增读取 `config.data_dir`，代次视图改为只读派生），其假运行时改为激活记录，使用本机已有的 chrome-headless-shell 复验通过；门控在“有候选”提示后的判定期间显示中性层，不再先显示页面底色（S_09 第 5 节、F_12） |
| 2026-11-07 | GW-08、GW-12 | 补充修复 PR #10763 的取消与跨代次边界：`ops.data.root()` 复用 Core 文件操作取消处理，后台线程结束后才释放调用计数；上传始终绑定最初的激活代次、实例与渲染版本，在清理过期凭据和目录创建之后分别核对。新增 7 项回归，修复前 5 失败、2 通过，修复后全部通过；包含新页面规则下重新上传成功的路径。复验（Python 3.11.17、agent-core `c48b5bb`）：Gateway 扩展与 Web 渠道 874 通过、1 跳过，AgentServer 共享设置契约 30 通过；前端源码本次未改，沿用 `0767763d5` 上相关 544 项通过记录。未重做浏览器、打包版或桌面冷启动 |
| 2026-11-07 | GW-03、GW-12、GW-14 | PR #10763：渲染 Ops 与提交绑定原激活；所有应用级目标保留清除版本下限；容量淘汰预留清除版本；宿主导入持有文件资源直到取消清理结束。后端 913 通过、1 跳过，前端 605 项通过；详细范围及未执行项见下方回归记录 |

### 2026-11-07 渲染与上传生命周期回归记录

负责人：Song Haoran；关联 [PR #10763](https://github.com/openJiuwen-ai/jiuwenswarm/pull/10763)，GW-03、GW-12、GW-14。
修复旧 Ops 脱离 Handler 后渲染、旧调用跨越实例锁后重建、发送失败回退清除版本、应用级旧帧复活及上传取消丢失句柄；
再次检视补齐容量淘汰的清除版本预留。实现与独立测试通过，整项真实环境验收状态仍按第 4 节记录。
技术依据见 [F_03](../features/F_03_render-service.md) 与 [F_13 V-13](../features/F_13_application-settings-page.md)。

| 验证范围 | 环境与命令 | 结果与限制 |
| --- | --- | --- |
| Gateway、Web 渠道、共享设置契约 | Python 3.11.17；独立 `.venv-v2-0120`；`PYTHONPATH` 使用锁定的 agent-core `c48b5bb` 与本工作树；独立 `JIUWENSWARM_DATA_DIR`；`python -m pytest -q --no-cov -o log_cli=false tests/unit_tests/gateway/extensions_v2 tests/unit_tests/channels/web tests/unit_tests/channel/test_trajectory_http.py tests/unit_tests/extensions_v2/webui/test_settings_contribution_cases.py` | 913 通过、1 跳过；新增后端 9 项覆盖正常新激活、旧激活、在途发送、排队、淘汰重现、取消清理与卸载重试 |
| 前端 | Node 22.23.4；前端目录运行 `npm run test:webui-components`、`test:webui-runtime`、`test:webui-client`、`test:webui-transport`、`test:plugin-settings`、`test:webui-contracts` | 366、21、25、9、17、167 项通过，共 605 项；新增 6 项清除版本回归 |
| 未执行 | 静态检查、构建、真实浏览器、打包版、桌面冷启动、用户验收 | 单元结果不替代这些验证；历史验证仍按原日期与范围理解 |

## 6. 风险与待确认

- Gateway 当前只接入 application.entry、application.sidebar.footer、application.background、application.splash 与 application.settings.page；其他槽位沿用所属宿主的链路。
- 文件导入与导出对远程浏览器开放，访问权只来自一次性凭据，凭据出现在请求地址中；远程部署应使用 HTTPS（S_10 不变量 6）。两台机器之间尚未实测。
- 鉴权由 USER 包负责，旧内置门禁已移除。平台数据路由不因示例账号变化而隔离。
- Gateway 与 AgentServer 的同名事件语义不同（`ui.click` 在两侧各自注册）；一个包只能归属一侧（`package.domain`），需要两侧能力时拆成两个包。
- 前端协议桩浏览器验证与真实 Gateway/示例接入分别记录在前端 F_02 和本模块 F_07，不将 Mock 验证解释为真实身份服务验收。
- 开发阶段（基线 `f38242f`）`uv.lock` 锁定的 agent-core `86990bd` 缺少当前代码导入的 `openjiuwen.core.events.ops_impl.store` 与 `openjiuwen.core.extension.data`，开发环境使用 agent-core `31fee3a` 验证。`releases/0.2.9.beta3` 锁定 `release/v0.1.20.post3`（2026-11-07 起为 `c48b5bb`），合入时未按该锁文件安装验证；依赖新版 agent-core 的 13 项用例在 `31fee3a` 环境中失败，其中 12 项在未修改的目标分支上同样失败，见第 5 节 2026-11-06 记录。
- `application.sidebar.footer` 的容量在本模块为 `list`，在前端槽位表与 AgentServer 常量中为 `single`，三处待统一；GW-07 不改变该槽位。
