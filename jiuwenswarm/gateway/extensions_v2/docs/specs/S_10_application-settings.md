# 应用级设置页与文件交接规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_10 / application-settings` |
| 所属模块 | Gateway `webui/`（设置槽位、宿主方法、开场偏好、导入与导出）、`management/`（包释放前关闭开场）；`channels/web/app_web.py`（导入与导出的转发）；Web 前端 `features/settings/`（“扩展”模块与插件设置项）与 `features/webui/`（`FileImport`、导入导出服务、开场顺序） |
| 最近一次修订日期 | 2026-11-07 |
| 契约状态 | 已实现（2026-11-05 修订：导入与导出对远程浏览器同样开放；2026-11-06 修订：插件设置页在设置导航中单独成项；2026-11-07 修订：标题取 `display.name` 并按语言显示） |
| 关联文档 | [F_13](../features/F_13_application-settings-page.md)、[S_09 启动门控与开场](S_09_startup-splash.md)、[S_06 浏览器桥接](S_06_local-browser-bridge.md)、[S_01 运行时](S_01_gateway-extension-runtime.md)、[S_17 组件属性](../../../../extensions_v2/docs/specs/S_17_webui-component-props.md) §4.25 |

## 1. 范围与术语

本规约定义：不依赖会话的应用级设置槽位、宿主设置方法、开场顺序与停用、`ui.closed` 的 `disabled` / `removed`
原因、经宿主中转的文件导入与导出。

| 术语 | 含义 |
| --- | --- |
| 设置页包 | 声明 `application.settings.page` 贡献的活动 Gateway 包 |
| 开场提供者 | 声明 `application.splash` 的活动包（S_09 第 1 节） |
| 宿主方法 | 本规约第 3 节列出、由 Gateway 自身应答的 WebSocket 方法 |
| 导入凭据 | 一次导入的随机标识（32 位十六进制），绑定连接、包、激活代次、实例与节点 |
| 下载凭据 | 一次导出的随机标识（32 位十六进制），绑定连接、包、激活代次与文件 |

## 2. 设置槽位

`application.settings.page`：应用级、`list`，接受普通组件树（`MediaSurface` 等专用根组件不可用）。

```json
{
  "ui": {
    "apiVersion": 1,
    "items": [{ "type": "web.slot.contribution", "id": "theme.settings", "slot": "application.settings.page" }]
  }
}
```

- 导航标题取 manifest.json 的 `display.name`，按 `{zh, en}` 下发，前端按界面语言显示；单个字符串按双语同值处理，
  只有一种语言时另一种取同值，缺省或为空时两种语言都取包标识。`webui.settings.pages` 与 `webui.splash.settings`
  中的 `title` 均为此对象。
- 前端设置导航中，每个设置页包单独一项，排在宿主“扩展”模块之后，顺序同 `webui.settings.pages`；名称为上条的标题，图标统一。
  选中该项时只挂载该包，实例键为 `main`。“扩展”模块只含宿主功能（第 4 节的开场顺序与停用）。
- 宿主访问策略隐藏“扩展”模块时，不显示插件项，也不发出 `webui.settings.pages`。
- 挂载时发送指定 `package_id` 的 `ui.ready`（载荷为空对象）。该帧与之后的交互都经 `gateway.request`，受请求策略约束，不是平台帧。
- 设置页只在门控开放之后可达（S_09 第 5 节）。

## 3. 宿主方法

以下方法经 `gateway.request` 后由 BUILTIN 宿主处理器应答。USER 请求策略先行判定，要求登录的插件拒绝未认证的帧时，
宿主方法同样不可用（鉴权职责见 [S_01 第 6 节](S_01_gateway-extension-runtime.md)）。参数不合法时返回 `deny`，
`error.code` 见第 7 节。没有任何 USER Handler 时这些方法同样由 Gateway 应答，不转发给 AgentServer。

### 3.1 `webui.settings.pages`

参数为空对象。返回 `{"pages": [{"package_id": str, "title": str}]}`，按发布顺序。

### 3.2 `webui.splash.settings`

参数为空对象。返回全部开场提供者（含已停用的），按生效顺序（第 4 节）：

```json
{ "providers": [ { "package_id": "pkg.theme", "title": "WorkSwarm Theme", "skippable": true,
                   "max_duration_ms": 9500, "enabled": true } ],
  "customized": false }
```

### 3.3 `webui.splash.settings.update`

参数至少含下列一项，不接受其他字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `order` | `list[str]` 或 `null` | 用户顺序；元素须是互不相同的活动提供者，未列出的活动提供者按安装顺序排在后面；`null` 恢复默认顺序 |
| `disabled` | `list[str]` | 停用的活动提供者，互不相同；替换当前活动提供者的停用集合，非活动包的停用记录保留 |

成功时保存并返回与 3.2 相同的结构。新停用的提供者若正在某连接上播放开场，按第 5 节以 `disabled` 关闭。

### 3.4 `webui.upload.begin`

为 `FileImport` 节点开始一次导入，见第 6.2 节。

### 3.5 平台帧 `webui.splash.providers` 的变化

S_09 第 3.1 节的返回改为：只含启用的提供者，按生效顺序，并增加 `customized`：

```json
{ "providers": [ { "package_id": "pkg.theme", "skippable": true, "max_duration_ms": 9500 } ], "customized": true }
```

## 4. 开场顺序与停用

| 项 | 规则 |
| --- | --- |
| 安装顺序 | 提供者首次发布时追加到记录末尾；记录建立前已安装的包按首次启动时的发布顺序 |
| 生效顺序 | 有用户顺序时：用户顺序中的活动提供者，其后是其余活动提供者（按安装顺序）；没有用户顺序时：安装顺序 |
| `customized` | 是否存在用户顺序 |
| 前端合并 | `customized` 为 false 时，最近一次的外观提供者（S_08）排在首位，其余按返回顺序；为 true 时按返回顺序 |
| 停用 | 停用的提供者不出现在 `webui.splash.providers` 中，不收到开场 `ui.ready`；包的背景与外观不受影响 |
| 保存位置 | `<data_dir>/host/splash.json`：`{"version": 1, "installed": [...], "order": [...] 或 null, "disabled": [...]}`，原子替换写入 |
| 生命周期 | 重载、升级与重启保留；卸载时从三项记录中删除该包；文件无法读取时按空记录处理并记录警告 |

## 5. `ui.closed` 的 `disabled` 与 `removed`

| 原因 | 时机 |
| --- | --- |
| `disabled` | `webui.splash.settings.update` 新停用的提供者在某连接上有开场实例 |
| `removed` | 管理门面卸载、重载或覆盖安装一个活动包之前，该包在某连接上有开场实例 |

宿主对每个这样的实例：先从注册表移除并发送清除帧（原因同上，预留版本号），再向该包触发 `ui.closed`
（`UiActionInput`，载荷 `{"reason": <原因>}`），Handler 时限 5 秒，超时或异常只记录日志。
前端收到当前开场实例的清除帧时结束这一段，不再发送 `ui.closed`（S_09 第 5 节），随后进入下一个提供者。

## 6. 文件导入

### 6.1 组件

`FileImport`（Gateway）：用户点击后由宿主打开系统文件选择；属性与事件见 S_17 §4.25。
插件只收到经宿主写入包数据目录后的相对路径，不接触本机路径。

### 6.2 开始：`webui.upload.begin`

```json
{ "target": { "scope": "application", "slot_id": "application.settings.page", "package_id": "pkg.theme",
              "instance_key": "main" },
  "node_key": "0.2", "revision": 3,
  "files": [ { "path": "aurora.zip", "size": 1048576 } ] }
```

| 检查 | 规则 |
| --- | --- |
| 目标 | 当前连接上该包的应用级实例，有内容，`revision` 与当前版本一致 |
| 节点 | `node_key` 指向 `FileImport`，未禁用，声明了 `ui.change` |
| 文件数 | 1～1000；未设 `multiple` 且未设 `directory` 时恰为 1 |
| 路径 | 以 `/` 分隔的相对路径，不超过 512 字节、16 层；每段 1～255 字节，不为 `.`、`..`，不含 `\`、控制字符与 `<>:"\|?*`；忽略大小写后互不相同，且不互为前缀目录 |
| 目录模式 | `directory` 为 true 时每个路径至少两段且首段相同；否则只有一段 |
| 类型 | 设置了 `accept` 时，文件名须以其中之一结尾（忽略大小写） |
| 大小 | 每个文件为非负整数；总和不超过 `extensions.gateway.uploads.max_bytes` |

成功返回 `{"upload_id": <导入凭据>, "url": "/extension-uploads/<upload_id>/"}`。凭据自开始起 10 分钟有效；
每个连接最多同时持有 4 个未完成的凭据，超出时最早的一个作废。

### 6.3 传输：`PUT /extension-uploads/<upload_id>/<path>`

| 规则 | 内容 |
| --- | --- |
| 来源 | 本机与远程浏览器都可使用，访问权只来自导入凭据，不判断对端地址；`app_web` 按块转发请求体 |
| 路径 | `<path>` 为 6.2 中声明的路径，按段百分号编码；每个路径只接收一次 |
| 内容 | 请求体字节数须等于声明大小 |
| 落盘 | `<包数据目录>/.uploads/<upload_id>/<path>`，先写临时文件，完整后改名 |
| 应答 | `204` 成功；`404` 凭据或路径无效、已过期；`409` 已接收；`413` 超出声明大小；`400` 内容不足 |

该路由不进入 `gateway.request`，访问权来自凭据；凭据只签发给请求已被策略继续的连接。素材路由仍经请求策略，见 S_06。

### 6.4 完成：`ui.change`

全部文件接收后，前端以 `webui.dispatch` 发送该节点的 `ui.change`，`data` 为 `{"upload_id": <导入凭据>}`。
宿主核对凭据的连接、包、激活代次、实例与节点，并确认全部文件已接收；凭据随即作废。包的 Handler 收到的 `data` 为：

```json
{ "upload_id": "…", "path": ".uploads/<upload_id>", "total_bytes": 1048576,
  "files": [ { "path": "aurora.zip", "size": 1048576 } ] }
```

`path` 相对 `ops.data.root()`。Handler 返回、失败或超时后，宿主删除 `.uploads/<upload_id>`；
插件需要保留的内容应在 Handler 内移出该目录（同一数据目录内改名即时完成），耗时处理放到后台。

### 6.5 清理

凭据过期、连接关闭、包释放时删除对应的 `.uploads/<upload_id>`；Gateway 启动时删除各活动包的 `.uploads`。

## 7. 文件导出

### 7.1 `ops.ui.download(path, *, filename=None)`

| 项 | 规则 |
| --- | --- |
| 调用方 | 应用级 UI 包的当前激活，调用上下文中有浏览器连接 |
| `path` | 相对 `ops.data.root()`；须是包数据目录内的普通文件，路径上不得有符号链接 |
| `filename` | 缺省取文件名；去掉路径分隔符与控制字符后非空，不超过 255 字节 |
| 效果 | 生成下载凭据，向该连接发送 `webui.download`：`{"package_id", "url": "/extension-downloads/<token>", "filename", "size"}` |
| 返回 | `{"status": "offered"}`；帧未送达时 `{"status": "delivery_failed"}` |

参数不合法、包失活或没有连接时抛出 `ValueError`。

### 7.2 `GET /extension-downloads/<token>`

本机与远程浏览器都可使用，访问权只来自下载凭据；凭据 5 分钟有效，第一次成功的 `GET` 后作废（`HEAD` 不作废）；连接关闭或包释放时作废。
响应头：`Content-Type: application/octet-stream`、`Content-Disposition: attachment; filename*=UTF-8''<编码后的文件名>`、
`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Content-Security-Policy: sandbox`。

### 7.3 前端

桌面版调用 `window.jiuwenDesktop.downloadFile(url, filename)`（系统保存对话框）；浏览器中以带 `download`
属性的链接触发下载。`url` 必须以 `/extension-downloads/` 开头。

## 8. 配置与错误码

| 配置键（`extensions.gateway`） | 类型 | 默认 | 约束 |
| --- | --- | --- | --- |
| `uploads.max_bytes` | int | `536870912`（512 MiB） | 上限 `4294967296`（4 GiB）；非法值回落默认 |

| 码 | 产生位置 | 含义 |
| --- | --- | --- |
| `BAD_REQUEST` | 宿主方法 | 参数缺失、多余或不合法 |
| `TARGET_INACTIVE` | `webui.upload.begin` | 实例、版本或节点不是当前状态 |
| `UPLOAD_TOO_LARGE` | `webui.upload.begin` | 总大小超过配置上限 |

## 9. 不变量

1. 设置页的 `ui.ready` 与交互受请求策略约束；宿主方法在 USER 请求策略继续后应答。
2. 停用的提供者不会收到开场 `ui.ready`；停用与包释放都不影响其他提供者的顺序记录。
3. 同一开场实例至多通知一次 `ui.closed`；宿主以 `disabled` / `removed` 关闭时，前端不再发送。
4. 插件只能读取宿主写入其数据目录的导入内容，导入不读取本机任意路径；导出只能提供本包数据目录内的文件。
5. 导入凭据只签发给发起导入的连接，完成时必须由同一连接提交；下载地址只发给调用所在的连接。两者都只能使用一次，
   过期、断线或包释放后失效，对应的临时目录被删除。
6. 导入与导出的访问权只来自一次性凭据，不区分本机与远程浏览器。凭据出现在请求地址中，远程使用时应经 HTTPS 访问。

## 10. 验证索引

| 契约 | 用例 |
| --- | --- |
| 设置槽位、页面列表、宿主方法与请求策略 | `test_gateway_application_settings.py` |
| 开场顺序、停用、保存与卸载清理；`disabled` / `removed` | `test_gateway_application_settings.py`、`test_gateway_splash.py` |
| 导入与导出 | `test_gateway_file_transfer.py`、`tests/unit_tests/channels/web/test_app_web_extension_transfers.py` |
| 组件与槽位两端一致 | `webuiGatewayCases.json`（`test_gateway_ui_render.py`、`test:webui-components`） |
| 前端设置模块、开场排序、导入导出 | `test:webui-components` |
