# 数学推理专家团（math_reasoning）

对接本机部署的 **Mathematical_Reasoning_Platform**（`/docker/zy/Mathematical_Reasoning_Platform`），
按"任务类型 × 交付要求"把数学任务分派给四个功能模块。

## 团队结构

| 成员 | 定位 | 交付物 | 耗时 |
|---|---|---|---|
| `leader` | 路由：按任务类型×交付要求分派 | 任务分派与结果转述 | — |
| `nl_solver` | 快：自然语言→自然语言 | 一般数学求解的解答与过程 | 分钟内 |
| `formal_prover` | 准：自然语言→Lean4 形式化 | 编译验证的证明 | 5-30 分钟 |
| `opt_solver` | 快：优化建模→求解 | 数值最优解 | 分钟级 |
| `paper_gen` | 完整：建模→计算→论文 PDF | 完整竞赛论文 | 小时级（异步） |

## 前置条件

- 本机已部署数学推理平台（含 `platform.config.json` 与可用的模型 API key）
- 平台 Python：`/usr/bin/python3`（3.10，openai 已装）
- paper_gen 的 XeLaTeX：`/docker/texlive`（生成 PDF 时使用）

可用环境变量覆盖：`MATH_PLATFORM_ROOT`、`MATH_PLATFORM_PYTHON`。

## 用法示例

- "计算 137×89-2048÷16" → leader 分派给 nl_solver
- "证明：根号2是无理数" → leader 分派给 formal_prover
- "工厂生产两种产品求最大利润的线性规划方案" → leader 分派给 opt_solver
- "完整解决这道数模竞赛题并生成论文" → leader 分派给 paper_gen（先启动后轮询）

## 工具

`math_platform_solve`（各成员自带）：把题目转交平台并回传结构化结果。

- `backend`：nl / formal / opt / paper
- `action`：run（同步，nl/formal/opt 用）/ start+status（两段式，paper 用）
- 返回字段按后端不同：verdict+proof / pred_value+solver_status / paper_status+pdf+pages
