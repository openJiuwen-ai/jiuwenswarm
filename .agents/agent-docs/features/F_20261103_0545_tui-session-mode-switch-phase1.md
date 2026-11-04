---
id: F_20261103_0545_tui-session-mode-switch-phase1
title: TUI 同一会话模式切换第一阶段
type: F
author: Yanqing-Shi
author_email: toddsyq@gmail.com
modules:
- jiuwenswarm/channels/tui/frontend
- agent-gateway/agent_gateway/channel_manager/tui
- agent-gateway/agent_gateway/common
- jiuwenswarm/runtime
- jiuwenswarm/server
- jiuwenswarm/server/runtime/session
files:
- jiuwenswarm/channels/tui/frontend/src/app-state.ts
- jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/mode.ts
- jiuwenswarm/channels/tui/frontend/src/core/ws-client.ts
- jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/swarmflow.ts
- jiuwenswarm/channels/tui/frontend/src/core/commands/types.ts
- jiuwenswarm/channels/tui/frontend/src/ui/app-screen.ts
- jiuwenswarm/channels/tui/frontend/tests/run-tests.mjs
- jiuwenswarm/channels/tui/frontend/tests/mode-draft-compat.test.mjs
- jiuwenswarm/channels/tui/frontend/package.json
- agent-gateway/agent_gateway/channel_manager/tui/tui_connect.py
- agent-gateway/agent_gateway/common/protocol_compat.py
- agent-gateway/tests/unit_tests/gateway/test_tui_mode_forwarding.py
- jiuwenswarm/common/schema/message.py
- jiuwenswarm/server/agent_ws_server.py
- jiuwenswarm/runtime/service.py
- jiuwenswarm/server/runtime/session/session_metadata.py
- jiuwenswarm/server/runtime/session/session_history.py
- tests/unit_tests/runtime/test_session_mode_switch.py
- tests/unit_tests/server/test_archive_lifecycle.py
- pyproject.toml
- uv.lock
designs:
- D-20261101-1139-tui-session-mode-switch
---

# TUI 同一会话模式切换第一阶段

## 实施与决策

TUI `/mode` 先经 Gateway 发送 `mode.set`，由 AgentServer 调用 Runtime 的统一入口提交当前 Session 的执行模式。成功响应后才更新本地模式。请求使用完整 canonical mode，并建议携带 `expected_mode`；同目标请求幂等，陈旧期望值返回 `MODE_CONFLICT`。本进程活动执行、待答交互或其他存活进程持有运行态时返回 `SESSION_BUSY`。`/mode` 不弹出自动取消确认，也不发送中断；用户单独完成或取消任务后重试。

提交在项目、Session 和 metadata 文件锁内同步完成，仅更新 `mode` 和递增的 `mode_revision`。原 `project_id`、`project_dir`、`work_mode` 及历史目录保持不变；旧异步 metadata 快照不能回滚显式切换。聊天准入先认领运行态，再读取模式并分类，防止跨进程切换发生在读取与认领之间。`session.mode_changed` 作为独立诊断事件写入历史，不充当路由事实源。

TUI 对超时和连接中断重试一次，再通过 `mode.get` 核对权威状态。冲突直接核对，不重复覆盖。无法确认时阻止聊天发送，保留编辑器文本和附件引用；同步完成后由用户重新发送。一次模式命令固定真实 Session ID，其重试和核对不会改发到后来切入的会话，也不会污染新会话界面。AgentServer 串行处理同一 Session 的 set/get，查询等待先前切换完成启动和提交。

`/swarmflow on` 复用相同入口，在启动恢复后读取实时模式，保留已有 Team profile；配置写入失败时尝试恢复实际原模式，并明确报告未确认的回切。协议包尚未提供 MODE_SET/MODE_GET 时沿用本仓枚举兼容层，Gateway 与 AgentServer 使用同名同值成员。

## 设计偏差与范围

本次实施依据为用户确认的第一阶段范围和原项目索引决策；关联设计仍是未完成正式评审的草案。接口要求完整 canonical 值，别名由 TUI 解析。只交付 TUI 的显式路由切换，不接入 Web、自动路由插件或 Agent/Team Leader 模型上下文交接，也不修改 agent-core。模式切换不会将源执行者的模型对话自动导入目标。

## 验证与交接

候选基于上游 develop 的 `a2abceccfba89ab25a518568916152865fa28acd`，保留上游新加的非 Team 附件处理路径。Windows / Python 3.14.7 下，以下定向行为测试共 244 passed：

- `tests/unit_tests/runtime/test_session_mode_switch.py`
- `tests/unit_tests/agentserver/test_mode_cross_layer_protocol.py`
- `tests/unit_tests/server/test_session_metadata_write_options.py`
- `tests/unit_tests/agentserver/test_team_dispatch_mode.py`
- `tests/unit_tests/runtime/test_runtime_service.py`
- `tests/unit_tests/runtime/test_request.py`

TUI 用例覆盖成功、忙碌拒绝且不自动中断、超时与网络中断、冲突核对、编辑器输入和附件保留、会话切换竞态、启动 Session ID 解析及 SwarmFlow 回切。使用 Node 26.13.0，仅将 TypeScript 转译为测试所需 ES module，不运行类型检查或格式化。最终 `npm test` 全部通过，包含输入编辑、监督环境、交接端口、任务生命周期、切换命令、会话注册、用户回答、Persist、记忆、演进、cron、状态栏及工具结果套件。

未执行真实服务端连接、外部模型或交互式终端截图验证；上述证据是自动化行为测试，不代表模型上下文交接验收。运行日志、会话轨迹和其他原始运行产物不属于交付内容。

## 检视后修订

2026-11-04 的旧数据恢复、成功响应归一化与 Gateway 导入顺序修复见 [B_20261104_2319_tui-mode-response-recovery](../bugfix/jiuwenswarm/server/runtime/session/B_20261104_2319_tui-mode-response-recovery.md)。当前恢复与错误语义以 `S_01_tui-mode-switch` 为准；上文保留第一阶段原验证事实。

## 2026-11-04 与 PR 10243 / gateway PR 24 的本地兼容

远端核对时 PR 10243 为 `d2cfad7942e8804bf5f82f064bb00d2ff1445e35`，PR 10515 为 `90cf5d0f6788795937466dcdf35bbfcf85c7546e`，gateway PR 24 为 `54ed9dd936e3a1bf92b21dfa4467663c730a592a`。用户要求尽量保留 PR 10243 提交，因此本地候选以 PR 10243 为基线叠加 PR 10515 及其已有未提交恢复修复；原两个工作区和 PR 提交不改写。gateway 副本同样以 PR 24 为基线追加模式转发。

`app-state.ts` 的三个文本冲突分别涉及状态字段、命令上下文和创建完成后的 ID 更新。保留 PR 10243 的所有草稿状态、排队帧重绑定及清理逻辑；模式命令增加启动结果与最近一次草稿到真实会话的映射，解析时等待已发起的创建，随后固定请求目标。命令上下文提供实时草稿判定。未创建的草稿只更新本地模式；已有会话继续走显式提交和忙碌保护。不用恢复启动即创建会话来绕过冲突，也不直接把命令目标改成任意当前会话，以免迟到结果误操作用户后来切入的会话。

独立 gateway 的转发集合增加 `mode.set` / `mode.get`，协议兼容列表增加同名同值枚举，共六行生产代码新增。沿用已有 `protocol_compat` 扩展点，无需把 `app_gateway.py` 改回导入宿主 `jiuwenswarm.common.schema.message`。PR 24 的归档、恢复、删除处理器及生命周期事件实现保留。JiuwenSwarm 新模式测试改用 `agent_gateway` 导入路径。

验证环境为 Windows、Python 3.14.7、Node 26.13.0、共享现有 `.venv` 和前端依赖；Python 通过 `PYTHONPATH` 加载上述两个本地候选，运行数据和临时目录隔离在 `artifacts/mode-draft-verification/`。仅转译 TypeScript 生成测试 ES module，不运行格式化或静态检查。

- 完整 TUI `npm test` 通过，包括原 PR 10243 的草稿、归档、删除测试及新增 `mode-draft-compat.test.mjs`。新增场景从实际 AppState / 命令入口覆盖启动屏障、`/new` / `/clear` 后本地模式选择、首条 Plan 消息、Persist 初始化、创建中切换、忙碌拒绝、切入其他会话、创建失败、草稿 SwarmFlow 和配置查询期间创建完成。
- 模式切换、跨层协议、metadata 写入、team dispatch、runtime service、request、archive lifecycle 及新增独立 gateway 用例共 **380 passed**。命令显式使用 `--asyncio-mode=auto`；清空 pytest 默认 addopts 时不能遗漏该参数。
- gateway CLI handlers、TUI project path、lifecycle handlers 和 GatewayServer 分派回归为 **130 passed, 1 skipped**。
- gateway 新用例在隔离子进程中导入包，验证枚举可用且未导入 JiuwenSwarm；实际 GatewayServer 接收两种 JSON 请求帧后保留会话和模式参数并转发，不发本地 ACK。

迁回 PR 10515 原分支后的全组复验暴露 `test_archive_lifecycle.py` 的一处历史 `gateway.channel_manager.web` 导入：初次候选环境仍可从已有 editable 包找到旧 gateway，因此该结果不能单独证明测试已完成拆仓适配。将该导入改为 `agent_gateway.channel_manager.web`，作为 PR 10515 的后续修复保留 PR 10243 历史；最终复验只加载独立 gateway，运行命令使用原分支的源码目录与隔离运行目录，十二组测试合计 **510 passed, 1 skipped**。完整 TUI 测试在原分支重新转译后通过。

以上结果对应本次候选源码及本地 gateway 覆盖，不代表锁定依赖已发布；未运行真实 AgentServer/模型连接或交互终端截图验证。后续替换 gateway 依赖、改动创建/分派装配或更新候选代码时重验受影响范围。初次候选准备阶段未提交或推送；用户随后确认将既有修复与兼容合并提交到 PR 10515 原分支。该阶段 gateway 修复单独保留本地分支，后续远端依赖闭环见下一节。

## Gateway 依赖发布与锁定验证

用户确认将 gateway 配套提交推送到 PR 24 的原分支 `pr10243-tui-session-lifecycle`，并更新该 PR 的标题和说明，分别关联 JiuwenSwarm PR 10243 的生命周期功能和 PR 10515 的模式转发功能。PR 24 保留原有生命周期实现；新增 `mode.set` / `mode.get` 转发和独立进程协议兼容。10243 的提交与依赖不在本次改动范围内。

PR 10515 的 `pyproject.toml` 两处依赖声明及 `uv.lock` 改为同一远端提交 `eec0a207435d103aa4be44063eeba35f121e813c`。使用 uv 0.12.5 执行 `uv lock --upgrade-package agent-gateway` 解析 330 个包；保留现有锁格式 revision 5，再以 uv 0.12.15 执行 `uv lock --locked --offline` 通过。锁文件仅更新 gateway 的提交引用，没有升级其他包。

验证从锁文件读取仓库和提交，将远端 gateway 构建为非 editable 包，安装到隔离目录，独立检查 `direct_url.json` 中的提交 ID、包实际加载路径，以及不导入 JiuwenSwarm 时的协议枚举注册和 TUI 转发集合。随后使用该安装目录替代本地 gateway checkout 的 `PYTHONPATH` 覆盖，重跑上述十二组测试，结果为 **510 passed, 1 skipped**。其他 Python 依赖沿用已有测试环境，本次不宣称完成全新环境的全部依赖安装或真实服务端端到端验证。
