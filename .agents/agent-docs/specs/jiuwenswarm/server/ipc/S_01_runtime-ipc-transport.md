---
id: S_01_runtime-ipc-transport
title: Runtime IPC transport 与 Worker 生命周期规约
type: S
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/ipc
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/server/worker
files:
  - jiuwenswarm/server/ipc/session.py
  - jiuwenswarm/server/ipc/loopback.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/server/supervisor/identity.py
  - jiuwenswarm/server/supervisor/worker_process.py
  - jiuwenswarm/server/worker/main.py
  - jiuwenswarm/server/worker/service.py
  - jiuwenswarm/common/runtime_platform.py
---

# Runtime IPC transport 与 Worker 生命周期规约

## 范围与选择

Front/Supervisor 负责 Worker 准入与生命周期；Worker 负责 Runtime 和请求执行。
`JIUWENSWARM_RUNTIME_IPC` 未设置、空值或纯空格时，鸿蒙运行环境默认使用 `front_loopback`，普通 POSIX/Windows 继续使用 Unix socket/Named Pipe。
鸿蒙识别复用 `common.runtime_platform.is_harmony_runtime()`：运行标志 `JIUWEN_HARMONY_RUNTIME=1`、`sys.platform == "ohos"`，或 PATH 中同时包含 `/data/app/` 和 `/hnp/` 的目录；不将普通 Linux 推断为鸿蒙。
显式 `front_loopback` 或 `resident` 优先于平台默认值；未知配置立即失败。Front 和 Worker 使用同一识别函数和继承的环境选择模式。
不探测失败后自动回退，也不修改 Gateway、Runtime 业务协议或 Linux resident owner/fencing。

依据：[D-20261102-1502-harmony-runtime-ipc](../../../../designs/active/D-20261102-1502-harmony-runtime-ipc/design.md)。
实施与证据：[F_20261102_1630_harmony-runtime-ipc](../../../../features/F_20261102_1630_harmony-runtime-ipc.md)。

## 建连契约与身份

1. Front 生成本次启动的随机正整数 generation；验证代码身份后，通过 `asyncio.start_server` 监听 IPv4 `127.0.0.1:0`。
2. 每次启动生成独立的 256-bit token；地址、端口、token、generation、code identity、data scope 和超时仅加入现有 stdin 启动载荷。token 不进入 argv、环境变量或日志。
3. Worker 保留原来的父进程、部署目录、数据范围、build label、源码身份校验，再校验 transport 参数。host 必须为字面量 `127.0.0.1`，port 为有效整数，generation 为正整数，超时为有限正数。
4. Worker 只执行 connect，不调用 resident endpoint 探测、不创建 socket 文件、不获取 `.startup.lock` 或 `.owner.lock`。
5. Worker 首帧为 `transport.auth`，包含 transport、token、generation、code identity 和 data scope。Front 使用常量时间 token 比较并逐项验证身份。
6. 认证帧单独限制为 4096 bytes；每个候选最多等待 2 秒，最多并发认证 16 个候选。非法/超时连接关闭，仍可在本次总建连截止时间内接受合法 Worker。
7. 第一个合法连接独占本次凭证并关闭 listener；Front 回复 `transport.ready`。Worker 收到确认后才交付连接、发送既有 Hello 并开始 Runtime 加载。
8. Hello 仍校验 protocol、code identity、data scope 和执行 profile；attached 模式额外要求 Hello generation 等于本次认证 generation。

transport 帧仅用于建连，不暴露给现有 dispatcher。既有 protocol version、请求/完成/推送/心跳/取消/shutdown Schema 和 32 MB 业务帧上限保持不变。

## 所有权与关闭

`IpcSessionSource` 抽象 `start/accept/close`、`reconnectable` 和可选 generation。
`IpcServer` 可重连，继续使用 Worker 端 listener 与 resident lease。
`FrontConnectSessionSource` 不可重连，只交付一次认证连接；连接所有权交给 service 后由 service 关闭。

| 场景 | 必须呈现的行为 |
| --- | --- |
| 正常 attached 停止 | 停止准入，发送 shutdown，drain dispatcher，关闭 Runtime，发送 shutdown.done，关闭 stream，观察 Worker 真正退出 |
| Front EOF | attached Worker drain dispatcher、关闭 Runtime 并退出，不等待第二个 Front；resident Worker 仍保留 Runtime |
| Worker 崩溃或连接失效 | 当前请求失败；关闭旧 stream、回收旧进程；按现有重试预算重新分配 listener、token、generation |
| bind/spawn/auth/Hello 失败或启动取消 | listener、认证任务、未交付/已交付连接和已启动子进程都必须回收 |
| attached `stop()` | 即使调用方未传 `terminate_worker=True`，也执行最终关闭与进程回收 |
| resident `stop()` | 保留原有默认 detach 与显式最终退出语义 |

listener 关闭不等待已经交给 Supervisor 的 stream 生命周期；Python 3.12+ 的 `Server.wait_closed()` 会等待活跃连接，不能在建连成功时据此阻塞 Runtime 启动。
阶段日志包含 `runtime_ipc=front_loopback`、listen/auth/connect/hello 等阶段；系统调用失败保留 errno，不打印认证内容。

## 适用限制与验证入口

本仓实现可在普通 Windows/POSIX 显式启用，在已识别的鸿蒙环境默认启用。HarmonyOS loopback、asyncio 和 stdin 能力仍需真机验证。
鸿蒙打包仓须同时更新业务基线、HNP 和 Python 覆盖层；无需新增 IPC 环境变量。本仓的 transport 实现不表示 HAP 已构建、签名或验收。
attached 模式不支持 Worker 跨 Front 重启常驻。

行为测试入口为 `tests/unit_tests/agentserver/test_front_loopback_ipc.py`；原平台回归入口包括 `test_runtime_worker_phase2.py`、`test_worker_residency.py`、`test_resident_worker_shutdown.py`、`test_worker_shutdown_deadline.py` 和 `test_code_identity_cache.py`。
