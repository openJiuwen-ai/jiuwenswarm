# 应用级媒体容器规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_07 / media-surface` |
| 所属模块 | Gateway `webui/`（校验）；Web 前端 `features/webui/`（宿主与渲染） |
| 最近一次修订日期 | 2026-11-07 |
| 契约状态 | 已实现（背景槽位）；开场槽位的策略在 EXTV2-GW-11 补充 |
| 关联文档 | [F_10](../features/F_10_media-surface-and-background.md)、[S_17 组件 Props](../../../../extensions_v2/docs/specs/S_17_webui-component-props.md)（`MediaSurface` 字段的唯一维护位置）、[S_02 渲染](S_02_ui-render.md)、[S_06 浏览器桥接](S_06_local-browser-bridge.md) |

本文维护媒体容器在宿主中的行为：可用槽位、宿主策略、与沙箱内容的消息协议及不变量。组件字段见 S_17。

## 1. 槽位

| 槽位 | 范围 | 容量 | 根节点 | 宿主位置 |
| --- | --- | --- | --- | --- |
| `application.background` | `application` | `single` | 只接受 `MediaSurface` | 应用根部的全窗口底层，位于全部界面之下 |
| `application.splash` | `application` | `single` | 只接受 `MediaSurface` | 启动门控的开场层，位于全部界面之上；时机与时限见 [S_09](S_09_startup-splash.md) |

- 槽位由 Gateway 应用级 UI 包贡献，宿主挂载后按连接发送 `ui.ready`（S_06）。
- 同一连接只显示版本最新的一个实例。
- 有实例显示时，宿主在文档根元素上标记 `data-webui-background="active"`，实例清除后移除；
  该标记供宿主外观使用，插件不能设置。

## 2. 宿主策略（背景）

| 项 | 行为 |
| --- | --- |
| 输入与焦点 | 容器不接收点击，不可获得焦点，对辅助技术隐藏 |
| 声音 | 一律静音，忽略 `muted`；HTML 页面不获得 autoplay 委派，见下行 |
| 图片 | 加载失败时显示 `poster`，没有 `poster` 时不显示 |
| 视频 | 自动播放、内联、按 `loop` 循环；窗口隐藏时暂停，显示时恢复；失败时显示 `poster` |
| HTML | 在 `sandbox="allow-scripts"`、`allow=""`（不委派 autoplay）的 iframe 中加载，页面无法开始有声播放或运行 WebAudio，静音视频照常播放；切换或清除时立即移除旧 iframe；就绪前 iframe 保持渲染但完全透明，其上显示 `poster`；15 秒内未就绪或报告错误时卸载 iframe 并显示 `poster` |
| 减少动态 | 用户开启减少动态时：有 `poster` 的视频与 HTML 只显示 `poster`；没有 `poster` 的视频不自动播放，HTML 照常加载并在上下文中收到 `reducedMotion: true` |
| 视觉参数 | `fit`、`position`、`dim`、`blur`、`opacity` 由宿主映射为样式 |

开场槽位中：HTML 页面委派 `allow="autoplay"`；遵循插件的 `muted`（有声自动播放被拒绝时静音重试），视频不循环，不做减少动态替换；
媒体就绪、心跳、完成与错误交给启动门控计时（[S_09](S_09_startup-splash.md) 第 5 节），容器不接收输入，只有跳过提示可按住。

## 3. 消息协议（HTML 内容）

### 3.1 令牌

宿主每次创建 iframe 时生成随机令牌，以 URL 片段 `webui-surface-token=<令牌>` 附加到 `src`
（`src` 已有片段时以 `&` 连接）。内容页从 `location.hash` 读取令牌，在发出的每条消息中携带。

### 3.2 内容 → 宿主

```json
{ "type": "webui.surface", "version": 1, "token": "<令牌>", "event": "ready" }
```

| `event` | 含义 | 背景槽位中的处理 |
| --- | --- | --- |
| `ready` | 内容已可显示 | 移除 `poster` 遮挡；再次发送 `start` |
| `error` | 内容无法显示，可带 `detail: {code, message}` | 卸载 iframe，显示 `poster` |
| `heartbeat` | 播放仍在进行 | 忽略；开场槽位中重置心跳时限 |
| `complete` | 内容播放完毕 | 忽略；开场槽位中按 `complete` 结束 |

`ready` 表示内容已可显示。宿主不保证就绪前的页面已获得动画帧（例如应用冷启动时），
内容页应在自身资源就绪时发送 `ready`，不以首个动画帧为前提；超过就绪时限仍会回退到 `poster`。

宿主只接受同时满足以下条件的消息，其余一律静默丢弃：`event.source` 是该 iframe 当前的 `contentWindow`；
`type`、`version`、`token` 与当前令牌一致；`event` 属于上表。不依据 `event.origin` 判断（沙箱文档的来源为 `null`）。

### 3.3 宿主 → 内容

```json
{ "type": "webui.surface", "version": 1, "event": "start",
  "context": { "role": "background", "visible": true, "reducedMotion": false, "locale": "zh-CN" } }
```

| `event` | 时机 |
| --- | --- |
| `start` | iframe 加载完成时；收到 `ready` 时再发一次 |
| `context` | 窗口可见性或减少动态设置变化时 |

`targetOrigin` 为 `*`：沙箱文档没有可指定的来源；消息只发往该 iframe 的 `contentWindow`，不含敏感数据。

## 4. 不变量

1. `MediaSurface` 只能作为树根，只能用于第 1 节列出的槽位；这两个槽位不接受其他根组件。
2. `src` 与 `poster` 只能是以 `/extension-assets/` 开头的站内路径，因此内容始终来自活动包素材或数据素材，
   并带有 S_06 规定的沙箱响应头。
3. HTML 内容固定在 `sandbox="allow-scripts"` 中运行，不含 `allow-same-origin`，插件不能覆盖。
4. 背景容器不接收输入、不获得焦点、始终静音。视频由宿主强制静音；HTML 页面不获得 autoplay 委派，在浏览器中无法开始有声播放。桌面应用（Electron）当前的 autoplay 策略不要求用户手势，也不按 iframe 权限策略拦截，背景 HTML 若自行播放声音，宿主在桌面端不能阻止——背景页面不得发声，这一点由插件遵守（见 F_10 第 9.2 节）。
5. 令牌在每次创建 iframe 时重新生成；旧 iframe 的消息不会影响新实例。
6. 背景宿主（`application.background`）的实例在生命周期内跨帧保持挂载：新帧的 `src` 与 `kind` 不变时，正在运行的页面或视频
   不重新加载，`dim`、`blur`、`opacity`、`fit`、`position` 只改变样式，文档根上的 `data-webui-background` 也不随之切换；`src` 或 `kind`
   变化时按新内容创建。其他槽位仍按 WebUI 的整树重建契约处理每个合法帧。

## 5. 验证入口

| 契约 | 用例 |
| --- | --- |
| 字段、根节点与槽位 | `test_gateway_ui_render.py`、`webuiGatewayCases.json`；前端 `test:webui-protocol`、`test:webui-components` |
| 宿主行为与消息协议 | 前端 `test:webui-components`（`MediaSurface` 与背景宿主用例；跨帧保持挂载见 `background updates with the same page keep it running and only restyle it`） |
| 包参与与 `ui.ready` 分派 | `test_gateway_application_assets.py` |
