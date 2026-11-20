---
id: F_20261119_1624_beta3-extensions-develop-port
title: beta3 扩展能力线性迁入 develop
type: F
author: lizhiyuan
author_email: lizhiyuan65@huawei.com
modules:
  - jiuwenswarm/extensions_v2
  - jiuwenswarm/server
  - jiuwenswarm/common
  - jiuwenswarm/channels/web
  - jiuwenswarm/channels/desktop
files:
  - jiuwenswarm/extensions_v2/assembly.py
  - jiuwenswarm/channels/cli/extension.py
  - jiuwenswarm/channels/cli/presentation.py
  - jiuwenswarm/server/worker/lifecycle.py
  - jiuwenswarm/channels/web/app_web.py
  - jiuwenswarm/common/builtin_skills.py
  - jiuwenswarm/common/system_plugin_recovery.py
  - jiuwenswarm/common/system_plugin_recovery_sidecar.py
  - jiuwenswarm/common/process_supervision.py
  - jiuwenswarm/channels/web/frontend/src/features/webui/react/WebUIAppRoot.tsx
  - jiuwenswarm/channels/desktop/electron/main.cjs
  - pyproject.toml
  - uv.lock
designs: []
design_note: 按用户明确确认的逐笔迁移、保留 develop 行为和 Gateway 跨仓边界执行；本次没有另立设计，原扩展契约沿用模块内规约。
---

# beta3 扩展能力线性迁入 develop

## 范围与依据

日期：2026-11-19（Asia/Singapore）。本记录维护本次分支迁移的实施与验证；不替代原扩展设计或 develop 既有能力规约。

- 目标基线为 `origin/develop` 的 `4b1b4fcff703578308871a5828d338b90797c05d`。
- 来源为 beta3 的 `75b0cbe3f4dc6e1cbe1c33c99e051afd087b1042`（含）至 `e4d3b811c5289a4181b26e9f907b535a707ecc1c` 的 160 个可达提交。
- 分支为 `codex/beta3-extensions-to-develop-20261119`。每笔保留作者、作者时间、原消息及 cherry-pick 来源，迁移后历史单父；已有等价内容和生成冲突清理保留空来源记录，不重复施加旧实现。
- 扩展契约以 `jiuwenswarm/extensions_v2/docs/specs/` 为准；还原历史依据为 `F_35_system-plugin-recovery.md`。
- 内置技能沿用 [S_01_builtin-catalog](../specs/jiuwenswarm/server/runtime/skill/builtin-catalog.md)，原 catalog 决策仍有效。

## 实施决策与拒绝的方案

1. 从 develop 建隔离工作树后逐笔迁移，不用双父 merge 或 squash。160 笔中 10 笔内容已等价包含，仍保留单独来源记录。逐笔核验作者、时间、来源与顺序无差异。原工作树和用户未跟踪文件保留。
2. 保留 develop 的 Worker/Front 拆分、JiuwenMate 初始化与重载、多模态适配器、权限与模型配置、企业租户鉴权及 HTTP/SSE；扩展只接入对应现有入口。不把 beta3 的旧整文件覆盖 develop，也不为已移出的 Gateway 恢复本仓兼容副本。
3. Gateway 使用 `agent_gateway` 和真实 e2a 公共入口。Python 依赖只将 Gateway 从 `d68e80bf` 改为用户指定 `e95c5c21`；pyproject 与 uv.lock 各两行，其余 340 包的锁定版本沿用 develop。前端新增 antd 所需闭包，保留 develop 原有包版本。
4. WebUI 仅迁入 beta3 已有 WebSocket 扩展能力；HTTP/SSE 继续原流程。共享 WS 用连接所有权管理挂载/卸载，显式重连和鉴权切换保留原行为。资源读取和界面宿主不能使普通 HTTP 页面依赖 WS-only API。恢复路由遵循 BASE_URL，显式恢复入口不会被持久 JiuwenMate 模式遮蔽。
5. 普通服务重启沿用 develop。还原专门等待 Gateway、Front 和所属 Worker 退出后再启动新后端；所属 Worker 通过既有所有权、退出回执及关停协议确认，失败不继续启动，不恢复 beta3 递归终止整棵进程树。Web 页面和恢复 sidecar 保持运行。
6. 新系统插件开发技能接入 workswarm-builtin 统一 catalog，不恢复旧独立技能安装列表。升级只补缺失资源；已有能力目录和用户修改的导航保留。
7. 新恢复 HTTP 入口实测接受了不可信 Host/Origin；修复只作用于新增入口，要求唯一回环 Host 和实际端口，同源 Origin，以及还原 POST 的 JSON 类型。令牌和桌面鉴权仍保留，拒绝请求不能读取私有 endpoint 或执行 restore。不改变 develop 普通 API/企业路由。
8. 纯 Gateway 实现测试由外仓负责；本仓保留实际 Swarm CLI、代理、打包及混合 AgentServer 契约测试。只对 e95 中确实不存在的接口对应用例定点延期，不吞掉其它 ImportError，不伪造实现或整体跳过 Swarm 测试。

9. CLI 初次读取 SDK 配置的日志曾污染 JSON stdout；只在执行期间把诊断输出转到 stderr，保留最终 JSON 与标准 --help 的 stdout，真实子进程覆盖这两个出口。

## Gateway 当前边界

已核实 e95 包含部分 V2 实现，但缺少生产装配和部分 WebUI、包管理、资源 HTTP 入口。用户后来明确要求 Gateway 暂不修复，等待同事合入后提供新提交号。该决定替代先前允许跨仓补齐的授权。

本分支不修改或提交 Gateway 仓，不将本地未完成候选作为运行依赖。现有 Gateway 功能按依赖提供；缺失入口相关扩展能力不能宣称已端到端可用。后续须更新 pyproject/lock 中 Gateway 引用，重新验证 WS 扩展激活、包管理、插件资源上传下载、还原后的状态刷新，以及 HTTP/SSE 原功能。

## 实际验证

环境：Windows，独立 CPython 3.12.13 环境，锁定 SDK `f5ecfd7435400fafc4e4b4d399c921a134c4358c`、Gateway e95 和 e2a `defa6d8d8904eb3272a58fa0ad13f94f7adb4a51`；源码通过 PYTHONPATH 指向隔离工作树。前端使用 Node 24.12 与 develop 依赖加 antd 闭包。不是 editable 外仓候选环境。

- 历史核验：160/160 来源、顺序、作者与作者时间一致，新增范围无 merge commit。暂存候选相对 develop 没有删除任何既有文件；最终适配提交的 16 个纯 Gateway 测试删除均为本次来源分支新增内容，不涉及 develop 原测试。
- `uv lock --check --offline`：通过，340 包。
- `python -m pytest tests/unit_tests/channels/web/test_system_plugin_recovery_http.py tests/unit_tests/common/test_system_plugin_recovery.py -q -o addopts= --no-cov -p no:cacheprovider`：46 通过。使用真实 HTTP 请求解析器和内存连接，覆盖合法本地恢复/历史、非法 Host/Origin/类型、令牌及拒绝前无副作用；未启动实际业务服务。
- 前端 24 组定向行为测试 1079 通过；包括 HTTP/SSE 29、设置 75、App Market 61、WS ACK/恢复与 lease 20、WebUI 客户端 38，以及 JiuwenMate、群聊、鉴权、资源和还原路径。最终源码直接 Vite 打包成功；未运行 tsc。
- Electron 生命周期 Node 测试 97 通过、4 项既有环境跳过；Python 进程/打包相关共 162 项经原集及失败范围复核通过，7 项 macOS 平台跳过。
- Swarm CLI、本地传输、跨仓控制和启动链 53 个独立相关用例通过；最终受影响的 4 个真实子进程用例再次通过，含 JSON stdout 和两种 --help。
- Web handler 全文件 168 通过；测试输入按 e95 的应用级 A4P 配置、会话级授权和非法身份拒绝契约适配。其它 Server/鉴权/租户/代理 14 文件在初轮 319 项通过；后续一次 Windows 原子写压力复核出现 sharing violation，生产与对应测试对 develop 基线均无差异，未扩大修改原子写逻辑。
- Runtime resource kernel 23 通过；仅修正测试用生产 opaque 租户键与 Windows normcase，生产内核未改。
- catalog、utils、模型编译和 beta3 组合契约原集 110 通过，新增两项真实 beta3 独立技能副本迁移回归也通过。独立检视发现的指纹问题已关闭：完整 15 文件摘要为 `251910f70c977d457205ba2a7c898b3b9f04172a392660e6bac6e14fa45fb0e8`；不保留从长路径漏读得到的不完整摘要作为可信值。原版归档且保留禁用偏好，用户改版保留 local 身份与内容。实际资源示例打包、loader 和 UI 相关 28 项通过。
- V2 主仓组完整运行 1280 通过、16 跳过、1 fixture 失败；按 SDK 实际 CORE 视图改用 `build_core` 后，受影响 visibility 全文件 36 通过。新增缺依赖保护回归后，最终独立覆盖为 1282 通过、16 跳过；跳过仅含 13 个已知 Gateway 缺接口用例及 3 个 Windows 平台能力限制。
- Worker 23 文件完整运行 343 通过、10 平台跳过、1 fixture 失败；受影响 owner 完整文件 11 项复核通过，最终独立覆盖为 344 通过、10 跳过。使用 Windows 扩展路径消除隔离工作树超长路径影响，未因此修改 Worker 生产扫描逻辑。
- develop Runtime 原组 980 通过，保留 1 个 Windows 原生缺 `fcntl` 的基线失败；另有控制交接范围 10 项通过，2 个已证实时钟并列相关基线场景单独保留诊断，不算通过。
- JiuwenMate、多模态、模型、权限及工作目录组原运行 1014 通过、14 跳过、16 失败；明确的 Windows 符号链接权限、路径表示和 fixture 签名/原始字节问题经定向复核，最终按真实 nodeid 核对为 1026 通过、17 跳过、1 个固定 SDK 的 Windows grep 命令失败。两个相关 Native/Multimodal 文件最终 120 通过、1 既有 POSIX 跳过、1 已有 SDK 失败，未改变产品实现或绕过原通路。
- 真实 wheel 构建及独立验包通过：无内部 Gateway 或嵌入外包副本，依赖固定 e95，包含 Swarm V2、恢复 sidecar 与 Worker identity。该后端 wheel 未包含前端 dist，不是完整桌面安装包。
- 证据由完整组运行加受影响完整文件复核组成；没有把修复前的失败整轮标为通过。精确范围、日志和已知限制见同级任务目录的 `python-behavior-validation-matrix.json`、`frontend-final-summary.json`。
- `jw-review` 限定检视覆盖恢复 HTTP 边界、普通/专用重启生命周期和 catalog 用户资产迁移。恢复范围未发现可确认问题；catalog 唯一 P2 经真实摘要和新增回归修复后复核关闭。没有宣称全项目或延期 Gateway 生产装配已验收。

验证日志和来源核验结果保存在本次隔离工作树的同级任务目录，未提交测试日志、覆盖率或真实配置。禁止的格式化、静态检查没有执行。

## 已知遗留与验收边界

- Gateway 新接口按用户决定延期，更新依赖后须重新进行实际跨仓装配和浏览器业务验收。
- Windows 现有 Coordinator 在同 tick 时间戳下的控制 owner 选择存在基线不稳定：实际选择器 300 次对照中，基线错选 144 次、候选错选 142 次，归一化源码完全相同；候选真实控制流 24 次中 10 次等待错误 owner。metadata 原子写压力复核也出现 Windows sharing violation，原生产和测试均与基线相同。
- Windows 原生缺 `fcntl` 使现有 AgentServer remote-service 启动用例失败；固定 f5 SDK 的 GrepTool 以带引号的 rg 路径直接调用 PowerShell，缺调用运算符，保留对应失败。以上均未改生产、未添加跳过掩盖，后续修复需独立范围。
- Windows 本地测试与模拟子进程不证明 macOS/Linux 打包和真实进程重启；相应平台需要独立验收。
- 本次不是全部项目功能或真实模型回放验收。未以 ACK、单测成功或源码存在替代实际插件启动、页面显示和进程退出证据。
