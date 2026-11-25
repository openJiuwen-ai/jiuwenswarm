# Harness 工作台

在 JiuwenSwarm 侧栏底部选择下一任务使用的 Harness，并为已绑定的 Claude / Codex 会话提供主题化界面装饰。

## 功能

- 侧栏底部 `application.sidebar.footer` 提供「普通对话 / Claude Code / Codex」切换；欢迎页无会话时也可操作。
- 激活时安装固定专家 `harness-claude-expert` 与 `harness-codex-expert`；Harness 类型冲突时不覆盖已有专家，切换入口仍可用。
- 已绑定会话展示输入区状态、标题标识、Claude 工具流与右侧沙箱面板，或 Codex 执行摘要；普通对话清除 Harness 专属装饰。

## 生效机制

1. 用户在侧栏或输入区上方选择下一任务的 Harness 模式，触发 `ui.click` / `ui.select`。
2. 各会话插槽在 `ui.ready` 时渲染对应界面树；已绑定会话依据运行时绑定展示 chrome。
3. `context.usage.updated` 在一轮结束后刷新已绑定会话的界面状态。
4. `session.append` 在转录写入后刷新活动区展示。

完整体验需配合 Gateway 包 `sample.harness-workbench.gateway`：由 Gateway 负责连接级主题预览与新会话首条消息模板注入，本包负责切换 UI、专家安装与会话 chrome。

## 优势

- 欢迎页即可设定下一任务的 Harness，无需先建会话。
- 动画资源均提供静态 PNG 回退，适合减少动态效果环境。
- 卸载本包不删除已安装专家与会话历史绑定。
