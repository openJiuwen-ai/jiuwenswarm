---
id: B_20261126_1915_agent-observability-threading-import
title: agent_observability 重写丢失 threading 导入致 CI 大面积红
type: B
author: gunner_beat
author_email: gunner_heart@163.com
designs: []
design_note: 9340c6526（!10852）重写 agent_observability.py 时误删 import threading，本修复仅恢复导入，无契约变化、无独立设计。
modules:
  - jiuwenswarm/agents/harness
files:
  - jiuwenswarm/agents/harness/agent_observability.py
---

# agent_observability 缺失 threading 导入的回归修复

## 现象与影响

2026-11-26 14:31 合入的 9340c6526（!10852 "unify tracing control and decouple trajectory storage"）重写 `jiuwenswarm/agents/harness/agent_observability.py` 时删掉了 `import threading`，但模块级 `_trace_turns_lock = threading.Lock()`（:147）仍在——模块导入即抛 `NameError: name 'threading' is not defined`。

该模块位于 harness import 闭包的枢纽位置，`interface_deep` / `interface_code` / `BrowserTaskPromptRail` 等入口全部级联失败：同窗所有含该基线的 PR CI 的 UT 大面积红（!11035 实测报告 372 Error + 264 Failed，失败用例的根因聚类均为该 NameError；!11029/!11030/!11032/!11034/!11036/!11039 同窗同红）。上游 !11027 曾提交同款修复（"restore threading import"）但被关闭未合入，破损持续。

## 根因与修复决策

diff 复核：9340c6526 对该文件的改动含 `-import threading` 与 `-_state_lock = threading.RLock()`（旧锁移除），但新实现引入的 `_trace_turns_lock` 仍引用 threading——属重写时删多了一行导入。修复仅恢复 `import threading`（按既有 import 区字母序插回 logging 与 time 之间），不触碰任何行为。

## 回归证据与适用范围

- 红：修复前 `python -c "import jiuwenswarm.agents.harness.agent_observability"` 抛 `NameError: name 'threading' is not defined`（与 CI 失败日志根因一致）；
- 绿：修复后同一命令 import 成功且 `_trace_turns_lock` 为正常 `threading.Lock` 实例；
- CI 侧预期：636 个级联失败用例全部转绿（失败聚类单一根因）。

适用范围：单文件一行导入恢复；无契约、无行为变化。本地完整 pytest 受 venv 依赖版本落后（openjiuwen/agent_gateway）限制，未在本机复跑全量，以根因模块红绿 + CI 报告聚类为验证依据。
