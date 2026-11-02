# skill 模块总览与包边界

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_01 / overview-and-boundaries |
| 关联模块 | jiuwenswarm/server/runtime/skill/ + jiuwenswarm/agents/harness/common/{rails,tools}/（skill 相关文件） |
| 最近一次修订日期 | 2026-10-16 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

### 本规约管什么

**技能管理模块（skill module）横跨 jiuwenswarm 仓库内两个代码位置**，本目录（`server/runtime/skill/`）是模块主体与文档归档点；agent 侧 rails/tools 以文件清单方式纳入管辖。本模块覆盖技能的**全生命周期管理**：

- **服务端管理**：安装 / 卸载 / 启停 / 账本（`skills_state.json`）/ 技能源注册（SourceRegistry SPI）/ 三个远程市场渠道（SkillNet / ClawHub / TeamSkillsHub）+ builtin + 本地导入 + 企业预置同步
- **Agent 运行时集成**：7 个 skill rails（活跃状态 / 凭据注入 / 授权门禁 / 执行规范 / 检索提示 / 休眠计数 / 子代理委托授权）+ 2 个工具集（市场管理三工具 `SkillToolkit`、树检索三工具 `SkillRetrievalToolkit`）
- **技能自演进流水线**（`skilldev/`，**当前未接线**，见 [[S_07]]）

### 本规约**不**管什么

| 不归本模块 | 归属 |
|---|---|
| Skill Turbo 加速执行引擎（PlanNode 节点树执行） | `server/runtime/skill_turbo/`（有独立 AGENTS.md + docs/specs S_01~S_09） |
| `skill_tool` / `list_skill` 内容查看工具原语、`SkillUseRail` 目录扫描与 skills 段注入、`SkillCreateRail` / `SkillEvolutionRail` / `TeamSkill*` 演进四件套、core 侧 `SkillAuthorizationRail` 裁决骨架 | **agent-core 仓** `openjiuwen/harness/rails/skills/` 与 `openjiuwen/harness/tools/skills/`（本模块文档不跨仓，仅在协作边界处描述接口） |
| 技能授权的 Grant / Manifest / 审批卡数据模型与裁决流程 | agent-core `openjiuwen/harness/security/skill_authorization/`（swarm 侧只做装配与存储适配，见 [[S_05]]） |
| symphony 检索索引构建 / 树披露算法本体 | `jiuwenswarm/symphony/`（本模块消费其 API，见 [[S_06]]） |
| 团队技能工作区（member 以目录链接共享技能） | `agents/swarm/providers/skills.py` + `agents/harness/team/`（消费本模块的 SkillManager） |

## 模块地图（跨目录管辖清单）

```
jiuwenswarm/server/runtime/skill/          ← 模块主体 + 文档归档点
├── __init__.py                # 门面：仅 re-export skilldev.state_utils 的启停账本纯函数
├── skill_manager.py           # SkillManager：skills.* RPC 路由全集（含 Skill Graph）+ 账本 + 六内置渠道 + Source SPI + git 市场 + 安全解压
├── source_registry.py         # SourceRegistry：技能源 Provider SPI 注册表（能力校验 / 懒启动 / 白名单合并）
├── artifact_security.py       # 产物完整性验证：SHA-256 摘要 + SkillHub HMAC-SHA256 签名（fail-closed）
├── enterprise_state_lock.py   # enterprise_skill_state_lock：企业版账本跨线程 + 跨 Pod 文件事务锁
├── workspace_provider.py      # SkillWorkspaceProvider：工作区 fail-closed 准备 + per-workspace manager 单例
├── skill_prebuilt.py          # SkillPrebuiltSynchronizer：企业预置技能模板硬同步
├── skilldev/                  # 技能自演进流水线（15 阶段状态机；未接线，见 [[S_07]]）
│   ├── state_utils.py         # skills_state.json 纯函数库（启停账本，被 SkillManager 反向引用）
│   └── ...                    # service / pipeline / schema / stages×11 / store / workspace / context / deps
└── docs/                      # 本设计文档体系（specs + designs + features + bugfix）

jiuwenswarm/agents/harness/common/rails/   ← agent 侧 rails（7 个文件，本模块管辖）
├── skill_active_state.py              # SkillActiveStateRail（priority=25）：会话级活跃技能状态
├── skill_credential_injection_rail.py # SkillCredentialInjectionRail（priority=5）：凭据注入 + SKILL_GATE
├── skill_authorization_rail.py        # SkillAuthorizationRail（core 子类，薄适配：文件化 Grant 存储）
├── skill_sleep_rail.py                # SkillSleepRail（priority=26）：使用计数 + 休眠训练触发
├── skill_protocol_prompt_rail.py      # SkillProtocolPromptRail（priority=8）：技能执行规范注入
├── skill_retrieval_prompt_rail.py     # SkillRetrievalPromptRail（priority=94 动态推导）：树检索提示
└── subagent_skill_authorization_rail.py # SubagentSkillAuthorizationRail：子代理委托授权

jiuwenswarm/agents/harness/common/tools/  ← agent 侧工具（2 个文件，本模块管辖）
├── skill_toolkits.py          # SkillToolkit：search_skill / install_skill / uninstall_skill 三工具
└── skill_retrieval_toolkits.py # SkillRetrievalToolkit：skill_index_build / branch_explore / branch_peek
```

## 不变量

- **N1（单一真相源）**：`SkillManager`（经 `skills_state.json` 账本 + skills 目录扫描）是 swarm 侧技能安装状态的事实唯一来源。core 侧 `SkillUseRail` 的 enabled/disabled 名单、动态授权的身份账本、`SkillToolkit` 工具、检索可见性过滤**全部回读它**——不要在 rail / tool 层另建安装状态副本。
- **N2（core 只认目录）**：agent-core 的技能原语（`SkillUseRail` / `SkillTool`）**只认文件系统上的 skills 目录**，不感知安装账本。swarm 侧对目录内容的每一次变更（安装 / 卸载 / 预置同步 / builtin 清理）都必须同步落账，并触发 `SkillUseRail` 刷新或 Agent 重载。
- **N3（fail-closed 三处）**：① `SkillWorkspaceProvider.ensure` 不能安全承载技能状态时抛 `SkillWorkspaceUnavailable`，**无 fallback 路由**；② 下载 URL 不在白名单（管理面显式配置 > Provider 声明默认 > 空）即拒绝下载；③ `verify_skillhub_artifact` 任一校验失败抛 `ArtifactVerificationError` 拒绝安装。
- **N4（文档跨目录不跨仓）**：本模块文档归档在 `server/runtime/skill/docs/`，但管辖范围包含 `agents/harness/common/` 下的 skill 文件；agent-core 侧技能代码**不建文档**，只在本体系内以协作边界方式描述。
- **N5（rail 状态即文件）**：活跃技能状态与授权 Grant 共用会话文件（`FileSkillGrantStore`，portalocker + 原子替换），无进程内授权缓存；存储不可用时抛 `AbortError` 中止本轮，**不带旧状态继续跑**。

## 接口契约

### 包门面（`server/runtime/skill/__init__.py`）

仅导出 `skilldev.state_utils` 的**启停账本纯函数**（供 harness / team / evolution 各处避免对 SkillManager 的循环依赖，名单以 `__all__` 为准）：

`filter_visible_skill_names` / `get_registered_skill_names` / `get_skill_enabled` / `get_state_file` / `list_disabled_skills` / `list_execution_disabled_skills` / `load_execution_disabled_skills` / `normalize_local_skills` / `normalize_skill_configs` / `set_skill_enabled`

**不导出 SkillManager / SourceRegistry / SkillPrebuiltSynchronizer**——一律走子模块全路径导入（窄门面约定）。

### SkillManager（唯一公开服务类）

| 面 | 入口 | 说明 |
|---|---|---|
| RPC 处理器 | skills.* RPC 路由全集（server/runtime/agent_adapter/interface.py `_SKILL_ROUTES`）→ `handle_skills_*` 处理器（企业镜像路由 `SKILLS_ENTERPRISE_SOURCE_*` 与非企业路由共享 handler；`handle_skills_install_builtin` 为内部调用不经路由）+ `handle_plugins_*`（`_PLUGIN_ROUTES`） | 见 [[S_02]] 路由表 |
| 安装渠道 | `install_skill_sync` / `install_prebuilt_from_provider` / `_import_local_from_path` / `_commit_web_skill_install` / `_commit_source_skill_entity` | 见 [[S_03]] |
| 同步查询 | `get_local_skills` / `get_installed_plugins` / `list_skill_installations` / `get_skill_meta` / `is_builtin_skill` / `list_enabled_skill_names` / `find_skill_dir_by_identity` | 见 [[S_02]] |
| 启停 | `get_skill_enabled` / `set_skill_enabled` / `remove_skill_config` / `list_disabled_skills` / `list_execution_disabled_skills` | 落 `skills_state.json` 的 `skill_configs` |
| 实例来源 | `SkillWorkspaceProvider.get_or_create_manager(workspace_dir)` | per-workspace 单例（WeakValueDictionary 缓存），**不要直接 `SkillManager()` 拿生产实例**（单测除外） |

### Agent 侧 rails / tools 公开符号

| 符号 | 文件 | 消费方 |
|---|---|---|
| `SkillActiveStateRail` + `get_session_active_skill` / `resolve_skill_session_id` / `clear_session_skill_state` | rails/skill_active_state.py | interface_deep 装配；凭据注入 rail / sleep rail 读取 |
| `SkillCredentialInjectionRail` | rails/skill_credential_injection_rail.py | interface_deep（`react.skill_envs`）|
| `SkillAuthorizationRail`（swarm 子类） | rails/skill_authorization_rail.py | interface_deep（复用 permission rail 的 engine） |
| `SubagentSkillAuthorizationRail` | rails/subagent_skill_authorization_rail.py | 装配方 `build_subagent_authorization_rails` / `install_subagent_authorization_wiring`（定义于 tools/subagent_executor/authorization.py） |
| `SkillSleepRail`（+ `SkillCallCounter` / `SkillSleepRunner` 于 skill_sleep/ 子目录） | rails/skill_sleep_rail.py | interface_deep |
| `SkillProtocolPromptRail` | rails/skill_protocol_prompt_rail.py | interface_deep（仅 agent 模式） |
| `SkillRetrievalPromptRail` | rails/skill_retrieval_prompt_rail.py | interface_deep / swarm providers |
| `SkillToolkit`（+ `SlimSkillToolkit` 在 harness/flash/tools/） | tools/skill_toolkits.py | interface_deep / flash / code / swarm / team |
| `SkillRetrievalToolkit` + `is_skill_retrieval_enabled` | tools/skill_retrieval_toolkits.py | interface_deep / code / swarm |

## 执行路径 / 装配关系

### P1：SkillManager 实例化链（谁创建、注入到哪）

```
JiuWenSwarm.__init__（server/runtime/agent_adapter/interface.py）
  / AgentManager（agent_manager.py，双检锁 per 租户 workspace）
    → SkillWorkspaceProvider.ensure(workspace_dir, require_valid_state=企业版)
    → SkillWorkspaceProvider.get_or_create_manager(workspace_dir)
        → SkillManager(workspace_dir, persist_skills_state=..., service_id, agent_id)
    → set_skill_manager(manager)  → 注入 interface_deep / interface_flash / interface_code 各 adapter
```

### P2：skills.* 请求路由（server/runtime/agent_adapter/interface.py）

```
AgentRequest(req_method=skills.*)
  → server/runtime/agent_adapter/interface.py `_SKILL_ROUTES`（按域分组）→ getattr(self._skill_manager, handler_name)
  → `SKILLS_EVOLUTION_ARCHIVES / ROLLBACK / REBUILD` → DeepAdapter（evolution rail 体系，不经 SkillManager）
  → skilldev.*（`_SKILLDEV_METHODS`，当前空集合）→ SkillDevService（未接线，见 [[S_07]]）
```

### P3：agent 侧装配（interface_deep.py，详见 [[S_05]]）

rails 经 `_RailBuildInfo` 列表 + `_instantiate_rails` 构建；flash 模式走 `_FLASH_RAIL_KEEP` 白名单保留技能链（credential / active_state / authorization）；code 模式注册 `SkillToolkit`；swarm 成员经 `agents/swarm/providers/`（harness_element：`swarm.member_skill_toolkit` / `swarm.skill_retrieval_prompt` / `swarm.skill_retrieval`）。

## 修改检查单

- 改 `__init__.py` 导出面 → 同步核对所有 `from jiuwenswarm.server.runtime.skill import ...` 消费点（grep 确认，当前这批函数被 swarm providers / evolution / symphony extension 等消费）。
- 新增 skills.* RPC → 三处同步：`ReqMethod` 枚举（common/schema/message.py）+ `_SKILL_ROUTES` 路由表 + `SkillManager.handle_skills_*`；企业版专属的走 `SKILLS_ENTERPRISE_*` 前缀。
- 新增 skill rail → 先读 [[S_05]] 装配顺序与 priority 约定；挂载点在 interface_deep 的 `rail_infos` 列表，热重载需实现原位更新（见 [[S_05]] N6）。
- 改 skills 目录布局 / 账本结构 → 同步 [[S_02]] 与 `state_utils.py` 的对账函数。

## 与其它 spec 的关系

- [[S_02]]：SkillManager 核心（路径布局 / 账本 / 安装卸载 / 事务锁）
- [[S_03]]：技能源体系（SourceRegistry SPI / 市场渠道 / 产物验证 / 下载白名单 / 安全解压）
- [[S_04]]：企业预置同步与多租户
- [[S_05]]：agent 侧 7 rails（运行时行为契约）
- [[S_06]]：agent 侧 2 工具集（模型可见的工具契约）
- [[S_07]]：skilldev 流水线（未接线）
- 技能加速执行（turbo）见 `server/runtime/skill_turbo/docs/`，与本体系正交。
