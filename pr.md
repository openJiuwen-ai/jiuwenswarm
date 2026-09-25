Paired: [GitHub #7346](https://github.com/openJiuwen-ai/jiuwenswarm/pull/7346) ↔ [GitCode !7475](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/7475)

## 与此前 PR 的关系

本 PR 是此前全双工功能的后续增量修复，目标分支仍为 `0.2.7`，延续以下 PR 的功能：

- [#2813](https://github.com/openJiuwen-ai/jiuwenswarm/pull/2813)、[#5301](https://github.com/openJiuwen-ai/jiuwenswarm/pull/5301)：此前已进入 `0.2.7` 的全双工相关功能。
- [#6846](https://github.com/openJiuwen-ai/jiuwenswarm/pull/6846)：后续全双工相关改动，由 [#7265](https://github.com/openJiuwen-ai/jiuwenswarm/pull/7265)（[GitCode !7430](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/7430)）定向移植到 `0.2.7`。

本次沿用上述功能和现有界面，仅新增下面的权限确认修复和统一回复语言配置。此前 PR 的功能已作为基线存在，不重复提交其代码，也不合入 `develop` 或 `fix/Multi-language-support` 的其他历史及任务协议重构。

## 本次新增改动

### 全双工权限确认卡片修复

全双工委托 Core Agent 执行工具时，权限确认事件此前可能被当作空输出，或者回答被发往没有活动执行的主 web 会话，导致 `returned empty output` 或 `session has no active execution`。

- 复用现有 `InteractionSlot` / `AuthorizationPrompt` 界面展示确认卡片。
- 通过 `video.search.answer` 将回答送回原始 Core Agent 会话，继续执行原任务。
- 支持连续确认、拒绝、取消，以及重连后的待确认卡片补发。
- 校验任务、会话和请求标识，拒绝过期或不匹配的回答；权限判断仍由 Core Agent 执行。

### JoyAI 与 Qwen 共用回复语言配置

移植 `fix/Multi-language-support` 的语言逻辑，提供“跟随对话 / 简体中文 / 英语”配置，默认跟随对话，在下次启动全双工时生效。JoyAI 与 Qwen 的会话回复与任务结果播报共用这一配置，替代原先跟随全局 `preferred_language` 的行为。

JoyAI 与 Qwen 均在现有设置界面选择语言。JoyAI 的用户输入和定时画面请求复用相同设置；Core Agent 语音回执也遵循该设置，不再强制使用中文。

## 关联 Issue

沿用此前 PR 的两组关联，便于追踪同一全双工功能的后续修复：

- 第一组：[agent-core #1751](https://gicode.com/openJiuwen/agent-core/issues/1751)、[jiuwenswarm #3930](https://gicode.com/openJiuwen/jiuwenswarm/issues/3930)。
- 第二组：[agent-core #1762](https://gicode.com/openJiuwen/agent-core/issues/1762)、[jiuwenswarm #3816](https://gicode.com/openJiuwen/jiuwenswarm/issues/3816)。

## 验证

- 后端 `test_video_live.py`：119 passed，覆盖权限确认、连续确认、取消及语言配置。
- JoyAI `npm run test:joyai-workflow`：16 passed。
- 前端 `npm run test:qwen-barge-in`：70 passed，包含语言与任务播报测试。
- `npm run build`、`git diff --check` 通过。
- 与 `origin/0.2.7`（`971ba33e`）进行 `merge-tree` 检查，无冲突。
- 本地服务已启动，前端 HTTP 200，AgentServer / Gateway / WebChannel 端口就绪。
- 已在浏览器验证 JoyAI 配置草稿显示三种回复语言选项，并取消草稿保留原模型配置；尚未完成真实模型语音端到端测试及窄屏布局验证。
