# Cursor 本地开发记忆机制

本机制维护 JiuwenSwarm 自己的 Cursor 开发记录 `docs/ai/`（已被根目录 `.gitignore` 忽略）。

它与 Downloads 下的外部长期项目 KB 是两套不同系统：

- `docs/ai/`：Cursor 开发过程、实验、问题、轨迹、QA、阶段和交接。
- 外部 KB：跨会话的长期项目知识，由其他维护流程负责。

Cursor hooks 不应把 `docs/ai/` 重定向到外部 KB，也不应删除原有开发记录。

## Hooks

- `sessionStart`：读取并注入 `docs/ai/HANDOFF.md`、阶段和偏好。
- `preCompact`：提醒在压缩前写回当前 Cursor 开发记录。
- `sessionEnd`：保存轻量 session 记录到 `docs/ai/_sessions/`。

主约束由 always-apply rules 提供，hook 只负责启动和提醒。

