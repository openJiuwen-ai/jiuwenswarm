---
id: B_20261124_2055_tui-config-get-contract
title: TUI config.get 被控制面拦截，返回 Web 面板契约
type: B
author: taozhenyu
author_email: taozhenyu4@huawei.com
designs: []
design_note: 按通道归还 TUI 自有配置契约的小范围路由修复，无独立设计文档。
modules:
  - jiuwenswarm
  - jiuwenswarm/common/config_panel
  - jiuwenswarm/server
  - jiuwenswarm/server/front
  - jiuwenswarm/server/worker
  - tests/unit_tests/agentserver
  - tests/unit_tests/gateway
files:
  - jiuwenswarm/common/config_panel/tui_models_handlers.py
  - jiuwenswarm/server/worker/execution_requests.py
  - jiuwenswarm/server/front/router.py
  - tests/unit_tests/agentserver/test_agent_ws_connection_close.py
  - tests/unit_tests/agentserver/test_front_phase1.py
  - tests/unit_tests/gateway/test_cli_channel_handlers.py
---

# TUI `config.get` 被控制面拦截，返回 Web 面板契约

## 现象与根因

Front 的 `MethodRouter` 把 `config.get` 归为控制面方法，`handle_config_request` 不区分通道，一律返回 Web 面板的 `get_panel()`。TUI 通道因此拿不到自己的契约：

- payload 没有 `schema`。TUI 的 `/config set`、`/config reset` 依赖 `schema` 做键校验，后端配置项全部报 `Unknown config key`；`/config`、`/config list|edit` 与 Tab 补全只剩前端本地项（`Theme`）。
- TUI 的敏感项脱敏同样依赖 `schema` 的 `sensitive` 标记。面板 payload 里带有各 API Key 的取值（`asr_api_key` 在 `get_panel` 中还会被解密），`/config get api_key` 以明文显示。`/export` 未纳入本次复现，不再据此作泄露结论。
- TUI 专有键缺失：`auto_recap_enabled`、`enable_swarmflow`、`auto_harness_*`，`/config get auto_recap_enabled` 报 `No config value found`。

TUI 的契约只在执行面的 `config_get_handler` 中构造。Front 与 Runtime 分属不同进程，且 `test_front_module_import_does_not_load_runtime` 约定 Front 的导入边界不得加载 `jiuwenswarm.common.config`，Front 无法自行拼出这份契约。TUI 的 `config.set`、`config.validate_model`、`models.list` 本来就在执行面处理，只有 `config.get` 被拦在控制面，读写两侧落在了不同进程、不同视图上。

发现时最新 `develop`（`5a875665f`）与已发布的 `releases/0.2.9` 均存在。已检索 open Issue 与 open PR（标题与正文），未发现覆盖该问题的条目。

## 修复与不变量

- `is_control_method` 对 `channel_id == "tui"` 的 `config.get` 返回 `False`，请求经原有 admission 路径进入执行面，由 `ConfigAdapter` 交给既有的 TUI `config_get_handler`，与 `config.set` 同进程、同视图。
- 不变量：Web 通道的 `config.get` 仍走控制面并返回 Web 面板契约；TUI 通道的其余控制面方法（如 `session.list`、`locale.get_conf`）不受影响；TUI 前端未改动。

## 评审后续修复

路由调整后，TUI 连接时的自动 `config.get` 开始触达执行面既有逻辑，评审据此补充以下修复：

- **损坏 YAML 不得被读取覆盖**：`get_auto_harness_config()` 读取或解析失败后直接抛出原异常，不再把配置当成空字典继续补默认值。`config.get` 与 Auto-Harness setter 的既有异常边界负责兜底，原文件保持不变；正常配置的默认值补齐行为不变。
- **GitCode Token 不进入普通读取响应**：`auto_harness_gitcode_access_token` 已配置时只返回固定 `******`，未配置时返回空字符串。`config.set` 收到该掩码时视为未修改，在通用加密前移除该字段，防止读回后写回把掩码保存成新 Token；显式空字符串仍表示清空，其他值正常替换。其他敏感配置字段的既有契约不在本次范围内。
- **应用级读取不依赖会话存活**：执行面的生命周期检查仅豁免 `config.get`，携带已归档会话 ID 时仍可读取配置；`config.set` 等其他执行面方法仍受原检查约束。
- **写入失败不得误报成功**：YAML setter 失败时记录失败键，`config.set` 返回 `ok=false`、`code=CONFIG_WRITE_FAILED`，payload 分别列出 `updated` 和 `failed`，不再携带 `applied_without_restart=true`。仅提交 Token 掩码仍是成功的无修改操作。

## 行为验证

- `tests/unit_tests/agentserver/test_front_phase1.py` 新增：
  - TUI `config.get` 进入 admission；
  - Web `config.get`、TUI `session.list`、TUI `locale.get_conf` 仍判为控制面方法；
  - 经路由器与真实 `ConfigAdapter` 的 TUI `config.get` 响应带 `schema`，`api_key` 标记为敏感，并含 `auto_recap_enabled`。
- 撤去 `router.py` 改动后，上述用例失败；恢复后通过。
- `tests/unit_tests/agentserver/test_front_phase1.py` 与 `tests/unit_tests/server/test_server_regressions_2.py`：**54 passed, 1 failed**。失败的 `test_enterprise_task_roots_are_tenant_scoped_and_stable` 在未修改的代码上同样失败，原因是用例以 `workspace_group::bot::admin` 作为目录名，Windows 下非法，与本修复无关。
- `python -m py_compile` 与 `git diff --check` 通过。
- UI 级对照（源码构建 TUI，两侧前端一致）：
  - 修复前（已安装 0.2.9，`ws://127.0.0.1:19001/tui`）：`/config` 编辑器仅 `Theme` 一项；`/config set context_engine_enabled true` 报 `Unknown config key`；`/config get api_key` 明文显示；`/config get auto_recap_enabled` 报 `No config value found`。
  - 修复后（源码实例，`ws://127.0.0.1:20001/tui`）：编辑器列出 35 项；同一 `config.set` 返回 `Config Updated`；`/config get api_key` 显示 `******`；`/config get auto_recap_enabled` 返回 `true`。
- 进程级协议对照（直连源码实例 AgentServer）：`channel_id=web` 仍返回 151 键的面板契约、无 `schema`；`channel_id=tui` 返回带 38 项 `schema`（13 项敏感）的 TUI 契约。
- 评审后续新增 10 个定向用例全部通过，覆盖损坏 YAML 在读写路径均保持原字节、YAML/环境变量/空值三种 Token 来源、掩码不写回、空值清空与新值替换，以及归档会话下 `config.get` 放行而 `config.set` 仍返回 `SESSION_ARCHIVED`。损坏 YAML 的写入用例还断言响应为 `ok=false`、`code=CONFIG_WRITE_FAILED`，且 `failed` 包含对应键；掩码空更新仍返回 `ok=true`。
- 2026-11-28 变基到 `develop` `1586ff3e7` 后，上游 Worker 拆分保持不变，会话豁免落在 `worker/execution_requests.py`。`test_cli_channel_handlers.py`、`test_agent_ws_connection_close.py`、`test_front_phase1.py`、`test_gateway_adapter.py` 整组：**185 passed**。

## 交接限制

桌面版 `workswarm.exe` 未打包 CLI/TUI 通道（issue #5462），UI 级验证使用源码构建的 TUI 前端分别连接已安装 0.2.9 后端（旧代码）与源码后端（含本修复）。评审后续项由真实处理器与 WebSocket 分发单测覆盖，未重复采集 UI 截图。
