# Agent Profile 模式（normal / flash）本地启动配置

JiuWenSwarm sidecar 支持两种 agent profile 模式，一个 sidecar 进程可同时服务两种模式的 session，无需重启：

- **normal**：完整 rail 集合，`enable_task_loop=true`（默认行为，不做过滤）。
- **flash**：精简 rail 集合（按白名单保留少量核心 rail），`enable_task_loop=false`，`evolution.skill_create/review_trigger=false`。适合追求轻量、低开销的单轮交互。

模式解析优先级（从高到低，命中即返回）：

1. 请求信号 `params.agent_kind` / `metadata.agent_kind`（由 relay 透传，stock relay 不发此字段）。
2. `agent_profiles.breed_map[agent_id]`（按 agent_id 映射）。
3. `agent_profiles.default`（**配置写死的默认值**；前两者都未命中时回退到它）。
4. 均无 → 不激活任何 profile，使用全局 `react` 配置。

> 第 3 项 `default` 是**纯配置、零 relay/前端改动**即可让 sidecar 默认进入 flash 模式的关键。stock relay 不透传 `agent_kind`，故 `default` 是本地不改动 relay 代码时的唯一途径。

---

## 本地启动 flash 模式需要做的事

### 1. 确认 sidecar 代码已含 `default` 来源

`_resolve_agent_kind`（`jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`）读取 `agent_profiles.default` 的逻辑需已存在。该改动在 `feat/per-session-agent-profiles` 分支。

### 2. 改用户目录的 config.yaml

用户目录配置覆盖 `resources/config.yaml`（详见 `user-config-overrides-resource`）。改**用户目录**那份：

路径示例（Windows，dolores 部署）：

```
C:\Users\<你>\.office-claw\.jiuwenclaw-dolores\config\config.yaml
```

在顶层 `agent_profiles:` 段加一行 `default: flash`：

```yaml
agent_profiles:
  default: flash          # ← 加这一行：默认进入 flash 模式
  normal:
    react:
      enable_task_loop: true
      evolution:
        skill_create: false
        review_trigger: false
  flash:
    react:
      enable_task_loop: false
      evolution:
        skill_create: false      # 必须与 review_trigger 同为 false，否则触发 force-revive
        review_trigger: false
    rails:
      keep:
        - _runtime_prompt_rail
        - _response_prompt_rail
        - _stream_event_rail
        - _task_planning_rail
        - _security_rail
        - _heartbeat_rail
        - _subagent_rail
        - _permission_rail
        - _context_processor_rail
        - _ask_user_rail
        - _filesystem_rail        # SysOperationRail：init() 注册 ReadFile/WriteFile/EditFile/Glob/ListDir/Grep/Bash 工具，丢了=无文件/shell 能力
        - _progressive_tool_rail   # ProgressiveToolRail：系统提示词中的渐进式工具引导/导航
```

- `default` 取值：`flash` → 默认精简模式；`normal` → 默认完整模式；留空或 `null` → 不激活 profile（使用全局 `react`，等价 normal 行为）。
- `default` 是**最低优先级回退**：若某 session 的请求带了 `agent_kind` 或命中 `breed_map`，则覆盖 `default`。因此设了 `default: flash` 后，仍可按 session 单独切回 normal。

### 3. 确认环境变量未强制覆盖

`evolution.skill_create` / `evolution.review_trigger` 有进程级 env 覆盖（优先级高于 config_base）。若设了 `SKILL_CREATE=true` 或 `EVOLUTION_REVIEW_TRIGGER=true`，会触发 force-revive 把 `enable_task_loop` 强拉回 `true`，flash 失效。本地启动前确认这两个 env 未设或为 false。

### 4. 重启 sidecar 并验证

重启后，检查 sidecar 日志（`service_default/.logs/agent_server.log`）：

```
[JiuWenSwarmDeepAdapter] profile=flash dropped rails: ['_skill_credential_injection_rail', ... ]
```

出现 `profile=flash dropped rails: [...]` 即 flash 已激活。

> **铁证**：sidecar 启动时的 prewarm 会话（无任何请求信号）若打印 `profile=flash`，证明 `default: flash` 纯配置生效，不依赖 relay/前端。

---

## ⚠️ 游离 rail：keep/drop 白名单管不到的 rail

`rails.keep`/`drop` 白名单**只管 `_build_agent_rails` 那张 31 条 `_RailBuildInfo` 表**。有些 rail 不在这张表里、走"另一条注册路径"，profile 的 keep/drop 完全碰不到它们，必须用各自的门控开关单独关：

| rail | 注册路径 | 门控开关 | flash 处理 |
|---|---|---|---|
| SkillEvolutionRail | `_reconcile_evolution_rails` → `configure_skill_evolution_runtime` | `evolution.skill_evolution`（`get_evolution_enabled` 读） | `false` 关掉 ✅（已配） |
| ContextAssembleRail | `_update_agent_rails` 直接 `register_rail` | agent mode（无 config 开关） | **保留**——核心必需 rail，注入 workspace 目录/context/工具段到 system prompt，关掉会破坏 agent |
| SkillCreateRail | 单独 `_build_skill_create_rail` | `skill_create` + `enable_task_loop` | `skill_create:false`（已配）+ `task_loop:false` 双关 |
| TaskCompletionRail | openjiuwen framework auto-inject | `enable_task_loop=true` | `task_loop:false` 自然不注入 ✅ |
| ObservabilityRail / AgentTraceBindingRail | framework standing rail（无条件） | 无 | 不可过滤（框架级行为） |

**SkillEvolutionRail 是最容易踩的坑**：它由 `react.evolution.skill_evolution` 门控，**不是** `skill_create`/`review_trigger`。flash profile 只设后两者为 false 时，SkillEvolutionRail 仍会以 `signal_trigger=True` 注册（日志会打印 `SkillEvolutionRail configured`）。必须显式设 `evolution.skill_evolution: false` 才真正关掉。

**验证 SkillEvolutionRail 已关**：重启后日志里 `SkillEvolutionRail configured` 行应消失（`grep -c` 返回 0）。

---

## ⚠️ 踩坑：新 config key 必须同时进模板，否则重启被 prune

sidecar 启动会执行 config 迁移 `migrate_config_from_template`（`jiuwenswarm/common/config.py`），做三方合并：**`resources/config.yaml` 模板是结构权威，模板里没有的 key 会被当废弃字段剪掉**（`_deep_merge` 只迭代模板 key，user-only key 直接丢弃；迁移还会 `yaml.safe_dump` 重写文件，注释一并清除）。

因此：

- 若 `default` / `breed_map` 只写在**用户目录**而**模板里没有对应 active key**，则 sidecar 首次重启后该字段被剪掉，flash 静默失效。
- 修法：新 key 必须**同时**作为 active key（不能只注释）加进 `resources/config.yaml` 模板。模板放中性默认值（如 `default: null`、`breed_map: {}`），用户目录放实际值。迁移合并时 user 值胜出（`_deep_merge` 中 `result[key] = user[key]`），且首次合并后 `merged == user_data` 不再写盘，配置稳定存活。

当前模板已含 `default: null`（中性，老部署行为不变），所以用户目录加 `default: flash` 即可稳定生效。

---

## 相关

- Profile 机制实现与端到端验证：见 memory `sidecar-per-session-profile-verified`。
- relay 侧 `agentKind` 透传链（per-session 动态覆盖，非本地默认）：见 memory `relay-agentkind-chain-implemented`。
