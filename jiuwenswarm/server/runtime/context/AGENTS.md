---
description: 上下文模块说明书——LLM 上下文窗口生命周期（装配/压缩/卸载/召回/溢出恢复）与动态系统提示词组装的公开入口、模块地图、架构铁律与 spec 索引。给修改/检视本模块代码的 AI 助手与工程师。
---

# Context Management（上下文）

LLM **上下文窗口**与**系统提示词**的产品化子系统。本模块覆盖：对话上下文在模型窗口内的**装配**（ContextEngine + processor 链）、**压缩 / 卸载 / 重载**、**压缩召回**、**溢出检测与自动恢复**，以及 swarm 侧的**动态 prompt rail**（运行时状态 / 回复格式 / 附件段）。

本文件是 **jiuwenswarm 产品侧**入口索引。**修改任何代码前先读对应 spec**（见「设计文档归档与双向同步」）。**实现与文档双仓并存**：

| 关切 | agent-core（实现契约） | jiuwenswarm（本 archive） |
|---|---|---|
| `ContextEngine` / processor 链 / FullCompact / recall 工具 | **OWNS** — `openjiuwen/core/context_engine/AGENTS.md` | 引用；adapter 传参 |
| `ContextAssembleRail` / `ContextProcessorRail` | **OWNS** — 同上 + `harness/rails/context_engineer/AGENTS.md` | 注册门控、allowlist 同步 |
| `ContextOverflowRecoveryRail` / `context_read_patch` / Runtime·Response prompt | 引用 | **OWNS** |
| `react.context_engine_config` 产品默认 | 引用 | **OWNS** |

改 engine / processor / core rails → 先读 **agent-core** 侧 AGENTS；改装配 / 溢出 / 产品 prompt → 读本目录。**本模块代码横跨多个目录**：文档归档在 `server/runtime/context/`；实现分布在 adapter / `context_read_patch` / harness rails+prompt（见模块地图）。

- `docs/specs/S_01_overview-and-boundaries.md` — 总览与包边界（跨目录管辖 / 两仓协作 / 与 PersonalContext·RequestContext 的切割）
- `docs/specs/S_02_context-engine-and-processors.md` — ContextEngine、forked processor 链、FullCompact GET 路径语义
- `docs/specs/S_03_context-engineer-rails.md` — AssembleRail vs ProcessorRail、priority、AbortError 循环中止
- `docs/specs/S_04_compression-recall-and-reload.md` — recall 工具、`[[HANDLE]]` 重载、enable 门控
- `docs/specs/S_05_overflow-recovery.md` — 413/400+关键词检测、force_compact、熔断事件、`/compact`
- `docs/specs/S_06_product-assembly-and-config.md` — interface_deep/code/flash 门控、context_read_patch、config 键、allowlist 与渐进工具同步
- `docs/specs/S_07_system-prompt-layering.md` — 工作区人设文件 vs Runtime/Response rails vs 压缩召回段的分层归属
- `docs/designs/context_overview.md` — 特性文档（生命周期 / 术语 / 能力边界）

姊妹域：`server/personal_context/`（Distill / 用户画像）、`server/context.py`（RequestContext 请求门面）、`server/runtime/skill_turbo/`（context_binding 执行引擎）有独立边界，本体系不覆盖其内部设计。

## 统一术语

```
ContextEngine     agent-core 上下文引擎：会话消息池 + processor 链 + checkpointer 落盘
processor 链      forked.activate → … → RoundLevel；FullCompact 由 swarm 按需注入（非 preset 默认项）
[[HANDLE:…]]      enable_reload 下模型标记，把已卸载大消息原文拉回窗口
[[COMPRESSION_RECALL:…]]  压缩召回标记（compression_recall_config.enabled 时）
```

| 术语 | 含义 |
|---|---|
| **ContextAssembleRail** | agent-core（priority=85）：向 system prompt 注入 workspace / 人设文件（AGENT/SOUL/…）/ tools 段；`tool_name_allowlist` 与 progressive eager 对齐 |
| **ContextProcessorRail** | agent-core（priority=85）：挂载 ContextEngine processor 链；受 `context_engine_config.enabled` 门控 |
| **ContextOverflowRecoveryRail** | swarm（priority=100）：LLM 上下文溢出时 reactive 恢复 + 熔断流式事件 |
| **FullCompactProcessor** | 整窗兜底压缩；日常 add 路径不触发，溢出时经 `set_force_compact` 在 GET 路径触发 |
| **RuntimePromptRail** | swarm（priority=5）：时间、运行时 YAML、shell 环境、附件段等动态注入 |
| **ResponsePromptRail** | swarm（priority=5）：回复格式、思考纪律、最终可见回复规则 |
| **context_read_patch** | 人设文件（AGENT/SOUL 等）读取时 `only_read=True` 跳过跨进程 FS 锁 |
| **react.context_engine_config** | `config.yaml` 上下文引擎配置根（enabled、各 processor 子配置、召回/重载） |
| **双默认漂移** | `interface_deep` 读 `enabled` 缺省 **False**；`get_context_engine_enabled` 缺省 **True**（团队/runtime 继承路径） |

## 公开入口（public API）

本目录**无 Python 包门面**——行为入口在装配层与 rails。改代码时以下列符号为导航锚点。

| 域 | 入口 | 说明 |
|---|---|---|
| 引擎配置对象 | `_deep_agent_context_engine_config(react_cfg)` | `interface_deep.py` → `create_deep_agent(..., context_engine_config=…)` |
| Assemble 构建 | `_build_context_assemble_rail(disabled_tools, tool_name_allowlist)` | 与 ProgressiveToolRail.eager_tools 对齐 tools 段（[[S_06]]） |
| Processor 构建 | `_build_context_processor_rail(config)` | 从 `context_engine_config` 子键组装 user_processors + preset 链 |
| 溢出恢复 | `_build_context_overflow_recovery_rail()` → `ContextOverflowRecoveryRail(max_recovery_attempts=3)` | deep/code 装配；flash **不含**（[[S_05]]） |
| 人设读补丁 | `apply_context_read_patch()` / `remove_context_read_patch()` | `create_instance` 路径安装；单测可 remove |
| Prompt rails | `RuntimePromptRail` / `ResponsePromptRail` | `before_model_call` 注入 system prompt sections |
| 手动压缩 | `handle_command_compact` / `handle_command_compact_partial` | `commands.py` → `agent.compress_context` / `compact_partial` |
| 团队门控 | `get_context_engine_enabled(config)` | `team_runtime_inheritance.py`；注意与 deep adapter 默认不一致（[[S_06]]） |
| Core 实现契约 | agent-core `openjiuwen/core/context_engine/AGENTS.md` + `harness/rails/context_engineer/AGENTS.md` | 改 core 代码以那边为就近真相源；本仓 specs 只锁产品装配 |

**新增 processor 或 config 子键**：改 `config.yaml` 注释与 `_build_context_processor_rail` 读取逻辑，并同步 [[S_02]] / [[S_06]]；不要在 rail 内硬读全局 yaml。

## 模块地图

```
server/runtime/context/                    ← 文档归档点（本 AGENTS.md + docs/）
└── docs/                                  # specs / designs / features / bugfix

server/runtime/context_read_patch.py       ← 本模块管辖：人设读 only_read 补丁

server/runtime/agent_adapter/              ← 装配与运行时（本模块管辖相关片段）
├── interface_deep.py                      # _deep_agent_context_engine_config / assemble / processor / overflow rail / compact API / save_contexts 收尾
├── interface_code.py                      # 复用 deep 的 processor/assemble 构建；固定 rail 表含 Context*Rail
└── interface_flash.py                     # _FLASH_RAIL_KEEP：无 overflow recovery；动态 rail 白名单裁剪

server/handlers/commands.py                ← command.compact / compact_partial 处理器

agents/harness/common/rails/               ← swarm context rails
├── context_overflow_recovery_rail.py      # ContextOverflowRecoveryRail（100）
├── runtime_prompt_rail.py                 # RuntimePromptRail（5）+ attachment sections
└── response_prompt_rail.py                # ResponsePromptRail（5）

agents/harness/common/prompt/              ← prompt 构建辅助（本模块管辖）
├── prompt_builder.py
├── user_prompt_builder.py
├── shell_environment.py
└── browser_task_prompt.py

agents/harness/team/team_runtime_inheritance.py  ← get_context_engine_enabled / 成员 ContextProcessorRail

jiuwenswarm/resources/config.yaml          ← react.context_engine_config（配置单一真相源，注释即契约）

agent-core（姊妹实现归档 — 改 core 代码读那边）：
├── openjiuwen/core/context_engine/AGENTS.md (+ docs/specs/)   # 引擎 / processor / FullCompact / recall 契约
├── openjiuwen/harness/rails/context_engineer/AGENTS.md       # Assemble / Processor rails（L2）
└── openjiuwen/harness/tools/compression_recall.py            # recall_compressed_context 工具

相邻但不归本模块：
├── server/personal_context/               # PersonalContext / Distill
├── server/context.py                      # RequestContext（HTTP/WS 请求门面）
├── agents/swarm/…/skill_turbo context_binding
└── team_context（团队快照上下文，非 LLM window 引擎）
```

## 架构铁律

以下 10 条从各 spec 不变量提炼（全量不变量以 spec 为准，条目可被 code review 单条引用）：

1. **ContextEngine 是会话上下文唯一真相源**（[[S_02]] N1）。窗口内消息与 processor 状态经 engine 读写；adapter 收尾必须 `save_contexts`，禁止旁路复制一份"影子上下文"喂模型。
2. **Assemble 与 Processor 门控分离**（[[S_03]] N1）。`ContextAssembleRail` 在 agent 模式下注册**不依赖** `context_engine_config.enabled`；`ContextProcessorRail` 仅当 `enabled` 为真时注册——二者不可绑成一个开关。
3. **FullCompact 走 GET 路径、force_compact 触发**（[[S_02]] N2 / [[S_05]] N2）。日常 add 不依赖 FullCompact；溢出恢复 rail 对 `FullCompactProcessor` 调 `set_force_compact(True)`（及可选 `set_overflow_threshold_override`），下一次 `get_context_window` 无条件压缩。
4. **溢出检测三分支**（[[S_05]] N1）：status **413**；**400 + 溢出关键词**；无 status 时**纯关键词兜底**（含 ModelArts 组合模式）。Good：先读 `_is_context_overflow_error` 再改检测。Bad：凡 400 都当溢出。
5. **熔断后用户可行动**（[[S_05]] N3）。连续失败超过 `max_recovery_attempts` 发 `context_overflow_circuit_break` 流式 error，提示 `/compact` 或新会话；不得 silent 无限 retry。
6. **Processor 循环中止必须 AbortError + CONTEXT_EXECUTION_ERROR**（[[S_03]] N2，agent-core 契约）。swarm 不得在 processor rail 外包一层"吞掉中止"的兜底。
7. **压缩召回与重载独立门控**（[[S_04]] N1）。`compression_recall_config.enabled` 与 `enable_reload` 分别生效；工具 `recall_compressed_context` 仅在前者开启且压缩归档存在时才有意义。
8. **Tools 段与 eager schema 对齐**（[[S_06]] N2）。`ContextAssembleRail.tool_name_allowlist` 须与 `ProgressiveToolRail.eager_tools` 同步（`_sync_context_assemble_tool_allowlist`），避免任务中途改写 prompt 前缀。
9. **人设读 patch 只放宽读、不改写**（[[S_06]] N3）。`apply_context_read_patch` 仅对 persona 读路径设 `only_read=True`；写文件与原锁语义不变。
10. **双仓归档、就近生效**（[[S_01]] N4）。产品装配 / 溢出 / prompt 契约在本目录 `docs/`；engine / processor / core rails 契约在 agent-core `context_engine/docs/`。同一不变量只在「拥有实现」的一侧写死，另一侧引用——禁止两边各写一套互相漂移的全文。

## 生命周期 / 配置消费

### 配置项（`jiuwenswarm/resources/config.yaml` → `react.context_engine_config`）

| 配置 | 消费点 | 语义摘要 |
|---|---|---|
| `enabled` | deep `_update_agent_rails` / team `get_context_engine_enabled` | ProcessorRail 总开关（见双默认漂移） |
| `enable_reload` | `_deep_agent_context_engine_config` | `[[HANDLE]]` 重载已卸载消息 |
| `compression_recall_config` | ContextEngineConfig + 压缩器 | 归档分块 + `[[COMPRESSION_RECALL]]` |
| `*_compressor_config` / `message_summary_offloader_config` | `_build_context_processor_rail` | 各 processor 阈值与行为 |
| `full_compact_processor_config` | user_processors 注入 FullCompact | 溢出兜底；与 RecoveryRail 联动 |
| `reasoning_tool_loop_compact_config` | ReasoningToolLoopCompactProcessor | 循环折叠 + bailout 阈值 |
| `context_window_tokens` / `enable_openrouter_model_context_window_tokens` | ContextEngineConfig | 窗口上限解析 |
| `enable_context_debug` / `context_debug_dir` | agent-core debug 落盘 | 压缩排查 JSONL |

KV cache affinity 走独立 `react.kv_cache_affinity_config`，**不归**上下文引擎 spec（见 [[S_06]]）。

### 关键路径

| 路径 | 用途 |
|---|---|
| `{workspace}/context/…` | 会话 context 落盘（agent-core 布局；debug 子目录见 config 注释） |
| `{session}/compression_recall/` | 压缩召回归档（enabled 时） |
| `<workspace>/AGENT.md` 等 | 工作区人设（core context section 读取；读 patch 见 [[S_07]]） |
| `runtime_state.yaml` | RuntimePromptRail 每轮读取的运行时快照 |

## 测试

pytest.ini 的 addopts 含全仓 coverage，日常跑请清掉（`-o addopts=""`）。

```powershell
# swarm：溢出恢复 / assemble allowlist / 读 patch / 部分 compact
.venv\Scripts\python.exe -m pytest tests/unit_tests/agentserver/test_code_adapter_context_overflow_recovery_rail.py tests/unit_tests/agentserver/test_context_assemble_allowlist_compat.py tests/unit_tests/agentserver/test_context_read_patch.py tests/unit_tests/agentserver/test_compact_partial.py -o addopts=""

# agent-core（协作边界回归，在 monorepo / vendor 可及处）
.venv\Scripts\python.exe -m pytest tests/unit_tests/core/context_engine/ tests/unit_tests/harness/test_context_*.py tests/unit_tests/harness/test_compression_recall_tool.py -o addopts=""
```

- **改溢出检测或 force_compact 桥接**：先跑 `test_code_adapter_context_overflow_recovery_rail.py`。
- **改 allowlist 与 Progressive 同步**：跑 `test_context_assemble_allowlist_compat.py`。
- **改 context_read_patch**：跑 `test_context_read_patch.py`（含 remove 还原）。
- **改 compact 命令或 partial 路径**：跑 `test_compact_partial.py` + 手动 `command.compact` 冒烟。

## 代码风格

- 类型注解：PEP 585 / PEP 604；与 `interface_deep` 存量风格一致，不强改 Optional 别名。
- 注释 / docstring：processor 注入顺序、FullCompact 非 preset、flash 不含 overflow rail 等注释是**契约**，清理时勿删。
- 日志禁止 `print`；熔断 `error_type=context_overflow_circuit_break` 是对外行为契约，改名 = 破坏性变更（[[S_05]]）。

## 设计文档归档与双向同步（本模块强制）

本模块设计文档**全部**落在 `docs/` 下，命名与结构规约见 [`docs/AGENTS.md`](docs/AGENTS.md)。把 **`server/runtime/context/docs/` + 模块地图所列实现文件 + 本 AGENTS.md** 当作一个一致性单元维护——上下文链跨 adapter、core engine、overflow rail 与 prompt 层，"只改一条装配路径"是缺陷的头号来源。

**前置铁律——文档先是设计的输入，才是提交的产出。** 动手改任何代码**之前**，先 grep + 读相关 `docs/specs/S_NN_*`（现有契约 / 不变量 / 修改检查单），拿它们校准方案；方案定了**当场**同步修订对应 spec。先码后档、拖到提交前才补文档是把流程做反了。

三条强制约束，提交时必查：

1. **特性更新归档 feature 且三连续提交**：`docs/features/` 新增 `F_NN_<slug>.md`；特性代码（`feat(context)`）、测试（`test(context)`）、文档（`docs(context)`）拆成三个紧邻提交、同一 PR 落地。bugfix 触及 spec 描述的路径 / 不变量时，同步修订对应 spec 并归档 `docs/bugfix/B_NN_<slug>.md`，代码、测试、文档合并为**一个** `fix(context)` 提交（三连续提交的唯一例外，与 skill 模块约定一致）。
2. **规约变动必更新 specs**：processor 链 / rail priority / 溢出检测分支 / config 键 / prompt 段归属变化时，同步修订对应 `S_NN`（元信息"最近一次修订日期"填当天）；新规约主题 = 新 spec 文件。
3. **读到不一致当场修文档**：本文件 / specs 里与代码不符的接口名、枚举值、路径、点名数量的结构表述（如「溢出检测三分支」「Assemble/Processor 门控分离」「flash 不含 overflow recovery rail」），先 grep 代码、以代码为准刷新文档，同一次改动落地；不把过时表述当新约束执行。**纯计数类数字（路由条数、方法数、文件行数）不写入文档**——它们随日常增减高频漂移且不承载设计语义。

## 提交约定

本模块改动的 commit message scope 固定用 `context`（如 `feat(context): tighten overflow keyword match`）——含模块地图所列实现文件的改动。

footer 用 `Refs: #<issue>` 格式关联 issue。issue 号若无法从当前上下文明确，必须先询问用户，不要臆造或留空。

涉及 `docs/features/F_*` / `docs/specs/S_*` 文档更新的特性改动，**特性代码、测试代码、文档拆成三个连续提交**（提交 1 特性代码 `feat(context)`，提交 2 单测 `test(context)`，提交 3 文档 `docs(context)`）；归档 `docs/bugfix/B_*` 的缺陷修复例外，代码、测试、文档合并为一个 `fix(context)` 提交。细则见上文「设计文档归档与双向同步」与 [`docs/AGENTS.md`](docs/AGENTS.md)。
