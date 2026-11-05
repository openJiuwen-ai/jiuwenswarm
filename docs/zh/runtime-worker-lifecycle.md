# Runtime Worker 停止、升级与故障恢复

桌面与普通服务使用 resident 模式，让 Worker 在 Front 重启后复用已加载的 Runtime。最终退出的责任属于启动它的应用或服务管理者。HarmonyOS 默认使用 `front_loopback`，其 Worker 随 Front 断开退出，不参与常驻端点选举或重连；两种模式的最终关闭都要核验进程实际退出。

| 操作 | 行为 |
| --- | --- |
| resident 模式普通 Front 重启或 Supervisor `stop()` | 断开连接，保留 Worker |
| HarmonyOS 默认 `front_loopback` 的 Supervisor `stop()` | 关闭并回收当前 Front 启动的 Worker |
| 应用正式退出或升级 | 先停止 Front，再显式关闭当前部署的 Worker，包括重新连接的 Worker |
| macOS 关闭窗口 | 遵循桌面隐藏到后台的既有行为；正式 Quit 才执行最终清理 |
| 进程被系统强杀、断电或解释器崩溃 | 无法保证运行清理；下次启动执行身份检查与兼容处理 |

workspace 准备期间正式 Quit 会取消后续服务启动。完整桌面模式在 AgentServer 尚未启动时也会检查并关闭当前部署已有的常驻 Worker；frontend-only 模式继续保留其外部后端。

IPC 端点按安装位置、Python 环境、数据目录和运行配置隔离。Worker HELLO 还携带代码/构建身份、实际安装依赖身份及进程创建时间；版本、依赖或构建改变后不复用旧代码进程。WHL 和原生桌面每次后端构建获得独立构建 ID，即使输入源码相同也会更换 ID。WHL 在构建时写入成品身份，启动读取该身份并检查实际依赖；源码 checkout 继续校验内容。

共享数据目录中的不同服务实例继续通过 `JIUWENSWARM_RUNTIME_INSTANCE`（优先）或 `HOSTNAME` 区分端点和部署身份。停止命令须使用对应实例的相同环境；不得将其他实例的历史 Worker 当成当前实例的进程回收。

最终关闭命令在 AgentServer 尚未执行启动迁移时也检查历史端点，但只自动关闭可验证归属的 Worker；缺少部署身份的旧 Python Worker 仍需显式指定 PID。

历史端点查找覆盖长 workspace 路径曾使用的 POSIX 临时目录位置，迁移共用本次启动选举的截止时间。失败 Worker 的清理被拒绝或无法确认实际退出时，恢复返回失败，保留 FAILED 状态及已拥有的进程句柄供重试，不启动替代进程。

桌面进程树清理遇到无法检查的分支时，仍尝试清理已确认的服务分支，保留 Worker 及归属未明的分支，并报告清理不完整。macOS 更新助手在 READY 后退出时不得删除应用仍可能写入的关闭凭据；助手确认应用退出后才清理本次尝试文件。

关闭成功需要同时确认完整关闭应答和实际 PID 退出。强制结束、负应答或缺少应答会报告失败；升级安装器还检查本次退出产生的结果文件，清理失败时停止替换后端。旧协议没有应答能力时，以验证后的进程退出作为兼容条件。

`jiuwenswarm-stop` 管理 `jiuwenswarm-start debug` 的后台状态。关闭失败保留状态，可重试；没有 debug 状态时仍按既有约定返回无操作。

WHL 的 `jiuwenswarm-start all/app/dev`、命名实例及独立 `jiuwenswarm-app` 在最终退出时先停止 Front，再关闭 Worker；`--stop <name>` 和完整实例 `--restart <name>` 也执行最终清理。命名实例在启动锁内按目标 workspace 的 bootstrap 和运行配置定位 Worker，确认退出后才删除 PID 记录，失败保留记录以便重试。Windows 服务进程树清理保留 Worker 分支，由后续 IPC 操作完成持久化和退出。仅运行 web 的启动器不接管外部后端。

Python 桌面的正式退出即使发生在首个子服务注册之前，也会清理已有 Worker 和可验证的历史端点。最终关闭发现尚未建立 IPC 的 Worker 启动进程时，会在同一截止时间内等待 HELLO，再验证归属；进程命令行只能用于等待，不能代替部署归属校验。

直接管理 `jiuwenswarm-agentserver` 的服务管理器仍需区分 Front 重启与应用最终停止；最终停止应先关闭 Front，再从相同安装环境执行：

```bash
python -m jiuwenswarm.server.supervisor.shutdown --shutdown-worker --timeout 30
```

Windows 同一控制台中的 Ctrl+C 也可能直接送达 Worker。Worker 会将中断转换成一次任务取消，
等待 Runtime、后台任务和在途 IPC I/O 清理后再退出；连续按 Ctrl+C 不会打断已经开始的清理。
启动器可用 130 表示用户中断，Worker 的完整关闭仍应无清理异常并确认实际退出。
这不改变普通 Front 断开时保留 resident Worker 的行为，也不能保证系统强杀时运行清理。

首次 Runtime 准备若超过默认 30 秒关闭预算，Worker 会报告清理不完整并以非零状态退出；进程无残留本身不代表优雅清理成功。

历史 Python Worker 没有部署身份时，不能仅凭共同解释器和数据目录推断安装来源。新部署会隔离这个旧端点并继续启动，日志给出旧 PID。操作者核对该 PID 的解释器、完整启动参数、安装来源及数据目录后，可显式关闭：

```bash
jiuwenswarm-stop --legacy-worker-pid 12345 --timeout 30
```

显式选择仍检查用户、解释器、Worker 模块、IPC 参数和进程创建时间；不允许关闭声明属于其他部署的进程。超时或进程已更换时返回失败。该参数只关闭所选历史 Worker；仍在运行的其他 Front 可能重新启动它，应先停止对应 Front。

Worker 启动或 IPC 连接的重试额度耗尽后，服务先发布 `FAILED`，随后在 30 秒冷却后自动尝试恢复。连续失败时冷却递增至 60、120、240、300 秒，上限 300 秒；成功后结束本轮冷却。停止服务会取消后台恢复。配置、协议、身份或 Runtime 加载的明确永久性错误仍需修正后显式 reload，不对它们无限快速重试。自动恢复不补发已失败的用户请求，也不保证操作系统内存压力的根因已消除。
