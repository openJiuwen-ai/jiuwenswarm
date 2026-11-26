---
id: B_20261126_1052_team-memory-rail-race
title: Team 订阅与用户请求并发卸载记忆 Rail
type: B
author: Wang Tao
author_email: zzyyww0808@gmail.com
modules: [jiuwenswarm/server/runtime/agent_adapter]
files: [jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, tests/unit_tests/agentserver/memory/test_jiuwen_coding_integration.py, tests/unit_tests/agentserver/memory/test_jiuwen_rail_builder.py]
designs: []
design_note: 按已确认的最小修复方案恢复被动订阅和记忆生命周期契约，无独立新设计。
---

# Team 记忆 Rail 并发卸载修复

## 现象与根因

同一 Team 会话的真实用户输入和 `attach_only` 订阅同时进入记忆清理。两个请求都通过非空判断后等待 drain，先完成者注销并清空 `_memory_rail`，后完成者重新读取属性得到 `None`，最终在 Core 的 `get_callbacks()` 处异常，真实轮次没有助手输出。

`attach_only` 本应只观察团队事件；原流式入口遗漏了记忆生命周期的订阅判断。记忆调整自身也没有串行保护，配置刷新等其他入口仍可能重复注销同一对象。问题取决于异步时序，不能按 Windows 专属故障处理。

## 修复决策与契约

Team 流式入口复用 `_is_attach_only_subscription`，仅真实输入调整入口记忆 Rail。每个 DeepAdapter 持有独立的 `asyncio.Lock`，串行处理记忆配置判断、注册、drain、注销和指纹更新；锁内捕获局部引用用于清理和注销。锁不覆盖整轮对话，其他 Adapter 使用自己的锁。

仅增加非空检查无法跨越异步等待保证状态；仅保存局部引用仍会重复清理；仅跳过订阅不能防止配置刷新与请求重叠，因此同时采用订阅绕过和生命周期锁。

当前契约更新在 [S_04 Coding 文档记忆接入](../../../../../specs/jiuwenswarm/server/runtime/agent_adapter/S_04_coding-document-memory.md) 的 Team 扩展中。保持原 Work/Code/Team 选择规则、工具、身份、存储与查询行为。

## 验证

基线为本地 `main/develop`（2026-11-25）。修改业务代码前，新增回归中真实输入正常，订阅误触清理和并发 `None.get_callbacks()` 两项失败；修改后两项均通过。

回归从 `process_message_stream_impl` 验证真实输入与订阅均能收到 Team 事件，并检查仅真实输入触发清理；并发用例用 Event 控制交错，检查两个调整请求正常完成，旧 Rail 只进入一次 drain 和注销。沿用已有配置身份、项目和媒体变更重建测试。

定向回归共 635 项通过（分两批 555 + 80），包含上述 3 个新增用例。范围为 `tests/unit_tests/agentserver/` 下：

- `memory/test_jiuwen_coding_integration.py`、`memory/test_jiuwen_rail_builder.py`。
- `memory/test_external_memory_config.py`、`memory/test_external_memory_builder.py`。
- `test_agentserver_modes.py`、`test_team_helpers.py`、`test_team_completion_runtime_snapshot.py`、`test_deep_adapter_login_model.py`。

使用 `pytest <上述文件> --asyncio-mode=auto`。本地旧环境的 Memory 可编辑安装指向已切换代码结构的目录，第一批跳过了 builder 模块；第二批通过临时导入此前 Memory 实现的源码快照补跑该模块，80 项全部执行通过，未修改仓库依赖。`git diff --check` 通过；仓库约定禁止运行本地静态检查工具。

环境为 Linux、Python 3.12，Core 使用主分支固定提交对应的安装包，Gateway 使用既有本地开发依赖。Provider 和团队执行为测试替身，不属于真实 LLM 或 Windows/macOS 端到端验收。

## 遗留

本次只修复 Swarm 状态竞争。Core `JiuwenMemoryRail.uninit()` 同步等待事件循环造成的约 15 秒阻塞、失败环境的成员登记 ImportError、前端错误探针识别分别保留为独立问题；不修改 Core、Memory、前端、依赖或现有运行服务。
