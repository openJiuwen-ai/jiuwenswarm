# 显式单段执行与恢复

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_11 / explicit-segment-execution |
| 最近一次修订日期 | 2026-11-27 |
| 关联模块 | agent、executor、skill_turbo_tools、resume_context、permission_bridge |
| 关联 feature | [[F_07]] |

## 范围 / 边界

本规约描述 SkillTurbo.run_accelerator_stream 的内部单段执行与恢复。它在 [[S_10]] 的台账和输入装配 API 上运行真实 PlanNode，不调用 planner。指定技能整流程由 run_skill_stream 精确构建旧根入口。

公开 skill_acceleration_exec 仍保留 query/fresh_start 签名；公开多入口、候选查询、批量、模型披露和宿主集成留待下一 PR。本规约不宣称这些入口已经开放。

## 不变量

1. N1 内部单段门面按 skill/accelerator 精确选择，零 planner；params 只作为本段声明输入。公开工具签名及 legacy 路由不变。
2. N2 执行前无缓存重读当前清单和源码并验证。初次身份保存、实际重试失效保存未经确认，零业务执行。
3. N3 已完成段核对文件和事实后默认跳过，零新 run；query/fresh_start 不改变任务身份。
4. N4 根输出在 JSON 序列化前捕获；内部 handoff 不进入显示和 after，子节点产物仍保留真实 owner。
   未恢复子失败不能被 root ok 覆盖；已修复 fallback 按实际最终结果验收，记录次数。
5. N5 仅实际执行的非暂停/取消终态记账；校验/加载失败 failed、零业务、零 run。
   无根 status 的正常结束也必须经过事实和产物验收。普通结果保存失败 fail-open。
6. N6 段返回控制给外层，不附整任务停止提示；交付确认只能来自真实发送 owner。
7. N7 恢复使用原身份、inputs、tcid 和 task_states；同会话租约只允许身份匹配的本次断点接管。
   恢复不重新装配、不调用 prepare_rerun、不清原节点桶。
8. N8 首次执行采集声明及技能源文件原始字节指纹，恢复重新读取比较；新增/删除/重命名及文案、
   注释变化均拒绝。工作文件不计入执行定义；不可验证定义或控制状态时拒绝恢复，禁止 fresh。
9. N9 can_pause 默认 true，AbortError 透传；false 在暂停信号外发前终止并返回
   `failed/success=false/error=该段不支持人工中断`，不触发 fallback，不保留本次卡片/断点。
   CancelledError 始终立即透传。外层统一授权和业务提问保持既有权限边界。
10. N10 segment 和 resume_in_flight 必须显式替换或置 None，防 SDK 嵌套 merge 残留。
    finally 关闭本次流并释放工具/租约，不用旧 Session 的全量 post_run 覆盖隔离状态。

暂停前必须确认已完成子树的产物 namespace 保存成功，再保存断点并放行卡片；任一确认失败均不得发可恢复暂停，也不触发普通 fallback。无法恢复原模型身份时返回段失败，清本次匹配断点，禁止 fresh 执行。

终态清断点也须持有同一执行租约，防重复答案在原恢复标 in-flight 前误清 owner 的断点。持久化清理失败披露 resume_cleanup_failed，不覆盖实际业务结果或吞掉取消；工具回收仍执行。无法清掉的 in-flight 槽继续拒绝重复执行，不宣称断点已清除。

## 接口契约 / 数据结构

`SkillTurbo.run_accelerator_stream` + `segment_result()` 为段门面。executor 的实例级
SegmentContext 含准确身份、spec、namespace 和首次执行快照，结果 holder 记录实际执行、根返回、
handoff、子终态、错误与 fallback_count。断点 segment 为可验证的纯数据控制状态，包含原 inputs、task_states 和 pending tcid 的完整性依据，及连续卡片重放所需的已回答工具记录。
段结果包含 success、mode、skill、accelerator、status、facts、artifacts、verified、covered_steps、
remaining_steps、next_steps、warnings、ledger、delivery_confirmed。

## 执行路径全景

P1 内部单段门面 → 新实例 → 当前精确 Registry → 会话租约 → 台账 current/open → 完成检查 →
必要时 prepare_rerun → 唯一输入装配 → 执行定义快照 → executor → 唯一后置验收 → record/save → 交回外层。

P2 原答案通道 → 读原断点 → 身份/参数/工作区/入口/内容快照验证 → 租约接管 → 原 inputs 重放 →
原 tcid 命中 → 已完成阶段短路/同 namespace 产物继承 → 二次暂停替换断点或终态清理。

## 异常分支与退出路径

参数 invalid_args、前提 blocked、业务 failed/partial 如实返回，段 chat.error 不外发为整轮失败。
无断点答案返回 resume_context_unavailable；确定不可复用的本次断点清除，不清其他任务。
取消先停止执行再清状态，保留业务文件和台账。

## 边界 case 清单

按第四步方案 E01–E09、H01–H11、C01 检查实际 SDK invoke、事件、文件、独立状态、节点次数及资源回收。
本 PR 的测试从内部单段门面进入；公开 SDK 多入口、真实模型、产品 UI、完整服务重启和 PPT 四段交接属于后续集成与验收。

## 修改检查单

- 改入口 → 精确选择、未知段、指定技能根入口及 legacy 工具签名兼容。
- 改结果 → 根与子输出、fallback 修复、后置不足、发送确认、内部键和常规保存失败。
- 改恢复 → 原 inputs/tcid/task_states、同会话防重、merge 负例、快照漂移、二次中断、取消及 can_pause。

## 与其它 spec 的关系

[[S_01]]/[[S_06]]/[[S_08]] 的 legacy 路径保持；普通段的例外以本规约为准。
[[S_02]] 负责声明，[[S_04]] 保持 PlanNode 模板生命周期，[[S_09]] 负责宿主配置注入，[[S_10]] 负责唯一状态和数据验收。
