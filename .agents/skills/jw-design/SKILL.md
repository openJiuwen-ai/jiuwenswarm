---
name: jw-design
description: 在 JiuwenSwarm 项目中分析需求、制定架构或特性方案、编写与评审设计文档，形成可交给开发阶段的已确认方案。适用于设计阶段；实现、测试和开发记忆维护交给 jw-dev。
---

# jw-design

设计资产根目录为 `.agents/agent-docs/designs/`，所有路径均相对 JiuwenSwarm 仓库根目录。
先阅读相关模块的代码、测试、spec 和已有设计，区分当前行为、目标方案与待确认假设。

## 工作流程

1. 从具体场景说明问题、目标、范围和非目标，确认影响的模块及契约。
2. 根据任务选择文档，不强制每次都写齐所有类型：
   - 整体定位与工作链路：[overview-template.md](references/overview-template.md)。
   - 模块职责、协作与系统约束：[architecture-template.md](references/architecture-template.md)。
   - 一次行为变更的详细方案与取舍：[feature-design-template.md](references/feature-design-template.md)。
3. 需要独立评审与阶段跟踪的设计，复制唯一模板 [design.md](assets/templates/design.md)。
   它整合了设计管理字段、详细方案与日期评审记录；reference 仅提供指导，不再包含第二套可复制正文。
   负责人、评审人、状态、日期等管理字段统一保留在设计 frontmatter。
4. 评估主要备选方案，说明选择依据、失败路径、兼容与迁移影响、验证方案及未决问题。
   成稿时按 [writing-guide.md](references/writing-guide.md) 核对表达与事实边界。
5. 根据反馈迭代；只有用户或实际评审者明确确认后才记录批准。不得自行填写审批结论或评审人。
6. 交接已确认的目标、设计标识、受影响契约、验收场景、待决问题给 `jw-dev`。
   用户只要求设计时，在交接处停止，不自动进入实现。

## 命名与生命周期

- 新设计目录及 `id`：`D-YYYYMMDD-HHMM-<slug>`，例如 `D-20261021-1530-message-routing`。
  时间使用 Asia/Singapore，精确到分钟；slug 为小写英文短横线主题，属于完整标识的一部分。
  完整名称存在时，先辨认是同一设计的续写还是不同设计；不同设计使用更具体的 slug，不覆盖、不分配递增号。
- 每目录含 `design.md` 和可选 `assets/`，初始位置是 `designs/proposals/`。
- `modules` 填相关模块目录列表，保留仓库相对路径，例如 `[jiuwenswarm/agents, jiuwenswarm/server/runtime]`；
  跨仓模块保留仓库前缀。INDEX 按模块分组，模块内展示生命周期。跨模块可在多个分组检索，
  缺失时进入“未分类”；`domain` 仍表示领域，不代替模块目录。
- 已有 SDD 编号及历史文件名保持不变。`REGISTRY.md` 仅是历史台账，新设计不登记、不取号。
- 本阶段管理 `draft → in-review ⇄ changes-requested → approved / rejected`。
  评审与设计修订按实际日期追加记录并更新 `updated`，不维护递增 `version`；历史版本字段保留兼容。
- 设计文档是开发前的方案资产，仅包含设计与验证计划。实际实施、决策、偏差、验证结果和遗留
  由 [jw-dev](../jw-dev/SKILL.md) 归档至 features，不回填设计正文或 `implementation` 字段。
- 阶段、状态及字段见 [lifecycle.md](references/lifecycle.md)；开发阶段可维护设计的迁移与归档管理字段，
  目标方案变化需返回设计阶段确认后按日期修订。
- 草稿和被否决设计不是生产实现依据；已归档设计属于历史证据，当前行为仍须核对代码和 specs。
  按稳定标识引用设计，通过索引定位当前路径。

## 索引工具

从 JiuwenSwarm 根目录激活 `.venv` 并设置 `PYTHONPATH` 后执行：

```bash
python .agents/skills/jw-design/scripts/generate_index.py --root .agents/agent-docs/designs
```

工具依赖 PyYAML，复用 JiuwenSwarm 环境，不创建独立虚拟环境。迁入的 `validate_frontmatter.py` 为可选文档工具，
不作为流程门禁；仓库禁止静态检查时不执行。旧格式资料仍保留原路径，见 designs 的 README。
遵循仓库的提交与权限规则，设计确认不授权提交或创建 PR。
