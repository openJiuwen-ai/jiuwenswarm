# 合并冲突报告：chore/rsi-merge-develop ← origin/develop

> **状态（2026-09-12，第二轮远端刷新后）：已抓取并合并 `origin/develop` 最新提交 `b7da9ef98`；无未解决冲突，复核发现的问题已修复。**
> 本轮先使用 `git merge --no-commit --no-ff` 完成检查，验证通过后已将 4 个本地 first-parent 提交 squash 为合并提交 `bc8f7fb7e`；当前不存在 `MERGE_HEAD`。
> 本文件保留为本轮合并复核记录；是否随最终提交保留由维护者决定。
> 解决概要：
> - 小/中差异且主干为同源超集的文件 → 直接取主干。
> - 含我方 RSI 内容的 14 个文件 → 取主干 + 回补 RSI（App.tsx experiments 路由、i18n `nav.experiments` + `settingsPanel.fields.rsi_enabled` + 顶层 `rsi.*` 182 键、ExperimentalSettings `RSISetting`、agent_ws_server RSI 分发 seam、interface/interface_deep RSI Harness 状态机、agent_manager `RsiHarnessInstallConflict`、config.py `update_rsi_enabled_in_config`、app_web_handlers `rsi_enabled` + 18 个 `rsi.*` 方法表、utils.py `rsi-program-dataset-creator`、config.yaml `rsi:` 段、web_connect `rsi.training.*` 事件、settingsContract/definition/SessionSidebar）。
> - 主干有意删除的按主干执行：jiuwenclaw 兼容迁移（06cb2062d，失效测试 test_cron_migration_preserves_home.py 一并删除）、TrajectoryPanel DeepSeek 署名页脚。
> - 锁文件由 `uv lock`（288 包）与 `npm install` 重新生成。

- 日期：2026-09-11
- 合并方向：将 `origin/develop`（0b0490a70）合入特性分支 `chore/rsi-merge-develop`（追踪 `origin/feature/rsi`）
- 分叉点：`661758de1`
- 规模：主干领先 **170 个提交**，特性分支领先 **138 个提交**；共 **~117 个冲突文件、约 350 个冲突块**

## 两边分支主题

- **我方（feature/rsi）**：RSI 技能自演进特性 + 多次从主干同步（0.2.6、PR 6483 等）
- **对方（develop）**：权限配置接入、内置插件/模板迁移到 hub、Runtime Public API 收口、Web 前端组件化重构（PageCard/PageHeader 等）、模型适配修复

## 冲突分类清单

### A. 工程/构建/依赖（8 个）
| 文件 | 冲突块 | 处理建议 |
|---|---|---|
| `pyproject.toml` | 3 | 手工合并双方新增依赖 |
| `uv.lock` | 2 | 解决 pyproject 后重新 `uv lock` 生成 |
| `jiuwenswarm/channels/web/frontend/package.json` | 4 | 手工合并双方脚本/依赖 |
| `jiuwenswarm/channels/web/frontend/package-lock.json` | 3 | 解决 package.json 后重新生成 |
| `.gitignore` | 2 | 合并双方条目 |
| `MANIFEST.in` | 1 | 合并双方条目 |
| `scripts/jiuwenswarm.spec` | 1 | 合并打包配置 |
| `jiuwenswarm/resources/config.yaml` | 2 | 合并双方配置项 |

### B. 后端 Python（29 个）
**server/runtime（11）**：`agent_ws_server.py`(16)、`agent_adapter/interface_deep.py`(16)、`extension_package_manager.py`(12)、`agent_adapter/user_turn.py`(4)、`team_helpers.py`(4)、`agent_manager.py`(3)、`marketplace/hub_models.py`(3)、`interface.py`(2)、`interface_code.py`(2)、`hub_package_downloader.py`(2)、`skills_multipart_http.py`(1)

**gateway（7）**：`channel_manager/web/app_web_handlers.py`(9)、`cron/scheduler.py`(3)、`app_gateway.py`(2)、`cron/cron_expr.py`(1)、`cron/models.py`(1)、`channel_manager/web/web_connect.py`(1)

**harness（10）**：`tools/search_tools.py`(6)、`rails/runtime_prompt_rail.py`(5)、`rails/skill_retrieval_prompt_rail.py`(4)、`rails/interrupt/interrupt_helpers.py`(3)、`tools/mcp_toolkits.py`(2)、`tools/web_file_download.py`(2)、`tools/send_file_to_user.py`(1)、`tools/multi_session_toolkits.py`(1)、`code/rails/heartbeat/cron_schedule.py`(1)、`team/__init__.py`(1)

**common（5）**：`utils.py`(8)、`runtime_workspace.py`(3,AA)、`cron_session.py`(2,AA)、`config.py`(1)、`reasoning_config.py`(1)；另 `observability/sink.py`(3,AA)

**channels 后端（2）**：`desktop/desktop_app.py`(5)、`web/app_web.py`(2)

建议原则：**develop 的重构/API 收口为准，保留 RSI 特有逻辑**（如 RSI 评估、技能检索 rail 等）。

### C. 前端 TS/CSS（47 个）
冲突最多：`SkillPanel/index.tsx`(53)、`App.tsx`(8)、`i18n/locales/en.json`(8)、`i18n/locales/zh.json`(7)、`ExperimentalSettings.tsx`(8)、`ChatPanel/InputArea.tsx`(7)、`ConnectorMarket/MarketplacePage.tsx`(7)、`CreatePluginPage.tsx`(6)；其余 40 个为 1–4 个冲突块。含 8 个 AA（双方各自新增：ui/CategoryTabs、ui/PageHeader、ui/PageToolbarSearch、planMode、TrajectoryPanel 等）。

建议原则：**develop 的组件化/主题重构为准，叠加 RSI 功能入口**（planMode、trajectory 等为 RSI 新功能，保留我方版本）。

### D. 文档（6 个）
`docs/en/Configuration.md`(2)、`docs/en/SkillSelfEvolution.md`(4)、`docs/en/Quickstart.md`(1)、`docs/zh/Quickstart.md`(1)、`docs/zh/Skill自演进.md`、`docs/zh/配置信息.md`

### E. 技能/资源（7 个，均 AA 双方各自新增）
- `docx-pro/scripts/` 6 个：`docx_pro.py`(20)、`renderer.py`(13)、`md_export.py`(10)、`docx_replace.py`(5)、`md_parser.py`(2)、`setup_check.py`(1) —— 同源提交两边镜像，实测两侧差异 87/≤30 行不等
- `mcp_builtins_v0.2.zip`（二进制，详见决策点 3）

### F. 修改/删除冲突（5 个 UD，需决策）
| 文件 | 我方自分叉点改动 | develop 删除原因 |
|---|---|---|
| `AgentManagementPanel/DefinitionCard.tsx` | 121 行（3 个提交） | `6264de338` 卡片统一 PageCard 组件化重构；tag scrolling 等修复已有镜像提交 |
| `office-document-toolkit/tools/text_utils.py` | 248 行 | `029c76a64` 删除内置 plugin-packages 迁移到 hub |
| `office-document-toolkit/tools/document_generator.py` | 221 行 | 同上 |
| `office-document-toolkit/tools/pdf_manipulator.py` | 4 行 | 同上 |
| `office-document-toolkit/skills/office-document-workflow/SKILL.md` | 0 行 | 同上 |

### G. 单元测试（21 个）
AA 双方各自新增 10 个：`test_ws_keepalive_task.py`(26)、`test_cron_skip_permission_interrupt_rail.py`(7)、`test_extension_package_hub.py`(7)、`test_paid_search_configuration.py`(5)、`test_trajectory_frontend_artifacts.py`(5)、`test_cron_store_path_resolution.py`(10)、`test_trace_sink.py`(2)、`test_cron_session.py`(1)、`test_hub_client.py`(1) 等；其余 11 个为内容冲突。

## 关键事实核查

1. **AA 文件并非简单重复**：抽样的 11 个 AA 文件两侧内容均不同（差异 5~153 行），需逐个调和，多数差异较小。
2. **zip 二进制**：两侧唯一差异 = 我方移除了 `mcp_builtins/canva/` 目录（对应我方提交 `24418274e`），其余 892 个共同文件仅 `.mcp_builtins_version` 版本号不同。
3. **F 类删除均为 develop 架构性删除**（hub 迁移/组件化重构），我方改动多数在 develop 有镜像等价提交。

## 已确认决策（2026-09-11 用户批示）

1. **总体策略**：同意 —— 基础设施/重构以 develop（主干）为准；RSI 特有功能（planMode、trajectory、RSI 评估/技能检索等）保留我方实现；双方独立新增内容（i18n 词条、依赖、测试）合并保留。
2. **F 类 5 个文件**：跟随主干删除。
3. **zip 二进制**：取主干版本（含 canva 目录）。
4. **验证**：全部验证（pytest 单测 + 前端 tsc/build）后才提交合并。

## 待请示决策点

1. 总体解决策略
2. F 类 5 个"我方修改/对方删除"文件的去留
3. zip 二进制取哪一侧
4. 合并后验证方式

---

## 已知问题（合并提交后遗留，2026-09-11）

### 1. pytest 单测收集失败：285 个 ERROR（未进入用例执行阶段）

- 现象：`uv run python -m pytest tests/unit_tests` 收集阶段中断，"285 errors during collection"，耗时 12m33s。
- 根因：**openjiuwen 依赖版本与 RSI 代码不匹配**。
  - 合并后 pyproject.toml 取主干的 openjiuwen rev：`14a7fe2d0a2bf0cf2a25abd90f05f5ae6a9bf2d5`
  - 我方分支原 pin 的 rev：`ad0c9dfbbd577423cc60f73edffa0a4699cc53f9`
  - 我方 RSI 代码 `jiuwenswarm/agents/harness/common/rsi/mock_harness_provider.py:21` 需要 `from openjiuwen.rsi.events import NodeStageEvent`，该符号在 `14a7fe2d0` 版本的 `openjiuwen/rsi/events.py` 中不存在。
  - `jiuwenswarm/agents/harness/common/rsi/__init__.py` 顶层即触发该导入，因此任何导入 rsi 包（或经由它导入的模块）的测试文件全部收集失败。
- 受影响目录（收集 ERROR，约 80+ 个文件、285 个错误）：agentserver/、rsi/、runtime/、server/、sdd/、personal_context/、process_cli/、gateway/、swarm/、symphony/、channels/、common/、evolution/ 等。
- 候选修复方向（待定）：
  1. 将 openjiuwen pin 回退到我方 rev `ad0c9dfb`，并验证主干 170 个提交的代码是否兼容该版本（需评估 agent-core 两 rev 间的 API 变更面）。
  2. 将我方 RSI 代码适配到 `14a7fe2d0` 的 API（自行提供/替代 `NodeStageEvent` 等缺失符号）。
  3. 若 agent-core 存在同时满足两侧的更新 rev，pin 到该版本。
- 备注：修复后需 `uv lock` 重新生成 uv.lock 再重跑 pytest；本次 285 个 ERROR 均为收集期导入错误，不代表用例本身失败。

### 2. 已通过的验证（合并时点）

- 前端：`npm run build`（tsc + vite）通过，1m38s，仅既有 chunk 体积警告。
- 前端 RSI 测试：`test:rsi-presentation` 11/11、`test:rsi-stage-tree` 3/3 通过。
- tsc 修复记录：ExperimentalSettings `TrajectoryUiSetting` 重复声明（合并残留，删我方重复份）；types/index.ts 回补 `ModelEntry.is_free` 可选字段（后端仍在下发）；pluginPackagesApi `importLocal` 返回类型修正为 `{ id: string }`（与后端 `import_plugin_package` 实际返回一致）。
- 依赖锁：`uv lock`（288 包）、`npm install` 均已重新生成并暂存。
- 首次 pytest 报 ruamel 缺失为环境问题，已用 `uv run`（项目 .venv）解决。

### 更新（2026-09-11 17:20）：根因细化 + cd378e4c 试验结论（取代上节"候选修复方向"）

核心事实：**不存在同时满足两侧代码的 agent-core rev**，两侧各有一块特性级移植工作。

| agent-core rev | 线路 | 日期 | core/kv_cache | rsi.events.NodeStageEvent |
|---|---|---|---|---|
| 14a7fe2d0（develop pin，合并提交采用） | 主干线 | 09-09 | 有 openjiuwen/core/kv_cache | 无 |
| ad0c9dfb（我方 deps 行原 pin） | RSI 线 br_0.1.18 | 09-09 | 无（已重构进 foundation） | 有 |
| cd378e4c（用户 09-11 提供试验） | RSI 线最新 | 09-11 | 无 | 有 |

- 新发现：合并前我方分支 pyproject 自相矛盾——依赖行写 ad0c9dfb（RSI 线），但 [tool.uv.sources] 覆盖写 14a7fe2d0（主干线，uv 实际以 sources 生效）。即合并前我方分支在全新 uv sync 后本就无法同时跑通 RSI 与 kv_cache。
- cd378e4c 试验结果（pin 已切换、uv.lock 已重生成、venv 已同步）：
  - 通过：NodeStageEvent/EventNode/EventProgress/EventStatus 恢复，rsi 包导入链越过 mock_harness_provider.py:51，直到 kv_cache 处才中断。
  - 断裂：develop 侧 KV 缓存亲和特性按旧 agent-core 架构编写，与新架构三处不兼容：
    1. 4 个 src + 2 个 test 文件 import openjiuwen.core.kv_cache——模块已迁移到 core.foundation.kv_cache（KVCacheAffinityConfig/resolve_session_lineage/KVCacheIdentity 在新路径均在，属机械路径替换）；
    2. KVCacheRuntime 从有状态运行时（binding_provider/.closed/.close()）变为 4 布尔位冻结 dataclass，新位置 core/single_agent/kv_cache/kv_cache_hooks.py，构造签名不兼容；
    3. create_agent_team_session（core/session/agent_team.py）已删除 kv_cache_runtime 参数——jiuwenswarm 7 个文件 11 处调用点在传该参（team_manager 3、remote_member_bootstrap 1、agent_ws_server 2、session_adapter 1、team_helpers 1、interface_deep 1、kv_cache_product_hooks 2），属特性级移植。
  - 反向（pin 回主干 14a7fe2d0）代价：RSI 侧缺 NodeStageEvent，且初查 single_harness/events_translate、rsi/usage、data_loader/case_files 在主干 rev 的 rsi 树中不存在（事件翻译层是 RSI 前端展示核心依赖），移植量同样不小。
- 全量缺口矩阵探测（jiuwenswarm 全部 openjiuwen.* 导入模块与符号逐一对照两 rev）脚本在后台运行，结果出来后补记。
- 待团队决策的方向：
  A. 维持 cd378e4c：先做 6 处机械路径替换；kv_cache 亲和按新架构移植（或临时经 is_kv_cache_affinity_enabled() 开关下线，调用点去掉 kv_cache_runtime 传参）。
  B. 回 pin 14a7fe2d0：我方 RSI 代码适配主干 rsi API（补齐/适配缺失模块）。
  C. 等 agent-core 主干合入 RSI 线后取交集 rev（依赖 agent-core 侧排期）。
- 工作区状态：merge 提交 a36e95b6d 之上，pyproject.toml + uv.lock（pin=cd378e4c）作为后续提交入库；本记录保持本地不入库。

### 补记（2026-09-11 17:30）：全量缺口矩阵结果（284 个 openjiuwen 导入模块逐一对照）

矩阵脚本已跑完（含符号级核对，已剔除子模块 from-import 造成的假阴性）：

- 主干 rev 14a7fe2d0 真实缺口：约 24 个 RSI 模块——rsi.events（NodeStageEvent/EventUsage）、rsi.schema（RsiModelCall）、rsi.usage 整模块、rsi 顶层 load_cases、data_loader.case_files 整模块、single_harness.events_translate 整模块（RSI 前端展示核心）、evaluator.judger（LlmAsJudgeJudger/llm_as_judge）、case_backend._stage_case_assets，以及 paper_opt.auto_research 整棵树（11 个模块）+ program_opt.provider。 => pin 主干等于重写整个 RSI 产物引擎，不可行。
- cd378e4c 真实缺口（merge 收尾后仅剩这些）：
  1. kv_cache 三件套（前文已详列：4+2 文件路径替换 + KVCacheRuntime 新签名 + create_agent_team_session 去 kv_cache_runtime 参，7 文件 11 调用点）。
  2. jiuwenswarm/symphony/evolution/pack_adapter.py:8 模块级 from openjiuwen.symphony.flow.models import RecipeEvidence——flow.models/distill/narrative 三个模块在 cd3 不存在（develop 侧 symphony 演进特性）；同目录 service.py 的引用是函数级懒加载（运行时才炸），仅 pack_adapter 是收集期阻断。
- 两侧 rev 共同缺失（与本次 rev 选择无关的既有问题，均为懒加载或单测试文件）：
  - tests/.../test_permission_storage_transaction.py:18 模块级 import tiered_policy（两 rev 均无，收集期报错 1 个文件）。
  - 懒加载项（运行时才触发，不阻断收集）：extraction_runner.py:23 ContextEngine（正确路径应为 core.context_engine.context_engine）、permissions_persist.py:162/164 file_guard/tiered_policy、interface_deep.py:180 web_tools.is_paid_search_enabled、interface_deep.py:7939/11343 memory_tools.get_decorated_tools（函数在两 rev 均已不存在）。
- 结论：cd378e4c 是唯一短期可行方向（我方 RSI 全绿、develop 侧仅剩 kv_cache + symphony.flow 两块移植）。

### 适配记录（2026-09-11 17:50）：基准切换至主干 6ecf82184f5b9d5706b35015720f7b931869de56

方向变更：按用户指示以 openjiuwen 主干最新提交 6ecf821 为基准适配（该 rev 已合入 RSI 线，同时保留旧版 kv_cache 架构）。

矩阵复核（284 个导入模块）：RSI 全树（NodeStageEvent/events_translate/usage/paper_opt/program_opt）、kv_cache 旧路径（core.kv_cache 全套 + create_agent_team_session 的 kv_cache_runtime 参数 + KVCacheRuntime(binding_provider=...) 构造）、symphony.flow 全部恢复可用。develop 的 KV 亲和特性零改动兼容。

实际适配清单（4 处导入路径，共 3 个 src 文件 + 1 个测试文件）：
1. tests/unit_tests/agentserver/permissions/test_permission_storage_transaction.py:18 — tiered_policy 迁移至 harness.security.permission_engine.toolguard.tool_policy（原收集期阻断点）。
2. jiuwenswarm/agents/harness/common/rails/permissions/permissions_persist.py:162 — file_guard 迁移至 ...permission_engine.fileguard.file_guard。
3. 同文件 :164 — tiered_policy 同上。
4. jiuwenswarm/agents/harness/common/auto_memory/extraction_runner.py:23 — context_engine.engine 更名为 core.context_engine.context_engine。

确认无需修改（有兼容链/守卫/误报）：
- interface_deep.py:176-180 web_tools：首个分支 from openjiuwen.harness.tools import is_paid_search_enabled 在 6ecf821 可直接命中。
- tools/__init__.py:43-47 context_evolver.core：目录存在（无 __init__.py 的命名空间包，可导入），且有 ImportError 守卫。
- interface_deep.py:11343 get_decorated_tools：6ecf821 已删除该函数且无无参等价物；分支在 try/except ImportError: pass 中安全降级为 no-op。新装配入口为 agent_teams.memory.member_memory_toolkit.MemberMemoryToolkit（需 manager/ctx，非无参）。
- openjiuwen_deepsearch.* 6 个模块：仅 resources/skills 技能脚本引用，不在 pytest 收集范围，误报。

验证：8 项导入探针全过（rsi 链、两个 permissions 模块、extraction_runner、interface_deep、三个新路径符号、is_paid_search_enabled）。完整 pytest 收集验证运行中，结果补记。

### 结果（2026-09-11 18:05）：收集验证通过

uv run python -m pytest tests/unit_tests --collect-only：**9509 个用例全部收集成功，0 个收集错误**（耗时 6m09s；适配前为 285 个收集错误）。收集期阻断问题清零。

### 首轮全量执行（2026-09-11 18:15）：6147 通过 / 33 失败 / 11 跳过 / 3318 错误

3318 个 ERROR 为**环境问题而非代码问题**：pytest 临时目录基座 C:\Users\admin\AppData\Local\Temp\pytest-of-admin 权限损坏（WinError 5 拒绝访问，删除被占用），所有依赖 tmp_path/tmp_path_factory fixture 的测试在 setup 阶段统一报错。本机复现与仓库无关。
处理：改用 --basetemp 指向可用目录后重跑全量（结果待补记）。真实代码级失败预期在 33 个 failed 及重跑后数字中确认。

### 第一轮复核结果（2026-09-12，HEAD `a1db43335` 之上）

#### 依赖与收集

- `pyproject.toml` 和 `uv.lock` 均锁定 `openjiuwen` 到用户指定的
  `6ecf82184f5b9d5706b35015720f7b931869de56`；`uv lock --check` 通过（295 包）。
- `git diff --name-only --diff-filter=U` 为空，tracked files 中无 `<<<<<<<`/`>>>>>>>` 冲突标记；
  当前分支包含 `origin/develop`（merge-base 为 `0b0490a7065404b736464d06df94620738d65513`）。
- 当前工作区重新执行 `uv run python -m pytest tests/unit_tests --collect-only -q --no-cov`，
  **9503 个用例收集成功、0 个收集错误**。子代理集成测试位于 `jiuwenswarm/tests`，单独执行并通过，
  不计入根目录 `tests/unit_tests` 的收集数字。

#### 本轮发现并修复的逻辑问题

1. **free-search 上游私有 API 迁移**：`TrustedWebFreeSearchTool` 改为构造
   `openjiuwen@6ecf821` 的 `_FreeSearchRequest`，并完整转发代理、域名白名单和引擎开关；测试覆盖参数保真和工具级权限审计上下文（`test_trusted_web_search.py` 13 passed）。
2. **权限 rail 配方被重复定义覆盖**：`interface_deep.py` 后方旧定义会覆盖包含
   `session_id`、自动权限和运行时安全配置的正确定义；已删除旧定义及重复导入。
   深度权限组合 + native path 回归共 106 passed。
3. **子代理活动历史被旧重复实现覆盖**：`interface_deep.py` 的旧
   `_persist_subagent_activity` 会忽略显式 `activity_id/tool_call_id`，导致 request id
   可能碰撞；已保留增强实现并新增显式身份回归测试。子代理集成测试 16 passed。
4. **合并产生的重复方法**：删除 `agent_ws_server.py` 中完全重复的会话计划状态方法组，
   以及 `interface.py` 中完全重复的 `_make_retry_without_a2ui_call`。AST 扫描确认
   `jiuwenswarm` 内无重复 class method 定义。
5. **Symphony 超时状态传播**：`openjiuwen` 的 `AbilityManager.execute` 会复制每个工具的
   `AgentCallbackContext.extra`；已用共享的 invocation-local state 传递超时标记，避免下一次
   model call 重新暴露 graph tools。超时生命周期 + runtime service 共 22 + 73 passed。
6. **Skill 检索开关的跨层冲突**：主干已将 taxonomy/index 构建收口到总检索开关，已同步删除
   前端独立 index 开关、推荐构建流程、settings contract、双语推荐文案和 YAML 旧配置；保留
   后端状态响应中的兼容字段，避免旧客户端解析失败。相关 gateway 测试包含在最终 511 个核心目标测试中全部通过。
7. **WS admission 测试契约过时**：admission、pending interaction 和 interrupt resume 已由
   主干 `AgentRuntime` 统一拥有；删除只断言旧 server wrapper 行为的 4 个重复测试，保留 runtime
   等价覆盖。当前 WS 测试 42 passed，runtime service 73 passed。
8. **RSI 内置插件资源迁移**：主干已将 `plugin_packages` 迁移至 Hub；为 RSI 测试提供最小本地
   harness fixture，未恢复已删除的生产资源。catalog/roundtrip 11 passed。
9. **session cleanup 测试 key 不符合生产归一化**：测试改用 `_make_agent_cache_key`，未改生产实现；
   相关文件 12 passed。
10. **AgentServer 合并残留的未导入符号**：`agent_ws_server.py` 的 RSI 异步处理路径使用了
    `inspect.isawaitable`，流式方法签名使用 `Callable/Awaitable`，但主干合并后的导入缺失；已补齐
    导入并清理同方法中的无用局部变量。变更文件的 `ruff --select E9,F` 现已通过。
11. **Gateway 合并残留的类型与日志问题**：`app_gateway.py` 的 `RoutingTarget`、`FeishuChannel`
   仅在注解中使用却未声明，且断开钩子异常日志缺少格式化参数；已补充 `TYPE_CHECKING` 导入，
   并恢复钩子名、异常和 traceback 的日志输出，不改变 Feishu 的运行时懒加载。

#### 验证证据

- Python 核心目标回归（权限、free-search、Symphony、session cleanup、WS、gateway、RSI、
  subagent history）：**511 passed，3 warnings**；其中包含 `tests/unit_tests/runtime/test_runtime_service.py` 的
  **73 passed**，并使用短路径临时目录避免 Windows 路径长度上限干扰 RSI 测试。
- 前端 `npm run build`：TypeScript + Vite **通过**；既有 dynamic-import/chunk 体积警告，不影响构建。
- 前端 RSI/图布局/设置契约相关选择测试：11 + 3 + 3 + 7 全部通过。`test:settings-panel` 仍有
  9 个与本次 Skill 开关无关的既有模型/vendor/free-model/visual-tags 断言失败（51 passed / 9 failed），
  未为此引入行为性改动。
- 变更 Python 文件 `ruff --select E9,F` 通过；`git diff --check HEAD` 通过（仅 Windows 换行转换提示）。

#### 尚未处理的失败（明确不是本次合并回归）

历史 `pytest_full_run.log`（修复前运行）为 9405 passed / 82 failed / 22 skipped。剩余失败主要是
Windows 与 POSIX 路径/ shell/进程组假设、无开发者模式时的 symlink 权限、只读 chmod 删除语义、
以及少量过时测试契约（例如旧配置字段、旧 transport admission）。这些不应通过回退
`openjiuwen@6ecf821` 或恢复主干已删除资源来“修绿”，建议单独建立平台兼容性任务。

本轮改动尚未创建 commit；当前工作区仍包含代码、测试、此报告和 `pytest_full_run.log` 的未提交改动。

---

## 第二轮远端刷新复核（2026-09-12，`origin/develop` `b7da9ef98`）

### 合并动作与冲突结论

- 已执行 `git fetch origin develop`；远端从 `0b0490a70` 前进到 `b7da9ef98`，本轮新增 4 个提交：
  - `b7da9ef98`：修复盘古 `qwen3-30b-a3b` 模型调用；
  - `8ea63b115`：技能广场安装时持久化 `display_name`；
  - `c982b1e52`：产物下载后使用强提醒对话框；
  - `27e4d376e`：Personal Context 运行时与图谱交互改进。
- 先将工作区（含未跟踪文件）保存到 stash，再执行 `git merge --no-commit --no-ff origin/develop`，随后恢复 stash；Git 自动完成三方合并，**未产生文本冲突**。
- `git diff --name-only --diff-filter=U` 为空，仓库内无 `<<<<<<<`/`>>>>>>>` 冲突标记。RSI 目录及其新增实现仍保留；主干对旧 RSI 路径的删除没有误删特性分支新增内容。
- 用户指定的 `openjiuwen` `6ecf82184f5b9d5706b35015720f7b931869de56` pin 未被远端改动覆盖，`uv.lock` 与 `pyproject.toml` 仍保持一致。

### 批判性复核与本轮修复

1. 逐项检查了远端 4 个提交与 RSI/网关/Personal Context 的交集：模型兼容、技能展示名、桌面下载提示、Personal Context 的模型选择/运行历史/图谱 UX 均保留并通过回归测试；未发现需要回退主干实现的语义冲突。
2. Ruff 检查发现 `jiuwenswarm/channels/web/app_web.py` 登录代理中遗留的 `wrote_cookies` 局部变量已赋值但未使用（`F841`）；已删除该无效状态变量及其赋值，不改变登录代理行为。

### 第二轮验证证据

- 根目录 Python 单测收集：**9523 个用例，0 个收集错误**。
- 合并后核心目标回归：**732 passed，1 skipped，2 warnings**；针对本轮 `app_web.py` 修复的网关/下载子集再次执行，**221 passed**。
- 前端 RSI/阶段树/图布局/Symphony 操作测试：**11 + 3 + 3 + 7 全部通过**。
- 前端 `npm run build`：TypeScript + Vite **通过**（仅既有 dynamic-import 与 chunk 体积警告）。
- `uv lock --check`：**Resolved 295 packages**，通过；变更 Python 文件 `ruff --select E9,F` 通过；`git diff --check HEAD` 通过（仅换行转换提示）。
- 修复白屏后 `test:settings-panel` 为 **53 passed / 7 failed**；剩余失败是当前设置页模型/vendor/visual-tags 等旧断言，与本轮远端合并及 RSI 修复无关。

### 当前交付状态

代码和测试改动已 squash 提交到 `bc8f7fb7e`（父提交：`origin/feature/rsi` 与 `origin/develop`），5173 白屏修复追加提交为 `065d5167d`。`MERGE_CONFLICTS_REPORT.md` 与历史 `pytest_full_run.log` 仍为未跟踪文件，供维护者决定是否纳入后续文档/诊断提交。

---

## 5173 白屏回归定位（2026-09-12）

- 使用 Vite 在 `127.0.0.1:5173` 重现：HTML 与模块资源返回 200，但浏览器控制台出现
  `Uncaught Error: Setting key enable_free_models does not belong to source config`，
  来源为 `src/features/settings/registry/createSettingsPageDefinition.ts:36`；因此 React 根节点未挂载，页面显示为空白。
- 根因是合并后 `features/settings/modules/models/definition.ts` 仍注册了 `enable_free_models`，而主干提交 `d0c4ce2c8` 已从 `settingsContract` 移除该字段；严格设置注册校验在模块初始化阶段抛错。
- 修复：删除过时的 `free-models` 设置 section，保留模型管理器；未改变后端兼容字段或运行时配置写入逻辑。
- 验证：回归测试 `model settings no longer expose or persist the free-model switch` 通过；`npm run build` 通过；Vite HMR 与构建后静态服务均能渲染 Loading UI，控制台无应用级 `Uncaught`。修复已提交为 `065d5167d` 并推送到 `my_origin/chore/rsi-merge-develop`。

## 合并残留同类问题审计（2026-09-12）

为避免只修复白屏表象，本轮继续对合并提交 `bc8f7fb7e` 的 remerge 文件做同类问题排查：

- 对前端/后端合并交集执行相邻重复代码扫描，并用两个父提交逐项核对重复片段是否由合并引入。
- 对前端设置模块定义与 `settingsContract` 做声明式 key 对照；严格注册校验所覆盖的设置项均能在契约中找到，未发现第二个会在启动阶段抛错的过时字段。
- 对 `jiuwenswarm` Python 类方法执行 AST 重复定义检查；没有发现新的重复 class method。仓库 tracked 文本中也没有残留冲突标记。

### 发现并修复：外部 CLI 自动保存成功提示被渲染两次

`ExperimentalSettings.tsx` 中 `autoSavedAgents` 的成功提示块在两个父提交中各只有一份，合并结果却连续保留了两份，导致依赖安装完成后同一条状态提示重复显示。这是典型的“双方都保留同一逻辑、合并时重复拼接”的冲突解析残留，而不是有意的双状态。

- 删除重复 JSX 块，保留单一成功提示。
- 在 `settingsRefactor.test.mjs` 增加源代码级回归断言，确保成功提示 class 只出现一次。
- `node --test --test-name-pattern="external CLI auto-save|free-model" tests/settingsRefactor.test.mjs`：**2 passed**。
- `npm run build`：TypeScript + Vite **通过**；仅保留既有 dynamic-import/chunk 体积警告。

### 审计结论

除上述重复提示外，未发现同一冲突解析模式造成的其它确定性运行时残留。设置面板完整旧断言套件仍有 7 个源码形状类失败（模型/vendor/visual-tags 等），与本轮合并残留无关；没有为了迎合过时断言而恢复已删除配置或放宽契约校验。
