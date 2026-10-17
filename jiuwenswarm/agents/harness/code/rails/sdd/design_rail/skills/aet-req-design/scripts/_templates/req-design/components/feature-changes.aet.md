---
heading_level: 2
checklist: |
**功能影响列表**

1. 功能树是否正确 (WARNING)
   - 业务功能由粗到细拆解，非代码维度概念

2. 变更点描述是否清晰 (WARNING)
---

## 功能影响

<!-- guideline: Focus on showing "what functions changed, corresponding to which requirements". If impact scope is small, can only keep the table. -->

<!--constraint: Function tree is a structured mapping of what a system *can do*, focusing on business functions and user value. Node must be an independent functional description from the user or business-flow perspective, in the form of "verb + noun" or "noun + function" (e.g., Manage product information, Process order refunds). DO NOT include code-level concepts such as Class, Function, or Method. -->

```text
- [e.g. 电子商城系统]
  - [e.g. 购物车管理]
    - [e.g. 清空购物车]
  - [e.g. 订单处理]
    - [e.g. 创建订单] [change description: e.g. "新增/删除订单状态检查逻辑"]
```

|功能|变更|变更点|对应需求|
|-|-|-|-|
|[Functional node]|[增/删/改]|[description]|[]|
