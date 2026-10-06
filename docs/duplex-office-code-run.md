# 办公 / 开发两组对照

当前状态（2026-09-16）：已在指定服务器的现有环境跑完四次真实模型试验。没有创建新的虚拟机。办公两组产物均通过检查但超时；开发普通组未修复、全双工组的产物通过原题测试，两组均耗尽调用预算。两次全双工试验都没有实际打断，因此不能把开发成绩差异归因于打断机制，也不能据此声称速度提升。

## 本次实测结果

| 测试题 | A2A | 实际运行秒数 | 产物质量 | 停止原因 | 实际打断 |
| --- | --- | ---: | --- | --- | ---: |
| 办公预算，离线改编 | 普通 | 480.0 | 28/28 规则检查 | 超时 | 0 |
| 办公预算，离线改编 | 全双工 | 480.0 | 28/28 规则检查 | 超时 | 0 |
| Flask 原题 | 普通 | 210.3 | 修复 0/1，回归 59/59 | 实现 Agent 耗尽预算 | 0 |
| Flask 原题 | 全双工 | 250.6 | 修复 1/1，回归 59/59 | 定位 Agent 耗尽预算 | 0 |

这里的秒数是试验实际运行时长，非成功完成任务的耗时。普通办公 / 全双工办公 / 普通开发 / 全双工开发的慢模型调用分别为 61 / 71 / 86 / 86；全双工另有 6 / 2 次路由判断，全部 APPEND。只有每场景一题、每策略一次，不作统计显著性或性能提升结论。

办公两个产物的部门总价均为 Marketing 10,000、Product 10,110、HR 6,780，回复草稿金额与结论正确。全双工草稿用“恰好等于预算”表达 Marketing 的零余额，没有单列 0.00。团队在产物完成后仍有重复核算、脚本转义错误和额外消息处理，最终达到超时上限。

开发普通组一度修复了问题，随后修改导致语法错误，最后恢复备份；预算耗尽时源码与基线相同。全双工保留了有效修复，并新增回归测试，但定位 Agent 预算耗尽后评测器停止全队，审查流程没有完成。额外行为检查发现该补丁还拒绝纯空格名称，这是原题未要求的约束；原题评分测试并不覆盖这点。这是单次产物差异，不能归因于全双工机制。

两组开发都由独立评分端应用原测试补丁。完整 485 项 pytest：普通 481 通过 / 2 失败 / 2 跳过，全双工 482 通过 / 1 失败 / 2 跳过。其中 `test_max_cookie_size` 在未改源码上也因 Python 3.12 警告计数失败；它不属于本题 59 个 PASS_TO_PASS。目标修复测试在基线和普通组失败、全双工通过。未把基线环境问题算成新增回归。

结果与逐调用日志已保存：

- 本地：`D:\jiusi_agent\_deps\duplex-direct-run-20260916\direct-runs-v1\report.md`，同目录有 `summary.json`、全部工作副本、事件日志、补丁与测试报告。
- 服务器：`/workspace/wzr/duplex-benchmark-20260908/office-code-20260915/duplex-direct-run-20260916.tar.gz`，完整归档约 6.8 MB。
- 归档 SHA256：`ae308a325fc411bc04a303577fa9662690fbc2977c8cf60f40d81532998ce778`。
- 本轮适配改动的两个三 Agent 接线回归均通过；Ruff 与 Python 编译检查通过。此前生产打断逻辑的回归见文末。

## 当前直接试跑

- 执行机：`wzr@47.79.124.13:31442`，使用现有环境，不创建嵌套虚拟机。
- 入口：`python -m jiuwenswarm.benchmarks.duplex_direct`。
- 办公题：TheAgentCompany `admin-check-employees-budget-and-reply` 的离线改编，使用冻结 CSV 和采购规则，核对三个部门的最新申请、取消项目、运费、预算以及回复草稿。这不是官方办公数据集成绩。
- 开发题：SWE-bench Verified `pallets__flask-5014` 原始问题，源码固定到 `7ee9ceb71e868944a46e1ff00b506772a53a4f1d`。使用原测试补丁，分别记录 1 个 FAIL_TO_PASS、59 个 PASS_TO_PASS，以及额外的完整 pytest 结果。原评分测试不提供给做题 Agent。
- 三个做题 Agent 均使用 SiliconFlow 的 `deepseek-ai/DeepSeek-V3.2`；全双工路由使用 `Qwen/Qwen3.5-9B`。temperature=0、thinking 关闭；慢模型 max_tokens=2048，快模型 max_tokens=256。
- 每 Agent 最多 30 次慢模型请求（取消也计数），每队最多 90 次 Bash，任务超时 480 秒。普通组为原 SDK `steer`，全双工组为 `model`。题目、角色和预算相同。
- 四次顺序：办公普通、开发全双工、办公全双工、开发普通；每次三个 Agent 并发工作，各试验独立，开发的定位 / 修复 / 审查 Agent 使用独立源码副本。没有人为注入纠正消息或延迟。
- 原生 Python 3.12，pytest 7.4.4、Werkzeug 2.2.3、setuptools 68.2.2；两组均过滤 DeprecationWarning 以兼容旧 Flask 源码。源码按当前工作目录的 `src` 加载。完整依赖见归档的 `manifest.json`。
- 原生运行使用最小工具环境与目录隔离，不构成安全沙箱。模型密钥不传给 Bash；工具提示限制访问题目目录。
- 运行日志：`/tmp/wzr-duplex-office-code-20260916/direct-runs-v1`；持久归档：`/workspace/wzr/duplex-benchmark-20260908/office-code-20260915/direct-runs-v1`。保存题目、角色、逐消息 / 模型 / 工具事件、实际产物、测试 XML、评分报告。

示例（使用已备好的服务器目录）：

```bash
STAGE=/tmp/wzr-duplex-office-code-20260916
BASE=/workspace/wzr/duplex-benchmark-20260908
PYTHONPATH="$STAGE/sdk:$STAGE/app:$BASE/app" "$BASE/native-venv/bin/python" \
  -m jiuwenswarm.benchmarks.duplex_direct \
  --suite development --policy model --timeout 480 \
  --assets "$STAGE/direct-assets" --inputs "$STAGE/inputs/development.jsonl" \
  --models "$BASE/private/models.json" --output "$STAGE/a-new-trial"
```

`--suite office` 跑办公题，`--policy steer` 跑普通组；每次必须使用新的输出目录。

## 改了什么

`DuplexNativeHarness` 在安全暂停后清除结构化 `task_plan`，并替换用于接续的任务指令。原用户需求与已提交工具结果保留，旧计划不再被任务循环自动推进；下一步要求根据纠正信息重新规划。模型阶段取消当前生成，工具阶段仍等待提交。新规划的正确性由模型与实际测试决定。

评测里的三个 Agent 使用独立订阅身份，避免 SDK 进程内消息总线上互相覆盖。发送直接复用 SDK 的 `TeamMessageManager`：持久化并通知后返回，接收方按原 EventBus、DB 与 ACK 流程处理，发送方不等待对方模型。

## 原完整数据集方案（本次未运行）

| 项目 | 固定设置 |
| --- | --- |
| 办公 | TheAgentCompany 原始 6 题；协调、资料处理、复核；原 NPC 与评分器 |
| 开发 | SWE-bench Verified 原始 12 题；定位复现、修复、测试审查 |
| 普通 A2A | 原 SDK `steer`，在下一模型边界接纳队友消息 |
| 全双工 A2A | `model` 路由；APPEND 或安全暂停后作废计划、重新规划 |
| 模型 | 现有私有配置的 DeepSeek-V3.2 / Qwen3.5-9B；两者关闭 thinking |
| 默认预算 | 每 Agent 最多 40 次慢模型流式请求（含取消）；全队 160 次 Bash；任务 1800 秒 |
| 次序 | 同题成对运行，交替哪组先跑；每次使用新任务环境 |

开发只有修复 Agent 的补丁被提交。复核 Agent 用 `ReviewSnapshot` 取得一致补丁副本，返回 SHA256，并在自己的容器运行测试。三个开发容器均无网络，Git 历史被裁成初始源码树；参考补丁、官方测试补丁与评分字段留在控制端。

办公的原 NPC / 评分器运行在官方任务容器。三个 Agent 的工作容器只挂载公共 `/instruction`、`/workspace`；不能看到 `/npc`、`/utils`、评分代码或模型密钥。协调 Agent 可访问原服务，其余两名读取共享材料并在内部沟通。原初始化会重置服务数据，所以必须使用专门的评测服务。工作目录在每次试跑后随容器和卷释放；动作日志、模型文本、补丁和评分报告保留。

记录原评分、执行/初始化/评分耗时、已知用量、消息到达阶段、建议与实际中断次数。完整 token、重复副作用等无法测全的字段为 `null`。缺失评分不会计成零分，配置或镜像不同不会组成有效对照。默认预算是试跑设置，成绩不能直接和上游不同预算的榜单比较。

## 入口

在项目与固定 SDK 可导入的 Python 环境运行：

```powershell
$env:PYTHONPATH='D:\jiusi_agent\_repos\agent-core-duplex;D:\jiusi_agent\wt-jiuwen-duplex-a2a'
D:\jiusi_agent\.venv\Scripts\python.exe -m jiuwenswarm.benchmarks.duplex_workloads plan `
  --inputs D:\jiusi_agent\_deps\duplex-dataset-review\local-inputs.json `
  --output D:\jiusi_agent\_deps\duplex-office-code-run-20260915
```

已生成 36 次执行清单。`run` 会先做环境检查；`check` 只检查，`report` 重建报告。现有结果目录：`D:\jiusi_agent\_deps\duplex-office-code-run-20260915`。

在支持 Docker 的 Linux 主机上，先准备原 TheAgentCompany 服务（[上游安装说明](https://github.com/TheAgentCompany/TheAgentCompany/blob/98b68ef82a47690c316f42fddb05baafaab56851/docs/SETUP.md)）及独立工作镜像：

```bash
docker build -f jiuwenswarm/benchmarks/office_workstation.Dockerfile \
  -t jiuwen-office-workstation:20260915 jiuwenswarm/benchmarks
```

SWE 官方评分器使用单独环境：

```bash
git clone --depth 1 --branch v3.0.17 https://github.com/SWE-bench/SWE-bench.git /path/SWE-bench
uv venv /path/swe-grader-venv
uv pip install --python /path/swe-grader-venv/bin/python -e /path/SWE-bench
```

把已固定的数据文件和 `local-inputs.json` 复制到执行主机，仅调整其中的本地路径。输入文件的哈希必须保持一致。执行：

```bash
python -m jiuwenswarm.benchmarks.duplex_workloads run \
  --inputs /path/local-inputs.json --models /path/private/models.json \
  --output /path/results/office-code \
  --office-host dedicated-benchmark-host \
  --swe-root /path/SWE-bench --swe-python /path/swe-grader-venv/bin/python
```

主执行环境需要 `pyarrow` 读取固定 parquet，可用 `--data-python-path` 指向已有独立安装。也可 `--suite development` 先跑开发。首次 Docker 接线检查用 `--limit 1` 和新输出目录；这是样本缩小的环境试跑，不能当成完整 18 题结果。修改代码、配置、样本后须新建输出目录。

正式执行机固定为 `wzr@47.79.124.13`；现有 SSH 配置端口为 `31442`（别名 `jiusi-vllm-evolve`）。工作目录为 `/workspace/wzr/duplex-benchmark-20260908/office-code-20260915`，SWE 评分器位于其 `grader-venv`。2026-09-16 曾误建 QEMU 嵌套虚拟机，用户纠正后已停止该实例和隧道，未在其中运行正式数据集。后续使用现有服务器环境，不再创建嵌套虚拟机。

## 验证边界

本轮相关回归：54 通过，13 因外部基准环境未配置而跳过；Ruff 的语法、未定义名称、无用导入检查通过。详细测试输出在 `D:\jiusi_agent\_deps\duplex-final-regression.log`。

真实 SQLite、EventBus、Native、模型 HTTP 接口和工具调用已测试，包括生成/工具阶段 Redis 纠错、原路径对照、三 Agent 订阅及双向消息。测试服务器提供脚本化模型回答，这些测试用于检查实现，不能作为数据集成绩。

SWE 在 Linux 下已验证官方包可导入，并用固定的 12 道原题检查 TestSpec / 镜像命名与评分脚本生成。容器内原依赖、办公初始化和 NPC 对话、最终原评分器执行仍待 Docker 环境验证。
