# code 模式漏装技能链两条 rail：会话首轮脚本读不到 `react.skill_envs`

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_03 / code-adapter-missing-skill-rails |
| 关联模块 | `jiuwenswarm/server/runtime/agent_adapter/interface_code.py`（`_build_agent_rails` / `_FIXED_RAIL_NAMES`）；观测前缀：`interface_deep.py`（两个 skill rail builder 日志） |
| 归档日期 | 2026-11-26 |
| 关联 spec | S_05_agent-skill-rails.md（装配全景 / 热重载原位更新约定）、模块 AGENTS.md 铁律 5、7 |
| 发现渠道 | BUG20261124415174：OfficeClaw 1.7.0 多会话执行 `huawei-cloud-skill-submit` 的 `submit_skill.py --validate-only` 报 `SUBMISSION_FAILED: domainId 不能为空` |

## 复现场景

relay-claw 在 `react.skill_envs` 里下发 `huawei-cloud-skill-submit` 的 9 个键
（`DOMAIN_ID` / `DOMAIN_NAME` / `OFFICEACE_USER_NAME` / `OFFICEACE_IAM_*`），
新建 code 会话（`mode=code sub_mode=normal`）后，首轮让模型激活技能并在下一轮用
bash 执行：

```
python -S ...\office-claw-skills\huawei-cloud-skill-submit\scripts\submit_skill.py \
  --skill-dir <待提交目录> --validate-only
```

`submit_skill.py:238` 的 `_validate_value(domain_id, "domainId", 64)` 拿到空串，
输出 `SUBMISSION_FAILED: domainId 不能为空`（exit=1）。

会话中途触发过 configure（换模型 / 切模式 / 开关技能 / 重新登录）后故障消失——
`reload` 路径补建了 rail，属自愈假象。

## 根因

`JiuwenSwarmCodeAdapter._build_agent_rails()` 的固定 rail 清单只覆盖 code 域
（Lsp / ProjectMemory / CodingMemory / 权限 / 上下文 / 事件流等），**从未装配**
`SkillCredentialInjectionRail` 与 `SkillActiveStateRail`：

- 没有 `SkillActiveStateRail` → 会话里 `active_skill` 不落盘；
- 没有 `SkillCredentialInjectionRail` → `react.skill_envs` 没有任何注入通道，
  bash 子进程 env 里没有 `DOMAIN_ID`。

两处装配缺失自 `0b2bb91eb`（2026-08-26）引入技能 rail 起即存在，agent 模式
（`interface_deep._build_agent_rails` 的 `insert(0/1)`）一直正常，所以只在 code
会话暴露。基类 `_get_current_agent_rails`（reload 路径）会补建两条 rail，因此
故障仅在「新会话 + 未中途 configure」时稳定复现。

判定证据（服务日志）：code 会话构建日志里没有
`SkillCredentialInjectionRail create success`、没有 `[SkillCredentialInjectionRail] gate:`
/ `injected env keys=`、没有 `[SkillActiveStateRail] activated`。

## 修复口径

1. `_build_agent_rails()` 在固定清单后 `insert(0/1)` 两条 rail，均传
   `{"config": config}`（`react.skill_envs` 在 runtime config，不在 `config_base`）：
   - `_skill_credential_injection_rail` → `_build_skill_credential_injection_rail`
   - `_skill_active_state_rail` → `_build_skill_active_state_rail`
2. `_FIXED_RAIL_NAMES` 补两个名字：`modes.code.rails` 误配时只跳过，不走动态重复注册。
3. **不挂 `SkillSleepRail`**：`_reconcile_evolution_rails` 每次 reload 主动停用演进链，
   休眠训练属演进能力。
4. **不改 `resources/config.yaml`**：`_RAIL_BUILD_NAMES` 没有这两个名字，配在那里只会
   被 unknown 分支跳过。
5. **不改 `coalesce_skill_envs`**：现行「incoming 非空即整体覆盖（含显式空串）」是
   `test_catalog_clear_with_skill_key_wins` / `test_config_catalog_clear_replaces`
   钉住的撤销凭据契约；改成按 key 合并会让 relay-claw 少发 key 时旧凭据残留。
6. 顺带把两个 builder 的日志前缀从硬编码 `[JiuWenSwarmDeepAdapter]` 改成
   `type(self).__name__`，否则 code 会话修好后仍打 DeepAdapter，会再次误导定位。

## 验证

- `tests/unit_tests/agentserver/test_code_adapter_skill_rails.py`（先红后绿）：
  两条 rail 位于装配列表前两位、复用父类 builder、params 携带 runtime `skill_envs`、
  `_skill_sleep_rail` 不挂、`_FIXED_RAIL_NAMES` 含两名字。
- 连带回归：`test_code_adapter_progressive_tools.py` / `test_code_adapter_context_overflow_recovery_rail.py`
  / `test_code_adapter_a2a_outbound_rail.py` / `test_skill_credential_injection_rail.py`
  / `test_skill_active_state.py`（227 passed, 2 skipped）。
- 手工：code 会话未激活直跑 → `[SKILL_GATE]` 拦截；先激活再跑预检 → `status: validated`，
  日志出现 `injected env keys=[DOMAIN_ID, ...]`。

## 已知遗留

- **凭据全空快照**仍会注入空串，症状以同样文案复现（rail 存在但值为空）。归 relay-claw
  快照口径；jiuwenclaw 侧可选加固：`SkillCredentialInjectionRail` 对 active 技能
  「全部 value 为空」fail-closed 并给 gate 文案（未纳入本次修复）。
- **team 子模式**由 `agents/swarm/providers/` 装配，`code_rails.py` 目前也不含这两条
  rail；本 bug 现场是 `sub_mode=normal`，team 场景需单独确认。
- `SkillActiveStateRail` 激活未落盘时 `FileSkillGrantStore` 静默 `pop` 无日志，待复现后补告警。
