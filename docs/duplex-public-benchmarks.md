> 2026-09-16 实测记录：[InterruptBench 用户更新回放](duplex-interruptbench-replay-20260916.md)已完成 24 次试验；[办公与开发测试题对照](duplex-office-code-run.md)已完成四次试验。两者的评分范围与官方完整数据集不同，详见各报告。此前选型见[办公与开发多 Agent 数据集](duplex-office-development-datasets.md)。
>
> 当前 Native 接入已收缩为 [最简路由](duplex-simplification.md)：不再使用工具账本、浏览器动作收据或进程恢复。下文既有验证数字为历史记录，最新结果以上述实测报告为准。

# 全双工公开评测接入

需求来源是工作区 `jiuwen/全双工快慢Agent_完整调研与方案.md` 和
`jiuwen/全双工快慢Agent_Benchmark数据集调研.md`，以及后续 HTML 提案。
Native 路由当前采用 HTML 的 APPEND / INTERRUPT 二选一；公开评测遵循两份调研的
“不造新任务、不编写中途消息、沿用原评分器”。人工 Kafka 用例只用于机制测试。

## 已核实的原始输入

| 上游 | 固定版本 | 内容 |
| --- | --- | --- |
| [AgentRadio](https://github.com/Coral-Protocol/AgentRadio) | `5e4e137991ce5662d95cf890e534b5b223d24d6a` | 124 个任务、1,306 条 rubric、固定仓库 commit 和容器环境、四 Agent 五阶段协议 |
| [InterruptBench](https://github.com/HenryPengZou/InterruptBench) | `17da111e4858b93c0cab1d88f85e1735fbd1d423` | 六个更新集合，各 165 个任务，共 1,815 条原始 updates |

第三方仓库留在工作区外部路径，不把数据集复制进 Jiuwen。清单生成器检查版本及
tracked 文件无修改，记录原任务、环境、协议、评分器和数据文件的 SHA256：

```powershell
python -m jiuwenswarm.common.duplex_public_benchmark --agentradio D:/jiusi_agent/_repos/AgentRadio --interruptbench D:/jiusi_agent/_repos/InterruptBench --output D:/results/public-inputs.json
```

输出目录须存在；同名结果拒绝覆盖。输出状态明确为 `inputs_prepared_not_evaluated`。

## InterruptBench 输入桥接

`load_official_interrupt` 读取官方 raw、task config、interrupt spec 和 baseline
trajectory，校验任务 ID、初始意图与更新原文；直接执行固定版本上游的纯函数
`_resolve_interrupt_at_action` 计算触发位置，不另写一套百分比规则。

环境 runner 到达原动作边界后，可调用 `OfficialInterrupt.deliver`，通过真实
`AgentLifecycleHandler.on_user_input` 交给 Native 全双工控制器。边界错过、任务已终止
或输入被改写时拒绝投递；同一实验/阶段的重试使用相同 run_id。真实目标答案和
评分条件不会交给快模型或慢模型。

这个输入级 helper 只支持初始阶段的一次更新。完整执行入口 `interruptbench_runner`
在上游 runner 的实际注入分支接入，保留上游生成的逐阶段意图与历史，支持连续更新。
上游 `update_mode=append/replace` 是用户意图更新方式，不是快模型的正确动作标签。

## 完整执行入口

`jiuwenswarm.benchmarks.duplex_experiment` 执行整个实验，配置例子在
[AgentRadio](examples/duplex-agentradio.json)、[InterruptBench](examples/duplex-interruptbench.json)
和[快慢模型](examples/duplex-models.json)。另提供已验证调用的
[SiliconFlow 模型模板](examples/duplex-models-siliconflow.json)。复制到工作区外填写真实配置，不提交密钥。
例子默认只选一题验证接线；删除 `task_ids` 后使用完整官方任务集合。

```powershell
python -m jiuwenswarm.benchmarks.duplex_experiment D:/private/duplex-experiment.json --output D:/results/duplex-run-001
```

输出目录必须是新的。子进程逐条执行，不经过 shell 拼接；环境文件是字符串键值的
JSON，凭据只进入子进程环境，不进入实验配置副本或汇总。产物包括每步日志、
`execution.json`、原评分文件和 `report.json`。失败立即留下步骤和日志，不把它删掉。

### AgentRadio

- `JiuwenAgentRadio` 是 Harbor 0.6.4 的真实适配器，继承上游 Coral 启动、四 Agent
  会话、任务容器和答案收集逻辑。原脚本生成的五阶段提示、通信脚本及恢复提示均保留。
- 每个 peer 启动真实 Native。Bash 后台任务完成后把原 watcher 输出送到接收侧路由；
  Agent 仍使用原 Coral 服务通信。这个实验测的是 Native 在原 Coral 通道上的执行策略，
  不是把 Coral 偷换成 Jiuwen DB；Jiuwen DB 链路另由运行时集成测试覆盖。
- L2/L3 使用上游原适配器；`steer` 使用原 Native；`abort_restart` 丢弃执行上下文，
  保留输入重建 Native；`model` 使用安全 checkpoint 恢复。工具副作用不会随重建撤销。
- 官方 `verify_local.py` 对每个 trial 评分。模型名、提供方和原基线的认证/代理需要配置一致；
  上游 Claude Code 基线不能直接接任意 OpenAI 服务，需要其支持的代理适配。

### InterruptBench

- 原 `run.py` 在内存中只增加一个官方更新回调；网页 reset、轨迹 replay、提示构造、
  动作解析、环境执行及 `evaluator_router` 都使用上游代码，不修改上游文件。
- WebArena 依赖较旧，与 Native SDK 冲突，使用两个 Python 环境。旧环境拥有网页；
  新环境通过独立进程执行 Native。stdin/stdout 只承载内部协议，模型日志走独立文件。
- 模型收到的 chat messages 由原 prompt constructor 生成；模型返回文本交回原动作解析器。
  Native 的内部工具及额外提示不进入浏览器模型请求。
- 官方模式保持原 step 边界更新；并发扩展在同一 K-action 边界发起旧模型调用并异步投递
  官方更新，期间不执行任何新网页动作。报告明确标记该注入条件，不冒充原版运行时。
- 全量 abort 基线取消并重建模型执行上下文，但保留上游权威网页轨迹和已提交页面状态；
  它不能撤销网页副作用，也不额外 reset 页面后重新执行旧动作。
- 实验先保存一份官方 baseline，Stage 1 各策略共享该轨迹；后续阶段逐策略调用原
  `make_multi_interrupt_stage.py`，沿各自上一阶段轨迹生成更新，历史不被清空。
- 成对网页实验强制要求 `--reset_server_url` 和 `--reset_before_each_task`，避免状态污染。

## 环境

Harbor 固定 `harbor[modal]==0.6.4`、`modal==1.4.2`，需 Docker 或已认证的 Modal。
将 Jiuwen 与 AgentRadio 仓库根目录放进 Harbor 环境的 `PYTHONPATH`。容器适配器会
安装锁定的 Native SDK，并上传当前软件包；只打包 tracked 文件和明确的适配代码。

WebArena 使用独立 Python 3.11，安装上游 requirements；此外，上游代码导入但未列在
requirements 中的 `lxml`、`dashscope`、`anthropic` 也需安装，并受原依赖版本约束：

```powershell
uv pip install --python D:/eval/Scripts/python.exe -r D:/repos/InterruptBench/Eval/requirements.txt
uv pip install --python D:/eval/Scripts/python.exe -c D:/repos/InterruptBench/Eval/requirements.txt lxml dashscope anthropic
D:/eval/Scripts/python.exe -m playwright install chromium
```

Native Python 使用仓库锁定的 agent-core `94e10cb`。环境文件填写上游要求的站点、
模型和评分器变量；真实 WebArena 网站镜像及 reset 服务仍须按上游部署说明启动。

## 验证记录

2026-09-08：8 项上游输入契约测试、1 项真实 Native U2A 桥接测试通过。
测试读取外部官方数据；模型响应和 baseline 轨迹形状为测试夹具。
这证明输入与投递接线，**不代表 WebArena 任务完成或公开评测跑分**。

```powershell
$env:JIUWEN_AGENTRADIO_ROOT='D:/jiusi_agent/_repos/AgentRadio'
$env:JIUWEN_INTERRUPT_BENCH_ROOT='D:/jiusi_agent/_repos/InterruptBench'
python -m pytest tests/unit_tests/agentserver/test_duplex_public_benchmark.py tests/integration_tests/test_duplex_e2e.py -k 'public_benchmark or official_interruptbench' -q -o addopts=''
```

后续完整回归 132 项通过，另新增 2 项测试单独通过，共 134 项。包括实际 Native 进程、后台 Bash 投递、取消恢复和从头重跑
副作用对照；原 AgentRadio 启动脚本真正执行并生成角色提示，官方三阶段生成器验证
了更新顺序、历史和 K 边界。新增测试覆盖成对报告统计，以及真实 Python 3.11 上游
PromptAgent → Python 3.12 Native → 原动作解析器；后者使用受控模型响应。
跨环境启动会隔离父进程的 PYTHONHOME 等解释器变量，避免标准库版本混用。

本机已成功启动原 Coral 服务（API 返回 200），并在独立依赖环境中加载原
InterruptBench runner 的命令入口。Harbor CLI 和上游 Chromium 已安装验证。
以上都不是公开任务成绩。

报告将缺失评分留在分母内，基于末阶段做任务配对和 task bootstrap 置信区间。
记录流式慢模型调用、取消、快模型决策次数、消息接受时间和可取得的 usage。
消息接受时间不冒充首次正确动作时间；缺失的全量 token、GPU 时间及外部副作用
统计保持 null，不推断成零。

已用真实 SiliconFlow 服务验证 DeepSeek-V3.2 慢模型和 Qwen3.5-9B 快路由。
Windows Native 在慢模型流式执行期间接收了官方首条更新，快路由在约 1.80 秒内
返回 APPEND，后续慢模型调用完成。该检查没有浏览器工具，只验证真实服务接线。

模型认证现已具备。服务器部署目录为 `/workspace/wzr/duplex-benchmark-20260908`，
使用独立虚拟环境；密钥置于仓库外的私有目录。当前服务器没有 Docker/socket，
`user.max_user_namespaces=0`，`unshare -Ur true` 失败，账号不具备免密 sudo。
正式任务仍需可用容器环境和 WebArena 站点，尚无公开任务成功率或性能结论。

同日服务器真实 Native 接线检查完成：快路由达到 2 秒预算后按设计降级为 APPEND，
慢模型继续完成响应，结果保存在上述服务器目录的 `results/live-native/`。
这个结果只验证接线和超时降级，不证明快模型及时决策，也不是官方网页任务成绩。
服务器工作目录位于网络文件系统；运行时将 `PYTHONPYCACHEPREFIX` 和
`XDG_CACHE_HOME` 指向本机临时目录，避免导入时大量写网络缓存。
服务器针对路由、官方输入、实验统计及 Native 接线的回归结果为 62 通过、2 跳过；
跳过项是尚未安装 Harbor 的原启动脚本测试和缺少独立 WebArena 环境的原动作解析测试。

## 评审后的接线修正

本节记录第一次接线修正；最新的 U2A 生产入口、网页动作提交和安全全量打断修复见
[第二轮修复](duplex-review-fixes.md)。

AgentRadio 保留原 Coral 协议、题目和评分；收到的消息现在先写入 Jiuwen DB，
由原 EventBus/Poll、未读 drain、Native 控制器和原 DAO ACK 处理。适配器注册真实
Jiuwen 成员，避免消息已经投递但因缺少成员记录无法确认已读。Coral 是外部通信协议，
Jiuwen DB 是本地收件账本，不能将其解释为替换了 Coral 的整个协作系统。

InterruptBench 同题跨动作复用一个 Native，任务 reset 或失败时才关闭；原 PromptAgent
仍负责 prompt 和动作解析，原 WebArena 环境负责 `env.step`。网页动作尚未纳入 Native
工具账本，当前适配不能证明网页外部副作用与 Native 检查点统一提交。

AgentRadio 成对报告默认对比原 SDK steer，可显式设置 `comparison_baseline`。
`always_interrupt`（安全检查点恢复）和 `abort_restart`（重建并从头运行）是不同对照，
不得混报。上述机制修正不产生官方分数，正式运行仍缺可用容器和 WebArena 站点。

本轮 Windows 完整回归 140 项通过（2026-09-08）：包含真实 SQLite 连续两条消息
取消旧判断并合并重判、原 DAO ACK 和重复消息去重、EventBus 事件不被输入分类阻塞、
真实工具执行收据落盘，以及同题持久 Native 和原 Python 3.11 浏览器动作解析器。
模型响应为受控测试端点；结果不是公开任务分数。Ruff 检查通过。

服务器本轮回归为 68 通过、2 跳过（仍缺 Harbor 和独立 WebArena 环境）。真实模型
独立启动进一步发现适配器未绑定 SDK 消息表会话；补上入口会话隔离后，本机和服务器
各自的独立 DB 专项测试均通过。服务器代码版本为 `b7e952cb`。

修复后的真实 SiliconFlow + Jiuwen DB 检查完成：原首条更新入库并由原 DAO 确认已读，
快路由约 2010 ms 超时降级 APPEND，慢模型后续采纳更新并完成响应。全链约 92 秒；
这仍是无网页工具的接线验证，不能作为任务成功率或延迟收益。结果位于服务器
`results/live-db-rework/result.json`、`events.jsonl`，相对于上述部署目录。
