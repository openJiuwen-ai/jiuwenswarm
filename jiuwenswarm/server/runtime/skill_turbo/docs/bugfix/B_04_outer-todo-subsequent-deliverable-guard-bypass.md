# fresh 守卫误拦多交付物后续调用：第二个 PPT 降级 pptx-craft 标准流程

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_04 / outer-todo-subsequent-deliverable-guard-bypass |
| 关联模块 | `jiuwenswarm/agents/harness/common/rails/task_execution_rail.py`（`_completed_items_before` 计算 + 绑定导出）；`jiuwenswarm/agents/harness/common/rails/stream_event_rail.py`（rebind 传参）；`jiuwenswarm/server/runtime/skill_turbo/context_binding.py`（第 9 通道）；`jiuwenswarm/server/runtime/skill_turbo/skill_turbo_tools.py`（ContextVar + 守卫放行） |
| 归档日期 | 2026-10-21 |
| 关联 spec | S_06_hitl-interrupt-resume.md（P5 清理触点表 + 守卫语义小节 + 边界 case 31 + 修改检查单 15）；S_09_outer-integration.md（4.2 通道表 8→9 + N1 计数）；S_02_skill-routing-and-registration.md（N17 串行契约的可执行前提） |
| 发现渠道 | 线上会话定界（2026-10-21 14:23 事件：同任务第二份 PPT 降级 pptx-craft 标准流程，见下） |

## 复现场景

线上事件链（2026-10-21 14:23，部署机 agent_server.log）：

1. **第一个 PPT turbo 成功**：turbo 走到成功路径，`node_artifacts` 按
   [[F_01]] 语义落盘保留（成功路径不清库——`clear_node_artifacts` 只有
   fresh_start 显式放行与 executor 开跑 stale 清盘两个调用方）。
2. **第二个 PPT fresh 调用被守卫拦**：外层 LLM 调 `skill_acceleration_exec`
   生成第二份 PPT，未带 `fresh_start=true` → `_fresh_call_artifacts_guard`
   检出上一交付物残留产物 → 机制性拦截（success=False + 产物摘要）。
3. **指南指令压过守卫出路**：部署版指南两条指令——"tool_result 提示
   『检测到上一轮留下的已完成产物』时不调用（加速通道）" + 失败必须转
   `skill_tool` 标准流程——使 LLM 放弃守卫文案给出的 `fresh_start=true`
   出路，直接降级 pptx-craft 标准流程。
4. **死锁结构**：守卫拦的正是能清掉守卫状态（残留产物）的动作——
   F_01 的会话级拦截语义无法区分"同任务盲目重跑"（要拦）与"多交付物
   清单推进的新交付物"（要放）。

## 根因

`_fresh_call_artifacts_guard` 的拦截语义是**会话级**的：会话内有产物记录且
未显式 `fresh_start=true` 即拦。多交付物场景（F_03 指南引导 todo_create 建
清单逐项交付）下，第一个交付物成功后的产物残留对第二个交付物是**陈旧数据**
（executor 开跑时 `_clear_stale_node_artifacts` 本就会清），守卫却把它当
"同任务重跑保护对象"拦截——把正常流程误判为盲目重跑。

判别信号缺失：守卫所在的工具层拿不到 todo 清单状态（todo.json 路径需
deep agent workspace 解析，属 rail 层职责），而工具层唯一可见的外层信号
（`get_current_task_id`）只表达"有绑定"，不表达"清单推进到了第几项"。

## 修复口径

新增第 9 个 ContextVar 通道 `outer_todo_completed_before`（早于当前绑定项
的已完成项数），四段接线（新增通道规范：bind 一段 + RESETTERS 一行）：

1. **rail 绑定点计算**（`task_execution_rail.py`）：
   `_completed_items_before(task_id) -> int | None`——迭代纪律与
   `_pending_items_after` 一致（剥 "todo:" 前缀 + `_todo_map` + index 元组
   排序 + `_TODO_DONE_STATUSES` 终态判定，completed/cancelled 均计）；
   `skill_acceleration_exec` 绑定分支导出第三个 extra。
   None=无绑定/项不在清单，0=绑定项之前无已完成项（首交付物/同任务重跑），
   ≥1=清单推进的后续交付物。
2. **StreamEventRail rebind**：读新 extra 传 `outer_todo_completed_before=`
   给 `bind_skill_turbo_context`（"共 8 个"注释同步 9）。
3. **context_binding**：`isinstance(int)` 才绑定产生 token（**0 是有效信号
   必须绑**，None 不绑）+ `_RESETTERS` 一行。
4. **守卫放行**（`skill_turbo_tools.py`）：检查置于守卫最顶部（免开 session
   快路径）、`not fresh_start` 条件下（fresh_start=true 的显式清库语义优先）
   ——`completed_before` 为 int 且 >0 → INFO 日志放行返回 None，**不在守卫
   内清库**（产物清理交 executor 开跑时 `_clear_stale_node_artifacts`，
   与 fresh_start 清库失败的兜底同序）。

配套关系：F_03 指南（串行契约）保证清单纪律存在（todo_create → 逐项
todo_modify → 再 exec），B_04 使该流程的后续项调用端到端可执行——指南给
行为、通道给信号、守卫给放行，三层缺一即回到 14:23 形态。

### 拒绝的方案

- **仅 `has_outer_todo` 即放行**：太松——同任务中断后绑定项重跑（in_progress，
  早前无已完成项）也会被放行，Stage 1 清盘丢失产物，正是 F_01 要防的原始
  故障。判别必须落到"清单推进"（早前有已完成项）而非"有清单"。
- **复用 `outer_todo_pending` 通道携带**：`_pending_items_after` 对清单末项
  返回 None——恰是多交付物最常见场景（两项清单的第二项），信号在最需要的
  位置缺失；且 pending 语义（停止提示条件化）与放行语义不同源。
- **以 pending count 判定**（"有后续未完成项 = 多交付物"）：同任务首项
  in_progress 时也有后续 pending 项，无法区分首交付物与后续交付物。
- **守卫直读 todo.json**：路径解析依赖 deep agent workspace 配置
  （rail 层实例态），工具层引入该依赖破坏分层；todo 状态归 rail 域，
  经通道传递是既有架构（S_09）的既定路径。
- **放行时守卫内显式清库**：executor 开跑时 `_clear_stale_node_artifacts`
  无条件清（S_06 P5 步 5），守卫内再清是重复动作且引入"守卫清了、执行
  没跑起来"的中间态窗口；交 executor 同序清理与 fresh_start 清库失败的
  兜底路径一致。

### 已知边界

- **无 todo 纪律的会话**（14:23 事件当时的部署形态：无 F_03 指南、模型未建
  清单）：completed_before=None → 守卫原语义完整保留，仍拦。B_04 单独不
  防该形态——它使 F_03 引导的流程端到端成立，两者配套部署。
- **绑定项手动标完成后重跑同项**：早前项已 completed → 放行（清单声明该
  交付物已收尾，其产物处置权交给清单推进语义）；中断重跑保护仅覆盖
  "绑定项之前无已完成项"的形态。
- **守卫读到的通道值是本次调用绑定快照**：rail 绑定分支在 before_tool_call
  计算，同调用内 todo 状态不回写通道（todo_modify 与 exec 串行契约下无
  竞态窗口，F_03 N17）。

## 测试

- `tests/unit_tests/server/runtime/skill_turbo/test_fresh_call_artifacts_guard.py`
  （新增 3 用例，先红后绿）：
  - `test_subsequent_deliverable_bypasses_guard_via_outer_todo`——本 bug
    复现（14:23 场景：产物残留 + completed_before=1 → 放行进 run_stream，
    守卫不清库）；实现前红（守卫拦截、降级路径复现）；
  - `test_first_deliverable_completed_before_zero_still_blocked`——
    completed_before=0 仍拦（F_01 保护面：首交付物/同任务重跑）；
  - `test_completed_before_none_still_blocked`——无绑定仍拦（守卫原语义
    保留，含 14:23 无清单形态）。
  - `_tool_env` 扩展 `completed_before` 参数：经真实 ContextVar 通道注入
    （非 patch getter），同时覆盖 set/get/reset 访问器。
- `tests/unit_tests/agentserver/rails/test_task_execution_todo_create_defer_start.py`
  （新增 2 用例）：
  - `test_completed_items_before_counts_earlier_done_items`——计数口径
    （completed/cancelled 计、in_progress 不计、"todo:" 前缀剥离、None/0
    语义区分）；
  - `test_skill_turbo_tool_call_exports_completed_before_extra`——绑定导出
    链（多交付物清单第二项 in_progress → 绑定 + extra=1）。
- `tests/unit_tests/server/runtime/skill_turbo/test_context_binding_channels.py`
  （修订）：7 通道计数勘误至 9（F_02 第 8 通道漏更一并补齐），全量绑定 /
  复位断言补 pending + completed_before 两通道，新增
  `test_outer_todo_completed_before_requires_int`（0 绑定 / None 不绑 /
  非 int 不绑）。
- 既有回归全绿：skill_turbo 模块 397 用例 + HITL/工具套件 + rails。
