---
heading_level: 2
checklist: |
已按相同格式改造完成：

**功能性需求**

1. 优先级是否合理 (WARNING)
   - P0/P1/P2 标注清晰
   - 核心主流程为 P0，效率辅助为 P1，体验优化为 P2

2. 需求描述是否完整 (WARNING)
   - 2-3句总结核心行为、用户价值、包含的子能力

3. 是否避免设计决策 (ERROR)
   - 仅描述需求，不涉及实现方案
   - 不由技术栈变化而失效
   - 判别尺度由是否容易引入过度约束限制后续设计判断
   - 不得矫枉过正影响需求描述清晰度，或做无必要抽象

4. 是否覆盖隐含能力 (WARNING)
   - 分析场景中隐含的系统能力，判断是否有关键遗漏
   - 逐一审视场景步骤，问"在此步骤中，如果 XX 发生了，系统怎么处理？"
   - 
**非性能需求（如有）**

1. 是否有量化验收标准 (WARNING)
   - 描述包含可量化的验收标准：响应时间、可用率、错误率、恢复时间等

2. Effort=High时，是否覆盖关键质量属性 (WARNING)
   - 可用&可靠性、性能

3. 是否合理全面 (WARNING)
   - 分析需求涉及的DFx属性，判断是否有遗漏或不合理之处

---

## 需求列表

<!-- policy: NEVER make design decisions. Breaking down functional requirements to the implementation level restricts subsequent design flexibility and makes it difficult to adapt to user-driven changes. -->

### 功能性需求

<!-- policy: FR must describe "what business capability the system must provide (who achieves what purpose through what means)", prohibiting implementation methods. -->

<!-- example (GOOD vs BAD):
  - GOOD: "System must provide configurable BE sibling CPU throttle ratio leveraging Kunpeng hardware capability."
  - BAD: "Throttle ratio configured via kernel parameter/sysfs" / "Via Kunpeng dedicated register XX."
-->

<!-- guideline: Can describe core functions by P0 / P1 / P2 levels; core main flow can be P0, efficiency-enhancing auxiliary capabilities can be P1, experience optimization items can be P2. -->

<!-- instruct: Each requirement description can use 2-3 sentences to summarize core behavior, user value, and included sub-capabilities. -->

<!-- instruct: After FR completion, review each scenario in the scenario list to confirm that every scenario's capability requirements are covered by FRs. Ask: what implicit system capabilities (error handling, boundary cleanup, concurrency protection) do these scenarios require? -->

|编号|类别|名称|描述|优先级|
|-|-|-|-|-|
|FR-001|[Function]|[Name]|[Description]|[P0/P1/P2]|

### 非功能性需求 <!-- condition: Low=AsNeeded, Medium=AsNeeded, High=Generate -->

<!-- instruct: Can supplement from availability, reliability, performance, serviceability, security, scalability, compatibility perspectives. Description should include quantifiable acceptance criteria, e.g. response time, availability rate, error rate, recovery time, etc. -->

<!-- constraint: When Effort=Medium/High, performance & availability must evaluate -->

<!-- constraint: UNSOURCED quantitative values must use the `[[PH:...]]` placeholder token. -->
<!-- Example: `[[PH:perf_p95 | rec:≤500ms | why:行业通用 Web P95 SLA 基线 | src:pending]]` -->

|编号|类别|名称|描述|优先级|
|-|-|-|-|-|
|NFR-001|[]|[]|[Description (including quantifiable acceptance criteria)]|[]|
