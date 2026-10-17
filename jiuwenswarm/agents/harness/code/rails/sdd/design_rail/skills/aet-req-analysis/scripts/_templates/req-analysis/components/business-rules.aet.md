---
heading_level: 2
checklist: |
**业务规则（如有）**

1. 业务规则是否明确 (WARNING)
   - 使用“必须”、“禁止”、“需要”等明确术语

2. 规则是否可判定 (WARNING)
   - 每条规则可判定

3. 禁止描述技术实现细节 (ERROR)
   - 不得随技术栈变化而失效
---

## 业务规则 <!-- condition: Low=Skip, Medium=AsNeeded, High=Generate -->

<!-- policy: Business rules express "what business behavior the system must exhibit", prohibiting implementation methods. Each rule must be decidable and must not rely on technical implementation details. -->

<!-- instruct: This section defines the constraint rules that the system must follow in all scenarios. These rules exist independently of specific interaction processes and represent the core business logic throughout the entire system. -->

<!-- instruct: Describe the internal logic driving scenario flows, including but not limited to: state definitions and transition conditions, trigger conditions and prerequisites, mutually exclusive rules and priority rules, validation logic, permission constraints, etc. Use terms such as "must", "prohibited", and "need"; vague expressions are prohibited. -->

<!-- constraint: Business rules must use decidable language. Prohibit non-decisive temporal terms: "立即", "快速", "及时", "适当". Each rule must specify clear trigger conditions and measurable judgment criteria. -->

|编号|描述|原因|影响范围|
|-|-|-|-|
|BC-001|[What has changed, what it was before, what it is now]|[Why backward compatibility cannot be maintained]|[Roles affected]|
