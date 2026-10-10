# Writing 模块编排改进方案（历史设计记录，非运行规范）

> **状态：2026-09-15 起不再作为实现依据。** 本文记录 2026-09-12 的四角色（Writer、Reviewer、Reviser、Formatter）重构设想，保留仅供理解历史产物。当前运行规范以 [`SKILL.md`](SKILL.md)、[`workflows/writing.md`](workflows/writing.md)、`scripts/workflow_v2.py` 和 `references/` 为准。不得按本文“维持现有四种 agent”、旧阶段顺序或旧接口继续实现功能；新功能必须使用 versioned paper contract、专职 writers、handoff、integration editor、专项 reviewers、revision coordinator 和 final paper reviewer。

> 状态：**设计稿，未实施**。写于 2026-09-12。本文件只规定下一轮改造目标、顺序和验收办法；不要把文中拟议接口误当作现有代码。目标是以 paper-gen 前三模块的真实产物，生成有证据、引用、图表和图注/表注的**英文 ICLR 2024 匿名投稿版 PDF**。尚无真实实验数据时，只能生成明确标记的草稿/链路测试结果，不能宣称论文已完成。

## 0. 一页执行摘要

1. **Writing 自己做论文规划**：从构想、方法/实验计划、已完成实验结果制定英文论文蓝图、章节结构、引用计划、图表计划和插入位置。上游不负责版面。
2. **先辨认事实再写句子**：区分“研究设想”“计划实验”“已运行并可追溯的结果”“模拟测试数据”；数值、曲线、对照和显著性只能来自最后一类中真实已验证的数据。模拟数据仅可用于端到端开发验收。
3. **文献和图表先登记再引用**：正文使用受控 citation/figure/table ID；Python 生成 `.bib`、真实图像、表格、caption、label，并检查双向引用。
4. **LLM 写英文结构化内容，程序负责 TeX**：保留 Writer、Reviewer、Reviser、Formatter 四类现有 agent，但重定职责；不让 LLM 自由生成整篇 LaTeX，也不靠一份巨大的 Part1/Part2 字符串承载所有章节。
5. **正式交付必须连过五关**：输入证据 → 英文/内容 → 引用/图表 → 官方 ICLR 模板与 Tectonic 编译 → PDF 版式和人工抽查。ReportLab 预览不属于正式 PDF。
6. **每阶段可恢复、可分析**：保存原始输入快照、蓝图、各章节、审阅修订、图表源数据/脚本、TeX、编译日志、PDF 检查和分 agent 的时间/token。未变更且通过的阶段不重复调用 LLM。

## 1. 当前真实链路与需修正之处

paper-gen 当前在 `scripts/main_flow.py::_adapt_stages_to_writing` 创建 `stage4_writing/_input/m1.json、m2.json、m3.json`，再由 `scripts/_stage_runner.py::run_writing_stage` 把三个路径交给 writing 的 `scripts/main.py`。

| 写作输入 | 当前从哪里来 | 可用信息 | 当前缺口 |
| --- | --- | --- | --- |
| `m1.json` | `stage1_conception/conception_output.json` | `research_question`、`hypotheses`、`gap_report`、`research_frontier`、`key_papers`、`references`、`domain` 等 | 中文 topic 不能直接成为英文标题；`key_papers` 未必有完整作者/年份，应优先核对 `references`。 |
| `m2.json` | `stage2_planning/method_design.json` + `experiment_plan.json` | 方法组件、创新点、假设绑定；数据集、基线、指标、矩阵、消融、成功判据 | `data_plan`、执行配置、资源信息及相关原文件路径尚未进入 writing；实验计划不是实验事实。 |
| `m3.json` | `stage3_experiment/outputs/experiment-module-output.json` 的简化投影 | `key_findings`、`tables`、`figures`、`statistics`，合成的 `experiment_setup` 和 `result_analysis` | 丢失 `status`、`run_id`、`experiment_runs`、`analysis_records` 以及原始文件/来源；真实 `REPLAN` 可出现空 `experiment_results`。目前空记录时会合成“实验由 paper-gen 自动执行”之类文字，易被误用。 |

现有 `writing_flow.py` 是 `Writer 一次出 Part1/Part2 → Reviewer → Reviser → Formatter 出摘要 → Python 拼 bibitem → TeX → PDF`。它没有图表计划/绘图步骤，也没有受控行内引用和完整 PDF 门禁。`write_outputs.py` 当前把 Introduction+Related Work 塞在 `sections.introduction`、Method+Experiments+Discussion 塞在 `sections.method`，其余相关章节可能为空。现有 `paper.tex.in` 为自写壳，虽引用官方 `.sty/.bst`，不能仅因此宣称版式已经验证。上一轮真实 LLM 测试走完五次请求但 Tectonic 因中文和未转义下划线失败，回退的 ReportLab PDF 是调试预览。

注意：`out/downstream-fixture/.../experiment-module-output.json` 的真实状态为 `REPLAN`、实验结果为 `null`；writing 的 `mock/module3_execution.json` 是模拟结果，其中 Figure 1 只有自然语言描述，不含绘图点。**当前无法用真实数据验收结果图和正式论文。**

## 2. 数据职责：每个上游模块能支持什么

### 2.1 模块一：研究构想（论文为什么写）

- `research_question.topic/scope/success_criteria`：定义研究问题、范围和背景；topic 作为英文标题候选的语义依据，不直接复制。
- `gap_report`、`research_frontier`：供 Introduction 的相关研究缺口和贡献定位；“尚无人做到”这类绝对断言需文献支持。
- `hypotheses`：建立 `H-ID → 可检验主张`；在论文蓝图里映射到方法组件和真实实验结果。
- `key_papers`、`references`：合成初始参考文献目录。优先选择带作者、年份、标题、URL/DOI/arXiv ID 的记录；缺元数据须核实或标待补，不填虚构作者/年份。
- `domain`、资源约束：决定读者背景、术语和合理篇幅；不构成任何实验结果。

### 2.2 模块二：规划（论文做了什么设计、原本打算如何验证）

- `method_design.framework/components/core_mechanism/technical_route`：Method 的机制、算法流程及**方法示意图**的源。组件名要与正文和图标签对齐。
- `innovation_points/hypothesis_coverage/limitations`：贡献与局限清单；每个贡献应绑定假设、实验 ID、指标和可用证据。
- `experiment_plan.objectives/datasets/baselines/metrics/primary_experiments/experiment_matrix/ablation_plan/success_criteria`：Experiments 中数据集、基线、指标、消融和对照关系的计划清单；用来检查结果覆盖和命名一致性。
- 拟扩展的 `data_plan`、`execution_config`（只读输入）用于具体预处理、切分、seed、环境、重复次数的可追溯叙述。若实际运行记录与计划不一致，以模块三的**实际执行记录**说明实际做法，并报告偏差，不能偷偷把计划写成已执行。

### 2.3 模块三：实验（论文实际有什么证据）

- 先读取顶层 `status` 与 `planning_feedback`。`REPLAN`/`FAILED`/未结束的运行不得进入“已验证结果”通道。
- 将实际 `experiment_runs`、`analysis_records`、指标文件、图像和日志作为证据来源。每个数值应追溯到 `run_id + experiment_id + dataset + method + metric + seed + source_file/row`，并标明单位/样本量。
- `key_findings` 与 `result_analysis` 是待核对的解读文本；应对照原始数值，不能直接变成标题/摘要中的定量结论。
- 若只有汇总标量：可做数值表格；**没有横轴逐点数据就不画曲线**，没有重复 seed 就不画误差条/宣称统计显著。
- 模块三未来补齐下载、基线、评估、消融后，writing 再从其受控结果文件读取真实数据。Writing 不负责实现实验，也不根据描述反推数据。

### 2.4 建议的 paper-gen → writing 输入扩展

保留现有 `--module1/--module2/--module3` 三入口，兼容旧调用；增加可选的只读 `--source-manifest <json>`（或在 `m3` 中加入同等 provenance 字段）。Manifest 至少列出原始 m1/m2/m3 路径及哈希、module3 顶层状态、`run_id`、实验产物根目录、允许读取的实际结果文件，以及 m2 的 `data_plan/execution_config` 路径。不得把 `REPLAN` 包装成实验完成。原始上游文件只读，writing 的所有转换物都写入自己的输出目录。

测试模式须显式标记 `evidence_mode=synthetic`，在最终状态、PDF 水印/首页标识和产物清单中可见；不能由“文件里恰好有数字”推断其是真实实验。正式模式为 `evidence_mode=verified`，证据不足时返回 `needs_experiment_data`，保留可写的非实验章节草稿。

## 3. 建议编排：按依赖写作，再按论文顺序排版

| 顺序 | 阶段及负责人 | 输入与工作 | 必须落盘的产物 | 不通过时 |
| --- | --- | --- | --- | --- |
| 0 | **输入清点**（代码） | 校验 m1/m2/m3 形状、顶层状态、原文件路径、文件哈希和模式；辨认真实/模拟/缺失数据 | `stages/00_input_inventory.json`、只读源索引 | `needs_experiment_data` 或结构错误；不伪造字段 |
| 1 | **事实与文献账本**（代码 + 必要时 LLM 辅助核对） | 建立主张—假设—实验—指标—结果链；校验运行来源和文献元数据 | `evidence/claims.json`、`evidence/measurements.json`、`evidence/bibliography.json`、缺口报告 | 缺证据的主张标 `planned/unverified`；正式正文不得当作结果 |
| 2 | **论文蓝图**（Writer/策划步骤） | 以研究问题和可用证据决定英文标题候选、章节任务、长度预算、结果主表、消融表、必要的图和建议插入段落 | `stages/02_blueprint.json`、`figures/plan.json`、`tables/plan.json` | 计划图若无数据，标 `blocked_missing_data`，不得画；可改为可追溯表格 |
| 3 | **文献核对与图表资产**（代码为主） | 生成 `refs.bib`；从已验证数据构建表格；内置 Matplotlib 等绘图脚本生成 PDF/PNG；方法流程图可由结构数据绘制 | `assets/figures/*`、`assets/tables/*`、绘图脚本、源数据快照、`assets/manifest.json` | 引文无对应条目、图无数据/文件/来源时阻断其正文引用 |
| 4 | **Method 与 Experiments**（Writer） | 先写方法与实验，两节都可对照蓝图、组件、实际运行和已登记图表；Results 与 Discussion 清楚区分观察和解释 | `sections/method.json`、`sections/experiments.json`、可选 `sections/results.json` | 没有真实结果时生成带状态的草稿，不写伪实验证明 |
| 5 | **Related Work、Introduction、Limitations、Conclusion**（Writer） | Related Work 只引用文献账本；Introduction 承接已成型贡献；Limitations 分方法和实验限制；Conclusion 不超出已验证结果 | 各节独立 JSON/Markdown | 空节、重复节、无证据的强断言送审修订 |
| 6 | **标题与 Abstract**（Formatter） | 论文内容稳定后产出英文标题和摘要；中文 m1.topic 仅作语义输入；数字与结论必须已在正文和证据中出现 | `stages/06_title_abstract.json` | 含中文、未验证数值、超出正文的贡献则退回定点修订 |
| 7 | **整体审阅与定点修订**（Reviewer + Reviser） | 审论文逻辑、主张证据、英文、章节衔接、引用、图表位置/图注、匿名性和超范围推断；反馈定位到 section/claim/asset ID | `reviews/round-N.json`、修改后的章节、变更说明 | 每次只重写受影响节并重验关联图表/引用；有轮次上限；`revise_exhausted` 不得标 success |
| 8 | **受控渲染**（代码） | 把结构化正文、citation/figure/table ID 和已登记资产转换为安全 LaTeX；只接入官方 ICLR 2024 样式 | `paper.tex`、`refs.bib`、`render_audit.json` | 未解析 ID、裸 `_ % & # $`、重复节、失踪文件等直接报错 |
| 9 | **正式编译**（Tectonic） | 使用 skill 自带 Tectonic 和缓存；日志、输入 `.tex`、官方资产版本随产物保留 | `paper.pdf`、`paper.log`、`compile_report.json` | TeX 失败时只修渲染/定位具体内容；ReportLab 预览只能作调试，不得作为正式交付 |
| 10 | **PDF 发布门禁**（代码 + 首页/抽页人工检查） | 查论文语言、页尺寸、字体嵌入/缺字、匿名性、目录/节/参考文献、图表 caption 与顺序、空白页、溢出或截断；渲染页图检查 | `pdf_validation.json`、页 PNG、最终 `status.json` | 任一硬条件不满足则 `partial`/`failed`，不能宣称 ICLR 2024 合格 |

章节在 PDF 中仍按阅读顺序排列（Title、Abstract、Introduction、Related Work、Method、Experiments/Results、Discussion/Limitations、Conclusion、References、必要附录）；**写作执行顺序**由信息依赖决定，不强行从摘要一路顺写。实际章节安排由蓝图结合内容确定，不预设所有论文都必须拆出完全相同的小节。

## 4. 图、表、插入位置与图注/表注的具体协议

Writing 在阶段 2 基于蓝图创建 `AssetPlan`，条目建议包含：`id`、`kind(figure|table)`、`purpose`、`section`、`placement_after_paragraph_id`、`claim_ids`、`source_paths`、`required_columns`、`status`、`caption_outline`。阶段 3 执行后，登记实际 `path`、`sha256`、`label`、`caption`、`alt/description`、源数据范围、生成脚本版本。段落只写受控 `[figure:method_overview]` / `[table:main_results]` 标记；Python 决定 LaTeX `figure/table` 浮动体和 `\ref`。**位置是 writing 的建议锚点，实际浮动由官方 LaTeX 排版决定**；发布检查核对图表离首次引用不要异常远离，并避免页边界溢出。

按证据来源区分图表：

- **方法示意图**：可依据 m2 方法组件绘制并标为 schematic；图注不含实验数值或未经证明的结果。
- **主结果表/消融表**：严格从同单位、同数据集、同指标、同 seed/汇总口径的实际运行数据构建；baseline 与 ours 的名称对齐 m2/m3。
- **结果曲线/误差图**：必须有逐点数据与横纵轴含义。置信区间/误差棒须有真实重复测量；统计显著性须有可复算方法。
- **图注与表注**：英文、说明数据集/指标/单位/方法/样本或 seed、缩写及高亮规则；由数据与蓝图生成，再由 Reviewer 对照表内数值审核。表格的最佳值加粗仅在可比较的列内计算。
- 没有图像也可生成合格论文，前提是正文不承诺不存在的 Figure；优先制作真正需要的图。不得为了凑图把一句自然语言“Figure 1”当绘图数据。

内置脚本属于 `writing/scripts/plotting/` 或同级明确目录，负责读受控 CSV/JSON、设置统一字号/宽度/配色、输出矢量 PDF 与缩略 PNG；生成脚本和输入数据一起保存在本次运行目录。图的文件名、引用 ID、caption 在 manifest 中唯一。

## 5. 参考文献与行内引用协议

1. 从 m1 `references` 与 `key_papers` 合并去重，用 DOI/arXiv ID/URL/规范化标题识别同一论文；m2 `algorithm_reference`/`baselines[].paper_id` 只作为必须覆盖的引用线索。禁止 LLM 自行创造新书目信息。
2. 书目至少核实标题、作者、年份、来源及稳定 ID/URL；缺项列 `bibliography_gaps.json`。可允许“作者待核”的草稿状态，但**正式 ICLR PDF 不得用假 `Anonymous`、`n.d.` 代填**。
3. 给每条文献稳定 citation key；Writer 只用 `[cite:key]` 等受控标记。涉及方法借鉴、数据集、基线、关键背景的事实应能追溯到文献或上游证据。
4. 渲染器生成 `\citep{key}`/`\citet{key}` 和 `.bib`；按官方 ICLR `.bst` 处理参考文献。编译流程需验证 BibTeX 能正常运行或确认 Tectonic 对本项目实际文献处理方式可用，不得仅因 `paper.pdf` 存在就认为文献已排版。
5. 门禁检查：引用 key 存在、未解析 `?` 引用为零、正文引用与参考文献一致、同一 key 无冲突元数据、未经核实的条目不进入正式版。不是每篇上游关键论文都必须被引用；应由正文需要决定。

## 6. 官方 ICLR 2024 模板与英文格式

- 只读保留官方下载的 `iclr2024_conference.tex`、`.sty`、`.bst`、`math_commands.tex` 等。将官方示例的**导言区、必要命令、匿名投稿设置、document/abstract 结构**作为基准，制定一个经对照检查的注入器。官方示例正文、示例作者及说明段落不可残留；官方 `.sty/.bst` 不改动。应记录官方资产 URL、版本和 SHA256。这里“以官方结构为基础”并非把整个示例正文复制进论文。
- 默认英文、匿名投稿；没有明确的 camera-ready 配置就不启用 `\iclrfinalcopy`，也不插入真实作者。英文约束覆盖标题、摘要、正文、图注、表注、附录和可控的文字表格单元；书目中的外文原题可按真实元数据保留。
- Writer/Reviser 返回结构化段落、公式、列表和引用/资产 ID。普通文本由 Python 转义 `_ % & # $ { } \`；数学公式走单独的受限字段。不要对整段合法 LaTeX 做全局字符串替换，也不要相信“请 LLM 自行正确转义”足够可靠。
- 模板包可扩展到 NeurIPS/ACL，但只在对应年份的完整官方资产、渲染规则和样张测试齐备后注册；本轮默认只验收 ICLR 2024。
- `writing/tools/tectonic/tectonic.exe` 与 `cache/` 已存在于当前 Windows skill；运行时需使用 skill 自己的缓存路径。跨平台分发需要对应平台二进制与缓存验证，不能把 Windows exe 当作全平台方案。

## 7. Agent 职责、状态与反思策略

维持现有四种 agent，先重划输入输出，无需为了每个步骤新设 agent：

| Agent | 建议职责 | 输出契约 |
| --- | --- | --- |
| Writer | 提出有来源的英文蓝图/标题候选、分节撰写 Method、Experiments、Related Work、Introduction 等；建议图表内容/位置但不填造数值 | `section_id`、段落 ID、受控引用/资产 ID、主张 ID、文字/受限数学块；不输出完整 `.tex` |
| Reviewer | 对照事实账本审英文、科学论断、覆盖度、数值、引用、图注、章节冗余及模板匿名要求；按 `section_id/claim_id/asset_id` 给可执行反馈 | `verdict`、定位问题、证据与修订要求；不能通过未知/未完成实验 |
| Reviser | 修改被指明的章节/图注/表注；若问题源于缺数据则报告依赖，不能“修辞性补救” | 修改后的局部结构和解决/未解决问题清单 |
| Formatter | 在正文稳定后写英文标题/Abstract，并审核其与正文数值一致；复杂 TeX 生成由程序负责 | 英文标题/摘要及依据的 claim ID；不产生自由 LaTeX 文档 |

先做代码确定性检查（状态、ID、文献、图表文件、数值、英文字符、重复节、TeX 编译），再把剩余语义问题交 Reviewer。反思循环可保留同一 run 的上下文，但每轮也必须带可重放的事实账本与版本化阶段产物；不要只靠会话记忆。Review `failed`、`revise_exhausted`、编译失败或 PDF 门禁失败都不能映射为 `success`。人审检查点可用于最后审查主张和页面，不应成为自动化链路悄悄放行的后门。

## 8. 状态、日志、断点续跑

建议内部状态：`success`（真实证据 + 所有关卡通过）/ `draft_only`（缺实验或文献而保留草稿）/ `synthetic_test`（模拟数据链路验收）/ `partial`（内容已写但格式/审阅待修）/ `failed`（不可恢复错误）。对 paper-gen 的映射应明确；**只有 `success` 才向 paper-gen 报完整论文**。原有 `paper.pdf` 字段不可仅因为 ReportLab 生成了文件就表示正式论文；可以把调试预览另命名 `preview.pdf`。

每阶段记录：`input_paths + sha256`、阶段/agent 名、prompt/template 版本、开始/结束时间、wall time、模型名、请求次数、prompt/completion/total tokens、结果状态、输出路径、warning/error、依赖阶段。现有 token 汇总的 `by_stage=unknown` 要修成实际阶段；不在日志里记录 API key 或原始凭证。

断点续跑：阶段完成后原子写 `stage.json` 和内容文件；下一次在**输入哈希、蓝图/模板/代码版本、上游阶段状态均相同且该阶段通过验收**时复用。更改图表数据只重画关联资产并重写引用它的章节及后续摘要/审阅/渲染；更改模板只重新渲染/编译/验 PDF；更改 m1 文献只重核文献、相关章节和下游。为强制重跑保留显式 `--force-stage`，但绝不静默复用旧数据。

建议目录：

```text
stage4_writing/
  _input/                 m1.json m2.json m3.json source_manifest.json
  evidence/               claims.json measurements.json bibliography.json gaps.json
  stages/                 00_inventory.json 01_evidence.json 02_blueprint.json ...
  sections/               introduction.json related_work.json method.json ...
  reviews/                round-1.json round-2.json
  assets/
    figures/              矢量图 PDF + 检查用 PNG
    tables/               结构化表与渲染片段
    scripts/              本次实际调用的绘图脚本版本/参数
    manifest.json
  refs.bib
  paper.tex
  paper.pdf               仅正式 TeX + PDF 门禁通过时作为交付
  preview.pdf             可选调试预览，不得冒充正式 PDF
  paper.log compile_report.json render_audit.json pdf_validation.json
  progress.json status.json usage.json writing.log
```

## 9. 分步实施与各阶段验收

**里程碑 A：接口与事实安全。** 先修 paper-gen 的只读输入清单/状态透传，删除“无运行记录也合成已做实验”的表述；writing 接受 `REPLAN`、缺结果、真实结果、synthetic 四种可区分输入。验收：真实 `REPLAN` 返回 `needs_experiment_data`；模拟结果只能进入 synthetic 状态；原 m1/m2/m3 文件不变。

**里程碑 B：证据、文献和论文蓝图。** 定义版本化 JSON 契约，产出可追溯的 claim、measurement、citation 与资产计划；英文标题候选来自研究内容而非直接复制中文 topic。验收：每条已写数值可追到源记录；文献 ID 缺/冲突会失败；无法绘图的描述明确标缺数据。

**里程碑 C：结构化写作和反思。** 将 Part1/Part2 拆为真实独立章节；改四个 agent 的 prompt/schema；正文仅使用已登记的引用和资产 ID。验收：`sections/*` 无伪空节/重复节；Reviewer 发现跨节不一致；Reviser 局部重写；英文/引用/数值检查通过。

**里程碑 D：内置绘图、表格与参考文献。** 从受控数值表生成主结果/消融表及可用的图；参考文献使用核实元数据并通过官方 bst。验收：数值与源完全一致，图表均有文件/英文 caption/首次正文引用，缺文件、缺数据、未知 citation 任一项会阻断正式渲染。

**里程碑 E：官方模板、Tectonic、版式门禁。** 从官方示例导出并对照结构，替换自写简壳；编译后检查 PDF，渲染并人工抽查首页及含图表、参考文献的页。验收：匿名英文 ICLR 2024 PDF，正确页尺寸/字体、无缺字与 TeX fatal、无重复节或模板示例残留、图表和引用可见；ReportLab 预览绝不被误报为正式交付。

**里程碑 F：paper-gen 集成与断点续跑。** 将阶段产物和 token/时间报告接回 paper-gen。先用 synthetic m3 做流程测试，标明不可作为研究成果；待实验同事补齐真实数据下载、基线实现、指标评估、消融和证据文件后再做正式全文验收。验收：上游未变时不重复消耗 Writer/Reviewer token；后段失败只重跑受影响阶段；paper-gen 从真实 `REPLAN` 不会拿到假论文成功状态。

每个里程碑结束向用户报告：**改了什么、当前调用链、具体产物路径、验证方法与结果、已知缺口、下一步**；再进入下一里程碑。真实 LLM 测试前启动 `scripts/llm_volcengine_proxy.py`，用本地 `http://127.0.0.1:8000/v1` 代理接环境中已配置的模型；凭证只传给消费进程，不打印到终端或日志。Python 命令统一 `uv run`。

## 10. 新窗口开始时的建议顺序

1. 先读本文件与当前 `writing/scripts/writing_flow.py`、`paper-gen/scripts/main_flow.py::_adapt_stages_to_writing`、真实 `out/run1/stage3_experiment/.../experiment-module-output.json`、`writing/templates/conferences/iclr2024/iclr2024_conference.tex`。旧 `writing/mock/module3_execution.json` 仅作 synthetic fixture。
2. 先核对工作区已有修改，再按里程碑 A→F 改，避免在未修数据状态时先跑高 token 的全文 LLM。不要修改 `experiment/` 同事模块或原始 stage1/2/3 产物。
3. 每轮先做确定性测试，再做必要的 LLM 调用；实际端到端要经代理。保留所有 run 产物和 token/时间报告。
4. 当真实实验仍为 REPLAN 或尚无绘图数值时，以 `synthetic_test` 验证编译及图表程序；正式论文验收必须等模块三提供真实数据。

## 11. 需要与实验模块制作者最终对齐的交付清单

至少需要：可下载/授权的数据集与版本、划分/seed、可运行的 ours 与 baseline 实现、主实验及消融的运行记录、每个指标的名称/定义/单位、结果表的结构化数值、绘制趋势图所需逐点数据（如有）、重复实验/不确定性数据（如论文要报告）、实际环境和命令、失败运行及偏离计划的说明。每项应能关联实验 ID 和源文件。Writing 可自行决定选哪些图表及放哪里，但不能替模块三生成这些事实。
