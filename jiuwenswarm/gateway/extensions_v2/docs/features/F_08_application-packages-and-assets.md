# 应用级 UI 包与素材交付设计方案

## 1. 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | feature |
| 编号 / slug | `F_08 / application-packages-and-assets` |
| 建档日期 | 2026-11-05 |
| 计划条目 | [EXTV2-GW-07](../designs/extensions-v2-development-plan.md) |
| 整体设计 | [应用级界面能力设计概览](../designs/application-surfaces-overview.md) |
| 方案成熟度 | 已确认，已实现 |
| 关联规约 | [S_06 浏览器桥接](../specs/S_06_local-browser-bridge.md)、[S_01 运行时](../specs/S_01_gateway-extension-runtime.md) |
| 验证摘要 | 单元验证通过（2026-11-05）；打包版桌面联调未执行 |

## 2. 背景、目标与范围

### 2.1 背景与问题

Gateway 的浏览器桥接把“应用级界面”与“浏览器请求绑定”合在同一个条件下：只有声明了 `client.requestBindings` 的包
才登记激活代次、接收 `application.entry` / `application.sidebar.footer` 的 `ui.ready` 与交互、调用
`ops.ui.asset_url` 并读取 `assets/` 素材（`webui/bridge.py` 中 `enabled`、`published`、`dispatch`、`_caller`、
`asset_file` 均以 `package.client` 为前提）。请求绑定用于把登录凭据带入请求，与界面本身无关；不需要凭据的应用级
界面包（例如外观、公告类插件）因此无法交互，也无法使用素材。

素材链路另有三个缺口：

1. 打包版前端由 `channels/web/app_web.py` 提供，`_dispatch_proxy` 只转发 `/api` 与 `/ws`，
   `/extension-assets/` 落到静态文件服务，素材不可达；开发模式只因 Vite 代理而可用。
2. 素材 HTTP 请求先进入 `gateway.request`。连接未登录时能否取得素材，取决于登录插件是否放行该路径
   （登录示例需显式放行），平台素材的可用性被其他插件的策略决定。2026-11-06 修订：保留该行为，见 D-03。
3. 素材中的 HTML 若被直接打开，会以前端同源身份运行；仅依赖嵌入处 iframe 的 `sandbox` 不够。
   此外 `app_web` 的 HTTP 代理先读取完整响应再写回，大媒体首播需等待整份文件。

### 2.2 目标与关键场景

| 场景 | 预期行为 |
| --- | --- |
| 包只声明 `ui.items`（应用级槽位），不声明 `client` | 可接收所声明槽位的 `ui.ready` 与交互，可调用 `ops.ui.asset_url` 并读取素材；不能调用 `ops.client.*` |
| 包声明请求绑定或客户端操作 | 保持现有校验要求，`client.declarations` 内容不变 |
| 打包版读取素材 | `app_web` 转发到 Gateway，支持 `Range`，响应边读边写 |
| 未登录连接读取素材 | 与其他 HTTP 请求一样先经 `gateway.request`，由插件请求策略决定是否放行；继续后由平台按活动包与激活代次判定 |
| 直接打开素材中的 HTML | 浏览器按沙箱处理，文档为不透明来源 |

### 2.3 本次范围与非目标

范围：浏览器桥接的参与条件、素材路由与响应头、`app_web` 的素材转发与流式代理、相关规约与测试。

非目标：新增槽位、媒体容器、外观、存储及启动门控（见计划 GW-08～GW-12）；`application.sidebar.footer`
容量三处不一致（记录于计划第 6 节）；agent-core 锁定版本对齐。

## 3. 设计约束、假设与待决问题

- Gateway 侧代码只依赖 `openjiuwen.core.events` 与本模块，不 import `jiuwenswarm.extensions_v2`。
- 仅客户端能力包的下行帧携带 `activation_generation`（S_06），前端据 `client.declarations` 校验；普通应用级包的帧保持不带该字段。
- 素材响应保持 `Cache-Control: no-store`，以维持“包失活后不可读取”的不变量。
- 无待决问题。

## 4. 总体方案与设计取舍

### 4.1 方案概览

把桥接中的参与条件拆成两层：**应用级 UI 包**（在 Gateway 应用级槽位声明贡献的包）取得激活代次、交互分派、
迟到调用拦截、`ops.ui.asset_url` 与素材；**客户端能力包**（声明 `client`）在此基础上再取得请求绑定、
`client.declarations` 与 `ops.client.*`。素材路由保留 `gateway.request` 请求策略，响应附带沙箱策略。
`app_web` 将 `/extension-assets/` 与 `/api` 一样转发到 Gateway HTTP，并以分块方式写回响应。

### 4.2 关键决策

#### D-01 以槽位贡献而非请求绑定判定应用级 UI 包

声明了至少一项 `web.slot.contribution` 且槽位属于 Gateway 应用级槽位的活动包即为应用级 UI 包。
依据：界面与凭据是两类能力；槽位贡献已是 manifest 中描述界面的正式字段。只改变参与条件，不新增 manifest 字段。

#### D-02 激活代次对所有应用级 UI 包登记，但只有客户端能力包的帧携带代次

代次用于素材地址与迟到调用拦截，这两项对所有应用级 UI 包都需要；前端按 `client.declarations` 校验帧中的代次，
普通包没有声明，故帧中不携带，保持 S_06 的约束。

#### D-03 素材路由保留请求策略

2026-11-06 修订：合入 `releases/0.2.9.beta3` 时采用目标分支已有契约，素材请求与其他 HTTP 请求一样先经
`gateway.request`，插件可以拒绝或接管素材路径；策略继续后再由平台校验包、激活代次与路径。目标分支的登录示例
显式放行 `/extension-assets/`，回归用例 `test_http_keeps_plugin_denial_and_consumption` 覆盖拒绝与接管。

原方案（2026-11-05，已被替代）：素材路由不进入 `gateway.request`，避免其他插件的请求策略决定平台素材是否可用。
该方案与目标分支“插件拒绝与接管保持有效”的契约冲突，未保留；由此带来的影响记录在第 9.2 节。

#### D-04 素材响应附带沙箱内容安全策略

响应头加入 `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads`，沙箱指令与前端 Iframe 一致且不含 `allow-same-origin`。直接打开的 HTML 以不透明来源运行，不能读取前端存储或以前端身份发起请求；
图片、媒体与脚本作为子资源加载时不受影响。

#### D-05 `app_web` 流式转发

`_proxy_http` 改为按块读取上游响应并写回，不再整体缓冲；保留状态码、响应头与 `Range` 请求头。
`/api` 也一并受益，语义不变。

### 4.3 替代方案与排除理由

| 方案 | 排除理由 |
| --- | --- |
| R-01 为不需凭据的包也要求声明空的 `client.requestBindings` | 把无关能力绑定到授权概念上，且空绑定仍会进入客户端声明流程 |
| R-02 由各登录插件放行 `/extension-assets/` | 平台能力依赖第三方插件的实现，任何一个插件遗漏都会导致所有包素材失效 |
| R-03 素材改为长期缓存 | 失活后浏览器缓存仍可读取，违背现有不变量；本地回环读取成本低，暂不调整 |
| R-04 只为素材路径单独实现流式转发 | 与 `/api` 使用同一转发函数，分别维护两套转发逻辑没有收益 |

## 5. 详细设计

### 5.1 模块责任与关键不变量

| 模块 | 修改 |
| --- | --- |
| `webui/bridge.py` | 新增应用级 UI 包判定；`published` 为其登记代次；`dispatch` 以其为分派范围；`asset_url` 与 `asset_file` 以其为前提；`ops.client.*` 仍要求客户端能力包；`frame_generation` 只为客户端能力包返回代次 |
| `runtime.py` | 为应用级 UI 包的 Handler 建立调用者作用域，使迟到的渲染与素材调用按激活代次拦截 |
| `webui/http.py` | 素材响应附带沙箱策略；素材路径与其他 HTTP 请求一样先经 `gateway.request` |
| `channels/web/app_web.py` | `/extension-assets/` 转发到 `api_target`；转发改为流式 |

不变量（补充到 S_06）：

1. 应用级 UI 包只能读取自身活动激活代次下 `assets/` 内的普通文件。
2. 客户端能力（请求绑定、`ops.client.*`、`client.declarations`）仍只属于声明 `client` 的包。
3. 素材请求与其他 HTTP 请求一样先经 `gateway.request`；策略继续后由平台判定包、激活代次与路径。

### 5.2 正常执行流程

1. 包发布时，运行时调用 `published` 登记激活代次（应用级 UI 包均登记）。
2. 浏览器挂载 entry/footer 时发送 `ui.ready`，`dispatch` 向声明了该槽位的应用级 UI 包分派。
3. 包在 Handler 中调用 `ops.ui.asset_url("bg.png")`，得到 `/extension-assets/<包>/<代次>/bg.png`。
4. 打包版请求经 `app_web` 转发到 Gateway；Gateway 校验代次与路径后以 `FileResponse` 返回，支持 `Range`。

### 5.3 异常处理

| 情况 | 结果 |
| --- | --- |
| 代次失效或包未激活 | 404 `NOT_FOUND` |
| 路径越出 `assets/`、指向目录或符号链接 | 404 `NOT_FOUND` |
| 非客户端能力包调用 `ops.client.*` | 抛出错误，与现有失活调用一致 |
| 上游中途断开 | 已发送响应头后直接结束连接；未发送前返回 502 |

## 6. 影响面与实施设计

### 6.1 修改点与实施顺序

1. 桥接参与条件与运行时调用者作用域，补充桥接测试。
2. 素材路由与响应头，补充素材测试。
3. `app_web` 转发与流式输出，补充代理测试。

### 6.2 文档同步

- [S_06](../specs/S_06_local-browser-bridge.md)：参与条件、素材路由与不变量。
- [S_01](../specs/S_01_gateway-extension-runtime.md)：应用级 UI 包参与 application 交互分派；HTTP 请求进入 `gateway.request` 的范围不变。
- 用户文档：本次不改变用户可见行为，无需更新 `docs/`。

## 7. 兼容与迁移

现有登录示例同时声明 `client` 与应用级槽位，行为不变；其放行 `/extension-assets/` 的分支继续生效。
没有声明槽位的 Gateway 包不受影响。

## 8. 验证设计、执行记录与结论

### 8.1 验证设计与通过条件

| 编号 | 场景 | 通过条件 |
| --- | --- | --- |
| F_08-V-01 | 无 `client` 的应用级包收到 `ui.ready` 与 `ui.click` | 分派到该包的 Handler |
| F_08-V-02 | 无 `client` 的包调用 `ops.ui.asset_url` | 返回带当前代次的地址；对应素材可读取 |
| F_08-V-03 | 无 `client` 的包调用 `ops.client.local_storage_set` | 被拒绝 |
| F_08-V-04 | 无 `client` 的包的下行帧 | 不携带 `activation_generation` |
| F_08-V-05 | 重载后旧代次素材与迟到渲染 | 404 与 `rejected` |
| F_08-V-06 | 请求策略放行素材、拒绝其他路径 | 素材可读取，其他路径被拒绝，两次请求均经过策略 |
| F_08-V-07 | 素材响应头 | 含 `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads`、`X-Content-Type-Options: nosniff`、`Cache-Control: no-store` |
| F_08-V-08 | `app_web` 转发素材 | 转发到 `api_target`，透传 `Range`，返回 206 与对应字节 |
| F_08-V-09 | `app_web` 大响应 | 分块写回，内容与上游一致 |
| F_08-V-10 | 既有 Gateway 扩展用例 | `tests/unit_tests/gateway/extensions_v2/` 全部通过 |

### 8.2 实际执行与证据

环境：macOS（arm64），Python 3.11.17，独立虚拟环境；仓库基线 `f38242f`；agent-core `31fee3a`
（锁定版本 `86990bd` 缺少当前导入的模块，见计划第 6 节）。命令在仓库根目录执行，未生成覆盖率报告：

```text
python -m pytest -q -o addopts= --asyncio-mode=auto --strict-markers tests/unit_tests/gateway/extensions_v2
python -m pytest -q -o addopts= --asyncio-mode=auto --strict-markers tests/unit_tests/channels/web \
    tests/unit_tests/test_app_web_raw_file.py tests/unit_tests/test_app_web_skill_upload.py \
    tests/unit_tests/channel/test_trajectory_http.py
```

| 编号 | 用例 | 结果 |
| --- | --- | --- |
| F_08-V-01、V-04 | `test_package_without_client_receives_ready_and_click` | 通过 |
| F_08-V-02、V-03、V-07 | `test_package_without_client_gets_assets_but_not_browser_operations`（含 `Range` 206 与越界 404） | 通过 |
| F_08-V-05 | `test_reload_retires_old_assets_and_late_renders` | 通过 |
| F_08-V-06 | `test_request_policy_decides_whether_assets_are_served` | 通过 |
| 范围外包 | `test_packages_without_application_slots_stay_outside_the_bridge` | 通过 |
| F_08-V-08 | `test_extension_assets_are_forwarded_to_the_gateway`、`test_range_requests_reach_the_gateway_and_return_partial_content`、`test_head_returns_headers_without_a_body` | 通过 |
| F_08-V-09 | `test_proxied_bodies_stream_before_the_upstream_finishes` | 通过 |
| F_08-V-10 | `tests/unit_tests/gateway/extensions_v2/` 全部用例 | 325 通过 |
| 回归 | Web 渠道与代理相关用例 | 155 通过，1 跳过 |

新增的 9 个用例在恢复修改前的源码后全部失败，确认用例覆盖本次行为。`test_trajectory_http.py` 中代理测试的
响应替身改为与 `http.client.HTTPResponse` 一致：`read(amt)` 读完后返回空字节，断言不变。

### 8.3 验证结论

单元范围内，第 2.2 节场景全部成立。未覆盖：打包版桌面应用中经 `app_web` 读取真实 Gateway 素材、
浏览器中直接打开素材 HTML 的沙箱行为；两者在 GW-09 媒体容器联调时验证。

## 9. 风险与已知遗留

### 9.2 已知遗留

- `application.sidebar.footer` 容量在三处不一致，未在本特性处理。
- 素材路径受其他插件的请求策略约束：拒绝匿名 HTTP 的鉴权插件须放行 `/extension-assets/`，否则登录前的应用级界面取不到素材（D-03）。
- agent-core 锁定版本缺少当前导入的模块，开发验证使用 `31fee3a`，见计划第 6 节。
- 真实桌面打包版中的素材读取尚待 GW-09 之后联调验证。
