---
id: D-20261102-1502-harmony-runtime-ipc
title: HarmonyOS AgentServer Front 与 Worker 进程间通信适配方案
slug: harmony-runtime-ipc
status: in-development
stage: active
author: douran
author_email: douran1@huawei.com
reviewers: []
created: 2026-11-02
updated: 2026-11-02
domain: core
modules:
  - jiuwenswarm/server/ipc
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/server/worker
  - jiuwenswarm_harmony_pc/entry/src/main/resources/rawfile
  - jiuwenswarm_harmony_pc/hnp
tags: [agentserver, harmonyos, ipc, worker]
scope: AgentServer Front/Worker IPC 及 HarmonyOS 打包适配；不涉及 Gateway、DeepAgent 和业务协议重构
related: []
type: D
files: []
---

# HarmonyOS AgentServer Front 与 Worker 进程间通信适配方案

## 1. 设计定位

本文解决 AgentServer 采用 Front/Worker 双进程后，在 HarmonyOS 应用沙箱中 Worker 无法可靠绑定 Unix domain socket 的问题。

方案只调整 Front 与 Worker 之间 IPC 连接的建立方式，保留现有 IPC 业务消息、Worker 运行时、Gateway 接入及 Agent 业务行为。本次修订替代本文最初的 `socketpair + pass_fds` 决策：HarmonyOS 主方案改为 Front 监听 loopback 临时端口、Worker 主动连接。原方案中 HarmonyOS 使用 attached Worker、普通桌面平台保持现状等决定继续有效。

## 2. 背景、目标与范围

### 2.1 当前实现

当前 JiuwenSwarm `develop` 中：

- AgentServer Front 负责 Gateway 连接、请求准入和 Worker 监管；
- Runtime Worker 持有 Agent Runtime；
- 非 Windows 平台由 Worker 绑定 Unix domain socket，Front 连接该端点；
- Windows 使用 Named Pipe；
- 固定端点允许 Front 重启后重新连接常驻 Worker。

对应代码主要位于 `jiuwenswarm/server/ipc/`、`jiuwenswarm/server/supervisor/`、`jiuwenswarm/server/worker/`。

### 2.2 HarmonyOS 实际启动链路

```text
ArkTS PythonService
        │ NAPI
        ▼
C++ fork/exec
        ▼
harmony_entry.py
        ├── AgentServer 进程
        ├── Gateway 进程
        └── Frontend 进程
```

AgentServer 进程内部才启动 Runtime Worker。当前鸿蒙外壳将 AgentServer 视为关键子进程；AgentServer 或 App 退出后，orchestrator 和 C++ 启动器会清理整组服务及其后代进程。因此，桌面版 Worker 跨 Front 重启常驻的能力在当前 HarmonyOS 产品生命周期内没有实际收益，HarmonyOS 可以采用 attached Worker。

### 2.3 版本现状

| 仓库或基线 | 提交 | 说明 |
| --- | --- | --- |
| `jiuwenswarm_harmony_pc/master` | `8834b56bafa9976bffc243e1204edc18e1812e9e` | 当前鸿蒙打包仓库 HEAD |
| 鸿蒙仓 JiuwenSwarm 基线 | `v0.2.7` / `2aa7fea7e55fbe1edaae0d468ef666b088c76fa9` | 早于 Front/Worker 常驻架构 |
| `jiuwenswarm/develop` | `d9550678741452eda07cc4c9e464a4541963f6f1` | 本方案调查时最新 develop |

### 2.4 目标

| 场景 | 当前行为 | 目标行为 |
| --- | --- | --- |
| HarmonyOS 启动 Worker | Worker 尝试绑定 Unix socket | Worker 不 bind、listen 或创建 socket 文件 |
| Front 与 Worker 通信 | Front 连接 Worker listener | Front 监听 loopback 临时端口，Worker 主动连接 |
| Worker 崩溃 | Supervisor 重启并重新连接 | 使用新的端口、token 和 generation 重启 Worker |
| Front 退出 | Worker等待新 Front | Worker收到 EOF 后清理并退出 |
| Linux/macOS/Windows | Unix socket 或 Named Pipe | 行为不变 |

### 2.5 范围与非目标

本次包含：

- 增加 Front-owned loopback IPC 模式；
- Front 监听 `127.0.0.1` 随机临时端口；
- Worker 主动连接 Front，复用现有 `IpcConnection`；
- 增加一次性 transport 认证和 generation 校验；
- HarmonyOS 默认启用该模式并重建 HNP、Python 覆盖层；
- 验证正常启动、Worker 重启、Front 退出和资源释放。

本次不包含：

- Gateway、DeepAgent、Tool、Memory、Model、Rail 等链路修改；
- 既有 IPC 业务消息 Schema 或最大帧大小调整；
- HarmonyOS IPC Kit 或 Front/Worker 单进程改造；
- ArkTS、NAPI、C++ 启动器和权限清单修改；
- Linux/macOS/Windows resident Worker 语义调整；
- Linux/UOS/openEuler 的重连、跨实例 session owner 或 fencing 修复。

### 2.6 与 Linux/UOS/openEuler 现象的边界

`runtime worker closed` 是 Supervisor 对 IPC 会话结束、Worker 退出、心跳超时等多类失败的统一结果；`another live AgentServer owns the session runtime` 是持久化 session owner 仍指向活进程时的所有权冲突。两者不是同一个直接错误，但在“Front 失联后旧 Worker 仍存活、新 Front 又启动第二个 Worker”时可以构成同一故障链。

当前 owner 实际记录 Runtime Worker PID，错误文案中的 `AgentServer` 已不准确。仅以本机 `pid + create_time` 判断所有者，在共享数据目录、容器、跨主机或 PID namespace 场景也不足以形成可靠的分布式 fencing。这是普通 Linux resident Worker 架构的独立问题，不随本次 HarmonyOS transport 适配修改。

本设计只通过 HarmonyOS attached Worker 避免在鸿蒙引入该风险，不宣称解决现有 Linux/UOS/openEuler 现场问题。

## 3. 约束、假设与待决问题

### 3.1 已确认约束

- 现有 Hello、readiness、heartbeat、request、wire 和 shutdown 协议继续复用。
- 当前 IPC 单帧上限保持 32 MB。
- 平台选择复用已有鸿蒙环境识别，显式 IPC 配置可以覆盖默认值；不能用“非 Windows 等于 Unix socket”判断 HarmonyOS。
- 鸿蒙打包代码已使用 `subprocess.Popen` 启动服务，并通过 `127.0.0.1` 检查 AgentServer、Gateway 和 Frontend；loopback TCP 是当前启动链已依赖的能力。
- HarmonyOS Python 不保证支持 `socketpair()` 或 `Popen(pass_fds=...)`，方案不能以二者可用为前提。
- HarmonyOS 沙箱对匿名 `AF_UNIX` 能力缺少可靠保证，方案不使用 `AF_UNIX`。

### 3.2 关键假设

- AgentServer Front 可以在 `127.0.0.1` 监听随机临时端口；
- Worker 可以连接同一应用沙箱内该端口；
- 当前 `Popen(stdin=PIPE)` 和 stdin 启动身份载荷在鸿蒙可用。

以上假设需真机验证，但 `socketpair`、`pass_fds`、`AF_UNIX` 不再是实施前置条件。

### 3.3 待决问题

- 配置值暂定 `JIUWENSWARM_RUNTIME_IPC=front_loopback`，名称需评审确认；
- 鸿蒙仓同步的 JiuwenSwarm 目标提交待主仓实现合入后确定。

## 4. 总体方案与取舍

### 4.1 方案概览

Front 在启动 Worker 前监听 `127.0.0.1:0`，生成本次 Worker generation 的一次性随机 token，把 transport、host、port、token、generation 合入现有 stdin 启动身份载荷。Worker 启动后主动连接 Front，完成 transport 认证后进入现有 Hello 和消息循环。

```text
AgentServer Front
   ├── listen 127.0.0.1:0
   ├── 生成 token + generation
   ├── stdin 传递连接参数
   └── accept + transport auth
                 ▲
                 │ loopback TCP
                 │
Runtime Worker
   ├── connect
   ├── transport auth
   ├── 现有 IPC protocol/dispatcher/runtime
   └── Front EOF 后清理退出
```

### 4.2 关键决策

#### D-01 Front-owned loopback listener

- 依据：Worker 不 bind，不依赖 Unix socket 路径、`AF_UNIX`、`socketpair` 或 `pass_fds`；鸿蒙现有启动链已依赖 `127.0.0.1`。
- 代价：Front 需要维护临时 listener、认证和接受超时；Worker 不支持跨 Front 常驻。

#### D-02 鸿蒙默认启用，保留显式覆盖

- 未设置 IPC mode 时，已识别的鸿蒙运行环境默认使用 `front_loopback`；识别沿用 runtime 标志、`ohos` 平台标识或 HNP PATH 规则；
- 普通 POSIX/Windows 未设置时保持当前行为；
- 显式 `JIUWENSWARM_RUNTIME_IPC=front_loopback` 或 `resident` 优先于默认值；未知值失败，不自动降级。

#### D-03 HarmonyOS 使用 attached Worker

- loopback IPC 断开后 Worker 不等待第二个 Supervisor，执行 Runtime 清理并退出；
- Front 重启时重新加载 Worker Runtime。

#### D-04 一次性认证

- 每次 spawn 生成新的 256-bit token，并绑定 generation、code identity 和 data scope；
- token 只经 stdin 传递，不进入命令行和日志；
- Worker 首帧完成 transport auth，Front 只接受一次合法连接，随后关闭 listener；
- 该认证帧只影响建连，既有业务消息 Schema 不变。

### 4.3 备选方案

| 方案 | 结论 |
| --- | --- |
| 更换 Unix socket 路径 | 不采用，不能解决 bind 权限或 namespace 限制 |
| Worker 绑定 loopback TCP | 不采用，仍要求 Worker bind |
| Front 绑定 loopback、Worker connect | 采用 |
| `socketpair + pass_fds` | 不采用，平台能力无可靠保证 |
| HarmonyOS IPC Kit | 暂缓，Native 适配和协议代价较大 |
| Front/Worker 合并 | 不采用，破坏现有进程隔离 |

首版不做运行时自动降级。`front_loopback` 失败时明确输出 transport、阶段和 errno，不静默切换 Unix socket。

## 5. 详细设计

### 5.1 Front/Supervisor

1. 读取 IPC mode；未设置时，鸿蒙默认 loopback，普通桌面保持当前行为。
2. `front_loopback` 下通过 `asyncio.start_server` 监听 `127.0.0.1:0`。
3. 生成新 token 和 generation。
4. 将连接参数合入 `launch_identity_payload`，通过 Worker stdin 发送。
5. 接受连接并校验 transport auth。
6. 无效连接立即关闭，但在总超时前继续接受后续连接。
7. 首个合法连接通过后关闭 listener，保留 `StreamConnection`。
8. 跳过 `try_connect()`、`endpoint_is_listening()`、`connect_with_retry()`、`.startup.lock` 和 resident reattach。
9. Worker 异常退出时使用新端口、token、generation 重试。

监听地址固定为 IPv4 `127.0.0.1`，不得读取外部 host 配置或监听 `0.0.0.0`。认证完成后继续执行现有 Hello、identity、scope、generation、readiness 和 watchdog 校验。

### 5.2 Worker

建议抽象连接源：

```python
class IpcSessionSource:
    reconnectable: bool
    async def start(self) -> None: ...
    async def accept(self) -> IpcConnection: ...
    async def close(self) -> None: ...
```

| 实现 | 场景 | reconnectable |
| --- | --- | --- |
| `IpcServer` | Unix socket / Named Pipe | `True` |
| `FrontConnectSessionSource` | HarmonyOS loopback | `False` |

`front_loopback` 下 Worker：

- 从 stdin 载荷取得连接参数，只执行 connect；
- 不创建 socket 路径，不获取 owner lease；
- 先发送 transport auth，再发送现有 Hello；
- 参数无效或建连超时时在加载 Runtime 前失败；
- EOF 后停止 Worker，按现有顺序 drain dispatcher、关闭 Runtime 和连接。

### 5.3 HarmonyOS 打包仓

使用本分支更新代码后，按原有方式启动 AgentServer 即可。原有 runtime 标志、`ohos` 平台标识或 HNP PATH 能识别鸿蒙，不需要新增 IPC 环境变量。自定义启动器没有这些识别信息时，可显式标注：

```python
os.environ["JIUWEN_HARMONY_RUNTIME"] = "1"
```

打包仓需要：

1. 升级到包含本设计实现的 JiuwenSwarm 提交；
2. 重建 `python-jiuwenswarm.hnp`；
3. 重新生成 `pyc_bundle.tar`，避免旧 `.py_src` 覆盖新代码；
4. 重新构建、注入、签名 HAP；
5. 更新业务代码基线说明。

无需修改 ArkTS 页面、`PythonService.ets`、NAPI、`shell_exec.cpp`、权限清单、Gateway 和 Frontend 启动方式。

### 5.4 资源与关闭顺序

| 资源 | 所有者 | 释放时机 |
| --- | --- | --- |
| loopback listener | Front | 合法 Worker 建连或建连失败 |
| 一次性 token | Front/Worker 短期持有 | auth 完成或失败后失效 |
| IPC stream | Front/Worker | shutdown、会话结束或重启前 |
| Worker process | RuntimeSupervisor | graceful shutdown 或强制终止 |
| Runtime | Worker | dispatcher drain 后关闭 |

关闭顺序：停止新请求 → `KIND_SHUTDOWN` → drain dispatcher → 关闭 Runtime → `KIND_SHUTDOWN_DONE` → 关闭 stream → 回收 Worker。Front 异常退出时 Worker 从 EOF 感知失联并有界清理，C++ 进程组清理作为最终兜底。

### 5.5 异常恢复

- Front bind 失败：明确失败，不自动回退；
- spawn 失败：关闭 listener，使 token 失效；
- 非法认证：关闭该连接并继续等待合法 Worker；
- Worker 在 auth/Hello 前退出：清理 listener、连接和子进程后重试；
- 运行中断开：失败当前请求，回收 Worker，以全新建连信息重启；
- Front 异常退出：Worker 收到 EOF 后退出；
- 无效启动载荷：Worker 在 Runtime 加载前失败。

## 6. 兼容、迁移与回滚

- Linux/macOS 默认继续使用 Unix socket 和 resident Worker；
- Windows 默认继续使用 Named Pipe；
- 鸿蒙默认启用新路径，普通桌面仅显式 `front_loopback` 启用；
- 既有业务消息、protocol version 和 Gateway 协议不变。

交付顺序为：主仓实现和测试 → 鸿蒙仓更新基线并核对环境识别 → 重建 HNP/覆盖层 → 真机验证。不能先设置新 transport 再等待主仓补实现。

回滚时主仓删除 loopback transport；鸿蒙仓必须成套恢复业务基线、HNP 和 `pyc_bundle.tar`，不能混用版本。

## 7. 开发拆分与工作量

### 7.1 主仓 PR

- IPC mode、loopback connection/source；
- Front listener、一次性认证和接受超时；
- stdin 启动载荷扩展；
- Worker connect、transport auth、attached 生命周期；
- 单元和子进程测试。

预计 3～4 人日。

### 7.2 HarmonyOS 打包仓 PR

- 核对 Harmony runtime 自动识别与 IPC mode 显式覆盖；
- 更新业务基线，重建 HNP 和覆盖层；
- HAP 注入、签名和真机验证。

预计 1～2 人日，总体 4～6 人日。

## 8. 验证与验收

### 8.1 真机探针

```text
Front bind 127.0.0.1:0
→ Popen(stdin=PIPE)
→ stdin 传递 host、port、token、generation
→ Worker connect + auth
→ 双向消息
→ Front 关闭连接
→ Worker 观察 EOF并退出
```

还需验证非法 token 被拒绝后，合法 Worker仍可在总超时内连接。失败时先定位 loopback 权限、Python asyncio 或打包环境问题，不回退到 Unix socket、socketpair 或 IPC Kit。

### 8.2 主仓验证

| ID | 场景 | 通过条件 |
| --- | --- | --- |
| HIPC-V-01 | `front_loopback` 启动 | 收到 auth、Hello、readiness，无 socket 文件 |
| HIPC-V-02 | 请求与 server push | 顺序、generation、完成消息不变 |
| HIPC-V-03 | Worker 崩溃 | 使用新端口、token、generation 恢复 |
| HIPC-V-04 | Front 断开 | Worker drain 后退出，不等待第二连接 |
| HIPC-V-05 | graceful shutdown | 收到 ACK，无残留 Worker |
| HIPC-V-06 | spawn/auth/Hello 失败 | listener、连接和 Worker 全部回收 |
| HIPC-V-07 | 非法 token 抢连 | 非法连接被拒绝，合法 Worker成功连接 |
| HIPC-V-08 | 默认平台回归 | Unix socket 和 Named Pipe 保持通过 |

### 8.3 HarmonyOS 真机验收

- 连续冷启动至少 20 次，无 Unix socket bind 失败；
- 正常退出、强制结束、再次启动无残留 Worker 和端口占用；
- Worker 崩溃后能够恢复并接受后续请求；
- 日志包含 `runtime_ipc=front_loopback`、建连和认证阶段，但不输出 token；
- 不产生 Runtime Unix socket 文件；
- 请求、流式返回、取消和 shutdown 正常；
- HNP 与 `.py_src` 中实现版本一致。

## 9. 风险与限制

| 风险 | 应对 |
| --- | --- |
| 真机 loopback 或 asyncio 行为不同 | 开发前探针，保留阶段化诊断，不静默回退 |
| listener/stream 关闭次序错误 | 明确所有权，覆盖失败注入和资源回收测试 |
| token 泄露或未绑定 generation | 仅经 stdin 传递、不写日志、auth 后失效 |
| HNP 与覆盖层版本不一致 | freshness 和包内版本检查 |
| 误用于普通桌面部署 | 默认仅在已识别的鸿蒙环境启用；桌面仍需显式配置 |

HarmonyOS `front_loopback` 不支持 Worker 跨 Front 常驻，符合当前 orchestrator 生命周期。未来若允许 Front 独立热重启，需要重新评估可重连 listener、IPC Kit 或 Worker lease/fencing。

## 10. 按日期记录的评审与设计修订

- 2026-11-02：因 HarmonyOS Python 不保证支持 `socketpair`、`pass_fds`，且匿名 `AF_UNIX` 缺少可靠保证，替代原 inherited socketpair 决策。主方案改为 Front 监听 `127.0.0.1:0`、Worker 主动连接并一次性认证；IPC 上层协议复用、HarmonyOS attached Worker、普通桌面平台行为不变等决定继续有效。
- 尚未进入正式评审。
- 2026-11-02：用户明确要求默认打开该设置；调整 D-02 为鸿蒙环境默认 `front_loopback`，保留普通桌面默认行为与显式覆盖。此项替代原先“鸿蒙也必须额外设置 IPC mode”的要求，不改变 transport 失败不自动降级的决定。
- 2026-11-02：用户确认将方案分别提交主仓 develop 和 0.2.8，并明确 Windows 仍采用原实现、仅鸿蒙默认采用新 transport。主仓交付以 D-02 的平台选择为准；先前鸿蒙专用测试分支的无条件默认值不进入主仓。

## 11. 参考与交接

- HarmonyOS 打包仓：[jiuwenswarm_harmony_pc](https://gitcode.com/yangzequ/jiuwenswarm_harmony_pc)
- JiuwenSwarm 调查基线：`develop@d9550678741452eda07cc4c9e464a4541963f6f1`
- HarmonyOS 仓调查基线：`master@8834b56bafa9976bffc243e1204edc18e1812e9e`
- 设计确认后先实施主仓 PR，再同步鸿蒙仓；不得实现已被替代的 socketpair/pass_fds 路径。
- 开发阶段实际代码、验证证据和偏差写入对应 feature；当前尚无实施归档。
