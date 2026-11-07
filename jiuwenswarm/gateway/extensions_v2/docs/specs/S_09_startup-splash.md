# 启动门控与开场队列规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_09 / startup-splash` |
| 所属模块 | Gateway `webui/`（声明、提供者、平台帧与 `ui.closed`）；`channels/web/app_web.py`（候选提示）；Electron 主进程与预加载（启动标识）；Web 前端 `features/webui/`（门控与开场队列） |
| 最近一次修订日期 | 2026-11-07 |
| 契约状态 | 已实现；开场顺序、停用与 `disabled` / `removed` 原因见 [S_10](S_10_application-settings.md)（EXTV2-GW-12）；开场预览（第 3.3 节）已实现（EXTV2-GW-13，[F_14](../features/F_14_splash-preview.md)） |
| 关联文档 | [F_12](../features/F_12_startup-gate-and-splash.md)、[S_07 媒体容器](S_07_media-surface.md)、[S_06 浏览器桥接](S_06_local-browser-bridge.md)、[S_08 受控外观](S_08_appearance.md) |

## 1. 声明

包在 manifest 的 `ui.items` 中声明开场：

```json
{
  "ui": {
    "apiVersion": 1,
    "items": [
      {
        "type": "web.slot.contribution",
        "id": "theme.splash",
        "slot": "application.splash",
        "splash": { "skippable": true, "maxDurationMs": 9500 }
      }
    ]
  }
}
```

`splash` 可省略；`skippable` 默认 `false`。

| 字段 | 约束 |
| --- | --- |
| `splash` | 只能出现在 `application.splash` 项上；只接受下列字段 |
| `skippable` | boolean，默认 `false` |
| `maxDurationMs` | 不小于 1000 的整数；省略按 60000；超过 60000 时发布时记录警告并按 60000 执行 |

字段不合法时包发布失败。

## 2. 槽位

`application.splash`：应用级、`single`，根节点只能是 `MediaSurface`（S_07）。开场容器位于全部界面与登录入口之上。

## 3. 平台帧

以下 WebSocket 帧由宿主直接处理，不进入 `gateway.request`，不受请求策略插件影响，登录前同样可用。
它们不携带用户输入；是否显示内容由包自己的 Handler 决定。

### 3.1 `webui.splash.providers`

参数为空，返回：

```json
{ "providers": [ { "package_id": "pkg.theme", "skippable": true, "max_duration_ms": 9500 } ], "customized": false }
```

列出声明 `application.splash` 且未被用户停用的活动包，按生效顺序；`max_duration_ms` 已按第 1 节取值；
`customized` 表示用户是否保存过顺序。顺序与停用的规则见 [S_10 第 4 节](S_10_application-settings.md)。

### 3.2 显示型槽位的 `ui.ready` 与 `ui.closed`

`webui.dispatch` 的目标为应用级的 `application.background` 或 `application.splash`、事件为 `ui.ready`
（两个槽位）或 `ui.closed`（仅开场）时按平台帧处理；其余 `webui.dispatch` 不变。包 Handler 的执行时限为 5 秒。

开场的 `ui.ready` 必须指定 `package_id`，载荷：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `launch_id` | string（1～128 字符） | 本次应用启动的标识 |
| `first_visit` | boolean | 本设备此前是否从未完成过开场 |
| `reduced_motion` | boolean | 用户是否要求减少动态 |
| `preview` | boolean | 是否为预览（第 3.3 节）；启动时为 false。预览时 `first_visit` 固定为 false |

回执：`{"accepted": true, "status": "playing"}` 表示 Handler 返回后该包在此连接上有开场实例；
`"skipped"` 表示没有；`{"accepted": false, "status": "unhandled" | "timeout"}` 表示无人处理或超时。

`ui.closed` 必须指定 `package_id`，载荷 `{"reason": <原因>, "revision": <开场实例版本>}`，原因为
`complete`、`skip`、`timeout`、`error`、`disabled`、`removed` 之一。版本与当前开场实例一致时，宿主向该包触发
`ui.closed`（`UiActionInput`，载荷 `{"reason": <原因>}`），随后丢弃该实例并回执 `{"accepted": true, "status": "closed"}`；
否则回执 `{"accepted": false, "status": "inactive", "code": "TARGET_INACTIVE"}`。

### 3.3 开场预览

包在处理浏览器交互（例如设置页的点击）时调用 `ops.splash.preview()`，请求在该连接上立即播放自己的开场一次。

| 项 | 规则 |
| --- | --- |
| 调用方 | 声明了 `application.splash` 的应用级 UI 包的当前激活；调用上下文中须有浏览器连接 |
| 停用 | 用户在设置中停用了该包的开场（S_10 第 4 节）时同样可以预览 |
| 效果 | 向该连接发送下行帧 `webui.splash.play`：`{"package_id", "skippable", "max_duration_ms"}`，后两项取第 1 节的声明 |
| 返回 | `{"status": "requested"}`；帧未送达时 `{"status": "delivery_failed"}`；包未声明开场、激活已失效或没有连接时抛出 `ValueError` |

前端收到 `webui.splash.play` 后，只在门控已开放且当前没有开场时处理，否则忽略：
按第 5 节的“准备”到“结束”为这一个包走一遍，`ui.ready` 的 `preview` 为 true、`first_visit` 为 false；
播放期间工作界面不接收输入。结束后回到门控开放状态，不改写本次启动的完成记录，也不改写本设备是否完成过开场的记录。

## 4. 启动标识与候选提示

| 项 | 来源 |
| --- | --- |
| 启动标识 | Electron 主进程每次启动生成，经 `window.jiuwenDesktop.launchId` 提供；浏览器中取会话存储中的随机值 |
| 已完成的启动 | 浏览器本地存储记录最近一次完成开场的启动标识；同一标识不再进入开场（刷新、重连、同一次启动中的新窗口） |
| 候选提示 | `app_web` 的 `GET /extension-splash/hint` 返回 `{"candidates": <bool>}`：已安装包的 manifest 中出现 `application.splash` 即为 true。只表示可能有候选，不代表包已激活；取不到或格式不符（如开发模式由 Vite 返回页面）按“未知”处理 |
| 首帧中性层 | 本地存储记录上次判定（`workswarm.splash.expected`）；上次判定没有开场或本启动已完成时，`index.html` 的内联脚本在首帧前移除中性层 |

## 5. 门控与开场队列（前端）

| 阶段 | 行为 |
| --- | --- |
| 首帧 | `index.html` 显示与 Electron 加载页一致的中性层；脚本启动后由门控接管 |
| 判定 | 本启动已完成，或提示为否：立即开放。提示为是：等待连接至多 30 秒后查询提供者。无法取得提示：等待连接并查询至多 3 秒 |
| 顺序 | `customized` 为 false 时，最近一次的外观提供者（S_08）排在首位，其余按提供者列表顺序；为 true 时按列表顺序 |
| 准备 | 依次向每个提供者发送定向 `ui.ready`；5 秒内无回执或回执不是 `playing` 时转到下一个 |
| 播放 | 只显示当前提供者的开场实例；媒体就绪时限 10 秒 |
| 心跳 | 页面就绪后 5 秒内既无 `heartbeat` 也无 `complete`，或视频 5 秒内无播放进度，按 `timeout` 结束 |
| 上限 | 自就绪起超过提供者的 `max_duration_ms` 按 `timeout` 结束；图片显示 3 秒（上限更短时取上限）后按 `complete` 结束 |
| 完成 | 页面 `complete` 或视频播放结束：`complete`；页面 `error` 或媒体失败：`error`；提供者清除实例或包被释放：结束且不发送 `ui.closed` |
| 跳过 | `skippable` 为 true 时显示“长按空格跳过”与进度环；长按空格或在提示上按住 0.8 秒：`skip`。只跳过当前开场 |
| 结束 | 先在本地关闭容器（停止声音），再发送 `ui.closed`，不等待回执，转到下一个；`disabled`、`removed` 由 Gateway 在停用与包释放时发出，前端只收到清除帧（[S_10 第 5 节](S_10_application-settings.md)） |
| 开放 | 记录本启动已完成，移除中性层与界面的 `inert`，登录入口此后才可显示 |
| 预览 | 门控开放后收到 `webui.splash.play`：重新给界面加上 `inert` 并显示开场层，只播放该包一次；结束后恢复，不改写完成记录（第 3.3 节） |

开场容器中，`MediaSurface` 遵循插件的 `muted`，视频不循环，不做减少动态替换（提供者已在 `ui.ready` 中收到该设置）。

## 6. 不变量

1. 门控开放前，主界面不显示、不接收输入，登录入口不弹出。
2. 门控总会开放：每个等待都有时限，单个开场不超过 60 秒，队列不设总上限。
3. 只显示当前提供者的开场实例；包身份来自 Gateway 的调用上下文。
4. 同一开场实例至多通知一次 `ui.closed`；迟到或过期的通知被拒绝。
5. 同一启动标识至多进入一次开场队列。
6. 平台帧不携带用户输入，不改变其他 `webui.dispatch` 与请求的策略语义。
7. 预览只由包在自己连接上的交互中发起，只在门控开放且没有开场播放时进行；同一时刻至多一个开场。
8. 预览不改变启动标识的完成记录与首次访问判定，不使门控重新进入启动队列。

## 7. 验证入口

| 契约 | 用例 |
| --- | --- |
| 声明、提供者、平台帧与 `ui.closed` | `test_gateway_splash.py` |
| 候选提示 | `tests/unit_tests/channels/web/test_app_web_splash_hint.py` |
| 门控、队列、跳过与时限 | 前端 `test:webui-components`（开场用例） |
| 开场预览 | `test_gateway_splash.py`（预览用例）、前端 `test:webui-components`（预览用例） |
