# 模型披露与交付关联

| 项 | 值 |
|---|---|
| 类型 / 编号 | spec / S_12 |
| 修订日期 | 2026-11-25 |
| 关联 feature | F_09_model-disclosure-and-delivery.md |

## 请求级披露

`SkillAcceleratorRail` 复用 `collect_accelerator_catalog` 的已解析声明，与宿主
`SkillUseRail.get_skills_for_session(session)` 取交集；无效声明不得落入 legacy 指南。
进程共享工具卡及 Flash 懒加载导航保留通用模板，实际 ModelCallInputs.tools 使用深拷贝。
目录 section `skill_accelerators` 的排版优先级为41，回调在SkillUse(95)/Retrieval(94)之后。
原生skills section被隐藏时不显示目录；Flash选择rail同时隐藏两个目录section。
空集合、禁用、卸载移除旧section。配置通过adapter注入，不在模块读取全局配置。

配置：enabled、catalog_enabled、load_appendix_enabled，后两者默认true；
catalog_line_budget默认96字符/行，catalog_max_per_skill默认8条/技能，两种预算分别实施。
renderer按order排序并标记review；last-good只授权展示，执行重新核验当前定义。

有清单技能可按场景、前提和进度选段，无需用户加速关键词；普通工具和段可交替，
显式禁用、普通流程及其他技能选择仍有效。无清单技能与query-only/__full__保留legacy。
PPT完整交付为目标，中间段成功不完成todo；SVG段不扩展为native/template_fill替代。
局部改稿不调用generate；合法完整单页集合可首次export，输入排除沿用第五步。
export的next_steps为空表示PPT内部无后续段，更新外层todo的文字放continue_hint；活跃外层todo仍阻止Flash自动结案。
第七步按 [[S_13]] 的batch_enabled开关披露批量及步骤force；仍不自动选首段，不新增单段顶层force。新增独立交付物先成功创建新todo，再串行调用。

统一规则：后续步骤及输入获取方式已经明确时，优先一次调用多个阶段。用户限定执行范围、要求中间检查或需要根据结果决定后续步骤时，在对应边界停止。
模型明确选择普通起始段后，省略/null auto_batch 默认沿 batch.auto_next 组批；true 显式要求组批，false 精确单段。
「先做某一步」也可能限定范围，不要求用户说出「单段」；只准备或只生成不导出时传 false，
先出大纲确认再生成时启用 outline_review 并保留大纲边界。显式列表仍由模型选择。
加载附录披露后继、三态及起始段参数语义。batch_enabled=false 时省略模式保留单段，
实际请求工具卡同时移除 accelerators 与 auto_batch，不披露自动组批调用。

## 根技能加载附录

复用SDK根技能加载事件判断，仅成功skill_tool根SKILL.md、请求可见且有有效启用声明时追加。
稳定marker保证幂等。保留ToolOutput.data的skill_content、目录和layout；更新data.content
与实际ToolMessage.content。未预置data.content时使用SDK已渲染ToolMessage正文，避免漏普通根加载。
附录给出完整schema、前提、适用范围、产出、handoff及只读进度，不调用执行段。
进度仅通过显式output_dir、实际todo及同card/session的SkillRunLedger.current读取；
没有任务和读取异常分别表示no_progress/unavailable。零open、零save、零新任务。
AbortError和取消透传。

## 交付与事件

普通段delivery_confirmed仍由当前holder发送owner产生。PPT owner补充delivery_evidence：
准确路径、sha256、发送状态、实际尝试及new_delivery。引擎记录真实send_file_to_user子调用ID和参数；
结果保留owner；后处理失败仍保留可信发送事实，业务失败与交付事实分别报告。已完成跳过保留historical_delivery；历史确认还须路径/hash对应当前文件，改稿后不冒充已投递当前内容。零新发送和新总结。
常规台账写失败不否定已确认发送。历史journal或去重回执确认不冒充本次新投递。

外层工具每次调用先清pending。只有ok、success=true、可信确认、没有remaining/next_steps、
非already_done且owner给出new_delivery=true时排队总结。pending整体保留summary与
任务/调用/owner/文件/回执归属；取出回填不得丢字段。rail再次核对本次结果与外层工具ID。

内层事件可选turbo_context关联skill/task_key/accelerator/outer_tool_call_id；总结llm_output
可选turbo_delivery携带完整pending。adapter解析保留这两个字段，不新增事件类型。
这些字段为后续Relay消费者提供依据，不能单凭success、文件存在或历史marker推断当前交付。

## SDK顺序与Flash收尾

目录回调priority93；加载附录在StreamEvent(80)之前；总结priority79在其之后。
新SDK支持callback_priority时，加载hook使用85；当前安装openjiuwen0.1.16不支持该扩展，
同一披露rail的加载hook使用93，仍满足附录→tool_result因果顺序。
总结rail无初始化副作用，直接使用79兼容实际SDK，不能按旧注释理解为数字越小越先执行。

Flash的finish_request仅修改请求级schema，执行前剥离。段结果必须确认本次交付且没有
剩余段、already_done、活跃/剩余外层todo，才可在历史追加及steering检查之后force_finish。
历史和文件去重各自独立；中间结果、暂停、异常、未知投递结果不复用旧pending。

## 消费边界

Relay packages/api的归属核验和最终替换门槛变更已由用户排除，本阶段不修改该消费者，
也不将此前候选补丁视作当前待办。不能据上游字段已产生就宣称Relay新消费门槛已集成；
此前补丁及隔离测试仅作为历史参考保留在技能侧turbo目录。
