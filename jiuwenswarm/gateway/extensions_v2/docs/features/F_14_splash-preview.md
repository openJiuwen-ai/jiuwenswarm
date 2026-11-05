# 开场预览设计方案

## 1. 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | feature |
| 编号 / slug | `F_14 / splash-preview` |
| 建档日期 | 2026-11-05 |
| 计划条目 | [EXTV2-GW-13](../designs/extensions-v2-development-plan.md) |
| 整体设计 | [应用级界面能力设计概览](../designs/application-surfaces-overview.md) 第 3.1 节 |
| 方案成熟度 | 已确认，已实现 |
| 关联规约 | [S_09 启动门控与开场](../specs/S_09_startup-splash.md) 第 3.3 节、第 5 节 |
| 验证摘要 | 单元验证与 Chromium 157 无头模式验证通过（2026-11-05）；桌面打包版联调随壁纸插件进行 |

## 2. 背景、目标与范围

### 2.1 背景与问题

V1 壁纸引擎的管理页有“预览开场”：在主界面上立即播放所选主题的开场。V2 的开场只在启动门控中播放（[F_12](F_12_startup-gate-and-splash.md)），
门控开放后 `application.splash` 不再有消费者，插件无法让用户在设置页里看到开场效果。

### 2.2 目标与关键场景

| 场景 | 预期行为 |
| --- | --- |
| 在设置页点“预览开场” | 开场层覆盖界面，播放该插件当前选中的开场；结束、跳过或超时后回到设置页 |
| 该插件的开场被用户停用 | 仍可预览 |
| 启动开场正在播放，或已有预览在播 | 新的预览请求被忽略 |
| 预览之后刷新或下次启动 | 启动判定与没有预览时相同 |

### 2.3 本次范围与非目标

范围：`ops.splash.preview()`、下行帧 `webui.splash.play`、开场 `ui.ready` 的 `preview` 字段、前端门控的预览播放。

非目标：由宿主界面直接触发预览（预览按钮属于插件页面）；预览队列中的多个插件；预览期间修改播放参数。

## 3. 设计约束、假设与待决问题

- 包身份与连接来自调用上下文；预览只能发给发起交互的那个连接。
- 开场层、跳过、时限与 `ui.closed` 复用启动时的实现，避免两套播放逻辑。
- 待决问题：无。

## 4. 总体方案与设计取舍

### 4.1 方案概览

插件在点击处理中调用 `ops.splash.preview()`；Gateway 校验包声明了开场，向该连接发送 `webui.splash.play`，载荷带上声明的
`skippable` 与 `max_duration_ms`。前端门控在开放状态下收到该帧时，为这个包执行一次准备与播放：定向 `ui.ready` 带 `preview: true`，
插件据此忽略播放时机与停用设置并渲染开场。结束后照常本地关闭并通知 `ui.closed`，门控回到开放状态。

### 4.2 关键决策

#### D-01 由插件发起，宿主播放

预览入口在插件自己的设置页里，插件最清楚要预览哪个主题；宿主只负责在正确的连接上用统一的开场层播放。
不在宿主界面增加预览按钮，也不让插件绕过门控直接渲染到开场层。

#### D-02 帧中带上声明的时长与可跳过

前端播放需要 `skippable` 与 `max_duration_ms`。由 Gateway 在帧里给出，前端不必为一次预览再查询提供者列表；
数值与 `webui.splash.providers` 取自同一份声明。

#### D-03 `ui.ready` 增加必填的 `preview`

插件需要区分启动与预览：启动时按播放时机判断，预览时直接播放。字段为必填，Gateway 仍按完整字段集合校验；
前端与 Gateway 同步发布，没有旧前端兼容的问题。预览时 `first_visit` 固定为 false，因为预览不是首次访问。

#### D-04 预览不改写启动记录

预览发生在门控开放之后。完成记录、首次访问记录与候选判定都只属于启动流程，预览不写入，下次启动的判定不受影响。

### 4.3 替代方案与排除理由

| 方案 | 排除理由 |
| --- | --- |
| R-01 插件在设置页里渲染 `MediaSurface` | 媒体容器只允许作为背景与开场槽位的根；设置页里的嵌入播放没有开场的跳过、时限与音频语义 |
| R-02 宿主提供预览按钮组件 | 预览哪个主题属于插件业务；组件还要携带主题参数，复杂度更高 |
| R-03 复用 `webui.splash.providers` 后由前端查询 | 多一次往返，且被停用的包不在该列表中 |

## 5. 详细设计

### 5.1 模块责任

| 模块 | 修改 |
| --- | --- |
| Gateway `webui/bridge.py` | `ops.splash.preview`；`ui.ready` 字段集合增加 `preview` |
| Gateway `webui/transport.py`、`constants.py` | `webui.splash.play` 下行帧 |
| 前端 `splash/controller.ts` | `preview(provider)`：门控开放且空闲时播放一次；`ui.ready` 载荷带 `preview` |
| 前端 `react/WebUISplashGate.tsx`、`WebUIAppRoot.tsx` | 订阅 `webui.splash.play` 并交给控制器 |

### 5.2 状态与生命周期

门控状态：开放 → 准备（预览）→ 播放 → 开放。准备或播放期间 `#root` 为 `inert`、开场层可见；结束后恢复。
控制器销毁时预览随之结束，不发送 `ui.closed`（与启动时相同）。

## 6. 影响面与实施设计

同步 S_09（已更新）、概览、计划与模块 `AGENTS.md`。GW-11 的开场用例因 `ui.ready` 载荷增加字段而更新。

## 7. 兼容与迁移

`ui.ready` 的开场载荷新增必填字段；仓库内只有前端与测试发送该帧，随本条目一并更新。已安装的开场插件若按完整字段读取载荷，需要接受新字段。

## 8. 验证设计、执行记录与结论

### 8.1 验证设计与通过条件

| 编号 | 场景 | 通过条件 |
| --- | --- | --- |
| F_14-V-01 | `ops.splash.preview` | 向调用连接发送带声明值的帧；未声明开场、失效激活、没有连接时拒绝；停用后仍可预览 |
| F_14-V-02 | `ui.ready` | `preview` 为必填布尔值，原样交给包 |
| F_14-V-03 | 前端控制器 | 开放时播放一次并带 `preview: true`、`first_visit: false`；播放中、未开放时忽略；不改写完成记录 |
| F_14-V-04 | 门控组件 | 收到帧后显示开场层并使界面 `inert`，结束后恢复 |
| F_14-V-05 | 回归与构建 | Gateway 扩展用例、Web 渠道用例、前端 WebUI 各套件通过；`npm run build` 成功 |
| F_14-V-06 | 浏览器 | Chromium 中从设置页点击到开场播放、结束并回到设置页 |

### 8.2 实际执行与证据

环境同 [F_13](F_13_application-settings-page.md) 第 8.2 节。

| 编号 | 用例或命令 | 结果 |
| --- | --- | --- |
| F_14-V-01、V-02 | `test_gateway_splash.py` 新增 2 项（预览帧只发往调用连接并带声明值、停用后仍可预览、预览的 `ui.ready` 原样交给包；未声明开场与没有连接时拒绝、连接不可达时 `delivery_failed`），以及非法载荷用例新增缺少 `preview` 与非布尔 `preview` 两种 | 通过 |
| F_14-V-03、V-04 | `test:webui-components` 新增 3 项（开放后预览播放一次且载荷为 `preview: true`、`first_visit: false`，播放中的第二次请求被忽略，不改写完成与首次访问记录；启动队列进行中忽略预览；门控组件收到请求后显示开场层并使界面 `inert`，结束后恢复并通知 `ui.closed`） | 通过 |
| 改动前对照 | 先写用例，在未实现的代码上运行 | Gateway 6 项失败（4 项既有用例因 `ui.ready` 新增字段被拒绝，2 项新用例因 `ops.splash` 不存在）；前端 4 项失败（1 项更新预期、3 项新用例） |
| F_14-V-05 | `tests/unit_tests/gateway/extensions_v2/` 485 项；`tests/unit_tests/channels/web/` 与 `test_trajectory_http.py` 150 项（1 项既有跳过）；前端 contracts 151、protocol 14、components 267、renderer 17、runtime 21、client 22、events 93、transport 9；`test:i18n-locales` 4；`tsc` 与 `npm run build` | 全部通过 |

F_14-V-06：在 F_13 的验证页中，外层加上真实的 `WebUISplashGate`（本次启动已完成，门控立即开放），测试包的设置页增加“Preview splash”按钮，
点击时调用 `ops.splash.preview()`，开场 `ui.ready` 渲染一个约 3 秒后自行结束的沙箱页面。

| 场景 | 结果 |
| --- | --- |
| 点击预览 | 插件得到 `{"status": "requested"}`；开场层进入播放，沙箱页面在开场层中运行，`#root` 为 `inert` |
| 插件收到的 `ui.ready` | `{"launch_id": "harness-launch", "first_visit": false, "reduced_motion": false, "preview": true}` |
| 播放结束 | 页面 `complete` 后开场层关闭，插件收到 `ui.closed`（`complete`），`#root` 恢复可交互 |
| 跳过提示 | 测试包未声明可跳过，开场层不显示长按提示 |

### 8.3 验证结论

第 2.2 节场景在单元范围与 Chromium 中成立；浏览器验证中 Gateway 运行时是真实的，WebChannel 以验证服务中的最小实现代替。

## 9. 风险与已知遗留

- 桌面打包版中的预览与音频随壁纸插件联调。
