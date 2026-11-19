---
title: 配置多模态模型后，Code 未按 Work 规则加载工具
author: null
author_note: 历史作者待确认；缺少原 owner 和可靠创建署名
id: B_20261026_1602_code-multimodal-loading
type: B
modules: []
files: []
designs: null
design_note: 历史设计关联待核实，不等同于没有关联设计
---

# 配置多模态模型后，Code 未按 Work 规则加载工具

| 项 | 值 |
| --- | --- |
| 日期 | 2026-10-26（Asia/Singapore） |
| Issue | [#4237](https://gitcode.com/openJiuwen/jiuwenswarm/issues/4237) |
| 范围 | `jiuwenswarm/server/runtime/agent_adapter`、Code Spec provider、多模态工具名称集合 |
| 关联 spec | [S_01_multimodal-tool-loading](../../../../../specs/jiuwenswarm/server/runtime/agent_adapter/S_01_multimodal-tool-loading.md) |

## 现象与根因

Issue 报告 Code 模式配置相关模型后无法正常调用视频、音频工具，环境标注为 0.2.6release。当前基线虽然已有四个多模态工具 builder，仍要求工具名显式出现在 `modes.code.tools`；默认模板没有这些名称。模型配置完整不等于工具装配成功。

Work 则在模型配置刷新后直接按能力构建工具组。Code 的独立热更新实现再次检查白名单，且只覆盖视觉两项、音频转写和视频理解。因此只补默认列表既不能覆盖已有自定义列表，也无法与 Work 的全部能力保持一致。

修复前新增行为测试使用同样的完整能力配置和空 Code 工具列表：Code 初始装配和热更新均缺少工具，Work 热更新通过；四个参数化案例中三个失败、一个通过。

开发检视发现统一工厂把构建异常返回成空组，局部热更新因此将仍启用的能力误判为禁用，并注销已运行工具；Code 候选 Spec 也会吞掉多模态构建／注册异常，提交缺少工具的配置。故障注入复现中，基线 Work 保留 OCR、VQA，初版改动将两者移除。

## 决策

1. 在 Work 基类集中构建、注册和跟踪六类工具组，Code 默认 Spec provider 与旧工具卡入口复用；删除 Code 独立的四个 builder 和热更新分支。
2. 普通工具仍由 `modes.code.tools` 选择；自动多模态组独立装配并对旧列表去重。无需迁移用户配置或修改默认模板。
3. 默认配置生成的 bundle 显式开启公共多模态装配。保留调用方自定义 Spec 的工具选择权，避免应用配置隐式改写外部定义。
4. 候选 Spec 注册与当前实例接管分开，维持全量重载失败回滚；多模态局部热更新同步更新 build context 和 deep config，防止后续全量重载漏清理或漏恢复工具。
5. 区分禁用空组与构建失败：局部热更新保留失败组的原实例并继续更新其他组；Code 已有实例的候选 Spec 构建启用异常传播，注册失败时清理本次所有已尝试的 owned 注册，再由外层恢复旧实例。首次装配仍可跳过失败组，不扩大单组故障影响。

## 拒绝的方案

- 只向默认 YAML 增加视频／音频工具名：旧配置列表仍会覆盖模板，也会保留两套装配规则。
- 再复制 Work 的工具分支到 Code：后续能力新增、开关和 ownership 修复仍需多处同步。
- 绕过自定义 Spec 自动追加工具：破坏调用方对显式工具集合的控制。

## 验证

定向 pytest 覆盖初始装配、全部能力组、旧列表去重、开关热更新、模型参数更新、组内注册失败、候选 Spec 失败恢复和共享资源 ownership。音频转写与视频理解使用实际工具实例调用，模拟外部模型响应，不发送真实模型请求。

检视修复前，新增四项 Work / Code 视觉、音频构建失败用例及两项候选构建／注册失败用例均失败；原有候选解析失败回滚用例通过。修复后另覆盖首次装配失败组隔离，以及候选中新增工具组的注册回收。

2026-10-26 最终定向回归结果：**202 passed in 6.77s**。执行命令：

```bash
uv run --no-sync pytest \
  tests/unit_tests/agentserver/test_code_adapter_multimodal_tools.py \
  tests/unit_tests/agentserver/test_multimodal_capability_hot_reload.py \
  tests/unit_tests/agentserver/test_code_spec_reload.py \
  tests/unit_tests/agents/harness/code/test_code_deep_agent_spec.py \
  tests/unit_tests/agents/harness/test_multimodal_config.py \
  tests/unit_tests/agentserver/test_deep_adapter_tool_ownership.py \
  tests/unit_tests/agentserver/test_agent_reload_scope.py \
  tests/unit_tests/agentserver/test_code_adapter_runtime_config.py \
  tests/unit_tests/agentserver/test_code_adapter_acp_chat_tool.py \
  tests/unit_tests/agentserver/test_code_adapter_symphony.py \
  tests/unit_tests/test_resource_config.py \
  tests/unit_tests/agentserver/permissions/test_code_smart_f01.py \
  tests/unit_tests/agentserver/permissions/test_permission_lifecycle_integration.py \
  tests/unit_tests/agentserver/memory/test_code_adapter_coding_memory.py \
  tests/unit_tests/agentserver/test_team_manager_skill_state.py \
  -o addopts='' -o log_cli=false -o asyncio_mode=auto \
  -p no:cacheprovider -q --tb=short --show-capture=no
```

未执行格式化或静态检查，未修改依赖锁文件。

2026-10-26 rebase 至 `upstream/develop`（`67b8c033d`）时，Code Spec 工具 bundle 与上游新增的 `tool_search` 装配发生冲突。合并后先装配多模态工具，再执行上游检索工具装配，保留两者及原有注册记录。扩展旧列表／空列表用例覆盖检索开关启停，并在上述回归命令中加入 `tests/unit_tests/agentserver/test_tool_retrieval_toolkits.py`，结果为 **232 passed in 6.75s**。

## 限制

未验证真实 provider 的凭据、配额、媒体格式和模型响应。本次复用既有工具输入 Schema 与调用协议；不扩展支持的模型或重写视频工具参数结构。
