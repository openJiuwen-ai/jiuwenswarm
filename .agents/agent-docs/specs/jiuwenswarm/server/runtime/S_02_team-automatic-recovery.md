---
id: S_02_team-automatic-recovery
title: 普通持久 Team 的 Worker 换代自动续跑
type: S
author: MengHan18
author_email: hanmeng22@huawei.com
modules:
  - jiuwenswarm/server/runtime
  - jiuwenswarm/runtime
  - jiuwenswarm/agents/harness/team
files:
  - jiuwenswarm/server/runtime/team_recovery.py
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/runtime/service.py
  - jiuwenswarm/agents/harness/team/team_manager.py
---

# Worker 换代后的 Team 自动续跑契约

## 适用范围与事实源

普通 `team.code.normal` / `team.work.normal` 持久 Team 在真实工作提交时登记恢复意图；旧模式名经公共模式规范化。检查 TeamSpec lifecycle，仅持久团队可以登记；应用 NPC 团队、临时有界任务、输入观察者、组织群聊、Design/Plan 模式不由此扫描器接管。升级前没有意图记录的会话不根据旧 busy 自动恢复，需要一次正常原会话提交建立意图。

SDK 检查点、任务数据库、既有会话历史分别负责模型运行状态、团队任务和用户指令。本模块只在现有 Session lifecycle resource 中维护 `team_recovery`，不另建目标或工具执行数据库，不保存密钥或重复一份原 Query。

## 意图、接管与执行所有权

- 正常工作开始写入 recipe、部署范围、PID/创建时间、token 和运行意图。recipe 仅含原 Team、渠道、模式、项目、成员组及调度/启动选择。
- 新 Worker 的 Runtime 就绪后启动后台扫描；只接管本部署、仍应执行、原所有者已死亡的意图。跨进程 lifecycle 锁、生命周期 guard、进程创建时间与 token 共同防重。
- 每五秒扫描，最多两个恢复准入并行。一次准入最多 120 秒，不能把这个超时变成团队总执行时限。
- 恢复通过 `AgentRuntime.recover_team_execution` 登记到 Session Coordinator 的 `TEAM_BACKGROUND`，实际 SDK stream 归 Coordinator 管理；启动操作和子任务绑定 Runtime execution context。正常取消和关闭会等待这些工作结束，不创建无人负责的永久协程。
- 不恢复丢失、归档、删除、主动暂停或取消的 Session。新提交、旧回调、恢复接管使用不同 token；旧代次不能覆盖新代次状态。

## 重建与续跑不同

先核对原 Session 绑定与 SDK 持久检查点、Team namespace；缺失即失败，禁止偷偷创建新 Team。用当前装配入口重建原 TeamSpec，保留原 Team 身份和执行选择，调用 SDK 无输入 start 入口；不重发原用户 Query，不重放历史工具。

`team.recovered` 只表示 SDK 运行对象恢复。Leader 已执行时不再送唤醒；待审批或输入时保持等待。Leader 待命时，每代次最多送一次标明 Runtime 来源的恢复通知：

1. 从既有会话历史读取按时间排列的真实用户指令，排除成员审计和自动任务输入。
2. 引用原指令作为任务参考，要求对照检查点、任务、文件和已有操作结果继续未完成工作；不是重新提交各个历史请求。
3. 原指令缺失、超过 64 KiB 或恢复通知不被接受时，明确失败，不截掉早期范围后继续。

这覆盖首次原生轮次尚未持久化上下文就崩溃的情况。它不能保证任意外部操作恰好执行一次；对结果未知的工具必须核对或报告边界，不能自动重复执行。模型是否遵循指令仍需真实工程证据验证。

## 状态、失败与停止

`running/restored` 持有续跑意图；`idle/completed/waiting_for_input/failed/paused/cancelled` 不自动重新派工。恢复就绪或 idle 不覆盖已恢复的待输入状态。新的真实工具活动或显式用户输入按既有生命周期推进。

连续准入最多三次，前两次失败后分别等待 10、20 秒。每次接管重新计稳定时间，稳定十分钟后的实际工具/输出活动清零连续失败数；第三次失败持久化 `recovery_failed`，停止自动尝试。已停止的 token 不被迟到失败回调复活。

停止恢复意图遵守 lifecycle 的写入围栏和运行属主检查；带 token 的调用只允许停止匹配代次。归档/删除的排空阶段仍可撤销恢复意图，禁止新执行的 admission guard 不代替 drain-write guard；完成写隔离后不再改动恢复记录。显式停止尚未接管的孤儿意图不要求先重建实时 Team。

暂停沿用 TeamManager 的结构化 PauseSessionResult 和 request-id round fence；先核对代次与回合，再撤销恢复意图，旧回合的迟到暂停不能停止新回合。浏览器断连走既有 workflow pause 语义，不视为用户永久取消，保留 Worker 换代后的恢复资格。

恢复 consumer 在首事件前正常结束时，必须以准入失败结算等待者，不能挂起或伪造恢复成功。调用取消仍传播并收尾 consumer。流结束时读取恢复快照失败只记录告警，不得跳过终态广播和运行注册释放。

平台已定义的恢复原因可对外展示；其他异常只暴露类型和日志入口，不把任意异常正文中的模型配置或密钥写进公共快照。

## 查询与事件消费

`team.snapshot.recovery` 与 `team.recovery` 暴露 `state/attempts/updated_at/last_progress_at/error`。实时事件在没有 chat waiter 时走已有 server-push 通道。旧客户端可以忽略新增字段，不影响后端自动恢复。

Worker `agent_ready`、恢复运行对象和任务实际推进是三个事实，不能互相替代。客户端可以独立消费新增可选字段；底层 Worker 健康与重启契约见 `docs/zh/runtime-worker-lifecycle.md`。
