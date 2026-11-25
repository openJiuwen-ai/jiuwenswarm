# 背景、开场和桌面形态

用户需要动态背景、界面配色、启动开场或桌宠形态时参考。运行域为 `gateway`。
以下片段仅供参考，不携带图片、音效、动画引擎或资源导入实现。

| 需求 | 主要事件和 Ops | 显示入口 |
| --- | --- | --- |
| 背景与开场 | `ui.ready`、`ops.ui.asset_url`、`ops.ui.render` | `application.background`、`application.splash` |
| 配色与面板透明度 | `ops.appearance.apply` | 宿主支持的外观字段 |
| 手动预览开场 | `ui.click`、`ops.splash.preview` | 已声明且已渲染的开场贡献 |
| 桌宠与窗口 | `ui.ready`、`ops.ui.render` | `application.presentation` |
| 设置与资源 | 按控件选择 `ui.click`、`ui.change` 或 `ui.select`，使用 `ops.store`、`ops.data.root` | `application.settings` |

## 1. 背景、配色和开场

背景用 `application.background`，开场用 `application.splash`，树根为 `MediaSurface`。
先在 `manifest.ui.items` 中声明所需贡献。例如背景声明如下，其他顶层字段沿用 [最小示例](../example.md#1-事件与效果)，domain 改为 `gateway`：

```json
{
  "type": "web.slot.contribution", "id": "background",
  "slot": "application.background", "scope": "application", "instanceKey": "background",
  "renderer": {"kind": "webui-tree"}
}
```

下面是入口对象中的背景方法，`register` 将 `ui.ready` 绑定到它，并按本系统插件身份和背景槽位匹配。
系统插件自行提供 `assets/background.png`，不把本机绝对路径写进页面：

```python
from dataclasses import replace
from agent_gateway.extensions_v2.webui.models import (
    RenderNode, RenderStatus, UiActionInput, UiActionOutput, UiRenderInput,
)

async def on_background_ready(self, event, ops, next_handler):
    receipt = await next_handler(event)
    request = event.event_input
    if not isinstance(request, UiActionInput):
        return receipt
    if not receipt.accepted and receipt.status != "unhandled":
        return receipt
    if ops is None or ops.package_metadata is None:
        return UiActionOutput(False, "context_unavailable")
    if request.target.package_id not in ("", ops.package_metadata.package_id):
        return receipt
    target = replace(request.target, package_id=ops.package_metadata.package_id, instance_key="background")
    if target.session_id or target.scope != "application" or not target.connection_id:
        return UiActionOutput(False, "invalid_target")
    source = await ops.ui.asset_url("background.png", source="package")
    root = RenderNode("MediaSurface", {
        "title": "背景", "kind": "image", "src": source, "fit": "cover",
    })
    output = await ops.ui.render(UiRenderInput(target, root))
    if output is None or output.status != RenderStatus.DELIVERED:
        return UiActionOutput(False, "render_failed")
    return UiActionOutput(True, "rendered")
```

- **默认回执：** Gateway 的 `ui.ready` 默认可能返回 unhandled，表示尚未有 Handler 处理；本 Handler 成功渲染后返回 rendered。明确的拒绝结果仍原样保留，不能照搬 AgentServer 的默认回执判断。
- **目标归属：** 保留宿主 `ui.ready` 给出的连接，只改成自己的实例和身份；应用级 session_id 为空。不要拿聊天会话目标渲染应用背景，也不要用浏览器自报的连接替代宿主上下文。
- **视频与 HTML：** kind 按资源改为 `video` 或 `html`，再选真实支持的 fit、position、loop、muted 等字段；资源 URL 仍由 `ops.ui.asset_url` 生成。
- **配色：** 先增加 `{"type": "web.slot.contribution", "id": "appearance", "slot": "application.appearance"}`，该项只声明能力，不加 renderer，也不渲染组件树。再调用例如 `await ops.appearance.apply({"accent": "#6366F1", "surface": "translucent", "surfaceOpacity": 0.88})`。背景渲染与配色是两次操作，分别检查回执；只传受支持字段。
- **开场：** 单独声明 `application.splash`，在 item 的 `splash` 中填写 skippable 和 maxDurationMs，并在就绪时渲染 MediaSurface。预览按钮再调用 `await ops.splash.preview()`；返回 requested 只表示播放请求已送达。
- **恢复原状：** 用 `UiRenderInput(target=target, mode=RenderMode.CLEAR)` 清除自己的背景或开场，RenderMode 从同域 models 导入；用 `await ops.appearance.apply(None)` 撤销自己的外观层。不能仅清背景却留下配色。

## 2. 桌宠与窗口形态

应用形态用 `application.presentation`，树根为 `ApplicationSurface`。
声明、就绪 Handler 和回执检查沿用上一节，改成自己的 presentation 槽位与实例；不要继续使用背景的 target。
在 Handler 中将资源构造成下列根节点，再调用 `ops.ui.render`：

```python
source = await ops.ui.asset_url("presentation.html", source="package")
root = RenderNode("ApplicationSurface", props={
    "src": source, "title": "桌面助手", "width": 320, "height": 400,
    "config": {"character": "assistant"},
})
```

- **页面职责：** HTML 实现角色、动画或自己的交互；config 只是传给该页面的配置，由页面解释，不能借此增加宿主未支持的窗口字段。
- **宿主职责：** 原生悬浮窗口取决于桌面宿主及其形态协议；投递成功不等于原生窗口已经打开。浏览器与桌面端分别验收。
- **窗口参数：** 当前契约的 width 为 160–960、height 为 120–960，config 为受大小限制的 JSON 对象。新增字段前核对目标版本，不猜 autoStart、置顶等能力。
- **网页权限：** 页面内再嵌套 iframe 时，同时核对外层贡献权限与内层 sandbox/allow；具体写法见 [前端开发说明](../frontend-development.md)。

## 3. 设置、恢复和验证

1. 在 `application.settings` 声明独立设置项。选择主题、修改透明度或关闭形态时，校验 action 和取值，再调用对应 Ops，渲染实际结果。
2. 用 `ops.store` 保存已确认的选择；重新 `ui.ready` 时读取并恢复。不保存 Ops 对象，不用每次刷新产生新实例的方式叠加背景或窗口。
3. 需要导入资源时才使用 `ops.data.root()`，资源发布用 `source="data"`。补齐路径、大小、格式和取消清理，不为单纯展示复制整套导入管理器。
4. 检查切换、关闭、重新就绪、重载和卸载后的效果。存在连接内存状态时订阅 `gateway.disconnect` 清理；“每天首次播放”需要根据真实 `ui.closed` 等生命周期记录，不能把渲染成功当成已经播放完。
