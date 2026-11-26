# 应用形态与应用内悬浮

Gateway 插件通过 `application.presentation` 槽位发布 `ApplicationSurface`，可声明两种承载方式：

| mode | 使用方式 |
| --- | --- |
| desktop（默认） | 桌面客户端创建透明原生窗口，页面 ready 后收起主工作台 |
| overlay | 浏览器和桌面客户端在应用内容区显示悬浮容器，主工作台保持可见 |

在现有插件的渲染处理器里构造根节点，素材 URL 使用当前包、当前激活代次实际发布的地址：

```python
root = RenderNode("ApplicationSurface", {
    "title": {"zh": "我的桌宠", "en": "My companion"},
    "src": resource_url,
    "mode": "overlay",
    "autoStart": True,
    "width": 160,
    "height": 120,
})
```

插件须声明 Gateway 域及该槽位。overlay 声明 `autoStart: True` 后，安装或启用会直接显示，禁用会关闭，无需形态切换菜单；应用重载后重新显示。
`autoStart` 默认 False，两种显示方式均可使用；desktop 仍需要桌面客户端。普通候选继续通过“切换应用形态”菜单或已有 `presentation query/start` 命令显式启动。
同一连接同时只运行一个形态。未声明 mode 的已有资源继续使用 desktop。

插件自行提供启停设置时，使用 `Switch.checked` + `ui.change` 将操作交给插件后端。后端通过 `ops.ui.render` 发布 `autoStart: True` 的形态，禁用时通过同一 Ops 的 CLEAR 清除。DELIVERED 仅表示帧已下发，不表示容器已经 ready；打开失败通过 `ui.closed` 通知插件关闭开关。开启当前会话不离开正在查看的页面。

点叉时宿主先销毁真实容器，再通过 `webui.dispatch` 上报 `ui.closed`（reason、revision、presentation_id）。Gateway 校验原连接、包、实例、版本和容器序号后通知所属插件；候选继续保留，由插件决定是否 CLEAR、更新显示状态并用 `ui.render` 刷新设置页。旧关闭不覆盖新形态，旧插件也能重新打开同一候选。`visible_when` 只表达显示条件，宿主不会据此修改偏好。形态选择、显示方式和启停策略由插件决定。

## 插件页面的拖动方式

页面继续使用 `workswarm.presentation` v1 沙箱协议：先注册消息监听，再向 parent 发送 ready。
宿主随后发送 state。overlay 的 state 额外带 `host.bounds`（当前容器位置与大小）和 `host.workArea`（整个应用视口）。
iframe 内的 window.innerWidth/innerHeight 是内容尺寸，不能作为桌宠的活动边界。

在拖动命中区调用 `setPointerCapture(event.pointerId)`，用不会随 iframe 移动而变化的 screenX/screenY 计算累计位移。
先将累计位移转换为整数，再减去已发送的累计值，发送 move 的 deltaX/deltaY；不要逐帧独立取整后累加。
pointerup 使用相同计算提交松手位置，再结束拖动。
宿主移动整个容器并约束应用边界。页面展开或收起时通过 resize 申请尺寸，宿主同步实际几何结果。
可运行的资源示例见 [拖动示例 HTML](../../jiuwenswarm/channels/web/frontend/tests/fixtures/presentationOverlayPet.html)。

## 生命周期与限制

切换会话或进入设置等页面保留 iframe。关闭按钮、停用、卸载、断线及页面失败会销毁悬浮容器。
修改根的 mode 或 src 会结束当前形态；自动显示的新资源重新启动，普通候选需显式启动。更新 config 不重建页面。
手动关闭不会因普通更新再次出现；可停用后重新启用，或重载应用。自动启动遇到已有活动形态时明确提示，不替换当前形态。
发送、回复和状态沿用现有会话入口；workspace 在应用内定位会话并保留悬浮层。
容器外正常点击应用，矩形内由插件接收输入。透明像素不提供逐像素穿透，应以紧凑容器承载桌宠。
overlay 不支持原生窗口截图过渡与 hitTest，这些动作返回 UNSUPPORTED_ACTION。

完整字段和生命周期见 [S_17](../../jiuwenswarm/extensions_v2/docs/specs/S_17_webui-component-props.md) 与独立 agent-gateway 仓库的 [S_11](https://gitcode.com/openJiuwen/agent-gateway/blob/develop/agent_gateway/extensions_v2/docs/specs/S_11_application-presentation.md)。客户端与 Gateway 均须包含 mode / autoStart 支持。
