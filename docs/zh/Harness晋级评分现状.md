# Harness 优化：分数计算与晋级流程现状

> 范围：2026-10-23 当前本地工作树，JiuwenSwarm Web HARNESS 场景调用的 `SingleHarnessIterativeOptimizationOrchestrator`。本文整理现状，不代表已经采用此前讨论的“整包逐次晋级”方案。
>
> 最近实验对照：`rsi-ebcd3005` / harness 07，5 个 case、3 个 Epoch、24 次候选尝试。

## 1. 系统中有哪些“分数”

| 名称 | 来源 | 取值/算法 | 当前用途 |
| --- | --- | --- | --- |
| Judge 单项分 | LLM 输出各 behavior.score | 0..1，逐项评分 | 组合连续总分，诊断缺口 |
| Judge 连续总分 | 程序使用可信 rubric 权重汇总 | 加权平均后应用扣分 | 保存在 assessment 与 optimization_signals；不直接作为正式晋级总分 |
| 单题 case score | LLM Judge 的通过阈值转换 | 连续总分 >= 0.8 时为 1，否则 0 | 局部比较、全量总分、晋级 |
| 候选 candidate_score | 本候选负责的目标题 | 目标 case score 的平均值 | 局部 Gate；不是整个数据集的成绩 |
| 全量分 | 当前完整 Harness 的全量评估 | 全部数值 case score 的算术平均 | 全局非回退检查与 Best 更新 |
| 逐要求结果 | requirement_results | 各要求 passed/score/evidence | 局部修复、部分进展识别 |
| 步数、tokens、耗时 | 运行/usage 记录 | 原生统计 | 当前不加入正式晋级分数 |

这里只描述 LLM Judge 的二值化。其他 backend/judger 可以返回自己的严格评分，不能据此认定所有评估方法的 case score 天然都只有 0/1。

## 2. LLM Judge 怎样算单题分数

### 2.1 连续分数

设 rubric 正向项为 w_j，Judge 给出的满足度为 x_j：

```text
base_score = sum(w_j * x_j) / sum(w_j)
```

权重和扣分额度由调用方的 reference/rubric 决定。程序覆盖 Judge 响应中的同名权重，不让模型自行改变它们。

两种扣分方式：

```text
subtract：score = max(0, base_score - sum(已触发扣分))
ceiling： score = min(base_score, 1 - max(已触发扣分))
```

带百分比的旧格式 judge_rubrics 经适配通常采用 subtract；原生 reference 配置未显式指定时可采用 ceiling。没有触发扣分则保持 base_score。

如果整份评分细则只有一个自然语言大项，程序只能拿到该大项的最终 score；里面的子项得分、重复扣分等仍可能藏在模型的 reason 中，程序没有逐子项数字自动对账。

### 2.2 转为通过分

当前 Swarm 生成的 LLM Judge profile 显式设置 `judge_success_score=0.8`：

```text
passed = continuous_score >= 0.8
case_score = float(passed)
```

所以：

| 连续分变化 | case score 变化 | 严格计分能否看到变化 |
| --- | --- | --- |
| 0.20 → 0.79 | 0 → 0 | 看不到 |
| 0.79 → 0.80 | 0 → 1 | 能看到完整一次通过提升 |
| 0.95 → 0.81 | 1 → 1 | 看不到质量下降 |

连续分保存在 `evaluation.metadata.parsed.overall_score` 与 `optimization_signals.continuous_score`，代码明确标注其角色为诊断/搜索信号。当前局部主要通过条件仍看严格 case score。

另外，单个 requirement 的 `passed` 定义为该项 score 恰等于 1.0，与“整题总分 >= 0.8 即通过”不是同一个阈值。整题通过时，一些单项仍会显示未完全通过。

## 3. 怎样算候选分与全量分

### 3.1 候选局部分

候选只重跑它负责的 target_case_ids：

```text
source_target_score    = mean(修改前 Source 的目标题 case score)
candidate_target_score = mean(修改后 Candidate 的目标题 case score)
target_score_delta     = candidate_target_score - source_target_score
```

Gate 中的 `source_score`、`candidate_score`、`score_delta` 在后续会采用这些目标局部值。不能拿它们直接与 5 题全量的 baseline_score/best_score 比较。

Source 可以复用匹配的历史评估；Candidate 重新运行 Solver 和 Judge。因此配对是同任务/同协议的结果对照，不保证两侧同时采样或同样随机性。

### 3.2 全量分

`_eval_score()` 从 eval_ref.yaml 的 cases[].score 重新求平均。`summary.json.average_score` 由 MetricsCollector 单独聚合，来源都是 case score。

对于 5 道使用 LLM Judge 的题，正常完整结果下：

```text
full_score = 通过题数 / 5
```

因此只有 0、0.2、0.4、0.6、0.8、1.0 这几个全量档位。题目之间没有另外配置权重，rubric 内部的权重只影响该题的连续分。

平均函数本身只平均有数值的 score；`_eval_score()` 在全部无数值时返回 0，MetricsCollector 在同样情况下返回 None。完整覆盖和异常可比性依赖外围流程，不能把这两个平均函数理解成已经统一处理了缺测/error 的评分协议。

### 3.3 部分进展

逐要求的新增通过、带证据的分数提升，可以触发局部部分进展。它要求仍有剩余失败项，且没有要求通过状态或要求分数回退。

满足额外证据/覆盖约束时，未通过的候选可作为同一 Batch 的下一次修复起点。它不更新全局 Best，也不会因为连续分提高就自动晋级。

## 4. 当前完整流程

```text
冻结 H0，跑全部题得到基线
  ↓
每个 Epoch 从上一 Best 开始
  ↓
按 Batch 筛出当前未通过题，读取/复用 Source
  ↓
诊断 → 假设 → 规划 → 修改 → 工程校验/Repair
  ↓
运行候选负责的目标题，进行局部 Gate
  ↓ 局部通过
暂存为 provisional，更新本轮工作 Harness
  ↓
再次评估当前 Batch（residual_source），更新剩余问题
  ↓
后续 Batch 在工作 Harness 上继续累积修改
  ↓
Epoch 末对累计完整包跑全量 full
  ↓
按各候选的目标题表现和能力使用记录筛选修改
  ↓ 部分保留、部分撤回
从 Epoch 起点重组被保留的修改，再跑全量 selected_full
  ↓
全局晋级判断：更新 Best，或整轮回到 Epoch 起点
  ↓
最终发布程序复制 Best 到稳定发布位置
```

不存在每个局部候选通过后就立即成为全局 Best 的规则。当前仍是 Epoch 级正式晋级。

## 5. 局部接纳有哪些门槛

并非“候选分高于 Source 就接纳”。主要条件如下：

1. Source 没有被识别出的 error case。
2. MemberOptimizer 状态为 success 或 partial_success，且实际产生了不同的候选引用。
3. 本候选负责的原失败目标都要满足 `candidate_case_score > source_case_score + candidate_min_target_behavior_delta`，并且目标集合无严格分回退；默认增量参数为 0。
4. 没有候选评估错误/目标 skip，或相应证据无法判断的问题。
5. 需要调用的新 Tool/Skill 有相关目标上的使用证据，且满足声明的作用时机。
6. 没有机器验证失败或不能判明的调用证据。
7. Behavior Check 的后续动作都为 verified。

LLM Judge 的严格分是二值，因此默认情况下，第 3 项主要体现为原失败目标题从 0 变成 1。0.3→0.7 这种连续改善仍不能满足它。

Behavior Check 另外调用模型比较 Source/Candidate 实际行为、引用证据。即使题目已经得分通过，它仍可能以行为未确认/unknown 挡住接纳。

局部接纳后的状态是 provisional。目标之外题目的回归，主要推迟到 Epoch 全量，不在每个局部候选上重跑完整数据集。

## 6. 为什么会重复跑题

| 评估 | 范围 | 是否使用新包 |
| --- | --- | --- |
| 目标局部评估 | 当前候选目标题 | 候选包 |
| residual_source | 当前整个 Batch | 刚才暂存的工作包，可能与目标评估完全相同 |
| full | 全部题 | Epoch 累计包 |
| selected_full | 全部题 | 筛选重组后的包 |

batch_size=1 时，目标评估后 residual_source 又运行同一题，可能是相同包下的立即重复。重组后的包有时也与已有某个历史候选逐字节相同，但当前路径没有统一跨目录复用这份结果，仍可能重跑 Solver。

这些评估都是重新执行题目，不只是拿冻结答案重新调用 Judge。因此一次分数变化可能同时来自 Solver 产物变化和 Judge 判定变化。

## 7. Epoch 怎样筛选候选

`_select_gate_from_epoch_checkpoint()` 对每个 provisional 候选独立判断：

- 全量中它负责的目标题是否全部通过。
- 有无目标 error/机器证据无法判断。
- 若是 Tool/Skill 修改，是否仍有使用记录；Prompt 不要求独立调用记录，Rail/Config 不按工具调用名检验。

它并不只看整个包比起点多了多少分。

随后：

| 筛选结果 | 程序行为 |
| --- | --- |
| 全部保留 | 继续用本次累计包进行全局判断 |
| 部分保留、部分移除 | 从本轮起点重新组合保留动作，进行一次 selected_full |
| 没有保留 | 本轮不晋级，回到本轮起点 |
| selected_full 中任一保留候选失效 | 拒绝全部本轮 provisional 修改，不继续枚举其他组合 |

重组主要按动作归属和文件复制完成，不具有完整的语义依赖回滚保证。同文件上的后续 modify 与前面的 add 可能互相依赖，不能把每个候选理解成可任意拆卸的独立补丁。

## 8. 全局晋级究竟检查什么

只有仍有 retained_gates 才进入全局晋级比较。条件包括：

1. 最终选中包的全量分不低于上一 Best，容差为 `1e-9`。
2. 上一 Best 已经通过的题继续通过。
3. 额外保护题继续通过。
4. 最终选中评估没有 error 或失败的机器证据。

注意：第一条是“非下降”，不是严格“提高才晋级”；分数相等时，若其他条件满足，代码仍允许替换 Best。

### 8.1 额外保护题怎样产生

设：

```text
P0 = Epoch 开始时的 retained_case_ids（上一 Best 成功集合）
W  = 本轮工作集合，随 Source/residual 的最新评估更新通过情况
T  = 本轮所有 provisional 候选负责的目标题并集
保护集合 = P0 ∪ (W - T)
```

W 不是只包含上一 Best 的成功，它可以包含本轮工作版本临时通过的新题。这就是基线 0 分也会出现 `protected_case_regressed` 的原因。

W 会在相关 case 再次被评估时更新，并不是永久保留所有历史成功；但检查点当时仍在 W 中的非目标题会成为硬保护。

### 8.2 晋级和回退更新什么

晋级：更新 best_score、best_eval_ref_path、best_harness_refs_path、retained_case_ids。下一 Epoch 从这个 Best 开始。

不晋级：恢复本轮开始时的 Best、分数和 retained 集合。不会在这些已拒绝的局部候选中再自动挑一个最高分版本当 Best。

## 9. 状态与字段的含义

| 字段/状态 | 含义 |
| --- | --- |
| MemberOptimizer success | 候选生成与相应工程流程成功，不等于题目通过 |
| Gate provisional | 目标局部接纳，尚未整轮确认 |
| Gate accepted | 候选的效果通过了 Epoch 级候选保留检查，不必然代表整体已晋级 |
| `epoch_checkpoints[].promotion_applied` | 整个版本是否正式晋级 |
| `best_score` | 正式 Best 的全量分 |
| checkpoint.score | 该检查点记录的评估分；失败重组时可能仍是前一个 full 的分，不是当前 Best 分 |
| `publication_status` | 最终发布程序的状态，不等同于局部接纳或 Best 晋级 |

前端 Epoch 节点以 promotion_applied 判断 adopted；它也会核对 selected 与 evaluated 的引用，包被筛选/回退且没有匹配评估时不展示原分数。不能只看内部 checkpoint.score 就认定前端应显示该分数或该包已晋级。

### 9.1 已确认的发布状态漏洞

当前 `_ensure_final_publication()` 只要看到存在 accepted Gate，就可能复制 Best 并标记 published；它没有要求对应 Epoch 确实 promotion_applied。

与此同时 `_persist_promotion()` 可根据 Gate accepted 写出 promoted_roles/promotion_status。这些局部命名可能比实际的全局状态更乐观。

因此存在“Gate accepted，但整轮没晋级，最后发布初始 Best”的实际情况。需要把局部保留、正式晋级、发布拆成明确状态，现状尚未统一。

## 10. harness 07 的实际对照

| Epoch | full 分 | 最终晋级 | 原因 |
| --- | --- | --- | --- |
| 1 | 0.6 | 否 | 保护集合加入本轮临时通过的缓存题，回归失败，protected_case_regressed |
| 2 | 0.2 | 否 | 候选负责的缓存目标题复测失败，无候选最终保留 |
| 3 | 0.8 | 否 | 一个目标题失败，重组后另一个保留目标也失败，filtered_harness_failed_replay |

基线和最终 best_score 都是 0。publication_status 却为 published，发布引用指回初始 Harness，稳定发布目录只有原始 harness_config.yaml。

第 3 轮原累计包 full=0.8，重组后的 selected_full=0.4；内部 checkpoint.score 仍记录原 full 的 0.8。不要把它误解为“已验证的新 Best 是 0.8”。

## 11. 配置项是否实际决定晋级

| 配置 | 当前单 Harness 主路径中的实际作用 |
| --- | --- |
| judge_success_score | LLM 连续分到二值通过分的阈值，Swarm profile 为 0.8 |
| candidate_min_target_behavior_delta | 局部原失败目标题的严格分增量下限，默认 0 |
| candidate_min_score_delta | 有配置字段；当前该主路径没有读取它作为正式晋级下限 |
| candidate_non_target_max_regression | 有字段；当前该主路径没有据它实现可容忍非目标回退 |
| candidate_holdout_cases | 单 Harness 构造时强制为 0，没有引擎内独立 holdout |
| candidate_holdout_max_regression | 当前该主路径未读取来执行独立 holdout 门槛 |
| full_evaluation_enabled | 该编排器要求启用 Epoch 全量检查点 |
| promotion_policy | profile 写 epoch_full_evaluation，符合当前主路径流程；不是逐候选立即晋级 |

不能只新增一个 YAML 数值就认为门槛已改变；要对应修改实际分支。

## 12. 当前需要明确的主要矛盾

1. 连续质量变化、二值通过率、目标局部分、全量分都叫 score，容易误读。
2. 正式分只看阈值后的通过率，阈值以下的改进和阈值以上的退步不直接体现。
3. 总分只是多个门槛之一；目标题效果、行为判断和临时保护题都可阻止更高总分晋级。
4. 同一包/同一题可能被多次重新执行，稳定性测量与普通接受流程混在一起。
5. 先累积、后按动作筛选，带来同文件依赖与新包重评的复杂性。
6. 相等分是否应替换、旧题回退可否容忍、error/缺测怎样入分，目前不是单一清晰协议。
7. accepted/promoted/promotion_applied/published 不是同一状态，且已有相互不一致的实际结果。
8. 当前优化和全量验证使用同一批题；全量回归不等于独立泛化验证。

## 13. 源码入口

- [Judge 权重与扣分计算](/Users/jiuwen/orca/workspaces/agent-core/release-v0.1.20/openjiuwen/rsi/harness_rsi/evaluator/judger/scoring.py:196)
- [LLM 单题二值化](/Users/jiuwen/orca/workspaces/agent-core/release-v0.1.20/openjiuwen/rsi/harness_rsi/evaluator/judger/llm_as_judge.py:298)
- [评估汇总](/Users/jiuwen/orca/workspaces/agent-core/release-v0.1.20/openjiuwen/rsi/harness_rsi/evaluator/metrics_collector.py:18)
- [局部 Candidate Gate](/Users/jiuwen/orca/workspaces/agent-core/release-v0.1.20/openjiuwen/rsi/harness_rsi/single_harness/iterative.py:1273)
- [Epoch 全量与筛选](/Users/jiuwen/orca/workspaces/agent-core/release-v0.1.20/openjiuwen/rsi/harness_rsi/single_harness/iterative.py:835)
- [全局晋级及额外保护集合](/Users/jiuwen/orca/workspaces/agent-core/release-v0.1.20/openjiuwen/rsi/harness_rsi/single_harness/iterative.py:979)
- [全局非回退函数](/Users/jiuwen/orca/workspaces/agent-core/release-v0.1.20/openjiuwen/rsi/harness_rsi/single_harness/iterative.py:3428)
- [最终发布条件](/Users/jiuwen/orca/workspaces/agent-core/release-v0.1.20/openjiuwen/rsi/harness_rsi/single_harness/iterative.py:4112)
- [前端 Epoch 节点投影](/Users/jiuwen/orca/workspaces/agent-core/release-v0.1.20/openjiuwen/rsi/harness_rsi/single_harness/events_translate.py:203)
- [Swarm 任务 profile 物化](/Users/jiuwen/orca/workspaces/jiuwenswarm/0.2.8-2/jiuwenswarm/agents/harness/common/rsi/materializer.py:293)
