# S_24 DeepResearch SDK 与 runner 日志规约

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_24 / deepresearch-sdk-diagnostics |
| 关联模块 | `common/tools/deepresearch/tools.py`、`common/tools/deepresearch/diagnostics.py`；RelayClaw `office-claw-skills/deepresearch/scripts/run_deepsearch.py`、`packages/api/src/routes/support-logs.ts` |
| 最近一次修订日期 | 2026-11-18 |
| 关联 feature | 暂无；故障与验证见 [B_07](../bugfix/B_07_deepresearch-sdk-logging.md) |

## 范围与边界

本规约维护主研究 Python 子进程的日志目录、完整 stderr 采集与跨进程定位，
不修改研究状态、报告内容、澄清/大纲交互、停止恢复或 stdout NDJSON 协议。
报告局部改写与选区解析不在本规约范围内。

安全定位日志只包含路径、调用关联和采集状态，不能打印报告正文、提示词、配置帧或凭据。
独占的原始 stderr 文件保存上游实际输出，可能包含异常中的工具参数或报告片段；
它是本机受限故障证据，不作为模型结果或普通定位日志输出。文件导出由宿主收集层负责脱敏。

## 跨仓契约卡

| 项 | 当前契约 |
|---|---|
| 生产者 | Jiuwen `tools._build_deepresearch_request_config` 与 `deepresearch_stream` |
| 消费者 | RelayClaw 独立 runner；OfficeClaw support-log export 消费文件 |
| 传输与版本 | 既有 stdin JSON config frame，`version=1`，保持所有 config 值为字符串 |
| 新字段 | 可选 `OFFICE_CLAW_DEEPRESEARCH_LOG_DIR`，绝对规范路径，结尾为 `output/logs`；值为 `get_logs_dir(service_id)/DeepResearch/output/logs` |
| 备用根 | 仅从可信宿主 `os.environ` 读取非空的 `OFFICEACE_USER_DATA_ROOT`、`OFFICE_CLAW_DATA_DIR`，规范成绝对路径后经 config frame 下发；均无有效值时，可信 `OFFICE_CLAW_HOST_HOME/.office-claw` 作为 `OFFICE_CLAW_DATA_DIR`；不接受租户模型配置提供的路径，不扩大子进程环境变量白名单 |
| 兼容 | 旧 runner 忽略未消费的 config 字段；旧宿主或独立 CLI 缺字段时由 runner 使用既有备用路径。没有宿主数据根的独立 Jiuwen 不伪造该配置 |
| 失败语义 | 日志目录、写入、读取或关闭失败均显式 warning；不得把诊断错误替换成研究失败。磁盘写失败后仍排空 stderr 管道；读取失败只能保存已取得部分并标记不完整 |
| 关联 | `request_id`、`conversation_id`、随机 `attempt`、子进程 `pid`；定位日志记录字节数、关闭结果、是否完整 |
| 验证责任 | Jiuwen 验证 config/spawn/drain 与真实短 Python 子进程；Relay runner 验证 SDK LogManager 实际落盘；宿主 export 验证实际文件进入 ZIP；跨进程集成由任务集成人复核 |
| 回滚 | 移除新增 config 字段、诊断 sink 与工作目录指定；保留既有 stdout/终态/停止协议，无持久研究数据迁移 |

## 不变量

- **N1 路径单一来源**：使用 `common.utils.get_logs_dir(service_id)`，尊重平台路径覆盖与 `LOG_ROOT_PATH`，不能根据 Electron、当前工作区或隔离后的 `HOME` 猜测服务日志根。
- **N2 SDK 早期导入一致**：宿主在启动前创建 `DeepResearch` 基目录，并把子进程 `cwd` 指向该目录；SDK 导入时的安全根与后续 `output/logs` 对齐。目录创建失败时保留继承 cwd，warning 后交给 runner 的备用路径处理。
- **N3 完整 stderr 与尾部分离**：每次进程独占 `runner/<safe-conversation>.<UTC>.<uuid>.stderr.log`，二进制逐块追加，完整文件不限于末 20KB。模型结果仍沿用有界、脱敏的 `stderr_tail`，不新增本机日志路径字段。
- **N4 所有终态保留**：`completed`、`error`、`interrupted`、`stopped` 和调用取消均保留文件；不能以 `outcome.status == error` 作为落盘条件。
- **N5 并发与跨平台**：文件名仅安全标识、无冒号 UTC 时间和 UUID；不包含 query。采用 `O_EXCL`、`O_APPEND`、平台可用时的 `O_BINARY` 和 `O_NOFOLLOW`，创建权限 `0600`；Windows 不依赖 POSIX 命令或删除打开中的文件。
- **N6 资源生命周期**：stderr 在发送 stdin 配置前已开始排空；通常先等进程收尾与 drain，再 flush/close。spawn 等待期间取消也关闭自有 journal 并移除自有 progress artifact，不处理未返回的其他进程。
- **N7 不阻塞事件循环**：目录与文件操作使用 `asyncio.to_thread`。逐块写入和关闭等待均有 1 秒界限，超时标记不完整并持有后台任务；写超时后禁用后续落盘但继续排空管道，取消不能无限等待故障磁盘。
- **N8 定位日志不泄露内容**：仅记录固定原因/异常类，不能把异常原文、stderr 内容或配置帧抄进宿主定位日志；不完整时不得声称完整保存。

## 接口与数据结构

`trusted_fallback_log_config(environ)` 返回经过绝对路径校验的宿主备用根字段。
`prepare_runner_diagnostics(log_dir, conversation_id, request_id)` 返回每次调用私有的
`RunnerDiagnostics`；字段缺省返回 `None`，不对旧适配器增加文件副作用。

`RunnerDiagnostics` 持有二进制文件与线程锁、实际 cwd、日志路径、attempt、pid、
已写字节数及不完整标记。`write` 支持短写并在磁盘错误后停止落盘但不停止管道读取；
`write_runner_diagnostics` 与 `close_runner_diagnostics` 提供有界等待，超时后引用保存在模块后台集合，最终完成后摘除。

start 未带 `conversation_id` 时，由宿主在认领旧快照分支之后生成 UUID，
通过既有 `--conversation-id` 参数交给 runner。首次 started 帧和日志文件使用同一标识；
恢复与认领已有快照不生成新标识。

## 执行路径全景

- **P1 配置**：`_build_deepresearch_request_config` → `get_logs_dir(service_id)` →
  新增目录与可信备用根进入既有 stdin frame，不进入 argv 或扩展 child env。
- **P2 准备**：`deepresearch_stream` 解析 runner/认领快照/构建 argv →
  `prepare_runner_diagnostics` 在线程创建根与独占文件 → 带可用 cwd 启动真实 Python。
- **P3 采集**：独立 `_drain_stderr` 读取原始 bytes → 有界内存尾部 + 线程写完整文件；
  stdout 同时由 `_consume_stream` 解析原协议，两条通道互不混入。
- **P4 收尾**：既有停止/回收进程 → 有界等待 stderr drain → 关闭日志；
  宿主记录路径、关联标识、字节数与完整性。只有既有 error 结果附带截短脱敏 tail。
- **P5 导出**：服务 `.logs` 下的 `DeepResearch/output/logs` 与 `DeepResearch/runner`
  由 OfficeClaw support-log roots 覆盖；完整日志进入诊断归档，不经研究工具结果回传全文。

## 异常分支与退出路径

- 非绝对目录/错误后缀、创建失败：warning；不能覆盖旧文件；cwd 保留继承路径或已创建的基目录。
- journal 打开/磁盘写失败：记录 `stderr_enabled`、`prepare_failed` 或不完整状态，继续排空。
- stderr 读取失败：保存已读取 bytes，warning 标记 `stderr_read_<异常类>`；研究结果不被诊断异常覆盖。
- spawn 失败/取消：关闭已创建 journal，移除本次 progress artifact；保留原失败/取消语义。
- 关闭慢/失败：1 秒后返回并记录不完整，后台仍负责最终关闭，不无限阻塞取消。

## 边界 case 清单

| 场景 | 当前行为与检查点 | 测试锚点 |
|---|---|---|
| 服务路径与可信备用根 | 经 stdin frame 传递；路径不进入 argv 或放宽后的 child env | `test_request_config_uses_service_log_root_for_deepresearch_sdk`、`test_runner_fallback_root_crosses_only_config_frame_from_trusted_process_env` |
| 并发、空格和 Unicode 目录 | 独占文件互不覆盖；stdout marker 与文件关联一致 | `test_real_runner_process_logs_first_cause_and_binary_tail_under_unicode_path`、`test_concurrent_attempts_do_not_share_files_or_block_event_loop` |
| 四终态与取消 | 原始 stderr 首尾保留，模型结果仍为短尾部 | `test_stream_preserves_complete_stderr_in_unique_attempt_logs`、`test_cancelled_stream_preserves_stderr_before_reraising` |
| spawn 等待中取消 | 清理自有 progress artifact 并关闭 journal；不处理未返回的其他进程 | `test_cancelled_spawn_closes_journal_and_removes_progress_artifact` |
| 短写与磁盘故障 | 短写继续追加；故障记录不完整并继续排空 | `test_stderr_journal_handles_short_writes_and_closes_after_disk_failure`、`test_journal_failure_keeps_research_and_pipe_drain_alive` |
| 慢写与慢关闭 | 有界返回，后台任务最终收尾 | `test_stalled_write_disables_capture_without_stalling_next_drain_chunk`、`test_cancelled_spawn_does_not_wait_indefinitely_for_slow_journal_close` |

## 修改检查单

- 改目录来源、备用根或配置帧 → 核对绝对路径、可信进程环境边界与旧 runner 兼容；复跑 frame 入口测试。
- 改 stderr 文件或 I/O 生命周期 → 复跑 `test_deepresearch_diagnostics.py` 和 `test_deepresearch_stream_tool.py`，独立读取真实子进程产生的文件字节。
- 改终态、恢复或取消 → 加跑 `test_deepresearch_runtime.py`、`test_deepresearch_execute_tool.py`；SDK 初始化与 ZIP 收集分别验证，单层通过不能推定安装包运行进程已更新。

## 与其它 spec 的关系

- [[S_06]] 维护 DeepResearch 四阶段成果快照；本规约只增加日志副作用，不改变其落盘条件或内容。
- [[S_20]] 维护工具结果与流式事件发射；本规约不向模型结果添加完整 stderr 或本机日志路径。
- SDK runner 与 support-log export 位于 RelayClaw 仓，双方按本规约的 config v1 与文件目录契约集成，验证结果维护在 [[B_07]]。
