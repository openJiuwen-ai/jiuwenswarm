# Gateway Extensions V2 开发与文档维护规范

本目录用于 Gateway 侧扩展机制的建设及其设计、规约、特性记录和开发计划维护。
通用开发与文档要求采用 [JiuwenSwarm 包公共规范](../../AGENTS.md)，本文仅补充模块职责和维护入口；
当前行为通过实现与测试核实，已确认契约由主责规约维护。

## 适用范围与规则入口

- 本文适用于 `jiuwenswarm/gateway/extensions_v2/` 及其全部子目录。先遵循
  [仓库根规则](../../../AGENTS.md)和 [JiuwenSwarm 包公共规范](../../AGENTS.md)，再执行本模块补充要求，
  同时遵守适用的 `AGENTS.local.md`。
- 本模块是 Gateway 进程内的独立扩展运行时，与 AgentServer 的
  [Extensions V2](../../extensions_v2/AGENTS.md) 是两个宿主、两套实例、两个包目录。
  两侧共享 agent-core `openjiuwen.core.events` 的编程模型，但**代码不得互相 import**：
  Gateway 侧只依赖 `openjiuwen.core.events` 与自身模块，禁止 import `jiuwenswarm.extensions_v2`。
- 不得修改 `jiuwenswarm/extensions_v2/event_handlers/__init__.py`；AgentServer 侧的变更走各自的模块规范。
- 修改前阅读已有实现、公开导出、调用方、相关测试和模块设计。渠道侧改动需同时核对
  [Web 渠道](../channel_manager/web/web_connect.py)与[基础 WS 渠道](../routing/base_ws_channel.py)。

## 内容边界与文档分工

| 目录 / 文件 | 用途 | 创建或更新时机 |
| --- | --- | --- |
| [AGENTS.md](AGENTS.md) | 模块规则、职责边界与阅读入口 | 模块职责或维护要求变化时 |
| [docs/designs/](docs/designs/) | 概览、详细架构、开发计划与开发模板 | 整体方案、范围或开发安排变化时 |
| [docs/specs/](docs/specs/) 中的 `S_NN_*.md` | 事件契约、错误码、配置键与不变量 | 新契约形成或已有契约变化时 |
| [docs/features/](docs/features/) 中的 `F_NN_*.md` | 具体特性的方案、决策、实施影响和验证记录 | 实现前建档，开发中补充，落地后保留历史 |

### 主要设计文件

- [设计概览](docs/designs/extensions-v2-overview.md)：定位、与 AgentServer 的关系、范围、核心模型与落地边界。
- [详细架构](docs/designs/extensions-v2-architecture.md)：分层、模块职责、装配与生命周期、事件体系、WebUI 子系统、状态与故障处理。
- [特性开发计划](docs/designs/extensions-v2-development-plan.md)：`EXTV2-GW-NN` 条目、依赖、验证要求与进度台账。
- [内置事件开发模板](docs/designs/builtin-event-development-guide.md)：新增事件、CORE/BUILTIN Handler、Ops 与触发点的完整步骤。
- [启动装配与运行时](docs/features/F_01_assembly-and-runtime.md)：单例装配、顺序约束、零扩展快路径、渠道钩子与停机。
- [`gateway.request` 与认证门禁](docs/features/F_02_gateway-request-and-auth-gate.md)：四路由语义、门禁放行条件与本地方法表。
- [运行时规约](docs/specs/S_01_gateway-extension-runtime.md)：入口、配置、包、事件、路由、错误码与不变量的唯一维护位置。
- 待建立：`docs/features/F_03_render-service.md`、`F_04_ui-store.md`、`F_05_ui-click.md`、`F_06_http-fetch.md`
  与 `docs/specs/S_02_render-service.md`、`S_03_ui-store.md`、`S_04_ui-click.md`、`S_05_http-fetch.md`。

### 代码入口

- [assembly.py](assembly.py)：Gateway 进程内唯一装配入口。
- [bootstrap.py](bootstrap.py)：事件类型、CORE Handler 与 BUILTIN Handler 的汇总注册。
- [runtime.py](runtime.py)：总线与各子系统的持有者，实现帧拦截与点击拦截触发点。
- [models.py](models.py)：全部事件输入输出与路由枚举。
- [builtin/](builtin/)：平台规则（认证门禁、本地方法表）。
- [event_types/](event_types/)：事件类型注册与匹配函数。
- [core_handlers/](core_handlers/)：各事件的 CORE Handler。
- [handler_ops/](handler_ops/)：`ops.build` 能力注册。
- 待建立：[webui/](webui/) 渲染协议、校验、登记、传输与上行适配；[examples/auth_login_demo/](examples/auth_login_demo/) 登录鉴权示例包。

## 命名与编号

- 事件名遵循 `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`；Gateway 侧事件使用 `gateway.*` 或与 AgentServer 对齐的 `ui.*`。
- 计划编号使用 `EXTV2-GW-NN`，与 AgentServer 的 `EXTV2-NN` 分别编号，互不占用。
- `docs/specs/` 与 `docs/features/` 各自作为编号作用域；新建前核对已有文件，避免编号冲突。

## 模板与必要内容

扩展相关文档须按涉及范围说明：

- 事件的身份、输入输出、匹配规则、CORE 默认动作与错误码。
- BUILTIN Handler 的规则来源、执行位置、放行/拒绝条件及与 USER 层的顺序关系。
- Ops 方法的命名空间、调用语义、失败返回形式与配置约束。
- 帧触发点的位置、超时、fail-open 行为与对原生路径的影响。
- 状态的作用域、创建与释放责任、断线与停机清理。
- 与 AgentServer 同名能力的差异点，以及共享 `extension_pkgs` 下 `domain` 归属的约束。

## 特性设计与契约同步

公共契约变化同步 [运行时规约](docs/specs/S_01_gateway-extension-runtime.md)；具体方案和实施记录在
`docs/features/` 维护。实现中需要调整已确认架构时，说明修改内容、原因、影响及验证方式。
不将目标设计或示例包行为直接写成已实现事实。

## 引用与交付检查

- 提交前检查文档相对路径与引用方，保持设计入口有效。
- 运行 `tests/unit_tests/gateway/extensions_v2/` 下的用例；只记录实际执行的验证及其限制。
- `tests/unit_tests/gateway/` 下存在与扩展无关的预存 collection 错误，不作为本模块回归依据。
- 分别运行 `git diff --check` 和 `git diff --cached --check`；`CLAUDE.md` 仅保留 `@AGENTS.md`。
