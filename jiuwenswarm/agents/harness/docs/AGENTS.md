# agents/harness/docs

`jiuwenswarm.agents.harness` 模块的设计归档目录。代码看 `git log` / 模块
[AGENTS.md](../AGENTS.md)，本目录承载**长期设计规约文档（specs）**与**变更归档
（features / bugfix）**。结构规约、命名与提交约定见本文。

harness 目录下辖 claw / code / common / flash / team / work 七个子树（外加本 docs/ 与两个
观测性根级件），是按**能力域**（任务规划、可靠性、subagent、中断交互、事件流等）而非按
目录立档的——一个 spec 可能横跨多个子树的文件。当前已归档的域见下表；**未归档的域
不等于不存在，只等于还没有契约文档**，动手前先确认要改的文件是否落在已归档域内。

| 已归档域 | specs | 建档批次 |
|---|---|---|
| 任务规划（todo 工具链 / 任务执行 rail / 计划审批 / profile 变体 / deepresearch 落盘） | [[S_01]] ~ [[S_06]] | 首批 |
| 可靠性（LLM 调用层失败恢复 / 工具循环熔断 / 并发安全 / 输入侧守卫） | [[S_07]] ~ [[S_10]] | 第二批 |
| subagent（创建分发 / 持久运行时 / 授权审批 / 分发通道与宿主集成） | [[S_11]] ~ [[S_14]] | 第三批 |
| 中断交互（中断原语与三态决策 / 权限拦截与授权记忆 / 宿主装配与交互转换 / 相位守卫与台账 / 取消结算与收尾） | [[S_15]] ~ [[S_19]] | 第四批 |
| 事件流（正向发射通道：底层 stream 队列、六类事件发射、sid 解析、cwd rebind、symphony 桥接、装配挂载、消费判定 / 暂停检查点与上下文修复：pause-resume-abort 状态机、早 checkpoint、取消结果收集、占位检测、call_goal 注入剥离、HITL 改写） | [[S_20]] ~ [[S_21]] | 第五批 |

## 目录用途

| 目录 | 用途 | 关联触发 |
|---|---|---|
| `specs/` | 模块设计规约：不变量、接口契约、执行路径全景、异常边界 case 清单、修改检查单。**长期有效**，描述"系统现在是什么样、改代码前必须知道什么"。 | 任何**模块设计规约变动**（契约 / 不变量 / 执行路径 / 事件协议 / 持久化结构变化）都必须同步修订对应 spec；新规约主题 = 新 spec 文件 |
| `features/` | 特性变更归档：一次完整设计/重构/新特性的来龙去脉（决策 + 拒绝的方案）。**只增不删**，描述"为什么这么改"。自 [[F_01]] 起建立。 | 每次特性更新提交代码前归档 `F_NN_<slug>.md`；归档日期写进文档元信息 |
| `bugfix/` | 缺陷修复归档：触及 spec 描述的执行路径 / 不变量的修复（复现场景 + 根因 + 修复口径）。**只增不删**。 | 触及 spec 路径 / 不变量的 bugfix 落地时归档 `B_NN_<slug>.md`，随单个 `fix(harness)` 提交落地（见提交约定） |

`specs/` 是状态快照（描述当前），`features/` 是变更日志（描述过程）。spec 改动通常对应一份
feature 文档解释"为什么改成这样"。

## 命名规约

```
specs/    S_NN_<slug>.md    例：S_02_task-execution-rail.md
features/ F_NN_<slug>.md    例：F_01_xxx.md
bugfix/   B_NN_<slug>.md    例：B_01_xxx.md
```

- 前缀 `S_` / `F_` / `B_` 表明文档类型，grep / IDE 列表可视识别
- `NN` 是两位**递增序号**，目录内已有 N 份文档时新文档编号 `N+1`；不复用、不回填空缺。
  **specs 编号跨域连续**：任务规划域占用 S_01~S_06、可靠性域占用 S_07~S_10、subagent
  域占用 S_11~S_14 后，下一个新域（中断交互、事件流……）的 spec 从 S_15 起编，不各域
  从 1 重编——编号唯一指向一份文档
- `<slug>` 短横线分隔的英文小写 slug，描述主题
- 文件名末尾不带日期——日期写在文档元信息表里

不允许：序号跳号（features / bugfix 不允许删除）、大写字母、下划线（`_` 仅用于前缀分隔）、
空格、未带 `S_` / `F_` / `B_` 前缀的散落 markdown。

## specs 内容约定（本模块特有骨架）

本模块的 specs 承担"修改前必读的全面说明书"职责，除标准骨架外强制包含三个特色章节
（缺任何一个等于没归档）：

```markdown
# <Spec 名称>

## 元信息
| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_NN / <slug> |
| 关联模块 | jiuwenswarm/agents/harness/<path>（跨子树时全部列出） |
| 最近一次修订日期 | <YYYY-MM-DD> |
| 关联 feature | F_NN_<slug>.md（暂无时写"暂无"） |

## 范围 / 边界
（本规约管什么、不管什么→指向其它 spec 或其它模块的 AGENTS.md）

## 不变量
（N1、N2… 编号，每条可判定；点名数量的句子是契约的一部分）

## 接口契约
（关键签名 / 参数语义 / 错误语义）

## 数据结构
（状态字段 / 谁写谁读 / 何时清理——含持久化键与生命周期）

## 执行路径全景
（P1、P2… 编号路径，逐步标注 文件+方法名；这是"改哪些路径才算改全"的依据）

## 异常分支与退出路径
（异常分型矩阵 / 拒绝与降级语义 / 违反后果）

## 边界 case 清单
（表格式：场景 / 现行为 / 风险 / 测试覆盖）

## 修改检查单
（"改 X → 必须连带检查 Y" 格式）

## 与其它 spec 的关系
（含与 jiuwenswarm 其它 AGENTS.md 体系的互链——common / agent_adapter /
runtime 等，跨模块接缝在此登记，不复制对方内容）
```

### specs 间的引用与分工

- 跨 spec 引用一律写 `[[S_NN]]`（上下文明确时），不复制对方内容——单一真相源。
- **代码锚点引用**：spec 中的行号 / 行数 / 计数以**修订当日仓内 head** 为基准（元信息
  "最近一次修订日期"即基准日）；定位一律「文件 + 函数名 / 类名」优先、行号仅辅助——
  行号漂移不代表契约变化，对不上时以函数名为准，修订 spec 时顺手用当前代码刷新锚点。
- spec 内不变量用 `N1`/`N2` 编号（各 spec 独立编号），供 code review 单条引用；
  模块 `AGENTS.md` 的"架构铁律"从各 spec 不变量中提炼，铁律条目回链 `[[S_NN]]`。
- 任务规划域内的跨 spec 共享编号：串行阻塞补发、双懒启动、收口三帧等机制编号
  统一以对应 spec 为准（见各 spec「与其它 spec 的关系」节），其它 spec 引用这些
  编号，不另立编号体系。

## 提交约定

涉及本目录文档（`S_*` / `F_*`）更新的特性改动，**特性代码、测试代码、文档拆成三个连续提交**：

1. **提交 1 — 特性代码**：`feat(harness): <实现>`（或 `fix` / `refactor` 等对应 type）。
2. **提交 2 — 测试代码**：本次新增 / 改动的单测，`test(harness): ...`。
3. **提交 3 — 文档**：本次涉及的 `specs/S_NN_*.md` 修订与 `features/F_NN_*.md` 新增，
   `docs(harness): ...`。

三个提交必须**连续紧邻**，在同一工作单元 / PR 内落地。bugfix 触及 spec 描述的执行路径 /
不变量时，归档 `bugfix/B_NN_<slug>.md`（复现场景 + 根因 + 修复口径），代码、测试、文档合并为
**一个** `fix(harness)` 提交——三连续提交的唯一例外，与 skill_turbo / common / skill
模块的约定**已统一**；未触及 spec 的普通小修复不必归档。

## 与其它 AGENTS.md 的关系

- `jiuwenswarm/agents/harness/AGENTS.md`：模块入口索引 + 公开入口 + 架构铁律 +
  **本目录的归档强制约束**（当前覆盖任务规划、可靠性、subagent、中断交互与事件流五域，
  其余域扩充时同步更新该声明）
- 本 `docs/AGENTS.md`：只讲归档结构与命名规约，**不重复模块设计内容**——内容写在对应
  `S_*` / `F_*` 文档里
- `jiuwenswarm/common/AGENTS.md`（S_14 todo 快照 / 代际过滤；其 S_07
  workspace-safety 覆盖配额门禁底层，与本域 [[S_10]] 的 WorkspaceQuotaRail 是薄挂载接缝）、
  `jiuwenswarm/server/runtime/agent_adapter/AGENTS.md`（todo close 文案保持、plan 暂停、
  陈旧 todo 清理、task id 绑定 patch 等散件）：这些文件与已归档域有接缝，接缝归属
  在对应 spec 的「与其它 spec 的关系」节登记——本目录不复制其内容，需要时引用
- `jiuwenswarm/server/runtime/skill_turbo/AGENTS.md`：另立体系（技能加速引擎），与
  本域正交；其 `hitl_dedup.OuterCallIDDeduplicator` 被 agent_adapter 复用属中断域
  接缝，不在本域 spec 展开

## 反模式

- **把 commit message 复制粘贴当 feature 归档**：feature 要写 commit 写不下的东西（拒绝的
  方案、设计思路、follow-up）
- **靠 docs 维护代码行为**：实现行为是代码 + 单测的事；specs 描述契约 / 路径 / 检查项，
  不是 source of truth
- **契约改了 spec 不改**：执行路径 / 不变量 / 事件协议 / 持久化结构变了但对应 spec 没改，
  下次读 spec 的人（或 agent）会被误导——提交前自查
- **在元信息里写 commit hash**：只写日期（`YYYY-MM-DD`）
- **spec 里堆砌问题故事**：specs 写"系统现在是什么样 + 改动必须检查什么"；变更缘由归
  feature 文档
- **一次连贯设计拆成 N 份小文档**：一次设计变动归档一份
- **按目录而非按域立 spec**：harness 的 rails/tools 分散在四个子树，同一机制常横跨
  common / code / flash——spec 按"改哪块机制"切，不按目录树切
