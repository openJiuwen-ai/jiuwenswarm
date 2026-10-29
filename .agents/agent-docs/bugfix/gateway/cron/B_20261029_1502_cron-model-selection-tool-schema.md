---
id: B_20261029_1502_cron-model-selection-tool-schema
title: 定时任务结构化模型选择缺少工具参数声明
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - gateway/cron
  - jiuwenswarm/agents/harness/common/tools/cron
files:
  - gateway/cron/controller.py
  - jiuwenswarm/agents/harness/common/tools/cron/cron_tools.py
  - jiuwenswarm/agents/harness/common/tools/cron/cron_runtime.py
  - tests/unit_tests/gateway/test_cron_login_model_binding.py
  - tests/unit_tests/agentserver/test_cron_runtime.py
designs: []
design_note: 按用户检视意见补齐既有结构化模型选择契约，无独立设计。
---

# 定时任务结构化模型选择缺少工具参数声明

## 现象与根因

`cf7a795ac` 已在创建和更新后端接收 `model_selection`，但 `CronTools.get_tools()` 和
`CronRuntimeBridge.build_tools()` 实际生成的工具卡片没有声明该字段。仅修改旧工厂定义
无法修复通过 core 工厂注册的模型可见工具。

## 决策与契约

- 复用同一 schema 定义 `{type: model|model_group, id, route_id?}`，声明非 null 的
  `model_selection` 优先于 `model_name`；`route_id` 仅用于模型组，null 清除结构化选择。
- 同步旧独立工具、实际注册的中英文独立工具及统一 `cron` 工具的 job/patch 参数，
  保留 core 的调用包装与已有权限限制，不修改依赖包。
- 未采纳“结构化选择按登录来源重新绑定”的行为变更：当前稳定目录只包含自配及 AgentOS
  模型，登录模型是运行时追加条目，没有稳定 ID；前端通过 `model_name` 选择登录模型，
  原有分支继续绑定当前登录会话。不能根据同名关系给自配模型注入登录凭据。
  未来若把登录模型纳入稳定目录，必须同时评审解析、调度和请求级凭据装配。
- 用户文档同步中英文参数与凭据边界；本记录补充该提交的检视修订，不改变会话统一提交。

## 验证

环境为项目 `.venv`，`PYTHONPATH=.`；无真实登录、网络或模型调用。
定向验证共通过 24 项（61 项未选中），包括 4 项新增用例及原有登录凭据绑定、
工具创建/更新权限限制和星期描述回归。新增用例使用真实目录解析验证自配模型与登录模型同名时不绑定；
从旧工厂及中英文 Runtime 工厂读取实际工具卡片，验证有效/无效结构化参数并调用创建/更新包装器，
确认选择原样传给后端。测试入口为 `test_cron_login_model_binding.py`、`test_cron_runtime.py`
和 `test_cron_dow_schema_fix.py`；命令为项目 Python 执行 `-m pytest` 上述路径，
以 `-k 'login or stable_group_switch or model_selection_is_advertised_and_forwarded or TestBuildToolsAllowCreate or build_tools_cards_have_croniter' -q -o addopts=` 筛选。
对应本次工作区在 `cf7a795ac` 上的修订，最终以 amend 后提交为准。
未验证真实模型推理；schema、目录范围或注册工厂变化后需要重验。
