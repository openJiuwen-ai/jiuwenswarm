# harness 07 候选 Harness 修改清单与实现逻辑

> 对象：`rsi-ebcd3005`（harness 07）。依据本次实验的实际候选文件、计划、执行结果和 Gate 状态整理，核对时间为 2026-10-23。
>
> 这里的“候选生成成功”指优化器形成了包，不表示一定能加载运行或最后晋升。最终 Best 仍为初始 H0；发布目录也实际只有初始 `harness_config.yaml`。

## 1. 总体情况

24 次候选尝试包含 25 个修改动作，其中 Prompt 12 个、Rail 7 个、Skill 6 个。21 次尝试形成候选，3 次失败；落成候选中的动作合计为 Prompt 10 个、Rail 7 个、Skill 5 个。

本次没有新增 Tool，没有修改模型配置，也没有创建 `runtime.yaml` 调整 300 步预算。多数改动针对“漏写答案/长期调试不交付”，其次针对独立验证和形式化语义转写。

| 方向 | 候选编号 | 主要意图 |
| --- | --- | --- |
| CUDA 审查覆盖与定量分析 | 1、2、10、20、21 | 补硬件机制，定量推导瓶颈，按请求检查答案覆盖 |
| 交付调度与循环控制 | 3、7、9、11、13、14、15、16、22、23、24 | 提前写答案/固定产物，约束自写代码和检查器的反复调试 |
| 独立验证与分歧裁决 | 4、5、6、8、12 | 从规格建立 Oracle，用不变量和第三条路径确定谁错 |
| 转移系统条件审计 | 17、18、19 | 防漏/多加条件，核查派生谓词，处理解释冲突 |

编号代表一次候选尝试，不能据此认为这些能力全部装进了一个最终包。比如 #3 和 #11 的 Rail 同名，但来自不同尝试、内容不同。

## 2. 三种修改方式如何生效

按这 5 道题归类：

| 题目 | 候选编号 | 修改主线 |
| --- | --- | --- |
| CUDA W8A8 审查题 T068 | 1、2、10、20、21 | 硬件机制覆盖、Roofline/瓶颈定量推导、成稿自查 |
| 精确推测解码规划题 T083 | 3、4、5、11、12、13、22、23 | 独立 Oracle、分歧裁决、预留正文交付、代码与文档同步 |
| 规划与证书批次题 T083 v2 | 6、7、8、14、15、24 | 避免困在自写检查器里，先交固定产物和编号正文 |
| Epoch/KV/WAL 协议题 T085 | 9、16 | 编号交付覆盖，避免原型代码替代协议说明与证明 |
| 缓存有限状态检查题 T086 | 17、18、19 | 条款转写、动作参数约束、派生谓词和解释冲突审计 |

- **Prompt**：增加 `prompt_sections/files/*.md` 及 `prompt_sections/sections.yaml` 注册，作为稳定提示参与 Solver 的上下文。它改变模型决策倾向，不自动执行检查。
- **Skill**：增加或修改 `skills/<name>/SKILL.md`，通过 `skills/skills.yaml` 挂载。任务开始可按元数据选中加载；正文中的步骤、assert 示例或“验收探针”仍需模型落实，不是宿主自动执行的验证器。
- **Rail**：增加 `rails/*.py` 和 `rails/rails.yaml` 注册，由宿主在调用/模型/工具生命周期 Hook 中执行。可保存状态、追加 steering、请求继续或请求结束；能做什么应看实际代码，不能只看计划中的“提醒”。

以下每项都给出实际文件。失败尝试只列计划入口，不把回退到 H0 的引用当作新实现。

## 3. 全部候选逐项说明
### 3.1 #01：CUDA 审查机制分解

- Epoch/Batch：1 / 1。优化器状态：`success`。
- 最终判定：候选执行出错，无法判断改善（`candidate_gate_inconclusive_due_to_error_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_001/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_001/plan.yaml)。

当建议 shared-memory tiling、Tensor Core 等结构优化时，不能只写“做分块”。提示模型逐项解释数据搬运、片上布局/冲突、搬运与计算重叠、寄存器片段供给，每项写出缺陷、硬件原因和修法；保留已有数值正确性分析。

**动作**：`prompt/add`，运行名称 `review_acceleration_layer`。

实际语义文件：[prompt_sections/files/review_acceleration_layer.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m001/candidate/r2c344a1f/prompt_sections/files/review_acceleration_layer.md)。

本次局部评估：case score `0.0`，连续分数 `无可信 LLM 分数`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e001/b001/a001/c001/cases/c001_5d012c26/result.json)。

### 3.2 #02：CUDA 瓶颈定量推导

- Epoch/Batch：1 / 1。优化器状态：`success`。
- 最终判定：候选执行出错，无法判断改善（`candidate_gate_inconclusive_due_to_error_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_002/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_002/plan.yaml)。

先按真实访问模式计算 FLOP/byte，再用设备峰值算力÷带宽得到机器平衡点，展示比较后才下 memory-bound/compute-bound 结论。设备数字和估计来源必须交代。

**动作**：`prompt/add`，运行名称 `quantitative_bottleneck_analysis`。

实际语义文件：[prompt_sections/files/quantitative_bottleneck_analysis.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m002/candidate/r2c344a1f/prompt_sections/files/quantitative_bottleneck_analysis.md)。

本次局部评估：case score `0.0`，连续分数 `无可信 LLM 分数`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e001/b001/a002/c001/cases/c001_5d012c26/result.json)。

### 3.3 #03：预算尾段交付 Rail

- Epoch/Batch：1 / 2。优化器状态：`success`。
- 最终判定：判分通过，但行为改善未确认（`candidate_behavior_change_not_verified`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_003/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_003/plan.yaml)。

实际代码默认保留约 25% 模型轮次，且不少于 3 轮；在尾段发出写编号文档的 steering，并保存最近的模型文字。代码还包含预算耗尽后 request_force_finish、返回最近文本并附未完整说明的分支；因此不只是提醒。是否触发要结合运行轨迹，本次目标执行仍达到 300 步。

**动作**：`rail/add`，运行名称 `deliverable_budget_rail`。

实际语义文件：[rails/deliverable_budget_rail.py](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m003/candidate/r2c344a1f/rails/deliverable_budget_rail.py)。

本次局部评估：case score `1.0`，连续分数 `0.903`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e001/b002/a001/c001/cases/c001_79a501fe/result.json)。

### 3.4 #04：独立规格 Oracle Skill（未形成有效候选）

- Epoch/Batch：1 / 2。优化器状态：`failed`。
- 最终判定：生成/校验失败（`member_optimization_status_failed`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_004/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_004/plan.yaml)。

计划要求先从规格转写“先提出完整 draft、再验证、如何计费”的顺序，独立枚举整条 draft，禁止复用被测实现的截断规则或修改期望值凑通过。此次 Skill 引用路径/加载检查失败，Repair 也遇到工作区文件误判，未生成可供正常比较的新候选。

**动作**：`skill/add`，运行名称 `independent_spec_oracle_verification`。

该动作没有形成成功的新候选包；计划意图与可用运行实现需分开看。

### 3.5 #05：独立 Oracle 推导提示

- Epoch/Batch：1 / 2。优化器状态：`success`。
- 最终判定：候选执行出错，无法判断改善（`candidate_gate_inconclusive_due_to_error_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_005/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_005/plan.yaml)。

实际 Prompt 要求先从规格定义生成、接受/拒绝和计费顺序，再写独立枚举器；即使首位拒绝，也不能把规格要求已生成的 draft 后缀费用丢掉。示例期望值来自独立推导，用负控证明错误捷径确实会给出不同结果。

**动作**：`prompt/add`，运行名称 `independent_oracle_derivation`。

实际语义文件：[prompt_sections/files/independent_oracle_derivation.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m005/candidate/r2c344a1f/prompt_sections/files/independent_oracle_derivation.md)。

本次局部评估：case score `0.0`，连续分数 `无可信 LLM 分数`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e001/b002/a003/c001/cases/c001_79a501fe/result.json)。

### 3.6 #06：交叉验证分歧归属提示

- Epoch/Batch：1 / 3。优化器状态：`success`。
- 最终判定：局部未改善（`candidate_did_not_improve_target_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_006/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_006/plan.yaml)。

实现与自写检查器不一致时，先从任务抽出概率守恒、分支质量等不变量，并构造手算微例，逐侧判断谁错；不能默认后写的检查器更权威。修复有违例的一侧，独立主路径成立后继续交付，并如实说明辅助检查仍未解决的问题。

**动作**：`prompt/add`，运行名称 `crosscheck_attribution`。

实际语义文件：[prompt_sections/files/crosscheck_attribution.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m006/candidate/r2c344a1f/prompt_sections/files/crosscheck_attribution.md)。

本次局部评估：case score `0.0`，连续分数 `0.08`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e001/b003/a001/c001/cases/c001_cbfa6d79/result.json)。

### 3.7 #07：交付进展 Rail

- Epoch/Batch：1 / 3。优化器状态：`success`。
- 最终判定：候选执行出错，无法判断改善（`candidate_gate_inconclusive_due_to_error_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_007/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_007/plan.yaml)。

实际代码按工具调用计步，默认达到 6 步且缺编号正文时反复 steering；跟踪若干文件名及章节标记。实现写有 P1–P4 标记和至少 2 节的判断，未完全动态抽取任务。hard_stop_steps 默认 None，只有显式配置且已有章节/文件时才可能在安全边界强停；不是计划描述中的“12/40 步、最多 3 次提醒”。

**动作**：`rail/add`，运行名称 `delivery_progress_guard`。

实际语义文件：[rails/delivery_progress_guard.py](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m007/candidate/r2c344a1f/rails/delivery_progress_guard.py)。

本次局部评估：case score `0.0`，连续分数 `无可信 LLM 分数`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e001/b003/a002/c001/cases/c001_cbfa6d79/result.json)。

### 3.8 #08：分歧定位 Rail

- Epoch/Batch：1 / 3。优化器状态：`success`。
- 最终判定：局部未改善（`candidate_did_not_improve_target_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_008/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_008/plan.yaml)。

实际实现建立 idle/unconfirmed/restart/diagnosed 状态，检查模型是否声称数值分歧以及 ctx.extra 中是否已有分支定位证据；缺证据时提醒，并可 request_force_finish，返回 unverified/divergence_unlocated。本身不会执行独立差分。此次候选第 5 轮即以 unverified 结果结束，得分 0；与“仅督促继续调查”的计划有明显区别。

**动作**：`rail/add`，运行名称 `differential_check_gate`。

实际语义文件：[rails/differential_check_gate.py](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m008/candidate/r2c344a1f/rails/differential_check_gate.py)。

本次局部评估：case score `0.0`，连续分数 `0.0`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e001/b003/a003/c001/cases/c001_cbfa6d79/result.json)。

### 3.9 #09：编号交付 Prompt + 覆盖检查 Rail

- Epoch/Batch：1 / 4。优化器状态：`success`。
- 最终判定：局部保留，但本轮全局晋升被拒绝（`candidate_effect_retained_after_epoch_checkpoint`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_009/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_009/plan.yaml)。

Prompt 要求开工建立任务编号清单、提前写每节、代码修改同步正文、预算紧时交付当前成果。Rail 从任务发现编号、检查正文是否真正覆盖，并排除占位/仅引用；工具调用仍在进行时通常记录缺口后返回。没有工具调用且正文不全时提醒，后续缺口仍在可强制返回草稿和覆盖说明。局部判分通过，但整轮未晋升。

**动作**：`prompt/add`，运行名称 `deliverable_coverage`。

实际语义文件：[prompt_sections/files/deliverable_coverage.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m009/candidate/r2c344a1f/prompt_sections/files/deliverable_coverage.md)。

**动作**：`rail/add`，运行名称 `deliverable_coverage_gate`。

实际语义文件：[rails/deliverable_coverage_gate.py](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m009/candidate/r2c344a1f/rails/deliverable_coverage_gate.py)。

本次局部评估：case score `1.0`，连续分数 `0.926`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e001/b004/a001/c001/cases/c001_fe2e66dc/result.json)。

### 3.10 #10：请求覆盖清单提示

- Epoch/Batch：2 / 1。优化器状态：`success`。
- 最终判定：候选执行出错，无法判断改善（`candidate_gate_inconclusive_due_to_error_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_010/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_010/plan.yaml)。

开写前从用户请求提取最少问题数量、必需解释、目标硬件和禁止项，按清单逐项写，再对成稿逐项核对补缺。强调具体目标上的技术机制，不用通用风格建议充数，也不能凭空增加任务未要求的内容。

**动作**：`prompt/add`，运行名称 `requirement_coverage_checklist`。

实际语义文件：[prompt_sections/files/requirement_coverage_checklist.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m010/candidate/r2c344a1f/prompt_sections/files/requirement_coverage_checklist.md)。

本次局部评估：case score `0.0`，连续分数 `无可信 LLM 分数`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e002/b001/a001/c001/cases/c001_5d012c26/result.json)。

### 3.11 #11：预算/正文状态 Rail 的另一版

- Epoch/Batch：2 / 2。优化器状态：`success`。
- 最终判定：局部未改善（`candidate_did_not_improve_target_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_011/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_011/plan.yaml)。

与 #3 同名但文件不同。实际实现每次模型调用前报告缺失/过短的编号章节，正文长度阈值为 200 字符，并识别自测命令。after_model_call 看到 tool_calls 会直接返回，因此停止前补齐正文的那条提醒不能在持续工具调用阶段触发；不宜按计划把它理解成可靠的“半程强制转交付”。

**动作**：`rail/add`，运行名称 `deliverable_budget_rail`。

实际语义文件：[rails/deliverable_budget_rail.py](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m011/candidate/r2c344a1f/rails/deliverable_budget_rail.py)。

本次局部评估：case score `0.0`，连续分数 `0.195`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e002/b002/a001/c001/cases/c001_79a501fe/result.json)。

### 3.12 #12：分歧裁决 Skill

- Epoch/Batch：2 / 2。优化器状态：`success`。
- 最终判定：局部未改善（`candidate_did_not_improve_target_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_012/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_012/plan.yaml)。

冻结实现和期望值，完整跑一遍验证并记录 pass/fail/not-executed，缩小最小分歧输入，再做第三条独立精确计算，确认哪一侧错误后才改期望或代码。禁止删断言、跳交叉检查或用浮点容差掩盖分歧。实际文件是多步文字规程，没有自动执行器。

**动作**：`skill/add`，运行名称 `disagreement_reconciliation`。

实际语义文件：[skills/disagreement_reconciliation/SKILL.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m012/candidate/r2c344a1f/skills/disagreement_reconciliation/SKILL.md)。

本次局部评估：case score `0.0`，连续分数 `0.146`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e002/b002/a002/c001/cases/c001_79a501fe/result.json)。

### 3.13 #13：预留尾段 Rail

- Epoch/Batch：2 / 2。优化器状态：`success`。
- 最终判定：局部未改善（`candidate_did_not_improve_target_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_013/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_013/plan.yaml)。

实际默认保留预算的 25%，最少 6 轮；接近上限且缺正文时追加提醒，文本为空时还可 request_model_continue。代码使用固定优先标记 I1–I5，而非对任意任务完整动态建模。它不自行写报告，也不保证模型放弃下一次工具调用。

**动作**：`rail/add`，运行名称 `deliverable_reservation_rail`。

实际语义文件：[rails/deliverable_reservation_rail.py](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m013/candidate/r2c344a1f/rails/deliverable_reservation_rail.py)。

本次局部评估：case score `0.0`，连续分数 `0.25`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e002/b002/a003/c001/cases/c001_79a501fe/result.json)。

### 3.14 #14：分歧归属与交付优先提示

- Epoch/Batch：2 / 3。优化器状态：`success`。
- 最终判定：候选执行出错，无法判断改善（`candidate_gate_inconclusive_due_to_error_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_014/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_014/plan.yaml)。

计算不一致时先检查正在编写的产物。只有全新进程、约三行且不依赖本地模块的最小复现也失败，才提出解释器/工具链假设。独立推导已经得到的结果应立即形成固定文件和编号正文，调试只处理未解决部分，不能拿工具链疑云替代交付。

**动作**：`prompt/add`，运行名称 `delivery_first_attribution`。

实际语义文件：[prompt_sections/files/delivery_first_attribution.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m014/candidate/r2c344a1f/prompt_sections/files/delivery_first_attribution.md)。

本次局部评估：case score `0.0`，连续分数 `无可信 LLM 分数`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e002/b003/a001/c001/cases/c001_cbfa6d79/result.json)。

### 3.15 #15：编号答案框架提示

- Epoch/Batch：2 / 3。优化器状态：`success`。
- 最终判定：候选执行出错，无法判断改善（`candidate_gate_inconclusive_due_to_error_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_015/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_015/plan.yaml)。

将每个编号拆成“现在可推导”和“需要执行”两栏，先完整写证明、递推、终止/最优性/复杂度/复用边界，再补执行结果。执行没发生就明确 not run；状态表或阻塞说明不能替代可完成的理论正文。

**动作**：`prompt/add`，运行名称 `numbered_deliverable_frame`。

实际语义文件：[prompt_sections/files/numbered_deliverable_frame.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m015/candidate/r2c344a1f/prompt_sections/files/numbered_deliverable_frame.md)。

本次局部评估：case score `0.0`，连续分数 `无可信 LLM 分数`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e002/b003/a002/c001/cases/c001_cbfa6d79/result.json)。

### 3.16 #16：加强交付 Prompt（失败）

- Epoch/Batch：2 / 4。优化器状态：`failed`。
- 最终判定：生成/校验失败（`member_optimization_status_failed`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_016/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_016/plan.yaml)。

计划修改已经存在的 deliverable_coverage，增加开工建答案、逐阶段更新，以及同一检查最多 3 次失败后记录局限再补正文。实际本轮基底已退回 H0，目标文件不存在，执行被 modify baseline mismatch 拒绝，因此没有形成这一改动。

**动作**：`prompt/modify`，运行名称 `deliverable_coverage`。

该动作没有形成成功的新候选包；计划意图与可用运行实现需分开看。

### 3.17 #17：转移前置条件审计 Skill

- Epoch/Batch：2 / 5。优化器状态：`success`。
- 最终判定：局部通过，整轮目标题复测失败（`candidate_failed_target_replay_checkpoint`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_017/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_017/plan.yaml)。

把规格中的每个动作拆成命名条件，区分动作参数与状态字段，逐条核对当前代/匹配 ID 等约束，代码断言条件名集合覆盖规格。发现反例时先按规格手算该步是否 enabled，再检查自己的条件是否漏掉或多加；单独保持每个 mutant 的指定改动。

**动作**：`skill/add`，运行名称 `transition_precondition_audit`。

实际语义文件：[skills/transition_precondition_audit/SKILL.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m017/candidate/r2c344a1f/skills/transition_precondition_audit/SKILL.md)。

本次局部评估：case score `1.0`，连续分数 `0.86`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e002/b005/a001/c001/cases/c001_91db1151/result.json)。

### 3.18 #18：规格歧义继续交付提示

- Epoch/Batch：2 / 5。优化器状态：`success`。
- 最终判定：候选执行出错，无法判断改善（`candidate_gate_inconclusive_due_to_error_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_018/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_018/plan.yaml)。

输入齐备但出现规格读法冲突时，写明竞争解释、对照不变量和变体定义选择读法、完成全部交付，再公开假设和不同解释的结果。真正缺权限、凭据或必要资源时才停止询问。该 Prompt 改的是解释冲突后的决策顺序，不会自动解出正确模型。

**动作**：`prompt/add`，运行名称 `ambiguity_is_an_assumption`。

实际语义文件：[prompt_sections/files/ambiguity_is_an_assumption.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m018/candidate/r2c344a1f/prompt_sections/files/ambiguity_is_an_assumption.md)。

本次局部评估：case score `0.0`，连续分数 `无可信 LLM 分数`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e002/b005/a002/c001/cases/c001_91db1151/result.json)。

### 3.19 #19：扩展派生谓词审计 Skill

- Epoch/Batch：2 / 5。优化器状态：`success`。
- 最终判定：有部分进展，未达到通过条件（`candidate_made_partial_verifier_progress`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_019/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_019/plan.yaml)。

在 #17 基础上修改同一 Skill，不只审计动作 guard，还审计 pins、存活性、占用等派生 helper：量化集合是已分配还是全部可能对象，依据物理完成还是 host 通知，析取/否定是否转写正确。用单条件替换与同一穷举比较定位错误，并保持 mutant 差异和计数口径。实际文本有整体改写。

**动作**：`skill/modify`，运行名称 `transition_precondition_audit`。

实际语义文件：[skills/transition_precondition_audit/SKILL.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m019/candidate/r2c344a1f/skills/transition_precondition_audit/SKILL.md)。

本次局部评估：case score `0.0`，连续分数 `0.78`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e002/b005/a003/c001/cases/c001_91db1151/result.json)。

### 3.20 #20：正确性/性能双轴审查提示

- Epoch/Batch：3 / 1。优化器状态：`success`。
- 最终判定：候选执行出错，无法判断改善（`candidate_gate_inconclusive_due_to_error_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_020/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_020/plan.yaml)。

从用户投诉提取正确性轴与性能轴，分别列机制清单和可观察证据，先写根因缺陷，再写各轴修复机制，成稿后双轴自查；正确性解释充分不能自动替代性能覆盖，也不能用任务禁止的泛泛建议补数量。

**动作**：`prompt/add`，运行名称 `review_axis_coverage_checklist`。

实际语义文件：[prompt_sections/files/review_axis_coverage_checklist.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m020/candidate/r2c344a1f/prompt_sections/files/review_axis_coverage_checklist.md)。

本次局部评估：case score `0.0`，连续分数 `无可信 LLM 分数`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e003/b001/a001/c001/cases/c001_5d012c26/result.json)。

### 3.21 #21：性能结论定量化提示（失败）

- Epoch/Batch：3 / 1。优化器状态：`failed`。
- 最终判定：生成/校验失败（`member_optimization_status_failed`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_021/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_021/plan.yaml)。

计划要求强度、机器平衡点、单位和估计来源共同支持瓶颈分类。实际 Prompt 注册含 PromptSectionSpec 不接受的 id 字段，修复又出现工作区文件越界误判，未形成有效候选。

**动作**：`prompt/add`，运行名称 `quantitative_regime_claims`。

该动作没有形成成功的新候选包；计划意图与可用运行实现需分开看。

### 3.22 #22：交付尾段提醒 Rail

- Epoch/Batch：3 / 2。优化器状态：`success`。
- 最终判定：局部未改善（`candidate_did_not_improve_target_cases`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_022/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_022/plan.yaml)。

实际默认预留最后 4 轮，最低 2 轮；读取宿主预算和迭代序号，每轮给剩余预算提示，在尾段请求编号正文，在识别到模型声称主产物已可运行时提早提示转写答案。它以文本识别“主产物成功”，不等于独立测试确认；不会自行写答案或强制禁用工具。与计划中的固定比例预留不同。

**动作**：`rail/add`，运行名称 `deliverable_tail_reservation`。

实际语义文件：[rails/deliverable_tail_reservation.py](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m022/candidate/r2c344a1f/rails/deliverable_tail_reservation.py)。

本次局部评估：case score `0.0`，连续分数 `0.155`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e003/b002/a001/c001/cases/c001_79a501fe/result.json)。

### 3.23 #23：代码与文字同步交付 Skill

- Epoch/Batch：3 / 2。优化器状态：`success`。
- 最终判定：局部通过，重组后复测失败（`filtered_harness_failed_replay`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_023/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_023/plan.yaml)。

实际成品主要是交付调度：先把编号分成 code/text，最小可核验里程碑一到就写完整答案骨架，后续代码改动同步更新相应文字；额外测试/重构需有具体失败观测，执行状态必须诚实，验收检查正文覆盖、数值一致和非中止回答。它是文字规程，不是自动运行的 guard；原计划提到的概率专用细则并未逐条原样落成。

**动作**：`skill/add`，运行名称 `spec_rederivation_gate`。

实际语义文件：[skills/spec_rederivation_gate/SKILL.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m023/candidate/r2c344a1f/skills/spec_rederivation_gate/SKILL.md)。

本次局部评估：case score `1.0`，连续分数 `0.938`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e003/b002/a002/c001/cases/c001_79a501fe/result.json)。

### 3.24 #24：固定产物优先与探查约束 Skill

- Epoch/Batch：3 / 3。优化器状态：`success`。
- 最终判定：局部通过，重组后复测失败（`filtered_harness_failed_replay`）。
- 审计入口：[member_optimization_ref.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_024/member_optimization_ref.yaml)；[plan.yaml](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/run/member_optimizations/member_optimization_024/plan.yaml)。

在 #23 同一路径上修改，但实际是重写标题、触发描述、决策步骤和验收规程，不是保留原文加一个小条目。里程碑从最小正确性检查改向公开入口可运行与固定文件落盘；尽早写固定产物与编号骨架，后续探查必须说明它解除哪项待验收阻塞，代码/正文同步。计划提到连续 20 次无产物就切换，但最终文本没有这个数值限制或运行计数器。

**动作**：`skill/modify`，运行名称 `spec_rederivation_gate`。

实际语义文件：[skills/spec_rederivation_gate/SKILL.md](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/mh/runs/m024/candidate/r2c344a1f/skills/spec_rederivation_gate/SKILL.md)。

本次局部评估：case score `1.0`，连续分数 `0.87`；结果见 [result.json](/Users/jiuwen/.jiuwenswarm/rsi/tasks/rsi-ebcd3005/ce/e003/b003/a001/c001/cases/c001_cbfa6d79/result.json)。

## 4. 本次真正发生的顺序累积

- 第 1 轮 #9 的 Prompt + Rail 局部通过后，成为该轮工作版本；后面的缓存题用这个工作版本执行。全局因临时成功保护失败，未晋升。
- 第 2 轮 #17 新增 `transition_precondition_audit`，#18 尝试加一个 Prompt，#19 修改既有 Skill。不是三个独立 Skill 同时注入，而是在当时工作/修复版本上迭代。
- 第 3 轮 #23 新增 `spec_rederivation_gate`，#24 修改同一个 Skill，形成前面讨论的 A+B。全量筛掉 #24 后，程序恢复 #23 的内容重评；#23 单独版本复测失败，最终整轮未晋升。

## 5. 计划与实现不一致的重点

| 候选 | 计划/名称容易形成的理解 | 实际文件中的行为 |
| --- | --- | --- |
| #3 | 提醒预留写作预算 | 还含强制结束和返回最近文本的分支 |
| #7 | 动态提取交付、12/40 步触发、最多 3 次提醒 | 默认 6 工具步，含固定 P1–P4 标记；可选 hard-stop；没有同样的提醒次数上限 |
| #8 | 促使模型继续做分支差分 | 缺定位证据可直接强制返回 unverified；本次第 5 轮即结束 |
| #9 | 只做交付覆盖提醒 | 一次提醒后仍缺正文，可强制返回草稿和覆盖说明；持续工具调用时并不直接阻断 |
| #11 | 半程预算提醒 | 实际偏重每轮章节状态检查；带工具调用的响应跳过停止前提醒分支 |
| #13 | 任意编号任务通用 | 使用固定优先编号 I1–I5；不是纯动态契约解析 |
| #22 | 按比例预留写作时间 | 实际默认只保留 4 轮；主产物成功主要靠文字信号识别 |
| #23 | 概率推导与交付联合规程 | 实际成品的中心是代码/正文同步与尽早交付；没有自动概率验证器 |
| #24 | 保留原文，追加连续 20 步探查上限 | 实际大量重写；最终文本没有 20 步硬限制，也没有执行计数器 |

这些说明只能证明源码里的逻辑，不代表每个分支都在本次运行中触发。#8 的提前 unverified 有实际结果佐证；#3/#9 的局部执行仍达到 300 步，说明不能只因代码写了结束分支就宣称已可靠解决预算耗尽。

## 6. 已观察到的结果与边界

- 9 个经过 Repair 的候选混入 Agent 工作区模板，运行时发生 identity 重复绑定；相关 Prompt/Rail 本身的效果因而无法从这些执行判断。
- #3 判分通过但行为核对 unknown；#9 局部保留但全局未晋升；#17、#23、#24 局部成功但回归未复现。
- #19 得到部分改进，连续分数 0.78，未达到 0.8 通过阈值。
- 最终发布状态为 published，但实际发布的是 H0，仅一个 `harness_config.yaml`。不能将这 21 次成包称为最终交付了 21 个已验证优化 Harness。

本次候选多数在改变模型的工作顺序、检查清单、提示和停止行为，没有直接修复 Solver 为题目生成的算法实现。效果仍依赖模型是否采用这些规程，Rail 的 Hook 和证据读取是否正确，以及同一任务重跑是否稳定。
