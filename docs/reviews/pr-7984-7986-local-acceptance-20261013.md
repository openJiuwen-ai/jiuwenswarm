# PR 7984 7985 7986 本地组合复核与验收

已在独立本地分支顺序合入三个 PR，完成身份校验、并发恢复和后台准备修复，并拆分可安全并行的冷 Worker 初始化链。正确性回归和真实 IPC、WebSocket 恢复验收通过。**冷 Worker P95 ≤ 2.5 秒仍未达标，完整性能验收不通过，不能据此关闭冷启动目标。** Front、首次 identity 最大值和串行控制指标中仍有超限项，下表保留全部门禁结果。

本次范围按用户确认限定为当前 Swarm 分支，保留锁定的 OpenJiuwen SDK。源代码与 Frozen 包分别验收，性能超限样本保留。

检视依据：[三 PR 联合复核](/D:/wechat_talk_file/xwechat_files/wxid_7532195322912_037f/msg/file/2026-10/pr-7984-7986-review-issues.md)、[PR 7942 性能差距](/D:/wechat_talk_file/xwechat_files/wxid_7532195322912_037f/msg/file/2026-10/pr-7942-performance-gap-and-action-plan.md)及 [前期设计检视](/D:/wechat_talk_file/xwechat_files/wxid_7532195322912_037f/msg/file/2026-10/jiuwenswarm-issue-4689-phase-234-review-20261010.html)。按本地代码链路确认建议有效后实施；保留共享资源与会话可变状态隔离、迁移先于消费者和各入口的传输边界。

## 分支与代码基线

- 本地分支：`fix/pr7984-7986-review-20261013`。
- 工作树：`D:\work_space\jiuwenswarm-pr7984-7986-review-20261013`。
- 开始工作时拉取的 develop：`9384bc2c74b218432d221ec13a5eadb2b5347cdc`。
- 顺序合入的 PR head：7984 `d3f129801fcf3b92cf84f8ce41abb5ac50572391`；7985 `aef5e167400a9d3dee93e2c767284e584e13fb31`；7986 `6c23e5d760c81010b60a133f14ac99b7e8cff53e`。
- 未修复的三 PR 组合基线：`068109eba0fafa6474edb0cc44066e0bc731e0a8`。
- 正式性能采样和 Frozen 构建的产品提交：`f002002d6397873a5ecf963ca56f9839842a32bf`。
- 终验查询确认远端 origin 同名分支已有产品提交 `f002002d6397873a5ecf963ca56f9839842a32bf`；本轮验收报告提交仍在本地。原工作树的无关文件保留。

## 检视建议的处理结果

| 建议 | 当前调整与验收结论 |
| --- | --- |
| 拆除冷 Worker 核心串行链 | 必要 SDK 代码导入与 workspace、配置迁移使用两个受控线程重叠；配置迁移仍为单一写入者，完成后才配置日志、应用全局 patch、开放消费者。扩展加载与中性 Server 构造重叠，二者完成后才启动 Runtime。构造成功后先登记清理 owner，再传播扩展失败。增加阶段耗时和事件循环 lag。目标 2.5 秒保留为未达标。 |
| 只准备最小执行集 | checkpointer 的进程生命周期移至独立模块；Team controller、RSI 公共导出、当前模式 adapter、非当前模型客户端按需加载。保留模型目录、别名、全局索引、固定模型 ID、自定义 Code Spec 和 reload 回滚语义。必要模型和安全 Rail 仍需在真实 SDK 入口前完成。 |
| 减少源码 identity 重复成本 | Front 经验证后通过一次性私有 stdin 管道传递身份和源码观察结果；Worker 校验父进程、部署路径、data scope、依赖与源码 manifest。常驻重连仍做新 Front 身份比较。内容读取只在缓存缺失时有界并行；USN 不可用时在可验证 NTFS 上使用 ChangeTime/file ID，其他不可可靠观察的文件系统全内容校验。源码路径和 marker 验证仍为 O(N)，尚未实现开发态 O(1)。 |
| 首次部署单独门禁 | 新增 source identity 首建、缓存命中及 Worker 新 checkout、缓存 checkout 指标。每个首次部署样本使用独立 identity cache；fresh.cold 成为必需项。首次 identity、新 checkout、fresh 同时检查 P95 和每个样本最大值。500ms identity 预算是新增工程预算，原 Worker 2500ms 阈值未修改。 |
| gitconfig 异常不阻断启动 | 捕获 configparser.Error，BOM、缺 section、非法行、损坏仓库配置和 GIT_CONFIG_GLOBAL 配置均降级完整内容校验。真实子进程验证在异常 global gitconfig 下仍发出 HELLO。 |
| 并发恢复隔离及显式 pending | publication lock 按完整 scope 隔离；同 scope 保持 snapshot、replay、live 顺序，并在首个 await 前固定 replay 列表。snapshot 有界等待后明确返回 recovery_pending。Front 和 Web 保留 processing 状态；Web 最多三次重试同一 metadata/cursor 查询，独立会话并发恢复，不重发输入。 |
| 可选准备降低首请求竞争 | 后台并发限制为 1；安全同步 import、配置读取和明确无 loop 亲和性的 factory 移至线程。CHAT、RESUME、ANSWER 排队即暂停新后台准备，直到真实 SDK 最小执行边界；首次显式使用能力可以晋升，避免互相等待。已开始的线程任务无法抢占中断，未声明的 async factory 同步前缀仍可能产生 lag。增加 optional 全开冷请求和 lag 证据。 |
| Kernel 及普通聊天 Sandbox 边界 | 保留合入 PR 7985 的 per-key singleflight 和普通聊天不等待 Sandbox 行为；本次对其进行回归，未重复记为新改造。 |
| 平台 CI 和最终性能目标 | 初次复核时原三个 PR 的远端 CI 有失败；终验查到远端已有产品提交，但其 CI 结果尚未核实。冷 Worker ≤2.5 秒及部分其他性能门禁失败，不能标记为全部完成。 |

关键实现见 `server/worker/lifecycle.py`、`server/worker/preparation.py`、`runtime/checkpointer.py`、`runtime/team_controller.py`、`server/supervisor/identity*.py`、`server/worker/gateway_bridge.py`、`server/supervisor/runtime_supervisor.py` 和 Web `services/webClient.ts`。

## 正确性验收

- Python 广回归：164 个测试文件，**2861 passed、3 skipped**，0 失败，0 重复用例，Python 源码采样前后相同，最终审计确认与产品冻结字节一致；测试子进程无遗留。证据：[汇总](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/broad-regression-finalized/summary.json)、[JUnit](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/broad-regression-finalized/results.xml)。
- 三项 skip 来自 Windows 平台边界：Unix socket 文件权限一项、Process CLI 的 SIGINT/SIGTERM 优雅退出两项。
- 真实持久 Worker 和 Windows named pipe：Front 重连前后 PID/instance 保持，回放 2048 帧，另一会话 snapshot 为 1.735ms；同 scope 超时明确 pending，随后恢复。replay/live 序列 1..2049 无缺失或重复。[IPC 原始证据](/D:/work_space/jiuwenswarm-pr7984-7986-review-20261013/artifacts/review/recovery-ipc.json)。该用例使用确定的 Runtime 状态 fixture，不调用外部模型。
- WebClient Node 回归 13 项通过，包括旧 socket 迟到事件隔离与旧 connect promise 结束；真实 localhost TCP WebSocket 中，两会话独立恢复，4 次 metadata 查询、2 次 pending，processing 保持，输入重发 0 次。[WebSocket 原始证据](/D:/work_space/jiuwenswarm-pr7984-7986-review-20261013/artifacts/review/recovery-websocket.json)。服务端为 metadata 协议 fixture，使用实际生产客户端 bundle。
- 前端从最终源码执行 tsc 与 Vite 构建通过；最终审计确认测试、构建的源码字节一致，Frozen 使用这次构建的 dist。[前端测试与构建凭证](/D:/work_space/jiuwenswarm-pr7984-7986-review-20261013/artifacts/review/frontend-final-build-receipt.json)。官方 PyInstaller spec clean 构建通过，安装 SDK、protocol 的 Python 源码字节及产品文件在构建前后保持。[构建与依赖凭证](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/frozen-candidate-build-result.json)。
- git diff --check 通过；修改 Python 文件相对三 PR 基线没有新增 Ruff 问题，保留的 73 项为原有 interface_deep.py 导入顺序问题，不能等同全仓 Ruff 全通过。
- 最终字节及清理审计通过：正式矩阵 1640 条原始记录，独立控制补测 480 条；原始记录功能失败 0，所属测试 scope 残留进程 0。产品文件、安装 SDK 与 protocol 的 Python 源码、前端产物和完整 EXE 均与构建凭证相符。[完整性与清理审计](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal-review-final-integrity.json)。

## 正式性能验收口径

Windows 本机分别测源码和新构建 Frozen 的 agent、code.normal。每个冷请求、Front、首次部署、optional 场景至少 30 个真实进程样本；同会话、新会话的常驻请求各 100 个。source identity 首建与命中各 30 个独立进程。输出 nearest-rank P50、P95、P99、max，超限退出非零，失败和不完整样本进入自动判定。

冷计时终点为首次实际 DeepAgent.send_input SDK 入口，另验证终态和进程清理。源码使用观察器，Frozen 使用紧邻实际 SDK sender 的只读 native hook。模型服务为本地 OpenAI 协议 fixture；它验证真实 Swarm、SDK、Runtime、IPC 和请求路径，不代表商业模型网络时延。正式采样关闭 trace、importtime、coverage。

首次部署使用新进程、新数据目录、隔离 identity cache；缓存 checkout 明确在计时前建立身份缓存。没有清除 Windows 页缓存，这些数据证明冷进程和首次身份缓存场景，不能替代强制冷磁盘验收。optional 场景启用当前平台支持的 PC、Proactive、Symphony、图像和真实本地 MCP 子进程；Windows 不支持的 Sandbox 按 unsupported 记录。保留产品权限策略。

source 与 Frozen、模式及版本分开统计。未修改组合树与修复树按样本交替比较冷新 checkout；开始工作时的 develop 作为相同场景的参考，未对参考树重复全部控制与常驻矩阵。参考树性能不参与修复树的门禁判定。

历史控制指标在一个 WebSocket 上按 session.list → project.list → config.get → history.get 串行发送，时间从 Front 进程创建算到各响应收到。因此后面的指标包含连接、HELLO 和此前 RPC 的等待。原始门禁仍使用该口径；单次 RTT 单独保留，不能用 RTT 替代启动门禁。补充的 control_first_response 每次使用独立新 Front，仅发送被测接口作为第一个 RPC，同样从进程创建算到响应；它用于解释首次可用性，与原串行门禁分开判定。

源码 code.normal 的 optional 全开场景在驱动中断前完成 16 个样本，随后在同一冻结产品、依赖和阈值下补齐剩余 14 个独立进程样本。原始文件前缀字节保持，补采记录单独留存；该组不是一次不间断运行。

### 修复树性能门禁

时间单位为 ms。新 checkout.cold 与 worker.cold_new_checkout、chat.cold 与 worker.cold_cached_checkout 为同一批样本的别名，表中仅列一次。首次部署的判定包含 max；其余使用 P95。

#### source agent

| 指标 | n | P50 | P95 | P99 | max | 阈值 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| source_identity.first_build | 30 | 378.78 | 440.63 | 512.35 | 512.35 | 500 P95及max | 未达标 |
| source_identity.cache_hit | 30 | 233.35 | 280.00 | 296.56 | 296.56 | 500 | 通过 |
| front.listen | 30 | 283.09 | 303.01 | 304.80 | 304.80 | 300 | 未达标 |
| session.list | 30 | 366.18 | 389.99 | 392.19 | 392.19 | 500 | 通过 |
| project.list | 30 | 421.23 | 475.18 | 604.22 | 604.22 | 500 | 通过 |
| config.get | 30 | 495.87 | 572.47 | 699.49 | 699.49 | 500 | 未达标 |
| history.get | 30 | 503.14 | 584.55 | 713.66 | 713.66 | 500 | 未达标 |
| worker.cold_new_checkout | 30 | 7745.44 | 8236.35 | 8335.95 | 8335.95 | 2500 P95及max | 未达标 |
| worker.cold_cached_checkout | 30 | 7541.32 | 14073.93 | 14348.15 | 14348.15 | 2500 | 未达标 |
| chat.warm_same | 100 | 109.15 | 145.66 | 156.89 | 174.55 | 500 | 通过 |
| chat.warm_new | 100 | 241.44 | 376.23 | 493.63 | 555.54 | 500 | 通过 |
| fresh.cold | 30 | 8589.71 | 9533.33 | 9609.68 | 9609.68 | 2500 P95及max | 未达标 |
| optional_all.cold | 30 | 7958.92 | 8607.54 | 8662.94 | 8662.94 | 2500 | 未达标 |

完整门禁：**失败**；功能失败样本 0，不完整统计组 0。[完整汇总](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal/candidate-source-agent/complete.summary.json)。

#### source code.normal

| 指标 | n | P50 | P95 | P99 | max | 阈值 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| source_identity.first_build | 30 | 413.97 | 481.43 | 506.50 | 506.50 | 500 P95及max | 未达标 |
| source_identity.cache_hit | 30 | 245.55 | 322.90 | 347.33 | 347.33 | 500 | 通过 |
| front.listen | 30 | 278.07 | 302.68 | 329.50 | 329.50 | 300 | 未达标 |
| session.list | 30 | 347.77 | 393.63 | 394.30 | 394.30 | 500 | 通过 |
| project.list | 30 | 393.13 | 448.37 | 564.46 | 564.46 | 500 | 通过 |
| config.get | 30 | 469.57 | 521.80 | 653.99 | 653.99 | 500 | 未达标 |
| history.get | 30 | 475.38 | 526.95 | 667.00 | 667.00 | 500 | 未达标 |
| worker.cold_new_checkout | 30 | 7934.15 | 8454.26 | 8927.64 | 8927.64 | 2500 P95及max | 未达标 |
| worker.cold_cached_checkout | 30 | 7753.57 | 8772.39 | 8842.06 | 8842.06 | 2500 | 未达标 |
| chat.warm_same | 100 | 77.17 | 108.28 | 119.52 | 127.17 | 500 | 通过 |
| chat.warm_new | 100 | 252.18 | 301.54 | 514.47 | 586.83 | 500 | 通过 |
| fresh.cold | 30 | 8570.30 | 9060.77 | 10595.35 | 10595.35 | 2500 P95及max | 未达标 |
| optional_all.cold | 30 | 7833.09 | 8587.10 | 8619.78 | 8619.78 | 2500 | 未达标 |

完整门禁：**失败**；功能失败样本 0，不完整统计组 0。[完整汇总](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal/candidate-source-code.normal/complete.summary.json)。

#### frozen agent

| 指标 | n | P50 | P95 | P99 | max | 阈值 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| front.listen | 30 | 339.89 | 394.99 | 396.19 | 396.19 | 300 | 未达标 |
| session.list | 30 | 391.08 | 450.95 | 456.48 | 456.48 | 500 | 通过 |
| project.list | 30 | 419.32 | 477.77 | 573.92 | 573.92 | 500 | 通过 |
| config.get | 30 | 481.32 | 540.13 | 639.47 | 639.47 | 500 | 未达标 |
| history.get | 30 | 485.80 | 544.30 | 643.45 | 643.45 | 500 | 未达标 |
| worker.cold_new_checkout | 30 | 6125.31 | 6855.49 | 6896.21 | 6896.21 | 2500 P95及max | 未达标 |
| worker.cold_cached_checkout | 30 | 6123.03 | 6944.14 | 7089.41 | 7089.41 | 2500 | 未达标 |
| chat.warm_same | 100 | 88.31 | 117.88 | 124.41 | 133.38 | 500 | 通过 |
| chat.warm_new | 100 | 244.06 | 305.53 | 480.85 | 528.82 | 500 | 通过 |
| fresh.cold | 30 | 6524.18 | 6816.91 | 6839.43 | 6839.43 | 2500 P95及max | 未达标 |
| optional_all.cold | 30 | 6004.51 | 6409.53 | 6540.12 | 6540.12 | 2500 | 未达标 |

完整门禁：**失败**；功能失败样本 0，不完整统计组 0。[完整汇总](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal/candidate-frozen-agent/samples.summary.json)。

#### frozen code.normal

| 指标 | n | P50 | P95 | P99 | max | 阈值 | 结果 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| front.listen | 30 | 347.57 | 394.44 | 397.57 | 397.57 | 300 | 未达标 |
| session.list | 30 | 400.27 | 449.17 | 458.56 | 458.56 | 500 | 通过 |
| project.list | 30 | 428.96 | 487.38 | 520.67 | 520.67 | 500 | 通过 |
| config.get | 30 | 491.07 | 563.74 | 577.22 | 577.22 | 500 | 未达标 |
| history.get | 30 | 495.58 | 569.96 | 581.06 | 581.06 | 500 | 未达标 |
| worker.cold_new_checkout | 30 | 5795.43 | 6398.94 | 6489.35 | 6489.35 | 2500 P95及max | 未达标 |
| worker.cold_cached_checkout | 30 | 5778.74 | 6170.40 | 6266.03 | 6266.03 | 2500 | 未达标 |
| chat.warm_same | 100 | 87.50 | 118.30 | 133.27 | 138.51 | 500 | 通过 |
| chat.warm_new | 100 | 269.14 | 365.07 | 566.30 | 659.09 | 500 | 通过 |
| fresh.cold | 30 | 6469.27 | 7136.94 | 11758.29 | 11758.29 | 2500 P95及max | 未达标 |
| optional_all.cold | 30 | 5890.28 | 6256.11 | 6452.00 | 6452.00 | 2500 | 未达标 |

完整门禁：**失败**；功能失败样本 0，不完整统计组 0。[完整汇总](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal/candidate-frozen-code.normal/samples.summary.json)。

### 冷 Worker 分段耗时

下表取新 checkout 的现有产品日志，各组 30 个 Worker。SDK import 和受控 join 为阶段耗时；后三列为 Worker 内部计时点开始的累计时间，与聊天请求计时起点不同。各列 P95 不能相加。

| 形态与模式 | SDK import P95 | 并行 join P95 | imports ready 累计 P95 | constructed 累计 P95 | runtime started 累计 P95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| source/agent | 3812.00 | 3812.00 | 4300.00 | 4670.00 | 4670.00 |
| source/code.normal | 4171.00 | 4171.00 | 4660.00 | 5020.00 | 5020.00 |
| frozen/agent | 2985.00 | 2985.00 | 3270.00 | 3700.00 | 3700.00 |
| frozen/code.normal | 3016.00 | 3032.00 | 3280.00 | 3660.00 | 3660.00 |

完整阶段、日志与 loop lag 汇总：[分段证据](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal-worker-phase-summary.json)。并行 join 仍主要受 SDK import 耗时控制，SDK 后的当前模式 adapter、模型、tool cards 和必要安全 Rail 准备仍留在关键路径。loop lag 取每个试验进程生命周期内的已记录区间最大值；chat 场景还包含常驻请求，不能据此单独归因于首个 SDK 入口。

### 控制接口独立首次请求补测

每个接口、形态和模式各 30 个独立新 Front，阈值为 P95 ≤500ms。下表结果不覆盖原串行控制门禁，也不改变完整矩阵失败的结论。

| 形态与模式 | 第一个 RPC | n | P95 ms | max ms | 结果 |
| --- | --- | ---: | ---: | ---: | --- |
| source/agent | session.list | 30 | 379.52 | 395.38 | 通过 |
| source/agent | project.list | 30 | 375.73 | 376.70 | 通过 |
| source/agent | config.get | 30 | 382.93 | 388.08 | 通过 |
| source/agent | history.get | 30 | 318.15 | 324.74 | 通过 |
| source/code.normal | session.list | 30 | 374.01 | 376.68 | 通过 |
| source/code.normal | project.list | 30 | 367.95 | 382.16 | 通过 |
| source/code.normal | config.get | 30 | 378.43 | 414.75 | 通过 |
| source/code.normal | history.get | 30 | 324.36 | 355.75 | 通过 |
| frozen/agent | session.list | 30 | 446.16 | 447.31 | 通过 |
| frozen/agent | project.list | 30 | 427.46 | 447.07 | 通过 |
| frozen/agent | config.get | 30 | 454.19 | 454.85 | 通过 |
| frozen/agent | history.get | 30 | 358.80 | 363.53 | 通过 |
| frozen/code.normal | session.list | 30 | 444.96 | 449.55 | 通过 |
| frozen/code.normal | project.list | 30 | 430.64 | 440.34 | 通过 |
| frozen/code.normal | config.get | 30 | 432.89 | 440.50 | 通过 |
| frozen/code.normal | history.get | 30 | 381.54 | 394.93 | 通过 |

独立首次请求补测：[原始汇总](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/control-first-response/aggregate.json)。

### 源码冷新 checkout 对照

各版本、模式各 30 次，单位 ms。变化取同一交替采样的三 PR 组合对照；它描述本机这批样本，不构成所有环境的加速承诺。

| 模式 | develop P95 | 原组合 P50 | 修复 P50 | 原组合 P95 | 修复 P95 | 观测 P95 降低 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| agent | 10572.21 | 7749.39 | 7745.44 | 8673.34 | 8236.35 | 5.04% |
| code.normal | 18543.00 | 8401.51 | 7934.15 | 9223.31 | 8454.26 | 8.34% |

agent 的 P50 基本不变，code.normal 的 P50 约降低 5.56%；两组 P95 有小幅下降。结果支持本机这批样本的有限改善，尚不能认定冷启动已大幅加速或达到目标。

## 保留的未完成项

1. **冷 Worker P95 ≤2.5 秒仍未完成。** 当前 Swarm 改造拆开了可安全重叠的路径，未改变锁定 SDK。SDK 自身仍有较重的公开 import 链；必要模型、tool cards 和安全 Rail 装配仍按依赖顺序进行，尚未实现最小执行集的全部并行化。显式选中其他模型时仍可能先构造 SDK 默认客户端。
2. **开发态 identity 仍为 O(N)。** controlled handoff 减少重复完整计算，可靠 NTFS 标记支持跨进程缓存；未知文件系统保留全内容校验。长远的安装时稳定 build identity 和开发变更观察机制尚未完成。
3. **其他性能门禁仍有失败。** source/agent：source_identity.first_build, front.listen, config.get, history.get；source/code.normal：source_identity.first_build, front.listen, config.get, history.get；frozen/agent：front.listen, config.get, history.get；frozen/code.normal：front.listen, config.get, history.get。独立首次接口补测另行报告，未删除或放宽原始门禁。
4. **修复分支的远端 CI 未验证。** 初次复核时三个原 PR 均有 ci-failed、AI-Review-Failed；7984 的 UT/build/Ruff 通过而静态检查失败，7985、7986 的 UT/静态检查失败。7986 官方 UT 报告中的 Linux 非 child zombie 清理和 benchmark sys.path provenance 两项失败已在本地修复并有回归覆盖；没有执行 Linux 真进程验收。7985 UT 报告访问 403，不能推断失败内容。终验时远端同名分支产品 head 为 `f002002d6397873a5ecf963ca56f9839842a32bf`，其 CI 结果本次尚未核实；验收报告提交仍在本地。原 PR 状态按初次复核快照记录，不能视为当前状态或修复提交结果。

## 环境与复现

正式驱动、源码 Worker 与打包使用 `D:\anaconda\envs\debug\python.exe`，Python 3.11.15。OpenJiuwen 为非 editable 的 0.1.19，commit `6df9699e650b3ad9b9835b46ae3c3b7572adb04b`；gateway-protocol 为非 editable 的 0.1.0，commit `dd005b9bdd684d079627bce3f4413e92a8a4069b`。pyproject.toml、uv.lock 及安装 SDK 的 Python 源码字节保持；SDK manifest 不涵盖所有第三方依赖的本机二进制文件。

Frozen 可执行文件：`D:\work_space\jiuwenswarm-pr7984-7986-review-reports-20261013\frozen-candidate-dist\workswarm\workswarm.exe`。SHA256：`e79539e16f9d4e101d9998052f8c3b992af4b74193f9163e1088e8254f7b11c9`。

性能总汇：[formal-review-aggregate.json](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal-review-aggregate.json)。正式任务 ledger：[源码和Frozen任务](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal-review-matrix-ledger.json)、[组合树对照任务](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal-source-pair/ledger.json)。每组目录保留 samples.jsonl、summary.json、environment.json、原生日志和 driver.log。

串行控制口径及源码慢样本分段分析：[分析凭证](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal-front-control-and-cold-tail-analysis.json)；源码 code.normal 补采：[补采与原始字节凭证](/D:/work_space/jiuwenswarm-pr7984-7986-review-reports-20261013/formal/candidate-source-code.normal/resumption.json)。分段日志只描述耗时所在阶段，不据此单独归因于 OS、磁盘、CPU 或某个 SDK 模块。

产品中可直接复用的基准入口：

```powershell
Set-Location 'D:\work_space\jiuwenswarm-pr7984-7986-review-20261013'
& 'D:\anaconda\envs\debug\python.exe' scripts/benchmarks/issue4689_performance.py --baseline 'D:\work_space\jiuwenswarm-pr7984-7986-combined-baseline-20261013' --candidate . --candidate-only --mode agent --cold 30 --fresh-samples 30 --front-samples 30 --warm-same 100 --warm-new 100 --output 'D:\work_space\jiuwenswarm-pr7984-7986-review-reports-20261013\repeat-source-agent\samples.jsonl'
```

源码默认执行全部场景；Frozen 增加 `--candidate-executable '<新构建的workswarm.exe>'`，code.normal 独立运行 `--mode code.normal`。输出必须位于两个源码 checkout 之外；每次使用新的输出目录和当前分支产物，不能替换为此前其他工作树的二进制。性能超限时非零退出是验收结论的一部分。
