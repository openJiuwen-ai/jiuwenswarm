# Harness 工作台组合包

一次安装 Harness 工作台的 AgentServer 与 Gateway 两个系统插件成员。

## 成员

- `sample.harness-workbench`：侧栏底部的普通对话 / Claude / Codex 切换、专家安装和已绑定会话的界面装饰。
- `sample.harness-workbench.gateway`：按浏览器连接预览 Claude / Codex 主题，并在新会话首条消息注入对应专家模板。

## 生效机制

1. AgentServer 成员渲染侧栏切换与会话 chrome，并把选择通过 `webui.dispatch` 同步给 Gateway。
2. Gateway 成员处理 `gateway.request`，在 `session.create` 后武装下一次 `chat.send`，只改写一次模板字段。
3. Gateway 成员处理 `ui.ready`，在 `application.background` 恢复当前连接的主题预览。

## 说明

- 两个成员保留独立包标识和版本，由各自宿主安装、加载。
- 组合包只改变分发方式，不改变成员的界面与事件行为。

## 示例说明

本示例为 1.0.1。选择入口首次就绪时，通过异步目录接口核对并准备固定专家；同 ID、同 Harness 的已有专家会保留。专家资源包含当前导入器要求的 README，persona 文件使用较短名称以兼容 Windows 路径长度。
