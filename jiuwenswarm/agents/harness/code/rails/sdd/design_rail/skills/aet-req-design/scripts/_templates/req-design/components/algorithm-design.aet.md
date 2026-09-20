---
heading_level: 2
checklist: |
**算法设计质量**

1. 算法目标是否明确 (WARNING)
   - 解决的问题和支持的需求清晰说明

2. 核心逻辑是否清晰 (ERROR)
   - 算法描述、流程图或伪代码易懂
   - 核心逻辑是否存在歧义

3. 输入输出是否完整 (WARNING)
   - 输入输出关键无遗漏
   - 描述是否存在歧义

5. 边界条件是否处理 (WARNING)
   - 关键异常情况和边界条件，制定处理策略

6. 是否撰写简单算法 (WARNING)
   - 简单 CRUD 或线性业务逻辑不检出
---

## 算法设计 <!-- condition: Low=Skip, Medium=AsNeeded, High=AsNeeded -->

<!-- guideline: If core algorithms requiring special design are involved, describe them here. Simple business logic may be omitted. Focus on algorithmic approach, design rationale, and key complexity metrics. -->
<!-- constraint: DO NOT include this section for simple CRUD or linear business processes. -->

### [algorithm/rule]

**目标**：[What problem does this algorithm solve? What requirement does it support?]

**核心逻辑**：

```
[Algorithm description, mermaid diagram, or pseudocode]
```

**输入**：

<!-- guideline: Specify inputs/outputs clearly; annotate units & implicit assumptions where needed to eliminate ambiguity. -->

- [item]: [argument description]

**输出**：
- [item]: [result description]

**复杂度分析**：[]

**边界条件与异常处理**：

- [Condition]: [Handling approach]

### []