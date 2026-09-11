# AgentSSAS 设计文档（design/）

本目录存放 AgentSSAS 的**开发设计稿**，面向项目开发者和贡献者，记录架构机制、接口契约与实现设计。这些内容属于开发过程资产，与面向最终用户的文档（[docs/](../docs/README.md)）分开归档。

## 文档清单

| 文档 | 内容 |
|------|------|
| [AgentSSAS_01_整体架构设计文档.md](AgentSSAS_01_整体架构设计文档.md) | 整体架构、设计思路、跨仓接口规格、交互、构建、验证、工作计划 |
| [AgentSSAS_02_AgentSSASClient功能设计文档.md](AgentSSAS_02_AgentSSASClient功能设计文档.md) | AgentSSASClient 子系统功能设计（事件采集 Rail，对接 JiuwenSwarm） |
| [AgentSSAS_03_AgentSSASCore功能设计文档.md](AgentSSAS_03_AgentSSASCore功能设计文档.md) | AgentSSASCore 子系统功能设计（通用威胁检测引擎） |

## 阅读顺序

1. 先读 01 整体架构设计文档，建立对两个子系统、跨仓接口（`AgentSSASBackendProtocol`、raw_event 三层结构）的全局认知。
2. 再按关注点选读：对接 JiuwenSwarm 读 02，检测引擎内部机制读 03。

## test-reports/

[test-reports/](test-reports/) 存放开发过程中的测试报告与测试结果（质量佐证材料）：

| 文件 | 内容 |
|------|------|
| [AgentSSAS_测试报告.md](test-reports/AgentSSAS_测试报告.md) | 三阶段测试报告（单元 / 集成 / 端到端） |
| [test_results.xml](test-reports/test_results.xml) | JUnit 格式测试结果（历史产物） |

## 说明

- 设计文档为中文（开发内部资产，暂不提供英文版）。
- 用户文档（教程、指南、参考、原理）位于 [docs/](../docs/README.md)，双语（zh/en）。
- 如设计文档与本目录外文档冲突，以最新代码实现为准，并欢迎提 Issue 指正。
