---
name: arch-design
description: |
  架构选型与设计方法论：架构模式对比、领域驱动设计、C4 模型图、ADR 架构决策记录、架构设计文档。
  TRIGGER when: 系统架构选型、微服务 vs 单体选型、事件驱动/CQRS 评估、分层/六边形/洋葱架构、领域建模、限界上下文、聚合、画 C4 模型图、写 ADR、架构决策记录、输出架构设计文档。
  DO NOT TRIGGER when: 方案评审（用 arch-review）、架构演进规划（用 arch-evolution）、全栈工程实践（用 arch-engineering）。
---

# 架构选型与设计

## 目标

为架构选型和设计提供结构化决策框架，确保每个决策有 trade-off 分析、关键决策有 ADR 记录、架构图用 C4 模型分层表达。

## 工作流

### 1. 领域发现

- 通过事件风暴识别领域事件和命令
- 映射限界上下文，划定模块边界
- 定义聚合根和不变量
- 建立上下文映射（上游/下游、防腐层、共享内核）

DDD 核心概念职责表和"何时不用 DDD"判断见：
`references/architecture-patterns.md`（领域驱动设计指南节）

### 2. 架构选型

根据团队规模、业务复杂度和演进阶段选择架构模式。7 种模式对比矩阵（模块化单体/微服务/事件驱动/CQRS/分层/六边形/洋葱）见：
`references/architecture-patterns.md`

核心决策原则：
- 小团队 + 边界不清晰 → 模块化单体
- 清晰领域 + 团队自治 → 微服务
- 松耦合 + 异步工作流 → 事件驱动
- 读写不对称 + 复杂查询 → CQRS
- 优先选择可逆决策而非"最优"决策

C4 模型画法规范（Mermaid/PlantUML 模板、命名规范、每层粒度、坏味道标红）见：
`references/c4-conventions.md`

### 3. 决策记录

关键架构决策必须写 ADR。模板、示例和编号规范见：
`references/adr-guide.md`

### 4. 架构设计文档

输出架构设计方案时遵循标准模板（12 节结构）见：
`references/architecture-spec-template.md`

## 决策规则

- 每个抽象层必须证明其复杂性的合理性——不做架构宇航员
- 领域优先，技术其次——先理解业务问题再选工具
- 架构方案含至少两个备选选项及 trade-off 矩阵
- 关键决策附 ADR（Context → Decision → Consequences）
- 架构图用 C4 模型分层表达（Context / Container / Component / Code）
