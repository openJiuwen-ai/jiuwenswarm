# 流光极昼（主题包）

动态背景示例，需要配合「主题接口」扩展使用。

- **动态背景**：`background/` 中的沙箱 canvas 页面，绘制柔和流动的三道光带（1/4 分辨率绘制后放大，24 fps）；减少动态时使用 `poster.svg`。
- **开场动画**：`boot/index.html` 是自定义 HTML 开场的最小示例——通过 `WorkSwarmThemeSDK.onStart` 获取研究员代号，显示按时段变化的问候语，2.75 秒后调用 `WorkSwarmThemeSDK.complete()` 进入界面。默认每天首次启动播放一次。
