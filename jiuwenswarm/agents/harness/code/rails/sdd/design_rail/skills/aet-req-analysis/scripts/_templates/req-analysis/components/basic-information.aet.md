---
heading_level: 2
checklist: |
**项目背景**

1. 需求价值是否具体可追溯 (INFO)
   - 避免"提升用户体验"等泛泛描述
   - 需追溯到业务根源，如"故障发现时间从15分钟缩短到实时"

2. 需求描述字段是否界定边界 (WARNING)
   - 说明"要做什么"和本质范围
   - 明确边界，说明"不做什么"

**结构化信息**

1. 内容是否具体可验证 (ERROR)
   - 每项 1-2 句，避免模糊表达

2. 定义 What 而不是 How (ERROR)
   - 需求禁止规定具体实现路径

3. Who 是否涵盖所有角色 (INFO)
   - 主要用户 + 管理员角色，不遗漏
---

## 基本信息

### 项目背景

<!-- guideline: Use 2-3 sentences to explain requirement value, emphasize "why it is worth doing", and trace back to business root causes whenever possible. -->

需求价值：[Core value this requirement brings to business]

<!-- constraint: DO NOT omit the “what it is NOT” part. -->

需求描述：[Essence and scope of the requirement, what it is and what it is NOT (boundary definition), 2-3 sentences]

### 结构化信息

<!-- policy: The "How" dimension must describe only the user operation flow to use the capability, strictly prohibiting internal implementation paths. 
e.g. 
  - Bad: "How describes the kernel scheduling path, IPI, Kconfig" → Should be user operation flow.
  - Bad: "How describes checkbox state, API route /api/submit, and function signature submitForm()" → Should be business capability description. -->

<!-- instruct: Each item should be controlled within 1-2 sentences, try to be specific and verifiable, avoid vague expressions. -->

<!-- constraint: DO NOT include multiple unrelated requests in one IR; the requests in the same IR must be strongly related. -->

|维度|内容|
|-|-|
|Who|[Stakeholder, the subject such as people or objects in the context, e.g. Developer, Administrator, etc.]|
|When|[At which lifecycle stage it is used]|
|What|[Stakeholder's expectation of the system (specific, not too abstract)]|
|Why|[Reason why the requirement arises] <!-- condition: AsNeeded -->|
|Where|[Environment where the requirement arises] <!-- condition: AsNeeded -->|
|How Much|[Constraints on the requirement specification, e.g. size, performance, etc.] <!-- condition: AsNeeded -->|
|How|[How the requester uses this requirement] <!-- condition: AsNeeded -->|
