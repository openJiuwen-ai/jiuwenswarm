# 最简全双工

当前只保留快模型路由与原 SDK 的执行对接。

| 图 | 预览 | PlantUML 源文件 |
| --- | --- | --- |
| 架构图 | [SVG](duplex-architecture.svg) | [PUML](duplex-architecture.puml) |
| 正常执行 / 新请求处理概览（A、B 并行） | [SVG](duplex-routing-flow.svg) | [PUML](duplex-routing-flow.puml) |
| 文件 / 类 / 方法级详图（A、B 分别编号） | [SVG](duplex-code-flow.svg) | [PUML](duplex-code-flow.puml) |

[详细版说明与源码索引](duplex-code-level.md)：从实际入口展开到快模型请求、原 steer、安全暂停、恢复与已读确认。

A 是正常执行循环：调用慢模型，需要时执行工具并收集结果，没有新消息也执行。B 只在新消息到达时触发；快模型判断期间 A 继续运行。

- APPEND：调用原 steer；在下一次原有慢模型调用前注入，不取消当前计算，也不补额外轮次。
- INTERRUPT：在 SDK supervisor 中校验状态，安全暂停，保留当前上下文和完整工具结果，再作废旧计划并结合新消息重新规划。工具已开始时沿用 SDK 等待完成。
- 快模型失败、超时或决定过期：原 steer。每条消息只判断一次，不合批重判。

消息确认沿用原 SDK：投递返回之后再确认已读。没有独立持久接受或后台投递承诺。

[实现与边界](../duplex-simplification.md)

本地渲染：

```powershell
java -jar plantuml.jar -charset UTF-8 -tsvg docs/diagrams/duplex-architecture.puml docs/diagrams/duplex-routing-flow.puml docs/diagrams/duplex-code-flow.puml
```
