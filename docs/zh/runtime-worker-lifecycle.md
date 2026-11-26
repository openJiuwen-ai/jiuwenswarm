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

HarmonyOS 沙箱拒绝读取全局 `/proc/stat` 时，Worker 和 Supervisor 可使用 `/proc/<pid>/stat` 的进程启动计数继续校验身份；它与创建时间戳分开传输。两种来源都不可读时，HELLO 失败日志包含 PID、阶段和异常栈，启动失败；后续清理也不会仅凭 PID 终止未经身份校验的目标。

共享数据目录中的不同服务实例继续通过 `JIUWENSWARM_RUNTIME_INSTANCE`（优先）或 `HOSTNAME` 区分端点和部署身份。停止命令须使用对应实例的相同环境；不得将其他实例的历史 Worker 当成当前实例的进程回收。

最终关闭命令在 AgentServer 尚未执行启动迁移时也检查历史端点，但只自动关闭可验证归属的 Worker；缺少部署身份的旧 Python Worker 仍需显式指定 PID。

历史端点查找覆盖长 workspace 路径曾使用的 POSIX 临时目录位置，迁移共用本次启动选举的截止时间。失败 Worker 的清理被拒绝或无法确认实际退出时，恢复返回失败，保留 FAILED 状态及已拥有的进程句柄供重试，不启动替代进程。

桌面进程树清理遇到无法检查的分支时，仍尝试清理已确认的服务分支，保留 Worker 及归属未明的分支，并报告清理不完整。macOS 更新助手在 READY 后退出时不得删除应用仍可能写入的关闭凭据；助手确认应用退出后才清理本次尝试文件。

关闭成功需要同时确认完整关闭应答和实际 PID 退出。强制结束、负应答或缺少应答会报告失败；升级安装器还检查本次退出产生的结果文件，清理失败时停止替换后端。旧协议没有应答能力时，以验证后的进程退出作为兼容条件。

`jiuwenswarm-stop` 管理 `jiuwenswarm-start debug` 的后台状态。关闭失败保留状态，可重试；没有 debug 状态时仍按既有约定返回无操作。

debug 停止默认分别给 Front 15 秒、Worker 35 秒；Worker 的预算覆盖其内部 30 秒清理期限，并留出 IPC 和确认进程退出的余量。两个阶段按顺序执行，预算不是整个停止命令的总时限。
显式 `--timeout N` 仍为两个阶段各设置 N 秒；`--worker-timeout N` 只覆盖 Worker 阶段，可与 `--timeout` 同用。超时必须是正的有限数值。
`make stop`、`make restart-debug` 和 `make restart-debug-fast` 通过 `STOP_ARGS` 将这些参数传给停止命令；停止失败时不会继续启动服务。例如，只调整 Worker 的预算：

```bash
make restart-debug-fast STOP_ARGS="--worker-timeout 45"
```

WHL 的 `jiuwenswarm-start all/app/dev`、命名实例及独立 `jiuwenswarm-app` 在最终退出时先停止 Front，再关闭 Worker；`--stop <name>` 和完整实例 `--restart <name>` 也执行最终清理。命名实例在启动锁内按目标 workspace 的 bootstrap 和运行配置定位 Worker，确认退出后才删除 PID 记录，失败保留记录以便重试。Windows 服务进程树清理保留 Worker 分支，由后续 IPC 操作完成持久化和退出。仅运行 web 的启动器不接管外部后端。

Python 桌面的正式退出即使发生在首个子服务注册之前，也会清理已有 Worker 和可验证的历史端点。最终关闭发现尚未建立 IPC 的 Worker 启动进程时，会在同一截止时间内等待 HELLO，再验证归属；进程命令行只能用于等待，不能代替部署归属校验。
后台线程正在清理时，桌面主线程等待到 30 秒关闭期限，另留 5 秒回收进程和写入升级回执。等待超时会记录关闭未完成，安装器仍要求成功回执。

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
jiuwenswarm-stop --legacy-worker-pid 12345
# 核实旧 Worker 的安装来源后，也可在重启时显式选择它：
make restart-debug STOP_ARGS="--legacy-worker-pid 12345"
```

显式选择仍检查用户、解释器、Worker 模块、IPC 参数和进程创建时间；不允许关闭声明属于其他部署的进程。超时或进程已更换时返回失败。该参数只关闭所选历史 Worker；仍在运行的其他 Front 可能重新启动它，应先停止对应 Front。

Worker 启动或 IPC 连接的重试额度耗尽后，服务先发布 `FAILED`，随后在 30 秒冷却后自动尝试恢复。连续失败时冷却递增至 60、120、240、300 秒，上限 300 秒；成功后结束本轮冷却。停止服务会取消后台恢复。配置、协议、身份或 Runtime 加载的明确永久性错误仍需修正后显式 reload，不对它们无限快速重试。自动恢复不补发已失败的用户请求，也不保证操作系统内存压力的根因已消除。

resident Worker 的 IPC 暂时断开时，Front 保留已提交流式请求的等待者和已投递事件游标，最多等待 90 秒完成连接恢复及回放确认。重连必须验证相同 Worker 实例、PID 和进程创建时间，再按原 dispatch 与调用方范围回放未投递输出；不重新发送任务输入或重做工具操作。Worker 保留断连期间的流式完成通知，回放完成后才恢复该 dispatch 的实时投递。Front 按已投递游标过滤重复回放。

90 秒仅限制传输恢复，不限制正常执行时长。原 Worker 已退出、被替换或被判不健康，重试额度耗尽，原 dispatch 不再保留，未投递输出已超过有界回放窗口，或服务停止时，原等待请求仍明确失败；不把缺失输出当成成功。普通非流式 RPC 保留原有投递确认和失败语义，`front_loopback` 仍按其 Worker 生命周期处理断连。

IPC 发送和关闭同时失败时，Front 仍保留原恢复等待；恢复订阅失败后退役该连接，再续接所有保留的请求。服务停止时，即使 IPC 关闭抛错，等待者和后台任务也会清理。

## 进程身份

IPC protocol 2 的 HELLO 必须携带有效的创建时间或进程启动计数，两个字段都缺失或为空时拒绝握手。仅 protocol 1 保留省略创建字段的兼容行为，并仍须通过操作系统归属校验。时间戳与启动计数保留不同单位，bool 和非正数均不是有效身份。

debug 服务状态和新的钉钉 OAuth PID 记录保存操作系统进程身份，受限系统可使用启动计数。身份不可读时保留未知状态：debug 停止拒绝发送信号，OAuth 取消报告清理未完成，并保留 pending 状态及凭据。旧时间戳记录在身份仍可验证时继续兼容。

debug 服务身份不可验证时，显式指定的 `--legacy-worker-pid` 仍可进入 Worker 独立归属校验及清理流程；不向未知身份的 Front 发送信号，保留 debug 状态，并以非零退出码报告服务整体停止尚未确认。OAuth 超时清理在二次校验或退出等待中失败时，状态查询返回 `authorization_cleanup_failed`，保留 pending 记录及凭据，不写入 `expired` 退出标记；后续查询继续校验进程状态。

develop 还发送可选的 `process_started` 原生启动标记，用于兼容观察方之间的时钟修正差异。该字段不可读时为空，不影响有效 ticks 的握手；它不能替代 protocol 2 必须声明的创建时间或 ticks，也不放宽旧时间戳的身份匹配。双方都有可读 ticks 时仍优先使用 ticks。

## Worker 输出日志

resident 与 `front_loopback` 的 stdout 继承启动进程的输出，stderr 直接追加写入每次启动独立的 `runtime-worker-<UUID>[-<owner>].stderr.log`。可在启动应用或 Supervisor 前设置 `JIUWEN_WORKER_LOG_DIR` 指定目录；未设置或为空时，使用当前 Worker socket 路径父目录下的 `worker-logs`。自定义 socket 路径时，默认日志目录随之变化；`front_loopback` 同样按传入的 socket 路径选择默认日志目录。该设置不改变 IPC 端点位置。

建议为 `JIUWEN_WORKER_LOG_DIR` 使用绝对路径；相对路径按 Supervisor 当前工作目录解析。

Supervisor 不再逐行读取并重标 Worker 输出，也不创建独立的 stdout 日志文件。stderr 可以包含正常 INFO 日志，不能据此判定 Worker 发生错误；排障时结合文件中的原始日志级别、异常堆栈与进程退出码。

同一目录下的 `latest-worker-error.log` 保存最近一次 Worker 的错误诊断，包括 PID、异常类型、消息和 traceback。Supervisor 在启动新 Worker 前尝试删除旧诊断，并通过内部环境变量 `JIUWEN_WORKER_DIAGNOSTIC_PATH` 将路径传给子进程，通常无需人工设置该变量。诊断文件不是按 Worker 永久归档的日志。

多个 Worker 使用同一日志目录时，会共享这个固定诊断文件：新 Worker 启动时可能清除其他 Worker 的诊断，并发写入也可能交错。需要隔离诊断时，为各实例指定独立日志目录；每次启动独立的 stderr 文件仍可用于排障。

Worker 将顶层异常、HELLO 失败及 ERROR 日志（包括未处理的 asyncio 任务异常）写入诊断文件。HELLO 失败诊断在关闭 IPC 前落盘，确保父进程开始回收时已可读取；普通 ERROR 日志沿用后台写入。诊断写入失败不会替换原异常。Worker 启动失败后，Supervisor 在回收子进程时记录诊断路径及文件末尾最多 16 KiB 的内容；文件不可读或不存在时记录不可用原因。普通 stderr 仍只保存在原始文件中。

Worker stderr 复用应用日志的大小与保留常量：20 MiB 触发轮转，最多保留 20 份归档。Worker 自己的后台线程每 5 秒检查文件，复制归档后截断原文件，保留原有文件描述符；resident Worker 在 Front 退出后仍继续轮转。归档名为 `runtime-worker-<UUID>[-<owner>].stderr_<UTC日期>_<UTC时间>_<纳秒>.log`，同一 Worker 的后缀递增去重，每份不超过 20 MiB。分块边界保留完整 UTF-8 字符。一次突发超过阈值时拆分归档；超过完整归档预算的旧内容被丢弃，最多保留最近 400 MiB。轮询期间当前文件可暂时超过阈值。

Worker 的维护线程首次运行及之后每 60 秒还会清理同目录的历史文件，Front 启动不等待目录扫描：

- 确认已关闭的 Worker 输出和已完成归档合计保留最新 20 个符合清理条件的文件，合计不超过 400 MiB。
- `supervisor-<PID>-<时间戳>.txt` 诊断快照独立保留最新 20 份，合计不超过 400 MiB。
- 中断轮转留下的临时归档在确认句柄已关闭、且超过 60 秒保护期后清理，保留其完整源日志。
- Linux/macOS 新基础文件名带有归属标识。Linux 使用内核启动 UUID、PID/用户命名空间、命名空间 init 启动时间及有效 UID 生成；macOS 使用内核启动会话 UUID 和有效 UID。POSIX 仅在标识与本地一致、且本地句柄扫描完整时删除基础日志或临时归档。其他实例的标识、无标识的旧文件、归属无法确认的文件（包括之前启动或命名空间遗留的日志）均保留在清理预算之外。本地未发现句柄或修改时间较旧，都不能证明远端 Pod 已关闭文件。已完成的归档不受归属标识限制。
- 修改时间距今不足 60 秒的文件暂缓删除。Windows 根据文件共享状态确认基础日志可否删除，保护尚未关闭的 stderr 继承句柄，无需枚举系统进程。POSIX 先按数字真实、有效和保存 UID 筛选相关进程，再检查打开的文件；无法解析用户名、其他用户进程的文件不可读不会阻断清理。进程归属或句柄仍不可读、或扫描超过 2 秒时，保留基础日志并记录告警，不把部分扫描结果当作已关闭的证明，继续清理已完成的归档。归档被占用而无法删除时稍后重试，不阻断当前文件轮转。

复制后截断与应用日志采用相同策略；复制快照与截断之间并发写入的内容可能丢失。维护工作在业务事件循环之外运行，维护失败记录告警，关闭时最多等待维护线程 0.2 秒。父进程已配置 stderr 文件但启动校验失败时，轮转不启动并记录告警；缺少父进程配置时仍正常静默跳过。参数沿用 `common/utils.py` 中的 `_LOG_FILE_MAX_BYTES` 与 `_LOG_FILE_BACKUP_COUNT`，不新增 `config.yaml` 配置项。
