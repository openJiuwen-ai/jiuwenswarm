# AgentSSAS 文档中心

AgentSSAS（Python 包名 `agent-ssas`）是通用的智能体安全态势感知系统，提供实时威胁检测、事件采集和安全决策能力，当前已接入 JiuwenSwarm 多智能体框架。本目录是 AgentSSAS 的文档中心，提供中英双语（`zh/` 与 `en/`）两套结构完全一致的文档。

如果你是第一次接触 AgentSSAS，建议从中文教程的[认识 AgentSSAS](./zh/tutorial/01-introduction.md) 开始，依次阅读[快速开始](./zh/tutorial/02-quickstart.md)与[集成 JiuwenSwarm](./zh/tutorial/03-integrate-jiuwenswarm.md)。

## 文档组织

文档遵循 [Diátaxis](https://diataxis.fr/) 框架，按读者的使用目的分为四类：

| 类型 | 目录 | 导向 | 内容特点 |
| ---- | ---- | ---- | -------- |
| 教程（tutorial） | `zh/tutorial/` | 学习导向 | 手把手带领新手完成一个完整任务，保证成功 |
| 指南（how-to） | `zh/how-to/` | 任务导向 | 针对明确目标给出操作步骤，假设读者已具备基础 |
| 参考（reference） | `zh/reference/` | 查阅导向 | 准确、中性、完整的事实描述（配置项、字段、API） |
| 解析（explanation） | `zh/explanation/` | 理解导向 | 解释设计背景、权衡与原理，不指挥具体操作 |

## 命名与术语

| 名称 | 含义 |
| ---- | ---- |
| **AgentSSAS** | 系统统一名称：通用的智能体安全态势感知系统；Python 包名 `agent-ssas`，导入名 `agent_ssas` |
| **AgentSSASCore** | 子系统一：通用威胁检测引擎，包 `agent_ssas.core` |
| **AgentSSASClient** | 子系统二：事件采集与上报客户端，包 `agent_ssas.backend_client`；当前提供 openjiuwen 客户端，核心类 `AgentSSASSecurityRail` |
| **JiuwenSSAS** | 逻辑概念：AgentSSASCore 子系统加上 AgentSSASClient 子系统中的 openjiuwen 客户端，即面向 JiuwenSwarm 的完整集成方案；物理代码与包统一命名为 AgentSSAS |

AgentSSAS 不绑定特定智能体框架，当前已接入 JiuwenSwarm 多智能体框架。

## 中文文档索引

### 教程（tutorial）

| 文档 | 内容 |
| ---- | ---- |
| [01-introduction.md](./zh/tutorial/01-introduction.md) | 认识 AgentSSAS：定位、解决的安全问题、核心概念与 0.1.0 能力边界 |
| [02-quickstart.md](./zh/tutorial/02-quickstart.md) | 快速开始：安装并在进程内模式跑通最小闭环，查看落盘数据 |
| [03-integrate-jiuwenswarm.md](./zh/tutorial/03-integrate-jiuwenswarm.md) | 集成 JiuwenSwarm：应用 patch、配置、启动验证与数据检查 |
| [04-http-mode.md](./zh/tutorial/04-http-mode.md) | HTTP 服务模式：启动独立服务端、运行客户端、启用认证 |
| [05-custom-detection-module.md](./zh/tutorial/05-custom-detection-module.md) | 编写自定义检测模块：module.yaml 声明与两类插件的实现 |

### 指南（how-to）

| 文档 | 内容 |
| ---- | ---- |
| [enable-disable-detection-modules.md](./zh/how-to/enable-disable-detection-modules.md) | 启用或禁用检测模块 |
| [run-tests.md](./zh/how-to/run-tests.md) | 运行测试 |
| [switch-decision-policy.md](./zh/how-to/switch-decision-policy.md) | 切换决策策略 |
| [view-threat-logs.md](./zh/how-to/view-threat-logs.md) | 查看威胁日志 |

### 参考（reference）

| 文档 | 内容 |
| ---- | ---- |
| [configuration.md](./zh/reference/configuration.md) | 配置参考：ssas 段字段与环境变量 |
| [detection-modules.md](./zh/reference/detection-modules.md) | 检测模块参考：内置模块与 module.yaml 字段 |
| [event-format.md](./zh/reference/event-format.md) | 事件格式参考：三层结构与全部事件类型 |
| [http-api.md](./zh/reference/http-api.md) | HTTP API 参考：端点、请求与响应 |

### 解析（explanation）

| 文档 | 内容 |
| ---- | ---- |
| [architecture.md](./zh/explanation/architecture.md) | 架构解析：双子系统与双运行模式 |
| [event-model.md](./zh/explanation/event-model.md) | 事件模型解析：三层结构与事件流关联语义 |
| [detection-pipeline.md](./zh/explanation/detection-pipeline.md) | 检测流水线解析：report_event 全流程与聚合策略 |

## 其他资源

- [README.zh.md](../README.zh.md)：中文项目自述文件
- [README.md](../README.md)：英文项目自述文件
- [examples/](../examples/)：可运行的示例（进程内快速上手、HTTP 服务演示、JiuwenSwarm 集成）
- [design/](../design/)：设计文档（整体架构与两个子系统的功能设计）
- [CHANGELOG.md](../CHANGELOG.md)：版本变更记录
- [SECURITY.md](../SECURITY.md)：安全策略与漏洞报告方式

---

# AgentSSAS Documentation

AgentSSAS (Python package name `agent-ssas`) is a general-purpose Agent Security Situational Awareness System that provides real-time threat detection, event collection, and security decision capabilities; it currently integrates with the JiuwenSwarm multi-agent framework. This directory hosts the AgentSSAS documentation in two languages (`zh/` and `en/`) with an identical structure.

If you are new to AgentSSAS, start with the English tutorial [Introduction to AgentSSAS](./en/tutorial/01-introduction.md), then follow [Quickstart](./en/tutorial/02-quickstart.md) and [Integrating with JiuwenSwarm](./en/tutorial/03-integrate-jiuwenswarm.md).

## Documentation Organization

The documentation follows the [Diátaxis](https://diataxis.fr/) framework and is organized into four types by reader intent:

| Type | Directory | Orientation | Content |
| ---- | --------- | ----------- | ------- |
| Tutorial | `en/tutorial/` | Learning-oriented | Guided lessons that take a novice through a complete task |
| How-to guide | `en/how-to/` | Task-oriented | Recipes for achieving specific goals, assuming basic knowledge |
| Reference | `en/reference/` | Information-oriented | Accurate, neutral, complete descriptions (options, fields, APIs) |
| Explanation | `en/explanation/` | Understanding-oriented | Background, trade-offs, and design rationale |

## Naming and Terminology

| Name | Meaning |
| ---- | ------- |
| **AgentSSAS** | The unified system name: a general-purpose Agent Security Situational Awareness System; Python package `agent-ssas`, import name `agent_ssas` |
| **AgentSSASCore** | Subsystem 1: the general-purpose threat detection engine, package `agent_ssas.core` |
| **AgentSSASClient** | Subsystem 2: the event collection and reporting client, package `agent_ssas.backend_client`; currently provides the openjiuwen client, whose core class is `AgentSSASSecurityRail` |
| **JiuwenSSAS** | A logical concept: the AgentSSASCore subsystem plus the openjiuwen client of the AgentSSASClient subsystem, i.e. the complete integration for JiuwenSwarm; the codebase and package are uniformly named AgentSSAS |

AgentSSAS is not bound to any specific agent framework; it currently integrates with the JiuwenSwarm multi-agent framework.

## English Documentation Index

### Tutorials

| Document | Content |
| -------- | ------- |
| [01-introduction.md](./en/tutorial/01-introduction.md) | Introduction to AgentSSAS: positioning, security problems, core concepts, and v0.1.0 scope |
| [02-quickstart.md](./en/tutorial/02-quickstart.md) | Quickstart: install, run the minimal inprocess loop, inspect stored data |
| [03-integrate-jiuwenswarm.md](./en/tutorial/03-integrate-jiuwenswarm.md) | Integrating with JiuwenSwarm: apply the patch, configure, verify, and check data |
| [04-http-mode.md](./en/tutorial/04-http-mode.md) | HTTP service mode: start the standalone server, run the client, enable authentication |
| [05-custom-detection-module.md](./en/tutorial/05-custom-detection-module.md) | Writing a custom detection module: module.yaml and the two plugin interfaces |

### How-to Guides

| Document | Content |
| -------- | ------- |
| [enable-disable-detection-modules.md](./en/how-to/enable-disable-detection-modules.md) | Enable or disable detection modules |
| [run-tests.md](./en/how-to/run-tests.md) | Run the test suite |
| [switch-decision-policy.md](./en/how-to/switch-decision-policy.md) | Switch the decision policy |
| [view-threat-logs.md](./en/how-to/view-threat-logs.md) | View threat logs |

### Reference

| Document | Content |
| -------- | ------- |
| [configuration.md](./en/reference/configuration.md) | Configuration reference: ssas section fields and environment variables |
| [detection-modules.md](./en/reference/detection-modules.md) | Detection module reference: built-in modules and module.yaml fields |
| [event-format.md](./en/reference/event-format.md) | Event format reference: three-layer structure and all event types |
| [http-api.md](./en/reference/http-api.md) | HTTP API reference: endpoints, requests, and responses |

### Explanation

| Document | Content |
| -------- | ------- |
| [architecture.md](./en/explanation/architecture.md) | Architecture explained: two subsystems and two runtime modes |
| [event-model.md](./en/explanation/event-model.md) | Event model explained: three-layer structure and event correlation semantics |
| [detection-pipeline.md](./en/explanation/detection-pipeline.md) | Detection pipeline explained: the report_event journey and aggregation strategy |

## Other Resources

- [README.zh.md](../README.zh.md): Chinese project README
- [README.md](../README.md): English project README
- [examples/](../examples/): Runnable examples (inprocess quickstart, HTTP server demo, JiuwenSwarm integration)
- [design/](../design/): Design documents (overall architecture and subsystem designs)
- [CHANGELOG.md](../CHANGELOG.md): Release history
- [SECURITY.md](../SECURITY.md): Security policy and vulnerability reporting
