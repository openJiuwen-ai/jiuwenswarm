---
id: B_20261103_1433_npc-session-project
title: 微缩生活岛 NPC 会话归属错误
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/extensions/app_market
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin
files:
  - jiuwenswarm/extensions/app_market/app_market/market_service.py
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md
  - tests/unit_tests/test_app_market_runtime.py
designs: []
design_note: 根据明确的故障修复要求落实既有项目绑定接口，无独立设计。
---

# 微缩生活岛 NPC 会话归属错误

## 现象与根因

创建应用的会话属于 code 模式的真实项目。应用服务创建 NPC 时不传 `project_id`，并固定 `work_mode: work`；应用开发技能表格也推荐该固定值，导致 NPC 展示在默认 work 项目下。

## 修复决策与范围

市场向启动和停止脚本注入来源会话 ID；应用开发技能要求通过 Gateway 查询来源项目与工作模式，并用于创建和消息。当前契约见 `S_01_app-session-project`。

同时修改本机仓库外应用 `D:/project/temp/living-island/scripts/app_server.py`，通过来源元数据绑定 NPC，并在应用自己的 `app_config.json` 保存来源 ID，兼容当前已运行的旧宿主。该应用文件不包含在本仓 Git 差异内。没有改写已有 NPC 的持久化元数据，也未自动重启应用；其既有启动流程负责旧角色会话清理。

## 验证证据

2026-11-03，Windows，项目 `.venv` Python 3.12.13：

- 定向执行 `pytest tests/unit_tests/test_app_market_runtime.py::test_market_assigns_non_conflicting_ports_and_injects_gateway_config -q --no-cov`，1 passed；真实启动静态进程并验证启动、停止环境均携带来源 ID。
- 应用本地可控客户端验证通过：来源为 `default_code/code` 时正确继承默认 code 项目，同角色不重复创建；来源查询失败时原样报告错误且不创建角色会话。
- 通过修改后的 `IslandAI.ensure_session` 连接当前 Gateway，查询新 NPC 元数据，独立断言其归属为创建应用的真实项目、路径为 `D:/project/temp`、工作模式为 `code`；同角色二次调用复用 ID。验证中禁止测试写入应用自身的角色绑定文件。
- 调用 Web `session.delete` 后，立即轮询遇到 `OPERATION_IN_PROGRESS`；随后独立扫描元数据确认验证会话已删除、没有该项目下的老渔夫测试会话残留。创建和归属验证通过，首次脚本因删除期间的临时状态退出非零。
- 未调用真实模型，未执行页面 NPC 对话及浏览器分组验收，未运行全量 UT、格式化或静态检查。

## 遗留

应用实例当前停止，下次启动加载本机应用修复。宿主的新增环境注入需要重启宿主后生效；此应用可先通过自身来源配置工作。其他既有应用不会自动改写，需各自更新会话创建逻辑。
