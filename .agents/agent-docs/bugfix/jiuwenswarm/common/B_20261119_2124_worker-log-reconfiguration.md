---
id: B_20261119_2124_worker-log-reconfiguration
title: Worker 默认日志初始化与后台处理器关闭的锁顺序修复
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/server/worker
files:
  - jiuwenswarm/common/background_logging.py
  - tests/unit_tests/common/test_background_logging_reconfigure.py
  - tests/unit_tests/common/fixtures/logging_reconfigure_process.py
  - docs/zh/日志.md
  - docs/en/Logs.md
designs: []
design_note: 修复既有后台日志处理器生命周期中的锁顺序，保持日志配置、队列和 Worker 启动契约，无独立设计。
---

# Worker 默认日志初始化与后台处理器关闭的锁顺序修复

## 现象与影响

在 `releases/0.2.9@48f6cfa996526e96bce4d111958c7324e07024cd` 的 Windows 源码隔离环境，生产 Worker 使用默认 foundation 日志 YAML 冷启动时，IPC 已连通而业务循环停在日志重配置，120 秒内无法进入 `agent_ready`。此前测试使用备用日志配置只验证了其他恢复路径，没有修复该阻塞。

该问题来自后台处理器关闭路径，与模型服务、会话 ID 或心跳阈值无关。`develop@2c6b3033950ef7bc73cab9c2babd397be2a1fc2f` 已有提前引导日志的逻辑，但共享的关闭路径仍可被真实日志重配置触发。

## 关联设计与当前契约

续用 `docs/zh/日志.md`、`docs/en/Logs.md` 中的 Worker 日志负载保护约定：日志由后台线程写入，队列有界，关闭失败可见，正常退出限时等待。集中规约中没有该处理器锁顺序的独立 spec。本次修复仅收紧内部关闭顺序，不修改日志配置、IPC、会话协议或依赖版本。

## 根因与修复决策

原始线程转储显示：

1. 主线程经扩展导入触发 foundation `setup_logging()`，在 `logging.config.dictConfig()` 持有 logging 模块锁后，由 `logging.shutdown()` 等待文件 handler 锁。
2. 后台 `BackgroundLogWriter._close_retired()` 同时调用 `FileHandler.close()`，先持有 handler 锁，再在 `Handler.close()` 等待模块锁。
3. 两个线程形成循环等待。传输心跳仍可应答，不能证明业务循环可用。

修复在退役处理器关闭前先获取 logging 模块锁，再进入原有 `handler.close()`，与标准库配置顺序一致；队列状态锁在此之前释放，失败诊断在释放模块锁后写出。这里只使用 CPython 3.11–3.14 logging 内部锁与名称注册表来协调标准库的同一份状态，不引入第二套日志配置锁。

重配置可能已经创建与旧 handler 同名的新 handler。旧 `Handler.close()` 会按名称删除注册项，因此在同一模块锁内保留原先指向其他 handler 的注册；旧关闭删除该项时恢复新 handler，避免后续增量配置找不到它。

未选择关闭默认日志、改用不存在的 YAML、提前导入所有可选扩展或放宽心跳超时作为修复。这些做法没有消除后台关闭与重配置的锁顺序问题。

## 回归证据与适用范围

新增独立子进程回归使用真实 `FileHandler.close()` 和 `dictConfig`、`fileConfig`、`basicConfig(force=True)`、`shutdown`。事件只观察后台线程对 logging 模块锁的请求，以确定竞态顺序；不替换关闭实现、不依赖睡眠制造时序。子进程自带限时转储/退出保护，避免死锁污染 pytest 进程。

未修改生产代码的对照：四个场景全部失败，其中 `dictConfig`、`fileConfig`、`shutdown` 触发锁反转，`basicConfig` 暴露同名替代 handler 注册丢失。修复后四项全部通过，并检查重配置前后日志落盘、新 handler 注册及文件关闭。

第一轮定向验证：develop 35 项通过；0.2.9 31 项通过。范围包括原有后台队列的容量、上下文、慢 sink、有界停止、共享 handler、关闭失败可见性及运行日志过滤；develop 额外覆盖其已有企业日志冷加载测试。

最终代码在更新后的目标基线 `6d49216531f6e7a8f2f3c334314ec00aab99ddcf` 上再次通过 31 项定向回归；Gateway 依赖按新的 uv.lock 安装为 `e0b507b99286a7705f232d9ff6ac8b96c360fabd`。CPython 3.14.6 的四个真实标准库重配置子进程场景全部通过。本分支默认 foundation 日志 YAML 冷启动 21.156 秒达到可对话状态；强杀执行中的 Worker 后 PID 29284 → 44060，11.562 秒恢复。3 轮真实 Qwen3.8-Flash 输出与原始 Web 协议独立核对，原会话上下文及客户端重连均通过；在途请求返回 WORKER_CLOSED。请求结束、路由清空、正常 Worker 关闭、进程/监听退出及 owner/startup 锁释放均通过。实际源码摘要与最终实现一致，依赖逐个对齐更新后的 uv.lock。原始基线、JUnit、线程转储和真实服务证据保存在本次工作区 `artifacts/worker-logging-reconfigure-20261119/`，运行数据与凭证不进入提交。

## 防错模式与资料同步

后台关闭 handler 同样参与 logging 的全局锁顺序；不能仅检查 `emit()` 是否离开业务线程。重配置后的命名注册应按对象身份保留，不能让旧对象注销新对象。已同步中英文日志说明，并以真实标准库调用回归维护该约束。

## 遗留与后续

GLM 模型验收尝试在默认日志配置下已就绪，但遇到服务端 HTTP 429，最终真实对话使用现有 Qwen3.8-Flash 配置通过。Windows、Python 3.11.15 的真实服务和 Python 3.14.6 的标准库回归均已验证；Linux/macOS、其他 Python 版本和安装包验收仍由对应 CI/部署完成。
