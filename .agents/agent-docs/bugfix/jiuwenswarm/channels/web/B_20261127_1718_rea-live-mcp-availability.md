---
id: B_20261127_1718_rea-live-mcp-availability
title: REA 能力状态必须以 Workswarm 实际 MCP 可调用为准
type: B
author: zhongxiaotian
author_email: zhongxiaotian@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend/src/features/reaSetup
  - jiuwenswarm/common
  - jiuwenswarm/gateway/channel_manager/web
  - jiuwenswarm/server
files:
  - jiuwenswarm/channels/web/frontend/src/features/reaSetup/ReaSetupPage.tsx
  - jiuwenswarm/channels/web/frontend/src/features/reaSetup/reaSetupProgress.ts
  - jiuwenswarm/common/rea_capability.py
  - jiuwenswarm/common/rea_mcp_probe.py
  - jiuwenswarm/common/rea_setup.py
  - jiuwenswarm/extensions/rea_setup/extension.py
  - jiuwenswarm/gateway/channel_manager/web/rea_setup_handlers.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/handlers/mcp.py
  - jiuwenswarm/server/runtime/mcp/apps.py
  - tests/unit_tests/common/test_rea_setup.py
  - tests/unit_tests/gateway/test_rea_setup_handlers.py
  - tests/unit_tests/test_mcp_apps.py
  - tests/reaSetupProgress.test.mjs
designs: []
design_note: 修复现有 REA 安装与状态展示契约，不改变组件清单、可安装分类或卡片分组。
---

# REA 能力状态必须以 Workswarm 实际 MCP 可调用为准

## 交接结论

以下事实是本修复的最高优先级口径，后续会话不得重新解释：

1. **“安装好”表示 Workswarm 已经能通过它注册并实际使用的 REA MCP 调用该能力。**
   本机发现文件、下载缓存、配置了路径、Gateway 进程能扫描到工具，都不等于 Workswarm 可用。
2. 扫描到、检测到、已缓存、已写配置，但实际 REA MCP 不能调用时，统一显示“未安装”。
3. “需配置”不是“文件已安装但配置不完整”的通用兜底。它只表示该能力本来就不能正常自动安装，
   例如需要用户登录、导入 License、安装商业软件或指定外部安装目录。该定义已经由现有组件配置固化。
4. “当前不适用”继续完全来自现有 component manifest 的平台支持清单。不得根据运行时扫描结果临时代替。
5. “部分可用”表示组合卡片中至少一个成员实际可用；其他成员可以处于未安装、需配置或其他状态。
6. 能力卡片的 `memberIds` 是用户确认过的产品分组，固定在
   `ReaSetupPage.tsx` 的 `CAPABILITY_SPECS` 中。不得修改 `install_mode`、平台支持、是否可自动安装或成员分组。
7. 基础能力安装完成后仍要扫描增强能力；增强安装按单个组件完成结果更新，不能把所有增强卡片统一切成“安装中”。

用户明确决定暂时不引入短时缓存、后台预热或定时扫描。当前完整检测虽然需要数秒，但可以接受；
实现优先保证语义正确和路径简单。

## REA 集成架构事实

- Web 前端通过 `ReaSetupApplicationPlugin` 将 REA 安装页 RPC 注册到 WebChannel。
- Gateway 侧的 `rea_setup_handlers.py` 负责状态查询、基础安装、增强安装、取消和组件辅助配置。
- REA MCP 配置记录位于：
  `<数据根>/profile/agent/workspace/mcp/state.json` 的 `mcp.rea`。
  记录包含 `command`、`args`、`env`、`state`、`tool_count` 等字段。
- 真正供会话调用的是 AgentServer 进程注册的 `rea` MCP。Gateway 自己临时启动的 stdio MCP
  只能做诊断，不能作为“Workswarm 已可用”的最终证据。
- `command.mcp` 是 AgentServer 的 MCP 控制面，支持查询、增改、连接、断开和工具列表。
  本次新增内部动作 `inspect_inventory`：通过已连接的实际 MCP 执行 `tools/list` 和
  `binary_session`，用于安装页可信诊断。
- `mcp_app.call_tool` 仍遵守 MCP Apps 的 tool visibility；安装诊断使用单独的
  `call_diagnostic_tool`，允许调用模型侧工具，但不把该能力开放为 MCP App RPC。
- 能力卡片的真实状态由后端 `build_capability_cards()` 使用实际
  `binary_session.tool_availability` 计算。前端 `CAPABILITY_SPECS` 只负责展示分组。
- REA 安装包固定为 `rea-agents@6.8.0`，组件安装来源、版本、哈希、平台和失败提示由
  `component-manifest.json` 管理。

## 现象与根因

页面曾把 Ghidra 显示为“可用”，但同一环境下 Workswarm 实际调用 REA 的
`binary_session` 返回 Ghidra `unavailable / not_configured`。

根因是状态扫描和实际 MCP 使用了不同环境：

- Gateway / AgentServer / Runtime Worker 可能继承了
  `GHIDRA_INSTALL_DIR=D:\tools\ghidra\ghidra_12.1.4_PUBLIC` 和 `JAVA_HOME=...`。
- `state.json` 中 `mcp.rea.env` 当时只有 `PATH`，没有上述变量。
- 安装页通过临时 REA 进程扫描时继承 Gateway 环境，看到 Ghidra 可用。
- 会话实际使用的 REA MCP 子进程没有继承这些变量，因此 Ghidra 不可用。
- 前端又优先信任组件扫描结果，导致“页面可用、Workswarm 不可用”。

另一个独立问题是安装/复用组件后只写入了 `state.json` 的 `env`，
没有确保 AgentServer 中实际注册的 REA MCP 重新加载，因此状态文件和运行实例可能不同步。

## 修复决策

- 增强能力扫描在受限环境中执行：只使用系统启动必需变量和 `mcp.rea.env`，
  不继承 Gateway 进程中的 `GHIDRA_INSTALL_DIR`、`JAVA_HOME` 等工具变量。
- 基础安装、增强安装和辅助配置完成后，通过 AgentServer 的 `command.mcp` 强制重载一次
  REA MCP，再通过实际连接执行 `tools/list + binary_session`。
- 只有实际工具合同校验通过后，才把 MCP 状态写成 `connected`；失败则降级为 `registered`。
- 卡片计算同时要求组件证据和 live tool evidence。Ghidra 组件文件存在但
  `binary_overview` 等工具报 `provider_unavailable` 时，卡片显示“需配置”而不是“可用”。
- 组件存在确认工具时，以确认工具的实际可用性约束组件状态；卡片级工具全部不可用时，
  不把仅靠文件发现得到的成员状态提升为“可用”。
- 前端在非安装过程中优先使用后端 `capability_cards` 的实际状态和成员状态。
  安装过程中仍展示当前组件的实时进度。
- 状态写入保持原有锁、唯一临时文件和 Windows 拒绝访问重试；
  组件环境写入不改变 MCP 生命周期状态。

## 拒绝的方案

- 不把“扫描到本机文件”当作 Workswarm 可用；这正是本次误报的来源。
- 不放宽 `connected` 或 `registered` 的含义来掩盖实际调用失败。
- 不使用 Gateway 进程环境构造诊断 MCP；该环境与实际 AgentServer MCP 不一致。
- 不把临时 MCP 探测作为最终成功依据；临时进程成功不能证明已注册实例可用。
- 不改组件 `memberIds`、平台支持或“需配置”清单来迎合当前机器。
- 不增加缓存、后台定时扫描或前端 TTL；用户已明确当前速度可接受，避免引入新的状态一致性问题。

## 验证证据

2026-11-27 在本工作区执行：

```text
pytest -o addopts='' -q --asyncio-mode=auto
  tests/unit_tests/gateway/test_rea_setup_handlers.py
  tests/unit_tests/common/test_rea_setup.py::test_component_environment_ignores_unregistered_host_tool_paths
  tests/unit_tests/common/test_rea_setup.py::test_capability_card_requires_live_mcp_tool_not_only_component_discovery
  tests/unit_tests/test_mcp_apps.py::test_diagnostic_tool_can_call_model_only_tool
  tests/unit_tests/extensions/test_rea_setup_extension.py

15 passed
```

```text
npm run test:rea-setup-progress
17 passed
```

```text
npx tsc --noEmit -p tsconfig.json
passed

npm run build
passed
```

四个 REA 测试文件去掉默认覆盖率插件后合计：

```text
92 passed, 4 failed, 6 skipped
```

四个失败均是现有 macOS / Unix 专属用例在 Windows 上无法模拟：

- Windows 上 `npx.cmd` 的 node 兄弟文件是 `node.exe`，旧测试创建的是 `node`。
- `ditto` DMG 用例需要创建 Windows 符号链接权限。
- ZIP 可执行位用例依赖 POSIX `chmod` 语义。
- macOS rizin launcher 用例尝试直接执行 shell 脚本。

本次新增和直接受影响用例全部通过，失败项未触及本次改动路径。

新增回归覆盖：

- Gateway 宿主存在 `GHIDRA_INSTALL_DIR`、但 `mcp.rea.env` 未登记时，组件不能判为可用。
- 实际 MCP 的 `binary_session` 报告 provider 不可用时，Ghidra 卡片不能显示可用。
- 状态查询通过 AgentServer 实际 MCP inventory 验证，不调用临时 stdio 进程。
- 诊断工具可以调用没有 MCP App visibility 的模型侧工具。

当前机器只读耗时参考：

```text
基础状态扫描        约 3.8 秒
完整增强状态扫描    约 10.3 秒
临时 MCP 完整校验   约 5.5 秒
```

因此当前串行完整检测上限约 10～16 秒。该耗时已被用户接受。

## 当前工作区与交付状态

- 工作区：
  `C:\Users\admin\.codex\worktrees\fix-rea-cancel-state\jiuwenswarm_rea_ready`
- 分支：`fix/rea-offline-install-recovery`
- develop PR：`!11128`，基于最新 `origin/develop`，单 commit。
- releases/0.2.10 PR：`!11130`，基于最新 `origin/releases/0.2.10`，单 commit。
- 两个分支均已推送；两个 PR 都只包含本次 REA 修复 commit。
- 不要影响用户正在运行的真实服务：`localhost:6173`、`localhost:9173`。
- 当前隔离验收实例已重启：前端 `6473`、Gateway `9473`、WebChannel `9474`、
  AgentServer `9475`，数据目录
  `D:\project\test\jiuwenswarm-rea-clean-20261127-1632`。

## 后续验证重点

1. 干净环境没有登记任何增强工具变量时，Ghidra、Java、JADX 等不能因宿主环境误显示可用。
2. 安装或配置组件后，`state.json` 的 `mcp.rea.env` 必须更新，实际 AgentServer MCP 必须重载。
3. 重载后通过实际 MCP 执行 `binary_session`，只有工具合同与实际可用性均通过才显示可用。
4. 基础安装期间增强卡片不统一显示“安装中”；基础完成后重新扫描增强能力。
5. 重新构建前端后，人工检查卡片状态、成员状态和检测按钮 busy 状态。
