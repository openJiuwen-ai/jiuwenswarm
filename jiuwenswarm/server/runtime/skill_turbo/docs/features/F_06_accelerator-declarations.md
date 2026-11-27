# 阶段加速器声明与跨段状态通道

| 项 | 值 |
| --- | --- |
| 类型 | feature |
| 日期 | 2026-11-15 |
| 范围 | 第二步声明发现/索引；第三步任务台账、产物隔离及输入输出检查 |
| 状态 | 第二、三步已实现并通过相关测试；第四步执行接线见 [[F_07]] |
| 关联规约 | [[S_01]]、[[S_02]]、[[S_05]]、[[S_06]]、[[S_09]]、[[S_10]] |

## 第二步：决策与边界

依据用户提供的第二步执行方案，从当前 dev-stable 创建独立分支实现，不读取或复用作废分支及 stash。当前目录中无已有本特性记录，使用下一编号 F_06；不承接作废候选的历史测试结论。

清单只传递声明数据，不调用模型或执行入口。坏声明不禁用合法 legacy，原有整目录 Python 安全校验和包名碰撞拒绝仍有效。严格区分整份冲突与独立坏项；独立文件优先，非法文件不得回退内联。

来源使用有序绝对 roots，元数据快照保留文件集合和各自 stat，不使用最大 mtime 合并元数据。展示 last-good 只限同来源，保留原采集时间并公开当前失败状态。Registry 只索引已注册快照；后续执行仍须重新读取和验证当前声明、源码，不能凭展示缓存或 Registry 放行。

__full__ 通过旧 builder 在注册期保存兼容入口字符串，Registry 查询不读盘、不调用模型。浅层 frozen 不代表深层不可变，缓存和查询边界深拷贝嵌套声明。

普通安装及 harmony 均直接声明 jsonschema 与 referencing；后者提供不带网络检索器的 schema registry，文档内引用可验证，无法解析的引用只移除坏项。uv.lock 仅新增八行依赖关系，未改变任何包版本。

不修改执行器、HITL、台账、批量、模型提示或 relay；不提交、推送、发布或替换用户运行环境。规范 JSON 声明指纹不是源码恢复凭证。

## 第二步：验证与交接

2026-11-15，在分支 `fix/skill-accelerator-manifest-v2` 验证；基线为 `0d8f41bc8d7d2af67f919c8164ff7612ec5a1a38`。未提交、推送或发布。

```powershell
.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_flash_turbo_discovery.py tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py -o addopts="--asyncio-mode=auto" -q -o log_cli=false
uv lock --check --offline
git diff --check
```

结果：**562 passed in 14.61s**，含新增两文件的 62 个声明/目录用例；锁文件一致性及 diff 空白检查通过。完整 pytest 日志在 `%TEMP%/skill-step2-full-tests.log`。既有测试仅增加新缓存的隔离设置，未删除或弱化断言。

覆盖真实临时目录的 discover→Environment→Registry 链路、无/坏清单保留 legacy、独立项移除/全局冲突拒绝、producer 重检、外部 schema 引用零联网、文档内引用及 defaults、深拷贝、同名歧义、原安全校验、TTL 重扫、未来 Python mtime、roots 顺序、清单/入口增删、真实 read_text/iterdir IO 异常与恢复。额外断言同一快照的 stat 和发现复用一次解析出的 roots。

真实 PPT 只读冒烟显式使用 `E:/relay-claw-zqh/relay-claw/office-claw-skills`，发现 `(pptx_craft, pptx-craft)`，经 Environment/Registry 得到 prep/10、outline/20、generate/30、export/40 及兼容 __full__。拦截动态业务包导入，业务入口导入与执行均为零；日志在 `%TEMP%/skill-step2-ppt-smoke.log`。relay 仍在原 dev-stable HEAD `f4a75a3624627d8cdc22478ecde16f7ae4877115`；23 个 PPT turbo 文件 SHA256 与第一步证据全部相同。

未验证多入口实际执行、台账、批次、真实模型或 UI；本步未实现执行前的当前状态复核门禁。后续执行必须重新确认当前声明和代码，失败则零执行；不能用本步 Registry、展示 last-good 或规范 JSON 指纹代替。

## 第三步：状态与数据通道

2026-11-15，按用户第三步执行方案继续本特性。从 `fix/skill-accelerator-manifest-v2` 新建 `fix/skill-accelerator-ledger`，保留第二步未提交工作区；基线 HEAD 未改变。作废分支与 stash 未读取、复用或恢复。第三步无需修改 relay，包括其 PPT turbo，亦未修改工具签名、模型提示、执行器或运行时配置。

新增 SkillRunLedger 单一 owner，保存 book/任务/active 关联、首次 query 依据、会话序号、事实来源、最新完成与32条历史。任务身份只在首次创建计算；todo 定位映射、无 todo 延续当前任务、fresh_start 忽略，恢复缺失 task_key 明确失败。run_dir 创建前校验工作区边界，身份保存未确认时不创建业务目录。段完成后保存失败不改真实业务结果；身份创建及重跑前失效保存则必须确认成功。竞争采用 revision/updated_at 较大者，调用方同步胜出记录和诊断；未采用基准版本变化即拒绝。

resume_context 增加窄范围的独立 Session 上下文及同会话/card 写锁，供台账和产物桶复用。真实 SDK 的 update_state 是嵌套 merge，因此先删除旧键再写完整值；独立提交后新建读 Session 确认，读操作不 post_run。同步已参与该通道的 turbo Session，避免陈旧 finally 回写；主 ReAct 状态不混入台账。ACK 或确认读取异常后尝试读取实际保存状态同步，本次仍报告失败。如果持续无法读取，后续实际执行调用方必须避免 post_run 陈旧快照，不能伪称已确认；实际工具接线在第四步。

会话/card 级执行租约拒绝并行业务；持久 pending 断点仍返回 busy。当前 app_agentserver.py 的入口用单个 asyncio.run 启动，方案支持同一 sidecar 进程内串行；此锁不提供跨进程 CAS，也不新增租约后台服务。

node_artifact_store 新增任务/技能/段命名空间，保留独立 legacy 块；save/load/clear 的缺省行为继续兼容 legacy，新桶替换/删除不影响兄弟桶和文件。新桶必须匹配非空 hash；旧无 hash 仅兼容 legacy。中断式 skip_post_run=True 在新桶路径仍由独立 Session 持久化，不提交或关闭调用方。executor 的清理、继承、finally 和 use_tool 暂停写入接线全部留给第四步。

segment_inputs 提供唯一异步装配与 finalize；文件 stat/hash、路径及 schema 检查均有界 offload。五层优先级、非None显式参数、未知/保留参数拒绝、严格 producer、hard/soft 分级均生效。手改文件读取当前内容并告警；删除使来源不再可信。只提交声明内、小于等于2048字节且有限 JSON 的事实，产物遵循 glob 与 handoff 边界；partial 逐事实 verified，不把存在的文件当业务成功证据。

实现细化：finalize 必须取得装配时的 initial_inputs 快照，以区分真实的新输入产出与未变化的旧值；这样重跑缺输出时不会再次验证旧事实。失效提交带独立 token，不增加完成 revision/run；record_segment 只对实际结束记录 +1，暂停/取消不计完成。上游重跑结果披露 fact_updated_by，下游最新记录、消费的旧 revision 和文件均保留，不自动重算。

第三步验证命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/test_skill_run_ledger.py tests/unit_tests/server/runtime/skill_turbo/test_segment_inputs.py tests/unit_tests/server/runtime/skill_turbo/test_segment_artifact_namespaces.py -o addopts="--asyncio-mode=auto" -q -o log_cli=false
.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_flash_turbo_discovery.py tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py -o addopts="--asyncio-mode=auto" -q -o log_cli=false
git diff --check
```

新增测试使用真实 SDK Session 与独立序列化 checkpointer，故障可控注入，所有业务文件位于 tmp_path。另用真实临时 SQLite，关闭引擎、重建 Checkpointer/Session 后读回台账及隔离桶；这只证明存储重建，不等同完整服务重启验收。Windows 目录联接用真实临时 junction 验证 resolve 后的越界拒绝。

最终结果：新增三文件 **56 passed in 2.47s**；模块及中断恢复/Flash/工具/PromptRail兼容回归 **629 passed in 15.38s**。日志分别为 `%TEMP%/skill-step3-targeted.log` 与 `%TEMP%/skill-step3-full-tests.log`。有界 offload 用例实际观察文件检查在非事件循环线程执行，九个并发请求的同时文件检查数量不超过4。重跑保存前失败、静默未生效、ACK丢失及确认读取失败均验证：本次受控业务零执行，旧用户文件保持原内容；内存旧事实也不继续作为已验证产出。

`git diff --check` 通过；relay 仍为原 dev-stable HEAD，23个 PPT turbo 文件的 SHA256 与第一步证据全部相同。未提交、推送、发布或修改用户运行时配置。没有运行真实模型、UI、多入口 PlanNode、HITL 段执行或 BatchRunner；未进行独立代码检视，不把单测结果写成用户验收或已发布结论。
