---
description: Agent 运行时域说明书——给修改/检视本域代码的 AI 助手与工程师：域定位与统一术语、模块地图（顶层 23 模块 + 9 子目录）、对仓内其它域的公开入口、从 4 份 spec 提炼的架构铁律、与 openjiuwen 仓的接缝、测试命令与设计归档指引。
---

# Agent 运行时（server/runtime）

Agent 运行时域：负责 agent 实例的**装配、缓存、借用、预热、热更新与会话操作**——
从 `AgentRequest` 进入，经 `agent_adapter` 翻译到 openjiuwen 的 DeepAgent，再把
流式事件送回 gateway。本目录是 jiuwenswarm 仓与 agent-core 仓（openjiuwen 包）
之间全部调用流的必经之路。

**范围**：顶层 23 个 .py（~9,821 行）+ `agent_adapter/`、`a2ui/`、`workspace/`、
`debug_trace/`、`enterprise_config/`、`docs/` 子目录。`server/runtime/` 物理上还
装着另外三个模块——`session/`（会话持久化）、`skill/`（技能管理）、`skill_turbo/`
（技能加速引擎）——它们与本文覆盖的 Agent 运行时**平级**，划分依据是模块边界而非
目录层级：skill/、skill_turbo/ 与 session/ 各有自己的入口文档
（[skill/AGENTS.md](skill/AGENTS.md)、[skill_turbo/AGENTS.md](skill_turbo/AGENTS.md)、
[session/AGENTS.md](session/AGENTS.md)），改它们以各自文档为准。本文对这三者
只索引不展开。业务上属于本域的 `agents/harness/common/session_ops_service.py`
（fork / rewind / warmup 等 12 个公开函数）物理在 harness 目录，契约见 [[S_04]]。

## 模块地图

### 顶层模块（23 个，~9,821 行）

| 文件 | 行数 | 职责 |
|---|---|---|
| [agent_manager.py](agent_manager.py) | 2165 | `AgentManager`：两级缓存 `agents[channel_key][cache_key]`、借用/pin/退休、reload、interrupt 排序 |
| [tenant_agent_pool.py](tenant_agent_pool.py) | 1490 | `TenantAgentPool` 单例池：`(agent_id, service_id, workspace_key)` 三元组 LRU、跨租户续跑、prewarm 后台 |
| [agent_warm_pool.py](agent_warm_pool.py) | 813 | 预热池：WarmKey/WarmSlot/WarmClaim、revision 淘汰、前台抢占 |
| [agent_config_service.py](agent_config_service.py) | 540 | agent 配置读取与变更服务 |
| [pip_env.py](pip_env.py) | 511 | pip 环境管理 |
| [team_entity_store.py](team_entity_store.py) | 494 | team 实体存取 |
| [sync_agents_configs.py](sync_agents_configs.py) | 462 | agent 配置同步（磁盘 ↔ 池） |
| [reload_result.py](reload_result.py) | 390 | 热更新结果类型、日志 helper、env 影响域判定表（4 个 frozenset） |
| [memory_init_patch.py](memory_init_patch.py) | 370 | 企业版记忆库初始化 patch（空库拷模板、建库放线程） |
| [tool_catalog.py](tool_catalog.py) | 330 | 工具目录构建 |
| [team_binding_store.py](team_binding_store.py) | 376 | team 绑定存取 |
| [proactive_adapter.py](proactive_adapter.py) | 281 | 主动会话适配 |
| [prewarm.py](prewarm.py) | 260 | `WarmupModelClient` 与三阶段启动预热 |
| [enterprise_attachment_materializer.py](enterprise_attachment_materializer.py) | 250 | 企业附件物化 |
| [team_snapshot_refresh.py](team_snapshot_refresh.py) | 222 | team 快照刷新 |
| [code_source_unicode.py](code_source_unicode.py) | 197 | 代码源 unicode 规整 |
| [cron_local_runtime.py](cron_local_runtime.py) | 192 | 本地 cron 运行时 |
| [tenant_catalog_registry.py](tenant_catalog_registry.py) | 110 | 租户目录注册表 |
| [runtime_scope.py](runtime_scope.py) | 95 | `RuntimeScopeKey`（租户/服务/agent 三级作用域） |
| [tenant_context.py](tenant_context.py) | 91 | ContextVar 租户工作区绑定 |
| [mem_reclaimer.py](mem_reclaimer.py) | 81 | 周期 gc.collect + malloc_trim（默认关，`JIUWENSWARM_GC_RECLAIM_INTERVAL`） |
| [context_read_patch.py](context_read_patch.py) | 78 | 人设文件读锁 patch（仅企业版） |
| [session_owner_routing.py](session_owner_routing.py) | 23 | 控制续跑绑定到已验证 session owner |

### 子目录

| 目录 | 规模 | 归属与职责 |
|---|---|---|
| [agent_adapter/](agent_adapter/AGENTS.md) | 27 文件 43,566 行 | **本域**。适配器层：`AgentAdapter` Protocol → `JiuWenSwarm` 门面 → `JiuWenSwarmDeepAdapter` 基类 → code/flash 子类，`create_adapter()` 工厂。见其 AGENTS.md |
| [a2ui/](a2ui/) | 11 文件 | **本域**。A2UI 集成 |
| [workspace/](workspace/) | 4 文件 | **本域**。工作区配额（fs_quota_guard 写盘包装） |
| [debug_trace/](debug_trace/) | 8 文件 | **本域**。request 级人类可读 dump（`/debug` 指令） |
| [enterprise_config/](enterprise_config/AGENTS.md) | 6 文件 | **本域**。Gateway 本地库解析企业配置策略与模板，见其 AGENTS.md |
| [docs/](docs/AGENTS.md) | specs/features/bugfix | **本域**。设计归档目录，规约见 docs/AGENTS.md |
| [session/](session/AGENTS.md) | 14 py | 会话持久化与辅助服务：history.jsonl 读写/截断、metadata、项目存储、diff 监视、权限账本。本域 [[S_04]] 的 session_ops 建立在其原语之上，见其 AGENTS.md |
| [skill/](skill/AGENTS.md) | 7 py + skilldev/ | **技能管理**：技能全生命周期（安装/卸载/启停/账本/市场/企业预置），跨 `server/runtime/skill/` 与 `agents/harness/common/{rails,tools}/` 两处，见 [skill/AGENTS.md](skill/AGENTS.md) |
| [skill_turbo/](skill_turbo/AGENTS.md) | 23 py + rails/ + runtime/ | **SkillTurbo 技能加速引擎**：技能执行改走预编排 PlanNode 节点树流式执行，见 [skill_turbo/AGENTS.md](skill_turbo/AGENTS.md) |

子目录是否立自己的 AGENTS.md，判断依据是**体量与本地规则密度**，不是目录本身：
agent_adapter/ 是本域主体（43,566 行 = 顶层全部代码的 4 倍多、其余子目录合计的
8 倍多），自带四层接口结构与一组只在它内部成立的铁律，不拆出去则 L1 会被
interface_deep.py 一个文件的细节淹没；enterprise_config/ 体量小但规则密度高
（四级加载链、三级进程缓存与 fail-closed 语义集中在 1,030 行内），同样立了
[enterprise_config/AGENTS.md](enterprise_config/AGENTS.md)；a2ui/、workspace/、
debug_trace/ 三个合计 4,118 行、职责单一、公开入口各 1-2 个，规则就近
放本文与 specs 足够，逐一立文档只会造成维护负担。

## 对仓内其它域的公开入口

| 入口 | 消费方 |
|---|---|
| `TenantAgentPool`（tenant_agent_pool.py） | gateway / handlers 的会话创建、续跑、配置同步 |
| `AgentManager`（agent_manager.py） | 企业版装配与缓存 |
| `create_adapter()`（agent_adapter/agent_adapters.py） | 两个池的唯一装配工厂 |
| `prewarm` 三阶段 + `WarmupModelClient`（prewarm.py） | agent_warm_pool / tenant_agent_pool 启动预热 |
| `mem_reclaimer.setup()`（mem_reclaimer.py） | 进程启动时调用一次 |
| session_ops 12 个公开函数（agents/harness/common/session_ops_service.py） | fork → handlers/bootstrap.py；rewind → handlers/session.py + gateway；redo → git_ws_handler；warmup → agent_adapter/interface_deep.py。契约见 [[S_04]] |

## 架构铁律（每条细则见对应 spec）

1. **cache_key 公式不可改**：`f"{mode}:{sub_mode}:{project_dir}"`；plan 是
   `DeepAgentState.plan_mode` 运行期状态**不是装配形态**，禁止为其加装配分支。
   [[S_01]] N1/N2
2. **borrower/pin 双引用通道**：退休必须等两通道都释放；HITL 续跑 owner 排序
   `(2, live pause) > (1, awaiting only)`。[[S_01]] N3/N4
3. **TenantAgentPool 的 LRU 无上限**：防 HITL 期间活跃会话被误淘汰，内存治理
   交给 evict 清理链与 mem_reclaimer，不要给 LRU 加 maxsize。[[S_01]] N7
4. **预热三层开关与互斥**：`JIUWENSWARM_AGENT_PREWARM` 默认关、`JIUWENSWARM_SESSION_CREATE_PREWARM`
   默认开；排除通道 `{"acp","a2a"}`；openjiuwen 进程级注册表互斥——同一
   adapter 实例禁止两处同时注册。[[S_02]] N1-N4
5. **reload 串行 + 指纹去重**：热更新全局串行锁（防 OOM）；指纹含拓扑，
   指纹不变不重建；env 走两段提交（staged tip、idle 时 promote）。[[S_03]] N1/N2/N4
6. **热更新日志红线**：只记 key/path 不记 value；错误信息过
   `redact_reload_error_message`（敏感词 → `[redacted]`，>120 字符截断）。[[S_03]] N6
7. **session_ops soft-only**：rewind/compact 只回退对话不动工作区文件；
   `project_dir` 必须先解析后截断（metadata.json 非原子覆写的 race）；
   warmup 限界 10,000 条且从 user 边界开始。[[S_04]] N1/N2/N4

## 与 openjiuwen（agent-core 仓）的接缝

- **主调用流**：`JiuWenSwarm` 门面 → `JiuWenSwarmDeepAdapter` → openjiuwen
  （`core.single_agent` 装配 / `harness.factory` 工厂 / `harness.rails` rails），
  三层消费的细节与禁忌见 [agent_adapter/AGENTS.md](agent_adapter/AGENTS.md)。
- **注册表互斥**：agent_warm_pool 借用 openjiuwen 进程级注册表做实例互斥
  （[[S_02]] N2）。
- **状态直操作**：session_ops 直接读写 openjiuwen 的 Session / checkpointer /
  context_engine（[[S_04]]）；`session_owner_routing` / `runtime_scope` 的
  模式解析须与 openjiuwen `resolve_adapter_mode` 对齐。
- 改 openjiuwen 公共接口前，先 grep 本目录（重点 `interface_deep.py` 556 个方法）。

## 测试

单测分布与跑法。pytest.ini 的 addopts 含全仓 coverage 与 `-v`，日常单跑请用
`-o addopts="--asyncio-mode=auto"` 覆盖为仅保留 asyncio 模式；`python` 指你
本机装有开发依赖的解释器（仓内各人 venv 路径不同，不统一规定）：

```bash
cd jiuwenswarm  # 从 dev-stable 双仓根出发；目标即含 pytest.ini 的 jiuwenswarm 仓根，
                # 下文相对路径均以此为基准

# 池与缓存
python -m pytest tests/unit_tests/agentserver/test_agent_warm_pool.py \
  tests/unit_tests/agentserver/test_agent_manager_session_cleanup.py \
  -o addopts="--asyncio-mode=auto"

# 热更新
python -m pytest tests/unit_tests/agentserver/test_agent_reload_config_hook.py \
  tests/unit_tests/agentserver/test_agent_reload_model_scope.py \
  tests/unit_tests/agentserver/test_agent_reload_scope.py \
  -o addopts="--asyncio-mode=auto"

# 会话操作
python -m pytest tests/unit_tests/test_session_ops_fork.py \
  tests/unit_tests/test_session_ops_rewind_turns.py \
  tests/unit_tests/test_session_ops_state_copy.py \
  tests/unit_tests/test_session_ops_warmup.py \
  tests/unit_tests/test_rewind_project_dir_race.py \
  -o addopts="--asyncio-mode=auto"
```

## 设计文档归档与双向同步

本域设计文档在 [docs/](docs/AGENTS.md)，规约（specs / features / bugfix 三表、
命名、`[[S_NN]]` 简引、三连续提交 `feat(runtime)`/`test(runtime)`/`docs(runtime)`）
见 [docs/AGENTS.md](docs/AGENTS.md)。现有 spec：

| spec | 主题 |
|---|---|
| [[S_01]] | agent 实例生命周期：两级缓存、borrower/pin、HITL 续跑、evict 清理链 |
| [[S_02]] | 预热与 warm pool：三层开关、注册表互斥、claim 三分支、revision 淘汰 |
| [[S_03]] | 配置热更新：串行锁、指纹去重、model scope fan-out、env 两段提交 |
| [[S_04]] | 会话操作与上下文重建：soft-only、三数据轴、事件流重建状态机 |

**双向同步**：读文档发现与代码不符时，以代码为准，当场修订对应 spec——
不要留待"以后补"。域规约变动（契约 / 不变量 / 执行路径 / 持久化结构）
必须同步修订对应 spec，并在提交代码的同一 PR 内完成。
