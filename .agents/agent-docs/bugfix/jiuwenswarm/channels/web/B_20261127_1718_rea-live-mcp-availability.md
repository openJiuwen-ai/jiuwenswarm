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
  - jiuwenswarm/channels/web/frontend/src/features/reaSetup/reaSetupApi.ts
  - jiuwenswarm/channels/web/frontend/src/features/reaSetup/reaSetupProgress.ts
  - jiuwenswarm/channels/web/frontend/tests/reaSetupFilePicker.test.mjs
  - jiuwenswarm/common/rea_capability.py
  - jiuwenswarm/common/rea_mcp_probe.py
  - jiuwenswarm/common/rea_setup.py
  - jiuwenswarm/resources/rea/capability-cards.json
  - jiuwenswarm/resources/rea/component-manifest.json
  - jiuwenswarm/extensions/rea_setup/extension.py
  - jiuwenswarm/gateway/channel_manager/web/rea_setup_handlers.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/handlers/mcp.py
  - jiuwenswarm/server/handlers/mcp_hub.py
  - jiuwenswarm/server/runtime/agent_manager.py
  - jiuwenswarm/server/runtime/mcp/apps.py
  - jiuwenswarm/server/runtime/mcp/control.py
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

## 2026-11-28 干净环境复核与修正

第一次交付后在全新环境验证发现目标仍未完全落地，必须记录为避免回归：

- `_scan_enhancements()` 当时没有真正进入 `_component_env_scope()`，Gateway 仍从宿主进程看到
  `GHIDRA_INSTALL_DIR` 和 `JAVA_HOME`，把本机已有目录误当成 Workswarm 已配置。
- 卡片的 live tool 判定曾把 `target_required` 当成可用证据。该原因只表示调用前还需要目标文件；
  当对应 provider 实际不可用时，不能据此显示可用。
- Java 是 Ghidra 和 Android 的前置依赖，不能在主能力缺失时单独把卡片提升为“部分可用”。
- 对于组合卡片，如果实际 MCP 只有一部分工具可用，卡片仍应显示“部分可用”；
  不能因为组件扫描未发现就忽略已经真实可调用的工具。

修正后的规则：

1. 增强扫描全程使用 `mcp.rea.env` 构建的受限环境。
2. `analysis_provider_candidates` 是 provider 可用性的权威证据。
3. `target_required` 只有在对应 provider 实际可用时才能参与可用性计算。
4. 自动安装组件实际不可用时映射为 `missing/未安装`；手动、License 或系统组件仍映射为
   `needs_action/需配置`；平台写死的 unsupported 继续显示“当前不适用”。
5. Java 等前置依赖不单独贡献卡片的 ready 数。
6. 卡片存在真实可调用工具子集时显示“部分可用”。

新环境实际 MCP 查询结果与本机页面投影一致：

```text
Ghidra          not_installed
IDA             needs_action
Kuna            not_installed
Hopper          unsupported
Android         not_installed
JEB             needs_action
Apple           unsupported
Dynamic         partial
Network/Firmware partial
Script/Formats  partial
```

其中 Ghidra provider 的 live 结果为：

```text
status = unavailable
code = not_configured
reason = GHIDRA_INSTALL_DIR is not set
selected = false
```

同日继续复核动态分析卡片，补充一条必须遵守的实际可用性规则：

- `chrome` 组件在清单中是 `install_mode=managed`，不是用户写死的“需配置”。
- 本机检测到 Edge，但尚未写入 `mcp.rea.env` 的 `REA_BROWSER_EXECUTABLE`。
- 实际 Workswarm MCP 的 `list_browser_targets`、`capture_web_screenshot`、
- `list_electron_targets` 等确认工具返回 `available=true`。
- 因此浏览器运行时是 Workswarm 实际可用，不能因为组件扫描是 `discovered` 就显示需配置。
- 自动安装组件只有“发现但未配置”、且没有实际确认工具可用时，才显示未安装。
- 手动/License/系统能力仍只按既有定义显示需配置。

随后用实际 `tools/call` 复核发现，仅看 `tools/list` 和
`binary_session.tool_availability` 仍会误报：Frida 三个工具在可用列表中，
但实际调用 `list_frida_devices` 返回 `capability_unavailable / unsupported_provider`
（`Could not locate the bindings file`）。因此“动态分析卡片为 available”的旧结论作废，
必须保留本节的负向证据。

Frida 不是孤例。现在的实际功能探测覆盖所有存在无目标安全诊断工具的能力：

- Frida：`list_frida_devices`
- Cutter：`list_cutter_sessions`
- 浏览器运行时：`list_browser_targets`
- ADB：`inspect_adb_client`
- Apktool：`inspect_apktool_client`

任一探测返回 `isError=true`、MCP 错误或超时，对应工具都会被降级；即使
`analysis_provider_candidates` 或静态 `tool_availability` 声称可用，也不能覆盖负向的实际调用证据。

同时统一收口“需配置”的生成条件：只有 `manual / manual_license / system`
组件在不可用且未被平台不支持规则优先覆盖时，才允许返回 `needs_action`。
所有自动安装模式统一返回 `missing`，不会再出现自动安装组件显示“需配置”。
回归用例遍历当前平台全部组件并验证这条分类规则。

安装流程在干净环境复核后进一步收口：

- “安装中”只由该组件自己的 `component_<id>_*` 步骤处于 `running` 时产生。
  其他组件下载不会让当前组件显示“安装中”。
- 组件安装完成但整个增强作业尚未完成最终 MCP 重载和 `binary_session` 校验时，
  显示“待验证”，不能提前显示可用。
- 增强安装开始前先读取实际 live MCP 可用性。已经由 Workswarm 确认可用的组件
  记录为 `reused/source=live-mcp` 并跳过下载。
- 取消检查覆盖最终重载前、重载后和最终扫描后；最终校验阶段点击取消仍得到
  `cancelled`。
- 取消或失败后清空本次组件临时结果并单次重新检测，刷新最长 45 秒；
  不重试、不循环，避免无限卡住，同时保留取消提示或原始失败信息。
- 卡片状态完全由小组件聚合，不再用卡片级工具绕过小组件状态：
  一个或多个小组件可用且不是全部为 `partial`，全部可用才是 `available`。

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
  `binary_session.tool_availability` 和无目标 `tools/call` 探测结果计算。
  前端 `CAPABILITY_SPECS` 只负责展示分组。
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
- 安装页诊断在同一已注册 REA MCP 上执行 Frida、Cutter、浏览器、ADB、Apktool
  的无目标安全调用；实际调用失败优先于 provider 或 `tools/list` 的可用声明。
- 前端在非安装过程中优先使用后端 `capability_cards` 的实际状态和成员状态。
  安装过程中仍展示当前组件的实时进度。
- 状态写入保持原有锁、唯一临时文件和 Windows 拒绝访问重试；
  组件环境写入不改变 MCP 生命周期状态。

## 拒绝的方案

- 不把“扫描到本机文件”当作 Workswarm 可用；这正是本次误报的来源。
- 不放宽 `connected` 或 `registered` 的含义来掩盖实际调用失败。
- 不使用 Gateway 进程环境构造诊断 MCP；该环境与实际 AgentServer MCP 不一致。
- 不把临时 MCP 探测作为最终成功依据；临时进程成功不能证明已注册实例可用。
- 不把“工具出现在 `tools/list`”或 `tool_availability` 的静态声明当成实际可调用；
  至少需要对存在安全诊断入口的能力执行一次真实 `tools/call`。
- 不改组件 `memberIds`、平台支持或“需配置”清单来迎合当前机器。
- 不增加缓存、后台定时扫描或前端 TTL；用户已明确当前速度可接受，避免引入新的状态一致性问题。

## 2026-11-28 Issue #7231 / #7294 / #7313

### #7231：IDA JSON 上传依赖 tkinter

现象：EXE 中点击 IDA 的“上传 JSON”没有打开资源管理器，返回
`file picker unavailable: No module named 'tkinter'`。

根因：REA 安装页直接调用 `path.select_file`，该后端入口在 Windows 上依赖
`tkinter`；EXE 已经提供 pywebview 文件桥接，但安装页绕过了它。

修复：REA 文件选择改用 Workswarm 统一的 `selectLocalFiles(false)`：

- EXE 使用 `window.pywebview.api.select_local_files`
- 浏览器 / WHL 回落到 `path.select_files`
- 目录选择复用 `selectProjectDirectory`

因此 REA 配置流程不再直接依赖 `path.select_file` 或 `tkinter`。

### #7294：重装期间连接/解绑出现 runtime worker closed

现象：REA 重装时，从“扩展-我的扩展”点击 REA 连接或解绑，与安装后的 MCP
重载并发，前端出现 `runtime worker closed`。

根因：安装重载、连接和解绑会同时修改同一 MCP 的注册/运行态。`reload_agents_config`
自身虽然串行化完整重载，但 targeted add/remove 与完整 reload 不共享同一把生命周期锁。

修复：`AgentManager` 增加按连接器名称隔离的 `mcp_change_guard`。
`command.mcp` 增改、`mcp.connect`、`mcp.disconnect` 对同一名称进入同一临界区；
不同连接器仍可并行，不扩大对其他 MCP 的影响。REA 安装重载会先完成或让用户操作等待，
不再交叉执行。

### #7313：不兼容的受管 Node 没有自动替换

现象：本机 Node 不兼容时，一键安装没有自动准备兼容版本，而是报
`受管 Node ... 不满足 REA 版本要求`。

根因：`ensure_node_runtime()` 只要受管目录里存在 `node` 文件就跳过下载；
已存在但版本不兼容、解压不完整或目录残留时，后续版本检查会直接失败。

修复：受管 Node 现在按“文件完整 + 实际执行版本在支持范围”判定。
不合格时重新下载固定版本到独立 staging 目录，验证通过后再原子替换旧目录；
替换失败会恢复旧目录。`26.13.0` 已加入显式兼容回归。

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
102 passed, 4 failed, 6 skipped
```

前端生产构建通过。

真实 RPC 流程验证（隔离环境 `6573/9573/9574/9575`）：

```text
一键基础安装              succeeded, base_ready=true
Cutter 继续安装           succeeded, state=reused, source=live-mcp，未重新下载
ADB 安装后取消            cancelled, error=null
取消后 rea.status.get     正常返回，不等待或卡死
非法组件作业                failed，保留明确错误
失败后 rea.status.get      正常返回，未无限重试
Ghidra 未配置             not_installed
网络与固件                 not_installed
动态分析                    partial
脚本与格式                  partial
```

四个失败均是现有 macOS / Unix 专属用例在 Windows 上无法模拟：

- Windows 上 `npx.cmd` 的 node 兄弟文件是 `node.exe`，旧测试创建的是 `node`。
- `ditto` DMG 用例需要创建 Windows 符号链接权限。
- ZIP 可执行位用例依赖 POSIX `chmod` 语义。
- macOS rizin launcher 用例尝试直接执行 shell 脚本。

本次新增和直接受影响用例全部通过，失败项未触及本次改动路径。

2026-11-28 三个 Issue 的增量验证：

```text
pytest tests/unit_tests/common/test_rea_setup.py
  4 selected tests passed
pytest tests/unit_tests/gateway/test_rea_setup_handlers.py
  12 passed
pytest tests/unit_tests/agentserver/mcp/test_command_mcp_handler.py
  tests/unit_tests/agentserver/mcp/test_mcp_targeted_control.py
  37 passed
pytest tests/unit_tests/agentserver/test_agentserver_cli_commands.py -k mcp
  14 passed
npm run test:rea-setup-file-picker
  3 passed
npm run test:rea-setup-progress
  19 passed
npm run build
  passed
```

测试使用 `--no-cov`，原因是并行 pytest 进程会竞争同一个 coverage SQLite 文件，
导致测试结束阶段出现 `no such table: cov_schema`。这是测试编排问题，不是业务失败。

新增回归覆盖：

- Gateway 宿主存在 `GHIDRA_INSTALL_DIR`、但 `mcp.rea.env` 未登记时，组件不能判为可用。
- 实际 MCP 的 `binary_session` 报告 provider 不可用时，Ghidra 卡片不能显示可用。
- 状态查询通过 AgentServer 实际 MCP inventory 验证，不调用临时 stdio 进程。
- 诊断工具可以调用没有 MCP App visibility 的模型侧工具。
- Frida、Cutter、浏览器、ADB、Apktool 的实际诊断失败可以覆盖静态可用声明。
- IDA JSON/目录选择复用桌面桥接或 `path.select_files`，不调用 `path.select_file`。
- 同一 MCP 的安装重载、连接和解绑通过按名称的异步锁串行。
- 已存在但不兼容的受管 Node 会被重新下载并原子替换。

当前机器只读耗时参考：

```text
基础状态扫描        约 3.8 秒
完整增强状态扫描    约 10.3 秒
临时 MCP 完整校验   约 5.5 秒
```

因此当前串行完整检测上限约 10～16 秒。该耗时已被用户接受。

## 2026-11-28 15:00 最终用户路径自验证

本轮在隔离数据根
`D:\project\test\jiuwenswarm-rea-verify-20261128_1145` 中完成，前端为
`6773`，Gateway/WebChannel/AgentServer 为 `9773/9774/9775`。没有操作用户正在使用的
`6173`、`9173`。

### 页面与卡片

服务进程早于最新前端 `dist` 启动时，页面会继续展示旧缓存和旧状态；重启同一进程树后，
卡片与新后端矩阵一致。最终页面状态：

```text
基础环境                 3/3 已安装
增强能力                 5 张已具备实际能力，5 张不可作为 Workswarm 已安装能力
Ghidra                   可用
Android 工具链           可用
IDA                     需配置，manifest=manual_license
Kuna                    当前不适用
Hopper                  当前不适用
JEB Android             需配置，manifest=manual_license
Apple 平台支持           当前不适用
动态分析                 部分可用
网络与固件               部分可用
脚本与格式               部分可用
```

逐个打开说明弹窗后的成员状态：

```text
动态分析
  GDB                      当前系统不支持
  Rizin                    部分可用
  Cutter                   尚未安装（没有 live bridge）
  浏览器运行时             可用
  Frida                    尚未安装
  浏览器 / Electron runtime 可用
  进程捕获                 当前系统不支持

网络与固件
  PCAP / TShark            可用
  mitmproxy                当前系统不支持
  Binwalk / Unblob         当前系统不支持
  sigrok                   当前系统不支持

脚本与格式
  pwntools Python          当前系统不支持
  WABT                     可用
  Wakaru                   当前系统不支持
  objdump                  需要配置
```

用户特别核对的 `objdump` 不是自动安装失败被错误映射成“需要配置”：
`component-manifest.json` 中明确写死 `install_mode: system`、`auto_install: false`，
因此它属于允许显示“需要配置”的既有固定分类。实测
`inspect_with_objdump(operation=architectures)` 仍返回
`capability_unavailable / ERR_REA_WINDOWS_NATIVE`，并提示配置
`REA_OBJDUMP_COMMAND`。

IDA 与 JEB 的“配置”入口均已打开并验证：

- IDA 支持 JSON 文件或粘贴 JSON，不收集账号、密码或许可证密钥。
- JEB 只配置外部 MCP URL，不安装、不启动 JEB，也不处理许可证。

### 实际 REA tools/call 证据

以下调用使用 `state.json` 中同一个 Workswarm REA MCP 配置和受限环境：

| 能力 | 实测结果 | Evidence / 关键值 |
|---|---|---|
| Rizin | `execute_rizin_command(path=target.exe, command=iI)` 成功 | `ev_9eb22c382e7d...`；PE32+ / AMD64；`artifact_changed=false` |
| WABT | `inspect_wasm_artifact(summary)` 成功 | `ev_938b9f3dc771...`；valid，10 sections，1 import，77 exports |
| PCAP 客户端 | `inspect_pcap_client` 成功 | `ev_71cb07d4a23d...`；TShark/Capinfos 4.6.9 均 verified |
| PCAP 实际分析 | 对自建单包 capture 调用 `inspect_pcap_capture(summary)` 成功 | `ev_cbb4f25e91cf...`；1 packet，coverage complete |
| Android | 对 `MotionHealth-debug.apk` 调用 `inspect_android_package` 成功 | `ev_06e622187784...`；JADX 0.7.1，722 classes，23 resources |
| Ghidra | `open_binary(provider_id=ghidra)` + `binary_overview` 成功 | `ev_26a59e1d1e91...`；Ghidra 12.1.4，794 procedures，557 strings |
| 浏览器 | 启动本机 Edge headless 后 `list_browser_targets` 成功 | `ev_2f97d6a29c7d...`；Edg/158.0.4370.33 |
| 浏览器场景 | `capture_browser_scenario` 启动 Edge 并加载 Workswarm 页面成功 | `ev_a52eb9908c2a...`；controlled-replay complete |
| Cutter | 未启动时 `list_cutter_sessions` 返回 `no_live_bridge_found` | 当前不能标为 Workswarm 可用 |
| Cutter 受控启动 | 启动 Cutter 2.5.0、打开 target 后 `cutter_command(iI)` 成功 | `ev_363a617e7b3f...`；证明安装和 bridge 本身有效 |
| Frida | `list_frida_devices` 失败 | `frida_binding.node` 缺失；对应组件必须显示未安装 |

Ghidra 首次打开并完成默认分析约 198 秒，因此“可用”不能通过同步等待完整分析来判定；
卡片只校验 provider 和真实轻量调用，实际反编译按需执行。

### 安装交互

- “去调用”会切回任务页，自动填入 REA 提示词、`reverse-engineer-anything` 技能标签，
  并保留当前可用模型 `deepseek-flash`。
- “暂不安装”只关闭增强安装确认框，不创建作业。
- “继续安装”在已安装/已缓存环境中不需要重新下载；实际约 30 秒到 `11/11` 阶段，
  约 50 秒完成最终 MCP 验证和状态刷新。
- 安装中点击“取消安装”会进入“正在取消”，最终状态为
  “安装已取消，已安装的能力会保留，可以稍后继续”，页面重新可操作且不丢已有能力。
- 取消后再次检测、再次继续安装、完成安装均可重复执行。
- 重启整套前后端后，`mcp.rea.env` 与页面状态仍保持一致。

### 检测性能修正

`_verify_rea_mcp()` 原先把 AgentServer 实际 MCP 校验和 `state.json` 配置记录校验串行执行。
两条校验互不依赖，现改为 `asyncio.gather()` 并发；仍优先采用 configured record，
AgentServer live inventory 继续作为实际可用证据和回退。

同一个 `inspect_inventory` 内的 Frida、Cutter、ADB、Apktool、objdump、Rizin
无目标功能探测也已改为并发。探测都是独立只读调用，结果仍按原组件键归一化，
任一失败仍会单独降级对应能力。

重启加载该改动后，稳定页面的一次完整“检测”实测约 29 秒，取消安装从点击到页面
显示“安装已取消”约 17 秒；页面随后继续完成一次状态刷新，但安装控件已恢复可用。

定向回归：

```text
tests/unit_tests/gateway/test_rea_setup_handlers.py
  12 passed

tests/unit_tests/agentserver/mcp/test_mcp_targeted_control.py
tests/unit_tests/agentserver/mcp/test_command_mcp_handler.py
  37 passed

npm run test:rea-setup-progress
  19 passed
```

REA common 定向测试共 `13 passed, 1 failed`；唯一失败仍是已知的
`test_macos_rizin_uses_private_dylib_launcher` 在 Windows 上无法执行 shell launcher，
与本次并发校验改动无关。

## 2026-11-28 16:00 objdump 受管安装与浏览器假阳性修正

用户确认 `objdump` 可以自动安装到 Workswarm 当前数据目录，并要求一并修正基础安装完成后
“浏览器运行时”和“浏览器 / Electron runtime”提前显示“可用”的问题。

### objdump 安装契约

- GNU objdump 本身不需要账号登录或激活 License，许可证为 GPL/LGPL 组合。
- Windows x64 改为 `managed`：固定使用 MSYS2 GNU Binutils `2.47-5`。
- 其他平台继续使用 `install_mode=system`，保留用户选择本机 `objdump` 的路径。
- Windows 包固定从清华 MSYS2 镜像优先下载，失败时回退到 MSYS2 官方镜像。
- 主包与 4 个最小运行依赖均固定版本、大小和 SHA-256：
  - `mingw-w64-x86_64-binutils-2.47-5`
  - `mingw-w64-x86_64-gettext-runtime-1.0-1`
  - `mingw-w64-x86_64-libiconv-1.19.1-1`
  - `mingw-w64-x86_64-zlib-ng-compat-2.3.3-4`
  - `mingw-w64-x86_64-zstd-1.6.0-1`
- 所有包解压到同一个受管目录：
  `rea/tools/objdump/2.47-5`。
  这样 `objdump.exe` 与 `libintl-8.dll`、`libiconv-2.dll`、`libz-1.dll`、
  `libzstd.dll` 位于同一目录，不修改系统 PATH。
- 每个包内的 `share/licenses` 保留在安装目录，并在 manifest 记录 GPL/LGPL 与
  MSYS2 构建配方来源。
- `tar.zst` 使用 REA 已安装的兼容 Node.js 自带的 `zlib.zstdDecompressSync` 解压，
  不依赖系统是否存在 `zstd.exe` 或 `7z.exe`，再由 Python `tarfile` 做路径越界检查和安全展开。
- `objdump` 已加入 Windows 默认增强安装集合。安装完成后执行：
  `objdump --version`、`inspect_with_objdump(operation=architectures)`，
  只有实际调用成功才显示可用。

干净环境实际安装结果：

```text
component: objdump
state: installed
version: 2.47-5
REA_OBJDUMP_COMMAND:
  <data-root>/profile/agent/workspace/rea/tools/objdump/2.47-5/mingw64/bin/objdump.exe
verification: GNU objdump (GNU Binutils) 2.47.20260726
```

Workswarm 会话实际调用成功：

```text
inspect_with_objdump(operation=architectures)
  Evidence: ev_df2ae05fb4582a28434635849bb6f270ccd6f6ae07357ce23b113be749e0bf12

inspect_with_objdump(operation=file_headers, target.exe)
  Evidence: ev_c640ae6f09a8d65b867b107e839933efed184fe6ecb08ce41e50ad940a29eeea
  Parent:   ev_9e0f5742e7f550a0722975e0e45cf1cfe3867e8bf7f01e218c00294d2fd31f40
```

### 浏览器运行时假阳性

基础环境安装完成后，旧逻辑把 REA 静态 `tool_availability` 中的
`list_browser_targets`、`list_electron_targets` 等工具声明算成能力可用，因此
“浏览器运行时”和“浏览器 / Electron runtime”在没有安装/配置 Chrome 组件时也显示可用。

修正后：

- `chrome` 必须存在实际安装/配置证据，不能只依赖浏览器工具注册。
- 浏览器 / Electron runtime 工具组显式依赖 `chrome` 组件；Chrome 未安装时该组成员显示未安装。
- 修正后在只完成基础环境、未安装增强组件的环境中：
  - 动态分析：未安装
  - 浏览器运行时：尚未安装
  - 浏览器 / Electron runtime：尚未安装
- 安装并配置 Chrome 后，工具组才随浏览器组件进入可用状态。

### 回归

```text
REA focused tests
  11 passed
Gateway REA setup tests
  12 passed
AgentServer MCP targeted tests
  37 passed
Frontend REA progress tests
  19 passed
git diff --check
  passed
```

新增覆盖包括 Windows/Linux objdump 安装模式、`tar.zst` 安全解压、companion 依赖合并、
浏览器工具组必须依赖已安装 Chrome，以及 objdump 已进入 Windows 默认增强安装计划。

## 2026-11-28 16:27 最终回归与运行态复核

本轮补齐测试桩函数对 `node_command` 关键字参数的兼容，并将只在 macOS / POSIX
文件系统语义下成立的三个用例在 Windows 显式跳过，避免把平台限制误报成产品失败。

最终定向验证：

```text
tests/unit_tests/common/test_rea_setup.py
  84 passed, 3 skipped

tests/unit_tests/gateway/test_rea_setup_handlers.py
  12 passed

tests/unit_tests/agentserver/mcp/test_mcp_targeted_control.py
tests/unit_tests/agentserver/mcp/test_command_mcp_handler.py
  37 passed

npm run test:rea-setup-progress
  19 passed

npm run test:rea-setup-file-picker
  3 passed

npm run build
  passed

git diff --check
  passed
```

跳过项分别是 DMG 符号链接语义、POSIX 可执行位和 macOS shell launcher 执行，
均不是 Windows 产品路径。

运行态复核使用隔离环境 `6873/9873/9874/9875`，数据根
`D:\project\test\jiuwenswarm-rea-fresh-20261128_1530`：

- 基础环境为 `3/3 已安装`。
- `mcp.rea.env.REA_OBJDUMP_COMMAND` 指向 Workswarm 数据根下的受管
  `rea/tools/objdump/2.47-5/mingw64/bin/objdump.exe`。
- 页面“脚本与格式”详情中 `objdump` 显示“可用”。
- Workswarm 会话通过已注册的 REA MCP 实际调用成功：
  - `inspect_with_objdump(operation=architectures)`：
    `ev_df2ae05fb4582a28434635849bb6f270ccd6f6ae07357ce23b113be749e0bf12`
  - `inspect_with_objdump(operation=file_headers, target.exe)`：
    `ev_c640ae6f09a8d65b867b107e839933efed184fe6ecb08ce41e50ad940a29eeea`
  - 完整父记录：
    `ev_9e0f5742e7f550a0722975e0e45cf1cfe3867e8bf7f01e218c00294d2fd31f40`
- 页面“动态分析”显示“未安装”，不再因 `list_browser_targets` / `list_electron_targets`
  的静态工具声明或空目标返回，把“浏览器运行时”和“浏览器 / Electron runtime”误报为可用。

因此用户截图中基础安装后出现的两个浏览器成员“可用”是旧误报；当前候选在同样只安装
基础环境和 `objdump` 的状态下已不再出现。`objdump` 的“可用”则有实际 Workswarm → REA
`tools/call` 成功证据，不是仅检测到文件。GNU objdump 不需要登录或 License 激活，
其分发合规由 manifest 记录的 GPL/LGPL 与 MSYS2 构建来源说明承担。

## 2026-11-28 17:13 Rizin 小组件语义与 Frida 自动安装

用户确认：能力卡片仍按成员聚合，但“小组件”本身只要有一个实际支持的能力可用，
小组件就显示“可用”。该规则只对 Rizin 显式启用，不改变其他小组件的判定。

- Rizin 在 Windows 上只要能实际执行 `execute_rizin_command`，即显示可用；
  Linux TTY 专属的持久调试能力不支持，不再把 Rizin 降级为“部分可用”。
- 这个开关是能力卡成员配置 `available_on_any_tool`，不是全局规则。

Frida 之前无法通过“继续安装”修复，根因是它只有能力卡成员和探测工具，没有对应的
manifest 组件，增强安装计划不会包含它。现新增受管 `frida` 组件：

- 固定版本 `frida@17.23.2`，Windows/Linux/macOS 均使用 Frida 官方 NAPI v8
  prebuild，并固定大小与 SHA-256。
- Windows 下载
  `frida-v17.23.2-napi-v8-win32-x64.tar.gz`，校验后只提取
  `build/frida_binding.node` 到 REA 实际加载的 `node_modules/frida/build/`。
- 不修改 REA npm 包版本，不覆盖其他依赖；重复安装时发现有效 binding 直接复用。
- `REA_FRIDA_BINDING` 记录最终 binding 路径；该字段用于状态和审计，不是新的运行依赖。
- 安装时在新 Node 进程中实际 import `frida` 验证 binding；随后由 Gateway 重载
  Workswarm 已注册的 REA MCP，并通过实际 `list_frida_devices` 调用决定最终可用性。
- 安装开始前仍先执行 live MCP 扫描；已可实际调用 Frida 的环境不会重复下载。

本轮验证：

```text
tests/unit_tests/common/test_rea_setup.py
  88 passed, 3 skipped

tests/unit_tests/gateway/test_rea_setup_handlers.py
  12 passed

npm run test:rea-setup-progress
  19 passed
```

在隔离环境 `D:\project\test\jiuwenswarm-rea-clean-20261128_1633` 实际安装结果：

```text
frida: installed
version: 17.23.2
binding:
  C:\Users\admin\AppData\Local\npm-cache\_npx\da7caeae9db9c589\node_modules\frida\build\frida_binding.node
```

使用安装后的同一 REA MCP 实际调用：

```text
list_frida_devices
  ok: true
  reason: available
```

重启完整服务并重新扫描后，页面“动态分析”详情中：

```text
Rizin：可用
Frida：可用
```

Rizin 的 Windows 持久调试子能力仍不支持，但按用户确认的小组件语义，
只要实际支持的单次 CLI 能力可用，Rizin 小组件即显示“可用”。

## 当前工作区与交付状态

- 工作区：
  `C:\Users\admin\.codex\worktrees\fix-rea-cancel-state\jiuwenswarm_rea_ready`
- 分支：`fix/rea-offline-install-recovery`
- develop PR：`!11128`，基于最新 `origin/develop`，单 commit。
- releases/0.2.10 PR：`!11130`，基于最新 `origin/releases/0.2.10`，单 commit。
- 两个分支均已推送；两个 PR 都只包含本次 REA 修复 commit。
- 不要影响用户正在运行的真实服务：`localhost:6173`、`localhost:9173`。
- 当前隔离验收实例：前端 `6873`、Gateway `9873`、WebChannel `9874`、
  AgentServer `9875`，数据目录
  `D:\project\test\jiuwenswarm-rea-fresh-20261128_1530`。
- 另有一套未安装 REA 的干净环境：前端 `6973`、Gateway `9973`、WebChannel `9974`、
  AgentServer `9975`，数据目录
  `D:\project\test\jiuwenswarm-rea-final-20261128_1615`。
- 上节 `objdump` 受管安装和浏览器假阳性修正目前仍是本地未提交改动；
  `!11128`、`!11130` 只包含此前已推送的修复，不包含这些新增改动。

## 后续验证重点

1. 干净环境没有登记任何增强工具变量时，Ghidra、Java、JADX 等不能因宿主环境误显示可用。
2. 安装或配置组件后，`state.json` 的 `mcp.rea.env` 必须更新，实际 AgentServer MCP 必须重载。
3. 重载后通过实际 MCP 执行 `binary_session`，只有工具合同与实际可用性均通过才显示可用。
4. 基础安装期间增强卡片不统一显示“安装中”；基础完成后重新扫描增强能力。
5. 重新构建前端后，人工检查卡片状态、成员状态和检测按钮 busy 状态。
