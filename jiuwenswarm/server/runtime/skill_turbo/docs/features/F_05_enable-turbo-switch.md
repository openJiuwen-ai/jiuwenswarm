# 外部 turbo 技能 enable_turbo 开关：meta.json 声明式启停

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature |
| 编号 / slug | F_05 / enable-turbo-switch |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/turbo_package_loader.py`（`discover_external_turbo_skills`） |
| 归档日期 | 2026-10-23 |
| 关联 spec | S_02_skill-routing-and-registration.md（不变量 18 + 边界 case 17-19 + 修改检查单） |

## 背景

外部 turbo 技能按约定路径自动发现（`{skill_root}/{external_name}/turbo/turbo_codes/`
下有入口文件即注册，零技能名硬编码），此前只有"全有"语义：目录在 = 进
加速路由池 + 进工具描述清单。想临时下线某技能的加速面（灰度验证 / 故障
隔离 / 回退观察）只能移走目录——部署动作重，且破坏技能侧仓目录完整性。
需要一个 per-skill 声明式开关。

## 决策

`{skill_root}/{external_name}/turbo/meta.json` 新增 `enable_turbo` 字段
（技能级，一次判定管该技能全部 turbo_codes）：

- **语义**：缺省 / `true` / 任何非 `False` 值 = 启用（向后兼容——
  pptx-craft / xlsx-craft 等未声明字段的既有技能零行为变化）；显式
  `false`（严格布尔 `meta.get("enable_turbo") is False`）= 跳过；
- **单一跳过点**：判定在 `discover_external_turbo_skills` 发现阶段
  （`load_skill_meta` 之后即刻 `continue`，打 info 日志含技能名与 turbo
  目录），一个点同时决定两个下游——① 不注册进 environment 路由池：
  planner 无匹配 → SkillTurboNotHandled → interface 层降级为标准流程
  （skill_tool 仍可用，用户侧能力无损，只是不走加速）；② 不进
  `skill_turbo_tools._collect_skill_entries` 源二（源二本就迭代
  `discover_external_turbo_skills` 的产出，天然继承跳过）：工具描述与
  指南清单同步消失。

## 拒绝的方案

**引擎 config 侧配置（如 `react.skill_turbo.disabled_skills`）**——开关
应跟着技能资产走（技能侧仓持有 meta.json），引擎侧 config 会造成部署
配置与技能目录两处真相源漂移；且 config 同样要解决"描述清单 / 注册池"
双下游同步问题。拒绝。

**environment 注册后过滤**——晚于发现：描述清单（`_collect_skill_entries`
独立扫盘）与注册池两处都要复制过滤逻辑，且与 `_external_scan_cache` 缓存
语义纠缠。发现阶段跳过是最上游的单点。

## 已知接受面

- **生效时序**（[[S_02]] 边界 case 18）：注册侧**下一次扫描即刻生效，无需重启**——发现阶段每次重扫（meta.json 每次重读），技能从 discovered 消失/出现即改变 `_external_scan_cache` mtime 快照的 key 集合，缓存必 miss 走全量重扫；描述清单 ≤60s（`_entries_cache` TTL）。窗口内 LLM 仍可能按旧描述调用——planner 已无匹配 → SkillTurboNotHandled 降级标准流程，无功能损害；
- **进行中的 HITL 会话与开关的交互（[[S_02]] 边界 case 21）**：同进程
  resume（动态包仍注册于 sys.modules）不经重发现，翻转前已暂停的会话
  恢复时仍走加速通道跑完；跨进程 resume（进程重启后自愈重注册）消费
  同一份被开关过滤的 `discover_external_turbo_skills` 产出，被禁技能
  动态包无法重注册 → `exec(plan_code)` 抛 ImportError →
  PlanCodeLoadError → chat.error+complete 收口（[[S_01]] E6 /
  [[S_05]] 边界 case 7 既有降级链路，外层可走标准流程重做）。故障
  隔离语义下视为合理：被禁技能不再执行，在途会话显式失败优于静默
  跑完；新调用立即受开关控制；
- **严格布尔判定**（边界 case 19）：字符串 `"false"` / `0` / `null` 均
  不触发跳过——JSON 里必须写字面 `false`；
- **仅含 `enable_turbo` 的 meta.json 开关失效**（边界 case 17）：
  `load_skill_meta` 要求至少一个已知键（external_name / description /
  match_keywords）才返回非空 dict；关停配置必须至少携带一个已知键；
  该失效分支在根因处打 warning（文件路径 + 实际键列表），不留静默
  信号黑洞；
- **只管外部 turbo 技能**：内置 skill_codes 扫描路径不读该字段；
- **降级而非移除**：技能走标准 skill_tool 流程仍可用，下线的是加速面。

## 测试

`tests/unit_tests/server/runtime/skill_turbo/test_turbo_package_loader.py
::TestEnableTurboSwitch`（6 用例）：显式 false 跳过（含 info 日志断言）/
同 root 仅裁禁用技能、兄弟技能不受影响 / 缺省与 true 保持启用（向后兼容）/
仅含 enable_turbo 无已知键开关失效（边界 case 17，含根因 warning 的
caplog 断言）/ 非严格布尔值（`"false"` / `0` / `null`）不触发跳过
（边界 case 19）。

`tests/unit_tests/server/runtime/skill_turbo/test_turbo_package_loader.py
::TestPlanCodeSelfHeal::test_ensure_packages_honors_enable_turbo_disabled`
（边界 case 21）：跨进程 resume 自愈尊重开关——重发现被过滤、被禁技能
包不重注册（后续 exec ImportError → PlanCodeLoadError 收口链路由
test_executor_plan_code_import unresolvable 用例锁定，[[S_05]] 边界 7）。

`tests/unit_tests/server/runtime/skill_turbo/test_environment_external_turbo.py
::test_enable_turbo_switch_takes_effect_on_next_scan`：只改 meta.json 不动
.py 的开关翻转（下线 / 重新启用双向）在下一次 Environment 构造（扫描）即
生效——锁定"成员变化即触发缓存重扫"，无需重启。

## Follow-up（不在本次范围）

- 既有陈旧面（非本特性引入，[[S_02]] 边界 case 20）：不改变发现成员的
  meta.json 编辑（description / match_keywords / external_name）在注册侧
  复用缓存 Skill 对象，需 .py mtime 变化或重启才刷新；如需 meta.json
  热更新，评估把 turbo/meta.json mtime 纳入 `_external_scan_cache` 失效
  快照，涉及缓存结构变更，单独设计。
