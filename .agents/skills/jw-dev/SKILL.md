---
name: jw-dev
description: 在 JiuwenSwarm 项目中落实已确认方案，编写实现与测试，并维护 specs、features、bugfix、实现偏差和开发交接记忆。适用于开发落地阶段；新方案的设计与评审使用 jw-design。
---

# jw-dev

从已确认的设计或明确的开发任务接入，阅读目标目录的 AGENTS.md、当前代码、测试及相关 spec。
先确定验收场景、实现边界与受影响契约，再按实际依赖安排开发。
普通修复无需被强制扩成完整设计流程；出现未确认的方案选择或方向级变化时，返回 `jw-design`。

## 实现与开发记忆

1. 需要拆分开发单元、依赖或跟踪交付时，读取
   [feature-development-plan-template.md](references/feature-development-plan-template.md)。
   计划中的并行机会不是自动启动多代理的授权。
2. 实现与必要测试同步推进，运行与变更相关的行为测试，记录真实结果、未执行项及限制。
   遵循现有 Python 环境与编码规范，不运行格式化工具、静态检查、`make check` 或 `make format`。
3. 契约变化及时维护 `.agents/agent-docs/specs/<module>/`；写法见
   [spec-template.md](references/spec-template.md)。spec 描述当前有效约束，不积累进度日志。
4. 设计是开发前资产；features 是开发落地后的实施与决策归档。一次连贯特性在
   `.agents/agent-docs/features/<module>/` 记录实际实现、实际决策及 why / why-not、设计偏差、验证和遗留；
   引用稳定设计标识与相关 spec，不复制整篇设计，不向设计回填实际结果。
   根因涉及契约或定位过程值得复用的修复，在 `.agents/agent-docs/bugfix/<module>/` 留存现象、根因和证据。
   可复制 [feature.md](assets/templates/feature.md)；详细结构及开发文档记忆规则见 [development-memory.md](references/development-memory.md)。
   修改应用 Harness、服务或接入层时另读 [swarm-documentation.md](references/swarm-documentation.md)，按实际目录维护契约和消费方。
5. 读到文档与代码不一致时，核实后修订当前 spec / 记忆，明确设计意图、实际实现和验证结果。
   归档设计的方向不重写；偏差写在开发记录，方向变化交回设计阶段。

## 命名

- 新 feature：`F_YYYYMMDD_HHMM_<slug>.md`。
- 新 bugfix：`B_YYYYMMDD_HHMM_<slug>.md`。
- 时间使用 Asia/Singapore，精确到分钟，创建后修订不改名。
- 完整标识包含主题；同一分钟不同主题可并行建档。重名时确认是否续写同一记录，否则细化主题，禁止覆盖。
- 历史编号和文件名不调整；spec 沿用已有编号规则。
- 每个事实保留一个维护位置，其余文档以标识或链接引用，避免复制当前契约与进度。

## 设计交接与归档

设计生命周期和字段复用 [lifecycle.md](../jw-design/references/lifecycle.md)，不重复定义。
开始实际开发时将已批准设计移到 `designs/active/`，同步 `stage`、`status`、日期；
设计负责人、评审人、状态与日期继续以设计 frontmatter 为唯一来源；计划及 features 引用该处。
随实现将版本、实现定位、实际决策、偏差、验证证据和遗留归档到 features，
不在设计中填写 `implementation` 或实际执行章节。设计中的验证计划只定义预期与通过条件。
目标方案需要变更时返回 jw-design，经确认后按日期更新设计，不把意外实现自动写成已确认方案。
根据真实测试和交付结论更新状态；仅在交付得到确认后移入 `designs/archive/`。
迁移保留设计标识和附件，更新索引；索引命令见 `jw-design`。
feature 的写法还可参考 [feature-design-template.md](../jw-design/references/feature-design-template.md)
中明确标注供开发阶段使用的证据、偏差与归档指导；设计正文唯一模板不作为实施归档模板重复复制。

完成后交给检视阶段：变更范围、设计/spec 依据、测试证据、已知风险、未完成项。
需要转测文档时，将实际实现版本、范围和测试证据交给 [jw-testing](../jw-testing/SKILL.md)；
转测资料的撰写与状态维护由该 skill 负责，开发阶段继续维护实现和测试本身。
遵循模块提交组织要求，先展示具体提交计划并取得用户确认；不自动提交或创建 PR。
