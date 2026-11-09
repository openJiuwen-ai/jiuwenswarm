---
name: jw-design
description: 在 JiuwenSwarm 项目中分析需求、制定架构或特性方案、编写与评审设计文档，形成可交给开发阶段的已确认方案。适用于设计阶段；实现、测试和开发记忆维护交给 jw-dev。
---

# jw-design

文档写在受影响模块的 `docs/`，路径相对仓库根目录。存放和命名见 [模块文档约定](references/document-metadata.md)。
先阅读该模块的 `AGENTS.md`、`docs/AGENTS.md`、代码、测试、spec 和已有设计叙事，区分当前行为、目标方案与待确认假设。

## 工作流程

1. 从具体场景说明问题、目标、范围和非目标，确认影响的模块及契约。
2. 根据任务选择文档，不强制每次都写齐所有类型：
   - 整体定位与工作链路：[overview-template.md](references/overview-template.md)。
   - 模块职责、协作与系统约束：[architecture-template.md](references/architecture-template.md)。
   - 一次行为变更的详细方案与取舍：[feature-design-template.md](references/feature-design-template.md)。
3. 需要展开方案时，用 [design.md](assets/templates/design.md) 组织正文。
   长期架构或概览写入该模块 `docs/designs/` 的描述性文件；一次特性的取舍写入 `docs/features/F_NN_<slug>.md`。
   确认人、状态和日期写在正文，未经真实确认不填写批准。
4. 评估主要备选方案，说明选择依据、失败路径、兼容与迁移影响、验证方案及未决问题。
   成稿时按 [writing-guide.md](references/writing-guide.md) 核对表达与事实边界。
5. 根据反馈迭代；只有用户或实际评审者明确确认后才记录批准。不得自行填写审批结论或评审人。
6. 交接已确认的目标、设计标识、受影响契约、验收场景、待决问题给 `jw-dev`。
   用户只要求设计时，在交接处停止，不自动进入实现。

## 决策维护与验证交接

新建或修订方案前按 [决策归属与替代](references/decision-records.md) 查找已有决定，
说明本次是续写、独立新决定还是替代；真实备选及代价据实记录，不为套模板虚构方案。
验收条件覆盖受影响的模型/用户可见结果、实际入口与副作用；验证选择参考
[行为证据](../jw-dev/references/behavior-evidence.md)，此阶段只定义预期与通过条件，实际执行归开发记录。
设计交接同时指出旧决定仍有效的范围及需核实的消费者。

对跨边界方案按 [因果流程与资料可信度](../jw-dev/references/context-and-flows.md) 核对产生方、状态权威、载体、消费/恢复与最终结果。
既有项目地图只作导航；方案依据区分已证实、推断与待核实，明确未追通的关键关系，不以设计稿完整替代实现或验证证据。

## 命名与生命周期

- spec、feature、bugfix、设计叙事的文件名见 [模块文档约定](references/document-metadata.md)。
  序号在该模块对应目录内递增，不覆盖已有文件。
- 契约变更改 `docs/specs/S_NN_<slug>.md`。长期架构或概览改 `docs/designs/` 里的描述性文件。
  一次特性的取舍、拒绝的方案和验证计划写进 `docs/features/F_NN_<slug>.md`。
- 评审按实际日期记在正文里。只有用户或实际评审者明确确认后才记录批准。
- 方案正文保留开发前的目标和验证计划。实际实施、偏差、验证结果和遗留由 [jw-dev](../jw-dev/SKILL.md)
  补进同一份 feature，不把开发事实写回 spec 当作已经发生的行为。
- 流转见 [lifecycle.md](references/lifecycle.md)。目标方案变化需返回设计阶段，确认后再改 spec 或 designs。
- 尚未确认的方案不是实现依据。已有设计叙事是历史说明，当前行为仍须核对代码和 specs。

遵循仓库的提交与权限规则，设计确认不授权提交或创建 PR。不建中央索引，不运行文档同步脚本。

## 文档元数据

遵循 [模块文档约定](references/document-metadata.md)。检视只报告缺口，不改写待审资料。
