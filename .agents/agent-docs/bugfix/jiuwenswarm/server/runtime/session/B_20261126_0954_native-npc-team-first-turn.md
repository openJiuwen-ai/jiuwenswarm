---
id: B_20261126_0954_native-npc-team-first-turn
title: 原生 NPC 队伍首轮发言失败
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/server/runtime/session
files:
  - jiuwenswarm/server/runtime/session/execution_settings.py
  - tests/unit_tests/server/test_execution_settings.py
designs: []
design_note: 修复现有原生 NPC Team 装配契约。
---

# 原生 NPC 队伍首轮发言失败

## 现象与根因

在 `releases/0.2.10` 的本地 WorkSwarm 启动篝火狼人 `1.0.2` 时，开局可以创建 `team.code.normal` 会话。第一次调用 NPC 时，团队装配将原生 NPC spec 当成常规代码 Team 初始化工具执行环境。原生 NPC spec 的 `build_context` 为空，写入 `execution_settings_managed` 时抛出 `AttributeError`。游戏随后使用规则兜底。

## 修复

原生 NPC Team 跳过普通代码 Team 的执行环境初始化。请求开启独立 Git 身份或设置非空工具执行环境时，返回配置错误。常规 Team 继续保留既有执行设置。见 [原生团队 NPC 约定](../../../../../specs/jiuwenswarm/server/runtime/agent_adapter/S_02_app-role-npc-team.md#原生团队-npc)。

## 回归证据

- Windows 项目 `.venv` 下运行 `python -m pytest -o addopts='' -q tests/unit_tests/server/test_execution_settings.py -k preparation`，4 个定向用例通过。
- 此前在本地 0.2.10 验证相同的 NPC Team 修复时，篝火狼人实测开局，首夜历史记录确认两位队员分别产生 `chat.final`。本次未重新启动服务或重跑实局。
