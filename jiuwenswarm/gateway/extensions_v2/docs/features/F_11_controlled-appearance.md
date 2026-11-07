# 受控外观设计方案

## 1. 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | feature |
| 编号 / slug | `F_11 / controlled-appearance` |
| 建档日期 | 2026-11-05 |
| 计划条目 | [EXTV2-GW-10](../designs/extensions-v2-development-plan.md) |
| 整体设计 | [应用级界面能力设计概览](../designs/application-surfaces-overview.md) 第 3.3 节 |
| 方案成熟度 | 已确认，已实现 |
| 关联规约 | [S_08 受控外观](../specs/S_08_appearance.md)、[S_07 媒体容器](../specs/S_07_media-surface.md)、[S_06 浏览器桥接](../specs/S_06_local-browser-bridge.md) |
| 验证摘要 | 单元验证与 Chromium 157 无头模式验证通过（2026-11-05）；桌面打包版联调随 GW-11 |

## 2. 背景、目标与范围

### 2.1 背景与问题

壁纸类插件需要让宿主界面配合背景：强调色随主题变化，大面板半透明以透出背景。V1 主题引擎靠注入样式表实现，
直接覆盖任意 `--color-*` 变量并用类选择器加 `!important` 调整容器，插件可以写出无法阅读的配色，
宿主改版也会悄悄失效。V2 中插件不能访问宿主 DOM，需要宿主提供受控的外观能力。

另外，Gateway 释放包（卸载、重载）时只在服务端丢弃该包的应用级实例，不通知浏览器，背景会在卸载后继续显示。

### 2.2 目标与关键场景

| 场景 | 预期行为 |
| --- | --- |
| 插件提交强调色 | 宿主派生按钮、开关、链接、焦点等变量，按钮文字保持可读 |
| 插件选择半透明且已显示背景 | 宿主按预设降低大面板不透明度，弹出层与文字保持可读 |
| 插件恢复默认 | 以空值撤销，界面回到宿主主题 |
| 另一插件提交外观 | 替换前一提供者 |
| 插件卸载或重载 | 外观撤销、背景清除；重载后插件被重新通知并可再次提交 |
| 网络中断后重连 | 不闪烁；插件未重新提交时撤销旧外观 |

### 2.3 本次范围与非目标

范围：`ops.appearance.apply`、校验、提供者登记、`webui.appearance` 下行帧、前端派生与应用、包释放时的撤销与实例清除、
重载后的重新通知。

非目标：暗色模式专用外观变量（概览已推迟）；任意样式表或字体；应用级设置页（GW-12）。

## 3. 设计约束、假设与待决问题

- 当前前端只有浅色主题，派生基色从当前主题变量读取，主题切换时重新计算。
- 前端 antd 主题桥接从计算样式读取变量，变量必须是浏览器可直接解析的颜色值，不能含 `calc()`。
- 无待决问题。

## 4. 总体方案与设计取舍

### 4.1 方案概览

插件提交小而固定的外观对象（强调色、表面模式、少量装饰变量）。Gateway 校验并按连接登记提供者，
经 `webui.appearance` 帧投递。前端外观宿主把对象派生为具体的 CSS 变量，写入一个样式元素；
半透明部分只在背景显示时通过属性选择器生效。包释放时 Gateway 主动撤销外观并清除实例。

### 4.2 关键决策

#### D-01 宿主派生，插件只给意图

插件给出强调色与半透明强度，宿主负责派生约 20 个变量并保证对比度。可读性规则只在宿主一处实现，
宿主改版时同步维护，插件不依赖宿主的类名。

#### D-02 装饰变量白名单

保留 V1 主题实际使用的少量装饰变量作为白名单，满足主题细节；文本、表面与弹出层不开放。

#### D-03 按连接的单一提供者

外观与应用级实例一样以浏览器连接为作用域；每个连接只认最后一次成功提交的包，切换即替换。
非提供者的撤销不影响当前外观，避免两个插件互相清除。

#### D-04 包释放时主动通知浏览器

释放包时向浏览器发送撤销帧与实例清除帧，补齐此前只在服务端丢弃实例的缺口；重载成功后复用安装时的
`extensions.v2.installed` 广播让槽位重新发送 `ui.ready`，插件以新激活的素材地址重新渲染。

#### D-05 断线保留、重连核对

浏览器断线时保留外观，与应用级实例一致，避免短暂断线闪烁；重连后应用级 `ui.ready` 全部返回仍未收到新外观，
说明提供者已不在，浏览器撤销旧外观。同一连接内的帧按 `revision` 排序。

#### D-06 具体数值写入样式元素

派生结果写成具体颜色值放入宿主样式元素，选择器比主题规则更具体；基色在写入前从主题变量读取。

### 4.3 替代方案与排除理由

| 方案 | 排除理由 |
| --- | --- |
| R-01 沿用 V1 任意变量与样式表 | 插件可破坏可读性，且依赖宿主类名 |
| R-02 在 Gateway 派生变量 | 派生依赖当前主题与背景状态，属于前端展示 |
| R-03 用 `calc()` 写半透明值 | antd 主题桥接无法解析 |
| R-04 断线即撤销 | 短暂断线造成闪烁，与实例保留语义不一致 |

## 5. 详细设计

### 5.1 模块责任

| 模块 | 修改 |
| --- | --- |
| Gateway `webui/appearance.py` | 外观对象校验、提供者登记 |
| Gateway `webui/bridge.py` | `ops.appearance.apply`；包释放时撤销外观 |
| Gateway `webui/transport.py`、`webui/constants.py` | `webui.appearance` 帧 |
| Gateway `runtime.py` | 释放包时向浏览器清除实例与撤销外观；断开连接时丢弃登记 |
| Gateway `management/local_cli.py` | 重载成功后广播 `extensions.v2.installed` |
| 前端 `appearance/` | 帧校验、派生、样式生成 |
| 前端 `transport/`、`runtime/` | 订阅帧、按连接排序、重连核对 |
| 前端 `react/WebUIAppearanceHost.tsx`、`WebUIAppRoot.tsx` | 应用与撤销 |

### 5.2 状态与生命周期

见 [S_08 第 5 节](../specs/S_08_appearance.md)。

2026-11-07 修订（PR #10763 检视）：提供者原先在外观帧发送完成后才登记；首帧发送途中卸载包时，释放逻辑找不到提供者，
不发送撤销，在途帧随后仍到达浏览器，外观残留。现在发送前登记在途记录，释放同时撤销在途连接，撤销帧在同一连接锁内发送，
排在在途帧之后；回归用例 `test_release_during_the_first_send_still_revokes_the_appearance` 在修复前失败、修复后通过。

## 6. 影响面与实施设计

同步 S_06（应用级 UI 包可用的 Ops）、S_01（包释放语义）、概览与计划；没有新增用户可见文字。

## 7. 兼容与迁移

纯新增接口。包释放时向浏览器清除实例是行为补正：此前实例在服务端已失效，浏览器只是继续显示失效内容。

## 8. 验证设计、执行记录与结论

### 8.1 验证设计与通过条件

| 编号 | 场景 | 通过条件 |
| --- | --- | --- |
| F_11-V-01 | 外观对象校验 | 共享用例两端结论一致 |
| F_11-V-02 | 接口 | 投递帧；返回状态；非应用级包、旧激活、无连接、非法对象被拒绝 |
| F_11-V-03 | 提供者 | 切换替换；非提供者撤销为 `unchanged`；断开连接后丢弃登记 |
| F_11-V-04 | 包释放 | 卸载与重载撤销外观并清除实例；重载后广播 |
| F_11-V-05 | 前端帧处理 | 合法帧应用、非法帧与旧 `revision` 忽略；重连核对撤销 |
| F_11-V-06 | 派生 | 强调色变量与对比度；token 覆盖；半透明只在背景显示时生效；撤销恢复主题 |
| F_11-V-07 | 回归与构建 | Gateway 扩展用例、前端 WebUI 用例通过；`npm run build` 成功 |
| F_11-V-08 | 浏览器 | 在 Chromium 中确认派生变量、半透明效果与 antd 组件取色 |

### 8.2 实际执行与证据

环境同 [F_10](F_10_media-surface-and-background.md) 第 8.2 节。

| 编号 | 用例或命令 | 结果 |
| --- | --- | --- |
| F_11-V-01 | `gatewayAppearanceCases.json` 38 条共享用例；`test_gateway_appearance.py` 与前端 `test:webui-components` 同时执行 | 两端结论一致，全部通过 |
| F_11-V-02、V-03 | `test_apply_revoke_and_provider_switch`、`test_invalid_calls_are_rejected_without_frames`、`test_closed_connection_forgets_its_provider` | 通过 |
| F_11-V-04 | `test_release_revokes_appearance_and_clears_views`、`test_local_cli_reload_asks_mounted_slots_for_ready` | 通过 |
| F_11-V-05 | `test:webui-runtime` 新增 2 项（排序与非法帧、重连核对）；`test:webui-transport` 订阅用例 | 通过 |
| F_11-V-06 | `test:webui-components` 新增 3 项（强调色对比度、半透明预设、外观宿主写入与撤销） | 通过 |
| F_11-V-07 | `tests/unit_tests/gateway/extensions_v2/` 413 项；`test_app_web_extension_assets.py` 与 `test_trajectory_http.py` 42 项；前端 contracts 150、protocol 14、components 234、renderer 17、runtime 21、client 22、events 93、transport 9；`tsc` 与 `npm run build` | 全部通过；构建仅有既有的分包与体积提示 |

新增 Gateway 行为用例（V-02～V-04 共 5 项）在恢复修改前的 Gateway 源码后全部失败。`test:webui-transport`
原有 3 项断言订阅数量，随新增的 `webui.appearance` 订阅更新，并改为同时校验外观帧的投递。

F_11-V-08：在 Chromium 157 无头模式中加载验证页，页面使用宿主真实的 `light.css` 主题、`WebUIThemeProvider`
中的 antd 按钮与开关、`WebUIBackgroundHost` 与 `WebUIAppearanceHost`，并以宿主容器类名模拟工作区：

| 步骤 | 结果 |
| --- | --- |
| 基线 | 主色 `#2563eb`，按钮与开关同色，各表面不透明 |
| 提交 `accent: #ffdd00` | 派生强色 `#857300`；antd 按钮、开关与链接随之变化 |
| 提交 `#b45309`、半透明 0.6、侧栏高亮 token，无背景 | 强调色与 token 生效，表面保持不透明 |
| 显示背景 | 页面根透明；侧栏与工作区容器 0.42、内层透明、卡片 0.68、弹出层 0.97；文字颜色不变 |
| 清除背景 | 表面恢复不透明，强调色保留 |
| 撤销 | 全部恢复主题默认值；`data-theme`、`data-color-mode` 全程不变 |

### 8.3 验证结论

第 2.2 节场景在单元范围与 Chromium 中成立。桌面打包版（Electron）与真实插件的联调随 GW-11 与插件重写进行。

## 9. 风险与已知遗留

### 9.2 已知遗留

- 暗色模式专用外观变量未支持（概览已推迟）。
- 浏览器在断线期间仍显示旧外观；若提供者在断线期间被卸载，重连核对后撤销。
