# B_11 — team 模式未装配 OfficeAceUserProfileRail，专家不遵守用户画像

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_11 / team-office-ace-user-profile-rail |
| 关联模块 | `jiuwenswarm/agents/swarm/providers/member_rails.py`、`jiuwenswarm/agents/swarm/config_specs.py`、`jiuwenswarm/agents/swarm/registry.py` |
| 修复日期 | 2026-11-26 |
| 关联 spec | 无（team 成员画像装配尚未单独立约；对齐既有 PersonalContext 成员装配模式） |
| 关联测试 | `tests/agents/swarm/test_office_ace_user_profile_member_rail.py`、`tests/agents/swarm/test_swarm_assembly.py`（共享名单计数）、`tests/unit_tests/agents/harness/team/test_team_accel.py`（名单断言） |

## 现象与影响

OfficeAce 经 `OfficeAceUserProfileRail` 注入的用户画像，在专家团（`mode=team` /
`code.team` / `team.plan`）会话中不被遵守。日志表现：

- adapter 层打印 `OfficeAceUserProfileRail create success` + `registered`（宿主
  `_instance` 注册成功）；
- team 成员 `BEFORE_MODEL_CALL` RailChain 无该 Rail；专家 LLM prompt 无画像段；
- 对照 flash/普通 agent：同 Rail 每轮正常执行并注入。

影响：所有专家团会话在画像启用时，成员感知不到跨端用户长期画像。

## 关联契约

无独立 memory/画像 team 装配 spec。修复口径对齐既有成员会话级 Rail 先例：
`swarm.personal_context`（`_COMMON_RAIL_NAMES` / `_CODE_RAIL_NAMES` +
`member_rails` provider）。不扩展遗留 `build_member_rails()`（PersonalContext
也不在该路径）。

## 根因与修复决策

**根因**：`OfficeAceUserProfileRail` 自 2026-09-22（`12be9fa719`）起只挂在单
agent adapter（deep/code，后 flash）的 `self._instance` 上；team 成员由 swarm
声明式规格装配，名单从未包含该 Rail。宿主仍 mode-independent 注册，造成「注册成功
但执行链缺失」的假象。属引入时覆盖缺口，非后续回归。

**修复**：

1. 新增 `swarm.office_ace_user_profile` provider，复用
   `get_office_ace_user_profile_config`；未启用返回 `None`（零残留）。
2. 将 `registry.OFFICE_ACE_USER_PROFILE` 加入 `_COMMON_RAIL_NAMES` 与
   `_CODE_RAIL_NAMES`（紧邻 `PERSONAL_CONTEXT`）。
3. registry 导出常量，供规格与测试引用。

**拒绝的方案**：

- 宿主 rail 扇出到成员：绕过 harness 规格，热重载/恢复难跟。
- 只挂 leader：teammate 仍不遵守画像，与「专家团要遵守」不完全对齐。
- 同步改 ExternalMemoryRail / 冷拉取去重：范围扩散，另单独立项。

## 回归证据与适用范围

- 单测：team / code.team / team.plan 成员规格含 `OFFICE_ACE_USER_PROFILE`；
  provider 禁用→`None`、启用→`OfficeAceUserProfileRail`。
- 未在本提交做真实 OfficeAce HTTP 联调；热路径仍读
  `~/.office-claw/users/<userId>/user-profile.md`，冷路径并发拉取风险与单 agent
  相同、乘以成员数（已知、不在本修复范围）。

## 防错模式与资料同步

会话级身份/画像类 Rail 接入单 agent 时，必须同步检查 swarm
`_COMMON_RAIL_NAMES` / `_CODE_RAIL_NAMES` + `member_rails` provider，勿只挂
adapter `_instance`。

## 遗留与后续

- 宿主 `_instance` 在 team 会话仍可能注册该 Rail（日志易混淆）；可选清理。
- 成员并发冷拉取去重（per-user fetch lock）可作 P1。
- ExternalMemoryRail 是否存在同类成员缺口需另评。
