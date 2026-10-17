---
heading_level: 2
checklist: |
**核心流程**

1. 流程图选择是否合理 (WARNING)
   - 根据设计需要选择合适的图表类型：sequenceDiagram、stateDiagram-v2、flowchart

2. 流程图是否有实际设计价值 (WARNING)
   - 无价值图标不应检出

3. 图表语法是否正确 (WARNING)
   - Mermaid 图表语法正确，可渲染
---


## 核心流程

### [主流程]

[Core business process overview, including but not limited to steps and key decision points]

<!-- guideline: sequenceDiagram for cross-module interaction flows, stateDiagram-v2 for state transitions, flowchart for decision branches. -->
```mermaid
sequenceDiagram
    participant U as [User/Caller]
    participant A as [Module A]
    participant B as [Module B]

    U->>A: [Trigger action]
    A->>B: [Internal call]
    B-->>A: [Return result]
    A-->>U: [Response]
```

### [] <!-- condition: generate only if omission would cause ambiguity -->

