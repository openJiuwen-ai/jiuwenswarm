# 受控外观规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_08 / appearance` |
| 所属模块 | Gateway `webui/`（接口、校验、提供者登记与投递）；Web 前端 `features/webui/`（派生与应用） |
| 最近一次修订日期 | 2026-11-07 |
| 契约状态 | 已实现 |
| 关联文档 | [F_11](../features/F_11_controlled-appearance.md)、[S_06 浏览器桥接](S_06_local-browser-bridge.md)、[S_07 媒体容器](S_07_media-surface.md) |

本文维护 `ops.appearance.apply` 的接口、外观对象、宿主派生规则、下行帧、提供者生命周期与不变量。

## 1. 接口

```python
await ops.appearance.apply(appearance: Mapping | None) -> dict
```

- 调用方须是活动的应用级 UI 包（[S_06](S_06_local-browser-bridge.md)），调用须属于该包当前激活，
  且发生在带浏览器连接的事件中（如应用级槽位的 `ui.ready`、`ui.click`）。外观作用于该连接。
- `appearance` 为 `None` 表示撤销本包提交的外观。
- 经事件链执行：Ops 只触发 `ui.appearance`（输入携带调用包 `package_id` 供匹配与查看；调用方由宿主在事件之外传递，见 S_01 第 4 节），USER Handler 可以拒绝或接管（返回自己的 `HostActionOutput`，例如 `{"status": "denied", "code": …}`，Ops 原样返回），继续后由 CORE 默认动作核对激活与连接再执行；CORE 拒绝时 Ops 抛出 `ValueError`。
- 返回 `{"status": <状态>, "revision": <int>}`：

| 状态 | 含义 |
| --- | --- |
| `applied` | 已投递，本包成为该连接的外观提供者 |
| `revoked` | 本包是提供者，已投递撤销 |
| `unchanged` | 撤销请求来自非提供者，未投递 |
| `delivery_failed` | 连接已不可用，提供者记录不变 |

- 外观对象不合法、调用方不是活动的应用级 UI 包、调用属于旧激活或没有浏览器连接时抛出 `ValueError`，状态不变。

## 2. 外观对象

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `accent` | string | 否 | `#RRGGBB` |
| `surface` | `opaque` / `translucent` | 否 | 省略按 `opaque` |
| `surfaceOpacity` | number | 否 | 0.3～1；只能与 `surface: "translucent"` 同时提供；省略按 0.86 |
| `tokens` | object | 否 | 至少一项；键取自第 2.1 节白名单，值为第 2.2 节的颜色 |

对象至少包含一个字段，不接受表外字段。

### 2.1 token 白名单

`--color-scrollbar-thumb`、`--color-sidebar-nav-active`、`--color-sidebar-nav-active-hover`、
`--color-conversation-active-surface`、`--color-chat-accent-subtle`、`--color-action-primary-subtle`、
`--color-action-primary-glow`、`--color-border-accent`。

白名单只含装饰性高亮变量；文本、表面与弹出层变量由宿主维护，插件不能直接修改。

### 2.2 颜色

`#rgb`、`#rrggbb`、`#rrggbbaa`（十六进制，大小写不限），或 `rgb(r, g, b)`、`rgba(r, g, b, a)`：
r、g、b 为 0～255 的整数，a 为 0～1 的数，逗号两侧可有空格。不接受其他写法。

## 3. 宿主派生

### 3.1 强调色

宿主由 `accent` 派生强色：将其逐步与黑色混合，直到与白色的对比度不低于 4.5:1；悬停色在强色基础上与白色混合，
保持与白色的对比度不低于 3:1。

| 变量 | 取值 |
| --- | --- |
| `--color-action-primary`、`--color-border-focus`、`--color-chat-accent`、`--color-chat-send-active`、`--color-switch-track-on`、`--color-settings-switch-checked`、`--color-skill-capsule-active`、`--color-conversation-unread`、`--color-settings-link`、`--color-cron-action-link`、`--color-text-link` | 强色 |
| `--color-action-primary-rgb` | 强色的 `r, g, b` |
| `--color-action-primary-hover` | 悬停色 |
| `--color-action-primary-subtle`、`--color-chat-accent-subtle`、`--color-action-primary-glow`、`--color-border-accent` | `accent` 不透明度 0.12、0.14、0.16、0.5 |
| `--color-border-accent-rgb` | `accent` 的 `r, g, b` |

`tokens` 在强调色派生之后应用，同名时以 `tokens` 为准。

### 3.2 半透明模式

只在 `surface` 为 `translucent` 且应用背景显示（S_07 的 `data-webui-background="active"`）时生效。
设不透明度为 o，基色取当前主题对应变量的颜色：

| 表面 | 不透明度 |
| --- | --- |
| 页面底色（`--color-surface-page`、`--color-surface-page-accent`、`--color-extension-section-surface`） | o × 0.42 |
| 侧栏与导航（`--color-sidebar-surface`、`--color-settings-nav-surface`、`--color-surface-chrome`、`--color-rsi-canvas-bg`） | o × 0.7 |
| 内容与面板（`--color-surface-content`、`--color-surface-panel`、`--color-rsi-page-bg`） | min(1, o + 0.02) |
| 卡片与输入（`--color-surface-card`、`--color-surface-elevated`、`--color-chat-composer-surface`） | min(1, o + 0.08) |
| 外壳（`--color-surface-shell`） | 透明 |

主题按角色提供的表面变量同样只在根上覆盖：

| 角色变量 | 半透明时 |
| --- | --- |
| `--color-surface-workspace`（大面积工作区容器） | 卡片基色 × o × 0.7 |
| `--color-surface-workspace-layer`（叠在工作区上的内层） | 透明 |
| `--color-surface-floating`（浮起的侧栏与设置导航） | 侧栏基色 × 0.9 |
| `--effect-surface-floating-backdrop`（浮起侧栏的磨砂） | `blur(24px) saturate(1.3)`，不透明模式为 `none` |
| `--color-surface-popover`（菜单、对话框、抽屉与提示） | 卡片基色 × 0.97 |

宿主另使页面根与应用根透明。外观样式只写这些变量，不出现任何业务组件的类名；各组件消费变量（主题文件中定义默认值），
新增弹出层只需使用 `--color-surface-popover`。全部文本变量不变。

## 4. 下行帧

事件 `webui.appearance`，载荷：

```json
{ "package_id": "<提供者>", "revision": 3, "appearance": { "accent": "#b45309" } }
```

`appearance` 为 `null` 表示撤销。`revision` 在同一连接内递增；浏览器在同一连接内忽略不大于已接受值的帧。
载荷不合法时浏览器忽略该帧。

## 5. 提供者与生命周期

| 情况 | 行为 |
| --- | --- |
| 包提交非空外观 | 成为该连接的提供者，替换此前其他包的外观 |
| 提供者提交 `None` | 投递撤销帧 |
| 非提供者提交 `None` | 不投递，返回 `unchanged` |
| 包释放（卸载、重载） | 向其作为提供者的连接、以及该包外观帧仍在发送中的连接投递撤销帧；撤销帧在同一连接上排在在途帧之后；向浏览器清除该包的应用级实例 |
| 重载成功 | 广播 `extensions.v2.installed`，已挂载的槽位重新发送 `ui.ready` |
| 连接关闭 | Gateway 丢弃该连接的提供者记录 |
| 浏览器断线 | 保留当前外观；重连后应用级槽位重新发送 `ui.ready`，全部返回后仍未收到新外观时浏览器撤销旧外观 |

撤销只移除外观贡献层，不改变 `data-theme` 与 `data-color-mode`。

## 6. 不变量

1. 外观只能改变第 3 节列出的变量与页面根、应用根的背景；不接受任意 CSS、选择器、`url()` 或 `var()`。
2. 文本颜色不受外观影响，弹出层在半透明模式下保持 0.97 的近不透明；派生的按钮主色与白色文字的对比度不低于 4.5:1。
3. 半透明只在应用背景显示时生效，背景移除即恢复不透明表面。
4. 包身份与连接来自调用上下文；包不能为其他包或其他连接提交外观。
5. 每个连接至多一个外观提供者。
6. 撤销与提供者切换不改变用户的明暗主题选择。
7. 包释放后，浏览器最终不保留该包的外观：释放时仍在发送的外观帧同样会被一个更大 revision 的撤销帧覆盖；
   释放前已通过激活核对、仍在等待连接锁的调用，取得锁后再次核对激活，失败则不发送任何帧（抛出 `ValueError`），
   因此不会出现比撤销帧 revision 更大的该包外观帧。

## 7. 验证入口

| 契约 | 用例 |
| --- | --- |
| 外观对象校验（两端一致） | `gatewayAppearanceCases.json`；`test_gateway_appearance.py`；前端 `test:webui-runtime` |
| 接口、提供者与释放 | `test_gateway_appearance.py` |
| 帧处理、断线与派生 | 前端 `test:webui-runtime`、`test:webui-components` |
