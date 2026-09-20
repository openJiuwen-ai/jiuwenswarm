---
heading_level: 2
checklist: |
**主成功场景**

1. PlantUML 流程图是否绘制 (WARNING)
   - 使用标准 PlantUML泳道语法
   - 流程完整：触发 → 操作 → 系统响应 → 结果生成 → 目标完成

2. 场景流程是否完整 (ERROR)
   - 覆盖核心操作流程
   - 用户与系统的交互清晰

**场景列表**

1. 场景路径是否完整 (ERROR)
   - Effort=High时，主成功、备选、异常路径均已覆盖

2. 备选路径是否识别 (WARNING)
   - Effort=Medium/High时需检查
   - 分析需求中是否存在不同条件下的分支行为被遗漏
   - 用户易忽略的场景已覆盖

3. 异常路径是否全面 (WARNING)
   - Effort=Medium/High时需检查
   - 关键边界条件和错误处理已识别
   - 从不限于以下角度检查缺失：外部依赖失败、非法输入、并发冲突、资源受限

4. 简要说明是否清晰 (INFO)
   - 触发条件和预期结果已说明
---

## 场景分析


**主成功场景**

```plantuml
@startuml
|用户|
start
:执行核心操作;
|系统|
:处理请求;
:返回成功结果;
|用户|
:完成目标;
stop
@enduml
```

<!-- guideline: When designing error and exception paths, examine these commonly overlooked scenarios: external dependency failures (network timeout, service unavailable), invalid inputs (out-of-range, malformed), concurrency conflicts (simultaneous data modification), resource exhaustion (file size exceeded, connection pool depletion), dynamic resource changes (hot-plug, online/offline), task/data migration to new locations, mid-operation failures (network/server errors), extreme-scale inputs beyond NFR-defined norms, and privilege-exempt entities that bypass regular constraints (e.g., RT tasks, kernel threads). -->

|编号|路径|类别|触发|步骤|
|-|-|-|-|-|
|S-001|[主成功/扩展<!-- condition: Low=AsNeeded, Medium=AsNeeded, High=Generate -->/备选<!-- condition: Low=AsNeeded, Medium=AsNeeded, High=Generate -->/异常<!-- condition: Low=AsNeeded, Medium=AsNeeded, High=Generate -->]|[业务/操作/维护...]|[Trigger conditions]|[]|