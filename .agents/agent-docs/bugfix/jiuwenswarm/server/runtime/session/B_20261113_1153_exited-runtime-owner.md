---
id: B_20261113_1153_exited-runtime-owner
title: Worker 退出未回收时存量会话无法重新认领
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/runtime
files:
  - jiuwenswarm/server/runtime/session/lifecycle.py
  - tests/unit_tests/server/test_runtime_owner_identity.py
  - tests/unit_tests/agentserver/test_worker_session_owner_recovery.py
  - tests/unit_tests/agentserver/fixtures/session_owner_worker.py
designs: []
design_note: 修复既有存活属主保护与 Worker 退出判定的不一致，不新增持久化格式或接管协议，无独立设计。
---

# Worker 退出未回收时存量会话无法重新认领

关联 #6563、develop !10132 与 releases/0.2.9 !10133。
Front 查询阻塞的另一修复见 [单项元数据查询记录](../../control/B_20261113_1051_single-session-metadata-io.md)。

## 现象与影响

新 Worker 已 ready 不代表旧会话已恢复。存量会话首次执行经过
`AgentRuntime._ensure_session_registered → _register_session → claim_runtime → assert_runtime_owner`，
读取 `agent/lifecycle/resources/session_<id>.json` 的 `runtime_owner`。
旧进程已退出且被父进程回收时，原实现已经允许接管；缺口是已退出但尚未回收的 Linux 僵尸进程。
它保留 PID、`create_time` 与 `/proc/<pid>/stat` 的 `starttime`，原检查仍判为存活，
持续返回 `another live AgentServer owns the session runtime`，业务无法重新进入。

附件确有替换 Worker ready 后大量旧会话 ownership 拒绝，但缺少旧属主 JSON 与原 PID 状态。
真实 Linux 对照证实此代码缺陷存在，不能据此将现场全部拒绝唯一归因于僵尸进程。

## 契约、根因与修复决策

`resident_lifecycle._alive` 已排除 `STATUS_ZOMBIE`，会话所有权却只检查进程身份，二者对退出的定义不一致。
身份核验匹配后增加进程状态检查，已无其他线程的 `STATUS_ZOMBIE` 与 `STATUS_DEAD` 不再阻止接管；
PID 不存在仍允许接管，活进程（含 sleeping / stopped）仍受保护。
僵尸主线程仍有其他线程时继续保护属主；线程数无法核验也继续拒绝接管。

保留各分支原有 PID 复用核验、启动时间与 HarmonyOS start-ticks 回退策略；
身份或状态无法核验时继续拒绝接管。所有权更新仍在原跨进程资源锁内由 `claim_runtime` 原子保存。
不按 Worker ready 批量删除所有权文件，不按报错无条件覆盖活属主，也不延长超时。
这避免两个仍能写盘的进程同时占有同一会话。

English summary: after matching the recorded process identity, exclude fully exited zombie/dead owners
before blocking Session adoption. Zombie leaders with other threads, live and unverifiable owners remain protected.

## 回归证据与适用范围

基线为 develop `9f403d1ca27cfb73a5521a7c7d7e1f517c0dc61d`、
0.2.9 `61246126c5a546c9eb412ae04fd0fec0940a078f`，分别保留本分支依赖锁。

- 两分支未修复的状态回归均失败：身份匹配的 zombie/dead 被判存活。
- 独立 QEMU / Alpine Linux 3.22、内核 6.12.115、Python 3.12.16、psutil 7.0.0：
  执行固定版本生命周期源码与生产文件锁，隔离不相关依赖导入和数据目录，使用真实 `fork` 子进程。
  四组 baseline/fixed 对照中，旧属主均先成功落盘；存活时接管被拒；退出后保留原生 `Z` 状态且不 `waitpid`。
  两个 baseline 均继续拒绝，两个候选均成功保存新 PID；最后回收子进程后两个 baseline 也可接管。
  metadata/history 字节保持不变。该实验是 Linux ownership 路径验证，没有启动完整 Linux Worker 或模型。
- Windows / Python 3.11.15 / 分支锁定环境：真实 Worker main、Supervisor、OS IPC、Dispatcher 与
  `AgentRuntime._ensure_session_registered/_register_session`、真实 Coordinator 及持久化所有权共同执行。
  跨构建驻留 Worker 替换和异常退出并回收后，原会话 ID 继续执行；新 PID 成为属主，原历史保留并追加新结果。
  两个同时存活的 Worker 争用同一旧会话时，新 Worker 被拒，原 Worker 仍可继续处理。
  业务结果为确定性替身，不连接模型。Linux 专属完整 Worker 僵尸场景已写入同一回归，Windows 跳过该一项。
- 证据与源码指纹保存在本地 `artifacts/issue6563-owner-recovery-20261113/`；
  首轮真实 Worker 夹具因测试身份/目录装配错误未通过，修正测试夹具后才完成上述验证，未将初始化失败算作缺陷复现。

最终定向选择包含身份核验、Front 查询与租户上下文、生命周期读重试、归档、群会话接管、
Runtime service、Gateway 元数据消费者、Front 路由及上述真实 Worker 场景：
develop **466 passed / 1 skipped**，0.2.9 **464 passed / 1 skipped**。
跳过项均为完整 Worker 的 Linux 真僵尸用例；另行执行的 Linux ownership 对照四组全部符合预期。

## 防错模式与遗留

进程存在、IPC ready 和业务会话可重新执行是三个不同观察点。
所有权保护应同时判断 PID 复用身份与进程是否仍能执行，不能把尚未回收的进程表条目当成活写入者。
回归必须进入旧会话并核对新属主、旧历史和请求结果，不能只断言新 Worker ready。

未进行原现场长稳或真实模型回放；全量远端 CI 以候选 SHA 的 CI 为准。
原现场属主 PID、进程状态与父进程回收条件仍需现场证据确认。

## Linux UT 失败与续验（2026-11-13）

附件 CI 检出 develop `7d170a621`：25344 passed / 204 skipped / 1 failed，唯一失败为
`unreaped-zombie` 在第二个 Worker 的 IPC 握手阶段发生 `Connection reset by peer`，尚未进入所有权断言。
本轮基于该提交及 release `b6b48a91d` 建立各分支锁定的完整 Linux 环境。
旧用例在 release 实跑为 3 passed / 1 failed，复现同一握手堆栈；develop 一轮为 4 passed，说明存在竞态。

Linux 原生控制实验进一步证实，主线程 `pthread_exit` 后状态为 Z、num_threads=2 时，其他线程仍持有
共享文件描述符、flock 生命周期锁，Unix listener 仍能接受 connect。两个原候选的实际
`runtime_owner_alive` 都会把这种属主判为退出；这修正了初轮“所有 Z 均已无写入者”的过宽判断。

续修包含两部分：

- 真正的旧 Worker 僵尸用例在启动替换前，等待状态为 Z、线程数收敛为 1、原 IPC 生命周期锁释放。
  不提前 waitpid，不删除所有权文件，不跳过 Linux 场景，也不靠增加重试或固定睡眠掩盖竞态。
- 生产属主核验对 Z 且 num_threads>1 继续返回存活；无法读取线程数时保持 fail-closed。
  原 PID/启动身份、资源锁和原子保存路径不变。原生双线程 Z 对照在两个候选上均继续受到保护。

QEMU/WHPX、Linux 6.12.115、Debian glibc、Python 3.11.2、psutil 7.2.2，Core/Gateway/Protocol
均按各分支 `uv.lock` 安装。按 CI 的 xdist 3.8.0、`-n 2 --dist=worksteal --timeout=60` 执行：
develop 68 passed、0.2.9 65 passed，无跳过；每分支四个真实 Worker 参数全部通过，
其中完整 Worker 的未回收僵尸场景再各独立连续通过 3 轮。
Windows 更广选择分别为 502 passed / 1 skipped 与 500 passed / 1 skipped，跳过项由上述 Linux 实跑补齐。

没有抽取单独的“终态状态集合”：Z 还必须结合线程数，Supervisor 退役还受 Popen/生命周期锁约束，
仅共享集合不足以表达退出条件。当前分别由本模块的所有权边界和完整 Worker 资源屏障回归约束。
证据、源码指纹、依赖、负向对照与重复执行结果保存在 `artifacts/issue6563-ut-review-20261113/`。
