---
heading_level: 2
checklist: |
**数据模型设计**

1. 数据模型变更是否说明 (WARNING)
   - 与现有系统的差异已描述

2. 实体关系是否清晰 (WARNING)
   - 实体-关系图或文字说明清晰易懂

3. 关键字段是否定义 (ERROR)
   - 主要实体字段、类型、约束已说明

4. 是否考虑向后兼容 (WARNING)
   - 新增字段对旧数据的影响

5. 是否仅涉及必要时填充 (INFO)
   - 系统不涉及数据结构变更时应该省略
---
## 数据模型 <!-- condition: Low=Skip, Medium=AsNeeded, High=AsNeeded -->

<!-- condition: Generate only when the system involves data structure changes or major data relationship changes. -->
<!-- guideline: Describe the primary entities and their relationships, emphasizing changes from the existing system. -->
<!-- guideline: Field ownership and entity relationships must be explicit. Use text for simple relationships; for complex ones (changes involving 2+ entity associations), use diagrams. -->
<!-- guideline: Address design considerations for special scenarios: memory-hardware coordination, concurrency & memory model, memory management & lifetimes, extreme boundary conditions, and safety. -->

### []

**描述**：[changes compared to the existing system]

**详细设计**：

[Entity-Relationship diagram or textual description of key data models, relationships, etc.]

### []