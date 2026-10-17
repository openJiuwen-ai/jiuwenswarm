# fresh 调用守卫：cancel 保留产物的消费方恢复

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature |
| 编号 / slug | F_01 / fresh-call-artifacts-guard |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/skill_turbo_tools.py`（守卫 + 工具参数）、`stream_event_rail.py`（占位文案识别）、`planner.py`（rule 7 清理） |
| 归档日期 | 2026-10-16（同日修订：机制性拦截 + fresh_start 显式放行） |
| 关联 spec | S_06_hitl-interrupt-resume.md（P5 清理触点表 + 边界 case 29/30 + 检查单 15） |

## 背景与要修的故障

bbc64b25c「收敛 HITL 中断恢复主路径」删除了旧三层兜底（prepare hook 注入产物摘要 +
hint 守卫 + 中断摘要键），其中兜底被删属有意收敛，但**连带删除了 cancel 保留
`node_artifacts` 的唯一消费方**——S_06 P5 步 5 明确"cancel 保留（供降级 DeepAgent
复用）"，消费方被删后该语义悬空：

用户停止 turbo PPT 生成（cancel 保留 node_artifacts、清 resume_ctx）→ 说"继续"
→ 状态机终态（cancelled）使下一条消息进全新 invocation → LLM fresh 重调
`skill_acceleration_exec` → 新 executor `_clear_stale_node_artifacts` 清盘 →
从 Stage 1 全量重跑，已完成 Stage 的工作丢失。

该故障未被当时的测试矩阵捕获的原因：旧兜底恢复的从来不是"turbo 断点续跑"，
而是"停止后不丢工作成果的合理继续路径"（引导 LLM 走 skill_tool / read_file 基于
产物文件续作）；三层兜底删除后的测试只验证了"取消不残留状态导致死循环"（P0
修复目标），未覆盖"取消后产物仍有消费方"这一半语义。

## 修订：一次性拦截被隔层重试击穿（2026-10-16 当日线上日志复盘）

首版守卫采用"一次性拦截"（拦截即清库 + sid 兜底集合）。实测日志
（宿主侧运行日志，turbo ppt 暂停后继续）显示其被**隔层重试**击穿：

1. 外层 fresh 调 turbo → 守卫拦截（预期内）✅；
2. 外层遵循引导转向 skill_tool 标准流程，派发 deep-researcher 子代理 ✅；
3. **子代理**收到"继续 + 步骤清单"式任务输入，据暂停占位文案"回答后将自动继续
   执行"的暗示推理出"re-invoke skill_acceleration_exec with a '继续' query to
   resume"，重调工具——子代理看不到外层的拒绝 tool_result，一次性语义已消费，
   守卫放行 → **全新 turbo 从 p0 重跑 13 任务** ❌。

根因三因素：① 一次性语义只对"被拒的那个 agent"可见，跨 agent 失效；② 工具暴露在
子代理工具集中；③ 暂停占位文案携带"重调即续跑"的机制暗示。

## 修订二：占位文案 v2"无条件禁止行动"诱导 cancel 后被动空转（同日第二次实跑复盘）

A+B 落地后的实跑自验暴露新故障：turbo HITL 暂停 → 用户作答 resume → 续跑中用户点
停止（cancel 清 resume_ctx）→ 用户说"继续" → 外层 LLM **什么工具都没调**，回复
"PPT 正在继续生成中，无需其他操作"后正常收尾——前端显示任务完成但实际无任何任务
在跑（幻觉式空转）。fresh 守卫全程未触发（LLM 未调工具，"不作为"形态的失败不在
守卫覆盖面内）。

根因：v2 占位文案"请勿再次调用 skill_acceleration_exec，**也无需其他操作**"是
**相位盲**的——写入时机是 HITL 暂停，但躺在会话历史中被所有后续相位的 LLM 读到。
cancel 已清断点、不存在自动恢复的相位里，"无需其他操作"字面成立且被 LLM 严格执行。

修复（v3 文案）：按用户意图分叉，替代无条件禁止——

- 作答 / 暂停仍有效 → "系统将自动恢复执行，请勿重新调用本工具**来恢复执行**"
  （保留 v2 要修的子代理"重调即续跑"阻断）；
- 非作答消息（停止/取消/调整/新任务）→ "说明本次暂停已被终止、不存在自动恢复，
  **必须**按该消息的用户意图正常处理；若需继续原任务，可基于会话中已生成的产物
  文件继续"（解除被动等待）。

**设计教训（二次）**：写入历史的一次性指令文案必须假设自己会在任意后续相位被
读取——要么分叉覆盖所有相位，要么不写指令性内容。历史文案 v1/v2 保留为
`_SKILL_TURBO_HITL_PLACEHOLDER_LEGACY` / `_LEGACY_V2` 供 stream_event_rail 识别
已持久化的 tool_msg。

## 决策（修订版）：机制性拦截 + fresh_start 显式放行（A+B）

LLM fresh 调用 `skill_acceleration_exec` 时（`resume_ctx is None` 分支、实例构造前），
守卫用 isolated session 读取 `__skill_turbo_node_artifacts__`：

- **有产物记录且未携带 `fresh_start=true`** → 返回 `success=False` + 产物摘要
  （含文件路径）+ 引导文案（继续走 skill_tool / read_file；明确全新任务则携带
  `fresh_start=true` 重调）。**拦截不清库**——产物记录是 P2-2 hash 继承与后续
  拦截的共享数据源，任何调用者（外层 / 子代理 / 其它入口）重复调用均持续被拦；
- **`fresh_start=true`** → 显式全新任务：清空产物记录后放行（清库失败仅 warning，
  executor `_clear_stale_node_artifacts` 仍是 fresh run 的机制性兜底）；
- 全链 fail-open：session 打不开 / 读库 / 清库任何异常均放行 fresh 执行。

配套修订：

- **工具 schema** 新增 `fresh_start: bool = False` 参数，docstring 明确"仅当用户
  明确要求与已有加速产物完全无关的新任务时才设为 true"；
- **暂停占位文案**（`_SKILL_TURBO_HITL_PLACEHOLDER`）按用户意图分叉（见"修订二"）：
  作答 → 自动恢复、勿重调本工具恢复；非作答消息 → 暂停已终止、必须按意图行动、
  可基于产物继续。历史文案 v1/v2 保留为 `_LEGACY` / `_LEGACY_V2` 供
  `stream_event_rail` 识别已持久化的历史 tool_msg；
- **planner rule 7 清理**：`__skill_turbo_prior_artifacts__` 字段注入已随 bbc64b25c
  删除，规则悬空（planner 实际推理"field doesn't exist"后放行路由），删除并在
  prompt 内留历史注；残留产物的 fresh 拦截由本守卫承接。

选工具层而非其它层的理由：fresh turbo 调用是**唯一**会触发 executor 清盘重跑的
动作，拦这一个点不多不少；无需意图判断（LLM 用行动表达意图：不带参数=可能误调，
带 fresh_start=true=显式全新任务）；机制性拦截对跨 agent 重试免疫——不需要被拒
方"记住"任何东西，数据记录本身就是状态。

## 拒绝的方案

**prepare hook 注入 supplementary_info（旧方案 B）**——新消息进入 runtime 前检测
产物残留并注入提示。拒绝理由：

1. 触发条件先天难写：hook 时机在意图判断之前，"有 artifacts 就注入"会污染停止后
   开全新任务的首条消息；"判断意图再注入"回到旧方案意图判断的老坑（正是
   bbc64b25c 要删的东西）。
2. 与 P2-2 hash 继承资源冲突：旧实现防重复注入靠"注入后清库"，但清库会废掉
   `_inherit_node_artifacts_for_replay` 依赖的数据；不清库则需再造哨兵/代际
   防重复——重新制造刚被拆掉的复杂度。
3. 时机过早：注入的摘要在澄清/路由长流程后沉底，不是 LLM 决策时的注意力焦点。

拿回旧三层守卫同样被拒：其 hint 守卫同样是一次性语义，隔层重试场景下同样放行
（见修订背景时序），且 prepare hook 覆盖不到子代理。

**turbo 断点续跑（方案 C，未做）**——停止时存"停止检查点"（plan_code + inputs +
task_states），继续时走 resume_stream 跳过已完成 stage。这是真·断点续跑，但需要
动 cancel 语义边界（区分"停止"与"放弃"，P0 死循环修复正是靠 cancel 清 resume_ctx）
且须按 S_06 检查单第 1 条跑全场景矩阵，单独立项。本守卫落地后，方案 C 的守卫
代码自然退化为兜底，两者不冲突。

**子代理工具面收口（移除子代理工具集中的 skill_acceleration_exec）**——作为纵深
防御后置：需先确认是否存在子代理合法调用 turbo 的场景，本次不做。

## 已知接受面

- **成功完成后残留产物同样被拦**：turbo 成功路径不清 node_artifacts（沿用现状），
  下次 fresh 调用（不带 fresh_start）会被拦，文案措辞中性（"已有产物"而非"被
  中断"）。多一轮 LLM 往返，换取"不要盲目重建已存在完整文件"的引导，接受。
- **守卫是引导性防线**：依赖 LLM 理解拒绝文案（修订版由机制性拦截兜底——即便
  LLM 不理解文案反复盲调，产物记录仍在、持续被拦，不会击穿到清盘重跑）；executor
  `_clear_stale_node_artifacts` 仍是 fresh_start=true 路径的机制性防线。
- **全新任务多一次往返且需 LLM 学会带参数**：拒绝文案明确指示携带 fresh_start=true
  重调；若 LLM 始终学不会带参数，守卫会持续拦截（fail-closed 引导），不会误清盘。
- **resume 路径不受影响**：守卫仅在 `resume_ctx is None` 时执行，HITL 卡片作答的
  hash 继承（P2-2）零改动。

## 测试

`tests/unit_tests/server/runtime/skill_turbo/test_fresh_call_artifacts_guard.py`
（6 用例，复现原故障 + 隔层重试场景）：

1. 残留产物 → 拦截：success=False + 摘要（节点/文件路径/技能名）+ 引导（含
   fresh_start=true 指示），不进 run_stream，**不清库**；
2. **重复 fresh 调用（子代理隔层重试场景）→ 仍被拦**，产物记录不消耗——机制性
   拦截核心语义；
3. fresh_start=true → 清空产物记录后放行进 run_stream；
4. fresh_start=true 但清库失败 → 仍放行（executor stale 清盘兜底）；
5. 无产物（正常首跑）→ 守卫零影响；
6. 守卫自身异常 → fail-open 放行。

## Follow-up（不在本次范围）

- 方案 C（turbo stage 级断点续跑）：停止检查点 + 无 pending tool call 的重放入口。
- 成功路径清产物（消除"成功后被拦"接受面）：需同时覆盖 adapter 直连 resume
  成功路径（interface_deep.py `_resume_impl`），影响面大于本次止血，未做。
- 子代理工具面收口（纵深防御）：确认无合法场景后从子代理工具集移除
  skill_acceleration_exec。
