# Agent 侧技能工具集

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_06 / agent-skill-tools |
| 关联模块 | jiuwenswarm/agents/harness/common/tools/{skill_toolkits.py, skill_retrieval_toolkits.py} + symphony/agent/agentic_retrieval_toolkit.py |
| 最近一次修订日期 | 2026-10-16 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

面向**模型**的两套技能工具：`SkillToolkit`（市场管理三工具：搜索 / 安装 / 卸载）与 `SkillRetrievalToolkit`（已安装技能的树检索三工具）。**注意与 core 侧工具的区分**：`skill_tool`（SKILL.md 内容查看）与 `list_skill`（清单 / 路由）在 agent-core `harness/tools/skills/`，不经本模块——本 spec 只在协作边界描述它们。渠道 RPC 契约见 [[S_03]]，账本见 [[S_02]]，rails 装配见 [[S_05]]。

## SkillToolkit（skill_toolkits.py）

**定位**：把 `SkillManager` 封装成模型友好的技能市场工具。构造 `(manager, *, service_id, agent_id, on_installed_skills_changed)`；`_is_enterprise()` = `is_skill_prebuilt_tenant(agent_id, service_id)`（[[S_04]]）；`on_installed_skills_changed` 回调在企业版安装 / 卸载后触发 `refresh_enabled_skills_from_db` 刷新 SkillUseRail。工具经 `ToolCard` + `LocalFunction` 注册（非 `@tool` 装饰器）。

### 三工具契约

| 工具 | 参数 | 行为要点 |
|---|---|---|
| `search_skill` | `query`（必填）/ `source` / `limit`（默认 10） | `source=auto` 搜全部渠道；skillnet 走 `handle_skills_skillnet_search`（**`params["mode"]="vector"`**——向量模式对多关键词召回更稳）；clawhub / teamskillshub 走各自 search；builtin 走 `_search_builtin_skills`（本地目录 frontmatter 解析 + 子串匹配）。结果经 `_normalize_search_item` 归一统一字段（name / description / source / identifier / installed / version / author / score——skillnet 的 score 取 stars）。企业版剔除 builtin 渠道（`error_code="builtin_not_available"`） |
| `install_skill` | `identifier`（SkillNet URL / ClawHub slug 或 owner/slug / TeamSkillsHub asset_id / builtin 技能名）/ `source`（**必填，不允许 auto**）/ `timeout_sec`（默认 60） | 先 `_check_already_installed`（`_find_installed_by_target` 按 origin / name / slug 多形态反查；**ClawHub 禁止按展示名匹配**防同名误判）；skillnet 走 `_install_skillnet_sync_wait`（发起后**单次 tool 调用内 0.5s 间隔轮询 install_status 至完成或超时**——把异步安装折叠成同步工具语义）；企业版成功后 `record_skill_installation(source_type="user")` 记账，`SkillNameConflictError` 或状态写失败**回滚删除技能目录** |
| `uninstall_skill` | `name` | 内置技能（真正运行于 builtin 目录，`is_builtin_skill`）不可卸；企业版仅 `source_type ∈ {prebuilt, user}` 可操作且 **prebuilt 不可卸**（`prebuilt_not_removable`）；匹配按内部名 / 展示名大小写不敏感，**实际卸载用磁盘规范名**（防展示名大小写删不到目录） |

`source` 枚举：非企业版 `auto / skillnet / clawhub / teamskillshub / builtin`（默认 skillnet）；企业版无 `builtin`。

### 衍生类

| 类 | 位置 | 差异 |
|---|---|---|
| `SlimSkillToolkit` | agents/harness/flash/tools/slim_skill_toolkit.py | flash 会话轻量版：安装 / 卸载折叠进 `search_skill` 的交互流 |
| `MemberSkillToolkitRail` | agents/harness/team/rails/team_member_skill_toolkit_rail.py | 团队技能工作区：按 agent 注册 / 注销 `SkillToolkit`，install / uninstall 成功后刷新技能链接视图 |

### 装配点

interface_deep（共享工具注册段——进程级索引是公共目录，不按会话切分视图）/ interface_flash（`SlimSkillToolkit` 轻量变体）/ interface_code（`_get_tool_cards` 统一注册）/ swarm `providers/tools.py`（swarm 成员）/ skill_turbo `tools_loader.py`（复用 SkillToolkit）。

## SkillRetrievalToolkit（skill_retrieval_toolkits.py）

**定位**：大规模已安装技能场景下的 **agentic 树检索**（装后检索，与 `search_skill` 的装前市场发现正交）。构造 `(manager, *, visible_skill_names)`；真实现位于 `symphony/agent/agentic_retrieval_toolkit.py`，本文件只是 `asyncio.to_thread` 薄包装。

### 三工具契约

| 工具 | 参数 | 行为 |
|---|---|---|
| `skill_index_build` | 无 | 构建 / 复用已安装技能的 symphony 检索索引（`build_skill_index_blocking(force=False, source="tool")`）。**描述明确要求不要主动调用**——先试 explore / peek，仅当结果说索引缺失 / 过期时调一次 |
| `skill_branch_explore` | `node_ids: array[string]`（必填） | **主浏览工具**：对给定分支节点披露下一层可见边界（分支 + 叶子技能条目）；有 `visible_skill_names` 时走 `skill_branch_explore_for_visible_skills` 做可见性过滤；`ROOT` 作为输入被拒（系统提示已含第一层分类）；叶子技能 id 误作 branch id 被拒并给指引 |
| `skill_branch_peek` | `node_ids: array[string]`（必填） | 轻量预览：子分支摘要 + 覆盖统计（多少 branch / skill），不返回完整叶子详情 |

### 检索机制（symphony 层要点）

**这不是相似度检索，没有相似度阈值**——是 **agent 驱动的树状渐进披露**（progressive disclosure）：模块 docstring 明言 "It does not call an LLM; the calling agent decides which tree branches to inspect next"。

- **数据源**：磁盘 symphony 技能索引（`_INDEX_FILENAMES = ("tree_index.yaml", "catalog.jsonl", "manifest.json")`），由 `SkillIndexService(manager).status()` 检查存在性与新鲜度（installed skills / build 设置变化 → stale）。
- **就绪判定**：未启用 → 禁用文案；索引缺失 → missing；过期 → stale——均返回"先调 skill_index_build 再重试"指引；就绪状态 30s TTL 缓存。
- **配置**：`RetrieveSettings`（top_k / max_branch_choices / max_parallel_branches / max_exposure_depth / flatten_tree 等）来自 `symphony/skill_retrieval/config`，受 `SYMPHONY_SKILL_RETRIEVAL_ENABLED/ROOT` 等环境变量影响。
- **可见性过滤**：`visible_skill_names`（None = 全可见）在叶子 / 分支多标识（payload / worker_id / name / item_id / label / canonical_id）上集合匹配；空分支（skill_count=0）整体隐藏。
- **缓存**：toolkit 按（index_dir 路径、索引产物签名、retrieve 设置签名、可见集签名）缓存单例。
- **结果形态**：`AgenticToolResult(payload, detailed_output=…)`——模型看 `success/result` 文本，结构化 `skill_tree` 载荷进 detailed 通道。

### 与 rails 的联动（见 [[S_05]]）

启用检索时：`SkillRetrievalPromptRail` 注入 `skill_retrieval` 段 + 单窗口隐藏 list_skill / skills 段；`_resolve_skill_mode` 强制 SkillUseRail 进 `auto_list`；热重载经 `_sync_skill_retrieval_tools_for_runtime` 与 prompt rail 成对开关。装配点：interface_deep（声明为 **shared 工具**——进程级索引是公共目录，不按会话切分视图）/ code / swarm providers。

## 协作边界：core 侧工具（不归本模块，装配链路相关）

| 工具 | 位置（agent-core） | 与本模块的接点 |
|---|---|---|
| `skill_tool` | harness/tools/skills/skill_tool.py | 只允许读 `SKILL.md`（含嵌套 `subskill/SKILL.md`；路径安全拒绝对路径 / `..` 穿越）；加载成功 `set_current_skill_name`——该信号被 [[S_05]] 的 ActiveStateRail / SleepRail 消费；tool_msg metadata 的 `skill_name` / `is_directory_listing` 是激活判定的依据 |
| `list_skill` | harness/tools/skills/list_skill.py | 仅 `skill_mode=auto_list` 时由 SkillUseRail 注册；检索启用时被 [[S_05]] 单窗口隐藏 |

## 不变量

- **N1（工具层零业务持久化）**：SkillToolkit / SkillRetrievalToolkit 不持有安装状态——全部技能 IO 走 `SkillManager.handle_skills_*` / 同步查询方法；企业账本逻辑抽到 `installed_skill_ops.py` 与 Web RPC 共用。
- **N2（同步等待不超时泄漏）**：skillnet 同步等待的轮询必须在 `timeout_sec` 内收敛（完成 / 失败 / 超时三态都有明确返回），不得无限轮询。
- **N3（企业回滚）**：企业版安装的"记账失败 → 删目录"回滚顺序不可倒置（先记账后删盘会留下幽灵目录；先删盘后记账会留下幽灵记录）。
- **N4（可见性单一来源）**：`visible_skill_names` 与 [[S_05]] N8 同源——工具过滤与 prompt 注入必须看到同一技能视图。
- **N5（索引构建惰性）**：`skill_index_build` 的工具描述承诺"不要主动调用"；改描述 / 行为时保持这一契约（否则每次会话都全量重建索引）。

## 修改检查单

- 改 `search_skill` 结果字段 → `_normalize_search_item` 是唯一归一点；新增渠道字段先在这里登记。
- 改安装去重逻辑 → `_find_installed_by_target` 与 [[S_02]] 的身份匹配规则保持一致（ClawHub 按 slug、不按展示名）。
- 改企业版安装 / 卸载 → 核对 N3 回滚 + `on_installed_skills_changed` 刷新链 + prebuilt 保护三件事。
- 改检索工具 → 索引文件名 / 就绪判定 / 缓存签名任一变化都会使旧缓存失效——核对 `_TOOLKIT_CACHE` 签名维度。
- 改工具描述文本 → 描述是**行为契约**（如 skill_index_build 的"不要主动调用"）；改文案 = 改行为。

## 与其它 spec 的关系

- 渠道 RPC 与安全断言见 [[S_03]]；账本与身份见 [[S_02]]；企业预置与租户判定见 [[S_04]]；rails 联动见 [[S_05]]。
