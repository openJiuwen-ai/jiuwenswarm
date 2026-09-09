# Slash 命令速查表

本文档按**解析位置**拆分：`TUI 本地解析` 与 `Gateway / Agent 侧解析`。  
用于快速查阅当前行为，最终实现以代码为准。

---

## 一览：按解析侧区分

### TUI 本地解析（CLI 内置）

在终端 UI 本地执行，不走 Gateway 受控命令管线。

| 命令 | 说明 |
|---|---|
| `/clear` | 清屏 |
| `/color` | 调整 TUI 配色 |
| `/copy` | 复制上一条消息 |
| `/exit` | 退出 |
| `/help` | 查看可用命令 |
| `/keybindings` | 查看/编辑/重置 TUI 快捷键（别名 `/keybind`） |
| `/theme` | 切换主题 |
| `/config` | 修改配置（当前为本地实现，后续计划统一到 Gateway） |
| `/context` | 查看上下文窗口占用与 Token 用量明细（见下文） |
| `/workspace` | 管理可信目录（见下文） |
| `/teamskills` | TeamSkills 管理（`init/validate/pack/info/search/list/install/uninstall/config/publish/delete`） |
| `/export` | 导出当前会话到文件或剪贴板（见下文） |
| `/status` | 查看 jiuwenswarm 运行状态概览、用量统计、配置编辑（见下文） |
| `/statusline` | 配置 TUI 底部状态栏的自定义命令（见下文） |
| `/permissions` | 管理工具权限（`allow`/`ask`/`deny`） |
| `/evolve` | Skill 自演进入口：触发 Skill 演进（见下文） |
| `/evolve_list` | 查看某个 Skill 的演进经验库（见下文） |
| `/evolve_simplify` | 整理、合并某个 Skill 的演进经验（见下文） |
| `/evolve_rebuild` | 基于归档与演进记录重建 `SKILL.md`（见下文） |
| `/hooks` | 浏览已配置的 hooks（只读，见下文） |
| `/simplify` | 代码精简审查：检查复用性、质量、效率并自动修复（仅 `code.*`，见下文） |
| `/autofix-pr` | 修复当前分支的开放 PR，直到检查通过并处理完评审意见；自动识别平台（GitHub 走 `gh`，GitCode 走 REST）；`--watch` 持续复查直到绿/合并/关闭（仅 `code.*`，详见[自动修复 PR](自动修复PR.md)） |
| `/sandbox` | 设置沙箱模式（见下文） |
| `/agents` | 管理 Agent 配置（list, get, create, update, enable, disable, delete，见下文） |
| `/auto-harness` | Auto-Harness 任务管理（`run`/`schedule`/`issue`，见下文） |
| `/btw` | 旁路快速提问，不中断主对话（见下文） |
| `/swarmflow` | SwarmFlow 开关、状态查询与 token 预算（`on` / `off` / `--budget`，见 [TUI SwarmFlow 指南](TUI使用SwarmFlow指南.md)） |
| `/swarmflows` | 全屏 SwarmFlow 运行树查看器（别名 `/swarmworkflows`，同上） |

> 说明：本页的 `/mode` 与 `/switch` 以 Gateway 受控通道行为为主。TUI 本地命令另支持 `/mode plan`、`/mode team.normal`，详见 [TUI 使用指南](TUI使用指南.md)。

### Gateway / Agent 侧解析（受控通道）

由 Gateway 识别并转发到 AgentServer 等后端能力。

| 命令 | 说明 |
|---|---|
| `/plan` | 切换规划子模式 |
| `/resume` | 历史会话恢复（见下文） |
| `/new_session` | 新建会话（仅 IM 生效） |
| `/mode` | 模式切换（支持一级入口与直达写法） |
| `/switch` | 在当前模式族内切换二级模式 |
| `/skills` | 技能管理（列表、安装、卸载、市场源、ClawHub、SkillNet） |
| `/model` | 模型查看、新增、编辑、删除、切换（见下文） |
| `/mcp` | MCP 服务管理（见下文） |
| `/diff` | 交互式改动回顾：按轮次 diff + 未提交工作树改动（见下文） |
| `/compact` | 压缩当前上下文（见下文） |
| `/init` | 项目初始化（见下文） |
| `/branch` | 从当前对话点创建分支会话（见下文） |
| `/rewind` | 回退对话到指定轮次之前（见下文） |
| `/memory` | 记忆管理（见下文） |
| `/cron` | 定时任务管理（见下文） |
| `/review` | 代码审查 PR（见下文） |
| `/security-review` | 安全审查当前分支待定变更（见下文） |

---

## 重点命令说明

### `/swarmflow` 与 `/swarmflows`（TUI 本地）

SwarmFlow 专用命令；完整流程见 **[TUI 使用 SwarmFlow 指南](TUI使用SwarmFlow指南.md)**。

| 命令 | 说明 |
|------|------|
| `/swarmflow` | 查询状态，如 `swarmflow: on · mode: team · budget: unbounded` |
| `/swarmflow on` | 写入 `enable_swarmflow=true`；非 team 时一并切到 team；可选 `--budget <tokens\|none>` |
| `/swarmflow off` | 写入 `enable_swarmflow=false`；不自动离开 team |
| `/swarmflows` | 打开全屏运行树（工作流 → 阶段 → 节点）；别名 `/swarmworkflows` |

配置变更后当前 session **不热更新**，提示 `Use /new to apply.`；主界面 **`h`** 用于 pending 人工回复（非本命令）。

### `/workspace`（TUI 可信目录管理）

管理 AI 可访问的目录范围，用于文件读取、编辑、执行等操作。

#### 子命令

| 命令 | 说明 |
|---|---|
| `/workspace` 或 `/workspace get` | 查看系统默认工作空间与当前可信目录列表 |
| `/workspace add [path]` | 添加可信目录（默认为当前目录，路径不存在时提示错误） |
| `/workspace set <path>` | 切换当前项目作用域，并把该路径加入对应项目的可信目录 |
| `/workspace remove <path>` | 移除指定可信目录 |
| `/workspace clear` | 清空所有可信目录（仅使用默认工作空间） |

#### 概念说明

- **系统默认工作空间（workspace）**：固定路径 `~/.jiuwenswarm/agent/workspace`，始终可用
- **可信目录（trusted_dirs）**：用户授权的可访问目录，由 TUI 管理，传递给后端 Agent
- **项目作用域（project scope）**：用于选择对应的可信目录集合，并填充请求中的 `project_dir` / `cwd`

#### 控制逻辑

1. **启动确认**：TUI 启动时询问用户是否信任当前目录
   - 选择「信任」：将当前目录添加为可信目录
   - 选择「不信任」：仅使用默认工作空间

2. **按项目持久化**：可信目录以规范化后的项目路径为键，保存到 `~/.jiuwenswarm-tui/config.json`。`/workspace set` 对项目作用域的切换仅在当前 TUI 进程中有效；重启后重新以启动目录作为项目作用域。

3. **后端传递**：TUI 通过请求参数传递 `trusted_dirs`、`project_dir` 和 `cwd`，Agent 据此限制文件操作范围并解析项目上下文

4. **路径限制**：Agent 收到可信目录后，文件操作需限制在可信目录范围内；超出范围需向用户确认

5. **路径校验**：`add` 和 `set` 操作会校验路径是否存在，不存在则提示错误

#### 兼容别名

`/workspace_dir`、`/workspace-dir`

### `/mode` 与 `/switch`（受控通道）

- 一级入口映射：
  - `/mode agent` -> `agent.plan`
  - `/mode code` -> `code.normal`
  - `/mode team` -> `team`
- 直达写法：
  - `/mode agent.plan` -> `agent.plan`
  - `/mode agent.fast` -> `agent.fast`
  - `/mode code.normal` -> `code.normal`
  - `/mode code.team` -> `code.team`
- 二级切换：
  - agent 族：`/switch plan` <-> `agent.plan`，`/switch fast` <-> `agent.fast`
  - code 族：`/switch normal` <-> `code.normal`，`/switch team` <-> `code.team`
- 非法组合（如在 `code.*` 下执行 `/switch fast`）返回：`非法指令`。
- 受控通道不接受 `/mode plan`、`/mode team.normal`。
- 备注：独立 `/team` 命令已移除，请统一使用 `/mode team`。

### `/resume`

- `/resume list`：列出历史会话。
- `/resume <conversation_id>`：恢复指定会话。

#### 交互式选择器（TUI）

输入 **`/resume`** 或 **`/continue`** 且无参数时，打开交互式会话选择器：

| 按键 | 功能 |
| --- | --- |
| `↑` / `↓` | 移动焦点 |
| `Enter` | 恢复焦点会话 |
| 输入字符 | 实时搜索（会话 ID / 标题 / 项目目录） |
| `Backspace` | 删除搜索字符 |
| `Space` | 预览焦点会话信息卡；预览态 `Enter` 恢复、`Space`/`Esc` 返回 |
| `Ctrl+R` | 重命名焦点会话；编辑态 `Enter` 保存、`Esc` 取消、留空清除标题 |
| `Ctrl+A` | 「全部项目」与「仅当前项目」范围切换 |
| `Ctrl+B` | 开关 git 分支过滤（严格匹配当前项目分支名） |
| `Esc` | 有搜索词时清空；否则关闭选择器 |

> 上表 `Space` / `Ctrl+R` / `Ctrl+A` / `Ctrl+B` / `Esc` 可在 `ResumeList` context 下通过 `/keybindings` 重绑；预览态、重命名编辑态与搜索文本输入除外。

说明：

- **默认列出全部项目** 的会话（`Ctrl+A` 切回仅当前项目）；当前项目无会话时仍打开（空）选择器以便按 `Ctrl+A`。
- **分支过滤** 仅按分支名严格匹配，存量无分支记录与 `HEAD` 会话会被过滤；按名比对不区分仓库，「全部项目 + 分支过滤」时同名分支会一并显示。
- **恢复范围**：仅恢复会话上下文（历史、会话 ID、accent 颜色、workflow 快照、窗口标题），**不切换 workspace / 当前工作目录**。

> 完整快捷键与行为详见 [TUI 使用指南](TUI使用指南.md#resume-与-continue-在-tui-中的特殊行为)；自定义快捷键见 [快捷键](TUI使用指南.md#快捷键)。

### `/model`（查看 / 新增 / 编辑 / 删除 / 切换模型）

管理 `config.yaml` 中 `models.defaults` 下定义的模型配置。支持文本子命令与**交互式列表**两种操作方式，删除/编辑主要通过交互列表完成。

- **文本用法**：
  - `/model` 或 `/model list`：打开**交互式模型列表**（含当前模型标记），可在列表内完成切换、新增、编辑、删除；
  - `/model <name>`：按名称直接切换到指定模型；
  - `/model add <name> key=value ...`：以文本表单新增模型配置（如 `model_name=...`、`provider=...`、`api_base=...`、`api_key=...`、`reasoning_level=...`）。
- **交互列表快捷键**（`/model` 或 `/model list` 打开列表后）：

  | 按键 | 作用 |
  | --- | --- |
  | `↑` / `↓` | 上下选择模型 |
  | `Enter` | 切换到当前选中的模型 |
  | `a` | 打开表单**新增**模型 |
  | `e` | 打开表单**编辑**当前选中的模型 |
  | `d` | 对当前选中的模型进入**删除确认** |
  | `Esc` | 关闭列表 / 取消当前操作 |

- **删除流程**（`d` → 确认）：进入 `Delete model: <名称>` 确认页后——
  - `Enter`：确认删除；后端按模型在 `models.defaults` 中的**原始下标**（`index`，即未过滤 video/audio/vision 等多模态条目前的下标）定位并移除该条目，回写 `config.yaml`，随后触发 Agent 配置重载（`AGENT_RELOAD_CONFIG`）。成功响应为 `{type: "model_deleted", name, current}`。
  - `Esc`：取消，返回列表。
- **编辑流程**（`e`）：复用新增表单，但 `api_key` 留空表示**保持原密钥不变**；只提交发生变化的字段。
- **限制与校验**：
  - `video` / `audio` / `vision` 为多模态专用键，不能通过 `/model <name>` 设置为默认聊天模型，需改用 `/config edit` 或 `/config set`；
  - 删除会拒绝移除**最后一个**模型（返回 `Cannot delete the last model`），索引越界返回 `model index not found`；
- **配置写入行为**：
  - 新增 / 编辑 / 删除会改写 `config.yaml` 的 `models.defaults`（兼容旧结构），并触发 Agent 配置重载；
  - 切换模型会校验配置与环境变量占位符，更新 `MODEL_NAME` / `MODEL_PROVIDER` / `API_BASE` / `API_KEY`，并回写 `.env`。
- **安全展示**：列表中 `api_key`、`token` 等敏感字段掩码显示；仅当存在**同名且 provider+api_base 完全相同**的模型（真正无法区分）时，才会附带显示密钥末 4 位 `[…xxxx]`。

### `/diff`（交互式改动回顾）

- 用法：`/diff`（无子命令）。
- 适用模式：全部模式。
- 数据来源：TUI 通过 `command.diff` 请求 AgentServer diff 服务（60s 超时），处理器从请求元数据解析当前 `session_id` 与 `project_dir`，然后**并行**通过 worker 线程获取两组数据：
  - `turns` — 基于 `.agent_history` 文件操作日志计算的每轮改动集合；
  - `gitDiff` — 基于 `git diff HEAD` 获取的未提交工作树改动。
- 响应格式：`{ type: "list", turns: [...], gitDiff?: {...} }`；出错时返回 `{ ok: false, error: "..." }`。
- 展示方式：打开 **交互式 Diff 查看器**（全屏覆盖模式）：
  - **列表视图**：展示所有变更文件（含工作树 `working` 和按轮次 `Turn N`），显示相对路径、来源、增删行数；
  - **详情视图**：选中文件后 `Enter` 进入，展示完整的 hunk diff，支持上下滚动。
- 列表视图快捷键：
  - `↑` / `↓` — 移动选择，自动滚动；
  - `Enter` — 查看选中文件的完整 diff；
  - `Home` / `g` — 跳至列表顶部；
  - `End` / `Shift+g` — 跳至列表底部；
  - `Esc` / `Ctrl+C` — 关闭。
- 详情视图快捷键：
  - `↑` / `↓` — 逐行滚动；
  - `PgUp` / `PgDn` — 上下翻页；
  - `Home` / `g` — 跳至文件开头；
  - `End` / `Shift+g` — 跳至文件末尾；
  - `←` / `Esc` — 返回列表视图。
- 回退行为：当 TUI 不提供 `enterDiffViewer` 能力时，回退为内联展示（仅显示文件名、来源和增删行数）。

#### 按轮次 diff 数据来源

按轮次 diff 基于 `.agent_history/file_ops_jiuwenswarm*.json` 日志计算，而非 git。服务从多个位置读取并合并文件操作日志：

1. Agent 工作区（`~/.jiuwenswarm/agent/workspace/.agent_history/`）
2. 用户工作区 `.agent_history/`
3. 项目目录 `.agent_history/`（含 session 专属文件和全局文件）

条目通过路径规范化和时间戳邻近度（±1 秒）去重。轮次边界由 session history 中的用户消息定义：一个轮次从一条用户消息时间戳到下一条用户消息为止。仅返回有文件变更的轮次，空轮次被过滤。轮次编号保留原始序号（与 history 中实际用户消息计数对齐），以便 `/rewind` 正确映射。

#### Git diff 数据来源

工作树改动通过 `git diff HEAD`（仅已跟踪文件）获取。Git 仓库根目录从 `project_dir`（可以是子目录）解析。暂存区重命名（含 brace 简写形式 `a/{b => c}/d.txt`）会进行归一化处理，确保 numstat key 与 hunk key 对齐。

#### 效果边界与限制

| 边界 | 值 | 行为 |
|---|---|---|
| 详情最大文件数 | 50 | 仅前 50 个已跟踪文件有 hunks；统计仍覆盖所有变更文件 |
| 单文件最大 diff 大小 | 1 MB | diff 超过 1 MB 的文件跳过 hunk 解析，设置 `isLargeFile` 标志，统计仍计入 |
| 详情最大文件数阈值 | 500 | 变更文件超过 500 时，仅返回汇总统计（无逐文件 hunk） |
| Git 命令超时 | 10s | 超过 10 秒的 git 命令返回 `None` |
| Git 根目录解析超时 | 5s | `git rev-parse --show-toplevel` 超过 5 秒返回 `None` |

#### 不覆盖的范围

- **未跟踪文件**：`git diff HEAD` 仅覆盖已跟踪文件的修改。未跟踪文件不在 git diff 部分中显示。（由 agent 编辑过的未跟踪文件可能通过 file_ops 日志出现在按轮次 diff 中。）
- **手动/bash 编辑**：按轮次 diff 来源于 agent 的 `.agent_history` 文件操作日志。手动或通过 bash 命令编辑的文件不在按轮次 diff 中追踪。
- **已提交的改动**：仅显示未提交的工作树改动，已提交的历史不覆盖。
- **瞬态 git 状态**：在 merge、rebase、cherry-pick、revert 期间，git diff 返回 `None`，避免显示误导性的 incoming 改动。
- **二进制文件**：二进制文件变更计入统计但不展示 hunks（设置 `isBinary` 标志）。
- **非 git 仓库**：`project_dir` 不在 git 仓库中时，`gitDiff` 为 `None`，仅返回按轮次 diff。
- **无 project_dir**：无法解析 `project_dir` 时，`gitDiff` 为 `None`，按轮次 diff 仍可通过 session metadata 工作。

> `/diff` 并非 `git diff` 的完整版本控制替代方案。它将 agent 追踪的按轮次变更与未提交已跟踪文件的快照结合，用于在编码会话内快速回顾改动。

### `/compact`（上下文压缩）

- 用法：`/compact`（无参数）。
- 功能：主动触发上下文压缩，清理对话历史但保留摘要信息在上下文中。
- 数据来源：TUI 通过 `command.compact` 请求 Agent 侧压缩服务。
- 返回结果：
  - `busy`：压缩正在进行中，请稍后重试；
  - `compressed`：压缩成功，显示压缩前后 token 数及节省比例；
  - `noop`：无需压缩，上下文已处于最优状态。

### `/context`（上下文窗口用量）

- 用法：`/context`（无参数、无子命令）。
- 功能：查看当前会话的上下文窗口占用情况与 Token 用量明细。
- 数据来源：TUI 通过 `command.context` 请求 Agent 侧上下文统计服务，携带当前 `mode`。
- 展示内容：
  - **概览面板**：上下文窗口占用百分比 + 进度条；`context_window`（已用/上限 tokens）、`occupancy`（占用率）、`messages`（消息数）；
  - **Token 拆分面板**：按 `system_prompt`、`messages`、`tools`、`total` 展示 Token 用量；
  - **DeepAgent 占用明细**（如有数据）：以键值列表展示 `context_occupancy` 各字段；
  - **DeepAgent 用量明细**（如有数据）：以键值列表展示 `deepagent_usage` 各字段。
- 阈值提示：当占用率 >= 90% 时，概览标题显示 `Context window 90% full — consider /compact` 提示。
- 错误处理：请求失败时显示 `context failed: <错误信息>`。

### `/init`（项目初始化）

- 用法：`/init`（无参数）。
- 功能：初始化项目 AI 协作配置，生成 `JIUWENSWARM.md` 和可选的 `JIUWENSWARM.local.md`。
- 适用范围：仅在 `code` 模式下运行。
- 流程：
  1. 选择范围：`团队共享`（JIUWENSWARM.md）、`个人私有`（JIUWENSWARM.local.md）或 `都要`。
  2. 检测已有配置：自动检测 `CLAUDE.md`、`.cursorrules`、`copilot-instructions.md` 等文件。
  3. 生成配置：根据选择生成项目配置文件。
- 自动模式切换：Code 初始化会使用 `code.normal` 以便写入文件。

### `/mcp`（MCP 服务管理）

- 用法：
  - `/mcp list`：列出全部 MCP 服务（名称、transport、启用状态）；
  - `/mcp show [name]`：查看 MCP 配置；不带参数时展示当前启用项，带 `name` 时展示单个服务详情；
  - `/mcp add --name <name> --transport <stdio|sse> ...`：新增 MCP 服务；
  - `/mcp update --name <name> ...`：更新指定 MCP 服务配置（支持更新 transport / 参数 / 启用状态）；
  - `/mcp enable <name>`：启用指定 MCP 服务；
  - `/mcp disable <name>`：禁用指定 MCP 服务；
  - `/mcp remove <name>`：删除指定 MCP 服务。
- 传输参数：
  - `stdio`：需提供 `--command`，可选 `--args`、`--cwd`、`--env`；
  - `sse`：需提供 `--url`，可选 `--headers`、`--timeout_s`。
- 示例：
  - `/mcp list`
  - `/mcp show`
  - `/mcp show playwright`
  - `/mcp add --name playwright --transport stdio --command python --args "server.py --transport stdio"`
  - `/mcp update --name playwright --transport sse --url http://127.0.0.1:9000/sse --headers "Authorization=Bearer xxx"`
  - `/mcp add --name local-sse --transport sse --url http://127.0.0.1:9000/sse`
  - `/mcp disable playwright`
  - `/mcp remove local-sse`
- 配置与生效：
  - 变更会写入 `config.yaml` 的 `mcp.servers`；
  - 写入后会触发 Agent 配置重载，运行时按配置同步 MCP server 绑定。

### `/teamskills`（TeamSkills 管理）

- 用法：
  - `/teamskills init <name> [--path <parent_dir>] [--type <teamskills|skill>] [--force]`
  - `/teamskills validate <path> [--type <teamskills|skill>]`
  - `/teamskills pack <path> [--output <dir>]`
  - `/teamskills info <asset_id> --version <x.y.z> [--market-url <url>]`
  - `/teamskills search <query> [--type <skill|teamskills>] [--author <name>] [--asset-id <id>] [--asset-type <type>] [--publisher-id <id>] [--page <n>] [--page-size <n>] [--order-by <field>] [--desc <bool>] [--market-url <url>]`
  - `/teamskills list`
  - `/teamskills install <asset_id> [--version <x.y.z>] [--output <dir>] [--force] [--market-url <url>]`
  - `/teamskills uninstall <name>`
  - `/teamskills config [--market-url <url>] [--token <user_token>] [--system-token <system_token>]`
  - `/teamskills publish <path> --version <x.y.z> [--id <skill_id>] [--file <zip>] (--token <t>|--system-token <t>) [--market-url <url>] [--force] [--version-desc <text>]`
  - `/teamskills delete <skill_id> [--version <x.y.z|all>] (--token <t>|--system-token <t>) [--market-url <url>]`
- 行为：
  - `list` 仅列出当前本地可见已安装技能（并展示 `type`，区分 `skill` 与 `teamskills`）；
  - `search` 仅用于 TeamSkills Hub 市场搜索；
  - `config` 用于持久化 TeamSkills Hub 地址与 token（写入配置并尽量即时生效）；
  - `publish` 走 TeamSkills Hub 原生发布接口 `POST /api/v1/plugins`；
  - `delete` 走 TeamSkills Hub 原生删除接口 `DELETE /api/v1/plugins/{skill_id}/versions/{version}`；
  - `--token` 与 `--system-token` 互斥，且必须二选一。

### `/evolve*`（Skill 自演进）

这组命令由 TUI 本地注册并解析，随后通过普通聊天通道把 slash 文本转发给后端。实际演进逻辑在 Agent / Team 侧完成：

- Agent 模式：由 `SkillEvolutionRail` 处理，仅 `agent.plan` 可用。
- Team 模式：由 `TeamSkillEvolutionRail` 处理，用于团队技能演进。
- Code 模式与 `agent.fast` 不支持这组命令。

#### 子命令

| 命令 | 说明 |
|---|---|
| `/evolve <skill_name> [user_query]` | 为指定 Skill 触发演进。`agent.plan` 会扫描当前会话中的工具失败、用户纠错等信号；Team 模式必须提供 `user_query`。 |
| `/evolve_list <skill_name> [--sort score]` | 按分数查看某个 Skill 的演进经验，展示记录数、平均分、使用/反馈统计、section 与内容预览。 |
| `/evolve_simplify <skill_name> [user_intent]` | 生成经验库整理方案，用于合并重复经验、拆分过长经验或清理低价值经验；尾随文本会作为整理意图传入后端。 |
| `/evolve_rebuild <skill_name> [user_intent]` | 生成重建 `SKILL.md` 的 follow-up prompt，并继续作为一次普通 Agent / Team 任务执行。 |

#### 审批流程

- `/evolve` 和 `/evolve_simplify` 不会直接落盘覆盖内容；后端会推送确认问题，TUI 进入等待确认状态。
- 接收后，后端接受本次演进记录并写入/固化；拒绝后丢弃本次生成内容。
- Team 技能演进接收后会同步团队技能目录。
- 演进或审批未完成时，用户补充的新输入会先排队，等待演进完成后再继续发送。

#### 示例

```bash
/evolve pptx 修复导出失败时的错误处理
/evolve_list pptx --sort score
/evolve_simplify pptx 合并重复的导出失败经验
/evolve_rebuild pptx 强化 Troubleshooting 和 Examples
```

### `/branch`（分支会话）

- 用法：`/branch [name]`。
- 别名：`/fork`。
- 功能：以当前会话的当前状态为起点，创建一个分支会话，复制当前对话历史。
- 约束：
  - 当前会话正在处理中（`session is busy`）时拒绝执行；
  - 当前会话无对话记录时拒绝执行。
- 行为：
  1. 向后端发送 `session.fork` RPC，只携带 `source_session_id` 与可选标题；AgentServer 分配目标 `session_id` 并在响应中返回。
  2. TUI 自动切换到新分支会话，清空当前 transcript 并恢复分支的历史记录。
  3. 提示用户已在新分支，并告知可用 `/resume <原会话ID>` 返回原会话。
- 示例：
  - `/branch` — 创建无标题分支
  - `/branch fix-login-bug` — 创建名为 `fix-login-bug` 的分支

### `/rewind`（回退对话）

- 用法：`/rewind [turn_number]`。
- 别名：`/checkpoint`。
- 功能：围绕指定轮次回退或压缩当前会话，支持仅回退对话、仅恢复文件、两者同时恢复，或摘要部分历史。
- 约束：
  - 当前会话正在处理中（`session is busy`）时拒绝执行；
  - 无对话轮次时拒绝执行。
- 交互流程：
  1. 无参数时，先展示当前会话所有轮次列表（含时间、文件变更统计），供用户选择目标轮次。
  2. 选择轮次后，展示恢复选项：
     - **Restore conversation and code** — 截断对话并恢复文件到该轮次之前的状态；
     - **Restore conversation only** — 仅截断对话，文件保持不变；
     - **Restore code only** — 仅恢复文件，对话保持不变（仅当目标轮次有文件变更时显示）；
     - **Summarize from here** — 保留更早的消息，把所选轮次及之后的内容替换为压缩摘要；
     - **Summarize up to here** — 摘要所选轮次之前的消息，保留所选轮次及之后的内容；
     - **Cancel** — 取消操作。
  3. 根据选择调用对应后端 RPC：
     - `both` → `session.rewind_and_restore`
     - `conversation` → `session.rewind`
     - `code` → `session.restore_files`
     - `summarize` → `command.rewind_compact`，`direction=from`
     - `summarize_up_to` → `command.rewind_compact`，`direction=up_to`
- 回退后：TUI 清空 transcript 并重新加载历史；若回退内容包含用户输入，会自动填入输入框。
- 仅恢复文件时，如果没有需要还原的文件，会明确显示 `No file changes to restore`；若部分文件恢复失败，会逐项列出失败文件并保持这些文件不变，不再统一报告为成功。
- 局限：回退不影响通过 bash 命令或手动编辑的文件。
- 示例：
  - `/rewind` — 交互式选择轮次并确认恢复方式
  - `/rewind 2` — 直接回退到第 2 轮之前

### `/memory`（记忆管理）

- 别名：`/mem`。
- 功能：查看与管理记忆系统状态、记忆文件、开关配置及目录路径。
- 子命令：

| 命令 | 说明 |
|---|---|
| `/memory` | 打开页签控制台，默认选中 edit 页签 |
| `/memory edit` | 打开页签控制台并选中 edit 页签 |
| `/memory edit <path>` | 直接编辑指定路径的记忆文件（通过 `$EDITOR`） |
| `/memory status` | 打开页签控制台并选中 status 页签 |
| `/memory toggle` | 打开页签控制台并选中 toggle 页签 |
| `/memory toggle <key>` | 直接切换指定记忆系统开关 |
| `/memory open` | 打开页签控制台并选中 open 页签 |

- 编辑安全边界：
  - 目标文件必须已经存在并位于允许的记忆目录中；`/memory edit` 不负责新建文件。
  - 运行时 auto/coding memory 文件为只读，禁止手动编辑。
  - 项目目录或其祖先目录中的 `JIUWENSWARM.md` / `JIUWENSWARM.local.md` 只有在文件已存在且通过路径校验时才能打开。

- 页签控制台：无参数或仅指定子命令（不带操作对象）时打开，包含 edit / status / toggle / open 四个页签。
  - ←/→ 切换页签；
  - ↑/↓ 在当前页签列表中移动；
  - Enter 执行当前选中项；
  - Ctrl+O 切换全路径显示（edit/open 页签），切换页签时重置为默认（相对路径）；
  - Esc 关闭控制台。
- `status` 展示内容：
  - Engine（格式：`builtin (local)`）；
  - 开关行（按模式自适应，`✓ on` / `✗ off`）；
  - 运行时记忆统计（agent 模式显示 "Auto Memory"，code 模式显示 "Coding Memory"）；
  - Project Memory 统计；
  - External Memory（如果有）。
  - 不展示 Current Mode、Index/FTS5/Vector/Cache 等字段。
- `edit` 页签展示：Project memory（Checked in at）/ Local memory（Saved in）/ User memory（Saved in），Enter 用 `$EDITOR` 打开。
- `open` 页签展示（按模式自适应）：agent 模式为 Memory Dir / Project Dir / User Project Dir；code 模式为 Coding Memory Dir / Project Dir / User Project Dir，Enter 直接打开系统文件管理器。
- `toggle` 页签展示格式（只显示 key，不显示英文 label，有 `✓ on` / `✗ off` 状态标记和中文描述，无 `·` 分隔符）：

  ```
  → memory_enabled            ✓ on   记忆功能总开关
    auto_coding_memory        ✓ on   每轮对话后自动提取记忆（需总开关开启）
    memory_forbidden_enabled  ✗ off  过滤敏感信息
  ```

  - agent mode：`memory_enabled`（记忆功能总开关）、`memory_proactive`（对话中自动搜索和记录）、`memory_forbidden_enabled`（过滤敏感信息）；
  - code mode：`memory_enabled`（记忆功能总开关）、`auto_coding_memory`（每轮对话后自动提取记忆（需总开关开启））、`memory_forbidden_enabled`（过滤敏感信息）。
  - 切换后若需要重启会话生效，会给出提示。
- Tab 补全：
  - `/memory edit ` 后显示文件列表（路径用 `getDisplayPath` 展示，去重）；
  - `/memory toggle ` 后显示当前 mode 的 key 列表；
  - 支持前缀过滤。
- 示例：
  - `/memory` — 打开页签控制台
  - `/memory edit memory/MEMORY.md` — 直接编辑指定记忆文件
  - `/memory status` — 查看详细状态
  - `/memory toggle memory_enabled` — 切换记忆总开关
  - `/memory open` — 查看记忆目录路径

### `/cron`（定时任务管理）

管理定时任务（Cron Job），通过 RPC 调用后端 `CronController`，与 Web 端共用同一套后端逻辑和数据存储。

- 别名：`/crontab`
- 子命令：

| 命令 | 说明 |
|---|---|
| `/cron` 或 `/cron list` | 列出所有定时任务 |
| `/cron show <job_id>` | 查看指定任务的详细信息 |
| `/cron add name=<名称> cron_expr=<表达式> description=<描述> [其他参数]` | 新增定时任务 |
| `/cron update <job_id> key=value ...` | 更新指定任务的部分字段 |
| `/cron delete <job_id>` | 删除指定任务 |
| `/cron toggle <job_id> <on或off>` | 启用或禁用指定任务 |
| `/cron run <job_id>` | 立即执行指定任务 |
| `/cron preview <job_id>` | 预览任务接下来几次执行时间 |

- `add` 参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 任务名称 |
| `cron_expr` | 是 | Cron 表达式，支持两种格式：5 字段（分 时 日 月 周）或 7 字段 Quartz（秒 分 时 日 月 周 年）。5 字段会自动转换为 7 字段（补 second=0, year=*）。示例：每天 9 点 = `0 9 * * *`（5 字段）或 `0 0 9 * * ? *`（7 字段） |
| `description` | 是 | 任务描述，即 Agent 执行时收到的输入指令 |
| `targets` | 否 | 推送渠道，默认 `tui`；可选：`tui`、`web`、`feishu`、`whatsapp`、`wecom`、`xiaoyi`、`wechat`、`dingtalk` 或 `feishu_enterprise:<app_id>`。`targets=tui` 时结果会广播到所有已连接的 TUI 窗口，详见 [定时任务 — 推送到 TUI](定时任务.md#推送到-tui-频道) |
| `timezone` | 否 | IANA 时区，默认 `Asia/Shanghai` |
| `mode` | 否 | 执行模式，默认 `agent.fast`。可选：`agent`、`agent.fast`、`agent.plan`、`plan`、`team`、`team.plan`、`code.team`。`team` 系列走多 Agent 流式执行，详见 [定时任务 — Team 模式](定时任务.md#team-模式与-swarmflow多智能体定时任务) |
| `timeout_seconds` | 否 | 单次执行超时（秒），范围 60～259200。未设置时普通模式与 Team 模式默认均为 3600（1 小时） |
| `wake_offset_seconds` | 否 | 提前唤醒秒数，默认 0 |
| `delete_after_run` | 否 | 执行一次后自动删除，默认 false |

- `add` 示例：
  - `/cron add name=每分钟测试 cron_expr="0 * * * *" description="告诉我现在几点了" targets=tui`
  - `/cron add name=晨报 cron_expr="0 9 * * *" description="生成今日晨报摘要" targets=tui mode=agent.plan`
  - `/cron add name=模型周报 cron_expr="0 9 * * 1" description="对比 GLM 与 DeepSeek 并输出报告" targets=tui mode=team`
  - `/cron add name=提醒 cron_expr="0 30 17 29 4 ? 2026" description="别忘了开会" targets=tui delete_after_run=true`
  - `/cron add name=每周一报 cron_expr="0 9 * * 1" description="生成本周周报" targets=web`

- `update` 用法：只需传入要修改的字段，如 `/cron update <id> name=新名称 enabled=false`
- `show` 显示内容：以 key-value 格式展示任务全部字段（id、name、status、cron_expr、timezone、description、targets、mode、timeout_seconds、wake_offset_seconds、delete_after_run）
- `list` 显示内容：序号、完整 job ID、名称、cron 表达式、启用状态、描述摘要
- `preview` 显示内容：每次执行计划的唤醒时间和推送时间

### `/skills`（技能管理）

管理技能的完整生命周期：列表查看、安装、卸载、市场源管理、ClawHub 和 SkillNet 在线技能库。

#### 子命令

| 命令 | 说明 |
|---|---|
| `/skills` 或 `/skills list` | 列出技能（分两组：已安装 / 可安装） |
| `/skills install <skill>` 或 `/skills install <slug@clawhub>` 或 `/skills install <name@skillnet>` 或 `/skills install <skill@marketplace>` 或 `/skills install <path_or_url>` | 安装技能：内置技能可直接用名称，ClawHub 用 `<slug>@clawhub`，SkillNet 用 `<名称>@skillnet`（自动搜索获取 URL），市场源用 `<名称>@<市场源>`，本地路径或远程 URL 直接传入 |
| `/skills uninstall <name>` | 按名称卸载技能 |
| `/skills marketplace` 或 `/skills marketplace list` | 列出市场源（名称、URL、启用状态、最后更新时间） |
| `/skills marketplace add <name> <url>` | 添加新的市场源 |
| `/skills marketplace remove <name>` | 移除市场源（同时清理缓存） |
| `/skills marketplace toggle <name> <on或off>` | 启用或禁用市场源（`on`/`true`/`1` 为启用，其余为禁用） |
| `/skills marketplace clawhub` | 查看 ClawHub token 状态（已配置/未配置） |
| `/skills marketplace clawhub token <value>` | 设置 ClawHub CLI token |
| `/skills marketplace clawhub token` | 查看 ClawHub token 状态 |
| `/skills skillnet` 或 `/skills skillnet search <query>` | 搜索 SkillNet 技能库（显示名称、简介、作者、星标、分类、URL） |
| `/skills skillnet install <skill_url>` | 通过 SkillNet URL 安装技能（异步下载，自动轮询进度） |
| `/skills use <skill_name>, <query>` | 使用指定技能执行查询 |

#### 概念说明

- **技能（Skill）**：可从市场源、ClawHub、SkillNet、内置目录或本地路径安装的扩展能力，为 Agent 提供额外功能。
- **内置技能（Builtin skill）**：随软件打包发布的预置技能，安装时可直接使用技能名称（如 `/skills install advanced-daily-report`），无需指定市场源。
- **ClawHub**：在线技能库（[clawhub.ai](https://clawhub.ai)），托管社区发布的技能。安装时使用 `<slug>@clawhub` 格式，其中 slug 是技能的唯一标识符（而非展示名）。使用前需先配置 ClawHub CLI token。
- **SkillNet**：学术技能库（支持两种安装方式：`<名称>@skillnet`（自动搜索获取 URL 后安装）和 `/skills skillnet install <url>`（直接用 URL 安装）。
- **市场源（Marketplace source）**：托管可用技能的远程 Git 仓库，每个源包含名称、URL 和启用/禁用状态。
- **规格标识（Spec）**：安装时使用的标识格式，支持以下几种：`<技能名>@builtin`（内置）、`<slug>@clawhub`（ClawHub）、`<技能名>@<市场源名>`（Git 市场源）；裸名不带 `@` 时系统会自动检测是否为内置技能。
- **本地安装（Local install）**：通过 `/skills install <path>` 将本地目录（需包含 `SKILL.md`）或远程归档 URL 安装为自定义技能；路径/URL 会自动识别并走本地导入流程。
- **安装位置（Install location）**：技能安装后的存储目录（`~/.jiuwenswarm/agent/workspace/skills/`）。
- **来源标签（Source tag）**：列表中每项技能标注来源，`[builtin]` 表示内置、`[local]` 表示本地导入、`[clawhub]` 表示从 ClawHub 安装、`[project]` 或市场源名表示其他来源。

#### 列表分组展示

`/skills list` 返回的技能列表分为两组：

1. **已安装（Installed）**：已存在于用户 skills 目录的技能，可直接使用。
2. **可安装（Available to install）**：内置但尚未安装的技能，以及市场源中可安装的技能，需先执行 `/skills install` 才能使用。

#### IM 与 TUI 的差异

两端最终都会请求 `skills.list`，但触发方式和展示形态不同。

| 端 | 触发方式 | 行为 |
|---|---|---|
| IM（飞书等受控通道） | 整行精确匹配 `/skills list`（会先做空白规范化） | Gateway 拦截控制消息并请求 `skills.list`，结果以 IM 通知/卡片等形式展示；单独输入 `/skills` 不走该控制路径。 |
| TUI（CLI 内置） | 输入 `/skills` | 本地执行内置命令并调用 `skills.list`，在会话内以分组列表视图展示（标题 `Installed Skills` 与 `Available Skills`）；无数据时提示 `No installed skills`。 |

对于其他子命令（`/skills install`、`/skills uninstall`、`/skills marketplace add/remove/toggle`、`/skills use`），Gateway **不会拦截**——在 IM 侧输入时会被当作普通聊天消息发送给 Agent。这些子命令仅在 TUI（CLI 内置）和 Web UI 路径下可用，通过 RPC 直连 AgentServer。

#### 备注

- **超时**：`install`、`uninstall`、`marketplace toggle` 请求在 TUI 侧有 120 秒超时；其余子命令无显式超时设置。
- **内置技能自动识别**：使用 `/skills install <skill>` 安装时，若技能名称不带 `@`，系统会自动检查是否为内置技能并重定向到内置安装流程；若不是内置技能则返回格式提示。
- **路径/URL 自动识别**：使用 `/skills install <path_or_url>` 安装时，若参数为本地路径（如 `/path/to/skill`、`~/skill`、`C:\skill`）或远程 URL（如 `https://...`），系统自动走本地导入流程（`skills.import_local`）。所有 URL 统一走 import_local，不自动路由 SkillNet。本地路径须为**绝对路径**或 `~/...` 路径；`~` 表示服务端 JiuwenClaw 进程用户的 home 目录。其他相对路径、位于系统/敏感目录（如 `/etc`、`~/.ssh`）的源、符号链接，以及不含合法 `name`/`description` frontmatter 的 `SKILL.md` 会被拒绝。
- **`@skillnet` 搜索安装**：使用 `/skills install <name>@skillnet` 时，前端先调用 `skills.skillnet.search` 搜索。**只有精确匹配 skill_name 时才自动安装**；无精确匹配时只展示搜索结果列表（含 URL 和名称），不自动安装第一个结果，用户需从中选择后用 `/skills skillnet install <url>` 或 `/skills install <精确名称>@skillnet` 安装。这是因为 SkillNet 搜索是语义匹配，搜索 "code" 可能返回 "taskflow"、"coding-agent" 等名称不含 "code" 的技能。
- **ClawHub token 必需**：从 ClawHub 安装技能前必须先配置 CLI token（通过 `/skills marketplace clawhub token <value>`）。未配置 token 时，`@clawhub` 安装会失败并提示配置方法。Token 可在 [clawhub.ai](https://clawhub.ai) 注册获取。
- **ClawHub slug 与展示名**：ClawHub 技能的唯一标识是 **slug**（如 `code-review-security`），而非展示名（如 "Code Review Assistant"）。当直接使用 slug 安装失败时，系统会自动搜索 ClawHub 并列出匹配结果（含真实 slug 和简介），帮助用户找到正确的技能。
- **ClawHub 重名覆盖确认**：当目标 slug 的技能已存在时（同名不同源也算已安装），TUI 会弹出交互式确认："Skill xxx 已安装，是否强制覆盖？"。用户选择"是"则用 `force: true` 重新安装并覆盖旧技能；选择"否"或退出则保持原技能不变。Web 端则直接以 `force: true` 覆盖，不弹确认。
- **SkillNet 异步安装**：SkillNet 安装是异步的——先发起下载任务获取 `install_id`，然后自动轮询 `install_status` 直到完成或失败。TUI 每 800ms 轮询一次，最长等待 15 分钟。安装过程中会显示 `Downloading... (install_id: xxx)`。
- **SkillNet 重名覆盖确认**：与 ClawHub 一致，TUI 在技能已存在时弹出交互确认。Web 端直接以 `force: true` 覆盖。
- **SkillNet 国内可访问**：SkillNet API 在 `http://api-skillnet.openkg.cn`（OpenKG 平台），国内可直接访问，无需 VPN。技能本体托管在 GitHub，GitHub 访问可能受限。
- **同名技能不可共存**：技能以目录名存储在 `skills/{name}/`，文件系统不允许同名目录共存。因此从不同源安装同名技能时，后安装的会覆盖前一个（需用户确认）。`/skills use` 只使用技能名，无法区分来源。
- **ClawHub 网络访问**：ClawHub API 地址为 `https://clawhub.ai`，在国内可能需要 VPN 才能正常访问。
- **缓存清理**：`marketplace remove` 发送 `{ name, remove_cache: true }` 以同时清理该源的本地缓存。
- **自动刷新**：`marketplace add`、`marketplace remove`、`marketplace toggle` 在操作成功后会自动重新列出市场源。
- **离线处理**：`/skills use` 会检查连接状态；离线时显示 `offline: waiting for reconnect before sending /skills use request`。

#### 示例

- `/skills` — 列出技能（分组：已安装 / 可安装）
- `/skills list` — 列出技能（显式子命令）
- `/skills install advanced-daily-report` — 安装内置技能（裸名自动识别）
- `/skills install advanced-daily-report@builtin` — 安装内置技能（显式指定）
- `/skills install code-review@clawhub` — 从 ClawHub 安装技能（使用 slug）
- `/skills install code-review@skillnet` — 从 SkillNet 安装技能（自动搜索获取 URL）
- `/skills skillnet search code-review` — 搜索 SkillNet 技能库
- `/skills skillnet install https://github.com/user/skill-repo` — 通过 SkillNet 子命令安装技能（直接用 URL）
- `/skills install my-skill@marketplace` — 从 Git 市场源安装技能
- `/skills install /path/to/my-skill` — 从本地目录安装技能
- `/skills install https://example.com/skill.zip` — 从远程 URL 安装技能（走本地导入）
- `/skills uninstall my-skill` — 卸载技能
- `/skills marketplace list` — 列出市场源
- `/skills marketplace add community https://github.com/user/skills-repo` — 添加名为"community"的市场源
- `/skills marketplace remove community` — 移除"community"市场源
- `/skills marketplace toggle community on` — 启用"community"市场源
- `/skills marketplace toggle community off` — 禁用"community"市场源
- `/skills marketplace clawhub` — 查看 ClawHub token 状态
- `/skills marketplace clawhub token abc123xyz` — 设置 ClawHub CLI token
- `/skills use my-skill, Code and execute a Hello World program.` — 使用技能执行查询

### `/export`（导出会话）

将当前对话导出到文件或剪贴板。

#### 子命令

| 命令 | 说明 |
|---|---|
| `/export` | 将整段对话复制到剪贴板；剪贴板不可用时提示指定文件名 |
| `/export <filename>` | 将对话写入工作空间目录下的 `filename.txt`；文件名不含 `.txt` 后缀时自动追加 |

#### 输出格式

导出的文本按时间戳与角色前缀逐条渲染：

- `[User] <时间戳>` — 用户输入
- `[Assistant] <时间戳>` — 助手回复
- `[Thinking] <时间戳>` — 内部推理过程
- `[Tools] <时间戳>` — 工具调用，含名称、摘要、截断结果（最多 500 字符）
- `[System] / [Error] / [Info] <时间戳>` — 系统消息
- `[Diff] <时间戳>` — 按轮次的文件变更摘要

#### Tab 补全

输入 `/export ` 后按 Tab，自动生成文件名建议：

- `<时间戳>-<净化后的首条提示>.txt` — 取首条用户消息（截断 50 字符，净化特殊字符）
- `conversation-<时间戳>.txt` — 通用时间戳名

时间戳格式：`YYYY-MM-DD-HHmmss`。

#### 行为细节

- **剪贴板回退**：未指定文件名且剪贴板不可用时，提示用户指定文件名导出到文件。
- **文件名规范化**：任何扩展名都会被替换为 `.txt`；例如 `/export my-chat.json` 变为 `my-chat.txt`。
- **写入位置**：文件保存到 `ctx.getWorkspaceDir()`（回退为 `process.cwd()`）。

#### 示例

- `/export` — 复制对话到剪贴板
- `/export my-chat` — 保存到工作空间下的 `my-chat.txt`
- `/export 2026-05-09-debug-session.txt` — 使用显式时间戳文件名保存

### `/simplify`（代码精简审查）

在 **TUI 本地解析**，通过专用 RPC `command.simplify` 获取服务端生成的三阶段审查 prompt，然后注入为 Agent 消息（`logAsUser: false`）。Agent 自动审查代码变更的复用性、质量与效率，并直接修复发现的问题。

- **范围**：仅复用 / 质量 / 效率。安全漏洞（注入、XSS、硬编码密钥、鉴权缺陷等）**不在范围内**，此处既不修复也不报告；请用 `/security-review` 获取只读的安全审查报告。
- **别名**：无。
- **适用模式**：**仅 `code.*`**。非 code 模式下提示先执行 `/mode code`。
- **解析位置**：TUI 本地（非 Gateway 受控通道），IM 不可用。

#### 用法

| 命令 | 说明 |
|---|---|
| `/simplify` | 审查当前 git 变更（或最近编辑的文件），自动修复问题 |
| `/simplify <target>` | 附加关注点：文件路径、模块名或特定审查维度 |

#### 执行流程

1. **TUI 校验**：确认当前处于 `code.*` 模式，否则返回错误提示。
2. **RPC 请求**：调用 `command.simplify`（携带可选 `target`），超时 30 秒。
3. **服务端生成 prompt**：基于 `_SIMPLIFY_PROMPT_TEMPLATE` 构建三阶段审查指令；若有 `target`，追加 `## Additional Focus` 段落。
4. **注入 Agent**：TUI 通过 `ctx.sendMessage(prompt, ..., { logAsUser: false })` 注入 prompt，Agent 开始执行。
5. **离线处理**：离线时提示重试。

#### 三阶段审查

**阶段 1 — 识别变更**：执行 `git diff`（或 `git diff HEAD`）获取变更文件列表；无 git 变更时审查对话中最近编辑的文件。

**阶段 2 — 并行启动三个审查 Agent**（若有子 Agent 工具则并发执行；否则自行完成三项审查）：

| 审查维度 | 关注点 |
|---|---|
| **代码复用审查** | 是否存在可替代新代码的现有工具/工具函数；重复实现已有功能；手写逻辑是否可用已有工具替代 |
| **代码质量审查** | 冗余状态；参数膨胀；复制粘贴变体；抽象泄漏；字符串硬编码（应用已有常量/枚举）；不必要的 JSX 嵌套；不必要的注释（仅保留说明 WHY 的注释） |
| **效率审查** | 不必要的工作（重复计算/文件读取/N+1）；错失并发机会；热路径膨胀；无条件触发的无效更新；TOCTOU 反模式（先检查再操作）；内存泄漏/未清理的监听器；过度操作（读取整文件但只需部分） |

**阶段 3 — 修复问题**：汇总所有发现，逐一修复；误报跳过即可，不争论；完成后简要总结修复内容（或确认代码已足够精简）。

#### 示例

- `/simplify` — 审查所有变更
- `/simplify src/auth/` — 关注 `src/auth/` 目录下的变更
- `/simplify focus on error handling patterns` — 重点关注错误处理模式

### `/sandbox`（沙箱模式管理）

进入/离开 jiuwenbox 沙箱模式，并调整其运行时策略。通过 `command.sandbox` 与 agent-server 交互。

#### 子命令

| 命令 | 说明 |
|---|---|
| `/sandbox` 或 `/sandbox status` | 显示当前 runtime（`enabled`、`landlock`、`excluded_commands`、`files.allow_write`、`files.deny_write`） |
| `/sandbox enable` | 进入沙箱模式（需要时启动 jiuwenbox，并重建 agent） |
| `/sandbox disable` | 离开沙箱模式（重建 agent；jiuwenbox 只在 jiuwenswarm 启动时才停掉） |
| `/sandbox exclude add <pattern>` | 加入一条 shell glob，命中后在本地而非沙箱内执行 |
| `/sandbox exclude remove <pattern>` | 移除一条 pattern |
| `/sandbox exclude list` | 列出当前 `excluded_commands` |
| `/sandbox files allow <path>` | 允许沙箱内写 `<path>`（显示为 rw） |
| `/sandbox files deny <path>` | 拒绝沙箱内写 `<path>`（仍可读，显示为 ro） |
| `/sandbox files remove <path>` | 从 user-configured allow & deny 中移除该 path |
| `/sandbox files list` | 列出生效的 `allow_write` / `deny_write` |
| `/sandbox help` | 打印用法 |

#### 概念说明

- **平台限制**：`/sandbox` 仅支持 Linux 平台（jiuwenbox 依赖 bwrap / Landlock / Linux namespace 等内核能力）。 在 Windows / macOS 上运行的 agent-server 收到任何 `/sandbox` 子命令都会返回 `SANDBOX_BAD_REQUEST` 错误；如果 TUI 在 Mac/Windows 上、agent-server 在 Linux 主机上，是支持的（看 agent-server 所在主机的平台）。
- **写入策略语义**：`allow` / `deny` 控制的是沙箱内的**写访问**（rw/ro），不是 Unix 八进制权限；enforcement 由 bwrap bind mount + `--remount-ro` 实现，Landlock 为纵深防御（`landlock.compatibility=disabled` 时主要依赖 bwrap）。
- **嵌套路径**：支持「父 allow + 子 deny」（例如 allow `/tmp`、deny `/tmp/secret`）；不支持「子 allow + 父 deny」（父 deny 会覆盖子 allow），服务端会拒绝此类配置。
- **生效写入策略**：状态面板里的 `files.allow_write` / `files.deny_write` 是 auto-managed 与 user-configured 合并后的视图，每条路径显示 `(rw)` 或 `(ro)`。
- **preserve_file_sharing_mode**：由 jiuwenswarm 配置决定，不通过 `/sandbox` 切换。仅支持 `mount`：intrinsic 文件与 `project_dir` 通过 bind mount 注入沙箱，`project_dir/config/config.yaml` 会显式加进 `deny_write`；yaml 里写入其它值会被服务端拒绝。
- **excluded_commands**：按 simple-command 叶子做 fnmatch（可匹配完整叶子文本或命令名）。全命中则整条本地；全未命中则整条沙箱；混合命中时由本地 bash 编排，远端段走 `jiuwenbox sandbox exec`（需宿主 CLI）。
- **add / remove 的去重与冲突**：`exclude add` 在已存在同名 pattern 时报错；`exclude remove` 在不存在该 pattern 时报错。`files allow|deny` 在同一 bucket 已有同 path 时报错，在对侧 bucket（allow vs deny）已登记同 path 时也报错，需要先 `files remove` 再 add；`files remove` 在用户配置里找不到该 path 时报错。
- **enable / disable**：会触发 agent 重建，响应里会列出 `rebuilt_modes`（典型 `agent.*` / `code.*`）和 jiuwenbox 端点。

#### 示例

- `/sandbox enable` — 打开沙箱模式
- `/sandbox status` — 查看 runtime 与生效路径
- `/sandbox files allow ./tmp/` — 允许沙箱写入 `./tmp/`（rw）
- `/sandbox files deny ./tmp/secret/` — 在已 allow 的父目录下禁止写入子目录（ro）
- `/sandbox exclude add "git *"` — 让 `git` 命令穿透到本地执行，不进沙箱

### `/keybindings`（快捷键配置）

查看、编辑或重置 TUI 键盘快捷键。配置文件：`~/.jiuwenswarm-tui/keybindings.json`。

#### 用法

| 命令 | 作用 |
|------|------|
| `/keybindings` | 同 `/keybindings edit` |
| `/keybindings edit` | 创建或打开配置文件；关闭外部编辑器后重新加载 |
| `/keybindings list` | 列出当前生效的快捷键（按 context 分组） |
| `/keybindings reset` | 删除用户配置，恢复内置默认 |

别名：`/keybind`。

#### 配置说明

- 以内置默认绑定为底，用户 JSON 按 **context** 覆盖；键设为 `null` 可取消默认绑定。
- 键名需符合 pi-tui `matchesKey` 格式（`ctrl`/`shift`/`alt` + 主键）；不支持 chord。
- **不可重绑**：`ctrl+c`、`ctrl+d`、`ctrl+m`（保留键）。
- Select 列表内部导航、Config 编辑器文本输入、Resume 预览/重命名子态等仍为硬编码。

详见 [TUI 使用指南 · 快捷键](TUI使用指南.md#快捷键)。

### `/hooks`（浏览 Hooks 配置）

查看当前 `config.yaml` 中配置的所有 hooks 的摘要信息（只读）。

#### 用法

- `/hooks`（无参数、无子命令）

#### 数据来源

TUI 通过 `hooks.list` RPC 请求 Gateway，Gateway 从 `config.yaml` 的 `hooks` 段加载配置并返回摘要。

#### 展示内容

`/hooks` 分三个层级展示 hooks 配置：

1. **事件列表（Level 1）**：按 hook 数量降序排列所有事件，每行显示事件名与 hook 数量，描述列显示各 matcher 的 hook 数量分布。
2. **状态面板**：
   - `Source` — 配置来源（`config.yaml`）
   - `Global Status` — 全局开关状态（`enabled` / `DISABLED`）
   - `Total Hooks` — 所有事件的 hook 总数
   - `Active Events` — 至少配置了 1 个 hook 的事件数 / 总事件数（共 17 种事件）
3. **Hook 详情卡片（Level 2）**：按 `事件 > matcher` 分组，每个 hook 展示：
   - `Type` — `command`（shell 命令）或 `prompt`（LLM 审查）
   - `Command` / `Prompt` — hook 的具体内容
   - `Timeout` — 超时时间（秒）
   - `Shell` — 执行 shell（command hook 专用）
   - `Status` — 状态消息

#### 无配置时

若 `config.yaml` 中没有配置任何 hooks，显示 `No hooks configured.` 并提示通过 `/config edit` 编辑配置。

#### Hooks 概念速览

Hooks 是在特定事件触发时自动执行的扩展逻辑，支持以下 17 种事件：

| 事件 | 执行层 | 触发时机 |
|---|---|---|
| `PreToolUse` | Agent Rail | 工具调用之前 |
| `PostToolUse` | Agent Rail | 工具调用成功之后 |
| `PostToolUseFailure` | Agent Rail | 工具调用失败之后 |
| `Stop` | Agent Rail | Agent 响应结束 |
| `PermissionRequest` | Agent Rail | 权限请求时 |
| `PermissionDenied` | Agent Rail | 权限被拒绝时 |
| `SubagentStart` | Agent Rail | 子 Agent 启动 |
| `SubagentStop` | Agent Rail | 子 Agent 停止 |
| `BeforeModelCall` | Agent Rail | 模型调用之前 |
| `AfterModelCall` | Agent Rail | 模型调用之后 |
| `UserPromptSubmit` | Gateway | 用户提交消息 |
| `SessionStart` | Gateway | 会话开始 |
| `SessionEnd` | Gateway | 会话结束 |
| `Notification` | Gateway | 通知发送 |
| `ConfigChange` | Gateway | 配置变更 |
| `InstructionsLoaded` | Gateway | 指令加载 |
| `Setup` | Gateway | 初始化 |

支持两种 hook 类型：

| 类型 | 说明 | 关键参数 |
|---|---|---|
| `command` | 执行 shell 命令（子进程）。通过环境变量 `$ARGUMENTS` 接收 JSON 上下文。退出码 0 = 成功，2 = 阻断。 | `command`、`timeout`（默认 30s）、`shell`（默认 bash） |
| `prompt` | 调用 LLM 审查。模板中 `$ARGUMENTS` 替换为 JSON 上下文，`$TOOL_NAME` 替换为工具名。LLM 响应中的 JSON `decision: "block"` 可阻断。 | `prompt`、`timeout`（默认 15s）、`model` |

- **阻断行为**：退出码 2（command）或 `decision: "block"`（prompt）会阻止当前操作（如跳过工具调用），并将原因反馈给模型。
- **输入修改**：PreToolUse hook 可通过 stdout JSON 的 `modifiedInput` 字段修改工具输入参数。
- **附加上下文**：可通过 stdout JSON 的 `additionalContext` 字段注入额外信息到工具结果或模型上下文。
- **全局开关**：`config.yaml` 中 `hooks.disable_all_hooks: true` 可禁用所有 hooks。

#### 配置示例

```yaml
hooks:
  PreToolUse:
    - matcher: "write_file"
      hooks:
        - type: command
          command: "echo 'write_file 即将执行' >> /tmp/hooks.log"
          timeout: 10
    - matcher: "bash|run_command"
      hooks:
        - type: prompt
          prompt: "审查以下命令是否安全: $ARGUMENTS"
          timeout: 20
  SessionStart:
    - matcher: "*"
      hooks:
        - type: command
          command: "echo '会话开始: $ARGUMENTS' >> /tmp/hooks.log"
```

#### 示例

- `/hooks` — 浏览当前所有 hooks 配置

### `/agents`（Agent 管理）

管理自定义 Agent（子代理 / Subagent）的全生命周期：查看、创建、更新、启用/禁用、删除。Agent 定义存储为 Markdown 文件，支持四级来源按优先级覆盖。

- **解析位置**：TUI 本地解析，通过 RPC 调用后端 `agents.*` 端点。
- **适用模式**：全部。
- **注意**：该命令在注册表中标记为隐藏（`hidden: true`），不在 `/help` 中列出，但可直接使用。

#### 子命令

| 命令 | 说明 |
|---|---|
| `/agents` 或 `/agents list` | 列出所有 Agent（名称、来源、启用状态、描述摘要） |
| `/agents get <name>` | 查看指定 Agent 完整详情（含 System Prompt 正文） |
| `/agents create [--project\|--local] <名称> <描述>` | 创建自定义 Agent，LLM 自动生成 prompt |
| `/agents update <name> [--generate] <新描述>` | 更新 Agent 描述；加 `--generate` 由 LLM 重写 prompt |
| `/agents enable <name>` | 启用自定义 Agent（内置 Agent 不可操作） |
| `/agents disable <name>` | 禁用自定义 Agent（内置 Agent 不可操作） |
| `/agents delete <name>` | 删除自定义 Agent（内置 Agent 不可操作） |

#### Agent 来源与存储

| 来源 | 存储位置 | 优先级 | 可管理 |
|------|----------|--------|--------|
| `builtin` | 代码内置 | 最低 | 不可启用/禁用/删除 |
| `local` | `<workspace>/.jiuwenswarm/agents-local/` | 本地 | 可全生命周期管理 |
| `user` | `~/.jiuwenswarm/agents/` | 用户 | 可全生命周期管理（默认 `create` 位置） |
| `project` | `<workspace>/.jiuwenswarm/agents/` | 最高 | 可全生命周期管理 |

同名 Agent 按 `project > user > local > builtin` 优先级覆盖，被覆盖的 Agent 标记 `shadowed_by`。

#### Agent 定义字段

| 字段 | 说明 |
|------|------|
| `name` | Agent 名称（唯一标识） |
| `description` | 简要描述 |
| `prompt` | System Prompt 正文 |
| `source` | 来源（`builtin` / `user` / `project` / `local`） |
| `file_path` | Agent 定义文件路径 |
| `model` | 指定模型（`null` 表示使用默认） |
| `tools` | 可用工具列表 |
| `disallowed_tools` | 禁用工具列表 |
| `color` | 显示颜色 |
| `permission_mode` | 权限模式 |
| `memory_scope` | 记忆范围 |
| `when_to_use` | 调用时机描述 |
| `max_iterations` | 最大迭代次数（默认 200） |
| `skills` | 关联技能列表 |
| `enabled` | 启用状态（`true` / `false` / `null`） |
| `shadowed_by` | 被哪个来源覆盖（`null` 表示活跃） |

#### `/agents create` 行为要点

- **参数解析**：`--project` / `--local` 为位置标志，需放在名称之前（如 `/agents create --project my-agent 描述`）。
- **LLM 生成**：默认调用当前模型自动生成 `when_to_use` 和 `system_prompt`，失败时回退到内置模板。
- **自动启用**：创建成功后自动写入 `config.yaml` 的 `react.subagents.<name>.enabled = true` 并热加载配置。
- **超时**：60 秒。
- **输出**：显示 LLM 生成标记、存储位置、文件路径。

#### `/agents update` 行为要点

- **无参数**：不提供描述时，展示当前 Agent 详情（等同 `get`）并提示用法。
- **`--generate`**：显式触发 LLM 重写 prompt；不加此标志时使用请求中的模板值。
- **自动热加载**：更新后自动重载 Agent 配置。

#### `/agents enable` / `disable` 约束

- 内置 Agent（`source == "builtin"`）无法启用/禁用，后端返回错误。
- 操作会写入 `config.yaml` 的 `react.subagents.<name>.enabled` 并热加载。

#### `/agents delete` 约束

- 内置 Agent 不可删除。
- 删除后自动从 `config.yaml` 的 `react.subagents` 中移除并热加载。

#### `/agents get` 展示内容

以键值对展示所有 Agent 定义字段，并在末尾完整输出 System Prompt 正文。

#### Tab 补全

`get`、`update`、`enable`、`disable`、`delete` 子命令支持按 Agent 名称 Tab 补全（通过 `agents.list` RPC 获取名称列表）。

#### 示例

```bash
/agents                            # 列出所有 Agent
/agents list                       # 同上
/agents get Explore                # 查看 Explore Agent 详情
/agents create bug-hunter 根因分析专家        # 创建用户级 Agent
/agents create --project proj-agent 项目级   # 创建项目级 Agent
/agents create --local local-agent 本地专用  # 创建本地 Agent
/agents update bug-hunter --generate 更好的描述  # 更新并用 LLM 重写 prompt
/agents enable bug-hunter           # 启用 Agent
/agents disable bug-hunter          # 禁用 Agent
/agents delete my-agent             # 删除 Agent
```

### `/status`（查看运行状态）

显示 jiuwenswarm 运行状态概览、用量统计或配置编辑界面。

#### 子命令

| 命令 | 说明 |
|---|---|
| `/status` | 显示完整状态概览（版本、会话、模型、连接、MCP 服务、配置来源） |
| `/status overview` | 与 `/status` 相同——显式概览子命令 |
| `/status usage` | 显示当前会话的 token 用量（输入、输出、总量、按模型拆分） |
| `/status config` | 进入交互式配置编辑器（与 `/config edit` 相同） |

#### 概览显示分区

运行 `/status` 时展示四个键值面板：

1. **核心信息**：版本号、会话 ID、会话名称（或提示 `/rename` 添加）、当前工作目录、当前模式
2. **模型与 API**：模型名称、提供商、API 基地址、连接状态
3. **MCP 服务**：每个服务的名称、传输类型、启用/禁用状态
4. **配置来源**：配置文件路径与所有设置来源路径

#### 用量显示

`/status usage` 显示当前会话的 token 消耗：

- 总输入 token、输出 token、总 token
- 按模型拆分：模型名称、token 总量、输入/输出细分

#### 交互模式

若 TUI 提供交互式 StatusView（`ctx.enterStatusView`），`/status` 会打开带标签页的完整状态 UI。子命令参数选择初始标签页：

- `/status` → 打开概览标签页
- `/status usage` → 打开用量标签页
- `/status config` → 打开配置标签页

若 StatusView 不可用，回退为内联键值展示。

#### 数据来源

- 概览数据：通过 `command.status` RPC 请求 AgentServer
- 用量数据：通过 `ctx.getUsageSummary()` 从本地会话追踪获取
- 配置数据：通过 `config.get` RPC 请求 AgentServer

#### 示例

- `/status` — 显示完整概览
- `/status overview` — 显示概览（显式）
- `/status usage` — 显示 token 用量
- `/status config` — 打开配置编辑器

### `/statusline`（TUI 状态栏配置）

配置 TUI 底部状态栏，通过自定义 shell 命令动态显示会话信息（模式、模型、工作目录等），仿照 Claude Code 的 `/statusline` 实现。

#### 子命令

| 命令 | 说明 |
|---|---|
| `/statusline` | 启动内置 setup 子 agent，创建、检查、修改或删除状态栏 |
| `/statusline get` | 始终只查看当前状态栏配置，不触发 setup agent |
| `/statusline set <shell-command>` | 设置状态栏命令（命令输出将显示在 TUI 底部） |
| `/statusline padding <number>` | 设置左右 padding；参数必须为非负整数，且需要先配置状态栏命令 |
| `/statusline clear` | 清除状态栏配置（底部栏将不再显示） |
| `/statusline help` | 显示使用指南（含写法模式、实用示例、字段列表） |
| `/statusline json` | 显示当前实际的 JSON 数据值（方便调试 jq 表达式） |
| `/statusline <prompt>` | 根据自然语言描述让 Agent 自动生成并配置状态栏脚本 |

#### 概念说明

- **状态栏（StatusLine）**：TUI 底部的文字区域，实时显示用户自定义的动态信息，支持多行输出。配置了自定义状态栏后，内置状态栏会自动隐藏，避免信息冗余。
- **Shell 命令**：用户配置的 shell 命令每 2 秒自动执行一次，其 stdout 输出渲染为状态栏文字。
- **Agent 自动生成模式**：无论是否附带自然语言描述，`/statusline` 都会交给内置 `statusline-setup` 子 agent。未配置时，它会引导创建并自动应用配置；已配置时，它会检查当前设置，并可说明、修改或删除状态栏。若只想查看原始配置，请使用 `/statusline get`。
- **JSON 输入**：每次执行时，系统通过 stdin 将当前会话信息以 JSON 格式传入命令，可用 `jq`、PowerShell `ConvertFrom-Json` 或其他可用工具解析。
- **Shell 选择**：Windows 优先使用已安装的 Git Bash，否则回退到 PowerShell；macOS/Linux 使用 `sh`。setup 子 agent 会生成适合当前平台的持久化脚本，Windows 不强制依赖 `jq`。

#### JSON 输入字段

命令执行时接收如下 JSON 数据：

| 字段 | 说明 |
|---|---|
| `session_id` | 当前会话 ID |
| `session_name` | 会话标题（通过 `/rename` 设置） |
| `cwd` | 当前工作目录 |
| `mode` | 当前模式（`agent.plan` / `agent.fast` / `code.normal` / `code.team` / `team`） |
| `model` | 当前模型名称 |
| `provider` | 模型提供商 |
| `version` | jiuwenswarm 版本号 |
| `connection` | 连接状态（`idle` / `connecting` / `connected` / `reconnecting` / `auth_failed`） |
| `theme` | 当前主题名 |
| `accent_color` | 当前强调色名 |
| `transcript_mode` | 对话显示模式（`compact` / `detailed`） |
| `transcript_fold_mode` | 折叠模式（`none` / `tools` / `thinking` / `all`） |
| `is_processing` | 是否正在处理（`true` / `false`） |
| `is_paused` | 是否暂停（`true` / `false`） |
| `is_interrupted` | 是否中断（`true` / `false`） |
| `cancellable_work` | 是否有可取消的工作（`true` / `false`） |
| `streaming_state` | 流式传输状态（`idle` / `streaming` / `tool_call` / `tool_result`） |
| `last_error` | 最近错误信息或 `null` |
| `evolution_status` | 演化状态（`idle` / `running`） |
| `active_subtask_count` | 活跃子任务数 |
| `todo_count` | 待办事项数 |
| `trusted_dirs` | 可信工作目录列表（路径字符串数组） |
| `usage.total_input_tokens` | 会话总输入 token |
| `usage.total_output_tokens` | 会话总输出 token |
| `usage.total_tokens` | 会话总 token |
| `context_window.context_window_size` | 模型最大上下文窗口 token 数（如 200000） |
| `context_window.used_percentage` | 上下文占用百分比（0-100） |
| `context_window.remaining_percentage` | 上下文剩余百分比（0-100） |

#### 命令编写模板

推荐使用以下模板编写命令。`input=$(cat)` 将 JSON 读入变量，后续用 `echo "$input" | jq -r .字段` 提取各字段。`// "默认值"` 是 jq 的备选语法，字段为空时使用默认值。

**通用公式**：

```
/statusline set 'input=$(cat); 字段1=$(echo "$input" | jq -r '.字段1 // "默认值"'); 字段2=$(echo "$input" | jq -r '.字段2 // "默认值"'); echo "格式化字符串"'
```

**推荐通用命令**（显示模式、模型、token、上下文占用、连接状态）：

```
/statusline set 'input=$(cat); mode=$(echo "$input" | jq -r '.mode // "?"'); model=$(echo "$input" | jq -r '.model // "?"'); tokens=$(echo "$input" | jq -r '.usage.total_tokens // 0'); pct=$(echo "$input" | jq -r '.context_window.used_percentage // 0'); conn=$(echo "$input" | jq -r '.connection // "?"'); echo "$mode | $model | ctx:${pct}% | tokens:$tokens | $conn"'
```

**各字段提取速查**：

| 要显示的字段 | jq 写法 |
|---|---|
| 会话名 | `jq -r '.session_name // ""'` |
| 工作目录 | `jq -r '.cwd // "?"'` |
| 模式 | `jq -r '.mode // "?"'` |
| 模型名 | `jq -r '.model // "?"'` |
| 提供商 | `jq -r '.provider // "?"'` |
| 版本号 | `jq -r '.version // "?"'` |
| 连接状态 | `jq -r '.connection // "?"'` |
| 是否在处理 | `jq -r '.is_processing // false'` |
| 是否暂停 | `jq -r '.is_paused // false'` |
| 流式状态 | `jq -r '.streaming_state // "idle"'` |
| 最近错误 | `jq -r '.last_error // ""'` |
| 演化状态 | `jq -r '.evolution_status // "idle"'` |
| 子任务数 | `jq -r '.active_subtask_count // 0'` |
| 待办数 | `jq -r '.todo_count // 0'` |
| 可信目录 | `jq -r '(.trusted_dirs // []) | join(" ")'` |
| 总输入 token | `jq -r '.usage.total_input_tokens // 0'` |
| 总输出 token | `jq -r '.usage.total_output_tokens // 0'` |
| 总 token | `jq -r '.usage.total_tokens // 0'` |
| 上下文窗口大小 | `jq -r '.context_window.context_window_size // 0'` |
| 上下文占用 % | `jq -r '.context_window.used_percentage // 0'` |
| 上下文剩余 % | `jq -r '.context_window.remaining_percentage // 0'` |

#### 更多示例

- `/statusline` — 启动内置 setup 子 agent，创建、检查、修改或删除状态栏
- `/statusline get` — 查看当前配置，不触发 setup agent
- `/statusline set 'input=$(cat); model=$(echo "$input" | jq -r .model); echo "$model"'` — 只显示模型名
- `/statusline set 'input=$(cat); proc=$(echo "$input" | jq -r .is_processing); model=$(echo "$input" | jq -r .model); echo "$proc | $model"'` — 显示是否在处理和模型名
- `/statusline set 'input=$(cat); pct=$(echo "$input" | jq -r .context_window.used_percentage); rem=$(echo "$input" | jq -r .context_window.remaining_percentage); cw=$(echo "$input" | jq -r ".context_window.context_window_size / 1000"); echo "ctx:${pct}% used (${rem}% left, ${cw}K window)"'` — 显示上下文窗口占用百分比
- `/statusline set 'input=$(cat); pct=$(echo "$input" | jq -r ".context_window.used_percentage // 0"); if [ "$pct" -ge 90 ]; then warn="⚠HIGH"; elif [ "$pct" -ge 70 ]; then warn="~MED"; else warn="OK"; fi; echo "ctx:${pct}% $warn"'` — 显示上下文占用百分比并带阈值警告（≥90% HIGH，≥70% MED）
- `/statusline set 'input=$(cat); err=$(echo "$input" | jq -r .last_error); if [ "$err" != "null" ] && [ "$err" != "" ]; then echo "error: $err"; else echo "ok"; fi'` — 有错误时显示错误信息，无错误时显示 ok
- `/statusline set 'input=$(cat); dirs=$(echo "$input" | jq -r '.trusted_dirs // [] | join(" ")'); mode=$(echo "$input" | jq -r '.mode // "?"'); echo "$mode | dirs:$dirs"'` — 显示模式与可信工作目录
- `/statusline clear` — 清除状态栏配置
- `/statusline help` — 查看使用指南（含写法模式、实用示例、可用字段）
- `/statusline json` — 查看当前实际的 JSON 数据值（方便调试 jq 表达式）

#### 行为细节

- **轮询频率**：每 2 秒自动执行一次配置的命令。
- **超时保护**：单次执行超时 3 秒后自动终止，不影响后续轮询。
- **输出限制**：命令输出超过 10KB 时截断；显示宽度自动适配 TUI 终端宽度。
- **故障静默**：命令执行失败时不显示错误，保持上一次成功输出或隐藏状态栏。
- **持久化**：配置保存在 `~/.jiuwenswarm-tui/config.json` 的 `statusLine` 字段，重启 TUI 后自动恢复。
- **别名**：`/sl`
- **Windows 适配**：运行器通过 stdin 传入 JSON，已安装 Git Bash 时使用 Git Bash，否则回退到 PowerShell；未安装 Git Bash 或 `jq` 时，建议使用持久化 PowerShell 脚本。

#### 配置文件结构

```json
{
  "statusLine": {
    "type": "command",
    "command": "input=$(cat); mode=$(echo \"$input\" | jq -r '.mode // \"?\"'); model=$(echo \"$input\" | jq -r '.model // \"?\"'); pct=$(echo \"$input\" | jq -r '.context_window.used_percentage // 0'); tokens=$(echo \"$input\" | jq -r '.usage.total_tokens // 0'); echo \"$mode | $model | ctx:${pct}% | tokens:$tokens\"",
    "padding": 0
  }
}
```

### `/auto-harness`（Auto-Harness 任务管理）

管理 Auto-Harness 任务的创建、执行与监控。Auto-Harness 通过自动化 Pipeline 生成 harness 扩展包，支持两种 Pipeline 类型：

- **optimize_expert_harness**（后端值 `extended_evolve_pipeline`）：生成本地 harness 扩展包
- **optimize_meta_harness**（后端值 `meta_evolve_pipeline`）：提交 PR（需配置 git）

Pipeline 执行过程中，扩展包**默认自动激活生效**，无需用户手动确认。日志中会展示 `harness.extension_ready`（扩展已就绪，显示目录与组件信息）和 `harness.activate_interaction`（激活确认提示）事件。

#### 配置要求

使用 `optimize_meta_harness` Pipeline 需配置以下字段（通过 `/config edit` 或 `/status config` 编辑）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `git.user_name` | 是 | Git commit 用户名 |
| `git.user_email` | 是 | Git commit 箱 |
| `git.fork_owner` | 是 | Fork 仓库所有者（如 `SnapeK`） |
| `gitcode.access_token` | 否 | GitCode API Token（也可通过环境变量 `GITCODE_ACCESS_TOKEN` 提供） |

若配置不完整，创建任务时会提示缺失字段。

#### 子命令

| 命令 | 说明 |
|---|---|
| `/auto-harness run [--pipeline <pipeline>] <query>` | 执行一次性 Auto-Harness 任务 |
| `/auto-harness schedule start --interval <hours> [--pipeline <pipeline>] <query>` | 创建定时任务 |
| `/auto-harness schedule list` | 列出所有任务 |
| `/auto-harness schedule status <task_id>` | 查看任务详情 |
| `/auto-harness schedule logs <task_id> [--history <n>]` | 查看任务执行日志 |
| `/auto-harness schedule cancel <task_id>` | 取消任务 |
| `/auto-harness schedule delete <task_id>` | 删除任务 |
| `/auto-harness issue fix <issue_numbers>` | 指定 GitCode issue 创建独立修复任务 |
| `/auto-harness issue scan [--repo <repo>] [--page <n>] [--labels <labels>] [--force-refresh]` | 扫描仓库 GitCode issue |
| `/auto-harness issue status` | 查看 GitCode issue 处理状态列表 |
| `/auto-harness issue delete <issue_numbers>` | 删除 issue 处理记录 |

#### `/auto-harness run`（一次性执行）

- 用法：`/auto-harness run [--pipeline <pipeline>] <query>`
- 流程：
  1. 若未指定 pipeline，交互式选择 Pipeline 类型
  2. 若选择 `optimize_meta_harness`，自动检查 git 配置是否完整
  3. 创建并执行一次性任务
  4. 自动进入实时日志跟踪模式（类似 `tail -f`）
- 示例：
  - `/auto-harness run 优化数据库查询性能` — 未指定 pipeline，交互选择
  - `/auto-harness run --pipeline optimize_expert_harness 优化上下文压缩能力` — 指定 pipeline

#### `/auto-harness schedule start`（创建定时任务）

- 用法：`/auto-harness schedule start --interval <hours> [--pipeline <pipeline>] <query>`
- 参数：
  - `--interval` / `-i`（必填）：执行间隔（小时），可选值 `1`、`2`、`4`、`8`、`12`、`24`
  - `--pipeline` / `-p`（可选）：Pipeline 类型，未指定时交互选择
  - `<query>`（必填）：优化目标描述
- 流程：
  1. 若未指定 pipeline，交互式选择
  2. 若选择 `optimize_meta_harness`，检查 git 配置
  3. 交互确认是否立即执行一次
  4. 创建定时任务
- 示例：
  - `/auto-harness schedule start --interval 4 优化上下文压缩能力`
  - `/auto-harness schedule start -i 2 -p optimize_meta_harness 提交数据库优化PR`

#### `/auto-harness schedule logs`（查看执行日志）

- 用法：`/auto-harness schedule logs <task_id> [--history <n>]`
- 模式：
  - 默认：实时跟踪当前运行日志（`tail -f` 模式），支持 Ctrl+C 中断
  - `--history <n>`：查看历史执行日志（`view` 模式，`n` 为历史索引，0 为最近一次）

### `/auto-harness issue`（GitCode Issue 自动处理）

管理 GitCode issue 的自动处理：扫描 issue 矩阵、创建修复任务、查看处理状态、清理记录。

需要先配置 `git.user_name`、`git.user_email` 和 `gitcode.access_token`（或 `GITCODE_ACCESS_TOKEN` 环境变量）。

#### 子命令

| 命令 | 说明 |
|---|---|
| `/auto-harness issue fix <issue_numbers>` | 为指定 GitCode issue 创建修复任务 |
| `/auto-harness issue scan [--repo <repo>] [选项]` | 扫描仓库 GitCode issue |
| `/auto-harness issue status` | 查看 issue 处理状态 |
| `/auto-harness issue delete <issue_numbers>` | 删除 issue 处理记录 |

#### `/auto-harness issue fix`（创建修复任务）

- 用法：`/auto-harness issue fix <issue_numbers>`
- 参数：
  - `<issue_numbers>`：issue 编号，多个用逗号分隔，如 `1272,1271,1270`
  - `--repo <repo>`：目标仓库，支持 `jiuwenswarm` / `agent_core`，未指定时交互选择
- 已关联 PR（open 或 merged）的 issue 自动跳过，不会创建重复任务
- 示例：
  - `/auto-harness issue fix 1286`
  - `/auto-harness issue fix 1272,1271,1270`

#### `/auto-harness issue scan`（扫描 Issue ）

- 用法：`/auto-harness issue scan`
- 参数：
  - `--repo <repo>`：目标仓库，未指定时交互选择
  - `--page <n>`：页码，默认 1
  - `--labels <labels>`：标签过滤，逗号分隔，默认只显示 bug 类型
  - `--force-refresh`：强制从 GitCode API 刷新数据（默认使用缓存）
- 展示内容：issue 编号、标题、标签、难度评估、更新时间
- 示例：
  - `/auto-harness issue scan`
  - `/auto-harness issue scan --repo jiuwenswarm --page 1`
  - `/auto-harness issue scan --repo agent_core --force-refresh`

#### `/auto-harness issue status`（查看处理状态）

- 用法：`/auto-harness issue status`（无参数）
- 以表格列出所有 issue 处理记录：编号、状态、阶段、进度、详情
- 示例：`/auto-harness issue status`

#### `/auto-harness issue delete`（删除记录）

- 用法：`/auto-harness issue delete <issue_numbers>`
- 参数：
  - `<issue_numbers>`：要删除的 issue 编号
- 示例：
  - `/auto-harness issue delete 123`
  - `/auto-harness issue delete 123 456`

### `/btw`（旁路提问）

在 **TUI 本地解析**，通过专用 RPC `command.btw` 向 AgentServer 发起一个独立的、无工具的、单轮 LLM 查询，基于当前对话上下文快速回答旁路问题，**不中断主对话**。

- **别名**：无。
- **适用模式**：全部。
- **约束**：必须提供问题文本；无对话上下文时返回 `no_context`。

#### 用法

| 命令 | 说明 |
|---|---|
| `/btw <question>` | 基于当前对话上下文，发起旁路提问 |

#### 行为细节

- **参数必填**：`/btw` 必须带问题文本，否则提示 `Usage: /btw <your question>`。
- **思考指示器**：发送请求后显示 `💭 Answering: <question>`（dim 样式）。
- **RPC 超时**：120 秒。
- **服务端处理**：
  - 后端通过 `command.btw` RPC 接收请求，获取当前 Agent 实例。
  - 与主 Agent 共享 system prompt（项目上下文、技能、CLAUDE.md 等），获取最近对话消息作为上下文。
  - 构建专用 btw prompt，通过 `<system-reminder>` 告知模型：无工具可用、单次回复、不中断主 Agent。
  - 直接调用模型（无工具、单轮），不修改对话历史（只读操作）。
- **返回状态**：
  - `ok`：显示 `💡 /btw <question>` + 回答内容。
  - `no_context`：提示 `No conversation context available yet — send a message first.`。
  - `failed`：显示错误信息或 `Couldn't answer the side question.`。

#### 示例

- `/btw what does git status do?` — 询问 git status 命令的作用
- `/btw 这段代码的时间复杂度是多少？` — 基于上下文分析算法复杂度

### `/review`（代码审查 PR）

在 **TUI** 中输入时，将原始 `/review` 文本作为聊天消息发送给 Gateway。Gateway 识别后注入 review prompt，由 Agent 使用 `gh` CLI 审查 PR。

在 **IM 受控通道**（飞书等）中，Gateway 拦截 `/review` 并注入 prompt，转发给 AgentServer 执行。

- **别名**：无。
- **适用模式**：全部（Agent、Code、Team）。
- **解析位置**：Gateway 受控通道（`scope: "gateway"`），TUI 作为聊天消息发送。

#### 用法

| 命令 | 说明 |
|---|---|
| `/review` | 无参数时，Agent 执行 `gh pr list` 展示开放 PR 列表 |
| `/review <PR 编号或 URL>` | 审查指定 PR：Agent 执行 `gh pr view/diff` 并分析 |

#### 行为细节

- **TUI 执行**：通过 `ctx.sendMessage()` 将 `/review [args]` 作为用户消息发送；离线时提示 `offline: waiting for reconnect before sending review request`。
- **Gateway 拦截**（IM 侧）：
  - 精确匹配 `/review` 或前缀匹配 `/review <arg>`。
  - 参数最长 2048 字节；含控制字符返回 `非法指令`。
  - 注入 review prompt 到 `msg.params["query"]`，继续转发给 AgentServer。
- **Agent 执行**：收到 review prompt 后，Agent 使用 `gh` CLI：
  1. 无参数时运行 `gh pr list` 展示开放 PR 列表。
  2. 有参数时运行 `gh pr view <number>` 获取详情、`gh pr diff <number>` 获取 diff。
  3. 分析变更并提供全面审查（正确性、约定、性能、测试覆盖、安全）。
- **无 git/gh 预检**：Gateway 不检查 `git` 或 `gh` 是否安装，由 Agent 自行处理。

#### 示例

- `/review` — 列出当前仓库的开放 PR
- `/review 123` — 审查 PR #123

### `/security-review`（安全审查）

在 **TUI** 中输入时，将原始 `/security-review` 文本作为聊天消息发送给 Gateway。Gateway 识别后注入安全审查 prompt，由 Agent 使用 `git` 命令分析当前分支的待定变更。

在 **IM 受控通道**（飞书等）中，Gateway 拦截 `/security-review` 并注入 prompt，转发给 AgentServer 执行。

- **别名**：无。
- **适用模式**：全部（Agent、Code、Team）。
- **解析位置**：Gateway 受控通道（`scope: "gateway"`），TUI 作为聊天消息发送。

#### 用法

| 命令 | 说明 |
|---|---|
| `/security-review` | 审查当前分支相对于 `origin/HEAD` 的所有待定变更 |
| `/security-review <附加说明>` | 附带焦点说明或约束（如"重点关注认证模块"） |

#### 行为细节

- **TUI 执行**：通过 `ctx.sendMessage()` 将 `/security-review [args]` 作为用户消息发送；离线时提示 `offline: waiting for reconnect before sending security review request`。
- **Gateway 拦截**（IM 侧）：
  - 精确匹配 `/security-review` 或前缀匹配 `/security-review <arg>`。
  - 参数最长 2048 字节；含控制字符返回 `非法指令`。
  - 注入安全审查 prompt 到 `msg.params["query"]`，继续转发给 AgentServer。
- **Agent 执行**：收到安全审查 prompt 后，Agent 执行以下步骤：
  1. **仓库上下文研究**：`git status`、`git diff --name-only origin/HEAD...`、`git log` 获取变更概览。
  2. **比较分析**：`git diff origin/HEAD...` 逐文件审查 diff。
  3. **漏洞评估**：按以下类别审查：
     - 输入验证漏洞
     - 身份认证和授权问题
     - 密码学与密钥管理
     - 注入与代码执行
     - 数据暴露
  4. 使用子任务识别漏洞、并行子任务进行误报过滤，仅报告置信度 > 80% 的发现。
  5. 输出结构化 Markdown 报告：文件、行号、严重级别、类别、描述、利用场景、修复建议。
- **硬排除列表**：不报告拒绝服务、密钥存储、限速、竞态条件等问题类型。
- **无 git 预检**：Gateway 不检查 `git` 是否安装，由 Agent 自行处理。

#### 示例

- `/security-review` — 审查当前分支所有待定变更
- `/security-review 重点关注认证模块的安全性` — 附带焦点说明

---

## 待开发

（暂无）
---

## 返回导航

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)