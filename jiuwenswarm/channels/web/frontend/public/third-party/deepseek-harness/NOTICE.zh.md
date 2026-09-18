# DeepSeek Harness 轨迹源码声明

[English](NOTICE.md) | 中文

JiuwenSwarm 的轨迹 renderer 改编自 DeepSeek Harness 提交 `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca` 中的下列源码目录：

- `packages/client/ui-trajectory`
- `packages/client/ui-theme/src/styles`
- `packages/client/ui-primitives/src`

改编范围包括轨迹浏览、时间线和表格展示、主题 token，以及 Tooltip、Menu、JSON tree、Markdown 与语法高亮内容展示、KaTeX 集成和图标。JiuwenSwarm 在此基础上增加了 OpenTelemetry projector、自有数据传输、单 Agent 宿主集成和兼容字段映射。

DeepSeek 源码依据同目录随附的 [MIT 许可证](LICENSE)分发。版权仍归 DeepSeek 及其他适用的版权所有者所有。
