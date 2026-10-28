# fresh 守卫后续交付物放行过宽：同一交付物已成功后的盲目重跑漏过 F_01

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_05 / fresh-guard-same-item-blind-rerun |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/skill_turbo_tools.py`（模块级登记 `_SUBSEQUENT_DELIVERABLE_LAST_TODO_ID` + `_release_subsequent_deliverable` + `_fresh_call_artifacts_guard` 放行分支收紧 + `reset_subsequent_deliverable_registry`） |
| 归档日期 | 2026-10-21 |
| 关联 spec | S_06_hitl-interrupt-resume.md（P5 放行语义 + 边界 case 31 + 修改检查单 15）；S_09_outer-integration.md（4.2 通道表第 9 行消费方描述） |
| 发现渠道 | BUG20261021388603 检视意见 3 |

## 复现场景

多交付物清单流程（F_03 指南引导 todo_create 建清单、逐项 todo_modify + exec）：

1. **第二个 PPT 首次 fresh 调用**：`todo=[PPT1 completed, PPT2 in_progress]`，
   早于绑定项存在已完成项 → `outer_todo_completed_before` = 1 → [[B_04]] 快路径
   放行 → executor 开跑 → PPT2 turbo 成功落盘 `node_artifacts`（成功路径不清库）。
2. **同绑定项重复调用**：绑定项仍停留在 PPT2（未 todo_modify），LLM 未携带
   `fresh_start=true` 再次调用 `skill_acceleration_exec`（同项盲目重跑 / 网关
   重试）。
3. **F_01 漏拦**：`completed_before` 是绑定时刻算出的静态计数，绑定项存续期内
   恒为 1 → [[B_04]] 放行分支再次放行 → executor 开跑 stale 清盘 → **清掉刚交付
   的 PPT2 产物**，从 Stage 1 全量重跑。

后果：F_01 对"同任务盲目重跑"的保护，在面对清单第 2+ 项时失守——同一交付物
已成功的产物在无显式 `fresh_start=true` 的情况下被静默清盘重跑。

## 根因

[[B_04]] 放行信号 `outer_todo_completed_before` 只表达"清单推进到了第 2+ 项"
（静态计数，绑定项存续期内不变），不表达"本次调用是不是同一交付物的重复
调用"。放行分支只看该计数，把两种本质不同的场景混为一谈：

- **清单推进到新交付物**（绑定项从 PPT1 切换到 PPT2）→ 应放行；
- **同一交付物已成功后的盲目重跑**（绑定项仍停在 PPT2）→ 应拦（F_01 保护面）。

判别所需的"绑定项是否切换"是**跨请求状态**：需要记住上一次放行时绑定的
todo id，与本次绑定 id 比对。B_04 的 9 通道均为请求级 ContextVar，无法承载
"上次调用"的跨请求记忆。

## 修复口径

新增模块级跨请求登记簿 `_SUBSEQUENT_DELIVERABLE_LAST_TODO_ID`（键 = 守卫的
sid 口径，值 = 上次放行时 `get_current_task_id()`；上限淘汰 4096，同
`task_slot_guard` 先例），配套 `_release_subsequent_deliverable(sid,
current_todo_id) -> bool`：

- 拿不到会话键或绑定项 id（含测试桩 / 退化态）→ 退回旧语义无条件放行；
- 绑定项相同（同项盲目重跑）→ 返回 False，调用方落回产物检查（命中残留即拦 +
  提示 `fresh_start=true`）；
- 绑定项不同 / 首次 → 放行并登记（返回 True）。

`_fresh_call_artifacts_guard` 放行分支由"`completed_before > 0` 即放行"改为
"`completed_before > 0` **且** `_release_subsequent_deliverable(sid,
get_current_task_id())`"；放行才 return None，否则落回下方产物检查路径。新增
`reset_subsequent_deliverable_registry()` 供测试隔离（生产零调用）。

不新增 ContextVar 通道（仍 9 通道）：切换判定所需的"上一次绑定的 id"是跨请求
状态，恰是 ContextVar 承载不了、而模块级单例（同 task_slot_guard /
hitl_dedup）能承载的形状。

### 拒绝的方案

- **放行前开 session 读产物、比对产物"所属交付物"**：需要给产物落盘写 owner
  todo id（executor 层拿不到外层 todo id，需跨层穿透），且放行分支必须开
  session 读产物——丢掉 [[B_04]] 的免开 session 快路径。登记簿方案无跨层穿透、
  不丢快路径。
- **对 `completed_before > 0` 一律不放行**：回到 [[B_04]] 修前的 14:23 形态
  （第二个 PPT 降级 pptx-craft 标准流程），多交付物清单流程端到端断裂。
- **用 ContextVar 登记切换**：切换是跨请求状态（上一调用的绑定 id），ContextVar
  是请求级、随请求结束失效，承载不了"上次调用"；必须模块级单例。

### 已知边界

- **同一交付物失败后的无产物重试不误伤**：同绑定项落回产物检查后，若该交付物
  此前失败未落产物（无残留）→ `load_node_artifacts` 为空 → 照常放行，合法重试
  不受影响。
- **登记键为 sid 口径**：同一父会话跨任务复用且 todo id 碰撞时，仅导致一次
  多余的产物检查兜底（由真实产物状态决定放行/拦截），不产生错误放行。
- **无绑定 id / 测试桩**：退回旧语义无条件放行，与生产不变量
  （`completed_before > 0` ⟹ 绑定存在）不冲突，是防御性回退。

## 测试

- `tests/unit_tests/server/runtime/skill_turbo/test_fresh_call_artifacts_guard.py`
  （新增 2 用例 + autouse reset + `_tool_env` 扩展 `current_task_id` 参数）：
  - `test_subsequent_deliverable_same_item_rerun_still_blocked`——本 bug 复现
    （completed_before=1 + 同 current_task_id="todo:ppt2" 两次调用：首次放行、
    第二次落回产物检查命中残留 → 拦 + `fresh_start=true` 提示）；
  - `test_subsequent_deliverable_bound_item_switch_releases`——绑定项切换
    （"todo:ppt2" → "todo:ppt3"）两次均放行，确保 B_05 收紧不误伤清单推进；
  - autouse `_reset_subsequent_deliverable_registry`——登记簿是模块级跨请求
    单例，键含固定 `_SID`，测试间必须隔离。
- 既有 [[B_04]] 3 用例（completed_before None / 0 / 1 + current_task_id=None
  回退）仍绿；既有回归全绿。