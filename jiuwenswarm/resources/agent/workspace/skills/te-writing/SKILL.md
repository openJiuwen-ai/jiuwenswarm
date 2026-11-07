---
name: te-writing
description: 科研流水线第四阶段（Writing）。当前三阶段产出（假设、方案、结果）齐备，需要按 ICLR 模板撰写英文论文并编译为 PDF 时使用。产出 paper/main.tex 与 paper/main.pdf。
---

# TE-Writing（写作模块）

## 职责

按学术规范完成英文论文（LaTeX + ICLR 模板）并编译 PDF。**论文的每一个数字
必须来自 `outputs/results/` 下的结果文件**，不得从记忆中填写。

## 输入

- `{PROJECT_DIR}/outputs/stage1-ideation.md`（假设与文献）
- `{PROJECT_DIR}/outputs/stage2-plan.md`（口径）
- `{PROJECT_DIR}/outputs/stage3-experiment.md`（结果与结论）
- `{PROJECT_DIR}/outputs/results/*.json|csv`、`figures/*.png`
- `{PROJECT_DIR}/paper/references.bib`

## 流程

1. **先建数字表**：把要写进论文的每个数字列成一张表，标注它的来源文件与字段。
   写作时只从这张表取数。
2. **核对引用**：`references.bib` 中每条都必须真实存在且信息正确。宁可少引。
3. 按 ICLR 模板撰写，正文结构：
   - Introduction：问题、为什么 VLA 的类比是结构性的而非隐喻
   - Related Work：必须显式承认最近邻工作，并说明本文不重复其贡献
   - Method：机制定义 + 记号；把 RTC/ACT 到 token 解码的映射写清楚
   - Experimental Setup：模型、数据、NFE 口径、统计口径、**记账定义**
   - Results：按假设组织，支持与否证并列
   - Mechanism / Analysis：解释失败或成功的原因（独立测量，不靠推测）
   - Limitations：**明确写出哪些结论依赖模型规模，哪些是结构性的**
   - Conclusion
4. **负结果的处理**：否证的假设照样写进 Results，并在 Analysis 里给出机制。
   不得因为假设被否证就把它从论文里删掉——那只会在审稿时被当作隐瞒。
5. 编译：`pdflatex` → `bibtex` → `pdflatex` ×2，确保 PDF 无未解析引用。
6. 图从 `figures/` 直接 `\includegraphics`，不要重画。

## 输出（写入 {PROJECT_DIR}/）

- `paper/main.tex`、`paper/references.bib`
- `paper/main.pdf`（最终改名/复制为 `paper/paper.pdf`）
- `outputs/stage4-writing.md`：写作决策记录（哪些假设进了论文、哪些数字来自哪个
  文件、与 stage3 结论的差异）
- `outputs/stage-4-done.txt`：最后写入，内容为 `DONE`

## 质量门

- 所有图表编号在正文中被引用
- 没有「显著提升」这类无数字的断言；每个断言带效应量与区间
- Limitations 中明确写出规模局限与「本条结论是否依赖规模」
- 编译后无 `??` 未解析引用
