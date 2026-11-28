# 段任务台账、产物命名空间与输入输出契约

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_10 / segment-state-and-inputs |
| 关联模块 | skill_run_ledger.py、segment_inputs.py、node_artifact_store.py、resume_context.py |
| 最近一次修订日期 | 2026-11-28 |
| 关联 feature | [[F_06]] |

## 范围与边界

本规约定义跨调用状态与纯数据检查。声明由 [[S_02]] 的统一解析器提供；实际 PlanNode 和 HITL 的内部单段接线归 [[S_11]]；公开工具扩展、批次编排与 Relay 消费适配留待后续集成。执行前当前声明/代码复核由段调用边界负责，Registry 与展示缓存均非执行许可。

## 不变量

1. N1 台账 KEY 只在 SkillRunLedger 定义，使用 `{card.id}__skill_turbo`；缺 card/session 身份明确拒绝，无默认键空间回退。
2. N2 current/load 只读，不分配序号或目录；独立读 Session 仅 close_stream，不 post_run。写路径在同会话/card 写锁内重读完整 book，替换写并同步已参与隔离通道的调用方快照。
   同身份的锁、调用方弱集合和已访问键由一个内存 scope 管理。身份索引弱持有 scope；存活调用方通过弱键索引保有它，活跃和等待操作也持有它。最后一个使用者释放后回收，不在单次提交结束或业务终态时删除仍被使用的锁。回收仅针对内存管理信息，不删除已持久化台账、产物或断点。
3. N3 首次 query 去除首尾及合并空白，工作区取显式 metadata.output_dir 的规范路径；段引擎不从宿主内部目录补值。task_key 为 UTF-8 编码 `normalized_query|workspace_base|sequence` 的 SHA1 前12位。todo 只定位，不进入哈希；后续 query/request/段/fresh_start 不改变身份。
4. N4 新身份/active 可靠持久化并重读确认后才返回可启动任务；保存失败或无法确认明确创建失败。重试读取实际状态，已生效就复用。run_dir 只能在工作区内，身份确认前不创建业务目录。
5. N5 同任务竞争按 `(revision, updated_at)` 取大者；同版本不采用基准版本冲突拒绝。save 回带胜负、revision、persisted，并同步最终选中记录；保留其他任务、sequence、active。
6. N6 会话/card 级执行租约约束所有任务；已有其他协程租约或持久 pending 断点返回 busy。已验证恢复仅可接管与传入原断点完全相同的 pending 槽，重复作答仍拒绝。单进程锁不是跨进程 CAS；无后台租约服务。
7. N7 prepare_rerun 在租约内取得最新状态、撤销本段覆盖并将本段旧事实标未验证；持久化并确认失效后才能返回 true。不增加完成 revision/run；暂停/取消不恢复旧状态。
8. N8 普通段结束 revision+1，保存失败只 warning/persisted=false，不改变真实业务结果、不回滚文件或单独禁止续接。latest_runs 保留每段当前结果，历史 runs 最多32条，覆盖仅从最新 ok 结果推导。
9. N9 命名空间含 task_key/准确技能/段，隔离 legacy；新桶的读取必须匹配 namespace 和非空入口字符串 hash。旧缺 hash 兼容只属于 legacy。清桶不删文件/台账/其他桶。
10. N10 装配顺序是上下文→全量台账facts→批内facts→defaults→非None显式params；保留上下文不被覆盖，未知显式键拒绝。schema 检查 properties 投影，required 可由可信台账满足。
    对仅由多个单字段 required 分支组成的 oneOf，按显式键集合选择互斥来源：显式一个来源时先移除本次输入中其他分支来源，再校验其原值（包括 None/空值）；显式多个来源直接 invalid_args。无显式来源时仅继承声明顺序第一个来源的已验证台账/上游事实，无有效值 blocked，其他分支不作历史默认。此投影不改写台账和来源记录，发生在路径/前提/schema 检查前；恢复仍只校验原 inputs，不重新装配。
11. N11 hard 前提不足 blocked，soft warning，参数/schema 错误 invalid_args；指定 producer 必须实际来源相符且 verified，显式路径不能冒充来源。合法手改文件告警并读取当前内容；文件删除使来源不再可信。
12. N12 路径相对 run_dir，resolve 后须在工作区/技能根内；声明 glob 禁绝对/盘符/..。文件 stat/hash 和 schema 检查在有界线程 offload 中执行。
13. N13 finalize 只提交声明事实，小于等于2048字节且为有限 JSON；路径存在并在边界内，handoff 产物必须匹配声明 glob。failed/blocked/not_applicable 不能因文件存在升级；ok 缺产物/后置条件降 partial。逐事实 verified，不按 partial 一刀切。
14. N14 根输出和 handoff 的事实可显式提交；从最终 inputs 提取事实须与装配后的 initial_inputs 比较，防重跑缺输出时沿用旧值。段门面在业务执行前保存该输入快照。
15. N15 重跑披露 fact_updated_by 及 revision；保留下游历史结果和文件，不自动重跑下游，不宣称下游消费了新事实。

## 接口契约

`SkillRunLedger.current/load/open/save/prepare_rerun/record_segment/digest` 维护唯一状态 owner；open 支持 task_key 恢复（缺失明确失败）及宿主 task_id 定位。执行方持有 `skill_execution_lease(session)` 才进入实际业务。

`assemble_segment_inputs(ctx, spec, params, ledger, upstream_facts)` 为异步唯一装配入口，返回 `(inputs, warnings)`；错误为带 status/reason/warnings 的 SegmentInputError。ctx.metadata.output_dir 必须与台账工作区一致。

`validate_segment_replay_inputs(ctx, spec, inputs, ledger)` 复用相同前置检查器，仅验证原 inputs 的身份、工作区、来源、当前文件和 schema，不重新装配或覆盖其值。

`finalize_segment_result(ctx, spec, root_output, inputs, handoff, *, initial_inputs)` 返回 status/facts/facts_provenance/artifacts/warnings；不落盘、不执行节点。Ledger.record_segment 为可信事实加完成 revision 和时间。输入输出 API 内部有界 offload，调用方不得另写装配规则。

## 数据结构

台账 book：version、sequence、active、tasks。task 保存 identity、initial_query、workspace_base、run_dir、revision、updated_at、facts、facts_provenance、artifacts、latest_runs、runs、covered_stages。失效有独立 invalidation_token，不冒充完成 revision。

产物 book：version=2、legacy、segments；旧 `{skill,nodes,plan_code_hash}` 只投影为 legacy。每个新桶保存 namespace、skill、nodes、plan_code_hash、updated_at。两个 owner 不接管 resume_ctx 或 INTERRUPTION_KEY。

## 执行路径全景

P1 open：校验身份/工作区→会话租约及写锁→独立 pre_run 读 book→定位/创建→替换写→单次 post_run→新 Session 重读确认→同步调用方→创建受限目录。

P2 save：写锁→重读→比较本任务 revision/时间→保留胜者、其他任务及映射→全量替换→提交确认→同步胜者与诊断；普通失败仅告警。

P3 rerun：租约→重读→原 owner 失效和覆盖撤销→单次提交确认→允许后续业务；普通段实际执行接线见 [[S_11]]。

P4 assembly/finalize：有界 offload→路径/事实/来源/文件检查→schema/后置条件→结构化结果；由 [[S_11]] 段门面接实际执行与保存。

## 异常分支与退出路径

身份创建/失效未确认是业务前失败；普通结果保存失败 fail-open。AbortError、CancelledError 均透传，不转为创建成功、完成 run 或工具成功。

## 边界 case 清单

| 场景 | 契约 | 验证入口 |
|---|---|---|
| 首次保存失败、ACK丢失后重试 | 本次零业务；后续读实际映射复用 | test_skill_run_ledger |
| 嵌套merge/陈旧Session | 真删除且不复活新状态 | ledger/namespace测试 |
| 版本竞争/32条历史截断 | 胜者可见、latest完成不丢 | test_skill_run_ledger |
| 两任务/两段/legacy | 桶互不覆盖、hash不串用 | test_segment_artifact_namespaces |
| 手改/删除/链接越界 | 新文件权威；告警或阻断 | test_segment_inputs |
| partial/坏事实/严格producer | 逐事实验证，不能冒充来源 | test_segment_inputs |
| SDK临时存储重建 | 新Session/Checkpointer读回隔离状态 | SDK集成用例；非服务重启验收 |

## 修改检查单

- 改状态写入→检查真实SDK merge、读会话零提交、确认生效及旧快照同步。
- 改完成记录→检查失效不计完成、暂停/取消、竞争胜者、历史截断和下游revision。
- 改路径/事实→检查显式output_dir、符号链接/目录联接、未知参数、producer、JSON大小及逐事实验证。
- 接第四步→覆盖首次执行前身份确认、重跑前失效确认、当前代码复核、输入初始快照、暂停前产物落盘及完整执行租约。

## 与其它 spec 的关系

[[S_01]] 保持 legacy 执行路径；[[S_06]] 保持断点 owner 和隔离规则；[[S_09]] 保持显式工作区来源。内部单段门面已接入状态和数据校验，公开工具仍保留 legacy 调用方式。

节点入口哈希通过 Executor.hash_plan_code 统一计算，沿用原 MD5 前八位算法；方法名调整不
改变已持久化桶的 hash 匹配规则，也不以新算法使原断点失效。
