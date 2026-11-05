---
id: B_20261105_1510_stream-heartbeat
title: Socket 心跳被业务循环阻塞及卡顿缺少业务归因
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/ipc
  - jiuwenswarm/server/worker
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/server/ipc/io_loop.py
  - jiuwenswarm/server/ipc/stream.py
  - jiuwenswarm/server/worker/stall_context.py
  - jiuwenswarm/server/worker/service.py
  - jiuwenswarm/server/worker/dispatcher.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - tests/unit_tests/agentserver/test_worker_shutdown_deadline.py
  - tests/unit_tests/agentserver/test_worker_loop_liveness.py
  - tests/unit_tests/server/test_handler_dispatch_agents.py
designs: []
design_note: 用户明确要求补齐现有心跳隔离实现并增加卡顿归因，沿用既有 30/180 秒健康策略，无独立设计。
---

# Socket 心跳被业务循环阻塞及卡顿缺少业务归因

## 现象与影响

Issue #6093 的视频合成案例暴露业务循环同步等待时，通信心跳可能一起停顿。实现起点为远端 develop `9e20edf206f7f6417edfa1b45807280daa0fe1f1`，交付前快进至重新拉取的 `b9820bf9342eb61cb344216bdf73b45868238f3f`；两者之间未修改本次 IPC、Worker、Supervisor 或依赖钉点。基线的 Windows named pipe 已有持续独立 reader，但 Unix domain socket 和 loopback TCP 的心跳仍由业务循环处理，可能先命中 30 秒通信超时；已有 180 秒持续业务卡顿恢复机制不能发挥预期作用。原有日志也未标明实际阻塞的业务。

## 关联设计与当前契约

本次补齐既有隔离契约，关联 [S_01_worker-heartbeat](../../../specs/jiuwenswarm/server/ipc/S_01_worker-heartbeat.md)，不改变 IPC 版本、平台传输选择、鉴权凭据、常驻 Worker 语义或 30/180 秒阈值。业务视频处理和前端终态问题不属于本次扩展范围。

## 根因与修复决策

1. 将 socket 建立、鉴权、读取、写入和关闭统一交给进程级专用 I/O loop，跨 loop 调用通过线程安全 future 桥接。直接复用现有 asyncio stream，避免两个线程竞争同一个 socket 或跨线程直接访问 transport。
2. socket 持续读取整帧，心跳立即回复，其他消息进入与 pipe 相同上限的有界队列。普通写入和整帧 pong 均由 I/O loop 串行执行。保留旧版心跳和现有生命周期消息。
3. 业务循环 task factory 链只传播白名单诊断标识，保留原 factory 和显式 context；请求及其后台子任务共享可关联的标识。I/O 线程采样实际运行 Task 和最多 16 层栈位置，避免把并发无关请求误列为原因。
4. 30 秒告警、180 秒重启和 Supervisor 诊断快照保留同一归因信息。首次实进程验证发现，30 秒 watchdog 可先于该周期 pong 被消费；将采样门槛提前到 5 秒，使之前的 10/20 秒 pong 已包含归因。告警和重启门槛保持不变。

拒绝仅增加同一业务 loop 上的 heartbeat task，因为它仍会随业务阻塞；拒绝在后台线程继续调用原业务 loop 回复心跳，因为仍需原 loop 调度；不增加第二条心跳连接，以免维护两套鉴权和 generation 生命周期。线程不能替代阻塞业务治理，也不能保证长期持有 GIL 时的响应。

## 回归证据与适用范围

依赖源码固定为 agent-core `4dbaa84ca568af3f181da4f9cd26b713a5b73039`、agent-gateway `a9e4d447033f0eadf5e8583c06cfb8993e01e539`、protocol `cbbadde9101db6e3d75e4ef2b9bdb6029fd92088`；Python 3.11.15。初步通过隔离 `PYTHONPATH` 使用对应 worktree，最终以独立 venv 的 `.pth` 固定这些来源，以兼容会重设 `PYTHONPATH` 的子进程生命周期测试；已核对各模块的 `__file__`。

- 改动前，心跳/loopback 的定向基线：107 passed，14 deselected。
- 初步定向：37 passed，4 skipped；包含新 TCP 阻塞、后台子任务归因、有界队列、取消与 Supervisor 转发测试。
- 最新基线的最终回归：14 个 IPC、鉴权、Worker 生命周期/恢复定向文件，289 passed、12 skipped、14 deselected；使用 `-m 'not slow' --no-cov -q -o log_cli=false`。跳过项为本机不适用的 POSIX/Unix 专用用例，slow 故障注入另行执行。按仓库约束未运行格式化或静态检查。
- 独立 Worker 60 秒同步阻塞：Windows resident pipe 与 front_loopback TCP 均通过；阻塞期间排队三个其他 session 请求，恢复后全部完成，同一 Worker PID、零重启，并留有约 25 秒原生栈转储。
- 首轮真实 180 秒验证：通信心跳持续，约 180 秒按业务卡顿进入恢复；首条 30 秒告警缺少归因，测试失败。提前采样后的完整复验通过：首条告警约 30.0 秒即包含实际 request/session/method 和阻塞函数；约 180.219 秒按业务卡顿策略清退旧 Worker，旧进程退出码 15，新 Worker 就绪并完成新请求。该场景未修改默认 30/180 秒阈值。

测试替换 Agent Runtime 构造为仓库的确定性故障注入 fixture，保留生产 Worker service/dispatcher、Supervisor、真实子进程与操作系统 IPC；不代表真实模型或桌面视频合成验收。测试数据与原始日志位于工作区之外的 `.codex-temp/stream-heartbeat-20261105`，不提交运行日志。

### PR !9604 评审修订

基于已发布提交 `7e996297246df3e5e6632048c779cba14afd3bcd` 逐项核对评审意见和远端 UT 报告后，修复以下问题：

- I/O 线程修改写锁等待者时，业务线程直接遍历同一字典可能抛出异常。等待者增删与诊断读取改为共用线程锁，且不在锁内等待 I/O；Supervisor 对指标采集、快照序列化及输出增加尽力而为保护，诊断失败不能终止 watchdog 或跳过转发失败处理。
- 原 POSIX-only slow 测试仍期待 60 秒业务冻结触发 30 秒传输超时，与新契约冲突。改为真正屏蔽 pong、保持业务循环响应的故障注入，在 resident 和 front_loopback 两种传输验证 30 秒超时仍生效；60 秒业务冻结存活用例也改为跨平台执行。
- 新异步用例增加显式 asyncio 标记，以支持 strict 模式。评审中“仓库默认非 auto、核心用例已被跳过”的判断不成立：`pytest.ini` 已指定 `--asyncio-mode=auto`，原提交的 Linux CI 中 13 个新 socket 用例全部通过，包含 Unix domain socket；本地跳过项是平台不适用的用例。
- 保持 256 条/64 MiB 的有界积压失败策略，补充单独告警和计数，区分队列溢出与 EOF。未采用暂停整个 transport 读取的建议，因为同一连接上的心跳也会随之停止。
- 远端 CI 的全局 60 秒 pytest-timeout 会终止真实 60/180 秒用例；为相应用例设置 120/150/300 秒测试预算并注册标记。生产 30/180 秒阈值保持不变。

修订后的 Windows 本地验证使用与 CI 相同的 `--timeout=60`，由用例级标记覆盖长场景预算：

- 14 个定向文件：293 passed、12 skipped、15 deselected，包含并发写入诊断及 IPC/Gateway/序列化故障下的恢复回归。
- 显式 `asyncio_mode=strict` 的两个心跳/存活文件：32 passed、6 skipped。
- 真实进程场景：5 passed。两种传输真正缺失 pong 时约 30.1～35.1 秒触发 30 秒传输超时；60 秒业务阻塞存活和 TCP 排队请求均通过；180.407 秒后清退旧 Worker，30 秒告警与重启日志都包含同一请求及阻塞函数，新 Worker 完成后续请求。计时包含 5 秒 watchdog 检查周期。

原提交的远端 Linux UT 为 21716 passed、125 skipped、19 failed，其中 4 个失败涉及本次修正的旧用例和长测试预算。其余 15 个失败涉及 CUA、Web 语言配置、子代理导入及系统提示词；在干净基线 `b9820bf9342eb61cb344216bdf73b45868238f3f`、同一组固定依赖下，相关四个文件复现 13 failed、120 passed，修订版本得到相同的失败用例集合及通过数。两项子代理导入失败未在该定向运行中复现，不能据此认定它们的根因。未扩大本次修改范围。修订验证数据保存在工作区外 `.codex-temp/stream-heartbeat-review-20261105`。

### CodeCheck 与第二轮 Linux UT 修订

用户提供的未解决 CodeCheck 导出包含两项：`io_loop.py` 的生产 `assert`（G.TES.01）改为显式运行时检查，避免优化模式删除检查；Supervisor 栈字段过滤的复杂推导式（G.EXP.04）展开为普通循环，保留整数行号校验、16 个输入帧上限及字符串长度约束。

附件 UT 日志运行的是 `42f761d0c774d1e9926e3e728353877dca811476` 与 develop `e2571b49496a34e4d02e74c88d2922c879667c55` 的合并版本，结果为 16 failed、21745 passed、124 skipped。新发现的 IPC 失败发生在真实 180 秒场景的下一代 Worker 就绪等待阶段：30 秒归因告警和 180 秒恢复决定均正确，但 POSIX Worker 的 SIGTERM 回调也要等待被阻塞的业务循环；首次进程等待耗尽了全部 5 秒清退预算，之后 SIGKILL 的确认等待得到 0 秒，Supervisor 因无法确认退出而进入 fatal。

修复在同一退出期限内为强制终止后的退出观察预留最多 1 秒，短预算最多预留剩余时间的一半；不延长清退期限，不改变 30/180 秒阈值。仍检查 PID 和创建时间，仍要求确认旧 Worker 退出后才恢复下一代；未确认退出和身份冲突保持失败处理。新增确定性回归在 0.5/5 秒预算下均先复现旧实现失败，再验证修复；另增加 POSIX 原生忽略 SIGTERM 子进程用例。

为解决远端索引冲突，合入当时最新 develop `f28b10207`，合并提交为 `2b7a03979`。该基线已修复 BrowserTaskPromptRail 重复构造器和 Web 语言测试初始化，消除了前述 13 项既存失败。其余两项导入失败已补充顺序复现：`test_handler_dispatch_agents.py` 直接覆盖 `sys.modules` 且未恢复，导致后续 `AgentDefinition` 导入失败；同一顺序在旧干净基线复现 2 failed、33 passed。测试替身改用 pytest monkeypatch，在用例结束后恢复原模块。

修订后的 Windows / Python 3.11.15 验证：

- IPC/Worker 的 14 个定向文件：296 passed、13 skipped、15 deselected；其中新增的 POSIX 信号用例在 Windows 跳过。
- 原 CI 其他失败及污染源所在的 5 个文件，按污染源先于子代理用例的顺序运行：164 passed；修复前同一组合为 2 failed、162 passed。
- 真实 180 秒 TCP 业务冻结复验：1 passed，约 180.188 秒触发恢复，告警与重启均含正确业务归因，旧 Worker 已退出，新 Worker 完成后续请求。

上述为本地行为证据，不等同于修订后 Linux 全量 UT 或远端 CodeCheck 通过。按仓库约束未运行本地格式化或静态检查；原始附件、复现日志和 XML 结果保存在工作区外 `.codex-temp/stream-heartbeat-ci-20261105`。

## 防错模式与资料同步

同步中英文日志说明和当前 IPC spec。关键回归覆盖普通业务帧不得终止心跳读取，以及首条告警必须已有实际阻塞业务上下文。记录仅保留标识与栈位置，并在消费端重新限制大小与字段。

## 遗留与后续

本机为 Windows；原提交的 Linux CI 已通过 13 个新 socket 用例，当前修订的原生 Linux/macOS 运行仍待新一轮 CI，HarmonyOS 真机未验证。本次已根据用户提供的两项 CodeCheck 导出修复对应代码，远端复验状态单独报告。
