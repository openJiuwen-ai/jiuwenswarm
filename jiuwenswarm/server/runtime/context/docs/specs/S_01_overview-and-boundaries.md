# 上下文模块总览与包边界

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_01 / overview-and-boundaries |
| 关联模块 | jiuwenswarm/server/runtime/context/ + agent_adapter + context_read_patch + agents/harness/common/{rails,prompt}/ |
| 最近一次修订日期 | 2026-10-26 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

### 本规约管什么

**上下文模块（context module）** 覆盖 LLM **上下文窗口**与 **系统提示词动态组装** 在 jiuwenswarm 侧的产品化接缝：

- **ContextEngine 生命周期**：装配窗口、processor 压缩/卸载、checkpointer 落盘、手动 `/compact`
- **Context 工程 rails**：ContextAssembleRail / ContextProcessorRail 的 swarm 装配与门控
- **溢出恢复**：ContextOverflowRecoveryRail（swarm）
- **压缩召回与重载**：config 门控 + core 工具 `recall_compressed_context`
- **Swarm prompt 层**：RuntimePromptRail、ResponsePromptRail 及 `agents/harness/common/prompt/` 辅助
- **人设读优化**：`context_read_patch`（persona 文件读跳过 FS 锁）

文档归档点在 `server/runtime/context/`；实现代码按模块地图分散在 adapter / rails / handlers。

### 本规约**不**管什么

| 不归本模块 | 归属 |
|---|---|
| PersonalContext / Distill、用户画像 WS 域 | `server/personal_context/` |
| `RequestContext` / `AgentServerServices` 请求门面 | `server/context.py`（server 模块 specs） |
| Team 快照 / 绑定上下文 | `agents/harness/team/`、`team_binding_store` 等 |
| Skill Turbo `context_binding` 执行引擎 | `server/runtime/skill_turbo/` |
| ContextEngine / processor **实现本体与实现契约** | **agent-core** `openjiuwen/core/context_engine/`（含 `AGENTS.md` + `docs/specs/`；见 N4） |
| KV cache affinity | `react.kv_cache_affinity_config`（独立配置域，见 [[S_06]]） |

## 模块地图（跨目录管辖清单）

```
server/runtime/context/                    ← 文档归档（无 Python 包要求）
└── docs/

server/runtime/context_read_patch.py

server/runtime/agent_adapter/
├── interface_deep.py                      # 主装配：context_engine_config / rails / overflow / compact / save_contexts
├── interface_code.py
└── interface_flash.py                     # 裁剪 overflow recovery 等 rail

server/handlers/commands.py                # command.compact / compact_partial

agents/harness/common/rails/
├── context_overflow_recovery_rail.py
├── runtime_prompt_rail.py
└── response_prompt_rail.py

agents/harness/common/prompt/
├── prompt_builder.py
├── user_prompt_builder.py
├── shell_environment.py
└── browser_task_prompt.py

agents/harness/team/team_runtime_inheritance.py   # get_context_engine_enabled

jiuwenswarm/resources/config.yaml          # react.context_engine_config

agent-core（姊妹实现归档）：
├── openjiuwen/core/context_engine/AGENTS.md + docs/specs/   # 引擎 / processor / FullCompact / recall
├── openjiuwen/harness/rails/context_engineer/AGENTS.md     # Assemble / Processor（L2）
└── openjiuwen/harness/tools/compression_recall.py
```

## 不变量

- **N1（窗口单一真相源）**：给定 session，模型所见上下文以 ContextEngine 经 Assemble + processor 链产出为准；adapter 仅 orchestrate，不得维护第二份未同步的消息列表。
- **N2（命名域切割）**：本模块「上下文」= LLM context window；与 `PersonalContext`、`RequestContext`、`team_context` 名称相近但**语义正交**——文档与 issue 必须写全名，避免混用。
- **N3（配置根键）**：产品侧上下文引擎配置统一在 `react.context_engine_config`（`config.yaml`）；deep adapter 的 `_config_cache` 在 agent 模式即 react 段本身，**不要再嵌套一层 `react` 键**（见 interface_deep 注释）。
- **N4（双仓归档、就近生效）**：jiuwenswarm 本目录归档**产品装配 / 溢出 / prompt**；agent-core `context_engine/` 归档**引擎与 rails 实现契约**。改哪仓代码读哪仓 AGENTS；跨仓不变量只在拥有实现的一侧写死，另一侧引用——禁止两边复制全文导致漂移。
- **N5（flash 能力边界）**：flash profile 白名单**不含** `ContextOverflowRecoveryRail`（及若干健壮性 rail）；长上下文任务在 flash 下溢出容错弱于 agent/deep——部署选型时显式评估（见 interface_flash 模块 docstring）。

## 接口契约

### 文档入口

| 文档 | 读者用途 |
|---|---|
| `server/runtime/context/AGENTS.md` | 模块索引、铁律、测试命令 |
| `docs/designs/context_overview.md` | 概念层生命周期与术语 |
| `docs/specs/S_02` ~ `S_07` | 改代码前的分域契约 |

### 姊妹仓实现契约（agent-core，改 core 代码先读那边）

| 符号 | 路径 / 文档 |
|---|---|
| `ContextEngine` | `openjiuwen/core/context_engine/` + 其 `AGENTS.md` / `docs/specs/` |
| `ContextAssembleRail` / `ContextProcessorRail` | `openjiuwen/harness/rails/context_engineer/`（`__all__` 仅二者）+ L2 `AGENTS.md` |
| `CompressionRecallTool` | `openjiuwen/harness/tools/compression_recall.py`，工具名 `recall_compressed_context` |

## 数据结构

（本 spec 不重复 engine 内部 schema；会话落盘布局以 agent-core ContextEngine + checkpointer 为准。swarm 侧额外落点见 [[S_04]] compression_recall 目录。）

## 执行路径全景

### P1：Agent 实例创建与 context 补丁

```
JiuWenSwarmDeepAdapter.create_instance (interface_deep.py)
  → apply_context_read_patch()          # 人设读 only_read
  → create_deep_agent(..., context_engine_config=_deep_agent_context_engine_config(config))
  → _instantiate_rails 含 ContextOverflowRecoveryRail(max_recovery_attempts=3)
  → _update_agent_rails (agent 模式) 注册 ContextAssembleRail + 条件注册 ContextProcessorRail
```

### P2：每轮模型调用（概念）

```
Rails（priority 高→低 节选）
  ContextOverflowRecoveryRail (100)     on_model_exception 溢出恢复
  ContextAssembleRail / ContextProcessorRail (85)   窗口组装 + processor GET 路径
  RuntimePromptRail / ResponsePromptRail (5)          动态 system sections
  → LLM
```

### P3：用户命令压缩

```
ReqMethod command.compact
  → handle_command_compact (commands.py)
  → agent.compress_context(session_id, return_state=True)
  → 可选 append_compact_history_records
```

## 修改检查单

- 新增「上下文」相关模块或目录 → 先更新本 spec 边界表与模块地图，避免与 PersonalContext / RequestContext 混域。
- 改 flash rail 白名单 → 核对是否误纳入/剔除 context 相关 rail，并更新 [[S_06]] 与 N5。
- 在 agent-core 新增 processor 或改 preset 链 → 先修订 **agent-core** `context_engine/docs/specs/`，再同步 jiuwenswarm `_build_context_processor_rail` 与本仓 [[S_02]]（产品侧只记装配影响，不复制 core 全文）。

## 与其它 spec 的关系

- [[S_02]]：ContextEngine 与 processor 链（含 FullCompact GET 语义）
- [[S_03]]：ContextAssembleRail / ContextProcessorRail
- [[S_04]]：压缩召回与 `[[HANDLE]]` 重载
- [[S_05]]：溢出恢复与 `/compact`
- [[S_06]]：adapter 装配、config、allowlist、双默认漂移
- [[S_07]]：系统提示词分层（人设文件 vs swarm rails）
