---
id: B_20261119_1547_daily-report-todo-collection
title: 日报技能的待办采集始终为空
type: B
author: liujiayu75
author_email: liujiayu29@huawei.com
modules:
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/advanced-daily-report
files:
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/advanced-daily-report/collectors/todo_collector.py
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/advanced-daily-report/run_report.py
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/advanced-daily-report/SKILL.md
  - tests/unit_tests/resources/test_advanced_daily_report_skill.py
  - docs/zh/开发实践/JiuwenSwarm日报生成器开发实践.md
  - docs/en/development-practices/JiuwenSwarm-Daily-Report-Generator.md
designs: []
design_note: 采集路径与现行存储契约不一致导致的实现缺陷；按 workspace todo.json 契约修复，无独立设计。
---

# 日报技能的待办采集始终为空

## 现象

生成日报时「已完成任务 / 进行中 / 待处理」恒为 0，即使会话里刚通过 todo 工具记录过任务。
实测环境同时满足：

- `~/.jiuwenswarm/agent/workspace/todo/` 下有 4 个 `todo.json`；
- `~/.jiuwenswarm/agent/sessions/` 下 `todo.md` 数量为 0。

## 根因

任务存储已切到 DeepAgent workspace：

- 默认：`{agent_root}/workspace/todo/{session_id}/todo.json`；
- 单 Agent code 且锁定 project_dir：`{project_dir}/todo/{session_id}/todo.json`；
- 状态值为 `pending` / `in_progress` / `completed` / `cancelled`，契约见
  `jiuwenswarm/common/todo_snapshot.py` 与 `docs/zh/任务规划.md`。

日报技能仍按旧布局采集：

1. `run_report.py` 只 `rglob("todo.md")` 扫描 `{agent_root}/sessions`，且只解析 `- [x]` / `- [ ]`；
2. `collectors/todo_collector.py` 同样只扫 `sessions/**/todo.md`，且不复用；
3. 旧 `todo.md` 解析不识别 `TodoToolkit` 写入的 `[-]`（取消）与 `| status | result` 列。

因此当前版本只要不存在旧 `todo.md`，待办采集必然返回空。

## 修复决策

按“先止血”最小化范围修复，只对齐存储路径，不改变报告结构与旧解析语义：

- `TodoCollector` 新增 `{agent_root}/workspace/todo/*/todo.json` 采集（同时兼容传入 `agent/workspace`）；
- 状态归一：`pending→waiting`、`in_progress→running`，保留 `completed` / `cancelled`，`deleted` 跳过；
- 存在 JSON 待办时不再解析旧 `todo.md`；无 JSON 时回退原有 `sessions/**/todo.md` 逻辑（仅修正 checkbox 正文提取顺序，避免正文残留 `- `）；
- `run_report.py` 改为复用 `TodoCollector`，概览「进行中」包含 `waiting` 与 `running`，报告结构不变。

## 验证

- 新增 4 项单测：`workspace/todo.json` 解析与状态归一、`run_report.py` 不再直接扫描 `todo.md`、旧 `todo.md` 回退、原有 IMAP 异常契约。
- 本机 `pytest` 不可用（`.venv` 未安装），改用独立脚本验证：JSON 输入 4 条得到 1/1/1/1，旧 md 回退得到 2 条且正文正确。

## 遗留

- 单 Agent code 锁定 project_dir 时，待办落在 `{project_dir}/todo/...`，本次未覆盖，需另开 issue。
- team 模式成员的待办位于各自 workspace，本采集只读默认 agent workspace，不在本次范围。
- 跨会话同名任务不去重，日报可能重复列出；按“先止血”原则本次不处理。
- 旧 `todo.md` 的 `[-]`、`| status` 列仍不识别；其写入器 `TodoToolkit` 已无运行时引用，暂不处理。
- `todo.json` 不记录完成时间，日报中的「已完成」按当前状态统计，不等同于「当天完成」；
  如需按天统计需在 core todo schema 增加时间字段。
- `report_helper.py` 为兼容旧版脚本，本次未调整；其 `workspace/session` 路径仍为历史实现。
