# B_03 — session.prepare / 首聊 evolution 配置漂移触发 Evolution rail config mismatch（OA.05000090）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_03 / evolution-rail-config-mismatch |
| 关联模块 | `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`（`_ensure_active_evolution_rails_registered`、`_active_evolution_rail_needs_rebuild`、`_unconfigure_active_evolution_rails`）；上游 `openjiuwen.harness.rails.evolution.configuration.configure_skill_evolution_runtime`；下游 relay-claw catalog 企业禁用快照 `packages/api/src/utils/relayclaw-evolution-config.ts` |
| 修复日期 | 2026-11-26 |
| 关联 issue | #5192 |
| 关联 feature / spec | runtime [[F_02]]（session.prepare）；配置热更见 [[S_03]]（本修复不改 S_03 不变量） |
| 关联测试 | `tests/unit_tests/agentserver/test_agentserver_modes.py`：`test_deep_adapter_rebuilds_evolution_rails_when_signal_trigger_changes`、`test_deep_adapter_retries_evolution_configure_after_cached_ref_mismatch`、`test_deep_adapter_rebuilds_plan_evolution_rails_when_language_changes` |

## 现象与影响

现场错误码 **OA.05000090**（前端「服务异常 / 未分类错误」）。链路：

1. Relay 在首条消息前发 `session.prepare`，Deep adapter 按当时 catalog / env 快照
   `configure_skill_evolution_runtime`，挂上 `SkillEvolutionRail`（例如
   `signal_trigger=False`）。
2. 随后正式 `chat.send`（或同进程内下一次 reconcile）解析出的触发器与预热不一致
   （例如 `signal_trigger=True`）。
3. openjiuwen 对**已存在** rail 做 in-place 配置严格校验，抛
   `RuntimeError: Evolution rail config mismatch...`，请求在模型运行前于 prepare /
   ensure 阶段终止。

影响：整轮对话起不来；用户只看到未分类错误，无模型产出。根因是**跨进程 / 跨阶段
契约冲突**（prepare 与首聊的 evolution 配置快照漂移 × 上游 fail-fast），不是模型本身故障。

## 关联契约

### openjiuwen（上游校验）

- `configure_skill_evolution_runtime` 在 agent 已持有 evolution rail 时调用
  `_validate_evolution_rail_config`；属性不一致即 fail-fast。
- 本仓兜底依赖异常文案子串：**`Evolution rail config mismatch`**
  （见 `interface_deep.py` except 分支）。上游改文案会导致兜底静默失效，回退为异常上抛。

### 本仓漂移检测属性集（必须与上游校验集同步）

`_active_evolution_rail_needs_rebuild` 当前对照缓存 rail 与目标配置的属性：

| 属性 | 来源 |
|---|---|
| `_language` | `rail._language` vs `_resolve_runtime_language()` |
| `signal_trigger` | `rail.signal_trigger` vs `get_passive_skill_evolution_triggers(...)["signal_trigger"]` |
| `review_trigger` | `rail.review_trigger` vs 同上 `["review_trigger"]` |

**维护约束**：上游 `_validate_evolution_rail_config` 新增可漂移属性时，本函数必须同步
扩展；否则漂移检测静默漏判，只能依赖 mismatch 兜底路径（或直接炸）。

### relay-claw（配置快照）

企业 / 关总开关的运行时快照（`disableSelfEvolutionInConfig` /
`ENTERPRISE_EVOLUTION_ENV`）只关能力开关，**不踩** `signal_trigger` /
`team_signal_trigger` 偏好，降低 prepare 与 chat 快照漂移概率。偏好恢复来自用户
config.yaml，不是靠取消禁用。Deep 路径在 `enabled=false` 时会 unconfigure rail，
trigger 不单独生效。

## 根因与修复决策

**根因**：prepare 与首聊使用的 evolution 触发器 / 语言快照可能不一致；旧实现仅在
语言变化时先 unconfigure，`signal_trigger` / `review_trigger` 变化仍直接
configure → 撞上 openjiuwen 严格校验。

**修复分工**（两条路径，缺一不可）：

1. **前置漂移检测**（主路径）：`_active_evolution_rail_needs_rebuild` 为真时先
   `_unconfigure_active_evolution_rails()` 再 configure。覆盖 adapter 缓存可见的
   language / signal_trigger / review_trigger 漂移。
2. **mismatch 兜底重试**（安全网）：缓存引用失效（`_skill_evolution_rail is None`
   或与实例实际 rail 不一致）时，前置检测跳过，configure 仍可能抛 mismatch；
   except 匹配固定文案后 unconfigure 一次并原参重试。覆盖 stale refs 场景。

**拒绝的方案**：

- 只改 relay 快照、不改 adapter：无法覆盖缓存漂移与其它路径的 trigger 变化，请求仍会炸。
- 放宽 / 绕过 openjiuwen 校验：属上游契约，本仓不应 fork 校验语义。
- 捕获任意 `RuntimeError` 重试：过宽，会吞非 mismatch 故障；故仅匹配文案子串。

## 回归证据与适用范围

- 单元：`test_deep_adapter_rebuilds_evolution_rails_when_signal_trigger_changes`
  （False→True 前置重建）。
- 单元：`test_deep_adapter_retries_evolution_configure_after_cached_ref_mismatch`
  （缓存置 `None` + 翻转 trigger → mismatch 被吞、重试成功）。
- 单元：语言变化重建既有用例仍通过；skills 目录 patch 改为
  `adapter._resolve_skill_dirs`（避免假 patch `get_agent_skills_dir`）。
- 未覆盖：真实多进程 prepare→chat 端到端往返；依赖集成环境。

## 防错模式与资料同步

- **跨仓校验属性同步**：本仓漂移检测属性集 ↔ openjiuwen
  `_validate_evolution_rail_config`；上游加字段时本仓同步扩展（见上表）。
- **跨仓文案脆弱点**：兜底依赖 `Evolution rail config mismatch` 子串；上游改文案需
  同步本仓 except，或改为结构化错误码（后续跟进）。
- 本文件为 runtime 域 bugfix 留档；契约本身仍以代码 + 上游 openjiuwen 为准。

## 遗留与后续

1. 若 openjiuwen 提供结构化 mismatch error code / typed exception，改为按类型识别，
   去掉文案匹配。
2. 上游校验属性清单变化时，同步更新 `_active_evolution_rail_needs_rebuild` 与本
   文档属性表。
3. 专家团 / swarm 路径挂 evolution rail 时对 `enabled` 的门控与单智能体 Deep 路径
   不完全一致；企业隔离若需严堵团长 `team_signal_trigger`，另案跟踪（本 B 不覆盖）。
