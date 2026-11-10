---
id: B_20261108_1200_team-stop-lifecycle-handles
title: 团队停止路径的早退判据漏项与 terminate 收尾缺失
type: B
author: yanhai
author_email: yanhai010@gmail.com
designs: []
design_note: 对既有团队生命周期路径的小范围一致性修复，复用已存在的 controller 停止、seal 语义与 Runner 镜像清理，无独立设计文档。
modules:
  - jiuwenswarm/agents/harness/team
files:
  - jiuwenswarm/agents/harness/team/team_manager.py
  - tests/unit_tests/agentserver/test_team_manager_registry.py
---

# 团队停止路径的早退判据漏项与 terminate 收尾缺失

## 现象与根因

团队会话的停止入口有四个：`pause_session_runtime`（网页「停止」在 team 模式下走这条）、
`cancel_session_runtime`（team 模式取消）、`stop_session_runtime`（会话切换 / 删除前置）、
以及 `terminate_session_runtime`（非 team 判定的取消兜底，经
`interface.py` 的 `_cancel_team_work_for_session` 调用）。四处各自维护一份「还有没有活儿要停」的
早退判据，实际漂移成三套：

- `pause_session_runtime` 与 `terminate_session_runtime` 只有 stream task / 本地 TeamAgent /
  `_team_monitors` / active / pending 五条腿，漏了 Runner 团队句柄镜像
  （`_runner_team_agents`）与后台 swarmflow run。
- `cancel_session_runtime` 有 swarmflow 腿，仍没有镜像腿——这一条是**有意**的：空闲断连兜底
  cancel 依赖早退保住常驻 runtime。
- `stop_session_runtime` 六条腿齐全。

后果有两处：

1. **早退漏项**：监控句柄缺失、只剩镜像或只剩后台 run 时，`pause` / `terminate` 直接
   `return False`，一个停止动作都不调用；`pause` 里的 `_pause_swarmflow_runs` 还套在
   `if team_name:` 之内，team_name 解析不到时同样什么都不停却返回 True。
2. **terminate 收尾缺失**：`terminate` 从不驱动 swarmflow controller，直接
   `Runner.stop_agent_team` 拆 harness。后台 run 被当成裸 cancel，engine 既不写 seal 记录
   也不发终态事件；同时它不清理 `_runner_team_agents` 镜像，也不释放该镜像持有的 A2X 预留。

## 修复与不变量

- 新增私有谓词 `_has_live_team_runtime(session_id, *, include_paused_runs=True)`，把镜像、监控句柄、
  active/pending、后台 run 收进一处；`pause`、`terminate`、`stop` 三条路径改用它。
  `has_swarmflow_runs` 增加同名关键字参数：pause 传 False，不让已停泊的 run 单独构成进入
  pause / 调用 `Runner.pause_agent_team` 的理由。
- `pause_session_runtime`：早退分支补日志；`_pause_swarmflow_runs` 提到 `team_name` 之外，保证
  只剩后台 run 时也会真正 park，而不是返回 True 什么都没停。
- `terminate_session_runtime`：早退分支补日志；在 `Runner.stop_agent_team` 之前
  `_dispatch_swarmflow_controller(action="stop")`，与 cancel / stop 同一槽位；Runner 停止之后按
  `stop_session_runtime` 的 Runner 分支释放 A2X 预留并 `_stop_runner_team_agent_transport` 丢弃镜像。
- `cancel_session_runtime` 判据保持不变，并在判据旁注明它有意不带镜像腿。
- 不变量：controller 必须先于 `Runner.stop_agent_team` 被驱动，否则协程被裸 cancel，seal 记录与
  句柄都会丢；pause 侧同理，`_pause_swarmflow_runs` 必须早于 `Runner.pause_agent_team`。

## 评审补强：terminate 的兜底解析与镜像清理判据

首版评审指出 `terminate_session_runtime` 仍有一处与 #6275 相反的收尾：`team_name` 解析不到时它
照样释放 A2X、停 transport 并返回 True，而 `_stop_runner_team_runtime` 仍被 `if team_name:` 门控，
池中的 PAUSED runtime 被留在原地——既报告「已终止」，又破坏了这个 runtime 的成员预留与消息通道。
第二处是 `_runner_team_agents` 镜像的清理没有断言，桩丢弃了 `team_name` / `team_agent` 实参，
实现退回旧行为用例仍然全绿。

- **兜底解析**：`team_name` 为空时，按 session_id 从 Runner 池解析出该会话的条目并真正
  `_stop_runner_team_runtime`。解析走新增的 `_find_runner_team_name(session_id, *, state=None)`，
  `state` 缺省匹配**任意状态**；`_find_paused_runner_team_name` 改为以
  `state=RuntimeState.PAUSED` 委托给它，签名与语义不变，`stop_paused_session_runtime` 仍只用
  PAUSED。这条限制是二次评审点出的：只认 PAUSED 时，「metadata 无 team_name + 池中是 RUNNING
  条目」（会话从未 pause，或 pause 没落到 PAUSED）会让 terminate 一次都不调用
  `Runner.stop_agent_team` 就返回 False，用户反复取消只会不断拿到 False，团队一直跑，最终只能靠
  永久删除回收。未尝试 stop 就返回 False 还有两种形态：池条目自带空 `team_name`，以及 Runner 池
  列举失败（按「仍持有」处理，见下一条判据），两者都已在 docstring 的 `Returns:` 里写明。
- **判据改用池快照**：`Runner.stop_agent_team` 在「池里没有条目」与「shutdown 失败」两种情况下
  都返回 False，单看返回值无法判断释放镜像是否安全，因此新增
  `_runner_pool_has_session_runtime(session_id)`（`Runner.list_active_teams()` 按 current_session_id
  匹配，**不区分状态**；列举异常按「仍持有」处理）。池里仍有该会话的 Runtime 时不释放 A2X、不停
  transport、不丢镜像，并 `return False`；否则保持首版清理。
- **语义落到日志与 docstring**：`terminated` 为 False 时不再打 `team session terminated` 的 info，
  改为 `terminate left a Runner runtime in the pool` 的 warning；docstring 补 `Returns:`，
  写明池里仍有该会话 Runtime 时返回 False、A2X 与 transport 保留。
- **仍执行 `_finalize_runtime_cleanup`**：`_dispatch_swarmflow_controller` 自身吞异常只打 warning，
  finalize 不会掩盖 controller 失败；这里的 `finalize_workflows=True` 语义是「前台回合结束」，
  不是 `_cleanup_runtime_locals` 注释里那种「runtime 已不在」的不可恢复拆除——后台 run 已按 stop
  收尾（seal），被保留的是池中团队 Runtime 与其镜像，二者不冲突。本地句柄清掉后镜像腿仍在
  `_has_live_team_runtime` 里，后续 terminate 会重试而不是被早退吞掉。

## 约定下沉到 pause 与 stop：共用判据与各路径自有判断

三次评审指出同一条根因不能在 terminate 一处收口：pause 与 stop 仍各自保留旧口径，形成新的
约定分叉。现在三条路径（pause / stop / terminate）共用下面这组判据，差异只允许出现在「早退腿」
与「是否由删除接管」这两处，写在代码注释里。

**三条路径共用**

- `_has_live_team_runtime(session_id, *, include_paused_runs, include_runner_mirror)`：唯一的早退
  判据，六个腿（本地 TeamAgent、Runner 镜像、monitor、active、pending、后台 run）在这里定义。
- `_find_runner_team_name(session_id, *, state=None)`：`team_name` 为空时的池兜底解析，
  缺省匹配任意状态。pause、stop、terminate 三处都用它；`_find_paused_runner_team_name` 以
  `state=PAUSED` 委托给同一实现，仅供 `stop_paused_session_runtime` 使用。
- `_runner_pool_has_session_runtime(session_id, *, include_paused=True)`：收尾前的池归属判据。池里
  仍有该会话**未被暂停 / 未被停止**的 Runtime 时不释放 A2X、不停 transport、不丢镜像，并返回 False。
  stop 与 terminate 用缺省值（任何状态都算仍在池里），pause 传 `include_paused=False`：PAUSED 条目
  正是 pause 想要的产物。pause 成功后池里按设计仍留有 PAUSED 条目，所以 pause 只在
  `runner_paused=False` 时才查这条判据，且只认尚未暂停的条目（见下节）。
- `_dispatch_swarmflow_controller(action=...)` 先于 Runner 停止；`_runner_pool_has_session_runtime`
  列举异常按「仍持有」处理。

**各路径自有判断（有意不同）**

- `cancel_session_runtime`：调用同一谓词但传 `include_runner_mirror=False`——空闲断连兜底依赖
  早退保住常驻 runtime。（此前 cancel 自己内联了同一组四条腿，已改成传参调用，腿的增减只在一处。）
- `pause_session_runtime`：`include_paused_runs=False`，且新增「已停泊」早退——除镜像腿外没有其他
  活句柄、且池中条目已是 PAUSED 时直接返回 **True**。这条早退只能返回 True：一次网页「停止」会让
  pause 走两遍（`AgentRuntime.cancel_request` 先暂停，随后 team interrupt 的
  `_process_team_interrupt` 再调一次），第二次必然落在已停泊状态上；返回 False 会让
  `interface.py` 给出 `success=false` /「当前没有可暂停的团队任务」，而前端只在 `success===true`
  时才切换暂停态、清 processing 并关掉团队流式卡片——用户点了停止、团队确实停了，界面却按失败
  处理。此前 `include_paused_runs=False` 还被镜像腿架空，真实的「已暂停常驻会话」再收到 pause 会
  重入并重复调用 `Runner.pause_agent_team`，原回归用例只覆盖了生产中不存在的形状（无镜像）。
- `stop_session_runtime`：`stop_runner=False`（永久删除前置）时不套用池归属判据——
  `Runner.delete_agent_team(force=True)` 接管真正的停止，条目留在池里是预期而非失败。
- `pause` 与 `stop` 的返回值：与 terminate 同约定，池里仍有未暂停 / 未停止的 Runtime 时返回 False，
  不再打「已暂停/已停止」的 info，改打 `left a Runner runtime unpaused/pool` 的 warning。

**顺带清理**：pause 里「再次检查 cancel 标志」原先被上移到 swarmflow 停放之前，与首次检查之间没有
`await`，分支不可达；现在放回 `_pause_swarmflow_runs` 之后、`Runner.pause_agent_team` 之前，
`test_pause_session_runtime_aborts_after_park_when_cancel_arrives` 覆盖这条路径。

## 第四次评审：pause 的成功判定按状态看池

评审指出 pause 的成功判定仍在用状态盲的池检查：`Runner.pause_agent_team` 返回 False 后，只要池里
还有该会话的条目就判失败，而池里剩下的可能正是 PAUSED 条目——恰恰是 pause 要的结果。这与函数自己
声明的契约（「池里不再留下**未暂停**的 Runtime 才算成功」）相反，也与紧接着的「已停泊早退」自相
矛盾：同一个「池中只有 PAUSED 条目」的状态，早退分支判成功，兜底分支判失败，区别只在于是否还挂着
stream task 等其他句柄。

触发形状：会话 metadata / active 标记里记的队名与池中条目的队名不一致（团队重建过，池里停泊的是上一个
runtime），`Runner.pause_agent_team` 按名字找不到条目返回 False，池里那个 PAUSED 条目却让状态盲的检查
判成「仍有一个未暂停的 runtime」——网页「停止」于是给出 `success=false`，界面按失败处理，而团队实际
已经停住。

修复：池归属判据加 `include_paused` 关键字参数，缺省 True 保持 stop / terminate 的口径（它们必须把
停泊条目也视为清除失败），pause 传 False，只把**仍未暂停**的条目当作「pause 没落到」。三条路径仍是
同一实现、同一实现里的一个开关，不是第二份判据。`list_active_teams` 异常仍按「仍持有」处理，pause 在
这种情况下继续报失败（Runner 本就没确认，不能凭空判成功）。

新增三条用例：
`test_pause_session_runtime_reports_success_when_pool_only_holds_paused_entry`（队名过期 + 池中
PAUSED 条目 → True；在状态盲实现上断言失败）、
`test_pause_session_runtime_reports_failure_when_pool_unlistable`（池不可列举 → False，锁住异常口径）
与 `test_pause_session_runtime_reports_failure_when_pool_also_holds_running_entry`（同一会话的
PAUSED 残留 + RUNNING 条目 → False）。第三条是评审判据：只有全部匹配条目都是 PAUSED 才算干净；
把它误写成「存在任一 PAUSED 就判成功」（`any(state == PAUSED)`）时，前两条加
`test_pause_session_runtime_reports_failure_when_runtime_stays_unpaused`（池中 RUNNING → False）
三条用例都仍是绿的，只有混合池这条会失败——实测在注入该错误实现后正是它单独转红。
stop / terminate 的 PAUSED 池用例继续守护「没有被放宽」的一面。

**已知遗留（本次不动）**：`stop_paused_session_runtime` 仍是 stop 失败后照旧
`_stop_runner_team_agent_transport` 且从不释放 A2X 的形状，`pause_session_runtime` 中
`Runner.pause_agent_team` 恢复后的 `CancelledError` 分支也仍以 `_resolve_session_team_name` 取名。
二者的池归属判据可以按同一约定下沉，但不在本轮评审范围内。

## 行为验证

隔离环境（`JIUWENSWARM_DATA_DIR` 指向数据目录副本，模型端点为本地 OpenAI 兼容服务），真实网页操作：

- 团队回合结束、后台 workflow run 仍在跑时点「停止」：`pause team session runtime` 出现，
  core 日志 `[bg-ctl] pause targets=['wf_…']`，run 从 active 移到 paused。修复前后均成立。
- 同一状态发 `chat.interrupt {intent:"cancel"}`：`cancel_session_runtime` 接管，
  `[bg-ctl] stop active_targets=['wf_…']`，run 的 journal WAL 写下
  `{"type": "seal", "terminal_status": "stopped"}`，卡片转终态。
- 以上两条在这两次实测里都没走到 `terminate_session_runtime`。原因是 `AgentRuntime.normalize_request`
  （`jiuwenswarm/runtime/service.py:3920`，由 `cancel_request` 在分类前调用）会从会话 metadata 回填
  `params["mode"]`，页面自动补发的 `{session_id, intent:"cancel"}`（不带 mode）因此仍被判为 team 请求，
  落到 `cancel_session_runtime`。`is_team_params` 本身只看 `request.params`（`server/utils/utils.py:38`），
  所以路由取决于 metadata 是否带 `mode`，而不是请求体。
- spec 设想的「先暂停一次、再发无 mode 的 cancel」自然支也不会复现：`cancel_session_runtime` 本来就带
  `has_swarmflow_runs` 判据，而 pause 用 `finalize_workflows=False` 保留了 controller 与停泊票据，
  该路径不会被早退挡住。

定向用例（`tests/unit_tests/agentserver/test_team_manager_registry.py`）覆盖三条早退状态
（只剩后台 run、只剩已停泊 run、只剩镜像），其中「只剩后台 run」分 team_name 可解析与不可解析
两支（前者还断言 `_pause_swarmflow_runs` 早于 `Runner.pause_agent_team`），另含 `terminate` 的调用
顺序与镜像清理，以及 `cancel` 有意早退的回归守护。该文件在 `-o addopts="--asyncio-mode=auto"`
下收集 90 条（80 个测试函数 + 参数化展开 2/3/8）。

评审补强后又加固一条、新增四条：`terminate` 的镜像清理改为断言真实的 `_runner_team_agents` 弹出，
并断言传给 `Runner.stop_agent_team` 的 `team_name` / caller 与传给 A2X 释放的 `team_agent` 是镜像
里的那个对象；新增「metadata 无名、只剩池中 PAUSED 条目」「只剩池中 RUNNING 条目」两条兜底解析
用例、「stop 失败后保留 A2X 与 transport、返回 False」以及「`list_active_teams` 抛异常按仍持有
处理」的用例。

约定下沉这一轮再新增六条：「真实已停泊会话（带镜像 + 池中 PAUSED）再收到 pause 直接跳过且返回
True」、「一次网页停止的两次 pause 调用都返回 True」、「pause 按池兜底停下 RUNNING runtime」、
「pause 失败（池中仍未暂停）返回 False」、「stop 按池兜底解析 team_name 并停下」、
「stop 失败后保留 A2X 与 transport、返回 False」，另加「停放 run 期间 cancel 到达则中止 pause」
覆盖不可达分支的修复。该文件因此 90 → 100 条，全部通过。

第四次评审再新增三条（见上节），该文件 100 → 103 条，全部通过；同目录 `runtime/` 下
`test_team_session_lifecycle.py`、`test_runtime_service.py`、`test_web_cron_cancellation.py` 与
`test_interface_interrupts.py` 合计 208 条同轮回归通过。

本补丁不包含 `sync_documents.py` 的索引重排（5 个索引文件、47 篇文档，含 `gateway/*` →
`agent_gateway/*` 这类既有漂移与全量 author 改写），沿用仓内既有做法，索引由合并侧统一刷新。

## 可达性与对外表述

- 缺口二（terminate 不驱动 controller）在会话 metadata 正常（带 `mode: team`）时**用户不可达**：
  `normalize_request` 会把取消请求回填成 team 请求，交给本来就正确的 `cancel_session_runtime`。
  但 metadata 缺失、不可读或没有 `mode` 时，`normalize_request` 原样返回请求（`service.py:3936-3949`），
  请求落到非 team 分支的 `_cancel_team_work_for_session`，terminate 就会执行——这正是保留该修复的理由，
  不是纯防御代码。保留修复，按「兜底路径加固」表述，不声称是用户可见故障。
- 缺口一在正常回合结束后同样不会命中（monitor 与镜像仍在）。能被真实点击触发的只有「第二次点停止」
  这一类，且此时 run 已停泊；「后台 run 继续心跳」需要监控注册缺失的竞态，只能在隔离实例里注入复现。
  修复按「跨路径判据收敛 + 竞态加固」表述。
- 因此本次改动对外定位为：把四条停止路径的判据收敛成一处，并补齐 terminate 的 controller 驱动、
  seal 语义与镜像清理，不宣称修复了用户可复现的「停止不生效」。
