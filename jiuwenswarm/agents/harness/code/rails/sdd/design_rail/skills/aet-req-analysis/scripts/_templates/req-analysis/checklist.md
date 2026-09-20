# 需求分析检查表

本检查表用于评审需求分析说明书的质量。

**评审准则**：文档是组件的全量功能规格，是**系统应该表现出什么业务行为**，是业务规则、数据约束、功能需求、非功能需求的集合，而不是采用何种技术手段实现业务行为。

---

## 检查项

**元数据**

1. 元数据是否完整 (INFO)

- 包含name, type, description, change, effort, version, base_commit, update_time等基本信息

2. 描述是否具体 (WARNING)

- 一两句话描述需求核心内容，避免模糊表达

3. 需求难度判断合理 (WARNING)

- 根据需求复杂度、涉及系统数量、变更范围等因素合理判断难度

{{basic-information.aet}}

{{scenario-analysis.aet}}

{{business-rules.aet}}

{{data-constraints.aet}}

{{requirements-list.aet}}

{{acceptance-plan.aet}}

**整体**

1. 内容无冲突、重复 (ERROR)

- 检查文档内容是否存在自相矛盾或重复描述

2. 编号规范 (INFO)

- 编号按顺序排列，无重复

**其他**

> 酌情添加其他满足**评审准则**的关键评审项。
