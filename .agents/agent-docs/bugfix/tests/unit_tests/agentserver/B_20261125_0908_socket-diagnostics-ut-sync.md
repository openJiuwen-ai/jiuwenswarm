---
id: B_20261125_0908_socket-diagnostics-ut-sync
title: Socket 写锁诊断 UT 的跨线程就绪同步
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - tests/unit_tests/agentserver
files:
  - tests/unit_tests/agentserver/test_stream_heartbeat.py
designs: []
design_note: 修复既有诊断测试的跨线程前置条件，保持真实传输和行为断言，无独立设计。
---

# Socket 写锁诊断 UT 的跨线程就绪同步

## 交付落点

测试修复首次随 develop [PR !10796](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10796) 的
`725c2b004886612f1f64d2bd88189fe9e76b62a5` 提交，实际文件为 `tests/unit_tests/agentserver/test_stream_heartbeat.py`。
0.2.10 由 [PR !10933](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10933) 的首个提交
`8ed9f7f00f43262a4e654706369388b0a6ce6a26` 移植，目标基线为 `f554e7717`。
该文件的跨线程等待同步改动属于此 PR，且已在该候选上执行；下文历史验证继续限定到原提交与实验条件。

## 现象与报告版本

用户提供的 `75dc7163-c64f-4e32-94ad-895d74b3572c.zip` 测试 PR !10796 的 `33aa8caef`
合入 develop `8982323c6`。Linux / Python 3.11.5 / pytest 9.2.0、xdist 两个 worker 的结果为
28,406 passed、247 skipped、27 subtests passed、1 failed。

唯一失败是 `test_socket_diagnostics_remain_available_while_writers_wait`：
等待 `_write_waiters` 非空的 2 秒前置条件超时，尚未执行后面的诊断和消息完整性断言。
[上次登录回调 UT 的修复](../common/B_20261124_1812_auth-callback-ut-race.md)两个参数分支均已通过。

## 根因与修复决策

测试在调用线程创建 100 个发送任务，再轮询 I/O 线程是否已开始等待写锁。
派发与跨线程入队的耗时被当成诊断正确性的前置条件；日志能确定该等待超时，不能据此确认生产诊断方法发生故障。

在候选及未修改的 CI 基线上，给发送批次增加 2.2 秒异步派发延迟，均复现原测试的 2 秒超时。
受控实验保持整个批次的调用顺序，不通过逐任务定时器改变消息顺序。
相关生产 `stream.py` 及原测试在两个待比较提交中的内容相同。

修复后先通过 I/O 线程的调用应答确认写锁已持有；在实际锁获取入口观察到等待者时，
通过调用线程事件循环的线程安全通知唤醒测试。等待就绪时同时观察发送任务，提前发生的发送错误直接上报。
清理仍在 I/O 线程释放锁，并回收就绪等待和全部发送任务。

真实 TCP、锁确实存在等待者、等待期间与发送期间的诊断访问、100 条帧完整性和顺序断言均保留。
生产 IPC、进程身份代码及测试整体超时未修改。

## 验证与边界

- Windows / Python 3.11.15：本次失败用例常规派发 20 轮、批次延后 2.2 秒 20 轮，均通过。
  延迟组逐个检查执行耗时不短于 2.2 秒，确认故障注入实际生效。
- 6 个相关文件最终回归：**160 passed、8 skipped、23 deselected**，包括 socket 心跳、原始输出路由、
  Worker 入口和诊断、进程身份以及前次认证 UT 修复。
- 单列排除 18 个 slow 用例与 5 个未修改的 Windows 时序用例，不计为通过。
  后者三个在原 CI 基线整文件运行中失败；`stale-resident` 在基线单项重复 5 次中失败 4 次；
  `disconnected-resident` 在候选回归中失败，基线原生 5 次通过，但在允许合法业务应答先于关闭完成的受控交错中复现同一断言失败。
  这些失败与最初模块回归结果均保留，不以本次修复覆盖其结论，也不修改它们的断言。

原始附件、版本对照、注入脚本和 JUnit 位于本地隔离目录
`D:/work_space/.codex-temp/harmony-process-identity-ut-20261125/`，不提交运行产物。
本次未在本机重跑 Linux 全仓 CI；新提交的门禁结果应按实际提交号重新核对。
