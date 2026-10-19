---
description: 技能管理模块说明书——技能全生命周期（安装/卸载/启停/账本/市场分发/企业预置）与 Agent 运行时集成（rails/工具/检索/休眠训练）的公开入口、模块地图、架构铁律与 spec 索引。给修改/检视本模块代码的 AI 助手与工程师。
---

# Skill Management（技能管理）

技能全生命周期管理子系统。一个技能 = 一个含 `SKILL.md` 的目录；本模块决定**目录里有什么、谁能看、怎么来的**：`SkillManager` 掌管安装 / 卸载 / 启停 / 账本（`skills_state.json`）、六个内置安装渠道（SkillNet / ClawHub / TeamSkillsHub / builtin / web / import_local）与 Source SPI 扩展面；agent 侧 7 个 rail + 2 个工具集负责运行时的授权门禁、凭据注入、活跃状态、使用计数与检索提示。技能"是什么、怎么被读取"的原语（`SkillUseRail` / `skill_tool` / `list_skill`）定义在 **agent-core 仓**，本模块是其产品化管理层。

本文件是模块入口索引。**修改任何代码前先读对应 spec**（见「设计文档归档与双向同步」）。**本模块代码横跨两个位置**：主体与文档归档在 `server/runtime/skill/`，agent 侧 rails / tools 在 `agents/harness/common/{rails,tools}/`（以文件清单方式纳入管辖，见模块地图）。

- `docs/specs/S_01_overview-and-boundaries.md` — 总览与包边界（跨目录管辖清单 / 两仓协作边界 / 装配关系 / RPC 路由全景）
- `docs/specs/S_02_skill-manager-and-ledger.md` — SkillManager 核心（路径布局 / skills_state.json 四桶账本 / 实体身份 / 事务与双层锁 / 盘→账本对账）
- `docs/specs/S_03_market-sources-and-distribution.md` — 技能源体系（SourceRegistry SPI / 六渠道契约 / HMAC 完整性验证 / 下载白名单 / 安全解压 / SkillNet 异步安装）
- `docs/specs/S_04_enterprise-prebuilt-sync.md` — 企业预置同步（模板硬同步 / 双安装路径 / 用户技能身份保护 / 多租户形态）
- `docs/specs/S_05_agent-skill-rails.md` — agent 侧 7 rails（priority 表 / 活跃状态 / SKILL_GATE / 授权链三层 / 休眠计数 / 两个 prompt rail / 热重载约定）
- `docs/specs/S_06_agent-skill-tools.md` — 工具集（SkillToolkit 三工具 / SkillRetrievalToolkit 树检索 / 与 core 侧 skill_tool 的边界）
- `docs/specs/S_07_skilldev-pipeline.md` — skilldev 自演进流水线（**未接线**——15 阶段状态机 / 三重接线断点 / 风险清单）
- `docs/designs/skill_overview.md` — 特性文档（概念层：生命周期总览 / 安全四道防线 / 术语表 / 能力边界）

姊妹模块：`server/runtime/skill_turbo/`（技能加速执行引擎）有独立 AGENTS.md 与 docs/specs，本体系不覆盖其内部设计。

## 统一术语

```
SKILL.md      技能入口文件（frontmatter + 指南正文）
└── skills 目录  <workspace>/skills/，core 原语只认这个目录（agent-core 不感知安装账本）
    └── skills_state.json  安装账本（SkillManager 唯一读写，四桶）
```

| 术语 | 含义 |
|---|---|
| **SkillManager** | 账本唯一读写者 + skills.* RPC 路由全集（`_SKILL_ROUTES`）+ 渠道实现 |
| **origin / source_type** | 安装来源标识（URL / `clawhub:slug` / `teamskillshub:asset_id`）与企业身份（`prebuilt` / `user`） |
| **SourceRegistry / Source SPI** | 技能源 Provider 注册表 / 扩展包提供新渠道的协议（`extensions/sdk/skill_source.py`） |
| **SKILL_GATE** | 凭据 rail 的执行位闸门：拦"直接运行未激活技能的脚本" |
| **执行位判定** | 从 shell 命令识别"真正被执行的脚本"（解释器后首个脚本 token / 直执行脚本）；只读引用不算 |
| **active skill** | 会话级"当前激活技能"（凭据注入与归属判定的依据；持久化在会话文件） |
| **Grant / Manifest** | 技能授权的临时权限凭证 / 技能声明的权限扩展（模型定义在 agent-core `harness/security/skill_authorization/`） |
| **FileSkillGrantStore** | Grant + 审批上下文 + 活跃状态三合一的会话文件存储（swarm 侧实现） |
| **预置同步（prebuilt sync）** | 企业管理面模板 → 租户工作区的硬同步（SkillPrebuiltSynchronizer） |
| **agentic 检索 / 渐进披露** | 大技能量场景：模型用 branch_explore/peek 逐层下钻技能树（无 LLM、无相似度） |
| **休眠训练（skill sleep）** | 使用量超阈值（默认 20）触发的离线自我进化周期 |
| **.skill 包** | skilldev 产出的技能 zip 交付物 |
| **disabled / execution disabled** | 账本全量禁用名 vs **已安装且禁用**名（后者喂给 SkillUseRail）——两个不同语义 |

## 公开入口（public API）

**包门面（`server/runtime/skill/__init__.py`）仅导出 `skilldev.state_utils` 的启停账本纯函数**（供 harness / swarm providers / evolution 免循环依赖消费，名单以 `__all__` 为准）：`filter_visible_skill_names` / `get_registered_skill_names` / `get_skill_enabled` / `get_state_file` / `list_disabled_skills` / `list_execution_disabled_skills` / `load_execution_disabled_skills` / `normalize_local_skills` / `normalize_skill_configs` / `set_skill_enabled`。**不导出 SkillManager**——走子模块全路径导入（窄门面约定）。

| 域 | 入口 | 说明 |
|---|---|---|
| 服务实例 | `SkillWorkspaceProvider.get_or_create_manager(workspace_dir)` | per-workspace 单例（`ensure` 先行，fail-closed）；**生产代码不要直接 `SkillManager()`**（单测除外） |
| RPC 面 | skills.* RPC 路由全集（server/runtime/agent_adapter/interface.py `_SKILL_ROUTES`）→ `handle_skills_*` 处理器（企业镜像路由 `SKILLS_ENTERPRISE_SOURCE_*` 与非企业路由共享 handler；`handle_skills_install_builtin` 为内部调用不经路由）+ `handle_plugins_*`（`_PLUGIN_ROUTES`） | 新增 RPC 三处同步：`ReqMethod` 枚举 + `_SKILL_ROUTES` + handler（见 [[S_02]]） |
| 同步查询 | `get_local_skills` / `list_skill_installations` / `get_skill_meta` / `is_builtin_skill` / `list_enabled_skill_names` / `find_skill_dir_by_identity` / `remove_skill_directory` | rails / tools 直接消费 |
| 企业安装 | `install_prebuilt_from_provider` / `install_skill_sync`（预置双路径，见 [[S_04]]） | provider 路径**无 url 回退** |
| Agent rails | `SkillActiveStateRail` / `SkillCredentialInjectionRail` / `SkillAuthorizationRail`（swarm 子类）/ `SubagentSkillAuthorizationRail` / `SkillSleepRail` / `SkillProtocolPromptRail` / `SkillRetrievalPromptRail` | 装配唯一入口在 interface_deep（[[S_05]] 装配表）；flash 走 keep 白名单、swarm 走 providers |
| Agent 工具 | `SkillToolkit`（search_skill / install_skill / uninstall_skill）/ `SkillRetrievalToolkit`（skill_index_build / skill_branch_explore / skill_branch_peek） | 工具描述是**行为契约**（[[S_06]]） |
| 活跃状态查询 | `get_session_active_skill` / `resolve_skill_session_id` / `clear_session_skill_state` | 凭据 rail / sleep rail 复用；default 会话只读不收养 |

**新增配置项走 config dict + 装配层注入**（`react.skill_envs` / `react.skill_stale_invoke_limit` / `react.evolution.*`），模块内部不读全局配置；新安装渠道优先走 Source SPI（扩展包 + SourceRegistry），不要继续往 skill_manager.py 堆渠道 handler。

## 模块地图

```
server/runtime/skill/                       ← 模块主体 + 文档归档点
├── __init__.py                # 门面：re-export state_utils 纯函数（窄门面）
├── skill_manager.py           # SkillManager（大文件，新渠道勿再加）：账本 + skills.* RPC 路由（含 Skill Graph，委托 symphony service）+ 六内置渠道 + Source SPI + git 市场 + 安全解压
├── source_registry.py         # SourceRegistry：SPI 注册表（能力校验 / 懒启动 / fail-closed 白名单合并）
├── artifact_security.py       # SHA-256 + HMAC-SHA256 产物验证（scope: skillhub-artifact-v1）
├── enterprise_state_lock.py   # 企业账本跨线程 RLock + 跨 Pod portalocker 双层锁
├── workspace_provider.py      # SkillWorkspaceProvider：fail-closed 工作区准备 + manager 单例缓存
├── skill_prebuilt.py          # SkillPrebuiltSynchronizer：企业预置模板硬同步
├── skilldev/                  # 自演进流水线（未接线，[[S_07]]）；state_utils.py 是账本纯函数库（被 SkillManager 反向引用）
└── docs/                      # 本设计文档体系

agents/harness/common/rails/               ← 本模块管辖（7 文件）
├── skill_active_state.py              # SkillActiveStateRail（25）：会话级活跃技能状态 + 过期兜底
├── skill_credential_injection_rail.py # SkillCredentialInjectionRail（5）：凭据注入 + SKILL_GATE 三分支
├── skill_authorization_rail.py        # SkillAuthorizationRail（core 子类）：授权门禁的文件存储适配（薄子类）
├── skill_sleep_rail.py                # SkillSleepRail（26）：使用计数 + 休眠训练触发（counter/runner 在 skill_sleep/）
├── skill_protocol_prompt_rail.py      # SkillProtocolPromptRail（8）：9 条执行规范注入（仅 agent 模式）
├── skill_retrieval_prompt_rail.py     # SkillRetrievalPromptRail（94 动态）：树检索提示 + 单窗口隐藏 list_skill
└── subagent_skill_authorization_rail.py # SubagentSkillAuthorizationRail：子代理委托授权（父会话发卡 / Grant 落 local）

agents/harness/common/tools/               ← 本模块管辖（2 文件）
├── skill_toolkits.py          # SkillToolkit：search/install/uninstall_skill（市场管理，模型可自主装技能）
└── skill_retrieval_toolkits.py # SkillRetrievalToolkit：树检索三工具（真实现 symphony/agentic_retrieval_toolkit）

相邻但不归本模块：
├── server/runtime/skill_turbo/          # 加速执行引擎（独立 AGENTS.md + docs）
├── agents/harness/common/rails/permissions/skill_authorization/  # FileSkillGrantStore + 授权权限 rail + runtime 辅助（授权存储，契约在 [[S_05]] 引用）
├── jiuwenswarm/symphony/                # 检索索引与树披露算法
└── agent-core openjiuwen/harness/…      # SkillUseRail / skill_tool / list_skill / 授权骨架（两仓边界见 [[S_01]]）
```

## 架构铁律

以下 12 条从各 spec 不变量提炼（全量不变量以 spec 为准，条目可被 code review 单条引用）：

1. **SkillManager 是技能状态唯一真相源**（[[S_02]] N1/N6）。账本一律临时文件 + `os.replace` + fsync 原子写；**落盘与落账必须在同一事务内**——只动盘不动账 = 对账时状态漂移。任何 rail / tool 层不得另建安装状态副本。
2. **core 只认目录，swarm 管目录内容**（[[S_01]] N2）。agent-core 原语不感知账本；swarm 对 skills 目录的每次变更（安装 / 卸载 / 预置同步 / builtin 清理）必须同步落账并触发 `SkillUseRail` 刷新或 Agent 重载。
3. **fail-closed 三处**（[[S_01]] N3）：工作区不可用即抛 `SkillWorkspaceUnavailable`（无 fallback）；下载白名单空即拒绝下载；产物验证（SHA-256 / HMAC）任一失败即拒绝安装。**不得为"可用性"放宽任何一处**。
4. **状态即文件 + AbortError**（[[S_05]] N2）。活跃状态与 Grant 走 `FileSkillGrantStore` 会话文件（portalocker + 原子写），无进程内缓存；存储不可用抛 `AbortError` 中止本轮——任何"带旧状态继续"的兜底都是违规。default 会话只读不收养（[[S_05]] N3）。任务终止（interrupt cancel/abort / 合规停用）熔断 main scope 拦截迟到回调；新 skill_tool 激活只能在**激活持久化的同一事务内**解除熔断（[[S_05]] N9）。
5. **单激活凭据语义**（[[S_05]] N1）。同一时刻只注入一个技能的凭据；跨技能执行硬拦（显式自带凭据的逃生口下**停注** active 凭据）；多技能复合命令指引拆分。改 SKILL_GATE 前先读 [[S_05]] 执行位判定的已知漏拦面声明。
6. **授权不可继承**（[[S_05]] N4）。子代理授权仅 main scope 父 Context 可装配；swarm `SkillAuthorizationRail` 以类属性 `inherit_to_subagents = False` 在代码层强制；scope 恒绑「主会话 + 子 scope」；子代理审批恒落 local Grant；生命周期 finally 清 scope。审批卡必须带 index 的 `OutputSchema`。
7. **热重载原位更新**（[[S_05]] N6）。技能链 rail 热重载一律 `update_*` 原位更新、不重建实例（重建会脱离 rail 注册链并丢动态加载技能）；`skill_envs` 空占位不清空已有凭据目录。
8. **工具层零业务持久化**（[[S_06]] N1）。SkillToolkit / SkillRetrievalToolkit 的全部技能 IO 走 SkillManager；企业安装的回滚顺序（记账失败 → 删目录）不可倒置（[[S_06]] N3）。
9. **预置集是管理面模板的完整镜像**（[[S_04]] N1/N2/N3）。模板移除即硬删（不降级）；`source_type=="user"` 的记录任何路径不可吞并（`skill_name_conflict` + `protected_source_types` 双保险）；prebuilt 不可被用户卸载 / 启停。
10. **provider 无 url 回退**（[[S_04]] N4）。预置的 provider 三元组路径失败即失败——provider 带 HMAC 验签、url 仅 sha256，两者信任模型不同，不可拉平。
11. **skilldev 未接线，勿假设其工作**（[[S_07]]）。`ReqMethod` 无 `SKILLDEV_*` 成员、包导出缺失、Agent 层全占位——生产不可达；接线前先补三重断点并处理风险清单（VALIDATE↔GENERATE 无重试上限、StateStore 无原子写等）。
12. **可见性单一来源**（[[S_05]] N8 / [[S_06]] N4）。`_visible_skill_names_for_list_skill`（实时扫描 + enabled / disabled / 企业过滤）同时喂检索 prompt rail 与检索 toolkit——prompt、工具、SkillUseRail 三方必须看到同一技能视图。

## 生命周期 / 配置消费

### 配置项（config.yaml，装配层读取后注入）

| 配置 | 消费点 | 默认 |
|---|---|---|
| `react.skill_envs` | `SkillCredentialInjectionRail`（per-skill 凭据 `{skill: {ENV: val}}`） | 空 |
| `react.skill_stale_invoke_limit` | `SkillActiveStateRail` 过期兜底轮数 | 5 |
| `react.evolution.skill_sleep` + `call_threshold` | `SkillSleepRail` / `SkillSleepRunner` 休眠训练 | 阈值 20 |
| `react.skill_turbo.enabled` | 姊妹模块（不归本模块） | false |
| 企业 `skill_prebuilt` 模板 | `SkillPrebuiltSynchronizer`（create_instance 时 sync） | - |
| symphony 检索配置（`SYMPHONY_SKILL_RETRIEVAL_ENABLED` 等） | `is_skill_retrieval_enabled` 门控（prompt rail + toolkit + auto_list 强制三联动） | 见 symphony |

### 关键路径

| 路径 | 用途 |
|---|---|
| `<workspace>/skills/` | 技能落盘目录（每技能一个子目录） |
| `<workspace>/skills/skills_state.json` | 安装账本（四桶） |
| `<workspace>/skills/_marketplace/` | git 市场仓库 clone |
| `<workspace>/.skill_sleep/call_counts.json` / `last_skills.json` | 休眠计数 / 任务归属续账 |
| 会话目录（`FileSkillGrantStore`） | Grant + 审批上下文 + active_skill 状态 |
| `~/.jiuwenswarm/agent/jiuwenclaw_workspace/skilldev/{task_id}/` | skilldev 任务工作区（未接线） |

## 测试

pytest.ini 的 addopts 含全仓 coverage，日常跑请清掉（`-o addopts=""`）。本模块测试集中在 `tests/unit_tests/agentserver/`（历史路径；skill_turbo 在 `tests/unit_tests/server/runtime/skill_turbo/`，勿混）：

```powershell
# 模块单测（按文件挑选：manager / rails / toolkits / prebuilt / grant store）
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/ -k "skill" -o addopts=""

# 重点单文件
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/test_skill_manager_path_safety.py tests/unit_tests/agentserver/test_skill_credential_injection_rail.py tests/unit_tests/agentserver/test_skill_active_state.py tests/unit_tests/agentserver/test_skill_toolkits.py -o addopts=""

# 企业账本锁 / 预置
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py tests/unit_tests/agentserver/test_skill_prebuilt_guards.py tests/unit_tests/agentserver/test_skill_prebuilt_parse.py -o addopts=""
```

- **改 SKILL_GATE / 执行位判定**：必须覆盖跨技能拦截、多技能拆分、逃生口停注、引号内分隔符不拆、`2>&1` 不拆、解释器 flag 跳过。
- **改账本 / 身份匹配**：跑 enterprise_state + clawhub_owner_handle；重名场景（ClawHub slug）按 origin 不按 name。
- **改授权链**：跑 `test_file_skill_grant_store.py` + `test_subagent_skill_authorization_config.py`；核对 core 骨架 / swarm 子类 / subagent 三层钩子语义不漂移。
- **修复类改动必须附能复现原故障的测试**（先红后绿）。

## 代码风格

- 类型注解：PEP 585 内置泛型（`list[X]`、`dict[K, V]`）+ PEP 604 联合类型（`X | None`）；禁止从 `typing` 导入已被内置语法替代的别名。
- 注释 / docstring：关键机制处写"为什么"而非"是什么"（skill_active_state 的过期兜底注释、credential rail 的漏拦面声明、skill_manager 构造期的登记顺序注释都是契约的一部分，清理时勿顺手删除）。
- 日志禁止 `print`；渠道错误码（`source_misconfigured` / `artifact_checksum_mismatch` 等）是对外契约，重命名 = 破坏性变更（[[S_03]] N5）。

## 设计文档归档与双向同步（本模块强制）

本模块设计文档**全部**落在 `docs/` 下，命名与结构规约见 [`docs/AGENTS.md`](docs/AGENTS.md)。把 **`server/runtime/skill/` 代码 + `agents/harness/common/` 下本模块管辖的 9 个文件 + `docs/specs/` + 本 AGENTS.md** 当作一个一致性单元维护——技能链跨层接缝多（账本 ↔ 目录 ↔ rail 状态 ↔ 工具视图），"只改部分执行路径"是缺陷的头号来源。

**前置铁律——文档先是设计的输入，才是提交的产出。** 动手改任何代码**之前**，先 grep + 读相关 `docs/specs/S_NN_*`（现有契约 / 不变量 / 修改检查单），拿它们校准方案；方案定了**当场**同步修订对应 spec。先码后档、拖到提交前才补文档是把流程做反了。

三条强制约束，提交时必查：

1. **特性更新归档 feature 且三连续提交**：`docs/features/` 新增 `F_NN_<slug>.md`；特性代码（`feat(skill)`）、测试（`test(skill)`）、文档（`docs(skill)`）拆成三个紧邻提交、同一 PR 落地。bugfix 触及 spec 描述的路径 / 不变量时，同步修订对应 spec 并归档 `docs/bugfix/B_NN_<slug>.md`，代码、测试、文档合并为**一个** `fix(skill)` 提交（三连续提交的唯一例外，与 skill_turbo 模块约定一致）。
2. **规约变动必更新 specs**：账本结构 / 渠道错误码 / rail priority / 工具参数 / 装配顺序变化时，同步修订对应 `S_NN`（元信息"最近一次修订日期"填当天）；新规约主题 = 新 spec 文件。
3. **读到不一致当场修文档**：本文件 / specs 里与代码不符的接口名、枚举值、路径、点名数量的结构表述（如"SKILL_GATE 三分支"、"四桶账本"、"7 个 rail"、priority 表），先 grep 代码、以代码为准刷新文档，同一次改动落地；不把过时表述当新约束执行。**纯计数类数字（路由条数、方法数、文件行数）不写入文档**——它们随日常增减高频漂移且不承载设计语义。

## 提交约定

本模块改动的 commit message scope 固定用 `skill`（如 `feat(skill): add source provider cache`）——含 `agents/harness/common/` 下本模块管辖文件的改动。

footer 用 `Refs: #<issue>` 格式关联 issue。issue 号若无法从当前上下文明确，必须先询问用户，不要臆造或留空。

涉及 `docs/features/F_*` / `docs/specs/S_*` 文档更新的特性改动，**特性代码、测试代码、文档拆成三个连续提交**（提交 1 特性代码 `feat(skill)`，提交 2 单测 `test(skill)`，提交 3 文档 `docs(skill)`）；归档 `docs/bugfix/B_*` 的缺陷修复例外，代码、测试、文档合并为一个 `fix(skill)` 提交。细则见上文「设计文档归档与双向同步」与 [`docs/AGENTS.md`](docs/AGENTS.md)。
