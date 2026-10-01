---
name: sci-paper-fars
description: End-to-end automated research short paper generation (FARS-style pipeline): literature retrieval, chain-of-thought ideation, experiment planning, simulated experiment execution with figures, ICLR LaTeX writing and PDF compilation. Use when the user asks to generate an academic paper, research paper, conference paper, ICLR paper, or to "write a paper about X" / "generate a paper on X". Triggers on sci-paper-fars, 论文生成, 自动写论文, 科研论文, 生成论文, ICLR 论文, paper generation, research paper.
description_cn: 全自动科研短论文生成流水线(FARS 风格):文献检索 → 思维链选题 → 实验方案设计 → 模拟实验真实执行(含图表)→ ICLR LaTeX 写作 → PDF 编译。用户要求"写一篇论文"、"生成科研论文"、"调研并写一篇 ICLR 论文"时使用。
---

# sci-paper-fars:全自动科研论文生成流水线

一条命令从零生成一篇 ICLR 模板英文科研短论文(含真实执行的模拟实验与图表),对应 FARS(Fully Automated Research System)流水线的四个模块:Ideation → Planning → Experiment → Writing。

## 评审驱动要求(2026-09-26,基于 Stanford Agentic Reviewer 反馈)

实验与写作阶段 prompt 已内置机器评审应对要求:per-seed 方差 + 配对 t 检验、Oracle 上界与强基线(Cross-Encoder Rerank)、第二领域(SynSupport-100)、lost-in-the-middle 位置压力测试、敏感性/代理-神谕相关性/贪心-vs-DP 差距、token 归一化前沿图、算法伪代码、超参表、LLM/embedding 命名、部署数据前置到摘要与贡献、6 篇并发工作(RCR-Router/PAACE/EXIT/VITAL-RAG/RISE/Know-Before-You-Fetch)对比。参考实现见 `D:\CCF-BDCI\demo\paper_v3\`(simulate_experiment.py + paper.tex)。

## STOP:执行前检查

1. **依赖**:运行 `python scripts/setup_check.py`(venv 需含 openai、python-dotenv、matplotlib、numpy)。
2. **模型配置**:读取 `~/.jiuwenswarm/config/.env` 的 `API_BASE`/`API_KEY`/`MODEL_NAME`(OpenAI 兼容协议,如 DeepSeek)。未配置则脚本会报错退出。
3. **LaTeX**:需要本机有 pdflatex(MiKTeX/TeX Live);脚本会自动定位常见安装路径,首次编译自动安装缺失宏包。
4. **输出目录**:默认 `./paper_output`,请勿与已有产物目录冲突;产物含 paper.pdf、idea.json、plan.json、experiment_results.json、figures/。

## 用法

```bash
# 生成一篇论文(默认主题:Agent Context Engineering)
python scripts/fars_pipeline.py

# 指定研究主题
python scripts/fars_pipeline.py --topic "Agent Memory Engine"

# 指定输出目录 / 用便宜模型
python scripts/fars_pipeline.py --out D:/papers/paper1 --model deepseek-flash

# 断点续跑:跳过已完成阶段(1=ideation 2=planning 3=experiment 4=writing)
python scripts/fars_pipeline.py --skip-stages 1,2
```

## 流水线说明

| 阶段 | 内容 | 产出 |
| --- | --- | --- |
| 1 Ideation | Semantic Scholar/arXiv 检索近期文献 → CoT 分析研究痛点 → 生成假设、标题、贡献列表 | idea.json |
| 2 Planning | 设计基线、消融变体、指标与数据集 | plan.json |
| 3 Experiment | LLM 生成**真实可执行的模拟实验脚本** → 脚本真实运行(固定随机种子)→ 结果 JSON + 5 张 matplotlib 图表(含误差棒/位置曲线/token 前沿) | experiment_results.json, figures/*.png |
| 4 Writing | 按 ICLR 2026 模板生成完整 LaTeX(引用白名单防幻觉)→ pdflatex 编译 | paper.tex, paper.pdf |

## 设计要点

- **数据可追溯**:论文中所有数字来自真实执行的实验脚本,非 LLM 编造;脚本固定种子可复现。
- **引用防幻觉**:内置真实文献白名单,LLM 只允许引用白名单内条目。
- **资源可计量**:每阶段 token/耗时写入 `logs/token_log.jsonl`,可直接用于资源报告。
- **防并发**:同一输出目录同时只允许一个实例运行(`.fars_run.lock`)。
- **自愈**:文献检索三级降级(Semantic Scholar → arXiv API → 白名单);pdflatex 失败自动重试。

## 安全

Stage 3(Experiment)会**真实执行 LLM 生成的 Python 脚本**。虽然执行前有静态检查(ast 扫描,拒绝 os/subprocess/socket/requests 等危险模块与 eval/exec 动态执行,违规时回喂 LLM 改写;单次运行超时 300 秒强制终止),但**这不是沙箱隔离**:

- **请仅在可信环境运行本流水线**(如本地开发机、自己的容器),不要在共享/多租户环境直接运行;
- **不要传入不可信输入**:`--topic` 与文献检索返回的摘要会进入 LLM 提示词,恶意内容可能诱导生成危险代码;
- 长期如需在不可信环境运行,建议将 Stage 3 的执行放入容器或 seccomp 等沙箱。
