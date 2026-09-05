# JiuwenSwarm VS Code 扩展 —— 使用指南

每个设置、面板元素和工作流的完整参考。安装说明见 [VSCode插件.md](VSCode插件.md)。

---

## 配置

打开 **设置 → 扩展 → JiuwenSwarm**（或在设置编辑器中搜索 `jiuwenswarm`）：

| 设置项 | 默认值 | 说明 |
|---|---|---|
| `jiuwenswarm.host` | `127.0.0.1` | JiuwenSwarm WebSocket 服务器的主机名或 IP |
| `jiuwenswarm.port` | `19000` | 端口——连接 `ws://host:port/ws` |
| `jiuwenswarm.channelId` | `ide` | 服务器日志和追踪中显示的客户端标识 |
| `jiuwenswarm.autoConnect` | `true` | VS Code 启动时打开 WebSocket |
| **`jiuwenswarm.defaultMode`** | `code.plan` | 模式选择器声明的默认模式（`code.plan` / `code.normal` / `code.team`）。聊天面板始终从**计划与执行**开始；通过模式胶囊按会话切换。 |
| `jiuwenswarm.approveEdits` | `false` | 应用任何智能体文件编辑前要求明确批准 |
| `jiuwenswarm.runCommandsInTerminal` | `true` | 在 JiuwenSwarm 终端中运行 `bash` / `run_command` 工具调用，以便查看实时输出 |
| `jiuwenswarm.useDiffViewer` | `false` | 应用每个文件编辑前在 VS Code 内置 diff 查看器中展示，并询问**接受 / 拒绝** |
| `jiuwenswarm.loadHistoryOnSwitch` | `true` | 切换到已有会话时获取并显示历史消息 |
| `jiuwenswarm.keepAlive.enabled` | `true` | 发送周期 WebSocket ping 帧以保持连接并尽早检测断开 |
| `jiuwenswarm.keepAlive.interval` | `30` | 保活 ping 的间隔秒数（5–300） |
| `jiuwenswarm.rewindEnabled` | `true` | 在智能体编辑前快照文件；每轮后显示回退栏 |
| `jiuwenswarm.projectTree.enabled` | `true` | 在每条消息前附加工作区根目录的 2 层目录列表 |
| `jiuwenswarm.projectTree.maxFiles` | `200` | 项目树列表的最大条目数（10–2000） |
| `jiuwenswarm.gitEnabled` | `false` | 在消息列表下方显示**提交** / **推送**按钮（需要 git 仓库） |

连接设置（host、port、channel ID、auto-connect、keep-alive）在扩展激活时读取；更改它们会提示
你重新加载窗口。行为开关（批准、diff 查看器、终端、回退、项目树、git）在使用时实时生效。

---

## 打开面板

从命令面板打开 **JiuwenSwarm: 打开聊天**，或按 **Ctrl+Shift+J** / **⌘⇧J**。面板作为 webview
面板在当前编辑器旁打开，可拖到任意编辑器组中。

首次连接时自动创建一个会话。页头显示会话标题和实时连接状态。

---

## 页头栏

```
● 会话标题                    [新建] [⚙]
```

| 元素 | 说明 |
|---|---|
| 状态点 | 首次连接前为灰色，之后绿色 = 已连接，黄色（脉动）= 重连中，红色 = 已断开。断开后如需重连，点击状态栏项（`$(circle-slash) JiuwenSwarm`）或使用**新建**。 |
| 会话标题 | 活动会话的名称 |
| **新建**按钮 | 开始新会话：重连 WebSocket 并清空消息列表 |
| **⚙** 菜单 | 会话、技能、主题（自动/深色/浅色）、调试日志 |

---

## 模式选择器

底部输入栏中的模式胶囊控制智能体的工作方式：

| 模式 | 键 | 说明 |
|---|---|---|
| **计划与执行** | `code.plan` | 智能体读取文件并设计计划，在进行任何编辑前等待你批准。适合非平凡或有风险的变化。 |
| **执行** | `code.normal` | 智能体不经计划阶段直接编辑文件并运行命令。适合清晰、范围受限的任务。 |
| **团队编码** | `code.team` | 主智能体将任务拆分为并行子任务，并同时分配给专家智能体。适合大型可分解的工作。 |

点击模式胶囊打开下拉框。如果当前会话已有消息，切换模式会要求确认并开始新会话。模式胶囊
从**计划与执行**开始。

---

## 聊天输入

```
[+]  [模式 ▾]  @ 文件 · # 技能 · ! 提示 — Enter 发送 · Shift+Enter 换行     [↑]
```

| 元素 | 说明 |
|---|---|
| **+** | 打开文件选择器以附加图片（PNG、JPEG、WebP、GIF；每张最大 10 MB）。输入框上方显示预览；点击 **✕** 移除。图片以 base64 编码随消息发送。 |
| 模式胶囊 | 快速模式切换器 |
| 文本框 | 随输入自动增高。**Enter** 发送；**Shift+Enter** 插入换行。 |
| 发送 / 停止按钮 | 空闲时提交消息。智能体流式输出时变为停止按钮——点击可中断。 |

### 内联选择器

三个字符会触发出现在输入框上方的自动补全下拉框：

**`@` — 文件提及**

输入 `@` 后跟部分文件名可搜索工作区文件。选择文件会在消息中插入 `@relative/path/to/file`。
发送时，扩展读取该文件并在带围栏代码块的上下文中包含其完整内容。

**`#` — 技能选择器**

输入 `#` 可看到所有已注册技能。继续输入可按名称过滤。选择技能会在消息中插入 `#skill-name`。

**`!` — 预设提示**

输入 `!` 可看到八个内置提示模板：

| 标签 | 模板 |
|---|---|
| 解释 | 解释这段代码做什么以及如何工作。 |
| 修复 Bug | 找出并修复这段代码中的 bug。说明是什么导致的。 |
| 写测试 | 为这段代码编写单元测试。覆盖边界情况。 |
| 重构 | 将这段代码重构得更简洁、更易维护。 |
| 优化 | 为性能优化这段代码。解释改动。 |
| 写文档 | 为这段代码添加清晰的文档和注释。 |
| 审查 | 审查这段代码中的 bug、安全问题和改进点。 |
| 实现 | 实现以下功能： |

继续输入可过滤列表。选择模板会用完整提示文本替换 `!query`。

对三个选择器：**方向键**导航，**Enter** 或 **Tab** 选择，**Escape** 关闭。

---

## 消息列表

每个完成的轮次包含：

- **你的消息** —— 右对齐的气泡。
- **推理块** —— 当模型使用扩展推理时，回复前会出现可折叠的**推理…** 区域。点击箭头展开或收起。
- **思考指示** —— 模型预填回复时（第一个 token 到达前），会出现**思考…** 胶囊。当推理或文本
  开始流式输出时翻转为**生成…**，模型调用结束时清除。
- **智能体回复** —— 文本随生成流式输出。重载会话历史时，助手消息以粗体/斜体、带围栏代码块和
  可点击文件链接渲染。
- **工具调用卡片** —— 智能体调用的每个工具都以内联卡片显示：
  - 工具图标和友好名称（齿轮图标加 `编辑`、`Bash`、`WebSearch`、`TodoWrite` 等标签；原始工具 id
    如 `str_replace_editor` 显示在卡片的工具提示中）
  - 实时旋转器 → 完成时对勾或 ✕
  - 可折叠的**输入**区域（发送给工具的参数）
  - 可折叠的**输出**区域（工具返回的结果）

---

## 统计栏与指标

页头与消息列表之间的统计栏显示会话级指标，每轮后更新（第一轮完成后出现）：

- **轮数** —— 会话中已完成的轮次总数
- **错误** —— 以错误结束的轮次数
- **Token** —— 累计 token 数（输入 + 输出）
- **LLM 调用** —— 累计模型调用次数
- **平均延迟** —— 各轮平均响应时间
- **TTFT** —— 平均首 token 时间
- **成本** —— 估算的美元成本（服务器报告定价时显示）
- **TODO** —— 智能体实时待办进度（✓ 已完成 / ◐ 进行中 / ☐ 待办），当智能体报告时显示

统计栏右侧的条形图图标（两轮或更多轮后出现）切换**迷你图表**——每轮 token 和耗时的条形图。
悬停条形可查看该轮详情。

**服务器内存**片显示 JiuwenSwarm 服务器的实时内存占用（RSS 总量与可用量），每 10 秒轮询。

底部输入区中的**上下文栏**显示活动模型上下文窗口的占用程度（0–100%）。超过 60% 变橙，
超过 80% 变红；上下文接近服务器的自动压缩阈值时会出现告警片。

---

## IDE 上下文注入

每条消息都前置一个结构化上下文块。智能体会把它视为你消息的一部分。

### 注入内容

| 字段 | 来源 |
|---|---|
| 活动文件路径与语言 | `vscode.window.activeTextEditor` + `document.languageId` |
| 光标行 | `editor.selection.active.line` |
| 选中的代码 | `editor.document.getText(editor.selection)`（若非空） |
| 诊断（最多 10 条） | `vscode.languages.getDiagnostics(doc.uri)` |
| 其他打开的标签页（最多 10 个） | `vscode.window.tabGroups.all` |
| 项目树（2 层） | 工作区文件夹遍历；跳过 `.git`、`build`、`node_modules`、`dist`、`target` 等 |
| Git 分支 + 变更数 | `git rev-parse` + `git status --porcelain` 子进程 |
| 项目规则 | 找到的第一个非空文件：`.jiuwenswarm/instructions.md`、`.jiuwenswarm/rules.md`、`AGENTS.md` |
| @提及的文件 | 消息中每个 `@path` 的完整文件内容 |

### 项目规则

在工作区根目录创建一个文件，向每条消息注入固定指令：

```
.jiuwenswarm/instructions.md   ← 首先检查
.jiuwenswarm/rules.md          ← 其次检查
AGENTS.md                      ← 第三检查
```

用它来定义编码风格、禁止模式、偏好的库，或任何智能体应始终知道的项目专属上下文。

### 控制注入内容

| 设置项 | 效果 |
|---|---|
| `jiuwenswarm.projectTree.enabled` | 打开或关闭目录列表 |
| `jiuwenswarm.projectTree.maxFiles` | 限制大型单仓库的条目数（10–2000） |

### 上下文块示例

```
<!-- IDE Context -->
Active file: /Users/mishka/project/src/api/handler.py  (python)
Cursor line: 87

Selected code:
```
def handle_request(req):
    result = blocking_call(req)
    return result
```

Diagnostics (2):
  • Line 87: Variable 'result' is not used before return
  • Line 88: blocking_call is deprecated

Other open files (2):
  /Users/mishka/project/src/api/router.py
  /Users/mishka/project/tests/test_handler.py

Project structure:
  src/
    api/
    models/
  tests/
  pyproject.toml

Git: branch=feature/async-refactor, 3 uncommitted changes

Project rules:
Always use async/await. No blocking calls. Follow PEP 8.
<!-- End IDE Context -->
```

---

## 可点击文件链接

智能体回复中的文件路径会变成可点击链接，在引用的行打开文件。

| 模式 | 示例 | 效果 |
|---|---|---|
| 带目录的反引号路径 | `` `src/api/handler.py` `` | 在第 1 行打开文件 |
| 带行号的反引号路径 | `` `src/api/handler.py:42` `` | 在第 42 行打开文件 |
| 裸 `path/to/file.ext:N` | `src/auth/router.py:87` | 在第 87 行打开文件 |

反引号中的纯标识符（无 `/` 且无 `:N`）不会作为文件链接化。围栏代码块内的路径原样渲染。

---

## 操作与键盘快捷键

| 操作 | Windows / Linux | Mac |
|---|---|---|
| 打开 / 聚焦聊天面板 | `Ctrl+Shift+J` | `⌘⇧J` |
| 发送选中内容 | `Ctrl+Shift+E` | `⌘⇧E` |
| 新会话（命令面板） | — | — |
| 用 JiuwenSwarm 修复（灯泡） | `Ctrl+.` | `⌘.` |

**打开 / 聚焦** —— 打开 JiuwenSwarm 面板。若已打开，则将其带到最前。

**发送选中内容**（`Ctrl+Shift+E` / `⌘⇧E` 或右键 → **将选中内容发送到 JiuwenSwarm**）——
打开面板并预填选中的代码：

```
[File: handler.py]
```
def handle_request(req):
    ...
```

补充你的问题并按 Enter。

**新会话**（命令面板：`JiuwenSwarm: 新会话`）——重连 WebSocket 以开始新会话。

---

## 代码操作快速修复

VS Code 会在有错误或警告的行旁显示灯泡 💡。JiuwenSwarm 注册了一个**用 JiuwenSwarm 修复**
代码操作：

1. 将光标放到有错误（红色波浪线）的行上。
2. 点击灯泡或按 `Ctrl+.` / `⌘.`。
3. 选择**用 JiuwenSwarm 修复**。
4. 聊天面板打开，预填错误消息和周围 ±7 行代码：

```
Fix this error in handler.py:

Error:
Variable 'result' is not used before return

```python
def handle_request(req):
    result = blocking_call(req)
    return result
```
```

5. 按 Enter 发送。

适用于 VS Code 有任何诊断的任何语言——TypeScript、Python、Java、Go、Rust、C# 等。

---

## 文件编辑工作流

当智能体调用 `str_replace_editor`、`write_file` 或 `create_file` 时，扩展会将编辑应用到
工作区，并弹出通知 toast 确认每次应用的变更。

### 带批准

在设置中启用 `jiuwenswarm.approveEdits`，可在每次文件变更前看到**批准 / 拒绝**提示。
点击**拒绝**丢弃编辑；点击**批准**写入磁盘。

### 带 diff 查看器

启用 `jiuwenswarm.useDiffViewer`，可在应用前于 VS Code 内置 diff 查看器中审查每个拟议编辑，
然后选择**接受**或**拒绝**。

---

## 终端集成

智能体 shell 命令（`bash`、`run_command`）在由 `vscode.window.createTerminal()` 创建的
**JiuwenSwarm** 终端中运行。终端在第一条命令时创建，后续命令复用；扩展停用时销毁。
关闭 `jiuwenswarm.runCommandsInTerminal` 可跳过本地运行命令（智能体仍在服务器上运行）。

---

## 检查点 / 回退

任何编辑过文件的智能体轮次结束后，回退栏出现在消息列表下方：

```
⟲ 智能体本轮回改了文件    [⟲ 撤销更改]
```

### 工作原理

在智能体某一轮首次编辑某文件之前，扩展会快照该文件的当前内容（通过 VS Code 文件系统 API
读取）。轮次结束时（`chat.final`）快照被锁定。

### 使用回退

点击 **⟲ 撤销更改**。扩展会恢复每个被快照的文件。该轮之前不存在的文件会被删除。

状态行确认结果：

```
⟲ 已回退 3 个文件
```

### 限制

| 场景 | 行为 |
|---|---|
| 智能体创建了文件 | 回退时文件被删除 |
| 智能体编辑了文件 | 文件恢复到该轮前状态 |
| 你发送另一条消息 | 栏消失；快照被丢弃 |
| 新会话 | 栏清空 |

通过设置中的 `jiuwenswarm.rewindEnabled` 关闭。

---

## Git 快捷操作

在设置中启用 `jiuwenswarm.gitEnabled`，可在消息列表下方显示包含两个按钮的工具栏：

**提交** —— 打开输入框，预填你最后发送的消息作为提交消息（前缀 "AI: "）。确认后运行
`git add -u && git commit -m <message>`。提交后 git 栏更新。

**推送** —— 在后台运行 `git push`。完成时状态更新。

git 栏显示当前分支和未提交文件数。每轮智能体结束后更新，且只在 git 仓库内出现。

---

## 会话

### 打开浮层

点击页头中的 **⚙ → 会话**。

### 列表显示什么

每行显示会话标题、最后消息时间（相对）和消息数。

### 切换

点击一行进行切换。开启 `jiuwenswarm.loadHistoryOnSwitch` 时，历史消息会自动流式加载。

### 创建

点击页头中的**新建**，或从命令面板运行 `JiuwenSwarm: 新会话`。

### 删除

点击非活动会话行上的 **✕**。点击一次（变红），然后在 2 秒内再点一次确认。活动会话无法删除——
先开始一个新会话。

### 刷新

点击浮层页头中的 **↺**。最多显示 20 个会话。

---

## 技能

### 打开浮层

点击页头中的 **⚙ → 技能**。

### 列表显示什么

每个技能行显示名称、说明、触发词和开/关开关。点击开或关以启用或禁用。变更通过
`skills.toggle` 发送到服务器。

### 从输入中选取技能

在文本框中输入 `#`。弹窗列出所有已加载技能。继续输入过滤。用 Enter 或 Tab 选择。

---

## 连接状态栏

状态栏（右下角）显示实时 WebSocket 状态：

| 图标 | 含义 |
|---|---|
| `$(check) JiuwenSwarm` | 已连接 |
| `$(loading~spin) JiuwenSwarm` | 连接中 |
| `$(sync~spin) JiuwenSwarm`（黄色） | 重连中——指数退避：1 秒 → 最大 30 秒 |
| `$(circle-slash) JiuwenSwarm`（红色） | 已断开——点击重新连接 |

标签旁显示 token 总量：`$(check) JiuwenSwarm · 42.3k`。

---

## 主题

| 选项 | 说明 |
|---|---|
| **⚙ → ◐ 自动** | 跟随 VS Code 的浅色或深色主题（默认） |
| **⚙ → 🌙 深色** | 无论 VS Code 主题如何强制深色 |
| **⚙ → ☀ 浅色** | 无论 VS Code 主题如何强制浅色 |

存储在 webview 本地存储中；面板重启后保留。

---

## 模型选择器

当连接到配置了多个模型的服务器时，输入栏会出现模型下拉框。点击打开并切换模型。活动模型
显示在迷你模型片中。

---

## 调试日志

点击 **⚙ → 调试日志** 打开消息列表下方的可滚动日志面板。它记录：

- 接收到的每个 WebSocket 帧（带时间戳的原始 JSON）
- 发送的每条消息（内容、上下文大小、媒体条目数）
- 会话切换、重连、连接状态转换
- 操作分发（list_sessions、list_skills、toggle_skill 等）
- 文件编辑工具调用（工具名和参数）

面板保留最近的 500 行。关闭以隐藏面板；使用**清除**清空日志（其内容在切换间保留）。

---

## 故障排查

| 症状 | 可能原因 | 解决方法 |
|---|---|---|
| 面板空白 | `chat.html` 缺失或 CSP 问题 | 从最新 VSIX 重新安装 |
| 状态栏显示 `$(circle-slash)` | 服务器未运行或主机/端口错误 | 启动 JiuwenSwarm；核对设置；点击部件重连 |
| 消息发出但无回复 | 握手后服务器不可达 | 启用调试日志；检查错误帧；从命令面板打开 Webview 开发者工具 |
| 发送选中内容无反应 | 未选中文本 | 确保按快捷键前在编辑器中选中了文本 |
| 文件链接打不开 | 文件路径不在工作区 | 检查被引用的文件是否存在 |
| 缺少回退栏 | `jiuwenswarm.rewindEnabled` 为 false，或编辑被拒绝 | 在设置中启用回退 |
| 回退恢复 0 个文件 | 快照被后续消息清除 | 在轮次结束后立即点击撤销 |
| 「正在加载历史…」不消失 | 服务器未发送 `history.done` | 通过状态栏重连；检查服务器日志 |
| 会话列表停留在「加载中…」 | 服务器超时或 `session.list` 不受支持 | 点击 ↺ 重试；检查服务器日志 |
| 技能列表显示错误 | 服务器不支持 `skills.list` | 较旧服务器版本属正常；升级服务器 |

### 阅读扩展日志

1. 打开 **视图 → 输出**（`Ctrl+Shift+U` / `⌘⇧U`）。
2. 从下拉框选择 **JiuwenSwarm**。

对于 webview JavaScript 错误：

1. 从命令面板运行 **开发人员: 打开 Webview 开发者工具**。
2. 检查**控制台**标签页。

---

## Swarm Map

Swarm Map 是一个专用 webview 面板，提供活动 `code.team` 会话的实时可视化概览——显示每个
工作智能体、它们的当前任务、实时文件活动、智能体间消息和整体进度。第一个团队智能体生成时
会自动在聊天面板旁打开。

### 三种视图 —— Map（默认）、List 和 Board

使用面板页头中的 **Map / List / Board** 开关切换：

- **Map 视图** —— 交互式「智能体地图」。每个工作智能体是画布上沿流水线弧线排列的彩色节点。
  工作时节点**脉冲发光**，完成时显示 **✓**，暂停时显示 **⏸**，当工作沿流水线推进时，动画点
  沿曲线从一个智能体流向下一个。**拖拽平移、滚轮缩放、双击自动适配**，**点击智能体**打开详情
  卡片（名称、角色、状态、实时耗时、当前动作、最近步骤）。
- **List 视图** —— 技术型逐智能体信息流：每个工作线程一张卡片，带状态片、实时耗时计时器、
  当前动作和可滚动活动流。
- **Board 视图** —— 三列看板（待办 / 进行中 / 完成）。每个任务一张卡片，显示标题、分配智能体
  名称（带颜色点）和状态徽标（阻塞、取消、完成）。智能体认领和完成任务时卡片实时更新。

### 友好状态措辞

地图和列表用通俗语言而非工具名描述智能体：**规划中**、**写作中**、**编辑中**、**探索中**、
**构建中**、**协调中**、**思考中**、**生成中**、**待命**、**完成**——始终伴随实时耗时计时器
（`0:42`）。

### 布局（List 视图）

```
┌────────────────────────────────────────────────────────────┐
│ JIUWENSWARM · SWARM MAP  [Map|List]  2/4 tasks · 3 agents · 1 working │
├────────────────────────────────────────────────────────────┤
│ [⚙ Write module → coder] [✓ Plan → planner]                │
├────────────────────────────────────────────────────────────┤
│ ● planner  TEAMMATE  WORKING  0:42                         │
│   editing · plan.md                                        │
│   Task: Decompose the work                                 │
│   12:01:44  reading · plan.md                              │
│   12:01:53  editing · plan.md                              │
│ ● coder    TEAMMATE  IDLE                                  │
│   standing by                                              │
├────────────────────────────────────────────────────────────┤
│ ▶ Messages (5)                                             │
└────────────────────────────────────────────────────────────┘
```

### 进度片与进度条

页头片（`N/M tasks · K agents · M working`）显示已完成多少任务、有多少工作线程、有多少正在
积极工作。页头下方一条细进度条显示完成任务百分比。

### 任务胶囊（List 视图）

List 视图顶部的一行胶囊概览每个任务：

| 外观 | 状态 |
|---|---|
| 绿色边框 | in_progress（进行中） |
| 黄色边框 | pending（待处理） |
| 红色边框 | blocked（阻塞，等待前置） |
| 灰色、暗淡 | completed / cancelled（已完成或已取消） |

### Board 视图（看板）

切换到 **Board** 以正规看板形式查看任务。三列铺满面板：

```
┌──────────────────┬──────────────────┬──────────────────┐
│    Backlog  (2)  │ In Progress (3)  │    Done     (1)  │
├──────────────────┼──────────────────┼──────────────────┤
│                  │                  │                  │
│  Add auth API    │ ● coder          │ ✓ Plan tasks     │
│                  │  Write module    │                  │
│  Write README    │ ● tester         │                  │
│                  │  ⚑ Add tests     │                  │
│                  │    Blocked       │                  │
└──────────────────┴──────────────────┴──────────────────┘
```

每张卡片显示：

| 元素 | 说明 |
|---|---|
| 任务标题 | 完整任务描述（过长时换行） |
| 颜色点 | 匹配所分配智能体的泳道颜色；未分配时无 |
| 智能体名称 | 任务分配到的智能体 |
| **阻塞**徽标 | 红色——任务在等待另一个任务或资源 |
| **完成**徽标 | 绿色——任务已完成 |
| **取消**徽标 | 暗淡——任务被放弃 |
| 删除线标题 | 应用于已完成和已取消的任务 |

看板在每次快照推送时更新——无需手动刷新。

### 工作线程泳道（List 视图）

编排者（`team-leader`）是会话所有者，而非工作线程，因此不显示。泳道是实际的工作线程：

| 元素 | 说明 |
|---|---|
| 脉动绿点 | 智能体正在 WORKING（积极运行工具） |
| 灰点 | 智能体 IDLE（待命） |
| 琥珀点 | 智能体 PAUSED（暂停） |
| 淡出卡片 | 智能体 DONE（已关闭） |
| 状态片 | 名称旁的 WORKING / IDLE / PAUSED / DONE 字样 |
| 耗时计时器 | 自智能体变为活动以来的实时 `0:42` |
| 活动行 | 当前操作：`writing · tasks.py`、`running · npm run build`、… |
| 活动流 | 最近约 8 个不同步骤，每个为 `HH:MM:SS 文本`（连续重复被折叠） |
| ⚠ 空闲 Ns | 智能体 WORKING 但静默超过 30 秒。模型调用期间泳道改为显示**思考…** / **生成…**，因此此告警仅在真正停滞时出现 |

### 泳道点击 → 跳转到文件

当智能体最近接触过某个文件时，悬停其卡片会显示 **↗ 打开文件** 提示。点击卡片在 VS Code
编辑器中打开该文件。焦点立即移动。

### 智能体间消息日志

当智能体之间互发消息（`team.message.*` 事件）时，面板底部会出现 **▶ 消息 (N)** 开关。
点击展开可滚动日志：

```
planner  →  coder    implement add_task(title), write to tasks.json…
planner  →  tester   write unit tests for all four operations…
coder    →  tester   tasks.py is done, file is at /…/tasks.py
```

发送者名称按其泳道卡片着色。日志保留最近 50 条消息并自动滚动到最新条目。

### 摘要卡片

当每个工作线程都达到 DONE 时，实时泳道卡片被会话摘要替换，包含每个智能体的总耗时：

```
✓ plan-code-review · Session complete
Agents              3
Tasks completed     3
Messages exchanged  9
plan-agent    · 0:58
code-agent    · 1:24
review-agent  · 0:49
```

### 调试控制台

页头中的 ☰ 菜单包含**调试日志**（默认关闭）。启用可看到驱动地图的每个事件的实时、带时间戳
日志——原始团队事件（`team.event: …`）和工具归属（`tool: … · member`），带**清除**和**复制**
按钮。

### Swarm Map 故障排查

| 症状 | 可能原因 | 解决方法 |
|---|---|---|
| Swarm Map 从不打开 | 服务器未发出 `team.member.spawned` | 在输出 → JiuwenSwarm 中检查 `team.event:` 行；确认服务器发送团队事件 |
| 出现泳道卡片但无文件活动 | `chat.tool_call` 载荷缺少 `member_name` | 确认服务器在工具调用事件中包含 `member_name` |
| 只出现工作线程（无主线程泳道） | 符合预期——编排者被过滤掉 | 设计如此；泳道即工作线程 |
| 消息开关从不出现 | `team.message.*` 事件缺失或无 `content` 字段 | 检查服务器事件模式 |
| 不显示 ↗ 打开文件提示 | 智能体尚未调用任何文件工具 | 等待第一次 `read_file`、`write_file` 或 `str_replace_editor` 调用 |
| 点击导航到错误文件 | 事件中的文件路径是服务器绝对路径，与工作区不匹配 | 确保服务器发送与本地项目根目录匹配的绝对路径 |
