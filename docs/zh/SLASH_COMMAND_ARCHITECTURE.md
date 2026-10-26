# Slash 命令体系设计（架构说明）

> **文档性质**：架构与演进约定，描述「单一事实来源」、分层边界与落地原则。  
> **与现有文档关系**：能力清单与入口对照见 [`命令行指令.md`](./命令行指令.md)；本文不重复罗列全部命令，只定义**如何组织与避免漂移**。

---

## 1. 背景与问题

当前项目中以 `/` 开头的指令存在多处实现：

- **Gateway**：`MessageHandler._handle_channel_control` 处理受控通道上的 `/new_session`、`/mode …` 等，并决定是否**拦截、不转发 Agent**。
- **IM 管线 / 各 Channel**：例如 `gateway/im_pipeline/im_inbound.py` 中的控制消息集合、飞书/企业微信等通道内的文本判断，可能与主逻辑**集合不一致**。
- **CLI TUI**：`jiuwenswarm/channels/tui/frontend/src/core/commands/` 下本地注册表，部分命令通过 WebSocket 调用后端，部分纯本地。

若不建立明确分层与单一数据源，会出现：**同名命令语义不一致、文档与代码漂移、新通道重复实现解析规则**。

---

## 2. 设计目标

| 目标 | 说明 |
|------|------|
| **单一事实来源（SSOT）** | 对「网关侧控制的通道 slash」而言，命令名、合法形式、是否整行匹配等应在一处定义，其它模块**引用**而非复制常量集合。 |
| **边界清晰** | 区分「仅 Gateway」「仅客户端」「两端都需对齐名称」三类，避免把「全产品 slash」误塞进 Gateway 单一模块。 |
| **可演进** | 新增受控通道时，只需对照注册表与 `_control_channel_types` 等策略，减少遗漏。 |
| **与 REPL 约定一致** | 本地 UI 侧 slash（如帮助、诊断类）仍**优先在客户端解析**；**未识别的 `/xxx` 不应默认当作普通用户内容上送 Gateway**（见项目内 REPL 设计说明）。 |

### 2.1 非目标

- 不要求**所有** slash 都在 Python Gateway 内执行（CLI 专属命令仍属客户端）。
- 不强制一次重构消灭所有历史分支；允许分阶段把「常量与解析」迁到 SSOT，行为保持不变。

---

## 3. 三层分类（必须遵守）

### 3.1 A 类：Gateway 通道控制（Channel Control）

**定义**：到达 Gateway 用户消息中，由 `MessageHandler`（或其后继统一入口）识别后，**可能只改会话/模式/路由，而不进入 Agent 对话**的指令。

**典型**：`/new_session`、受控通道上的 `/mode agent|code|team` 与 `/switch plan|fast|normal|team`，以及 `/mode agent.plan|agent.fast|code.normal|code.team` 直达写法（以当前实现为准）。TUI 本地额外支持 `/mode plan`、`/mode team.normal`，这些不属于 Gateway 受控通道白名单。

**要求**：

- 合法形式、是否允许带多余字符、非法时的提示方式，应由 **SSOT** 描述；`im_inbound`、各 IM Channel 若需预判，应**引用同一 SSOT**，禁止私自维护另一份「子集」常量。
- 注册项中应标明 **适用的通道类型**（与 `_control_channel_types`、`_session_map_channel_types` 等策略一致），避免「全通道一刀切」。

### 3.2 B 类：客户端本地（Client-Only）

**定义**：仅在 CLI TUI、Web REPL 等**本地**解析与执行，或明确不应经 Gateway 做语义拦截的命令。

**典型**：CLI 的界面切换、`/resume`、`/model`（若仅本地配置）、帮助/诊断类 stub 等（以 [`命令行指令.md`](./命令行指令.md) 为准）。

**要求**：

- **不**纳入 Gateway 的「拦截表」作为唯一真相；若需与后端模式对齐，通过已有协议字段（如 `params.mode`）传递，而不是在 Gateway 再解析一遍 CLI 专有别名。
- 文档中标注 **处理进程**：Node CLI / 浏览器前端等。

### 3.3 C 类：名称对齐、执行分端（Hybrid）

**定义**：用户感知的**名称**与帮助文案希望跨 CLI 与 IM 一致，但**执行**发生在客户端 + 后端组合（例如先本地校验再发 RPC）。

**要求**：

- 在 SSOT 或文档矩阵中增加 **`canonical_name` + `cli_alias`** 等字段，避免「CLI 与 IM 斜杠写法不一致」时无据可查。
- Gateway **不必**理解 CLI 别名；若未来需要服务端识别，应显式列为新需求并扩展协议，而非隐式复用用户输入字符串。

---

## 4. 单一事实来源（SSOT）建议形态

### 4.1 模块位置（建议）

- **Python 侧**：已实现为 `jiuwenswarm/gateway/slash_command.py`（受控通道解析、`CONTROL_MESSAGE_TEXTS`、第一批命令元数据 `FIRST_BATCH_REGISTRY`）。亦可按演进拆分为 `channel_control_slash.py` 等。原建议职责为：
  - **数据**：受控命令的集合、匹配规则（精确 / 前缀 / 禁止多行等）、元数据（说明、是否转发 Agent）。
  - **纯函数**：给定通道类型与用户文本，返回**结构化判定结果**（未命中 / 命中且合法 / 命中但非法），**不**直接做 IO（不在这里 `create_task` 发通知）。
- **命名建议**：若仅包含 A 类，模块名应避免被误解为「全站所有 slash」，以免后续贡献者把 B 类逻辑塞入 Gateway。

### 4.2 注册表最小字段（建议）

| 字段 | 说明 |
|------|------|
| `id` | 内部稳定标识（如 `new_session`、`mode_switch`）。 |
| `patterns` | 合法用户输入形式（含是否仅整句精确匹配）。 |
| `scope` | `gateway`（本设计主体）。 |
| `channels` | `all_controlled` 或显式列表，与配置中的通道类型对齐。 |
| `intercept` | 命中且合法时是否不转发 Agent（与现 `_handle_channel_control` 语义一致）。 |
| `notes` | 与 CLI 别名、文档锚点的交叉引用（链接到 [`命令行指令.md`](./命令行指令.md) 章节）。 |

### 4.3 与 `MessageHandler` 的关系

- **解析与判定**：调用 SSOT 模块，减少 `message_handler.py` 内联 `startswith` 链。
- **副作用**：仍在 `MessageHandler`（或专门的服务类）中执行取消任务、下发通知等，避免在「表模块」中掺杂异步副作用，便于单元测试。

---

## 5. 各入口如何接入（原则）

| 入口 | 原则 |
|------|------|
| **Gateway `MessageHandler`** | 受控通道用户文本进入统一控制逻辑前，以 SSOT 为准做判定。 |
| **`im_inbound` 等管线** | 若需提前过滤或统计，**import 同一常量/函数**，禁止维护独立 `frozenset` 子集。 |
| **Feishu / WeCom 等 Channel** | 若因协议必须在 Channel 层做轻量判断，应**复用 SSOT**或仅做「是否可能为控制指令」的粗筛，最终语义以 Gateway 为准；若存在双点判断，须在注册表 `notes` 中写明并压测避免重复提示。 |
| **CLI** | 继续维护 TS `registry`；与 A 类命令**名称对齐**见 [`命令行指令.md`](./命令行指令.md) 与本文第 3.3 节，不在 Gateway 重复实现 B 类。 |

---

## 6. 与「未知 slash」相关的协议约定

- **客户端（REPL/TUI）**：以 `/` 开头的输入应先走本地命令路由器；未识别命令应对用户提示并**不**作为普通对话内容上送（与产品体验一致时），详见 REPL 设计文档中的 Slash 章节。
- **Gateway**：仅对**确实会到达 Gateway 的通道**保证按 SSOT 处理 A 类；不要求 Gateway 识别所有 CLI 专有命令。

---

## 7. 测试与质量保障

- 为 SSOT 提供**单元测试**：覆盖精确匹配、非法后缀、多行文本、`/mode` 一级入口与直达值、`/switch` 合法/非法组合等与现网一致的行为。
- 回归时注意：**SessionMap 通道族**与普通受控通道在 `session_id` 行为上的差异仍由现有状态机负责，SSOT 只解决「字符串层」一致性与可维护性。

---

## 8. 分阶段落地（建议）

| 阶段 | 内容 |
|------|------|
| **P0** | 提炼 A 类命令到 SSOT 模块；`im_inbound` 等改为引用，消除集合漂移。 |
| **P1** | 收敛 `message_handler.py` 内联解析到对 SSOT 的调用，行为不变。 |
| **P2** | 在文档矩阵中补齐 B/C 类与 [`命令行指令.md`](./命令行指令.md) 的交叉引用；可选：生成给前端的只读清单（若未来需要统一补全）。 |

---

## 9. 小结

- **Gateway 中的「表」**应主要承载 **A 类（通道控制）** 的 SSOT，而不是全产品所有 slash。
- **B 类**保留在客户端；**C 类**用文档与字段做名称对齐，避免强行集中执行逻辑。
- 成功标准：**一处改命令集合，管线与 Gateway 主路径不再分叉**；CLI 与 IM 的体验边界仍清晰、可文档化。

---

## 10. 当前命令现状表（基于 `gateway/slash_command.py`）

当前命令清单已拆分到独立文档：[`Slash命令表.md`](./Slash命令表.md)。
