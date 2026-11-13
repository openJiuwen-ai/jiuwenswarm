---
id: B_20261113_1051_single-session-metadata-io
title: 单项会话元数据查询阻塞 Front 的 IPC 消费
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/control
files:
  - jiuwenswarm/server/control/repositories/session_repository.py
  - tests/unit_tests/server/test_control_query_execution.py
  - tests/unit_tests/server/test_session_repository.py
  - tests/unit_tests/agentserver/test_ipc_backpressure.py
designs: []
design_note: 在既有 Front 有界查询执行边界内补齐单项查询，无独立设计；公开响应与持久化语义不变。
---

# 单项会话元数据查询阻塞 Front 的 IPC 消费

关联 #6563 的 IPC 触发路径。目标为 develop 与 releases/0.2.9。
本记录不替代 [IPC 突发与恢复记录](../ipc/B_20261110_1042_issue6191-ipc-recovery.md)：
后者的接收容量等待与断连恢复保持有效，本次补齐一个仍会阻塞消费者的查询入口。

## 现象、根因与现场边界

`session.get_metadata` 经 Front 的 `MethodRouter` 进入 async handler，内部却同步执行
`get_session_metadata(cache_bust=True)`。其中的 `_FILE_LOCK`、文件读取及字段推断可占住
Front 事件循环；独立 IPC 线程继续接收流式帧，256 条队列满后可能耗尽 0.5 秒等待预算。
已有项目和会话列表查询使用 `run_control_query`，单项查询未覆盖。

附件在 2026-11-12 23:41:32.421 记录该查询，23:41:32.922 报接收超限，
256 条共 425571 字节。时间相关与受控复现支持该代码缺口，但缺少现场 Front 堆栈，
不能认定现场一定卡在 `_FILE_LOCK`。Worker 替换后的 ownership 冲突属于另一恢复环节，
不因本修复宣称已经解决。

## 修复决策与保持的契约

- handler 将完整同步读取与响应构造交给既有 `run_control_query`，不新建线程池或查询框架。
- 保留调用者 ContextVars、租户目录绑定、每循环最多四个后台查询及取消后的容量保留。
  请求取消不终止原生 I/O；原生工作真正完成才释放名额。
- `cache_bust=True`、`enable_writeback` 默认与非布尔值回退、请求身份、响应载荷及错误码保持原行为。
  不移除数据一致性锁，不改变 IPC 容量、超时、协议、依赖或磁盘格式。
- Front 的租户绑定与企业装配仍在路由线程完成；后台工作继承已经建立的上下文。
  Runtime snapshot 的获取仍由原路由在元数据响应成功后执行。

English summary: single-session metadata reads use the existing bounded Front query executor.
File-lock waits, disk reads and inference no longer occupy the Front event loop; response and writeback semantics remain unchanged.

## 行为验证与适用范围

两个分支从各自上游创建隔离工作树，使用 `uv sync --locked --group test` 的独立 Python 3.11.15 环境。
基线 develop `4b7a8d3f9ba1233636a432a47dfcd9c2b8959a11`，
0.2.9 `76e877929e4b14ba63a42e3cc75ca0900b63a4ab`。

- 新增实际元数据文件锁回归：两个未修复基线均在 Front 无法继续调度的断言处失败。
- 定向验证包含读取选项、正常/无记录/I/O 错误、非法 ID、租户与请求上下文及真实企业目录绑定。
- 真实 Worker / Supervisor / OS IPC / 本地 WebSocket 用例覆盖单项查询等待时持续消费 320 条有序消息、
  原 Worker PID/创建时间保持、零重启和进程退出回收。业务输出使用 TrafficRuntime 替身。
- 独立文件锁实验通过生产 StreamConnection 和读取入口验证，保留 256 条/64 MiB/0.5 秒配置。
  原始日志、JUnit、命令与分支依赖记录保存在本地 `artifacts/issue6563-metadata-fix-20261113/`。

最终基线推进至 develop `f5638897d03df426c82fd3a9e9417b59cde7f504`、
0.2.9 `17d424e2c0fa27f7f6a4d495df6a852a4d0b0cb6`，相关查询、IPC 与依赖锁未变化，
两分支均重新通过 84 项直接调用方、上下文与真实 Worker 回归。
此前更广选择中 0.2.9 为 164 passed；develop 为 163 passed / 1 failed，失败是并发上传同名文件用例，
候选单项原样重跑通过，未修改基线的该文件 75 项也通过；保留该偶发结果，不计作整组通过。
独立锁竞争实验中两分支均在元数据等待期间继续消费，700 条消息全部有序送达，无 IPC 错误。

未运行真实模型、原环境 Linux 长稳、全量 CI 或格式化/静态检查；
本地通过不替代远端 CI。若候选、依赖或租户装配前提改变，重验对应路径。

## 防错约束与遗留

Front async 查询调用同步元数据入口时，应将锁等待、磁盘读取和推断整体纳入受限后台工作，
不能仅凭 async 声明或“单个 JSON”判断不会阻塞。实际文件锁、上下文与消息消费回归锁定该边界。
公开协议未变化，不另建接口 spec；当前契约由原 E2A 类型、路由与行为测试维护。
现场具体阻塞阶段及 Worker ownership 冲突仍需各自的证据和修复。
