# context/docs

`jiuwenswarm.server.runtime.context` 上下文模块的设计归档目录。代码看 `git log` / 模块
`AGENTS.md`，本目录只承载**长期设计文档**——specs（系统现状规约）与 designs（特性 / 架构
叙事），features / bugfix 是变更归档区。结构规约、命名与提交约定见本文。

## 目录用途

| 目录 | 用途 | 关联触发 |
|---|---|---|
| `specs/` | 模块设计规约：不变量、接口契约、执行路径、修改检查单。**长期有效**，描述"系统现在是什么样、改代码前必须知道什么"。 | 任何**模块设计规约变动**（契约 / 不变量 / 执行路径 / processor 或 rail 行为变化）都必须同步修订对应 spec；新规约主题 = 新 spec 文件 |
| `designs/` | 面向**理解系统**的叙事文档（特性总览：功能 / 原理 / 能力边界 / 术语表）。 | 模块能力发生结构性变化时修订对应章节 |
| `features/` | 特性变更归档：一次完整设计 / 重构 / 新特性的来龙去脉（决策 + 拒绝的方案）。**只增不删**，描述"为什么这么改"。 | 每次特性更新提交代码前归档 `F_NN_<slug>.md` |
| `bugfix/` | 缺陷修复归档（触及 spec 描述路径 / 不变量的修复）。 | 修复落地时归档 `B_NN_<slug>.md` |

`specs/` 是状态快照（描述当前），`features/` 是变更日志（描述过程）。spec 改动通常对应一份
feature 文档解释"为什么改成这样"。

## 命名规约

```
specs/    S_NN_<slug>.md    例：S_05_overflow-recovery.md
features/ F_NN_<slug>.md    例：F_01_xxx.md
bugfix/   B_NN_<slug>.md    例：B_01_xxx.md
designs/  <自由命名>.md     例：context_overview.md
```

- 前缀 `S_` / `F_` / `B_` 表明文档类型，grep / IDE 列表可视识别
- `NN` 是两位**递增序号**，目录内已有 N 份文档时新文档编号 `N+1`；不复用、不回填空缺
- `<slug>` 短横线分隔的英文小写 slug，描述主题
- 文件名末尾不带日期——日期写在文档元信息表里

不允许：序号跳号（features / bugfix 不允许删除）、大写字母、下划线（`_` 仅用于前缀分隔）、
空格、未带 `S_` / `F_` / `B_` 前缀的散落 markdown。

## specs 内容约定（本模块骨架）

```markdown
# <Spec 名称>

## 元信息
| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_NN / <slug> |
| 关联模块 | jiuwenswarm/server/runtime/context/（或 agent_adapter / harness rails 等） |
| 最近一次修订日期 | <YYYY-MM-DD> |
| 关联 feature | F_NN_<slug>.md（暂无时写"暂无"）

## 范围 / 边界
（本规约管什么、不管什么 → 指向其它 spec）

## 不变量
（N1、N2… 编号，每条可判定；点名数量的句子是契约的一部分）

## 接口契约
（关键签名 / 参数语义 / 错误语义）

## 数据结构
（状态字段 / 谁写谁读 / 何时清理）

## 执行路径全景
（P1、P2… 编号路径，逐步标注 文件+方法名；"改哪些路径才算改全"的依据）

## 修改检查单
（"改 X → 必须连带检查 Y" 格式）

## 与其它 spec 的关系
```

### specs 间的引用与分工

- 跨 spec 引用一律写 `[[S_NN]]`（上下文明确时），不复制对方内容——单一真相源。
- spec 内不变量用 `N1`/`N2` 编号（各 spec 独立编号），供 code review 单条引用；
  模块 `AGENTS.md` 的"架构铁律"从各 spec 不变量中提炼，铁律条目回链 `[[S_NN]]`。
- 当前 7 份 spec 的分工见模块 `AGENTS.md` 入口索引；新增主题优先判断是否属于既有
  spec 的一节，确实独立才开新 spec。

## features 内容约定（骨架）

```markdown
# <Feature 名称>

## 元信息
| 项 | 值 |
|---|---|
| 日期 | YYYY-MM-DD |
| 范围 | <修改路径> |
| 测试基线 | <pytest 结果> |
| Refs | #<issue> |

## 背景
## 数据结构 / 状态机（如有）
## 决策
## 拒绝的方案
## 验证
## 已知遗留
```

**"决策"和"拒绝的方案"是 features 的核心**，缺它们等于没归档——commit message 已写了
what，feature 文档要写 why / why-not。

## 提交约定

涉及本目录文档（`S_*` / `F_*`）更新的特性改动，**特性代码、测试代码、文档拆成三个连续提交**：

1. **提交 1 — 特性代码**：`feat(context): <实现>`（或 `fix` / `refactor` 等对应 type）。
2. **提交 2 — 测试代码**：本次新增 / 改动的单测，`test(context): ...`。
3. **提交 3 — 文档**：本次涉及的 `specs/S_NN_*.md` 修订与 `features/F_NN_*.md` 新增，
   `docs(context): ...`。

三个提交必须**连续紧邻**，在同一工作单元 / PR 内落地。bugfix 触及 spec 描述的路径 / 不变量时
归档 `bugfix/B_NN_<slug>.md`（复现场景 + 根因 + 修复口径），代码、测试、文档合并为**一个**
`fix(context)` 提交——三连续提交的唯一例外。本约定与 skill 模块
（`server/runtime/skill/docs/AGENTS.md`）**已统一**。

## 与其它 AGENTS.md 的关系

- `jiuwenswarm/server/runtime/context/AGENTS.md`：模块入口索引 + 公开 API + 架构铁律 +
  **本目录的归档强制约束**
- 本 `docs/AGENTS.md`：只讲归档结构与命名规约，**不重复模块设计内容**——内容写在对应
  `S_*` / `F_*` 文档里

## 反模式

- **把 commit message 复制粘贴当 feature 归档**：feature 要写 commit 写不下的东西（拒绝的方案、设计思路、follow-up）
- **靠 docs 维护代码行为**：实现行为是代码 + 单测的事；specs 描述契约 / 路径 / 检查项，不是 source of truth
- **契约改了 spec 不改**：processor 链 / 溢出检测 / config 键变了但对应 spec 没改，下次读 spec 的人（或 agent）会被误导——提交前自查
- **在元信息里写 commit hash**：只写日期（`YYYY-MM-DD`）
- **spec 里堆砌问题故事**：specs 写"系统现在是什么样 + 改动必须检查什么"；变更缘由归 feature 文档
- **一次连贯设计拆成 N 份小文档**：一次设计变动归档一份
- **在本仓复制 agent-core 引擎规约全文**：core 实现契约在 `openjiuwen/core/context_engine/docs/`；本目录只写产品装配 / 溢出 / prompt，两边用职责表互链
