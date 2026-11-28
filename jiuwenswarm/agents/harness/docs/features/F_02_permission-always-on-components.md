# F_02 permission always_on_components：总开关关闭时的守卫底线

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature |
| 编号 / slug | F_02 / permission-always-on-components |
| 日期 | 2026-11-27 |
| 关联 spec | [[S_17]]（N1 改写 + 新增 N23） |
| 关联仓 | agent-security（零改动）、agent-core（`tool_security_rail.py` keep_keys 补键）、relay-claw（零改动） |

## 动机

relay-claw 前端开启「完全权限 / 关闭审批护栏」（`PATCH /api/config/relayclaw/security` →
wire RPC `permissions.enabled.set`）后，jiuwenswarm 的 `permissions.enabled` 落盘为 false。
原架构下 `build_permission_rail` 对 `enabled=false` 直接返回 None，权限 rail 不装配，
agent_shield（agent-security 仓）完全不参与工具判定——包括 hostguard（拦截杀宿主进程、
worktree 破坏等 shell 命令）。后果：用户关闭审批即失去全部宿主守卫底线，高危命令零拦截。

agent-security 引擎本身已具备 always-on 能力（`engine.check_permission`：`enabled=false`
时仅 `always_on_components` 白名单内组件继续判定，其余快速 ALLOW），但 jiuwenswarm /
agent-core 全链路从未传入该键（改前全仓 grep 零出现），且装配层短路使该配置永远无效。

## 决策

1. **总开关裁决下沉 security 引擎**：`enabled` 不再门控 rail 装配——`build_permission_rail`
   删除 enabled 短路，disabled 时照常构建并发布 `enabled:false + always_on_components`，
   引擎对白名单外工具快速 ALLOW（进程内调用，开销可忽略）。
2. **always_on_components 走 yaml 配置键**，三份模板默认 `["host_guard"]`：
   `resources/config.yaml`、`config.team.distributed.leader.yaml`、
   `config.team.distributed.teammate.yaml`。键必须写入模板——用户 yaml 独有键会在
   `_load_permissions_from_yaml` 的 deep-merge 中被丢弃。用户显式给 list 即整体替换白名单；
   显式 `[]` 表示主动关闭底线。
   **缺键兜底播种**：企业模板 body 由部署侧下发，旧模板可能未携带该键——
   `build_permission_rail` 在 disabled 且缺键时播种 `["host_guard"]`，保证底线跨配置源
   成立；非 list 值（YAML 误配标量）与缺键/null **同向兜底播种**并告警（引擎对
   非 list 静默置空，只告警透传会让字符级误配重新打开底线缺口——失效方向必须
   与缺键一致）；list 内封闭集外未知名告警（宿主侧 `_ALWAYS_ON_COMPONENT_NAMES`
   封闭集、strip 归一比对——引擎会静默跳过未知名，拼写错误会让底线悄悄收窄）。
   **快照归一**：`_get_permissions_snapshot` effective 分支在 persist 合并
   （`_persist_allow_always` 以快照为 base_cfg 整体 update_config）等刷新点被
   重新消费，返回前同源兜底（检视意见 1）——否则配置源缺键时构建期播种被无键
   快照冲掉、底线静默失效。
   **挂起例外（检视意见 3）**：spawn_fallback headless 子代理的
   `_suspend_subagent_permission_rails` 置 enabled=False 时显式清空
   `always_on_components`——挂起契约 = 子代理完全不参与审批（S_03 N13），保留
   底线会让 headless 上下文被重新 DENY/ASK（ASK 无人作答 → RuntimeError）；
   显式 [] 把"全放行"裁决交回引擎，与用户总开关路径刻意不同。
3. **keep_keys 三处补键**：agent-core `tool_security_rail.update_config`、jiuwenswarm
   `_persist_global_permissions_to_yaml`、`common/config._PERMISSIONS_OVERRIDE_KEEP_KEYS`
   （启动 override 清理链）均登记 `always_on_components`（[[S_17]] N23）——否则稀疏
   快照发布 / persist 写盘 / 启动清理会静默抹掉白名单。
4. **meta-tool 展开契约无论 enabled 与否注册**（幂等）：always_on 组件的判定同样需要
   invoke_tool 等转发器展开到内层真实调用。
5. **always_on 命中 ASK 时照常弹卡**：hosted confirm / interrupt 链路无 enabled 隐藏门
   （已核实），disabled 下 ASK 照常走正常审批交互。

## 拒绝的方案

- **host_guard 独立开关（与 permissions.enabled 并列的布尔）**：多出一个配置面，且
  relay-claw 的「完全权限」语义要求单开关——独立开关要么被前端忽略（失去意义），
  要么把语义分裂到两处。
- **rail 层硬编码 always_on 白名单**：不可配置，且白名单会绕过 keep_keys 语义
  （config 缺键时 rail 层强行注入，来源不可追溯）。
- **security 仓改动**：引擎已具备全部能力（enabled + always_on_components 判决、
  配置整体深拷贝校验），零改动即达目标——接入链路打通即可。

## 明确不纳入（已知不对称）

**team member 路径不纳入本次**：team member 权限 rail 有三层门控
（`harness/team/config_loader.py` `_resolve_enable_permissions` 的 AND 语义 →
`swarm/config_specs.py` RailSpec 条件 → `member_rails.py` 运行时防御门），第一层在
spec 装配层；放开需重构装配语义并牵动 TeamToolApprovalRail 互斥关系，且 ASK 呈现是
leader-mediated（`TeamApprovalOrchestrator` 转发 leader 审批），需单独设计。
`narrow_permissions` 已核实只改 tools 键、`always_on_components` 透传，将来纳入无阻塞。

## 行为变化点（转测关注）

1. **场景豁免链 disabled 下首次参与判定**：标准版等价现状（owner_scopes / 数字分身
   无配置 → None 回落主序 → 引擎 ALLOW）；**企业版**数字分身（N10）与 owner_scopes
   （N11）的拒绝分支在 enabled=false 下开始生效——场景守卫独立于总开关，属预期安全姿态；
   若产品要求「完全权限连场景守卫一起放」，追加方案是 `_permission_scene_hook` 入口加
   disabled 快查。
2. **deep adapter 热更**：`_update_permission_rail` 的 `elif enabled` 改为 `else`——
   disabled 也建轨；stale key-change 语义收敛为「重建成功后注销旧 key」。
3. **性能**：disabled 的 agent 从无 rail 变为建轨 + 每工具调用进引擎（无命中快速 ALLOW）。
4. relay-claw 零改动（PATCH 只 set enabled 键，always_on_components 持久保留在 yaml）。

## 验证

- jiuwenclaw：`test_permissions_meta_tool_expansion`（disabled 建轨 + 注册 + 发布断言）、
  `test_resource_config`（三份模板结构校验）、`test_deep_adapter_interrupt`
  （disabled 建轨 + stale 注销）、中断交互域回归 137 passed、flash/code/team 装配回归
  493 passed。
- agent-core：`test_permission_rail_manager`（keep_keys 保留 + disabled 下引擎判定透传）、
  harness 全量 987 passed。
