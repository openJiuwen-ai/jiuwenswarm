# 应用级媒体容器规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_07 / media-surface` |
| 所属模块 | Gateway `webui/`（校验）；Web 前端 `features/webui/`（宿主与渲染） |
| 最近一次修订日期 | 2026-11-05 |
| 契约状态 | 已实现（背景槽位）；开场槽位的策略在 EXTV2-GW-11 补充 |
| 关联文档 | [F_10](../features/F_10_media-surface-and-background.md)、[S_17 组件 Props](../../../../extensions_v2/docs/specs/S_17_webui-component-props.md)（`MediaSurface` 字段的唯一维护位置）、[S_02 渲染](S_02_ui-render.md)、[S_06 浏览器桥接](S_06_local-browser-bridge.md) |

本文维护媒体容器在宿主中的行为：可用槽位、宿主策略、与沙箱内容的消息协议及不变量。组件字段见 S_17。

## 1. 槽位

| 槽位 | 范围 | 容量 | 根节点 | 宿主位置 |
| --- | --- | --- | --- | --- |
| `application.background` | `application` | `single` | 只接受 `MediaSurface` | 应用根部的全窗口底层，位于全部界面之下 |

- 槽位由 Gateway 应用级 UI 包贡献，宿主挂载后按连接发送 `ui.ready`（S_06）。
- 同一连接只显示版本最新的一个实例。
- 有实例显示时，宿主在文档根元素上标记 `data-webui-background="active"`，实例清除后移除；
  该标记供宿主外观使用，插件不能设置。

## 2. 宿主策略（背景）

| 项 | 行为 |
| --- | --- |
| 输入与焦点 | 容器不接收点击，不可获得焦点，对辅助技术隐藏 |
| 声音 | 一律静音，忽略 `muted` |
| 图片 | 加载失败时显示 `poster`，没有 `poster` 时不显示 |
| 视频 | 自动播放、内联、按 `loop` 循环；窗口隐藏时暂停，显示时恢复；失败时显示 `poster` |
| HTML | 在 `sandbox="allow-scripts"`、`allow="autoplay"` 的 iframe 中加载；就绪前 iframe 保持渲染但完全透明，其上显示 `poster`；15 秒内未就绪或报告错误时卸载 iframe 并显示 `poster` |
| 减少动态 | 用户开启减少动态时：有 `poster` 的视频与 HTML 只显示 `poster`；没有 `poster` 的视频不自动播放，HTML 照常加载并在上下文中收到 `reducedMotion: true` |
| 视觉参数 | `fit`、`position`、`dim`、`blur`、`opacity` 由宿主映射为样式 |

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
| `heartbeat` | 播放仍在进行 | 忽略（开场槽位使用） |
| `complete` | 内容播放完毕 | 忽略（开场槽位使用） |

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

1. `MediaSurface` 只能作为树根，只能用于第 1 节列出的槽位；`application.background` 不接受其他根组件。
2. `src` 与 `poster` 只能是以 `/extension-assets/` 开头的站内路径，因此内容始终来自活动包素材或数据素材，
   并带有 S_06 规定的沙箱响应头。
3. HTML 内容固定在 `sandbox="allow-scripts"` 中运行，不含 `allow-same-origin`，插件不能覆盖。
4. 背景容器不接收输入、不获得焦点、始终静音。
5. 令牌在每次创建 iframe 时重新生成；旧 iframe 的消息不会影响新实例。

## 5. 验证入口

| 契约 | 用例 |
| --- | --- |
| 字段、根节点与槽位 | `test_gateway_ui_render.py`、`webuiGatewayCases.json`；前端 `test:webui-protocol`、`test:webui-components` |
| 宿主行为与消息协议 | 前端 `test:webui-components`（`MediaSurface` 与背景宿主用例） |
| 包参与与 `ui.ready` 分派 | `test_gateway_application_assets.py` |
