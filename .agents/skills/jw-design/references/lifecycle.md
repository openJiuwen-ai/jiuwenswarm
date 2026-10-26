# 设计与交付生命周期

设计资产根目录为 `.agents/agent-docs/designs/`。本文件统一定义字段与状态；
`jw-design` 负责方案形成和日期评审；`jw-dev` 将开发事实归档到 features，仅同步设计的必要管理字段。
本规则不改变用户指令或仓库的提交、PR 权限。

## 标识与文档结构

新设计目录及 frontmatter `id` 均为 `D-YYYYMMDD-HHMM-<slug>`，例如
`D-20261021-1530-message-routing`。使用 Asia/Singapore、精确到分钟，slug 为小写英文短横线主题。
同一分钟不同主题各自独立；完整标识重名时确认是否同一设计，不能覆盖、分配递增号或修改历史编号。
已有 `SDD-NNNN` 继续有效，文件名与引用保持不变，`REGISTRY.md` 仅保留旧编号台账。
按完整标识引用，索引提供当前路径，不以阶段路径作为永久标识。

每个阶段目录下的设计文件夹含 `design.md` 和可选 `assets/`。
设计文件之间标识唯一。
旧格式散落资料保留原位置，见 designs 的 `legacy-documents.md`，不伪造状态或补造历史审批。

## Frontmatter

| 字段 | 约定 |
|---|---|
| `id` | 新分钟时间标识，或历史 SDD 标识 |
| `title`、`slug` | 中文标题与英文主题；slug 对应目录主题 |
| `stage` | 相对设计资产根目录的阶段：proposals / active / archive |
| `status` | 与阶段对应的状态，见下表 |
| `owner` | 真实负责人；未知时明确待确认，不使用示例值冒充 |
| `reviewers` | 真实参与者；进入评审时填写，未知时不得冒充已评审 |
| `created`、`updated` | YYYY-MM-DD；updated 不早于 created |
| 评审日期 | 正文按 YYYY-MM-DD 记录评审主题、真实参与者、结论及修改依据；同步 updated，不再递增版本 |
| `domain` | memory / scheduler / messaging / protocol / harness / security / observability / core / tooling |
| `modules` | 相关模块目录列表，如 `[jiuwenswarm/agents, jiuwenswarm/server/runtime]`，跨仓模块使用 `openjiuwen/agent_teams` 等完整路径；索引按模块分组，跨模块设计在各组展示，未填写时进入“未分类”，不以 domain 推断模块 |
| `tags`、`related` | 可选主题标签、相关但不替代的完整设计标识 |
| `supersedes`、`superseded-by` | 可选替代关系；确认替代后双向关联 |
| `approved-date` | 实际批准日期；不能以生成文档或开始编码推断批准 |
| `archived-date` | 设计资产归档管理日期，不作为实际交付证据 |
| `scope`、`plan` | 设计范围与可选计划入口；负责人、评审人、状态及日期始终以设计为管理字段来源 |

新设计不填写 version 或 implementation，不将 PR / commit、实施结果、验证证据或交付遗留写入设计。
历史文档原有版本、日期和实现字段保留兼容，不在本次批量改写。

## 状态与阶段

| 阶段 | 合法状态 | 维护阶段 |
|---|---|---|
| proposals | draft / in-review / changes-requested / approved / rejected | jw-design |
| active | in-development / on-hold | jw-dev |
| archive | shipped / superseded / abandoned | jw-dev；方向替代交回 jw-design |

- `draft → in-review ⇄ changes-requested`，评审后进入 approved 或 rejected。
- 用户或实际评审者明确确认后记录 approved、approved-date 和评审结论。
- 实际开工后 `approved → in-development`，移动 proposals → active，同步 stage、status 与 updated；实际开工记录留在 features / 执行计划。
- `in-development ⇄ on-hold`；交付确认后进入 shipped，移动 active → archive。
- 开发终止可进入 abandoned，移动 active → archive，说明原因和未完成部分。
- 已交付方案改变方向时创建新设计，以 supersedes / superseded-by 关联；旧设计标为 superseded。
- rejected 留在 proposals，不冒充已交付归档。

移动时同步状态、stage、日期和索引；已跟踪路径使用 `git mv`，未跟踪路径使用普通移动，
这不代表已经获准提交。附件随设计移动，标识不变。

## 日期评审与开发交接

设计文档是开发前的设计资产。每次实质评审或目标方案修订按日期追加记录，说明参与者、
主题、结论、原因与受影响章节，更新 updated；同一天可有多条不同主题记录，不分配轮次或递增版本。
负责人、评审人、阶段、状态与日期保留在设计 frontmatter，计划和 features 只引用该来源。

设计确认后交给开发实施。features 是开发落地后实施与决策归档，记录实际实现、实际选择与取舍、
设计偏差、版本定位、验证执行、结果及遗留，引用稳定设计标识，不复制整篇方案，也不反向回填设计。
验证方案及通过条件属于设计，实际证据属于 features；已确认与已实现、已验证分别判断。
目标方案需要改变时返回设计阶段确认，并按日期修订；实施经过始终保留在 features。

沿用现有 proposals / active / archive 布局维护设计资产的流转管理信息；archive 不增加 implementation 必填条件。
归档设计保留当时的目标方案及评审依据，方向变化另建设计并以替代关系关联。历史版本字段不批量移除。

## 内容归属与工具

设计记录问题、方案和决策依据；specs 维护当前契约；features 记录特性落地过程；bugfix 留存故障证据。
开发计划维护依赖、进展与交付条件。相互引用，不复制多份状态或契约。
所有新文档以分钟时间标识为准，模板中的历史编号示例不构成新取号要求。

索引生成器在 `.agents/skills/jw-design/scripts/generate_index.py`，支持显式 `--root`。
复用 JiuwenSwarm 的 `.venv` 与 PyYAML，不安装钩子或增加 CI 门禁。
可选 frontmatter 工具保留为参考，不要求自动执行；禁止静态检查时不执行。
