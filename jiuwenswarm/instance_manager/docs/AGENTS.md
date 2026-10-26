# instance_manager/docs

`jiuwenswarm.instance_manager` 模块的设计归档目录。代码看 `git log` / 模块
[AGENTS.md](../AGENTS.md)，本目录承载**四类长期文档**——规约（specs）、设计叙事
（designs）、特性变更（features）与缺陷修复（bugfix）。

## 目录用途

| 目录 | 用途 | 关联触发 |
|---|---|---|
| `specs/` | 模块设计规约：跨子模块的契约、协议、边界、不变量、修改检查单。**长期有效**，描述"系统是什么样、改代码前必须知道什么"。 | 任何**模块设计规约变动**（契约 / 不变量 / 执行路径 / 端口分配公式 / 锁语义 / yaml 结构变化）都必须同步修订对应 spec；新规约主题 = 新 spec 文件 |
| `designs/` | 设计叙事文档：面向**理解**的概览与架构说明（特性文档 + 架构文档），描述"系统为什么这样设计、整体如何运转"。含 mermaid 架构图 / 时序图 / 设计决策表 / 术语表。与 specs 的分工：specs 给**改代码的人**（不变量 / 检查单），designs 给**理解系统的人**（概念 / 原理 / 全景）。 | 重大架构变动（新增子模块 / 装配链变化 / 设计决策表新增条目）时同步修订；specs 契约变化若影响叙事准确性（数据流 / 分层 / 决策理由），一并刷新 |
| `features/` | 特性变更归档：一次完整设计/重构/新特性的来龙去脉（决策 + 拒绝的方案）。**只增不删**，描述"为什么这么改"。当前暂无；首次特性变更时建立。 | 每次特性更新提交代码前归档 `F_NN_<slug>.md`；归档日期写进文档元信息 |
| `bugfix/` | 缺陷修复归档：一次线上/测试问题的现象、定位证据、根因与修复决策。**只增不删**，描述"为什么会坏、为什么这样修"。当前暂无；首次缺陷归档时建立。 | 根因涉及契约/不变量、或定位过程值得复用的缺陷修复必须归档；契约变了同步修订 spec |

`specs/` 是状态快照（描述当前），`features/` 是变更日志（描述过程），`bugfix/` 是故障档案
（描述失效），`designs/` 是入门叙事（描述全貌与理由）。当 spec 改了，通常对应一份
feature 或 bugfix 文档解释"为什么改成这样"；当分层数据流或设计决策变了，designs 同步。

## 命名规约

```
specs/    S_NN_<slug>.md    例：S_03_port-allocation-and-conflict-detection.md
features/ F_NN_<slug>.md    例：F_01_xxx.md
bugfix/   B_NN_<slug>.md    例：B_01_xxx.md
designs/  <descriptive>.md  例：instance_manager_overview.md（特性文档）/ architecture_cn.md（架构文档）
```

- 前缀 `S_` / `F_` / `B_` 表明文档类型，grep / IDE 列表可视识别
- `NN` 是两位**递增序号**（`01`、`02`、…），目录内已有 N 份文档时新文档编号 `N+1`；不复用、不回填空缺
- `<slug>` 短横线分隔的英文小写 slug，描述主题
- 文件名末尾不带日期——日期写在文档元信息表里
- **designs 例外**：不参与 S/F/B 编号体系——它是少量长期维护的叙事文档（非增量归档），
  用描述性文件名（`<模块>_overview.md` 概览 / `architecture_cn.md` 架构）；新增主题叙事
  文档时用同样的描述性命名

不允许：序号跳号（features 不允许删除）、大写字母、下划线（`_` 仅用于前缀分隔）、空格、
未带 `S_` / `F_` / `B_` 前缀的散落 markdown（designs 下的描述性命名文档除外）。

## specs 内容约定（本模块骨架）

本模块的 specs 承担"修改前必读的全面说明书"职责，标准骨架如下（可根据主题增减，
但元信息 / 范围 / 不变量 / 修改检查单四节缺一不可）：

```markdown
# <Spec 名称>

## 元信息
| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_NN / <slug> |
| 关联模块 | jiuwenswarm/instance_manager/<path> |
| 最近一次修订日期 | <YYYY-MM-DD> |
| 关联 feature | F_NN_<slug>.md（暂无时写"暂无"） |

## 范围 / 边界
（本规约管什么、不管什么 → 指向其它 spec）

## 不变量
（N1、N2… 编号，每条可判定；点名数量的句子是契约的一部分）

## 接口契约
（关键签名 / 参数语义 / 错误语义）

## 数据结构
（状态字段 / 谁写谁读 / 何时清理——含持久化键与生命周期）

## 执行路径 / 装配关系
（如适用：P1、P2… 编号路径，逐步标注 文件+方法名；这是"改哪些路径才算改全"的依据）

## 边界 case 清单
（如适用：场景 / 现行为 / 风险 / 测试覆盖）

## 修改检查单
（"改 X → 必须连带检查 Y" 格式）

## 与其它 spec 的关系
```

### specs 间的引用与分工

- 跨 spec 引用一律写 `[[S_NN]]`（上下文明确时），不复制对方内容——单一真相源。
- spec 内不变量用 `N1`/`N2` 编号（各 spec 独立编号），供 code review 单条引用；
  模块 `AGENTS.md` 的"架构铁律"从各 spec 不变量中提炼，铁律条目回链 `[[S_NN]]`。
- 领域内统一编号以源头 spec 为准、其它 spec 引用不另立体系：命名规则与数据模型
  （[[S_02]]）、端口分配公式与探测语义（[[S_03]]）、instances.yaml 结构与原子写
  （[[S_04]]）、锁与 PID 生命周期（[[S_05]]）、状态判定与进程停止（[[S_06]]）、
  bootstrap .env 与早期引导（[[S_07]]）。

## features / bugfix 内容约定（建议骨架）

### features/

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
## 决策
## 拒绝的方案
## 验证
## 已知遗留
```

### bugfix/

```markdown
# <缺陷标题：一句话写清"什么条件下坏了什么">

## 元信息
| 项 | 值 |
|---|---|
| 日期 | YYYY-MM-DD |
| 严重程度 | <高/中/低 + 影响面> |
| 范围 | <修改路径> |
| 测试基线 | <pytest 结果> |
| 关联 spec | S_NN_<slug>.md（契约有变时必填） |

## 现象
## 根因
（证据链 + 代码层面的失效路径；写清复现条件）
## 决策
## 拒绝的方案
## 验证
## 已知遗留
```

可以根据情况增减小节，但 **"决策"和"拒绝的方案"是 features / bugfix 的核心**，缺它们等于
没归档——commit message 已经写了 what，feature 文档要写 why-not。

## 提交约定

涉及本目录文档（`S_*` / `F_*`）更新的特性改动，**特性代码、测试代码、文档拆成三个连续提交**：

1. **提交 1 — 特性代码**：`feat(instance-manager): <实现>`（或 `fix` / `refactor` 等对应 type）。
2. **提交 2 — 测试代码**：本次新增 / 改动的单测，`test(instance-manager): ...`。
3. **提交 3 — 文档**：本次涉及的 `specs/S_NN_*.md` 修订与 `features/F_NN_*.md` 新增，
   `docs(instance-manager): ...`。

三个提交必须**连续紧邻**，在同一工作单元 / PR 内落地——既不让大段文档 diff 淹没代码评审，
让测试改动独立可审，也不把文档归档拖到下次而丢失设计上下文。这是硬约束，不是"同一次
commit 也行"。scope / footer 等其余约定见模块 `AGENTS.md` 的「提交约定」段。

**例外——缺陷修复合并为一个提交**：归档到 `bugfix/B_NN_*.md` 的缺陷修复，修复代码、
回归测试、`B_NN` 文档及其连带的 `specs/S_NN_*.md` 修订合并为**一个** `fix(instance-manager)`
提交。缺陷修复的代码、回归用例和根因记录是同一份证据链：拆开后单看修复提交无法判断修的是
什么，单看测试提交又复现不出问题，cherry-pick / revert 时也必须作为一个整体移动。

## 与其它 AGENTS.md 的关系

- `jiuwenswarm/instance_manager/AGENTS.md`：模块入口索引 + 公开 API + 架构铁律 + **本目录的归档强制约束**
- 本 `docs/AGENTS.md`：只讲归档结构与命名规约，**不重复模块设计内容**——内容写在对应
  `S_*` / `F_*` / `B_*` 文档里
- 同仓 `jiuwenswarm/common/docs/AGENTS.md` 与
  `jiuwenswarm/server/runtime/skill_turbo/docs/AGENTS.md`：其它模块的同类归档规约，
  体系一致、互不管辖；instance_manager 的规约变动不触碰它们

## 反模式

- **把 commit message 复制粘贴当 feature 归档**：feature 要写 commit 写不下的东西（拒绝的
  方案、设计思路、follow-up），不是 commit 的拷贝
- **靠 docs 维护代码行为**：实现行为是代码 + 单测的事；specs 描述契约 / 路径 / 检查项，
  不是 source of truth
- **契约改了 spec 不改**：端口公式 / 保留名集合 / yaml 结构 / 锁超时变了但对应 spec 没改，
  下次读 spec 的人（或 agent）会被误导——提交前自查
- **一次连贯设计拆成 N 份小文档**：一次设计变动归档一份；多 commit 落地的特性也归档为
  一份，不按 commit 拆
- **在元信息里写 commit hash**：只写日期（`YYYY-MM-DD`）——hash 需要"提交后回填"，来回
  反复且容易过时
- **spec 里堆砌问题故事**：specs 写"系统现在是什么样 + 改动必须检查什么"；变更缘由归
  feature / bugfix 文档
