# openjiuwen 扩展性开发指南

OpenJiuwen 扩展用于向宿主添加事件处理、平台能力和界面交互。按照下面的顺序阅读，可以先了解扩展范围，再创建最小扩展，接着学习事件、Ops、测试和完整参考。

## 阅读顺序

| 顺序 | 文档 | 你将在这里看到 |
| --- | --- | --- |
| 1 | [1.【概览】OpenJiuwen 扩展性概览](<1.【概览】OpenJiuwen 扩展性概览.md>) | 扩展可以做什么、如何工作、由什么组成、在哪里运行，以及扩展边界。 |
| 2 | [2.【创建扩展】创建第一个 OpenJiuwen 扩展](<2.【创建扩展】创建第一个 OpenJiuwen 扩展.md>) | 从扩展包、`manifest.json`、事件和 Handler 开始，完成一个最小扩展。 |
| 3 | [3.【事件】使用事件响应宿主流程](<3.【事件】使用事件响应宿主流程.md>) | 事件生命周期、Matcher、处理链、结果、错误和取消。 |
| 4 | [4.【Ops】使用 Ops API](<4.【Ops】使用 Ops API.md>) | Ops 的命名、输入输出、权限、生命周期和宿主差异。 |
| 5 | [5. 【前端】前端扩展](<5. 【前端】前端扩展.md>) | 前端槽位、组件、渲染生命周期和浏览器客户端扩展。 |
| 6 | [6.【调测与辅助开发】OpenJiuwen 扩展调测与 AI 辅助开发](<6.【调测与辅助开发】OpenJiuwen 扩展调测与 AI 辅助开发.md>) | 覆盖扩展调测、问题定位、测试验证和 AI 辅助开发边界。 |
| 7 | [7.【API文档完整版】OpenJiuwen 扩展参考](<7.【API文档完整版】OpenJiuwen 扩展参考.md>) | WebClient、Gateway、AgentServer、事件、Ops 和宿主边界。 |
| 8 | [8.【API文档索引快查】OpenJiuwen 扩展 API 索引](<8.【API文档索引快查】OpenJiuwen 扩展 API 索引.md>) | 快速查询能力名称、功能定位、输入和输出。 |

## 当前文档范围

1.x 文档负责解释开发过程和完整契约，其中 [7.【API文档完整版】OpenJiuwen 扩展参考](<7.【API文档完整版】OpenJiuwen 扩展参考.md>) 按 Web Client、Gateway、AgentServer 详尽说明事件、Ops 方法、前端槽位、组件和宿主边界；[8.【API文档索引快查】OpenJiuwen 扩展 API 索引](<8.【API文档索引快查】OpenJiuwen 扩展 API 索引.md>) 只保留功能定位、输入和输出，作为快速查询入口。

文档中的 API 状态分为已实现、已确认待实现、目标设计和暂不支持。设计模型用于统一概念，不代表当前版本已经提供可调用入口。

## 相关文档

- [5. 【前端】前端扩展](<5. 【前端】前端扩展.md>)
- [6.【调测与辅助开发】OpenJiuwen 扩展调测与 AI 辅助开发](<6.【调测与辅助开发】OpenJiuwen 扩展调测与 AI 辅助开发.md>)
- [7.【API文档完整版】OpenJiuwen 扩展参考](<7.【API文档完整版】OpenJiuwen 扩展参考.md>)
- [8.【API文档索引快查】OpenJiuwen 扩展 API 索引](<8.【API文档索引快查】OpenJiuwen 扩展 API 索引.md>)
