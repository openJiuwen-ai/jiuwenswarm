# Slash 命令现状表

本文档按**解析位置**区分：TUI 本地解析与 Gateway（Agent）侧解析。说明文字仅作速查，实现以代码为准。

---

## TUI 本地解析

在终端 UI 内直接处理，不经过 Gateway 的受控指令管线。

| 命令 | 作用 |
|------|------|
| `/clear` | 清屏 |
| `/color` | 调整 TUI 配色 |
| `/copy` | 复制上一条消息 |
| `/exit` | 退出 |
| `/help` | 查看可用命令 |
| `/theme` | 切换主题 |
| `/mode` | 切换当前模式（支持一级入口与直达值；**仅 IM 通道生效**） |
| `/config` | 修改配置（当前为 TUI 本地实现；规划改为走 Gateway 统一接口） |
| `/workspace_dir` | 设置、查看或清空 **TUI 工作区路径**（见下方说明） |

**`/workspace_dir`（TUI 工作区目录）**

- **子命令**：`/workspace_dir` 或 `/workspace_dir get` 查看当前值；`/workspace_dir set <path>` 保存路径（路径可含空格）；`/workspace_dir clear` 清空。
- **别名**：`/workspace-dir`。
- **持久化**：写入用户目录下 `~/.jiuwenclaw/tui-workspace-dir`（单行文本）。
- **与 Gateway 的关系**：在已保存非空路径时，TUI 通过 `sendEventOnly` 发往 Gateway 的请求 **`params` 会附带 `workspace_dir`**（与普通对话、部分中断/权限相关请求一致），供 Gateway / AgentServer 在 `Message.params` / `AgentRequest.params` 中使用；具体消费逻辑以 Agent 与扩展为准。

---

## Gateway / Agent 侧解析

由 Gateway 识别并转发至 AgentServer 等，与 TUI 内置表互不替代。

| 命令 | 作用 |
|------|------|
| `/add_dir` | 将文件夹权限设为可读写 |
| `/plan` | 切换规划子模式 |
| `/resume` | 见下方子用法 |
| `/new_session` | 创建新会话（**仅 IM 生效**） |
| `/mode` | 受控通道模式切换（支持一级与直达写法） |
| `/switch` | 在当前模式族内切换二级模式 |
| `/skills` | 见下方「技能」说明 |

**`/mode` 与 `/switch`（受控通道）**

- 一级入口：
  - `/mode agent` -> `agent.plan`
  - `/mode code` -> `code.normal`
  - `/mode team` -> `team`
- 已移除独立 `/team` 命令，请统一使用 `/mode team`。
- 直达写法（今日已支持）：
  - `/mode agent.plan` -> `agent.plan`
  - `/mode agent.fast` -> `agent.fast`
  - `/mode code.plan` -> `code.plan`
  - `/mode code.normal` -> `code.normal`
- 二级切换（按当前模式族）：
  - agent 族：`/switch plan` <-> `agent.plan`，`/switch fast` <-> `agent.fast`
  - code 族：`/switch plan` <-> `code.plan`，`/switch normal` <-> `code.normal`
- 非法组合（例如 `code.*` 下 `/switch fast`）返回：`非法指令`

**`/resume`**

- `/resume list`：列出历史会话  
- `/resume <conversation_id>`：恢复指定会话  

---

## `/skills`：IM 与 TUI 的差异（技能列表）

两端最终都会请求 **`skills.list`**，但**触发形式与展示不同**，尚未完全统一。

| 端 | 如何触发 | 行为摘要 |
|----|----------|----------|
| **IM**（飞书等受控通道） | 整行精确匹配 **`/skills list`**（多余空白会规范化后匹配） | Gateway 拦截该控制消息，请求 `skills.list`，结果以 IM 通知/卡片等形式展示（**单独一行 `/skills` 不会走该控制路径**）。 |
| **TUI**（CLI 内置 slash） | 输入 **`/skills`** | 本地执行内置命令：调用 `skills.list`，在会话里以 **列表视图** 渲染（标题「Skills」）；每条展示技能 **名称**、**路径**（若有）、**描述**（若有）；无数据时提示 *No skills returned*。 |

**小结**：IM 用「`/skills list`」；TUI 用「`/skills`」即可列出技能，二者写法不一致属已知现状。

---

## 正在开发

| 命令 | 说明 |
|------|------|
| `/model` | `/model add` 增加模型；`/model list` 列出当前模型 |
| `/compact` | 压缩当前上下文 |
| `/diff` | 展示本次任务涉及文件改动 |
| `/init` | 调用 `jiuwenclaw-init` |
| `/files` | 列出 Agent 目录文件 |
