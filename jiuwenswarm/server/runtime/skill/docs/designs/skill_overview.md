# 技能管理（Skill Management）特性文档

## 概述

技能（Skill）是 JiuwenSwarm 让 Agent 获得领域专长的核心机制：一个技能 = 一个包含 `SKILL.md`（frontmatter 声明 name / description + 正文指南）的目录，Agent 通过 `skill_tool` 读取指南后按其流程工作。**技能管理模块**负责技能的**全生命周期**——从市场发现、安装落盘、启用禁用，到运行时的授权、凭据注入、使用统计，最终卸载回收；同时以企业预置同步、多租户账本、产物完整性验证支撑企业形态的技能分发。

本模块横跨两个代码位置：**服务端管理**在 `jiuwenswarm/server/runtime/skill/`（安装 / 账本 / 市场渠道 / 预置同步），**Agent 运行时集成**在 `jiuwenswarm/agents/harness/common/{rails,tools}/`（7 个 rail + 2 个工具集）。技能"是什么、怎么被读取"的原语定义在 agent-core 仓（`openjiuwen/harness/`），本模块是其之上的产品化管理层——core 只认文件系统上的 skills 目录，本模块决定**目录里有什么、谁能看、怎么来的**。

本文从概念层面介绍技能管理的功能、原理与能力边界，帮助读者理解系统；改代码前请先读 `docs/specs/` 对应规约。

---

## 核心概念

### 技能生命周期总览

```
发现（search_skill / skills.* RPC）          ← 三市场 + builtin + Source SPI
  → 安装（install_skill / 预置同步 / 本地导入）  ← 下载白名单 + SHA-256/HMAC 验证 + 安全解压
  → 落账（skills_state.json 四桶账本）          ← 实体身份（name/origin/slug/source_type）
  → 启停（skill_configs.enabled）               ← disabled 名单喂给 SkillUseRail
  → 使用（skill_tool 加载 → 授权门禁 → 凭据注入 → 工具执行 → 使用计数）
  → 休眠训练（使用量超阈值 → 离线自我进化）        ← SkillSleepRail + SkillSleepRunner
  → 卸载（uninstall_skill）                     ← builtin/prebuilt 保护 + 账本同步清理
```

### 安装账本：skills_state.json

每个工作区一份权威 JSON 账本，四个桶：`marketplaces`（git 市场仓库）、`installed_plugins`（市场安装记录）、`local_skills`（本地导入 / 手工拷入记录）、`skill_configs`（per-skill 启停）。**SkillManager 是这份账本的唯一读写者**：目录的每次变更（安装 / 卸载 / 预置同步）都在同一事务内落账；core 侧 `SkillUseRail` 的启用名单、动态授权的身份校验、工具层的去重判断全部回读它。企业版下账本变更经**双层锁**（进程内 RLock + 跨 Pod portalocker 文件锁）串行化，多 Pod 共享 workspace 不会写坏。

### 多源分发：六个内置渠道 + Source SPI 扩展面

| 渠道 | 形态 | 特点 |
|---|---|---|
| **SkillNet** | github 文件夹技能 | **异步安装**：install 立即返回 install_id，后台下载，状态轮询；工具层把它折叠成同步等待 |
| **ClawHub** | owner/slug + token | 目录名 = slug；重名技能按 slug 判存活 |
| **TeamSkillsHub** | asset_id | 全生命周期 RPC（含发布 / 打包 / 校验） |
| **builtin** | 仓库内置目录 | 复制安装到工作区；企业版不提供 |
| **web** | URL 直装 | 企业版技能分发（`skills.enterprise.install`） |
| **import_local** | 本地路径 / URL | 本地导入（目录 / zip / 白名单内 URL） |
| **Source SPI** | 扩展包 Provider | `SourceRegistry` 注册表：能力校验、懒启动、**fail-closed 下载白名单**——新增渠道的推荐路径（非内置渠道） |

企业版另有**预置同步**（`SkillPrebuiltSynchronizer`）：管理面模板硬同步到租户工作区——模板新增即安装、移除即硬删、用户技能身份不可侵犯、prebuilt 不可被用户卸载。

### 安全体系：四道防线

1. **下载白名单**（fail-closed）：所有远程下载先断言 URL host 在白名单（管理面显式配置 > Provider 声明默认 > 空 = 拒绝）。
2. **产物完整性**：SHA-256 摘要比对 + SkillHub HMAC-SHA256 签名验证（冻结作用域 `skillhub-artifact-v1`，签名输入为 scope/skill_id/version_id/sha256 四行拼接），失败即拒绝安装。
3. **安全解压**：zip-slip 防护（成员路径归一后验证落在目标内），tar / zip 统一分发。
4. **运行时凭据隔离**：per-skill 凭据（`react.skill_envs`）只注入给**当前激活技能**的 shell 命令；直接执行未激活技能的脚本被 **SKILL_GATE** 拦截并指引先激活——同一时刻一个技能的凭据绝不流入另一个技能的执行环境。

### Agent 运行时：7 个 rail + 2 个工具集

Agent 侧技能行为由一组按 priority 排序的 rail 协作（数值大者先执行）：

| 环节 | 组件 | 一句话职责 |
|---|---|---|
| 技能清单注入 | core `SkillUseRail`（95） | 扫描 skills 目录、注入 skills 段、注册 skill_tool |
| 加载授权 | `SkillAuthorizationRail`（95，core 骨架 + swarm 存储适配） | 技能加载门禁：Manifest 权限差分 → 审批卡 → Grant 激活/回收；子代理经委托版（父会话发卡、Grant 落 local） |
| 检索提示 | `SkillRetrievalPromptRail`（94，动态推导） | 大技能量场景：隐藏原生清单、注入树检索指引 |
| 使用计数 | `SkillSleepRail`（26） | 统计**工作工具**使用量（非加载量），超阈值（默认 20）触发离线休眠训练 |
| 活跃状态 | `SkillActiveStateRail`（25） | 会话级"当前激活技能"追踪（文件持久化 + 过期兜底清空） |
| 执行规范 | `SkillProtocolPromptRail`（8） | 注入 9 条技能执行规范（skill_tool 唯一加载入口 / todo 搭便车 / 顺序不跳步…） |
| 凭据注入 | `SkillCredentialInjectionRail`（5） | 凭据注入 + SKILL_GATE 跨技能拦截 |

工具面：**`SkillToolkit`**（`search_skill` / `install_skill` / `uninstall_skill`——市场管理，模型可自主装技能）与 **`SkillRetrievalToolkit`**（`skill_index_build` / `skill_branch_explore` / `skill_branch_peek`——已装技能的 agentic 树检索）。注意区分：`skill_tool`（读 SKILL.md 正文）与 `list_skill`（清单）是 core 侧原语，不在本模块。

### 授权门禁：动态授权

技能可声明权限扩展（Manifest）；加载时 `SkillAuthorizationRail` 计算权限 overlay 差分，弹审批卡让用户决定（本次允许 / 会话内允许 / 仅加载不授权），同意后激活 Grant（技能执行期间临时提权），`skill_complete` 或技能切换时回收。Builtin / 预置目录自动信任；低风险技能静默批准。Grant 与审批上下文落**会话文件**（`FileSkillGrantStore`），多进程一致、存储不可用时宁可中止本轮也不带旧状态跑。任务终止（取消 / 中断 / 合规停用）会**熔断** main scope 凭据窗口拦截迟到回调，全新的显式 `skill_tool` 激活才在激活落盘的同一事务内解除熔断。

### 技能检索：渐进披露

已安装技能很多时（数十上百个），全量清单注入会淹没模型上下文。Agentic 检索改为**树状渐进披露**：系统提示只给第一层分类，模型用 `skill_branch_explore` / `skill_branch_peek` 逐层下钻（不调 LLM、无相似度计算，靠模型自己决定看哪个分支），原生 `list_skill` 与全量清单在检索模式下被单窗口隐藏。

### 休眠训练：使用驱动进化

`SkillSleepRail` 按"任务内去重"统计每个技能经工作工具的真实使用次数，累计超过阈值（默认 20）触发 `SkillSleepRunner` 后台离线训练（自我进化）；失败回滚计数、指数退避。计数跨会话持久化（`call_counts.json`），"中途暂停的任务"归属存 `last_skills.json` 供恢复续账。

### 技能自演进流水线（skilldev，未接线）

`skilldev/` 是"用流水线创造技能"的脚手架：15 阶段确定性状态机（需求 → 规划 → 生成 → 校验 → 测试 → 评测 → 改进迭代 → 打包 → 描述优化），3 个人工确认挂起点，11 种前端事件，产物为可安装的 `.skill` 包。**当前未接线**（`ReqMethod` 缺枚举成员 + 包导出缺失 + Agent 层占位），生产不可达——详见 `docs/specs/S_07`。

### 企业形态

- **多租户**：`TenantAgentPool` per-tenant AgentManager，每租户 workspace 独立账本与 SkillManager 单例；企业版构造期 fail-fast（无 workspace 即 raise）。
- **预置同步**：管理面模板硬同步，`_prebuilt_skills` 快照同时喂给授权 trust 判定。
- **专属 RPC 面**：`skills.enterprise.*`（web 装卸 / 源查询）；工具层剔除 builtin 渠道、prebuilt 不可卸。
- **跨 Pod 一致性**：账本文件锁（30s 超时）+ 预置同步进程锁双层串行。

---

## 术语表

| 术语 | 含义 |
|---|---|
| **SKILL.md** | 技能入口文件：YAML frontmatter（name / description / allowed-tools…）+ Markdown 指南正文 |
| **skills 目录** | `<workspace>/skills/`，每个子目录一个技能；core 原语只认这个目录 |
| **skills_state.json** | 安装账本（四桶：marketplaces / installed_plugins / local_skills / skill_configs） |
| **SkillManager** | 账本唯一读写者 + skills.* RPC 路由全集（`_SKILL_ROUTES`）+ 渠道实现 |
| **origin / source_type** | 安装来源标识（URL / `clawhub:slug` / `teamskillshub:asset_id`）与企业身份（prebuilt / user） |
| **SKILL_GATE** | 凭据注入 rail 的执行位闸门：拦"直接运行未激活技能的脚本" |
| **Grant / Manifest** | 技能授权的临时权限凭证 / 技能声明的权限扩展描述（模型定义在 agent-core） |
| **执行位判定** | 从 shell 命令中识别"真正被执行的脚本"（解释器后首个脚本 token / 直执行脚本），只读引用不算 |
| **SourceRegistry / Source SPI** | 技能源 Provider 注册表 / 扩展包提供新渠道的协议（`extensions/sdk/skill_source.py`） |
| **预置同步（prebuilt sync）** | 企业管理面模板 → 租户工作区的硬同步 |
| **agentic 检索 / 渐进披露** | 模型逐层下钻技能树的检索模式（无 LLM、无相似度） |
| **休眠训练（skill sleep）** | 使用量超阈值触发的离线自我进化周期 |
| **active skill** | 会话级"当前激活技能"状态（凭据注入与归属判定的依据） |
| **.skill 包** | skilldev 产出的技能 zip 交付物（与官方 skill-creator 格式一致） |

## 能力边界

- 本模块**不管**技能加速执行（PlanNode 节点树）——那是 `skill_turbo`（有独立文档体系）。
- 本模块**不管** `skill_tool` / `list_skill` 工具原语与 `SkillUseRail` 目录扫描的实现——那是 agent-core 仓 `openjiuwen/harness/` 的技能原语层；本模块是其装配器与扩展者。
- 技能演进经验记录（`SkillEvolutionRail` / `TeamSkillEvolutionRail`）与创建建议（`SkillCreateRail`）在 agent-core 与 swarm providers 侧，本模块只提供休眠训练触发这一条进化路径。
- symphony 检索索引的构建与树披露算法在 `jiuwenswarm/symphony/`，本模块消费其 API。
