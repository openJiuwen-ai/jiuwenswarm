---
name: te-experiment
description: 科研流水线第三阶段（Experiment）。当实验方案（stage2-plan.md）已定，需要编写并运行扩散语言模型 token 编辑实验、做统计分析并产出图表时使用。产出 stage3-experiment.md。
---

# TE-Experiment（实验模块）

## 职责

脚本编写 → 执行 → 结果收集 → 分析。**负结果同样是结果**，必须如实记录并给出
机制解释，不得为了支持假设而调参重跑。

## 输入

- `{PROJECT_DIR}/outputs/stage2-plan.md`（实验方案）
- `{PROJECT_DIR}/experiments/`（脚本目录，**先读里面已有的脚本再改**）

## 流程

1. **先跑 `smoke_test.py`**。它用随机权重 checkpoint 以极小样本量跑通每个脚本，
   让语法与接线错误在秒级暴露。不要跳过。
2. 按方案编写/修改实验脚本到 `{PROJECT_DIR}/experiments/`：
   - `e1_boundary.py`：分块解码的代价及其位置分布
   - `e2_rtc.py`：重叠与衰减
   - `e3_temporal_ensemble.py`：时间集成与稳定性
   - `e4_efficiency.py`：达到目标精度的 NFE、Pareto 前沿
   - `e5_policy_ablation.py`：编辑策略与算子消融
   - `run_all.py`：串起全部实验，把假设判定汇总成一张表
3. 用 `mcp_exec_command` 执行（workdir 用绝对路径），单次超时 ≤ 600s，
   长跑必须放后台并把输出重定向到日志文件。失败时读 stderr 修复后重跑，最多 3 次。
4. 结果输出到 `{PROJECT_DIR}/outputs/results/`（JSON/CSV）+ `figures/`
   （matplotlib PNG，DPI ≥ 150）。

## 硬性纪律

- **结果文件必须由脚本产生**。禁止手工写入或修补 `outputs/results/` 下的任何
  数字；一旦发现，该结果作废并重跑。
- **改动实验代码后，此前所有结果作废**。在 `stage3-experiment.md` 中明确写出
  「本轮结果的代码版本 / 时间戳」，以及哪些早期结果已被作废以及原因。
- **记账口径不得中途改变**。若发现口径实现有误，作废全部受影响结果并整体重跑，
  不允许新旧口径的数字混在同一张表里。
- **不要在假设之间摇摆**：一次只改一个耦合量。若同时改了语料规模与模型容量，
  任何效应都无法归因。

## 分析

对每个假设给出 支持 / 否证 / 部分支持。对**否证**的假设必须回答：
该失败是「信号不够好」还是「机制用不上信号」？这两者的区别是本主题的核心，
必须用独立测量区分（例如：测量信号对「哪些 token 是错的」的 AUC 与 precision@k，
以及测量算子改变 token 的概率）。

## 输出（写入 {PROJECT_DIR}/outputs/）

- `stage3-experiment.md`：每个实验的方法摘要、结果表、结论、失败与重试记录、
  **被作废结果清单**
- `stage-3-done.txt`：最后写入，内容为 `DONE`

## 质量门

- 每个数字结论都能追溯到具体结果文件（文件名 + 路径写进 stage3 文档）
- 图表轴标签完整、可独立理解
- 所有 p 值同时给出效应量与置信区间
- 负结果给出了机制层面的解释，而不只是「不显著」
