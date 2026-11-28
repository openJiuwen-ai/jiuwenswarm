# 同技能批量执行与断点恢复

## 元信息

| 项 | 值 |
|---|---|
| 类型 / 编号 | spec / S_13 |
| 关联模块 | batch.py、segment_execution.py、segment_inputs.py、skill_turbo_tools.py |
| 最近一次修订日期 | 2026-11-28 |
| 关联 feature | F_10_batch-execution-and-resume.md；F_11_declaration-driven-auto-batch.md |

## 公开入口与范围

`skill_acceleration_exec`在旧query/fresh_start/skill/accelerator/params位置之后追加accelerators。
None不选择批模式；空列表非法，不能回legacy。必须准确skill，与accelerator互斥。
每批1～8段，元素只含id、params、严格bool force；不支持__full__、重复、乱序、跨技能。
standalone=false可以独占单元素批量（含force），原列表长度大于1时整批拒绝，不扣除预计跳过项。
共享params在每段均须合法，step.params的非None值覆盖共享；未知/保留键即使值为None也拒绝。
单段不新增顶层force；query-only和__full__保留legacy。

## 声明驱动的自动组批

公开工具的 `auto_batch: bool | None = None` 为三态；省略或 null 时，准确 skill 加普通 accelerator
默认沿声明组批；false 明确只执行该段，true 明确要求组批。没有后继声明只执行起始段。
这是有意改变省略模式的语义，已有只执行单段的调用者须补 auto_batch=false。
显式 accelerators 列表、__full__、仅 skill 和旧 query 入口在省略/null 时保持原分流；
true 必须同时指定准确 skill 和普通 accelerator，与 accelerators、__full__ 冲突即 invalid_args。
起始段的 params 只作为该段 step.params，后段经前段输出、原台账和合法文件严格装配，不虚构参数或转成共享参数。
batch_enabled=false 时省略/null 的普通起始段保留单段能力；显式 true 仍按既有批次开关阻断。
底层 run_batch_stream 仍接收已确定的布尔模式，run_accelerator_stream 保持精确单段语义。

可选 batch.auto_next 声明同技能启用且 order 更大的后继；未知、反向、自循环及独占关系非法。
引擎只沿当前声明组批，遵守 max_batch_steps（最多8）并在链尾、审阅边界或独占边界结束。
自动计划再交原 build_batch_plan 校验及 run_batch_stream 执行，不另建调度或台账。
`hitl.review_when_param` 可选指定本段 input_schema 中的布尔参数，只适用于 review 段。
未声明该条件的 review 保持原固定停点；声明条件的 review 允许规划后继，实际执行完成后按段门面计算的
review_required 判断停点，参数缺省/false 不停、true 按既有双条件处理审阅边界。不根据 query 关键词猜停点。
PPT 声明 prep→outline→generate→export；outline 条件为 outline_review。默认完整请求一批四段，
显式审阅时在 outline 内发卡，回答恢复后返回 review_checkpoint，剩余 generate/export 不执行，交外层改稿或确认。
返回 planned_steps 确认实际规划；next_accelerator 仅在正常完成、审阅或澄清停点返回实际已完成段的声明后继，否则为 None，不能据此跳过失败段。

恢复记录原起始段、原 params 和实际组批列表；原调用参数或定义变化均拒绝恢复，不根据新声明重新扩展。
批次及步骤的既有失败、人工确认、取消、完成复用和发送保护保持有效。

`SkillTurbo.run_batch_stream`为批门面，segment_result返回批结果，artifact_holder保留本批实际owner。
不并行、不回滚业务文件、不猜自然语言取消，不创建第二套任务调度或台账。
批内忽略fresh_start，初始query定位任务后由宿主todo/session身份复用；新交付物仍须先成功创建新外层todo。

## 静态校验与副作用门槛

build_batch_plan只读当前声明、源码和可取得台账；不open任务、不创建业务目录、不执行root或planner。
批次入口先核验显式工作区，缺失或非法时返回 blocked/workspace_missing，不从内部默认目录补齐。
校验shape、长度、精确id/order、独占、共享/步骤参数、JSON可序列化性、schema和前提可达性。
hard不足blocked，soft告警但不豁免schema.required。仅前序启用段的声明输出可解释缺失，producer须匹配。
当前已验证事实可供依赖，不能用显式参数伪造producer；文件前提还须实际存在或由合法前序输出可达。

缺失且可由前序产生的required可以延迟；已给出的坏类型、范围、越界路径不可延迟。
anyOf/oneOf逐分支校验；显式已经满足多个oneOf分支时不能因其他未来事实而放行。
运行前仍调用唯一严格输入装配，保持互斥页面来源投影，不造假路径值来满足schema。
无任务时静态相对路径仅按同层级的未创建验证基目录界定边界；业务值不被替换，实际run_dir由Ledger确认后分配。
当前整个技能源码仍须通过注册安全策略。静态拒绝零新Ledger、目录、节点调用。

## 串行执行、去重与保存

父批次持有原会话/card租约；子段复用同协程租约及任务身份，每段新建SkillTurbo/Executor。
Ledger仍是唯一事实owner；本批持有该owner的真实工作快照，以保留常规保存失败的已验证输出及失效信息。
段前读可取得最新任务状态，沿用revision/updated_at择新规则；不以落后持久层覆盖本批新事实。
首次身份保存和实际重跑前prepare_rerun失效保存必须可靠确认，否则该段零业务执行。
普通结果保存失败保持真实业务状态和文件、persisted=false及告警；后段按逐fact verified、producer、文件重新检查。
prepare_rerun也保留该owner中尚未保存的较新批内事实，再确认旧事实失效，不增加完成run。

latest_runs决定已完成，不依赖最多32条runs历史。未force时检查当前前后置产物后跳过；
完成项文件不足则blocked，不能偷偷重跑。参数改变不隐式force，跳过时告警未重用新参数。
force=true只存在步骤内，绕过完成复用但不绕过权限、前提、源码或发送保护；恢复原force段不再次失效/重做。
记录consumed_facts及参数，报告fact_updated_by；上游重跑不自动重算下游，跳过旧下游时披露来源revision变化。

## 停批与结果

results仅实际执行段；already_done和batch_stopped单列skipped。聚合facts/artifacts保留来源，
warnings保留每次保存风险，后段保存成功不能抹掉前段风险。success只表达本批范围。
partial按逐fact验证可继续，最后保留partial；failed/blocked/not_applicable/invalid_args停批，失败末段不变成ok。
resume_remaining只列失败段之后未执行项，next_steps要求先修复stopped_at再考虑余段。

实际执行的review正常结束，仅continue_after_resume=true且stop_after=never两者均成立才继续；
条件 review 仅在 review_required=true 时应用上述规则；由段门面从实际执行输入计算，技能返回的同名字段不能覆盖。
其他组合（包括默认）review_checkpoint停批，stopped_at=null，不把先前partial抹成ok。
already_done的review不重发卡或重复停批。clarify恢复后按continue_after_resume决定是否clarify_checkpoint停批。
未答卡、业务失败、取消、实际权限/审阅前提始终优先，不因组合允许续跑而越过。

## 原段断点

仅使用resume_ctx.segment.batch保存批控制，当前段inputs/plan_code/pending_tool_call_id/task_states仍归原段。
控制含原query/skill/完整steps/共享参数/todo/工作区、batch_id、cursor、已完成结果/跳过/告警、
真实Ledger工作快照及保存风险、首次执行定义快照。状态只含可序列化数据和文件引用，不放执行器/ContextVar。
批和段的指纹及字段校验覆盖完整前缀、游标、原参数、身份；坏控制不能猜测重建。
首次实际执行前采集声明/技能文件原始内容、文件集合与路径；当前或未来段、文案/注释、新增/删除/重命名、
版本或无法读取均拒绝旧断点，清本次匹配状态failed，零当前/后段恢复，不fresh。工作文件修改不改变定义快照。

恢复原整批调用，重建SDK Session/Checkpointer可加载原cursor。先校验整个定义，再恢复当前段原inputs/节点状态；
前段零重复，原步骤force不触发新重跑；后段才按当前文件和本批事实严格装配。
连续卡保留同cursor和已回答工具记录。pending/in-flight归原owner，busy/重复作答不清正在执行的槽。
can_pause=true/默认AbortError透传；false返回failed且无恢复卡；CancelledError终止，清理存储失败不得转换为普通failed。
原断点resume_in_flight为None/缺失/false表示未占用，true表示正在恢复；其他类型拒绝，不宽松转换字符串或数字。

不同子执行器的相同工具参数也必须有不同调用ID：只在既有TCID hash的request域附加batch_id/cursor，
不改TCID格式或解析器，外层request_id/事件路由不变；恢复仍沿用既有精确/idx回退规则。

## 配置、披露与交付

配置经既有adapter的config dict注入：enabled、batch_enabled（默认true）、max_batch_steps（默认8，上限8）、
max_batch_p50_seconds（默认600）。超p50预算仅warning，不作为新超时；不修改运行配置。
流程确定时模型可优先批量，完整schema说明独占单元素、步骤force、审阅和失败修复。
batch_enabled=false时只给单段指南，并从实际模型工具副本移除accelerators，不改共享公共工具卡。

沿用task/chat事件，可选batch_id/batch_index/accelerator/task_key标注内层归属；turbo_context继续关联外层调用。
聚合delivery_confirmed仅对应本批可信新增完整投递，且批正常完成无余段；全跳过/历史回执不产生新确认或总结。
confirmed_deliveries保留实际已发生发送依据，与整个批业务状态分别报告；普通保存失败不抹掉发送事实。
PPT缺页/合并失败不部分发送；投递未知不得借force/下一批自动重发。
Flash仍在历史追加、无余段/外层todo/steering且真实确认后才考虑finish_request。
Relay packages/api的结果消费变更已被用户排除，本步不应用此前待审补丁，不宣称该消费者具备新门槛。

## 不变量

1. N1 批前静态失败不创建任务身份、业务目录或执行任何段。
2. N2 同批各段使用新执行器、同一任务和逐段Ledger提交；常规保存失败不改写已发生的业务事实。
3. N3 force仅属于显式步骤；首次执行前可靠确认失效，原段恢复不重复失效或重做前缀。
4. N4 缺失事实仅可按前序声明延迟；实际段前仍校验真实值、来源和当前文件。
5. N5 恢复完整原调用和内容快照，任何定义漂移或损坏控制都不得猜测续跑。
6. N6 审阅只有双条件允许才继续；历史跳过不重发卡或重复总结。
7. N7 批最终交付确认必须来自本批实际新增发送且批已正常结束，无余段。

## 执行路径全景

| 路径 | 入口与步骤 |
|---|---|
| P1 新批 | skill_acceleration_exec → _invoke_segment_tool → run_batch_stream → build_batch_plan只读校验 → Ledger.open确认身份 → 新子实例run_accelerator_stream → record/save → 下一段 → _aggregate |
| P2 已完成复用 | 单段入口读取latest_runs → 当前产物/来源检查 → already_done → 批skipped；不新建完成run |
| P3 显式重跑 | 单段入口检查前提 → prepare_rerun确认失效 → 原段业务执行 → record/save；下游保持历史并报告revision变化 |
| P4 暂停 | SegmentContext.checkpoint将batch纯数据加入原段控制 → 原ResumeContextManager保存 → AbortError透传，父循环不推进cursor |
| P5 原批恢复 | _resume_state校验身份/指纹/前缀 → 当前完整源码校验 → 原段恢复inputs/task_states → 当前段完成提交 → 按审阅组合停批或继续 |
| P6 模型披露 | AcceleratorRail复制请求工具卡 → renderer按batch_enabled输出指南/schema → Deep/Flash实际模型输入；根加载只追加说明和只读进度 |

## 异常分支与退出路径

沿用 [[S_01]] / [[S_06]] 的退出和发卡语义，不新增事件终态。

| 分支 | 批结果与副作用 |
|---|---|
| 参数/静态前提/源码非法 | invalid_args、blocked或failed；无新任务/目录/节点 |
| 身份或重跑失效无法确认 | failed；本段零业务，保留已完成前缀 |
| 段业务失败、运行时前提不足 | 保留原status、stopped_at和prefix；余段不执行 |
| 常规结果保存失败 | 保留业务status，persisted=false和warning；按逐fact前提决定后段 |
| 审阅边界正常停批 | ok/partial，review_checkpoint，stopped_at=null，报告余段 |
| 暂停 / 已占用 | AbortError透传 / busy；不误清其他请求的pending槽 |
| 定义漂移、断点损坏 | failed，清匹配旧断点；无当前/后段恢复，不fresh |
| CancelledError | 透传；清理失败不转换成普通业务失败 |

## 边界case清单

| 场景 | 现行为 / 风险 | 测试覆盖 |
|---|---|---|
| 0/9段、重复、乱序、__full__、字符串force | 整批零业务拒绝，防止回legacy或隐式重排 | test_batch_execution |
| 独占段混入已完成步骤 | 按原列表长度拒绝；独占单元素可force | test_batch_execution |
| 未来required、非相邻producer、anyOf/oneOf | 仅可达缺失延迟，显式坏值和多匹配oneOf拒绝 | test_segment_acceptance |
| partial、soft/schema、保存失败 | 逐事实verified决定继续；不伪造成功 | test_batch_execution / test_segment_acceptance |
| review四组合、两次卡、clarify不续跑 | 按声明停批，不重复前段/叶子 | test_batch_execution / test_segment_acceptance |
| 当前/未来源码漂移、游标/原参数损坏 | 恢复前failed并清旧断点 | test_batch_execution / test_segment_acceptance |
| 新SDK Session/持久checkpointer | 原游标与前缀恢复；不等同产品进程重启验收 | test_batch_execution |
| 并发单段/批量、重复答案 | busy不清正在执行槽 | test_batch_execution / test_segment_acceptance |
| PPT四段、缺页/merge失败、发送未知、显式页面源 | 真实文件交接；失败不部分发送；force不自动重发 | Relay test_segment_batch_entries |

## 修改检查单

- 修改批参数或配置 → 同查真实SDK工具schema、Deep/Flash两种懒加载与batch开关，以及legacy/单段兼容。
- 修改静态装配 → 同查未知/保留键、共享值被覆盖、future required、producer、路径、anyOf/oneOf，并确认实际段前仍严格装配。
- 修改Ledger/force → 同查身份确认、失效确认、常规保存失败、跨暂停的工作快照、latest_runs和历史下游来源。
- 修改恢复 → 同查单卡/连续卡、取消、重复作答、并发、坏控制、新Session及当前/未来定义漂移，核对 [[S_06]] 原有路径。
- 修改聚合/交付 → 同查中间停批、partial、全跳过、真实新发送、未知回执和无余段门槛，不把排除的Relay消费适配算作已完成。

## 与其他spec的关系

声明与发现归 [[S_02]]，安全执行归 [[S_03]]，原段恢复归 [[S_06]] / [[S_11]]，
事件终态归 [[S_08]]，Ledger与严格实际装配归 [[S_10]]，模型披露与交付依据归 [[S_12]]。
