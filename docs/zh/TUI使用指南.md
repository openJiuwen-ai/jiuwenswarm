# JiuwenSwarm TUI 使用指南

> 本文档面向 **JiuwenSwarm 终端界面（`jiuwenswarm-tui` / `jiuwenswarm-cli`）** 用户，结构与 [Claude Code CLI 参考](https://code.claude.com/docs/zh-CN/cli-reference) 类似：先列 **CLI 启动参数**，再列 **Slash 命令**，接着是 **工具参考** 与 **交互模式**，并对 **Code 模式** 做重点说明。  
> 行为以仓库代码为准：`jiuwenswarm/cli/src/index.ts`、`jiuwenswarm/cli/src/core/commands/registry.ts` 及各 `builtins/*.ts`。

---

## 启动前提

- TUI 通过 **WebSocket** 连接本机 Gateway 的 TUI 端点（默认 `ws://127.0.0.1:19001/tui`）。请先按 [快速开始(TUI)](Quickstart_tui.md) 启动后端服务，再打开 TUI。
- TUI 需要 **交互式 TTY**；在管道或非 TTY 环境下会报错退出。

### 多窗口 TUI

同一套 **Gateway 后端**（同一 `GATEWAY_PORT`，默认 `ws://127.0.0.1:19001/tui`）可**同时打开多个 TUI 终端窗口**。每个窗口维护独立的 `session_id` 与进行中的对话/任务：

| 行为 | 说明 |
|------|------|
| **事件隔离** | Gateway 按 `session_id` 将流式响应、工具输出等事件精确投递到对应窗口，不会串到其他窗口。 |
| **并发任务** | 不同 session 可并行执行 Agent 任务。 |
| **同窗口新消息** | 在同一窗口（相同 `session_id`）再次发送聊天，仍会取消该 session 上旧的流式任务。 |
| **与 ACP 差异** | ACP 仍为 single-user channel（新消息会取消同 channel 上所有进行中任务）；TUI/CLI 已改为按 session 隔离。 |

**打开多窗口**：在多个终端分别运行 `jiuwenswarm-tui` 或 `jiuwenswarm-cli` 即可。需要恢复或指定会话时使用 `--session <id>`（启动时，详见下文 [CLI 参考](#cli-参考) 中的 `--session` 小节）或运行时 `/resume`。注意：同一 `--session <id>` 在两个窗口同时启动会被 Gateway 拒绝（`SESSION_IN_USE`），需先关闭另一窗口。

> **与「单机多实例」的区别**：[单机多实例运行](单机多实例运行.md) 指不同工作区、不同端口的独立后端；**多窗口 TUI** 指多个终端共享同一 Gateway 后端。二者可同时使用（例如 `dev` 实例上开两个 TUI 窗口），但不要混淆端口与工作区。

---

## CLI 参考

### 启动方式

| 方式 | 说明 |
|------|------|
| `jiuwenswarm-tui` | 通过 `jiuwenswarm-tui` PyPI 包启动时，由包装器拉起对应平台的二进制（见 `packages/jiuwenswarm-tui`）。 |
| `jiuwenswarm-cli` | 源码/开发路径下，在 `jiuwenswarm/cli` 执行 `npm run dev` 或 `npm run start` 后使用 `jiuwenswarm-cli`（见 `package.json` 的 `bin`）。 |

以下 **命令行标志** 由 `jiuwenswarm/channels/tui/frontend/src/index.ts` 中的 `parseArgs` 定义：

| 标志 | 说明 | 默认值 | 示例 |
|------|------|--------|------|
| `--url <url>` | Gateway 的 CLI WebSocket 地址 | `ws://127.0.0.1:19001/tui` | `jiuwenswarm-cli --url ws://192.168.1.10:19001/tui` |
| `--session <id>` | 启动时按 id **恢复或新建**会话（详见下节） | 无 | `jiuwenswarm-tui --session tui_myproj_001` |
| `--token <token>` | 鉴权令牌（若 Gateway 需要） | 空字符串 | `jiuwenswarm-cli --token YOUR_TOKEN` |
| `-h`, `--help` | 打印帮助并退出 | - | `jiuwenswarm-cli -h` |

### `--session`：按 id 恢复或新建会话

`--session <id>` 让 TUI 在启动时**以指定 session id 为身份**连接后端。连接建立后，TUI 统一向 AgentServer 注册该 id；由于身份来自外部，这条兼容路径明确绕过预热池。

| id 状态 | 启动行为 | 后端 RPC |
|------|------|------|
| **已存在** | AgentServer 保留已落盘的项目与模式绑定，执行切换生命周期，随后前端拉取并回显历史 | 显式 ID 的 `session.create` + `history.get` + `session.rename`（取标题） |
| **不存在** | AgentServer 校验 id，在该 id 的互斥锁内解析 TUI 项目并写入 `metadata.json`，以空历史启动且不领取预热实例 | 显式 ID 的 `session.create` + `history.get`（空）|

显式 ID 的 `session.create` 仅对 TUI 开放且保持幂等，AgentServer 会记录该兼容请求绕过预热。该逻辑在收到 `connection.ack` 后由 `app-state.ts` 的 `initializeBootSession` 放行，重连时只执行一次；普通启动、`/new` 和 `/clear` 不传 `session_id`，仍由 AgentServer 分配新 ID。

**示例**：

```bash
# 首次用某 id 启动 → 不存在 → 新建落盘
jiuwenswarm-tui --session tui_myproj_001
# 在 TUI 内对话若干轮后退出。该 id 已落盘到 ~/.jiuwenswarm/agent/sessions/tui_myproj_001/

# 再次用同 id 启动 → 已存在 → 恢复并回显历史
jiuwenswarm-tui --session tui_myproj_001
```

**与运行时 `/resume` 的关系**：`--session` 是**启动时**的外部 id 兼容入口，调用显式 ID 的 `session.create`；`/resume` 是 TUI 已启动后切换到已有会话的命令，调用 `session.switch`。

**id 命名约束**（前端在启动前校验，不合规直接报错退出，不进入 TUI）：

| 约束 | 值 | 原因 |
|------|------|------|
| 长度上限 | ≤ 128 字符 | `session_id` 直接作为目录名落地（`~/.jiuwenswarm/agent/sessions/<id>/`），受文件系统单项 255 字符限制，留路径前缀余量 |
| 允许字符 | `A-Z a-z 0-9 . _ -` | 与 `generateSessionId` 产出的 `tui_<hex>_<hex>` 同字符集 |
| 禁止字符 | 中文、空格、`/ \ : * ? " < > \|` 等 | 防止目录注入（`/` 会建出嵌套目录导致会话丢失）与跨平台 `mkdir` 失败 |

不合规示例：

```bash
jiuwenswarm-tui --session 测试会话       # 含中文 → 启动即退出：--session <id> 含非法字符
jiuwenswarm-tui --session "my session"   # 含空格 → 同上
jiuwenswarm-tui --session a/b            # 含 / → 同上
jiuwenswarm-tui --session "$(printf 'a%.0s' {1..200})"  # 超 128 → 长度超限
```

**不传 `--session`**：前端生成随机 id（`generateSessionId` → `tui_<hex>_<hex>`），后端目录在**首条对话写入历史时**懒创建。即未传参启动、未发消息即退出 → 后端无目录、`/sessions` 列表不显示该 id（空会话不污染列表）。传 `--session` 启动即建目录，**即使不发言也会出现在列表**，方便下次恢复——这是两者关键行为差异。

### 连接到非标准端口（端口冲突回退后）

当后端启动时端口被占用，`jiuwenswarm-start` 会自动回退到相邻可用端口组（详见 [快速开始 · 端口冲突自动处理](Quickstart.md#端口冲突自动处理)）。回退后新端口会被持久化，TUI / CLI 有两种连接方式：

1. **自动读取**（推荐）：`jiuwenswarm chat` 和 `jiuwenswarm-tui` 会自动从持久化的 `GATEWAY_PORT` 读取回退后的 Gateway 端口，无需任何参数。

   ```bash
   jiuwenswarm chat        # 自动读 GATEWAY_PORT
   jiuwenswarm-tui         # 自动读 GATEWAY_PORT
   ```

2. **显式 `--url`**：若 TUI 未读到持久化端口（例如在另一台机器、或持久化文件被清理），按启动日志里的提示显式指定：

   ```bash
   jiuwenswarm-tui --url ws://127.0.0.1:20001/tui
   ```

   启动后端时日志会打印实际的 Gateway 端口和对应的 `--url`，直接复制即可。

### 启动后界面

- **欢迎区**：ASCII 标题、版本、当前 Provider / Model / Mode；窄终端下为精简布局（`jiuwenswarm/cli/src/ui/welcome.ts`）。
- **连接提示**：未连上后端时会提示检查 Gateway 或 `--url`；鉴权失败时提示检查 `--token`。
- **ripgrep 提示**：若本机未安装 `rg`，会提示安装以优化文件搜索。

---

## Slash 命令参考

### 一览：按解析位置区分

| 类型 | 说明 |
|------|------|
| **TUI 本地** | 在 `CommandService` 中注册，输入后由 TUI 直接执行，不当作普通聊天发给 Agent。 |
| **经 TUI 调后端 RPC** | 仍为本地解析的命令，但内部通过 `ctx.request(...)` 或 `sendMessage` 调用 AgentServer / Gateway 能力。 |

当前 **已注册** 的顶层命令来自 `createBuiltinCommands()`（`registry.ts`），按名称排序如下表。

> **与文档 [Slash命令表.md](Slash命令表.md) 的差异**：`jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/` 下另有 `cancel.ts`、`new.ts`、`sessions.ts` 等实现文件，但当前没有把它们注册为独立顶层命令。`/new` 仍然可用，因为它是 `/clear` 的别名；默认构建没有独立 `/cancel`，中断任务请优先使用 **`Ctrl+C`**（第一次中断，连按两次退出）。`/switch` 仅在 `agentos-tui` 托管环境（`AGENTOS_TUI_SUPERVISED=1`）中注册，用于 `/switch claude`、`/switch list` 等第三方 TUI handoff；它不同于 Gateway 受控频道的模式切换指令。Gateway 侧行为仍以 `jiuwenswarm/gateway/slash_command.py` 与 Slash命令表为准。

### 命令总表

| 命令 | 别名 | 用途 | 示例 | 适用模式 |
|------|------|------|------|----------|
| `/help` | - | 列出已注册 Slash 命令 | `/help` | 全部 |
| `/keybindings` | `/keybind` | 查看/编辑/重置 TUI 快捷键配置 | `/keybindings`、`/keybindings list` | 全部 |
| `/hooks` | - | 浏览已配置的 hooks（只读） | `/hooks` | 全部 |
| `/exit` | `/quit` | 退出 TUI | `/exit` | 全部 |
| `/clear` | `/reset`, `/new` | 新建会话 ID、清空当前 transcript（忙时拒绝） | `/clear` | 全部 |
| `/copy` | - | 复制最近第 N 条助手回复到剪贴板 | `/copy` 或 `/copy 2` | 全部 |
| `/theme` | - | 切换深/浅色主题 | `/theme dark` | 全部 |
| `/color` | - | 设置提示条强调色 | `/color blue` | 全部 |
| `/compact` | - | 压缩上下文，保留摘要 | `/compact` | 全部 |
| `/config` | `/settings`, `/setting` | 查看/设置后端配置 | `/config`、`/config get`、`/config set key value` | 全部 |
| `/context` | - | 查看上下文窗口占用与 Token 用量明细 | `/context` | 全部 |
| `/diff` | - | 交互式回顾按轮次 diff + 未提交工作树改动 | `/diff` | 全部 |
| `/evolve` | - | 触发技能演进 | `/evolve myskill 修正错误处理` | `agent.plan` / `team`（见下） |
| `/evolve_list` | - | 列出某技能的演进条目 | `/evolve_list myskill --sort score` | `agent.plan` / `team` |
| `/evolve_rebuild` | - | 从归档与演进记录重建 SKILL.md | `/evolve_rebuild myskill 强化错误处理` | `agent.plan` / `team` |
| `/evolve_simplify` | - | 整理、合并某技能的演进经验 | `/evolve_simplify myskill 合并重复经验` | `agent.plan` / `team` |
| `/init` | - | 在 **Code 模式** 下初始化 `JIUWENSWARM.md` / `JIUWENSWARM.local.md` | `/init` | **仅 `code.*`** |
| `/mcp` | - | 管理 MCP 服务 | `/mcp list`、`/mcp add ...` | 全部 |
| `/mode` | - | 切换或查看模式 | `/mode`、`/mode code`、`/mode team` | 全部 |
| `/permissions` | - | 设置 `permissions.tools` 中单工具的 allow/ask/deny | `/permissions ask write_file` | 全部 |
| `/plan` | - | 进入 Agent 规划模式，或发送规划请求 | `/plan`、`/plan open`、`/plan 迁移步骤` | 非 `team` |
| `/rename` | - | 查看/重命名/清空当前会话标题 | `/rename`、`/rename 标题`、`/rename clear` | 全部 |
| `/review` | - | 审查 PR（TUI 发送聊天消息，Gateway 拦截注入 prompt） | `/review`、`/review 123` | 全部 |
| `/resume` | `/continue` | 列出或恢复历史会话；无参 `/resume` 与 `/continue` 在 TUI 中可打开交互列表（见下） | `/resume list`、`/resume <id>` | 全部 |
| `/skills` | - | 技能与市场源管理 | `/skills`、`/skills install ...` | 全部 |
| `/teamskills` | - | TeamSkills Hub（初始化、校验、打包、搜索、安装等） | `/teamskills list` | 全部 |
| `/model` | - | 查看/新增/切换模型 | `/model`、`/model add name k=v` | 全部 |
| `/workspace` | `/workspace_dir`, `/workspace-dir` | 管理文件操作可信目录 | `/workspace add .` | 全部 |
| `/export` | - | 导出当前会话到文件或剪贴板 | `/export`、`/export my-chat` | 全部 |
| `/status` | - | 查看运行状态概览、用量、配置 | `/status`、`/status usage` | 全部 |
| `/agents` | - | 管理 Agent 配置（list, get, create, update, enable, disable, delete） | `/agents list`、`/agents get Explore` | 全部 |
| `/branch` | `/fork` | 从当前对话点创建分支会话 | `/branch fix-login-bug` | 全部 |
| `/btw` | - | 旁路快速提问，不中断主对话 | `/btw what does git status do?` | 全部 |
| `/rewind` | `/checkpoint` | 回退对话到指定轮次之前 | `/rewind 2` | 全部 |
| `/memory` | `/mem` | 记忆管理（状态、文件、开关、目录） | `/memory status` | 全部 |
| `/sandbox` | - | 进出沙箱模式 / 管理 excluded_commands / files | `/sandbox enable`、`/sandbox status`、`/sandbox files allow ./tmp/` | 全部 |
| `/security-review` | - | 安全审查当前分支待定变更 | `/security-review`、`/security-review 重点关注认证` | 全部 |
| `/simplify` | - | 代码精简审查（复用性、质量、效率），自动修复问题 | `/simplify`、`/simplify src/auth/` | **仅 `code.*`** |
| `/swarmflow` | - | SwarmFlow 开关/状态/预算（`on`/`off`/`--budget`） | `/swarmflow on` | **推荐 `team`** |
| `/swarmflows` | `/swarmworkflows` | 全屏 SwarmFlow 运行树 | `/swarmflows` | **推荐 `team`**（需 `/swarmflow on`） |

> SwarmFlow 完整说明见 **[TUI 使用 SwarmFlow 指南](TUI使用SwarmFlow指南.md)**。

#### `/resume` 与 `/continue` 在 TUI 中的特殊行为

- 输入 **`/resume`** 或 **`/continue`** 且 **无其它参数** 时，TUI 会打开 **交互式会话选择器** 供选择恢复，而不走纯文本 `session.list` 展示（`app-screen.ts`）。
- 带参数时仍走命令实现：`/resume list`、`/resume <conversation_id>` 等。

交互式选择器的快捷键：

| 按键 | 功能 |
| --- | --- |
| `↑` / `↓` | 在会话间移动焦点 |
| `Enter` | 恢复当前焦点的会话 |
| 输入字符 | 实时搜索（按会话 ID / 标题 / 项目目录过滤） |
| `Backspace` | 删除搜索字符 |
| `Space` | 预览当前焦点会话的信息卡（标题、ID、项目目录、分支、消息数、最近活跃/创建时间）；预览态 `Enter` 恢复、`Space`/`Esc` 返回 |
| `Ctrl+R` | 重命名当前焦点会话；编辑态 `Enter` 保存、`Esc` 取消、留空则清除标题 |
| `Ctrl+A` | 在「全部项目」与「仅当前项目」范围间切换 |
| `Ctrl+B` | 开关 git 分支过滤（仅显示 `git_branch` 严格等于当前项目分支的会话） |
| `Esc` | 有搜索词时清空搜索；否则关闭选择器 |

> 上表 `Space` / `Ctrl+R` / `Ctrl+A` / `Ctrl+B` / `Esc` 属于 **`ResumeList` context**，可通过 `/keybindings` 重绑；预览态、重命名编辑态内的 `Enter` / `Esc` / `Backspace` 及搜索框文本输入仍为硬编码。

行为说明：

- **默认列出全部项目** 的会话（进入后可按 `Ctrl+A` 切回仅当前项目）。当前项目无会话时仍会打开（空）选择器，便于按 `Ctrl+A` 查看其它项目。
- **分支记录与过滤（`Ctrl+B`）**：会话首条消息时会按其 `project_dir` 记录 git 分支（非 git/detached 记为 `HEAD`）。开启过滤后按「分支名」**严格匹配**当前项目分支，存量无分支记录的会话与 `HEAD` 会话都会被过滤掉；关掉过滤即可看到全部。注意分支过滤仅按名字比对，不区分仓库——「全部项目 + 分支过滤」同时开启时，不同目录下的同名分支会一并显示。
- **恢复范围**：resume 仅恢复 **会话上下文**（对话历史、会话 ID、accent 颜色、workflow 快照、窗口标题），**不切换 workspace / 当前工作目录**。

---

### 重点命令说明

#### `/mode` 与子模式切换

- **`/mode`**（`mode.ts`）  
  - 无参数：显示当前模式。  
  - TUI 当前接受：`agent`、`plan`、`agent.plan`、`agent.fast`、`code`、`code.normal`、`code.team`、`team`、`team.normal`。
  - 实际映射：`agent` / `plan` → `agent.plan`；`code` → `code.normal`；`team.normal` → `team`；其它直达值保持不变。
  - 切换时会尝试调用 `mode.set` RPC，同时更新本地 UI 状态；如果后端不支持 `mode.set`，TUI 仍会在后续发送消息时通过当前模式传递。
  - 从 `team` / `code.team` 离开 Team 族且当前有 Team 任务运行时，TUI 会先弹出确认，确认后发送 `chat.interrupt` 再切换。
- **与 Gateway 受控通道的差异**：受控通道只接受 `/mode agent|code|team|agent.plan|agent.fast|code.normal|code.team`；`/mode plan`、`/mode team.normal` 是 TUI 本地命令能力，不属于 Gateway slash 白名单。
- **同族子模式**：可用 **`/mode agent.fast`**、**`/mode code.normal`**、**`/mode code.team`** 等直达；**`/switch plan|fast|normal|team`** 在 `switch.ts` 中实现，但 **默认 TUI 注册表未注册**。

#### `/workspace`（可信目录）

- 系统默认工作空间：`~/.jiuwenswarm/agent/workspace`（始终可用）。
- `add`：默认路径为当前工作目录；成功后会 `command.add_dir` 同步到服务端并 `remember: true`。
- `set`：重置为单个可信目录；若已有列表会二次确认。
- 详见 [Slash命令表.md](Slash命令表.md) 的 `/workspace` 小节。

#### `/agents`（Agent 管理）

管理自定义 Agent（子代理）的全生命周期：查看、创建、更新、启用/禁用、删除。Agent 定义支持四级来源，按优先级 project > user > local > builtin 覆盖。

- **解析位置**：TUI 本地解析，通过 RPC 调用后端 `agents.*` 端点。
- **注意**：该命令在注册表中标记为 `hidden: true`，不会出现在 `/help` 列表中，但可以直接使用。

**子命令**：

| 子命令 | 用法 | 说明 |
|--------|------|------|
| `list` | `/agents list` | 列出所有 Agent（名称、来源、启用状态、描述） |
| `get` | `/agents get <name>` | 查看指定 Agent 详细信息（含 System Prompt 正文） |
| `create` | `/agents create [--project\|--local] <名称> <描述>` | 创建自定义 Agent，LLM 自动生成 prompt |
| `update` | `/agents update <name> [--generate] <新描述>` | 更新 Agent 描述；加 `--generate` 由 LLM 重写 prompt |
| `enable` | `/agents enable <name>` | 启用自定义 Agent |
| `disable` | `/agents disable <name>` | 禁用自定义 Agent |
| `delete` | `/agents delete <name>` | 删除自定义 Agent |

**Agent 来源**：

| 来源 | 存储位置 | 说明 |
|------|----------|------|
| `builtin` | 代码内置 | 系统预置 Agent，不可启用/禁用/删除 |
| `user` | `~/.jiuwenswarm/agents/` | 用户级 Agent（默认 `create` 位置） |
| `project` | `<workspace>/.jiuwenswarm/agents/` | 项目级 Agent（`--project`） |
| `local` | `<workspace>/.jiuwenswarm/agents-local/` | 本地 Agent（`--local`） |

**`create` 行为要点**：
- 默认由 LLM 自动生成 `when_to_use` 和 `system_prompt`（失败时回退到内置模板）。
- 创建成功后自动写入 `config.yaml` 的 `react.subagents.<name>.enabled = true` 并热加载。
- 响应面板显示 LLM 生成标记、文件路径。
- 超时：60 秒。

**`update` 行为要点**：
- 无描述参数时展示当前 Agent 详情（同 `get`）。
- `--generate` 标志触发 LLM 重写 prompt（默认不使用 LLM，用请求中的模板值）。
- 更新后自动热加载配置。

**`enable` / `disable` 约束**：
- 不能对内置 Agent（`source: builtin`）执行启用/禁用操作。

**`delete` 约束**：
- 删除后自动从 `config.yaml` 的 `react.subagents` 中移除并热加载。
- 内置 Agent 不可删除。

**`get` 详细信息字段**：
名称、描述、状态、来源、调用时机、模型、颜色、权限模式、记忆范围、最大迭代、工具列表、禁用工具、技能列表、文件路径、System Prompt 正文。

**无参数行为**：`/agents` 无参数时等同 `/agents list`，列出所有 Agent。

**Tab 补全**：`get`、`update`、`enable`、`disable`、`delete` 子命令支持按 Agent 名称 Tab 补全。

#### `/init`（仅 Code 模式）

- 必须在 `code.*` 下执行；否则提示先 `/mode code`。
- 需能解析工作目录：优先 `trustedDirs[0]`，否则 `process.cwd()`；无法解析时提示先 `/workspace set <path>`。
- 交互选择范围：团队共享 `JIUWENSWARM.md`、个人 `JIUWENSWARM.local.md` 或两者；然后向后端发送编排提示（`logAsUser: false`）。
- 详见 [Slash命令表.md](Slash命令表.md) 与源码 `init.ts`。

#### `/diff`（交互式改动回顾）

- **`/diff`**：调用 `command.diff`（60s 超时），处理器从请求元数据解析 `session_id` 与 `project_dir`，并行获取两组数据后打开 **交互式 Diff 查看器**（全屏覆盖模式）：
  - **按轮次改动**：基于 `.agent_history` 文件操作日志计算，涵盖 agent 工作区、用户工作区和项目目录三处日志，去重合并后按用户消息边界划分轮次；
  - **工作树改动**：基于 `git diff HEAD` 获取未提交的已跟踪文件改动。

  **快捷键**：

  | 按键 | 功能 |
  |------|------|
  | `↑` / `↓` | 在文件列表间移动焦点 |
  | `Enter` | 查看焦点文件的完整 diff（进入详情视图） |
  | `Esc` / `Ctrl+C` | 详情视图 → 返回列表；列表视图 → 关闭 |
  | `←` | 从详情视图返回列表 |
  | `PgUp` / `PgDn` | 详情视图上下翻页 |
  | `Home` / `g` | 列表 → 跳至顶部；详情 → 跳至文件开头 |
  | `End` / `Shift+g` | 列表 → 跳至底部；详情 → 跳至文件末尾 |

  **效果边界**：
  - 同一文件在工作树和某轮次中均出现时会重复列出，来源标注为 `working` 或 `Turn N`；
  - 按轮次 diff 仅追踪 agent 的文件操作日志，不覆盖手动或 bash 编辑的文件；
  - git diff 部分仅覆盖已跟踪文件的未提交改动，不包含未跟踪文件和已提交历史；
  - 在 merge/rebase/cherry-pick/revert 等瞬态 git 状态下，git diff 返回 `None`；
  - 单文件 hunk 超过 400 行会被截断；单文件 diff 超过 1 MB 跳过 hunk 解析；变更文件超过 500 时仅返回统计；
  - 非 git 仓库或无法解析 `project_dir` 时，仅返回按轮次 diff；
  - 详见 [Slash命令表](Slash命令表.md#diff交互式改动回顾)。

- **`/compact`**：调用 `command.compact`，返回 `busy` | `compressed` | `noop`；成功时展示 token 节省比例（`compact.ts`）。

#### `/context`（上下文窗口用量）

- **`/context`**（`context.ts`）
  - 无参数，无子命令。
  - 调用 `command.context` RPC，携带当前 `mode`，获取上下文窗口占用与 Token 用量明细。
  - 展示分为多个面板：
    - **概览面板**：进度条 + 占用百分比；`context_window`（已用/上限 tokens）、`occupancy`（占用率）、`messages`（消息数）。
    - **Token 拆分面板**：按 `system_prompt`、`messages`、`tools`、`total` 展示。
    - **DeepAgent 占用明细**（如有数据）：`context_occupancy` 键值列表。
    - **DeepAgent 用量明细**（如有数据）：`deepagent_usage` 键值列表。
  - 阈值提示：占用率 >= 90% 时，提示 `Context window 90% full — consider /compact`。
  - 错误处理：请求失败时显示 `context failed: <错误信息>`。
  - 与 `/status usage` 的区别：`/context` 侧重**实时上下文窗口占用**，含进度条和阈值告警；`/status usage` 侧重**会话累计用量统计**与**按模型拆分**。

#### `/config`

- 子命令：`get`、`set`、`list`、`edit`、`reset`；无参数时展示分组概览。
- `set` 会校验 schema 中的 key；`toggle` 类型可省略 value 表示翻转。
- 敏感字段展示会掩码。

#### `/model`

- `/model add <name> key=value ...` 新增模型配置。
- `video` / `audio` / `vision` 不能作为默认聊天模型切换，需用 `/config`。
- 详见 [Slash命令表.md](Slash命令表.md)。

#### `/mcp`

- 子命令：`list`、`show`、`add`、`update`、`enable`、`disable`、`remove`（`delete` 同 `remove`）。
- `stdio`：需 `--command`，可选 `--args`、`--cwd`、`--env`。
- `sse`：需 `--url`，可选 `--headers`、`--timeout_s`。
- 详见 [Slash命令表.md](Slash命令表.md) 与 `mcp.ts`。

#### `/skills` 与 `/teamskills`

- **`/skills`**：默认等价 `list`；子命令含 `install`、`uninstall`、`marketplace`、`use` 等；部分长操作有 120s 超时（见 `skills.ts`）。
- **`/teamskills`**：无子命令时打印用法提示；支持 `init`、`validate`、`pack`、`info`、`search`、`list`、`install`、`uninstall`、`config`、`publish`、`delete`（见 `teamskills.ts` 与 Slash命令表）。

#### `/evolve*`（Skill 自演进）

这组命令在 TUI 本地注册（`evolve.ts`），但业务逻辑不在前端执行：TUI 只做必要参数校验，然后通过 `sendMessage(...)` 把原始 slash 文本发给后端。后端在 Agent / Team 流程中拦截并调用 SkillEvolutionRail / TeamSkillEvolutionRail。

| 命令 | 用途 | 行为要点 |
|------|------|----------|
| `/evolve <skill_name> [user_query]` | 为单个 Skill 生成演进记录 | `agent.plan` 下会先扫描当前会话中的工具失败和用户纠错信号；若没有信号且未给 `user_query`，返回“未发现明确演进信号”。Team 模式必须提供 `<user_query>`。 |
| `/evolve_list <skill_name> [--sort score]` | 查看某 Skill 的经验库 | 展示记录数、平均分、使用/反馈统计、目标 section 与内容预览；当前实现按 score 获取记录。 |
| `/evolve_simplify <skill_name> [user_intent]` | 智能整理经验库 | 生成可审批的整理方案，用于合并、拆分或清理演进经验。尾随文本会作为整理意图传给后端，不是独立 CLI flag。 |
| `/evolve_rebuild <skill_name> [user_intent]` | 重建 SKILL.md | 由后端生成 follow-up prompt，并继续作为普通 Agent / Team 任务执行，用归档历史与演进记录重建 Skill 文档。 |

适用条件：

- `agent.plan`：用于单 Agent Skill 自演进；其它 Agent / Code 子模式不处理这组命令。
- `team`：使用团队技能演进 rail；`/evolve <skill_name> <user_query>`、`/evolve_list`、`/evolve_simplify`、`/evolve_rebuild` 可用。
- 无参数 `/evolve` 仅在 `agent.plan` 下返回待处理演进记录摘要；Team 模式会要求补充 Skill 名称和演进意图。

审批与状态：

- `/evolve` 和 `/evolve_simplify` 生成变更后不会静默写入，会推送 `chat.ask_user_question`，TUI 进入确认态，用户确认后才由后端接受或丢弃记录。
- Team 技能演进确认后会同步团队技能；拒绝则丢弃本次生成内容。
- 后端推送 `chat.evolution_status` 时，TUI 会把演进状态标记为 running / idle；演进或审批未完成时补充输入会先排队，等待演进完成后再发送。

更多机制说明见 [Skill 自演进](Skill自演进.md)。

#### `/plan`

- 在 `team` 模式下不可用。
- 无参数：进入 `agent.plan`。
- `open`：仅提示已进入规划模式。
- 其它文本：在切换到 plan 后作为规划请求发送。

#### `/permissions`

- 用法：`/permissions <allow|ask|deny> <tool_name>`
- 调用 `permissions.tools.update`，写入配置中的 per-tool 策略。

#### `/export`

- 无参数：复制当前对话到剪贴板；剪贴板不可用时提示指定文件名。
- `/export <filename>`：将对话写入工作空间目录下的 `.txt` 文件（自动追加 `.txt` 后缀）。
- 输出格式为纯文本，每条消息按 `[User]`、`[Assistant]`、`[Thinking]`、`[Tools]` 等角色前缀与时间戳逐条渲染。
- 支持 Tab 补全：自动生成 `<时间戳>-<首条提示>.txt` 和 `conversation-<时间戳>.txt` 建议。
- 详见 [Slash命令表.md](Slash命令表.md) 的 `/export` 小节。

#### `/status`

- `/status`：显示完整状态概览（版本、会话、模型、连接、MCP 服务、配置来源）。
- `/status usage`：显示当前会话 token 用量统计（含按模型拆分）。
- `/status config`：进入交互式配置编辑器。
- 若 TUI 提供 StatusView，会打开带标签页的交互界面；否则回退为内联键值展示。
- 详见 [Slash命令表.md](Slash命令表.md) 的 `/status` 小节。

#### `/branch`（分支会话）

- 别名：`/fork`。
- 约束：当前会话忙时或无对话记录时拒绝执行。
- 行为：TUI 调用 `session.fork` 时发送当前 `source_session_id` 与可选标题，由 AgentServer 分配并返回新 `session_id`；随后 TUI 自动切换到新分支会话，清空 transcript 并恢复分支历史。提示用户可用 `/resume <原会话ID>` 返回原会话。
- 示例：`/branch`、`/branch fix-login-bug`。

#### `/btw`（旁路提问）

- **别名**：无。
- **适用模式**：全部。
- 在 TUI 本地解析，通过专用 RPC `command.btw` 向 AgentServer 发起独立、无工具、单轮 LLM 查询，基于当前对话上下文快速回答旁路问题，**不中断主对话**。
- **参数必填**：`/btw <question>`，无参数时提示 `Usage: /btw <your question>`。
- 发送后显示 `💭 Answering: <question>`（dim 样式），RPC 超时 120 秒。
- 返回状态：
  - `ok`：显示 `💡 /btw <question>` + 回答。
  - `no_context`：提示无对话上下文。
  - `failed`：显示错误信息。
- 服务端与主 Agent 共享 system prompt，直接调用模型（无工具、单轮），不修改对话历史（只读操作）。
- 示例：`/btw what does git status do?`、`/btw 这段代码的时间复杂度是多少？`

#### `/rewind`（回退对话）

- 别名：`/checkpoint`。
- 约束：当前会话忙时或无对话轮次时拒绝执行。
- 交互流程：
  1. 无参数时先展示轮次列表（含时间、文件变更统计），供用户选择目标轮次。
  2. 选择后展示恢复选项：
     - **Restore conversation and code** — 截断对话并恢复文件；
     - **Restore conversation only** — 仅截断对话；
     - **Restore code only** — 仅恢复文件（仅当目标轮次有文件变更时显示）；
     - **Cancel** — 取消。
  3. 对应调用 `session.rewind_and_restore`、`session.rewind` 或 `session.restore_files`。
- 回退后：TUI 清空 transcript 并重新加载历史；若回退内容包含用户输入，会自动填入输入框。
- 局限：回退不影响通过 bash 命令或手动编辑的文件。
- 示例：`/rewind`（交互式）、`/rewind 2`（直接回退到第 2 轮前）。

#### `/memory`（记忆管理）

- 别名：`/mem`。
- 无参数时打开**页签控制台**（仿 StatusView），含 4 个页签：`edit` / `status` / `toggle` / `open`，默认停在 `edit` 页签。
- 子命令：
  - `edit [path]` — 编辑记忆文件；无参数时进入 `edit` 页签（展示 Project memory（Checked in at）/ Local memory（Saved in）/ User memory（Saved in），Enter 用 `$EDITOR` 打开），带路径时直接用 `$EDITOR` 打开（不打开页签控制台）。
  - `status` — 打开 `status` 页签，展示：Engine（`builtin (local)`）、按模式自适应的开关行（`✓ on` / `✗ off`）、运行时记忆统计（agent 模式 "Auto Memory"、code 模式 "Coding Memory"）、Project Memory 统计、External Memory（如果有）；不展示 Current Mode、Index/FTS5/Vector/Cache。
  - `toggle [key]` — 切换记忆开关；无参数时进入 `toggle` 页签列出可切换项，带 key 时直接切换（不打开页签控制台）。
  - `open` — 打开 `open` 页签，按模式自适应展示目录（agent 模式：Memory Dir / Project Dir / User Project Dir；code 模式：Coding Memory Dir / Project Dir / User Project Dir），Enter 直接打开系统文件管理器。
- 页签控制台交互：
  - `←` / `→` — 切换页签（循环）。
  - `↑` / `↓` — 在列表项间移动光标。
  - `Enter` — 执行当前页签操作（edit 编辑文件、toggle 切换开关、open 打开目录）。
  - `Ctrl+O` — 在 `edit` / `open` 页签切换显示完整路径与相对路径，切换页签时重置为默认（相对路径）。
  - `Esc` — 关闭页签控制台。
- `toggle` 页签展示格式（只显示 key，不显示英文 label，有 `✓ on` / `✗ off` 状态标记和中文描述，无 `·` 分隔符）。可切换项按当前 mode 过滤：
  - agent mode：`memory_enabled`（记忆功能总开关）、`memory_proactive`（对话中自动搜索和记录）、`memory_forbidden_enabled`（过滤敏感信息）；
  - code mode：`memory_enabled`（记忆功能总开关）、`auto_coding_memory`（每轮对话后自动提取记忆（需总开关开启））、`memory_forbidden_enabled`（过滤敏感信息）。
  - 切换后若需重启会话生效会给出提示。
- Tab 补全：`/memory edit ` 后显示文件列表（路径用 `getDisplayPath` 展示，去重）；`/memory toggle ` 后显示当前 mode 的 key 列表；均支持前缀过滤。
- `/memory edit <path>` 只允许打开已存在且通过记忆目录路径校验的文件，不负责创建缺失文件；运行时 auto/coding memory 文件为只读。
- 示例：`/memory`（打开控制台）、`/memory status`、`/memory toggle memory_enabled`、`/memory edit memory/MEMORY.md`。

#### `/sandbox`（沙箱模式管理）

- 平台限制：仅在 Linux 上的 agent-server 可用；Windows / macOS 的 agent-server 收到 `/sandbox` 命令会直接返回错误。TUI 本身跑在哪个平台不影响——只要 agent-server 在 Linux 上即可。
- 子命令：`status`（默认）/ `enable` / `disable` / `exclude add|remove|list` / `files allow|deny|remove|list` / `help`。
- `enable` 行为：必要时启动 jiuwenbox（已有 endpoint 则复用），随后触发 agent 重建；响应面板会显示 `rebuilt_modes` 与 jiuwenbox 端点。
- `disable` 行为：重建 agent；只有 jiuwenswarm 自己启动的 jiuwenbox 才会被停掉，外部 endpoint 会显式保留。
- 状态面板字段：
  - `enabled` — 当前开关。
  - `excluded_commands` — 命中后穿透到本地执行的 shell glob 列表。
  - `landlock` — jiuwenbox Landlock 支持情况（`supported` + `compatibility`）。
  - `files.allow_write` / `files.deny_write` — 生效（auto-managed ∪ user-configured，去重）的写入策略，显示 `(rw)` / `(ro)`。
- 自动配置路径：当前工作路径。`preserve_file_sharing_mode` 仅支持 `mount`。
- `excluded_commands` 的匹配：按 simple-command 叶子做 fnmatch；写 glob 时建议覆盖参数（例如 `"git *"`）。混合管道会本地/远端拆分执行，不安全结构则整条进沙箱。本质仍是沙箱穿透口，不要对 `rm -rf` / `curl` 这类高风险命令使用。
- add / remove 严格校验：`exclude add` 已存在 pattern、`exclude remove` 不存在 pattern 都会报错；`files allow|deny` 在同 bucket 已有 path 或对侧 bucket 已有 path（allow/deny 冲突）会报错，先 `files remove` 再 add；`files remove` 没匹配到也会报错。避免"看起来执行了实际什么也没改"。
- 写入策略：`allow` / `deny` 控制写访问（rw/ro），不是 Unix 八进制权限；支持「父 allow + 子 deny」，不支持「子 allow + 父 deny」。
- 示例：`/sandbox enable`、`/sandbox status`、`/sandbox files allow ./tmp/`、`/sandbox exclude add "git *"`。

#### `/review`（代码审查 PR）

- **别名**：无。
- **适用模式**：全部。
- TUI 通过 `ctx.sendMessage()` 将 `/review [args]` 作为聊天消息发送给 Gateway；离线时提示错误。
- Gateway 识别后注入 review prompt，由 Agent 使用 `gh` CLI 执行审查。
- 无参数时 Agent 执行 `gh pr list` 展示开放 PR 列表；有参数时执行 `gh pr view/diff` 并分析变更（正确性、约定、性能、测试覆盖、安全）。
- 无 git/gh 预检，由 Agent 自行处理缺失工具的情况。
- 示例：`/review`（列出 PR）、`/review 123`（审查 PR #123）。

#### `/security-review`（安全审查）

- **别名**：无。
- **适用模式**：全部。
- TUI 通过 `ctx.sendMessage()` 将 `/security-review [args]` 作为聊天消息发送给 Gateway；离线时提示错误。
- Gateway 识别后注入安全审查 prompt，由 Agent 使用 `git` 命令分析当前分支相对于 `origin/HEAD` 的待定变更。
- Agent 执行三步分析：仓库上下文研究（`git status`/`diff --name-only`/`log`）→ 比较分析（`git diff`）→ 漏洞评估（输入验证、认证授权、密码学、注入、数据暴露）。
- 仅报告置信度 > 80% 的发现，输出结构化 Markdown 报告（文件、行号、严重级别、类别、描述、利用场景、修复建议）。
- `[args]` 可选，用于附加焦点说明（如"重点关注认证模块"）。
- 示例：`/security-review`、`/security-review 重点关注认证模块的安全性`。

#### `/simplify`（代码精简审查）

- **别名**：无。
- **适用模式**：**仅 `code.*`**。非 code 模式下提示先执行 `/mode code`。
- TUI 本地解析，调用 `command.simplify` RPC（30 秒超时）获取服务端生成的三阶段审查 prompt，然后通过 `ctx.sendMessage(prompt, ..., { logAsUser: false })` 注入为 Agent 消息。
- 可选参数 `[target]`：附加关注点（文件路径、模块名或特定审查维度）。
- **执行流程**：校验 code 模式 → RPC 获取 prompt → 注入 Agent → Agent 执行三阶段审查并自动修复。
- **三阶段**：
  1. **识别变更**：`git diff` 获取变更列表。
  2. **并行审查**（三个维度）：代码复用（现有工具/重复功能）、代码质量（冗余状态/参数膨胀/复制粘贴/抽象泄漏/字符串硬编码/不必要注释）、效率（不必要工作/错失并发/热路径膨胀/无效更新/TOCTOU/内存泄漏）。
  3. **修复问题**：逐一修复，误报跳过，完成后总结。
- 离线时提示重试。
- 示例：`/simplify`（审查所有变更）、`/simplify src/auth/`（关注特定目录）、`/simplify focus on error handling`（关注错误处理）。

#### `/hooks`（浏览 Hooks 配置）

- 用法：`/hooks`（无参数、无子命令，只读）。
- 调用 `hooks.list` RPC 从 Gateway 获取 `config.yaml` 中 `hooks` 段的摘要。
- 展示：
  - **事件列表**：按 hook 数量降序排列，每行显示事件名、hook 数量、matcher 分布。
  - **状态面板**：配置来源（`config.yaml`）、全局开关（`enabled` / `DISABLED`）、Total Hooks、Active Events（有配置的事件数 / 18）。
  - **Hook 详情卡片**：每个 hook 按 Type、Command/Prompt、Timeout、Shell、Status 展示。
- 无配置时：显示 `No hooks configured.`，提示通过 `/config edit` 编辑配置。
- Hooks 概念：
  - **18 种触发事件**：Agent Rail 层（`PreToolUse`、`PostToolUse`、`PostToolUseFailure`、`Stop`、`PermissionRequest`、`PermissionDenied`、`SubagentStart`、`SubagentStop`、`BeforeModelCall`、`AfterModelCall`）、Gateway 层（`UserPromptSubmit`、`SessionStart`、`SessionEnd`、`Notification`、`ConfigChange`、`InstructionsLoaded`、`Setup`）和 Agent Server 层（`PreCompact`）。
  - **2 种 Hook 类型**：`command`（执行 shell 命令，退出码 0 = 成功、2 = 阻断）和 `prompt`（LLM 审查，响应 JSON `decision: "block"` 阻断）。
  - **阻断行为**：PreToolUse 阻断可跳过工具调用并将原因反馈给模型。
  - **输入修改**：PreToolUse hook 可通过 stdout JSON 的 `modifiedInput` 修改工具参数。
  - **附加上下文**：可通过 stdout JSON 的 `additionalContext` 注入信息到工具结果或模型上下文。
  - **全局开关**：`config.yaml` 中 `hooks.disable_all_hooks: true` 禁用所有 hooks。
- 详见 [Slash命令表.md](Slash命令表.md) 的 `/hooks` 小节。

#### `/clear` 与忙状态

- 若 `session is busy`（正在处理），`/clear` 会拒绝执行，需先中断任务（**`Ctrl+C`** 第一次中断；默认构建无 `/cancel` 命令）。
- `/clear` 会调用后端 `session.create` 创建全新会话，由 AgentServer 分配并返回新的 `session_id`，随后切换会话、清空 transcript 并恢复新会话的空历史；它不是只清理当前屏幕。
- `/new`、`/reset` 是 `/clear` 的别名，执行相同的新建会话流程。若 `session.create` 失败，TUI 保持在原会话，不会先行切换到一个本地生成的 ID。

---

## 工具参考（Tools）

本节描述 **Code 模式** 下与 Agent 模式差异最大的部分；Agent 模式通常为全量工具集，以服务端配置为准（见 [模式系统.md](模式系统.md)）。

### Code 模式配置中的动态工具（`modes.code.tools`）

默认示例（以你环境 `config.yaml` 为准）：

| 工具 | 说明 |
|------|------|
| `web_free_search` | 免费网页检索 |
| `web_fetch_webpage` | 抓取网页正文 |
| `web_paid_search` | 付费检索（若已配置） |
| `user_todos` | 用户待办相关能力 |

文档约定：`coding_memory_*` 与 `send_file_to_user` 由运行时自动注册，**不必**写在 `modes.code.tools` 列表中（见 [模式系统.md](模式系统.md)）。

### Code 模式专属：编码记忆工具

| 工具 | 参数要点 | 说明 |
|------|-----------|------|
| `coding_memory_read` | `query` | 按关键词检索编码上下文 |
| `coding_memory_write` | `content`, `section` | 写入片段或经验，`section` 如 `architecture`、`bugfix` |
| `coding_memory_edit` | `memory_id`, `content`, `section?` | 更新已有条目 |

存储与更多说明见 [编码记忆.md](编码记忆.md)。

### Code 模式安全 Rails（`modes.code.rails`）

| Rail | 作用 |
|------|------|
| `FileSystemRail` | 文件系统访问约束 |
| `SkillUseRail` | 技能调用约束 |
| `LspRail` | LSP 相关约束 |

### 调整工具权限

- 在 TUI 中使用 **`/permissions`** 将某一工具设为 `allow` / `ask` / `deny`，写入 `permissions.tools`。
- 全局安全模型见 [工具权限与安全防护.md](工具权限与安全防护.md)。

---

## 交互模式（Interactive Mode）

### 快捷键

#### 默认全局快捷键

| 按键 | 行为 |
|------|------|
| `Ctrl+C` | **第一次**：中断当前任务（`chat.interrupt`）；**3 秒内第二次**：退出 TUI（**不可重绑**） |
| `Ctrl+D` | 中断任务；连按两次退出（**不可重绑**） |
| `Ctrl+L` | 重绘整屏 |
| `Ctrl+T` | 显示/隐藏 Todos 面板 |
| `Ctrl+G` | 显示/隐藏 Team 面板 |
| `Ctrl+O` | 在 transcript **紧凑 / 详细** 视图间切换 |
| `Esc` | 无 overlay 且存在可取消工作时，发送取消 |

权限类问答中，`y` / `n` 可快速选择允许/拒绝类选项（`Confirmation` context）。

底部 shortcut 提示栏仍显示**默认键名**；若已自定义，请以 `/keybindings list` 或实际按键行为为准。

#### 自定义快捷键（`/keybindings`）

配置文件路径：`~/.jiuwenswarm-tui/keybindings.json`（首次 `/keybindings` 会按当前默认值生成模板）。

| 子命令 | 作用 |
|--------|------|
| `/keybindings` 或 `/keybindings edit` | 创建或打开配置文件；关闭外部编辑器后自动重新加载 |
| `/keybindings list` | 列出当前**生效**的快捷键（按 context 分组） |
| `/keybindings reset` | 删除用户配置文件，恢复内置默认 |

**合并规则**：以内置 `DEFAULT_BINDINGS` 为底，用户 JSON 按 context 覆盖；某键设为 `null` 表示取消该默认绑定。非法键名、未知 action、保留键等会给出 warning，TUI 仍用合法部分继续运行。

**示例**（将重绘改为 `F5`，并取消 Esc 取消任务）：

```json
{
  "bindings": [
    {
      "context": "Global",
      "bindings": {
        "f5": "app:redraw",
        "escape": null
      }
    }
  ]
}
```

键名格式需符合 pi-tui 的 `matchesKey` 约定：小写修饰键 `ctrl` / `shift` / `alt`，特殊键如 `pageUp`、`escape`、`return` 等；不支持 chord（空格分隔的多段按键）。

**不支持的按键**：
- **`win` / `cmd` / `super` / `meta`**：这些修饰键依赖 Kitty 键盘协议，普通终端（Windows CMD、VS Code 内置终端等）无法发送，快捷键不会生效。
- **`ctrl+shift+字母`**：在 Windows CMD 等传统终端中，`ctrl+shift+l` 与 `ctrl+l` 发送的字节码相同，终端层面无法区分，不建议使用。如需使用此类组合建议换用 Windows Terminal、WezTerm 等支持 VT 模式的终端。
- **chord 组合键**（空格分隔，如 `"ctrl+x ctrl+k"`）：当前版本不支持。

#### 可配置的 Context 与 Action

| Context | 生效场景 | 主要默认键 |
|---------|----------|------------|
| `Global` | 主界面（无 overlay 时部分 Esc 行为） | `ctrl+l/t/g/o`，`escape` → 取消任务 |
| `Scroll` | Transcript 滚动 | `pageUp` / `pageDown`，`ctrl+home/end` |
| `FileViewer` | 全屏文件/日志查看 | `esc`/`q` 退出，`↑↓`/`jk` 行移，`g`/`shift+g` 顶/底 |
| `Confirmation` | 权限/确认问答 | `y` / `n` |
| `TeamPanel` | Team 面板打开 | `←` 返回列表，`↑↓` 选成员，`Enter` 查看 |
| `SwarmWorkflows` | Swarm 工作流视图 | `esc` 返回，`tab`/`→` 切焦点，`l/p/o/e/r` 等 |
| `StatusView` | 状态/配置视图 | `esc` 关闭，`←`/`→` 切标签（搜索框输入仍硬编码） |
| `ResumeList` | 会话选择器列表 | `space` 预览，`ctrl+r` 重命名，`ctrl+a/b`，`esc` 关闭 |
| `Overlay` | MCP 详情/工具子视图 | `esc` 关闭 |

完整 action 名称与说明见 `/keybindings list` 或生成的 `keybindings.json` 模板。

#### 刻意不可重绑或仍硬编码的部分

- **保留键**：`ctrl+c`、`ctrl+d`、`ctrl+m`（终端语义 / 连按退出，见 `reserved.ts`）。
- **Select 列表内部**：`/model`、`/theme`、`/mcp` 等 pi-tui `SelectList` 的上下移动、回车选中、typeahead 过滤。
- **Config 编辑器**：搜索框与值输入阶段的文本键、`/status` 配置 Tab 的 `/` 进入搜索等。
- **Resume 子态**：只读预览态、重命名输入态内的按键逻辑。
- **Diff 查看器**、**Startup 提示**等尚未接入 resolver 的视图。

如需扩展可配置范围，需在 `actions.ts` / `defaultBindings.ts` 声明 action，并在 `app-screen.ts` 对应输入路径改为 `resolveAction`。

### 输入、附件与 `@` 引用

- **`@` 路径自动补全**：在输入框中键入 `@` 后，TUI 会自动弹出文件路径补全下拉框，支持：
  - 相对路径补全（基于当前工作目录 `cwd`）；
  - 绝对路径补全（`/` 开头）；
  - home 目录简写补全（`~` / `~/` 开头）；
  - 含空格路径补全：使用 `@"路径"` 语法。
  - 选择补全项后自动补全路径并追加一个空格。
  - 补全来源为文件系统实时扫描，目录优先排序。
- **文件附件**：在输入中使用 `@路径` 或 `@"含空格路径"` 后，`@` 引用的文件会作为消息附件发送给 Agent；解析规则见 `attachments.ts`。
- **常见作为附件的文件扩展名**：含源码、文档、配置、压缩包、Office 文档等一大类扩展名（见 `SUPPORTED_FILE_EXTENSIONS`）；未在列表中的扩展名可能不会生成附件。
- **粘贴路径**：支持拖拽文件到终端（纯文件路径粘贴）、`file://` 协议路径、Windows 盘符路径等，由 `extractFilePathsFromPaste` 解析并自动转换为 `@路径` 引用。

### 命令与路径补全

- 输入 **`/`** 后可通过补全选择已注册命令（`CommandService.getSuggestions`）。
- 部分命令注册了 `completion`（如 `/mode`、`/theme`、`/color`、`/config`）。

### 启动时「是否信任当前文件夹」

- 首次启动可能询问是否信任当前目录；选择信任会将该目录加入可信列表（与 `/workspace` 逻辑一致，见 `app-screen.ts`）。

---

## 模式速览

| 模式 | 代号 | 摘要 |
|------|------|------|
| Agent（规划） | `agent.plan` | 全工具 + 主动记忆，偏规划 |
| Agent（快速） | `agent.fast` | 全工具 + 被动记忆，偏快 |
| Code（常规） | `code.normal` | 编码 + 编码记忆，偏执行 |
| Team | `team` | 多 Agent 协作 |

切换示例与 `config.yaml` 中 `modes` 段说明见 [模式系统.md](模式系统.md)。

---

## Code 模式专题（重点）

### 为什么需要 Code 模式

- **更贴合仓库工作流**：在工具白名单 + 安全 Rails 下读写项目文件，并用 **编码记忆** 持久化代码上下文。
- **与 Agent 模式的区别**：Code 模式 **不是**「全工具 + 无额外 Rails」；默认启用 `FileSystemRail`、`SkillUseRail`、`LspRail`，工具集为配置项 + 自动注册的编码记忆等（见上文工具参考）。

### 推荐工作流

1. **进入 Code 模式**  
   ` /mode code` → 默认 `code.normal`。
2. **划定可信目录**  
   ` /workspace add .` 或 ` /workspace set /path/to/repo`，确保 Agent 文件工具允许访问你的工程树。
3. **（可选）项目级说明文件**  
   在 `code.*` 下执行 ` /init`，按提示选择团队/个人/两者，生成 `JIUWENSWARM.md` 等。
4. **日常编码对话**  
   直接描述需求；模型会通过白名单工具与编码记忆完成任务。
5. **查看本轮改动**  
   ` /diff` 查看会话内按轮次文件变更轨迹。
6. **上下文过长**  
   ` /compact` 压缩历史，保留摘要。
7. **收紧危险工具**  
   例如 ` /permissions ask write_file`，在写入前要求确认。

### 编码记忆使用提示

- 长任务中可让助手「把本次接口约定写入编码记忆，`section=architecture`」等，便于后续 `coding_memory_read` 检索。
- 与对话记忆、经验记忆的关系见 [编码记忆.md](编码记忆.md) 中的对照表。

### 与 `/skills`、`/mcp` 的配合

- Code 模式下仍可通过 **`/skills`**、**`/mcp`** 管理扩展能力；具体工具是否出现在当前会话取决于服务端模式配置与权限。
- MCP 详细配置见 [MCP配置.md](MCP配置.md)。

### 常见问题

| 现象 | 处理 |
|------|------|
| `/init` 提示需要 Code 模式 | 先执行 `/mode code` |
| 无法写文件或总被要求确认 | 确认处于 `code.normal` |
| 文件路径不在允许范围 | 使用 `/workspace add` 将仓库根或子目录加入可信列表 |
| 连接失败 | 检查 Gateway 是否监听、`--url` 是否正确、防火墙 |
| 未安装 `rg` | 安装 ripgrep 以改善搜索体验（欢迎屏提示） |
| Cron 通知出现在所有 TUI 窗口 | 预期行为：`targets=tui` 的定时任务会广播到所有已连接终端，详见 [定时任务](定时任务.md#5-推送到-tui-频道) |
| 多窗口之间聊天/流式输出串台 | 请确认 Gateway 为 multi-tui 版本；各窗口应使用不同 `session_id`，事件按 session 精确路由 |

---

## 故障排查

| 问题 | 建议 |
|------|------|
| `jiuwenswarm-cli requires an interactive TTY` | 在真实终端中运行，勿用管道代替 |
| `Authentication failed` | 检查 `--token` |
| `Backend unavailable` | 启动 `jiuwenswarm-gateway` 或修正 `--url` |
| `Unknown command: /xxx` | 该构建未注册该命令；用 `/help` 查看当前可用列表，或对照本文「命令总表」与 `registry.ts` |
| `/copy` 失败 | 当前系统无剪贴板集成；Linux 需常见剪贴板工具 |

---

## 另请参阅

- [快速开始(TUI)](Quickstart_tui.md)
- [模式系统](模式系统.md)
- [Slash命令表](Slash命令表.md)
- [编码记忆](编码记忆.md)
- [工具权限与安全防护](工具权限与安全防护.md)
- [MCP配置](MCP配置.md)
- [配置信息](配置信息.md)
- [Claude Code CLI 参考（结构参考）](https://code.claude.com/docs/zh-CN/cli-reference)
---

## 返回导航

- [返回文档首页](../README.md)
- [返回项目首页](../../README_CN.md)