---
heading_level: 2
checklist: |
**可用性 / 可靠性**

1. 方案决策是否具体 (WARNING)
   - 具体措施如"超时 300ms + 最大重试 1 次 + 熔断降级"

**性能**

1. 指标是否继承自需求分析 (INFO)
   - 关键操作响应时间等指标与需求分析说明书一致

2. 测量条件是否明确 (WARNING)
   - 并发条件、数据规模等已说明

3. 模块分解是否合理 (WARNING)
   - 系统级目标分解到模块/链路节点，有时间分配和分解依据

**安全性**

1. 应对策略是否具体 (WARNING)
   - 明确的风险分析和应对措施

**其他**

1. 其他DFx设计是否合理 (WARNING)
   - 应对策略与取舍合理
---

## DFx 设计

<!-- guideline: Content described in DFx must be reflected in key design sections (e.g., core process, module design) -->

### 可用性 / 可靠性 <!-- condition: generate only if omission would cause ambiguity -->

|故障/风险场景|触发|应对策略|取舍/决策|
|-|-|-|-|
|[]|[Trigger]|[strategy, recovery criteria]|[]|

### 性能 <!-- condition: generate only if omission would cause ambiguity -->

<!-- guideline: Decompose system-level performance targets to modules or link nodes. Performance values must be inherited from IR. When no source exists, emit a `[[PH:...]]` token; -->

|指标|目标值|模块分解|分解假设|备注|
|-|-|-|-|-|
|[Key operation] 响应时间|`[[PH:perf_p95 | rec:≤500ms | why:行业通用 Web P95 SLA 基线]]`|[Module A: `[[PH:perf_p95_a]]` / Module B: `[[PH:perf_p95_b]]`]|[1,2,...]|[N/A or 测量条件etc]|

**优化措施**：

|关注点|应对策略|取舍/决策|
|-|-|-|
|[]|[]|[]|

### 安全性 <!-- condition: generate only if omission would cause ambiguity -->

<!-- condition: 仅在涉及高风险模块时生成 -->

|高风险项|类型|风险分析|应对策略|
|-|-|-|-|
|[]|[数据保护/依赖安全/日志审计/授权认证/etc]|-|-|

### 其他 <!-- condition: generate only if omission would cause ambiguity -->

|目标|类型|应对策略|取舍/决策|
|-|-|-|-|
|[]|[易用性/可扩展性/可测试性/可维护性/可升级性/etc]|-|-|