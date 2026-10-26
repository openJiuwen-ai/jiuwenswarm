---
id: D-YYYYMMDD-HHMM-<slug> # Asia/Singapore minute timestamp plus topic; preserve legacy IDs
title: <转测文档标题>      # 与下方 H1 保持一致
slug: <kebab-slug>        # 必须等于文件夹名的 slug 段（小写、连字符）
status: draft             # draft|in-review|in-testing|completed|failed|rejected
stage: testing            # Independent testing document stage, outside the design lifecycle
owner: "@your-handle"     # 单一负责人（DRI）
reviewers: []             # Fill actual reviewers when review starts; never invent approval
created: YYYY-MM-DD
updated: YYYY-MM-DD
version: 1                # 每完成一轮评审 +1
domain: core              # memory|scheduler|messaging|protocol|harness|security|observability|core|tooling
design-id: D-YYYYMMDD-HHMM-<slug>       # 关联的设计文档 id
test-type: functional     # functional|integration|e2e|performance|security|regression
tags: []
related: []              # Related designs; both D-prefixed and legacy SDD IDs are supported
# ↓↓↓ 进入测试 / 完成时按生命周期补充 ↓↓↓
# approved-date: YYYY-MM-DD       # status 变 in-testing 时
# completed-date: YYYY-MM-DD      # status 变 completed 时
# implementation:                 # 关联实现的代码定位
#   status: not-started           # not-started|in-progress|shipped|partially-shipped|abandoned
#   repos: []                     # 实现所在仓库名 / URL 列表
#   prs: []                       # 完整 PR URL 列表
#   commits: []                   # commit URL 或 repo@sha
#   tracking:                     # 开发仓 issue / epic URL
---

# <转测文档标题>

## 1. 转测范围

<本次转测的功能范围，关联的设计文档 id（design-id）。说明哪些功能点纳入转测、哪些排除。>

## 2. 测试环境

<测试环境配置、依赖服务、数据准备等。包括：
- 环境类型（开发 / 预发 / 生产灰度）
- 依赖服务版本
- 测试数据准备方式
- 特殊配置项>

## 3. 测试策略

<根据实际范围选择测试方法、测试层级和覆盖要求，不自行设定统一指标。包括：
- 测试层级（单元 / 集成 / E2E）
- 测试方法（黑盒 / 白盒 / 灰盒）
- 覆盖率要求
- 自动化测试比例>

## 4. 测试用例

### 4.1 功能测试

| 用例编号 | 用例描述 | 前置条件 | 操作步骤 | 预期结果 | 优先级 |
|---|---|---|---|---|---|
| TC-001 | <描述> | <条件> | <步骤> | <预期> | P0/P1/P2/P3 |

### 4.2 非功能测试

<性能、安全、兼容性等测试用例。若无则注明「不适用」并说明理由。>

### 4.3 边界与异常测试

<边界值、异常输入、容错等测试用例。>

## 5. 测试结果

<测试执行汇总。在 status 变为 completed 时填写。包括：
- 执行用例总数 / 通过数 / 失败数 / 阻塞数
- 通过率
- 测试执行时间范围>

### 证据适用范围与重验条件

<引用实际证据对应的候选版本、环境/依赖、真实入口、记录路径和已证明范围。
模型/用户可见输出与文件/数据库/进程副作用分别列证据；回放注明模拟边界和独立预期来源。
说明哪些代码、配置、协议、fixture 或环境变化使结论需要重验；未执行或未核实的新候选填写待重验。>

### 覆盖与资料可信度

<分别说明资料/用例已盘点、链路已理解、候选已检视及场景已执行验证的范围。
引用资料版本，列出推断、待核实/待重验、排除项与理由；关键链路或环境缺口对应具体后续入口，不用已整理用例数代替实际通过结果。>

## 6. 缺陷清单

<测试过程中发现的缺陷。>

| 缺陷编号 | 严重程度 | 描述 | 状态 | 关联用例 |
|---|---|---|---|---|
| BUG-001 | 致命/严重/一般/提示 | <描述> | 新建/修复中/已修复/延期 | <用例编号> |

## 7. 风险与遗留

<测试过程中发现的风险、遗留问题及后续计划。>

## 8. 转测结论

<转测是否通过，签字确认。在 status 变为 completed 或 failed 时填写。>

- **结论：** 待验证 / 通过 / 不通过 / 有条件通过
- **条件（如有）：** <需满足的附加条件>
- **确认依据：** <实际确认人、日期及记录；尚未确认时填待确认>

## 9. 评审记录 (Iteration Log)

<每完成一轮评审追加一行，与 frontmatter 的 version 同步。>

| 轮次 | 日期 | 版本 | 评审人 | 结论 | 主要修改/讨论 |
|---|---|---|---|---|---|
| 1 | YYYY-MM-DD | 1 | @reviewer | changes-requested | <要点> |

## 10. 参考与关联设计

<按完整标识引用关联设计（D-日期-时分-主题或历史 SDD）、spec、feature、实际实现版本和证据。>
