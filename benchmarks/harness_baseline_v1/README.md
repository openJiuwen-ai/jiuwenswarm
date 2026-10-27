# DeepAgent 原生 Harness 定向评测集 v1

这组题针对当前原生 DeepAgent 的执行弱点，测量正确交付、工具反馈处理、独立验证和局部修改保护。题目使用小型本地材料，不要求训练模型、联网、GPU 或从零完成大型数学项目。

它是一个 benchmark-owned 本地 RSI 接入包：沿用原生 DeepAgent、CaseRunner、TeamEvaluator、Analyzer 和 MemberOptimizer，替换该评测集的判分器。没有改动产品判分逻辑。

## 1. 已有证据与尚待确认的部分

本次设计依据：

- harness 07 的多次执行中，模型长时间修主实现/自写检查器，已有代码却缺答案和报告。
- 普通本地 Solver 的 RSIBashTool 默认未启用 pipefail，日志中有 Python Traceback 被管道末端成功掩盖的情况。
- 单 Agent 可靠性 Rail 已有重复工具检测和输出长度治理；它并非完全没有循环控制。但 tool_error 检测在当前单 Agent 装配中被关闭，变化参数的探查和交付缺失仍需定向测量。
- 模型曾错误信任自己写的验证器，也曾为了修检查器而反复改变主实现。
- 文件保护、日志检索和 Schema 反馈是原生已有基础工具可以完成、且能被宿主确定性检查的相邻能力。

**本包尚未执行真实模型基线或 RSI 优化。**生成出来的题是待校准的评测探针，不保证原生基线必然失败。正常对照应较容易通过；某类训练题若基线已经稳定通过，就不应人为增加刁难去制造失败。

## 2. 六类题与验收逻辑

| 类别 | 小任务 | 主要验收 | 可能的 Harness 改进面 |
| --- | --- | --- | --- |
| delivery | 3 行订单统计，同时交 JSON 和简短编号说明；提供可选诊断器 | 精确金额/数量、公开 JSON Schema、编号内结果/方法/状态字段、输入保留 | 交付检查点、进展跟踪、提示/Skill；减少不推进交付的探查 |
| pipeline | 修正一个配置并运行现有生产器；错误路径先打印局部 PASS，且有旧结果 | 正确配置、正确批次/金额/输入摘要、原生接口执行记录、结果交接文件 | Shell 失败识别、pipefail、输出新鲜度验证、工具错误反馈 |
| oracle | 正确主实现与一个错误四舍五入检查器冲突 | 只读主实现保持、检查器接受正确实现且拒绝两个错误变体、结果正确 | 独立验证工具/Skill、最小案例对照、检查器自身的负控 |
| regression | 修复字符串归一化，保留另一函数与用户笔记 | 公开语义、Unicode/None 边界、旧函数行为、只读文件不变 | 项目约束发现、受限编辑、修改后回归、变更范围保护 |
| schema | 本地接口返回字段级验证错误，修正配置拿到接收回执 | 精确字段和值/类型、接收状态与配置摘要、原生接口执行记录、只读接口不变 | 工具反馈驱动修正、结构化输出校验、有限重试 |
| logs | 在多 job、多次运行的日志中定位 r2 的真实根因 | 正确 job/run、致因而非汇总症状、版本字段、日志不改 | 有界证据搜索、运行身份区分、日志上下文恢复 |

目标是比较可观察结果，不规定必须调用哪个工具、采用哪种架构或写多少代码。Skill/Rail/Tool/Prompt 都是可选的干预手段，只有真实改善验收结果才有价值。

### 正常对照

每类有一个 controls case：配置/主逻辑已经正确，或目标日志已经成功。它检查优化后是否仍能正常交付，不会无端改正确文件、制造根因、跳过产物或强行结束。

### 难度和规模

计算只涉及整数、简单函数或日志取证。复杂度故意远小于旧的算法+证明+18任务+恢复+故障注入大题。日志输入较长但目标规则简单，允许任意等价检索方式。题目可能依然被模型一次性做对，这应作为基线校准结果保留。

## 3. 数据划分

- `train.json`：12 题，每类两个数据变体，供诊断和优化。
- `holdout.json`：6 题，每类一个未用于优化的变体，包含不同数据、Unicode/命名变化。
- `controls.json`：6 题，每类一个正常对照。
- `fixtures/`：全部显式声明的公开附件；复制到 Solver 工作区后才运行。
- `baseline/`：不附加能力的原生基线包。仍保留宿主原生工具与 Rail。
- `baseline_refs.yaml`：角色到该基线的相对引用，本地入口会物化为绝对路径。

holdout 的 JSON 可能与 train 在同一个源目录；入口与编排器只传入明确的 `train.json`，不会扫描同目录其他 split。优化结束后单独评估 holdout/controls，结果不会回灌本次优化。

oracle 的两个训练题分别提供四舍五入和向上取整的错误期望，holdout 则提供错误最低价限制，输入样例也不同，不只是换目录名复用同一个检查器错误。

这只是第一版小规模划分，不能据此宣称广泛泛化。调参时不要反复查看 holdout 再继续针对它优化；需要继续开发时应补新的验证题。

## 4. 评分与原生接入

`grader.py` 使用宿主代码检查文件、输入摘要、函数行为、Schema 和负控；不调用模型判分。主指标为每 case 0/1，通过需满足所有题面公开要求；同时输出逐要求结果及部分完成率，供 Analyzer/后续修复使用。

对应关系：

- 判分方法：`harness_baseline_v1`。
- `evaluation.metadata.requirement_results`：原生标准逐要求结构。
- `optimization_signals.continuous_score`：确定性部分完成率。
- `<case>/verifier/acceptance.json`：原始验收记录。
- `<case>/result.json`：原生 case 汇总。

`adapter.py` 确保串行和并发构造的 CaseRunner 都使用本验收器。模型和步数策略从指定 profile 借用；评分固定走本包，不会悄悄回退到 LLM Judge。

每次运行会把数据与公开附件冻结到 `<output>/input/`。优化状态位于 `<output>/run/`，短候选目录位于 `<output>/mh/`，不会让多个 `/tmp/...` 实验共用 `/tmp/mh`。独立验证结果位于 `<output>/post_holdout/` 和 `<output>/post_controls/`。

pipeline/schema 还要求在原生工具轨迹中看到提供的 Python CLI 入口成功执行，并出现相应完成标记或本次配置摘要。仅手写正确文件而没有接口调用证据不能通过。若角色轨迹缺失/不完整、又没有正向执行证据，则报告评估基础设施证据不完整，不把“没记录到”直接判成“没执行”。这不是主动对抗伪造 Shell 输出的安全基准。

输入身份包含公开附件 SHA-256 和生成器/判分器/接入代码摘要。修改验收实现后，需要重新生成数据并新建评估目录，不能继续复用旧分数。

### 当前前端限制

**不能仅把 train.json 导入现有前端并选择 script_based，就得到本包的确定性评分。**现有 script_based 主要处理后端评分或回答比较，没有通用的任意脚本执行入口。本包通过下述本地入口注入 benchmark-owned Judger。前端接入是单独的适配工作，当前没有修改服务去实现它。

题目、公开附件与原生数据加载器兼容，不等于现有前端已经支持本包的验收协议。若在前端改用 LLM Judge，得到的是另一种评估协议，不能与本包分数混用。

### 子进程边界

只有 oracle/regression 的候选小程序检查需要子进程；使用独立进程、5 秒时限和去掉模型凭据的最小环境。它不是恶意代码安全沙箱。本集用于正常 Agent 工程行为评估，不用于让主动攻击宿主的代码逃逸对抗；真实对抗测试应另用容器/沙箱。

## 5. 不调用模型的检查

在 JiuwenSwarm 仓库根目录执行：

```bash
.venv/bin/python benchmarks/harness_baseline_v1/build_cases.py
.venv/bin/python benchmarks/harness_baseline_v1/run_suite.py validate --split train
.venv/bin/python benchmarks/harness_baseline_v1/run_suite.py validate --split holdout
.venv/bin/python benchmarks/harness_baseline_v1/run_suite.py validate --split controls
.venv/bin/python benchmarks/harness_baseline_v1/selfcheck.py
```

selfcheck 对 24 题运行空交付拒绝、已知正确产物通过、伪造 PASS、重复 JSON 字段、只读材料篡改，以及检查器负控、坏回执、旧输出、回归破坏等检查。还用假执行后端通过真实 TeamEvaluator/CaseRunner 验证串行/并发判分接线。假后端不会创建 DeepAgent 或请求模型，不能用它的通过率冒充基线成绩。

重建会更新本包生成的 JSON/fixtures；不要把 Solver 工作区放进 fixtures，也不要在这里保存手工实验产物。

## 6. 真实模型评估与优化入口

以下命令会调用 profile 中的模型并产生费用，本次构建未执行它们。`--output` 必须是新的目录。

profile 可使用已有 RSI 任务的 `config/harness_orchestrator.yaml`，其中模型路径应指向仍有效的本地模型配置。不要把模型配置或密钥复制进本包。

```bash
# 原生基线校准：先看各类是否已有稳定通过，再决定优化目标
.venv/bin/python benchmarks/harness_baseline_v1/run_suite.py evaluate \
  --split train \
  --profile /Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/config/harness_orchestrator.yaml \
  --output /tmp/hb1-baseline-train-001

# 正常对照也应测基线，避免把任务/验收器缺陷当成模型能力缺口
.venv/bin/python benchmarks/harness_baseline_v1/run_suite.py evaluate \
  --split controls \
  --profile /Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/config/harness_orchestrator.yaml \
  --output /tmp/hb1-baseline-controls-001

# 原生 RSI 优化：只用 train，结束后单独评估 holdout 与 controls
.venv/bin/python benchmarks/harness_baseline_v1/run_suite.py optimize \
  --profile /Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/config/harness_orchestrator.yaml \
  --epochs 1 \
  --output /tmp/hb1-optimize-001
```

默认使用本包的空能力基线。若要精确使用某次实验的起点，追加 `--harness-refs <该任务的初始 harness_refs.yaml>`。若要评估已有候选，传候选引用，使用 evaluate 模式。

执行仍受当前 agent-core 的发布/Repair/晋级逻辑影响，本包没有顺带修复这些已知问题。解读时以实际 Best、判分记录和发布文件内容为准。

## 7. 怎样判断改善成立

1. 先在未修改基线上校准：训练题失败是否可复现，正常对照能否正常通过。
2. 每类分别看通过率和失败检查，不用一个总分掩盖某类回退。
3. 比较同一冻结模型配置、步数预算与相同评估协议下的源/候选。
4. 正确产物的得分不因工具调用多而直接扣除；步骤、调用数、重复探查和 token 作为效率指标另看原生记录。
5. 有意测稳定性时，使用预先约定的重复次数和新目录；不要把某次偶然成功缓存当成稳定性结论，也不要“跑到失败一次”为止。
6. 最后看独立 holdout 和 controls；若这些变差，训练分上涨不足以证明通用 Harness 改善。

完整语义检查是有限的：JSON Schema/整数/函数行为等能确定验证，summary.md 检查题面公开的编号和结果、方法、状态字段，不声称自动证明任意自然语言论证正确。
