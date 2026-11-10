---
id: F_20261110_1438_opencode-go-provider
title: 新增 opencode-go 模型供应商
type: F
author: wang_njust_608
author_email: wang_njust_608@noreply.gitcode.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/common/config_panel
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/server/runtime
  - jiuwenswarm/agents/swarm
  - jiuwenswarm/agents/harness/team/rails
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/channels/tui/frontend
files:
  - jiuwenswarm/common/opencode_go.py
  - jiuwenswarm/common/config.py
  - jiuwenswarm/common/config_panel/models_handlers.py
  - jiuwenswarm/common/config_panel/tui_models_handlers.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/moa_runtime.py
  - jiuwenswarm/agents/harness/team/rails/opencode_session_rail.py
  - jiuwenswarm/agents/swarm/config_specs.py
  - jiuwenswarm/agents/swarm/providers/member_rails.py
  - jiuwenswarm/agents/swarm/registry.py
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelAdapters.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelDialog.tsx
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelGroupAdapters.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelVendorLabels.ts
  - jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts
  - jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/model.ts
  - jiuwenswarm/resources/.env.template
  - jiuwenswarm/resources/config.yaml
  - docs/zh/配置信息.md
  - docs/en/Configuration.md
designs: []
design_note: 需求与方案由本地 Spec Kit 资产 specs/001-opencode-go-provider/ 承载（spec/plan/tasks，仅本地保留、不入库），未另建 .agents/agent-docs 下的 D 编号；上游供应商预设归 agent-gateway 仓。
---

# 新增 opencode-go 模型供应商

## 实施与决策

参考 Issue 6276，新增 `opencode-go` 供应商。它是 OpenAI 兼容方言，身份只由
`model_client_config.vendor_key == "opencode-go"` 判定：`client_provider` 固定为
`OpenAI`，`api_base` 无预设值（必须手填），`models_needs_key=True`。

请求头 `x-opencode-session` 的值为 `sha256("jiuwenswarm:opencode-go:" + scope)`
取前 32 位十六进制，作用域按上下文派生：交互会话为 `session:<id>`，团队成员为
`member:<session>:<成员标识>`，配置探测固定 `probe:config-validate`，其余共享
模型构造为 `aux`，无会话时为 `session:unscoped`。用户已在 `custom_headers` 中
手工配置同名头（大小写不敏感）时一律保留用户值，程序不重复注入。

注入点：适配器 recipe 深拷贝（按会话隔离，不写回 `config` 指纹）、stable/default
模型解析、MoA 运行时、`build_model_from_entry` 兜底（`aux`）、团队成员 rail
（优先级 4，`model_copy(update=...)` 重设 LLM）。Web 端供应商卡片来自上游
`agent-gateway` 的 `model_vendor_registry`；本仓只负责选择后的保存、探测与运行。

与设计不一致的落地决策：

- `_merge_models_for_replace_all` 的保存回归放在
  `tests/unit_tests/test_models_replace_all_merge.py`（该文件已有口令往返用例），
  而不是 tasks.md 写的 `test_model_config_validation.py`。
- `inject_session_into_selection` 对未知 selection 形态（非 `ResolvedModel` /
  `ResolvedModelGroup`）原样返回：既有测试用哨兵对象模拟 resolver，未来新增
  selection DTO 也不应让模型构建路径崩溃。代价是未识别的新 DTO 会先缺少请求头，
  需再补 helper 分支。
- TUI 的 `command.model` 在 gateway 进程中执行的是已安装 `agent_gateway` wheel
  内的副本（文件头注明“Gateway 侧副本，改动请同步两侧”）。本仓改动落在
  `jiuwenswarm/common/config_panel/tui_models_handlers.py`（AgentServer /
  ConfigAdapter 使用），同样的 `vendor_key` 透出与 provider 配对校验需提交到
  上游 agent-gateway，并随 T002 的 rev 升级生效。
- 原计划用于装配断言的 `tests/agents/swarm/test_swarm_assembly.py` 在当前
  checkout 无法收集（导入已删除的 `jiuwenswarm.common.mode_matrix`，属既有破损），
  因此无条件挂载改由新文件 `tests/unit_tests/agents/swarm/test_opencode_session_rail_mount.py` 验证。

## 验证

- 后端定向：`tests/unit_tests/common/test_opencode_go.py`、`test_config_validate_model_mcc.py`、
  `test_available_models_merge.py`、`tests/unit_tests/agentserver/test_model_recipe_preparation.py`、
  `tests/unit_tests/agentserver/rails/test_opencode_session_rail.py`、`tests/unit_tests/agentserver/test_moa_runtime.py`、
  `tests/unit_tests/gateway/test_cli_channel_handlers.py`、挂载与设计 profile 测试均通过。
- 兜底回归：`uv run pytest tests/unit_tests/common tests/unit_tests/agentserver/test_model_recipe_preparation.py tests/unit_tests/agentserver/rails tests/unit_tests/agentserver/test_moa_runtime.py tests/unit_tests/gateway/test_cli_channel_handlers.py`
  → 1423 passed、3 skipped、16 failed。16 个失败均为既有环境问题：`test_windows_cli_paths.py`
  的 Windows 文件锁 `PermissionError`，`test_jev_model_clear.py` 以 GBK 读取含 UTF-8 中文的
  `resources/config.yaml`（改动前的 HEAD 版本同样失败）。
- 前端：Web `npm run test:model-picker` 59/59；`npm run test:settings-panel` 68/69（唯一失败为
  既有 `ModelsSettings.tsx` 静态扫描断言，与本次改动无关）；TUI `npm run build && npm test`
  全绿。
- 真实上游：用用户临时提供的 Key 对 `https://opencode.ai/zen/go/v1` 完成探测与直连调用，
  模型 `deepseek-v4.1-flash`；Key 未写入仓库、夹具、日志或截图，验证后已清除。
- 会话头：本地抓包服务确认同一会话 10 次请求头一致、跨会话不同、重启后同会话不变，
  其他供应商不带该头。

## 遗留

- 上游 `agent-gateway` 的 `opencode-go` 预设与 gateway 侧 `command.model` 副本尚未合入，
  Web 供应商下拉与 TUI gateway 本地执行路径需等上游发版后升级 rev（tasks.md T002）。
- 未做完整团队与定时任务端到端验证；仅以 rail 与 recipe 单测覆盖成员作用域与重建稳定性。
- 未通过真实 Web/TUI 界面逐项点击走查 §2、§5，改由前端测试、handler 测试与真实上游探测覆盖。
