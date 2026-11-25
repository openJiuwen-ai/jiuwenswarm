# 背景、开场和桌面形态

用户需要动态背景、界面配色、启动开场或桌宠形态时参考。运行域为 `gateway`。
以下片段仅供参考，不携带图片、音效、动画引擎或资源导入实现。

## 1. 背景、配色和开场

背景用 `application.background`，开场用 `application.splash`，树根为 `MediaSurface`。
下面是 Handler 内构建图片背景的片段，资源文件需由本次系统插件自己提供：

```python
from agent_gateway.extensions_v2.webui.models import RenderNode

source = await ops.ui.asset_url("background.png", source="package")
root = RenderNode(component="MediaSurface", props={
    "title": "背景", "kind": "image", "src": source, "fit": "cover",
})
```

- **显示：** 用所属域的 `ops.ui.render` 投递到已声明的槽位；不是对工作台 DOM 直接改样式。
- **配色与开场：** 配色查 `ops.appearance.apply`，预览开场查 `ops.splash.preview`；参数以目标 Gateway 契约为准。关闭效果时按契约撤销，避免残留外观。
- **偏好：** 用 `ops.store` 保存已选主题、开场时机等设置；资源需要落盘时使用 `ops.data.root`。未知字段不直接传给宿主。

## 2. 桌宠与窗口形态

应用形态用 `application.presentation`，树根为 `ApplicationSurface`，不同资源使用稳定的实例标识：

```python
source = await ops.ui.asset_url("presentation.html", source="package")
root = RenderNode(component="ApplicationSurface", props={"src": source, "title": "桌面助手"})
```

- **窗口与资源：** 再按实际需求补受支持的窗口参数、资源配置和设置入口，不把背景组件当作桌面窗口。
- **运行环境：** 原生悬浮窗口取决于桌面宿主及其形态协议；浏览器环境不能因声明组件就具备同样窗口能力。
- **导入功能：** 只有用户需要时才开发 ZIP、图片或音效导入，并核对路径、大小和资源格式；不为一个形态展示复制整套资源管理器。
