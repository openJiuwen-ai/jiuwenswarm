---
id: S_01_worker-lifecycle
title: Runtime Worker 生命周期与恢复规约
type: S
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/server/worker
  - jiuwenswarm/channels/desktop
files:
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/server/supervisor/resident_lifecycle.py
  - jiuwenswarm/server/supervisor/shutdown.py
---

# Runtime Worker 生命周期与恢复规约

## 范围与归属

Supervisor 负责连接、验证、故障分类及恢复；Worker 负责 Runtime 和关闭应答；桌面或外部服务管理者负责最终退出。普通 Front 重启保留 Worker，最终应用退出和升级显式关闭 Worker。

## 身份与端点

1. 默认端点由安装位置、解释器环境、数据目录和运行配置共同决定；显式 `JIUWENSWARM_RUNTIME_SOCKET` 保留调用方隔离责任。
2. HELLO 通过协议、数据/部署/代码身份和进程创建时间校验后才可分发请求或行使退出权限。代码身份包含安装依赖观察；WHL 与原生后端构建 ID 区分同源码重新构建。受控成品在构建时校验内容，运行时读取成品身份并观察实际依赖，源码运行继续校验内容。
3. 历史端点未知归属不阻断新部署的独立端点启动，也不自动获得清理权。Python 无部署身份的历史进程必须由操作者显式指定 PID，继续经过 OS 用户、解释器、模块和 socket 参数验证；不同已声明部署不可被接管。
4. 连接、关闭、替换采用端点启动锁；进程生命周期锁释放与 PID 退出均须可观察。PID 创建时间变化时禁止强杀。

## 最终关闭与升级

1. 先停止 Front，避免关闭期间重新产生 Worker，再请求 Worker 取消调度、等待加载、关闭 Runtime 与 IPC。
2. 新 Worker 通过 `shutdown_ack` 明示应答能力，成功应答仅表示关闭步骤完成，调用方还必须观察实际 PID 退出。旧协议无应答能力时兼容已验证的原生进程退出。
3. 各关闭阶段共用一个期限并为强制退出观察预留时间。收到负应答、缺少所需应答或只能强杀时，不得报告完整持久化成功。
4. 升级须持有本次退出的成功结果文件；不存在、pending 或 failed 均禁止替换后端。关闭失败保留 debug 状态，以供重试。
5. 最终退出可以关闭通过验证后重连的 Worker；不以当前 Supervisor 是否亲自创建进程决定清理权限。

## 重试与恢复

1. 启动/IPC 的有限重试耗尽先发布 FAILED 与错误，随后使用唯一后台恢复任务，按 30 到 300 秒指数冷却重试；`max_restarts=0` 显式禁用此恢复。
2. 启动成功结束本轮恢复。永久配置/身份/协议/Runtime 加载错误不自动重试；修正后可显式 reload。
3. stop 开始后取消后台恢复，不允许新建 Worker。恢复不重放已失败请求。
4. Worker 日志的有界后台队列避免同步日志 sink 阻塞业务循环；队列满丢弃并计数，不能退回同步写入。关闭日志也有期限。

## 限制及验证入口

系统强杀和硬件故障不能保证优雅关闭；遗留无身份、IPC 不可达的进程需要人工核实处置。内存压力的物理原因与恢复状态机是不同诊断范围。

行为入口见 `test_worker_automatic_recovery.py`、`test_worker_legacy_compatibility.py`、`test_worker_shutdown_deadline.py`、`test_resident_worker_shutdown.py`、`test_background_logging.py` 和 `tests/electron/update_shutdown.test.cjs`。用户操作见 `docs/zh/runtime-worker-lifecycle.md` 及英文版本。
