---
name: arch-evolution
description: |
  架构演进规划与治理：绞杀者模式迁移、技术债务量化评级、分阶段改造路线图、团队拓扑与 Conway's Law、ARB 评审流程、技术雷达管理、数据库 schema 设计与迁移。
  TRIGGER when: 架构演进规划、技术债务管理、迁移路线图、绞杀者模式、灰度切换、并行运行、团队拓扑、Conway's Law、架构治理、ARB 评审、技术雷达、ADR 生命周期、数据库 schema 设计、索引策略、多租户、分片、大表迁移。
  DO NOT TRIGGER when: 架构选型（用 arch-design）、方案评审（用 arch-review）、全栈工程实践（用 arch-engineering）。
---

# 架构演进与治理

## 目标

制定从现状到目标架构的安全过渡方案，管理技术债务，确保架构边界与团队拓扑对齐，建立可持续的架构治理体系。

## 工作流

### 1. 架构演进规划

从现状到目标架构的安全过渡策略。绞杀者模式落地步骤、技术债务 D1-D4 评级矩阵、四阶段演进框架、路线图模板见：
`references/evolution-strategy.md`

### 2. 数据库 schema 设计与迁移

涉及数据库变更时制定 schema 迁移方案。索引策略、迁移管理、大表分步变更、多租户模式、分片策略见：
`references/db-schema.md`

### 3. 架构治理与团队协作

确保架构边界与团队拓扑对齐（Conway's Law）。Team Topologies 四类团队模型、ARB 评审流程、技术雷达四象限、ADR 生命周期管理、架构原则制定与传播见：
`references/governance.md`

## 决策规则

- 架构边界与团队边界对齐——Conway's Law，避免分布式单体
- 技术债务可视化管理——评级、排期、每迭代预留偿还容量
- 演进方案含分阶段迁移路线图，每阶段标注回退方案和验收标准
- 数据库迁移必须可回滚，大表变更分步执行
- 架构原则写入团队 Wiki，代码审查时引用，季度回顾适用性
