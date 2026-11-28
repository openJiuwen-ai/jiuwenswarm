---
id: B_20261128_1105_npc-team-attachment-mode
title: 原生 NPC Team 只读订阅模式错配
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/runtime
files:
  - jiuwenswarm/runtime/service.py
  - tests/unit_tests/runtime/test_runtime_service.py
designs: []
design_note: 修复现有原生 NPC Team 的只读观察契约。
---

# 原生 NPC Team 只读订阅模式错配

## 现象与根因

0.2.10 的 Swarm 主界面切换到「军师」时，现场请求 `req_mwyqb559_4628` 发送了空的 `chat.send`，携带 `attach_only:true` 和 `mode:"team"`。Runtime 用代码工作模式组合出 `code.team`，与会话保存的 `team.code.normal` 不匹配。NPC 模式校验返回 `the NPC Team mode is fixed at session creation`，订阅结束。现场证据见 [Issue #7263](https://gitcode.com/openJiuwen/jiuwenswarm/issues/7263)。

## 修复决策

Runtime 在分类和派发前，为原生 NPC Team 的空订阅沿用会话元数据中的模式和工作模式。历史会话缺少工作模式时，从已保存的会话模式推断。带正文、文件或媒体的请求继续走现有模式校验。把兼容逻辑放在宿主侧可覆盖已经发布的 0.2.10 客户端。约定见 [NPC 会话标识](../../../specs/jiuwenswarm/server/runtime/agent_adapter/S_02_app-role-npc-team.md#npc-会话标识)。

## 回归证据

- 在 Windows 的项目 `.venv` 运行 `python -m pytest -o addopts= --asyncio-mode=auto --tb=short -q tests/unit_tests/runtime/test_runtime_service.py -k "npc or team_attach_only_registers or stale_team_attachment"`。修复前两个模式继承用例均得到 `code.team`。发布分支修复后 15 项通过，涵盖旧请求、缺少工作模式的会话、带输入的模式校验和只读订阅控制。
- `develop` 修复分支通过移植差异核对。按任务要求，运行验证限于发布修复分支。现场 Electron 界面未做端到端复测。
