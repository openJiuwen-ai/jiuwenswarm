# JiuwenSwarm HarmonyOS 开发功能可执行试验说明书

## 1. 文档用途

本文用于检验当前分支已经实现的 HarmonyOS 开发辅助能力，供开发、测试、评审和演示人员逐项执行并记录结果。

当前可试验的入口只有：

- `/harmonyos-dev-init`：检查、安装或更新 `devecocli`，强制刷新 `deveco-cli` Skill，校验或升级 `harmonyos-dev-suite`，并可选配置华为官方 HarmonyOS Developer Knowledge MCP。
- `/harmonyos-project-init [absolute-project-path]`：只读识别 HarmonyOS 工程，切换到 `code.normal`，激活工程作用域、持久化工程上下文，并由 TUI 构建内部初始化提示词发给当前 Agent。
- `/skills use harmonyos-dev-suite, <任务>`：让 Agent 按已安装的聚合 Skill 处理具体 HarmonyOS 开发请求。

前两条 Slash Command 默认不注册。只有在启动 TUI 前设置 `JIUWENSWARM_TUI_HARMONYOS_ENABLED=1`，它们才会出现在帮助、补全和命令执行入口中；修改环境变量后需要重启 TUI。

> 重要：自动化测试通过只说明代码路径通过测试，不等于已在真实 DevEco Studio、真实工程或真实设备上完成验证。本文会分别记录这两类结果。

## 2. 试验影响与安全边界

执行前请知悉：

- 当本机没有 `devecocli` 且用户明确确认时，会执行 `npm install -g @deveco/deveco-cli@latest`，并附加关闭 audit/fund、限制网络请求等待与重试次数的 npm 参数。该操作需要网络，会修改全局 npm 环境，并存在 `latest` 版本漂移和全局目录权限风险。
- 当本机已有 `devecocli` 时，会先显示当前版本并询问是否执行 `devecocli update`；默认项是更新，也可选择保留当前版本。更新同样需要网络并会修改全局安装。
- 基础 Skill 使用 `devecocli init --skill --path <dir> --force` 刷新。内置 Suite 使用受管版本和目录摘要；只有未修改的受管旧版会原子升级，未知或被修改的目录会报告冲突而不会被静默覆盖。
- Skill 会写入 JiuwenSwarm 自身的数据目录，不会写入当前 HarmonyOS 工程。
- 官方知识 MCP 是远程服务；配置后，搜索词和 MCP 请求会发送到华为托管的服务。
- `/harmonyos-project-init` 会读取工程描述文件并切换当前 TUI 的 mode/workspace，但不修改工程源码。
- 工程上下文写入 `<JIUWENSWARM_DATA_DIR>/agent/workspace/harmonyos-projects/`，不写入用户工程目录。
- 工程初始化不会修改共享 Agent/Swarm 提示词，也不会自动新增共享或全局 MCP 配置；HarmonyOS 元数据只通过当前 TUI 会话的内部消息传递。

在日常开发机上，不要为了试验“未安装”分支而卸载正在使用的 `devecocli`。TEST-02 和 TEST-03 应在干净虚拟机、测试账号或可丢弃的隔离环境中执行。

## 3. 环境与版本记录

### 3.1 基础环境

- macOS 或 Windows。
- Python 3.11、3.12 或 3.13。
- `uv`。
- Node.js 18 或更高版本。
- `npm`。
- 可访问 npm registry；试验官方知识 MCP 时还需能访问华为远程服务。
- TEST-07、TEST-09、TEST-10 需要一个真实、可正常打开的 HarmonyOS 工程。

本文 shell 命令以 macOS/zsh 为例。Windows 可使用 PowerShell 执行等价命令；实现会识别 Windows 的 `.cmd`/`.bat` 可执行文件。

### 3.2 记录被测版本

在仓库根目录执行：

```bash
git branch --show-current
git rev-parse --short HEAD
git status --short
python --version
uv --version
node --version
npm --version
command -v devecocli || true
devecocli --version || true
```

当前上游合入分支名称为 `feature/harmonyos-dev-workflow`，符合贡献指南的 `feature/` 分支命名规范；实际交付时仍以负责人给出的分支和提交号为准。若 `git status --short` 非空，必须把未提交文件清单附在试验记录中，避免把工作区修改误认为某个提交已经包含的内容。

## 4. 启动被测程序

切换分支或更新代码后，应同时重启 Gateway 和 TUI，避免前端存在命令、旧 Gateway 却返回 `unknown method: harmonyos.dev_init`。

### 4.1 终端 A：启动 Gateway

在仓库根目录执行：

```bash
uv sync
uv run jiuwenswarm-init
uv run jiuwenswarm-start
```

`jiuwenswarm-init` 只需在该 JiuwenSwarm 数据目录尚未初始化时执行。Gateway 默认监听 `ws://127.0.0.1:19001/tui`。

### 4.2 终端 B：启动源码 TUI

```bash
cd jiuwenswarm/channels/tui/frontend
npm install
JIUWENSWARM_TUI_HARMONYOS_ENABLED=1 npm run dev
```

Windows PowerShell 使用：`$env:JIUWENSWARM_TUI_HARMONYOS_ENABLED="1"; npm run dev`。

进入 TUI 后先确认 `/harmonyos-dev-init` 和 `/harmonyos-project-init` 能被 Slash Command 补全。

## 5. 自动化基线检查

### TEST-00：代码级回归测试

目的：在进行人工交互试验前，确认 HarmonyOS 后端、工程识别和 TUI 交互回归测试通过。

前端测试：

```bash
cd jiuwenswarm/channels/tui/frontend
npm run build
npm test
```

后端测试（回到仓库根目录执行）：

```bash
TEST_TMP="$(mktemp -d)"
JIUWENSWARM_DATA_DIR="$TEST_TMP/data" \
UV_CACHE_DIR="$TEST_TMP/uv-cache" \
uv run --no-sync python -m pytest \
  tests/unit_tests/gateway/test_tui_harmonyos_project.py \
  tests/unit_tests/gateway/test_harmonyos_dev.py \
  tests/unit_tests/gateway/test_cli_channel_handlers.py \
  tests/agents/swarm/test_swarm_assembly.py::test_enrich_team_spec_for_swarm_injects_config_mcp_servers \
  -q --no-cov
```

通过标准：

- 两条测试命令退出码均为 0。
- TUI 测试覆盖安装、更新和知识 MCP 确认框的默认首项、Kitty Enter repeat/release 抑制，以及用户新按普通 Enter 或 Kitty Enter press 后确认。
- 后端测试覆盖安装/更新确认、Node.js/npm 前置检查、强制刷新、受管 Suite 升级、TUI 工程上下文过期刷新和工程识别；Swarm 既有测试确认 `team`、`code.team`、`team.plan` 的 MCP 装配不依赖 HarmonyOS 项目路径改动。

结果只能记录为“自动化基线通过”，不能替代后续真实 TUI 试验。

## 6. `/harmonyos-dev-init` 试验

### TEST-01：本机已有 `devecocli`

前置条件：

```bash
command -v devecocli
devecocli --version
```

在 TUI 中执行：

```text
/harmonyos-dev-init
```

预期结果：

- 不出现全局 npm 安装确认框。
- 出现 `Update DevEco CLI` 确认框，展示当前版本和 `devecocli update`；默认高亮 `Update devecocli`。残留 Enter repeat/release 不得自动批准，用户新按 Enter 后才执行更新。
- 更新完成后重新读取 `devecocli --version`，再执行 `devecocli init --skill --path <JiuwenSwarm skills dir> --force`。
- 校验、复用或原子升级受管的 `harmonyos-dev-suite`；修改版或未知目录必须报告冲突。
- 最终 `HarmonyOS Dev Init` 报告至少满足：
  - `ok=true`
  - `install_attempted=false`
  - `update_attempted=true`
  - `update_result=ok`
  - `init_attempted=true`
  - `init_result=ok`
  - `suite_attempted=true`
  - `suite_result=ok`
  - `base_skill=verified`
  - `suite_skill=verified`
  - `skill_verified=true`
- 如果官方知识 MCP 尚不存在，随后出现 TEST-04 的第二个确认框；该确认框是可选能力，不改变本项核心初始化的判定。

若报告提示新 Skill 未显示，执行：

```text
/reload-plugins
/skills list
```

### TEST-02：缺少 `devecocli`，用户重新按 Enter 安装

前置条件：在隔离环境中确认 `devecocli` 不存在，但 Node.js 版本不低于 18 且 `npm` 可用。

```bash
command -v devecocli || true
node --version
npm --version
```

在 TUI 中执行：

```text
/harmonyos-dev-init
```

确认框验收步骤：

1. 确认屏幕出现 `Install DevEco CLI`，并展示以 `npm install -g @deveco/deveco-cli@latest` 开头的完整命令及网络、全局环境和 `latest` 风险；命令应包含 `--no-audit`、`--no-fund`、`--fetch-timeout=30000` 和 `--fetch-retries=1`。
2. 确认默认高亮项是 `Install devecocli`，不是 `Cancel`。
3. 确认框刚出现时先不要操作；提交 Slash Command 后残留的 Kitty Enter repeat/release 事件不得自动批准安装，确认框应继续停留。
4. 用户重新按一次普通 Enter 或 Kitty Enter press。
5. 此时才应开始全局安装，并继续初始化 Skills。
6. 安装阶段应每 30 秒报告一次已耗时，并提示可按 Esc 或 Ctrl+C 取消；后端安装进程的实际硬超时为 5 分钟，不得一直停留在没有耗时和退出说明的静态提示上。

最终通过标准：

- `install_attempted=true`。
- `install_result=ok`。
- `install_command` 对应确认框展示的完整 npm 命令。
- 新安装已经使用 `@latest`，不应再次调用 `devecocli update`。
- `init_command` 包含 `--force`。
- `devecocli`、`init_result`、`suite_result` 和 `skill_verified` 均成功。

如果 npm 进程超过 5 分钟，程序必须终止其完整进程组并返回包含 `npm ping` 的可操作超时信息，不得继续执行 Skill 初始化。其他安装失败应记录 `returncode`、错误、权限和网络信息。本项判为 FAIL，但普通 JiuwenSwarm 功能不应受影响。

### TEST-03：缺少 `devecocli`，用户取消安装

前置条件与 TEST-02 相同。

1. 执行 `/harmonyos-dev-init`。
2. 在确认框按向下键选中 `Cancel`，再按 Enter。

预期结果：

- 显示 `HarmonyOS Dev init cancelled. devecocli was not installed.`。
- 不执行 npm 全局安装。
- 不继续初始化 HarmonyOS Skills。
- 再次执行命令时仍应重新请求确认。

### TEST-04：配置官方 HarmonyOS Developer Knowledge MCP

前置条件：核心初始化成功，且当前没有名为 `harmonyos_developer_knowledge` 的 MCP 配置。只在允许访问远程华为服务的环境中执行。

```text
/mcp show harmonyos_developer_knowledge
/harmonyos-dev-init
```

确认框验收步骤：

1. 确认显示远程地址 `https://connect-api.cloud.huawei.com/api/developerknowledge/mcp`。
2. 确认明确提示搜索词和 MCP 请求会发送到华为托管服务。
3. 确认默认高亮项是 `Configure MCP`，不是 `Skip`。
4. 确认框刚出现时先不要操作；残留的 Kitty Enter repeat/release 不得自动配置。
5. 用户重新按一次普通 Enter 或 Kitty Enter press 后才开始配置。

配置后执行：

```text
/mcp show harmonyos_developer_knowledge
```

通过标准：

- `knowledge_mcp=configured`。
- `knowledge_mcp_server=harmonyos_developer_knowledge`。
- `knowledge_mcp_tools` 同时包含 `searchDocuments` 和 `getDocumentsById`。
- MCP 为启用状态，transport 为 `streamable-http`，URL 与上述华为地址一致。

如果服务不可达、工具缺失或工具数为 0，应记录 `knowledge_mcp=blocked` 和 `knowledge_mcp_error`。这只使 MCP 子项失败，不应把已经成功的 CLI/Skills 初始化改判为失败。

### TEST-05：跳过官方知识 MCP

前置条件：当前没有同名 MCP 配置，核心初始化能够成功。

1. 执行 `/harmonyos-dev-init`。
2. 在 `Official Knowledge MCP` 确认框按向下键选择 `Skip`，再按 Enter。

预期结果：

- 核心报告仍为 `ok=true`。
- `knowledge_mcp=declined`。
- `/mcp show harmonyos_developer_knowledge` 不应出现新增配置。

### TEST-06：MCP 重复执行、禁用和冲突

本项中的冲突试验会改动 MCP 配置，只能在隔离数据目录中执行。

1. 已正常配置时再次执行 `/harmonyos-dev-init`：不应重复询问或新增第二个服务，结果应为 `already_configured`，两个预期工具仍存在。
2. 执行 `/mcp disable harmonyos_developer_knowledge` 后再次执行：结果应为 `disabled`，程序不得自动重新启用。试验后可用 `/mcp enable harmonyos_developer_knowledge` 恢复。
3. 冲突试验：先删除测试配置，再创建同名但不同 URL 的配置，然后执行初始化：

```text
/mcp remove harmonyos_developer_knowledge
/mcp add --name harmonyos_developer_knowledge --transport streamable-http --url http://127.0.0.1:9/mcp
/harmonyos-dev-init
/mcp show harmonyos_developer_knowledge
```

预期结果：

- 报告 `knowledge_mcp=conflict`。
- 原有测试 URL 不被静默覆盖。
- 核心 CLI/Skills 初始化仍为成功。

冲突试验完成后执行：

```text
/mcp remove harmonyos_developer_knowledge
```

## 7. `/harmonyos-project-init` 试验

### TEST-07：真实 HarmonyOS 工程初始化

前置条件：准备一个真实 HarmonyOS 工程，至少包含 `build-profile.json5`、`oh-package.json5`、`AppScope/app.json5` 和模块的 `src/main/module.json5`。优先使用可被 DevEco Studio 正常打开的工程，不使用手工拼装的不完整目录。

先记录工程状态：

```bash
HARMONY_PROJECT="/absolute/path/to/HarmonyProject"
git -C "$HARMONY_PROJECT" status --short
```

在 TUI 中使用同一绝对路径执行：

```text
/harmonyos-project-init /absolute/path/to/HarmonyProject
```

检查 `HarmonyOS Project Init` 报告：

- `project` 和 `root` 正确，`root` 为工程绝对路径。
- `bundle_name`、`product`、`module`、`ability` 与工程描述文件一致；若存在多个候选，报告应明确显示 ambiguity，而不是随意选择。
- `mode=code.normal`。
- `devecocli` 显示实际版本与路径。
- `context_state` 位于 JiuwenSwarm 数据目录，不在 HarmonyOS 工程内。
- `source_files` 只列出实际读取过的工程描述文件。
- 持久化上下文包含描述文件指纹；后续描述文件变化时再次执行命令应重新识别，而不是继续发送旧 module/ability。
- `/mode` 显示当前为 `code.normal`，`/workspace get` 显示当前工程作用域。
- TUI 自动发送一条不展示为普通用户输入的内部初始化提示词；Agent 只确认工程根目录、module/Ability、ambiguity 和 `devecocli` 可用性，然后等待用户任务，不执行构建、安装、设备访问或文件修改。
- `/mcp list` 与执行前一致；命令不会创建 `deveco-mcp-<project-id>` 或其他项目级/全局 MCP 条目。

再次检查工程状态：

```bash
git -C "$HARMONY_PROJECT" status --short
```

通过标准：前后状态一致，工程识别没有修改用户工程；当前 TUI 会话收到初始化上下文，共享 MCP 配置和公共 Agent/Swarm 行为保持不变。

### TEST-08：拒绝非 HarmonyOS 目录

先记录当前状态：

```text
/mode
/workspace get
```

创建一个空目录，并把实际输出的绝对路径填入 TUI：

```bash
mktemp -d
```

```text
/harmonyos-project-init /absolute/path/to/empty-directory
```

预期结果：

- 返回“不是 HarmonyOS 工程”或缺少必要描述文件的明确错误。
- 不切换 mode。
- 不改变 workspace。
- 不创建共享 MCP 配置，也不写入该空目录。

执行 `/mode` 和 `/workspace get` 与试验前记录对比。

### TEST-09：工程初始化幂等性

对同一个真实工程连续执行两次：

```text
/harmonyos-project-init /absolute/path/to/HarmonyProject
/harmonyos-project-init /absolute/path/to/HarmonyProject
```

预期结果：

- 两次识别出的 project id、root、product、module 和 ability 一致。
- 更新同一个工程上下文状态文件，不产生重复快照语义。
- 每次命令只向当前 TUI 会话发送一条内部初始化提示词，不向后续普通请求追加公共层提示词。
- 执行前后的 `/mcp list` 一致，Swarm 和其他 channel 不受影响。
- 第二次执行仍保持工程目录只读。

## 8. Skill 使用试验

### TEST-10：调用 `harmonyos-dev-suite`

先执行：

```text
/reload-plugins
/skills list
```

确认 `deveco-cli` 和 `harmonyos-dev-suite` 已安装，再执行：

```text
/skills use harmonyos-dev-suite, 检查当前鸿蒙工程结构并说明模块、Ability 和可用验证方式
```

通过标准：

- 不出现 `Unknown command`、Skill 未安装或离线错误。
- Agent 能结合当前工程上下文说明 module、Ability 和可用验证路径。
- Agent 的回答与当前工程上下文和实际工具调用证据一致。

该项依赖所使用的模型和工程内容，应保存完整输入、回答与相关工具调用，单独标记为“Agent/Skill 真实调用试验”。

## 9. 常见问题排查

| 现象 | 可能原因与处理 |
| --- | --- |
| `harmonyos-dev-init failed: unknown method: harmonyos.dev_init` | TUI 与 Gateway 代码版本不一致，后端 RPC 文件或注册未同步，或仍在运行旧 Gateway。确认分支后同时重启 Gateway 和 TUI。 |
| `tsc: command not found` | TUI 前端依赖未安装。在 `jiuwenswarm/channels/tui/frontend` 执行 `npm install`。 |
| Node.js 版本过低 | 手工安装或升级到 Node.js 18 以上；程序不会自动安装 Node.js。 |
| npm 全局安装权限失败 | 检查 npm prefix 和当前账号权限；不要默认使用 `sudo`，先按本机 npm 管理方式处理。 |
| npm 下载失败 | 检查网络、代理、registry 和证书配置，保存 stderr 后重试。 |
| Skill 初始化成功但列表未刷新 | 执行 `/reload-plugins`，仍无效时重启 TUI。 |
| `knowledge_mcp=blocked` | 用 `/mcp show harmonyos_developer_knowledge` 检查 URL、启用状态、工具数和错误；确认远程服务可访问。 |
| 官方知识 MCP 缺少工具 | 只有同时发现 `searchDocuments`、`getDocumentsById` 才通过。`tool_count=0` 或缺少任一工具均不能判定成功。 |
| 执行工程初始化后出现 `deveco-mcp-<project-id>` | 当前运行的 TUI/Gateway 仍是旧版本，或配置中存在历史条目。重启新版本并人工检查后再决定是否删除；当前实现不会自动创建该条目。 |
| 项目识别结果不完整 | 检查真实工程的 `build-profile.json5`、`oh-package.json5`、`AppScope/app.json5` 和模块 `module.json5`，同时查看 notices/ambiguities。 |

## 10. 清理与回滚

只执行与本次试验实际改动对应的命令：

```text
/mcp disable harmonyos_developer_knowledge
/mcp remove harmonyos_developer_knowledge
/skills uninstall harmonyos-dev-suite
/skills uninstall deveco-cli
```

如果本次试验确实新装了全局 `devecocli`，且确认本机其他项目不依赖它，再在 shell 中执行：

```bash
npm uninstall -g @deveco/deveco-cli
```

项目初始化返回的 `context_state` 是单个状态文件。若必须清理，应先核对它确实位于 JiuwenSwarm 数据目录的 `agent/workspace/harmonyos-projects/` 下，再只删除报告中显示的那个精确文件；不要递归删除整个 JiuwenSwarm 数据目录。

如需恢复试验前的 TUI 状态，可执行：

```text
/mode <试验前记录的模式>
/workspace set <试验前记录的路径>
```

## 11. 结果记录模板

每个执行人复制以下模板填写：

```markdown
### 试验记录

- 执行人：
- 日期：
- 操作系统：
- 分支：
- 提交号：
- `git status --short`：
- Python / uv：
- Node.js / npm：
- devecocli 版本与路径：
- DevEco Studio 版本：
- HarmonyOS 工程与提交号：

| 编号 | 试验项 | 预期 | 实际 | 结果（PASS/FAIL/BLOCKED/N/A） | 截图或日志 |
| --- | --- | --- | --- | --- | --- |
| TEST-00 | 自动化基线 | 前后端测试通过 |  |  |  |
| TEST-01 | 已安装 devecocli | Skills 初始化成功 |  |  |  |
| TEST-02 | 新 Enter 确认安装 | 残留事件不批准，新 Enter 才安装 |  |  |  |
| TEST-03 | 取消安装 | 不执行全局安装 |  |  |  |
| TEST-04 | 配置官方知识 MCP | 两个预期工具可用 |  |  |  |
| TEST-05 | 跳过官方知识 MCP | 核心初始化仍成功 |  |  |  |
| TEST-06 | MCP 状态与幂等 | 不覆盖、不重复、保持禁用 |  |  |  |
| TEST-07 | 真实工程初始化 | 识别正确且工程无写入 |  |  |  |
| TEST-08 | 非鸿蒙目录 | 拒绝且不改变状态 |  |  |  |
| TEST-09 | 工程幂等 | 上下文稳定且共享 MCP 不变 |  |  |  |
| TEST-10 | Skill 真实调用 | 正确使用当前工程上下文 |  |  |  |

- 核心 CLI/Skills 结论：
- 官方知识 MCP 结论：
- 工程识别结论：
- TUI 内部提示词与公共层隔离结论：
- Agent/Skill 调用结论：
- 已知限制与阻塞：
- 是否建议进入下一阶段：
```

最终报告必须分别给出“自动化基线”“真实 TUI 交互”“远程知识 MCP”“真实工程识别/上下文隔离”和“Agent/Skill 调用”的结论，不能用一个总 PASS 掩盖未执行或被阻塞的子项。
---

## 返回导航

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)