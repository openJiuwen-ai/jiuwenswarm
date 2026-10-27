# 多交付物待办清单强制引导：指南层让 skill_turbo 流转到外层 todo

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature |
| 编号 / slug | F_03 / multi-deliverable-todo-guide |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/rails/skill_prompt_rail.py`（指南文本） |
| 归档日期 | 2026-10-21 |
| 关联 spec | S_02_skill-routing-and-registration.md（不变量 17 + 修改检查单"改指南规则文案"条目）；关联 bugfix [[B_03]]（串行收紧消除的竞态 + 机制层兜底登记清算）、[[B_04]]（fresh 守卫后续交付物放行——本指南流程第二项调用的可执行前提） |

## 背景与要修的故障

线上会话（单轮请求"李白+杜甫两份 PPT"）中，外层 LLM 连续两次 fresh 调用
`skill_acceleration_exec` 而**全程未建任何外层 todo**，最终交付总结一份被下游
分拣丢弃、一份过时补发错位（同一轮出现两条内容矛盾的双总结）。实证链：

1. 该轮 tool_use 分布：2 次 `skill_acceleration_exec`、10 次 `ask_user`、
   **0 次 todo 工具调用**；
2. 六次 skill_turbo 入口日志全部为 `outer todo active=False rebound=False
   outer_task_id=None … task events will be forwarded as-is`；
3. 两次调用各自生成整套 13-stage 内部流水线任务（李白
   `task_e6e8b01c…task_0ac895e8`、杜甫 `task_5b120195…task_20fd34db`），
   后者在前端任务槽位**整套顶替**前者。

架构缺口：入口的 outer_todo 感知分支（skill_turbo_tools 入口拦截：有活跃外层
todo 时跳过内部 task.* 事件、chat.* 归外层 todo 步骤 segment；交付总结的
CONTINUE_HINT 也只在 `outer_todo_pending` 多项时引导"先标完成再继续下一项"）
**全部以外层 LLM 已建 todo 为前提**。而注入指南只有条件句"若待办仍有
in_progress，先单独调 todo_modify 标 completed"——假设 todo 已存在，无任何
"多交付物必须先建"的引导。todo 建不建全凭模型自觉，本例模型没自觉，整条
外层集成路径休眠，多份交付的呈现退化为下游网关的实例局部缓冲 + 一次性替换
机制——正是跨 HITL resume 碎片化出故障的位置。

## 决策：指南层强制引导（建议 1）

在 `_build_skill_turbo_guide_text`（中英双语同步）增加：

1. **段落 1 例外链追加"同样例外"**：单轮需要多份交付物（即需要多次调用
   `skill_acceleration_exec`）时，必须先用 `todo_create` 建好清单再进入加速
   通道——以例外子句形式挂入，避免与"你的第一个工具调用必须是
   skill_acceleration_exec"直接矛盾；
2. **新增 📋 强制段**：todo_create 建清单（每份交付物一项）→ 对当前项
   skill_acceleration_exec → 成功后立即 todo_modify（action=update）标当前项
   completed + 下一项 in_progress → 清单未清空前继续下一项、**不要中途向用户
   做最终总结** → 全部完成后**一次性统一总结**；
3. **成功段尾部改流向**：原"标 completed，再向用户总结结果"改为"清单还有
   未完成项时继续下一项，全部完成后再向用户总结结果"（消除与多项场景的矛盾）；
4. **串行契约收紧（定界后追加，消竞态触发路径）**：定界发现同轮**并行**
   `todo_create` + `skill_acceleration_exec`（仅隔 8ms）会让入口 outer_todo 检测
   竞态误判（rail 的 `_ACTIVE_TASK_ID` 尚未设置）→ turbo Stage 快照顶掉
   taskProgress 槽位且续跑入口不再补发终态（[[B_03]]）。指南两处同步收紧
   （中英双语 + 段落 1 例外链与 📋 强制段双落点）：强制段流程总则改为
   "**串行推进：每一步都必须等上一步的工具结果返回后再发起下一步，严禁同批
   并行调用**"，流程步骤改"**等 `todo_create` 返回成功后**，对当前项调用
   skill_acceleration_exec""**等 `todo_modify` 返回成功后**再进入下一项"；
   段落 1 例外链追加"等 todo_create 成功返回后再进入（不要同一批并行调用）"。

   措辞红线：EN 段落 1 例外链用 "has returned successfully" 而非 "returns
   success"——后者会抢先命中结构对齐测试的段落标记（`returns success` 标记
   指向成功段），📋 段内 "after `todo_create` returns success" 在标记之后不冲突。

措辞全部对齐真实工具契约（openjiuwen `harness/prompts/tools/todo.py`）：
`todo_create` 第一项自动 in_progress、覆盖式建清单；`todo_modify action=update`
单次批量"当前项 completed + 下一项 in_progress"正是其推荐用法；与
`_PPT_DELIVERY_SUMMARY_CONTINUE_HINT`（"先将当前待办标记为完成（todo_modify），
再继续执行下一待办项"）和 todo_close_nudge 的"先单独调 todo_modify"措辞契约
保持一致。

## 拒绝的方案

**入口结构性兜底（建议 2，未做）**——检测同轮第二次 fresh `skill_acceleration_exec`
且 `has_outer_todo=False` 时返回引导性失败让外层补建 todo。拒绝理由（本次）：
需要新增"同轮第 N 次 fresh 调用"的会话级计数状态，且与 [[F_01]] fresh 守卫
（残留产物拦截 + fresh_start 放行）的判定交织，误伤面（如用户明确要求"再来
一份无关的"）需要额外语义区分；先落指南引导观察实效，机制兜底单独立项。

**relay-claw 侧修复**（总结缓冲线程化到轮级 / 末尾兜底浮出加闸 / 持久化对齐
replace 语义）——本次故障的直接根因在下游网关（jiuwenswarm 两次交付总结均
正确 emit），但属另一仓库的独立修复线，不在本特性范围；即便外层 todo 激活，
segment 内文本在下游的 taskRunAccum 仍是实例局部的，两侧修复互补不互替。

**指南硬编码 todo 工具参数示例**——不在指南里展开 todo_create/todo_modify 的
JSON schema 示例：工具描述已自带完整契约，指南复制会制造第二真相源（漂移
风险），只保留工具名 + action 语义级引用。

## 已知接受面

- **指南是引导性防线**：依赖 LLM 遵从。本例模型未建 todo 属行为层缺失，
  指南强指令（禁止 + 正确流程）是当前架构下最小侵入的矫正手段；机制性
  兜底已同批落地两层——[[B_03]] 的槽位登记清算（治已发生的泄漏）、
  [[B_04]] 的 fresh 守卫后续交付物放行（本指南流程的第二项 turbo 调用
  此前会被 [[F_01]] 守卫以"残留产物"误拦、降级标准流程——清单推进信号
  `outer_todo_completed_before` ≥1 时放行）；入口结构性兜底（防**无**
  清单纪律时的第二次 fresh 连跑）仍见 Follow-up。
- **todo 工具不可用的部署**：指南无条件引用 `todo_create`/`todo_modify`
  （沿用既有成功段的先例）；若部署在 `react.disabled_tools` 移除 todo 工具，
  指令指向不可用工具——现状未区分，接受（与既有措辞同等风险面）。
- **单交付物轮零行为变化**：新段落触发条件（多份交付物）不满足时无约束；
  段落 1 首调指令、暂停、选区排除等既有规则原文未动。
- **多一次 todo_create 往返**：多交付物轮首个动作变为建清单，比直接连跑多
  一次工具调用，换取全程交付归位与统一总结。

## 测试

`tests/unit_tests/server/runtime/skill_turbo/test_skill_prompt_multi_deliverable_todo.py`
（5 用例）：

1. CN 指南：例外链"同样例外" + 强制段五要素（建清单 / 每份交付物一项 /
   action=update / 不中途最终总结 / 一次性统一总结）；
2. EN 指南：同上英文镜像；
3. 成功段措辞契约回归：保留"先单独调 `todo_modify`"（todo_close_nudge 对齐），
   旧措辞"标 completed 后无条件立即总结"不得复活，新流向"全部完成后再总结"在场；
4. 中英结构对齐：6 个正文段标记出现顺序一致（新增段在"成功"与"暂停"之间）；
5. 串行契约（B_03 竞态收敛）：强制段"严禁同批并行调用"总则 + 两处"等返回
   成功后"（todo_create / todo_modify）+ 段落 1 例外链的"不要同一批并行调用"
   双语断言。

既有回归：`tests/unit_tests/agentserver/rails/test_task_execution_todo_close_nudge.py
::test_skill_turbo_success_hint_closes_todo_before_summary` 的四处断言
（先单独调 todo_modify / first call todo_modify alone / 两处 not in）全部保持通过。

## Follow-up（不在本次范围）

- 入口结构性兜底（建议 2）：同轮第二次 fresh 调用且**无**外层 todo 时的
  引导性拦截，需处理与 F_01 fresh 守卫的交织与误伤面（有 todo 的半边已由
  [[B_04]] 放行分支解决——无 todo 形态恰是 2026-10-21 14:23 事件的部署
  形态，守卫原语义保留是有意取舍：无清单信号时宁可拦截）。
- relay-claw 侧三项修复（缓冲状态轮级化 / 兜底浮出加闸 / 持久化对齐 replace）。
- 实效观察：多交付物轮的 todo 建档率（入口日志 `outer todo active=` 统计），
  作为是否升级为机制性兜底的依据。
