# 显式单段执行与恢复

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_11 / explicit-segment-execution |
| 最近一次修订日期 | 2026-11-25 |
| 关联模块 | agent、executor、skill_turbo_tools、resume_context、permission_bridge |
| 关联 feature | [[F_07]] |

## 范围 / 边界

第四步在 [[S_10]] 的唯一台账和输入装配 API 上接入真实 PlanNode。legacy 和指定技能的
`__full__` 保留整流程语义；批次和步骤force由 [[S_13]] 编排，单段工具不新增顶层force。公开工具指定普通起始段且省略/null auto_batch 默认组批；精确单段须显式 auto_batch=false。底层 run_accelerator_stream 不改变单段语义。PPT四段业务在技能侧；第六步加载描述及交付关联见 [[S_12]]。

## 不变量

1. N1 `skill_turbo(query, fresh_start=False, skill='', accelerator='', params=None, accelerators=None, auto_batch=False)` 保留旧位置参数。
   普通段精确选择，零 planner；不接受顶层force；accelerators选择批次且与accelerator互斥，未选择普通段或批次时拒绝params。auto_batch省略/null由调用形式分流，普通起始段默认组批，显式false执行单段；true须带skill及普通accelerator，互斥accelerators/__full__。自动批次params仅传起始段，经 [[S_13]] 批量执行器运行。
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

P1 工具模式检查 → 新实例 → 当前精确 Registry → 会话租约 → 台账 current/open → 完成检查 →
必要时 prepare_rerun → 唯一输入装配 → 执行定义快照 → executor → 唯一后置验收 → record/save → 交回外层。

P2 原答案通道 → 读原断点 → 身份/参数/工作区/入口/内容快照验证 → 租约接管 → 原 inputs 重放 →
原 tcid 命中 → 已完成阶段短路/同 namespace 产物继承 → 二次暂停替换断点或终态清理。

## 异常分支与退出路径

参数 invalid_args、前提 blocked、业务 failed/partial 如实返回，段 chat.error 不外发为整轮失败。
无断点答案返回 resume_context_unavailable；确定不可复用的本次断点清除，不清其他任务。
取消先停止执行再清状态，保留业务文件和台账。

## 边界 case 清单

按第四步方案 E01–E09、H01–H11、C01 检查实际 SDK invoke、事件、文件、独立状态、节点次数及资源回收。
真实模型、产品 UI、完整服务重启和 PPT 四段交接属于后续验收，不由模块测试替代。

## 修改检查单

- 改模式 → 旧位置参数/SDK schema/未知参数、准确选择、__full__、fresh 守卫和停止提示。
- 改结果 → 根与子输出、fallback 修复、后置不足、发送确认、内部键和常规保存失败。
- 改恢复 → 原 inputs/tcid/task_states、同会话防重、merge 负例、快照漂移、二次中断、取消及 can_pause。

## 与其它 spec 的关系

[[S_01]]/[[S_06]]/[[S_08]] 的 legacy 路径保持；普通段的例外以本规约为准。
[[S_02]] 负责声明，[[S_04]] 保持 PlanNode 模板生命周期，[[S_09]] 负责宿主配置注入，[[S_10]] 负责唯一状态和数据验收。
