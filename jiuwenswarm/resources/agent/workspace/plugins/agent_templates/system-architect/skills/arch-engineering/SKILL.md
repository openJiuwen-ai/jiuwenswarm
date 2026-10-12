---
name: arch-engineering
description: |
  全栈工程实践：项目分层与三层架构、配置管理、错误处理、数据库访问、缓存、实时通信、API 设计与治理、认证与授权架构、环境与配置管理、云原生基础设施、技术选型评估。
  TRIGGER when: 全栈架构设计、前后端集成、项目分层、Feature-first 结构、依赖注入、类型化错误体系、N+1 查询、缓存模式、SSE/WebSocket、REST/GraphQL/gRPC/tRPC 选型、API 版本化、契约管理、JWT/Session/OAuth2、RBAC、中间件顺序、CORS、多环境策略、K8s 决策、服务网格、多区域部署、IaC、技术选型评估、可观测性建设、OpenTelemetry、日志规范、指标埋点、链路追踪、事件驱动架构、消息队列选型、Kafka/RabbitMQ、事件溯源、Transactional Outbox、CDC。
  DO NOT TRIGGER when: 架构选型（用 arch-design）、方案评审（用 arch-review）、架构演进规划（用 arch-evolution）。
---

# 全栈工程实践

## 目标

为全栈架构设计和工程实现提供实践指南，确保项目分层清晰、配置管理规范、错误处理类型化、API 契约稳定、认证架构安全、环境配置可管理。

## 工作流

### 1. 全栈开发架构实践

项目结构（Feature-first）、三层架构（Controller→Service→Repository）、依赖注入（TS/Python/Go）、错误处理体系、数据库访问模式（N+1/事务/连接池）、缓存模式、后台任务、实时通信、反模式速查表见：
`references/fullstack-practices.md`

配置管理、环境变量校验、.env.example 规范和 CORS 配置见：
`references/environment-management.md`

### 2. API 设计与治理

API 风格选型（REST/GraphQL/gRPC/tRPC 决策树）、REST 设计规范、版本化与向后兼容、API 网关与 BFF 模式、OpenAPI 契约管理见：
`references/api-governance.md`

### 3. 认证与授权架构

JWT vs Session 决策、JWT Bearer 流程、Token 自动刷新、RBAC 四层权限检查、中间件 10 步顺序、OAuth2/OIDC 授权码流程见：
`references/auth-flow.md`

### 4. 云原生基础设施

K8s 命名空间划分与资源管理、服务网格选型（Linkerd/Istio/Cilium）、多区域多活部署、IaC 模块化、云成本优化见：
`references/cloud-native-infra.md`

### 5. 技术选型评估

6 维度评估矩阵（成熟度/团队匹配/生态/运维/性能/退出成本）、语言/框架/数据库/基础设施选型、Build vs Buy 决策见：
`references/technology-selection.md`

### 6. 可观测性建设

OpenTelemetry 集成、结构化日志规范、指标埋点（RED/USE）、Trace 串联、告警分级、仪表盘见：
`references/observability.md`

### 7. 事件/消息平台

消息中间件选型（Kafka/RabbitMQ/NATS/Pulsar）、事件命名规范、Schema 演进、Transactional Outbox、CDC、幂等消费、死信队列、事件溯源见：
`references/event-platform.md`

## 决策规则

- 按 FEATURE 组织代码，不按技术层
- Controller 不含业务逻辑，Service 不导入 HTTP 类型
- 所有配置走环境变量，启动校验，快速失败
- 每个错误都有类型，全局处理，统一格式
- 所有输入在边界校验——不信任客户端
- 技术选型含 6 维度评估矩阵和退出策略，不只说"用这个"
