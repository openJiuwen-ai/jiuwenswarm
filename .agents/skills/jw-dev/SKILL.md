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
   遵循现有 Python 环境与编码规范，不运行格式化工具或静态检查。本分支没有根 Makefile，也不使用 `make check` 或 `make format`。
3. 契约变化写入受影响模块的 `docs/specs/`；写法见
   [spec-template.md](references/spec-template.md)，命名以该模块 `docs/AGENTS.md` 为准。spec 描述当前有效约束，不积累进度日志。
4. 一次连贯特性在该模块 `docs/features/F_NN_<slug>.md` 记录实际实现、实际决策及 why / why-not、设计偏差、验证和遗留；
   链接相关 spec 和设计叙事，不复制整篇方案，不把开发结果写回 spec 当作已确认行为。
   根因涉及契约或定位过程值得复用的修复，在该模块 `docs/bugfix/B_NN_<slug>.md` 留存现象、根因和证据。
   可复制 [feature.md](assets/templates/feature.md)；详细结构及开发文档记忆规则见 [development-memory.md](references/development-memory.md)。
   修改应用 Harness、服务或接入层时另读 [swarm-documentation.md](references/swarm-documentation.md)，按实际目录维护契约和消费方。
5. 读到文档与代码不一致时，核实后修订当前 spec / 记忆，明确设计意图、实际实现和验证结果。
   归档设计的方向不重写；偏差写在开发记录，方向变化交回设计阶段。

## 决策归属、行为证据与故障复用

- 记录实施前按 [决策归属与替代](../jw-design/references/decision-records.md) 判断续写或新建，明确旧结论的效力；不重写历史验证事实。
- 按 [行为验证与证据](references/behavior-evidence.md) 选择实际入口、模型/用户可见结果和独立副作用验证；快照不能自己生成自己的正确性依据。
- 证据写入已有实施/故障记录，注明候选、环境、覆盖及重验条件；只重验受后续变化影响的范围，不把旧版本通过直接当成新版本通过。
- 有复用价值的根因按 [防错模式提炼](references/defensive-patterns.md) 更新所属 AGENTS/spec 与回归；不为普通修复机械新增模式文档。

按 [因果流程与资料可信度](references/context-and-flows.md) 选择上下文、追踪跨边界影响，
资料缺失时直接从源码和现有证据推进，不先建设全仓知识库。验证后同步本次影响的规则、契约和已有模块/流程资料，
交接明确盘点、理解、检视与验证各自的覆盖和仍待完成部分。

## 命名

- 新 feature：该模块 `docs/features/F_NN_<slug>.md`。
- 新 bugfix：该模块 `docs/bugfix/B_NN_<slug>.md`。
- `NN` 为该目录内下一位两位序号，创建后修订不改名。日期写在元信息表，文件名不带日期。
- 重名时确认是否续写同一记录，否则细化主题，禁止覆盖。已有编号和文件名不调整。
- 每个事实保留一个维护位置，其余文档以路径链接引用，避免复制当前契约与进度。

## 设计交接与归档

流转见 [lifecycle.md](../jw-design/references/lifecycle.md)，不重复定义，也不在目录之间搬移设计文件。
随实现把实际决策、偏差、验证证据和遗留补进同一份 feature。
spec 和设计叙事只保留当前契约与长期说明，不写入实际执行结果。
目标方案需要变更时返回 jw-design，经确认后按日期更新 spec 或设计叙事，不把意外实现自动写成已确认方案。
feature 的写法还可参考 [feature-design-template.md](../jw-design/references/feature-design-template.md)
中明确标注供开发阶段使用的证据、偏差与归档指导；设计正文唯一模板不作为实施归档模板重复复制。

完成后交给检视阶段：变更范围、设计/spec 依据、测试证据、已知风险、未完成项。
需要转测文档时，将实际实现版本、范围和测试证据交给 [jw-testing](../jw-testing/SKILL.md)；
转测资料的撰写与状态维护由该 skill 负责，开发阶段继续维护实现和测试本身。
遵循模块提交组织要求，先展示具体提交计划并取得用户确认；不自动提交或创建 PR。

## 文档元数据

遵循 [模块文档约定](../jw-design/references/document-metadata.md)。检视只报告缺口，不改写待审资料。
新故障记录可复制 [bugfix.md](assets/templates/bugfix.md)。
