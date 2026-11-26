# 能力选择记录

- **来源：** 教学示例，不对应真实 DeepSeek Harness 插件。
- **运行域：** `agentserver`。
- **事件：** `ui.ready`，目标槽位挂载时触发。
- **Ops：** `ops.ui.render`，向已有会话槽位发送渲染树。
- **UI 槽位：** `conversation.input.above`。
- **验收：** 从最终候选 ZIP 安装后触发 `ui.ready`，核对渲染帧中的文本。
