# 认识 AgentSSAS

> 本篇介绍 AgentSSAS 的定位、解决的安全问题与核心概念，帮助你在动手之前建立整体认识。适合所有初次接触 AgentSSAS 的用户阅读，不需要任何前置知识。

## AgentSSAS 是什么

AgentSSAS（Python 包名 `agent-ssas`）是一个通用的智能体安全态势感知系统（Agent Security Situational Awareness System），提供实时的威胁检测、事件采集和安全决策能力。AgentSSAS 不绑定特定的智能体框架，目前已接入 JiuwenSwarm 多智能体框架。

大模型智能体在运行时会执行工具调用、读写数据、生成内容，这些动态行为带来了传统应用不具备的安全风险。AgentSSAS 关注三类典型问题：

- **智能体运行时的行为风险**：多步行为链异常，例如通过多轮诱导逐步升级操作、执行破坏性命令、植入持久化机制
- **数据泄露**：敏感数据经 LLM 推理或工具调用流向外部，例如用户隐私信息被拼进 prompt 后写入文件或发送到外部服务
- **工具滥用**：调用未被授权的工具、构造危险参数（如删除文件系统的 shell 命令）

AgentSSAS 的做法是：在智能体运行时以安全 Rail 的形式采集 Agent 行为事件，将事件送入检测引擎分析，产出统一格式的风险评估结果，并落盘为可追溯的威胁日志。整个过程遵循 **fail-open** 设计原则：AgentSSAS 自身出现任何异常都不会影响智能体框架的主流程。

## 与 JiuwenSwarm / agent-core 的关系

AgentSSAS 由两个子系统组成，分别对应包内的两个路径：

| 子系统 | 包路径 | 职责 |
| ------ | ------ | ---- |
| **AgentSSASCore** | `agent_ssas.core` | 通用威胁检测引擎：数据预处理、分析流水线、检测模块管理、存储、呈现 |
| **AgentSSASClient** | `agent_ssas.backend_client` | 事件采集与上报客户端：当前提供 openjiuwen 客户端（`agent_ssas.backend_client.openjiuwen`），核心类 `AgentSSASSecurityRail` 继承 agent-core 的 `BaseSecurityRail` 基类，把 Agent 行为组装为事件并上报给 AgentSSASCore |

三者的依赖关系值得特别注意：

- `agent-ssas` 包本身**不依赖 openjiuwen（agent-core）包**，安装它只需要 pyyaml（HTTP 模式额外需要 fastapi / uvicorn / httpx）
- **AgentSSASCore 可以完全独立使用**：任何 Python 进程都可以创建分析引擎、上报事件、获取风险评估（参见[快速开始](./02-quickstart.md)）
- 只有 **AgentSSASClient 的插桩代码**需要 jiuwenswarm 运行时（openjiuwen 的 `BaseSecurityRail` 等），因为它必须以 Rail 形式挂进 JiuwenSwarm 的 Agent 执行链路

也就是说：如果你只想用检测引擎分析自己的事件流，不需要安装 JiuwenSwarm；如果你想让 JiuwenSwarm 的 Agent 行为被自动采集和检测，才需要走集成流程（参见[集成 JiuwenSwarm](./03-integrate-jiuwenswarm.md)）。

> **术语说明**：面向 JiuwenSwarm 的完整集成方案——AgentSSASCore 加上 AgentSSASClient 中的 openjiuwen 客户端——在早期文档中也被称为 **JiuwenSSAS**。它只是一个逻辑概念，指代这套组合，并不是一个独立的系统。

## 核心概念

| 概念 | 说明 |
| ---- | ---- |
| **事件（Event）** | Agent 运行行为的结构化记录，共 7 个：6 个生命周期事件（`invoke_start` / `llm_input` / `tool_input` / `tool_output` / `llm_output` / `invoke_end`）加 1 个安全检测衍生事件（`permission_interrupt_tool`）。事件采用三层结构：`common`（14 个关联字段）、`payload`（事件内容）、`metadata`（预留扩展） |
| **检测模块（Detection Module）** | 订阅事件并执行威胁分析的独立单元。由 `module.yaml` 声明、一个数据建模插件和一个威胁分析插件组成，拥有独立的存储目录 |
| **订阅模式** | `notify`：后台异步检测，不阻断主流程（当前内置模块均为该模式）；`auth`：同步参与安全决策，上报方等待检测结果返回 |
| **RiskAssessment** | 检测结果的统一输出格式，共 9 个字段：`has_risk`、`risk_level`、`risk_type`、`risk_score`、`confidence`、`detected_threats`、`recommended_actions`、`details`、`evidence` |
| **决策策略** | 把 RiskAssessment 映射为安全决策的规则表（`decision_policies.yaml`）：`observe_only`（默认，全部放行，仅态势感知）；`active_protection`（critical 拒绝阻断，high / medium / low 告警，safe 放行） |
| **运行模式** | `inprocess`（进程内库模式，默认，零网络延迟）；`http`（独立 FastAPI 服务，多 Agent 共享同一分析引擎） |

风险等级 `RiskLevel` 共五级，从低到高依次为：

```text
safe < low < medium < high < critical
```

### 内置检测模块

AgentSSAS 自带 3 个检测模块：

| 模块 | 默认状态 | 订阅事件 | 说明 |
| ---- | -------- | -------- | ---- |
| `agent_moss` | `enabled: true` | 6 个生命周期事件 | 内置 AgentMoss 分析引擎，提供 rule（确定性规则）、behavior_chain（行为链）、pdg（数据泄露分析）三种分析方法 |
| `security_rail_detection` | `enabled: true` | `permission_interrupt_tool` | 识别并上报其他安全 Rail 的拦截决策，只做上报与呈现 |
| `test_detection` | `enabled: false` | `*`（全部事件） | 空白插件透传所有事件，用于验证流水线是否跑通 |

你也可以编写自己的检测模块，参见[编写自定义检测模块](./05-custom-detection-module.md)。

## 0.1.0 的能力总览与边界

**当前版本（v0.1.0）已具备的能力：**

- 7 类事件的三层结构采集与上报
- 双运行模式：inprocess（进程内，默认）与 http（独立 FastAPI 服务）
- 检测模块框架：module.yaml 声明 + 数据建模插件 + 威胁分析插件，启动时自动扫描加载
- 3 个内置检测模块（见上表）
- 统一风险评估输出（RiskAssessment）
- SQLite 落盘存储与 OCSF 格式威胁日志
- fail-open：SSAS 异常不影响 JiuwenSwarm 主流程

**当前版本的边界：**

- 内置检测模块均为 `notify` 模式：检测结果异步写入威胁日志与告警表，**不会阻断** JiuwenSwarm 的执行
- `auth` 订阅模式与 `active_protection` 决策策略属于机制预留：框架支持同步检测与按风险处置，但 0.1.0 尚未内置使用它们的检测模块
- 因此 0.1.0 的定位以**态势感知**为主：看得见、查得到、可追溯，暂不直接拦截

有关这些设计取舍的背景，参见[架构解析](../explanation/architecture.md)；notify 与 auth 订阅模式的详细说明见[检测模块参考](../reference/detection-modules.md)。

## 下一步

- [快速开始](./02-quickstart.md)：十分钟内跑通进程内模式的最小闭环
- [集成 JiuwenSwarm](./03-integrate-jiuwenswarm.md)：让 JiuwenSwarm 的 Agent 行为被自动采集
- [HTTP 服务模式](./04-http-mode.md)：多 Agent 共享同一分析引擎
- [事件格式参考](../reference/event-format.md)：事件的完整字段定义
