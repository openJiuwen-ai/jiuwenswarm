# UI 基础组件来源

[English](PROVENANCE.md) | 中文

本目录中的文件复制自 DeepSeek Harness commit `99f6f02fecdb7dff40c3fbc9470f5907c29f74ca` 的 `packages/client/ui-primitives/src`，包含轨迹查看器所需的模块。这些文件依据 MIT 许可证使用并在本仓库中维护，因此本仓库在运行时、构建、workspace 或源码路径上均不依赖 DeepSeek Harness。

复制内容包括 Tooltip、Menu、JSON tree、Markdown 解析和渲染、语法高亮、KaTeX 渲染、图标及其 CSS。JiuwenSwarm 副本增加了轨迹局部的 portal 主题传递，并把具体展示颜色移入限定作用域的主题 token。`index.ts` 有意仅导出本 feature 使用的基础组件。
