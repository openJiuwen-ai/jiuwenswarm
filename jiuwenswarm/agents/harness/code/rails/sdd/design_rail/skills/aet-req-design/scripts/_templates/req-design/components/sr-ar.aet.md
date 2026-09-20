---
heading_level: 2
checklist: |
**SR 列表**

1. SR 编号是否规范 (WARNING)
   - 格式 `SR.[IR number].[sequence]`，关联 IR 编号

2. SR 数量是否合理 (ERROR)
   - 一般 1-2 个

3. SR理解是否正确 (WARNING)
   - SR为系统为实现特定系统特性而必须满足的所有可验证需求

**AR 分配**

1. AR 编号是否规范 (WARNING)
   - 格式 `AR.[SR number].[sequence]`，关联 SR 编号

2. 每个 SR 的 AR 数量是否合理 (ERROR)
   - 默认 1-2 个，无必要不超3个

3. 一 AR 一元素原则 (ERROR)
   - 每个 AR 属于且仅属于一个系统元素（模块/组件/服务）

4. IR-SR─AR追踪是否完整 (WARNING)
   - 每个IR实现无遗漏
   - 避免务必要的过渡设计（无IR支撑）

4. 系统元素是否明确 (ERROR)
   - 聚焦于单个开发团队可实现的范围

5. 严格聚焦于实现层面的责任 (ERROR)
---
## SR-AR 分解

<!--guideline:
### SR (System Requirement)
**Definition:**  
Concrete requirements that support System Features. They form the complete, externally visible, and testable requirement set of the system. This includes both customer-facing needs and internal constraints or capability requirements that reflect competitiveness.
**Characteristics:**

- Represent major capabilities required to solve customer problems (challenges, opportunities, strategies, pain points).
- Provide end-to-end solutions that deliver specific business value.
- Form the core selling points of the product package.
**Essence:**  
All verifiable requirements the system **MUST** satisfy to realize a specific system feature, including:
- Functional Requirements
  - Clearly define what the system **MUST** do.
  - Scenario-based and testable.
  - May describe external or internal system behaviors.
- Non-Functional Requirements, including, but not limited to:
  - Performance (response time, throughput)
  - Cost objectives (cost reduction targets)
  - DFX (usability, security, testability, etc.)
  - Technical constraints and limitations
  - Performance indicators (e.g., memory size, processing capability)
  All SRs **MUST** be testable and verifiable.
### AR (Allocated Requirement)
**Definition:**  
Requirements decomposed from SR and allocated to specific subsystems, modules, or development teams.
**Essence:**
- Organizational-level decomposition of SR.
- Focused on the scope that a single development team can implement.
**Characteristics:**
- Describe specific functional or performance requirements of a module.
- Clearly define interfaces, resource usage, and constraints.
- **NEVER** restate system-level business value.
- Focus strictly on implementation-level obligations.
-->

### SR 列表

|SR编号|名称|描述|覆盖|
|-|-|-|-|
|SR.[IR number].001|[]|[1-2 sentence description]|[Why it covers the intent of the IR]|

### AR 分配

#### SR.[IR number].001：[SR name]

|AR编号|名称|系统元素|操作类型|描述|
|-|-|-|-|-|
|AR.[SR number].001|[]|[]|新增/扩展/依赖|[]|