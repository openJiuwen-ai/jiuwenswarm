# Design Spec Reference

复制本结构到任务工作区的 `design-spec.md`，用实际内容替换说明。只写已确认或可追溯的信息。**每个字段都必须填**：一行装不下就写多行；确实不适用的字段显式写 `none` 并给一句原因，不许删字段。冒号后的斜体说明是填写要求，落笔时替换成实际内容，不要保留。

## I. Project

- Project:
- Audience: *具体到职能与决策层级（如「企业 AI / Agent 架构、研发与技术管理人员」），不写「相关人员」*
- Scenario: *使用场合 + 演讲/阅读比重*
- Language:
- Delivery purpose: `text | balanced | presentation`
- Page count / duration: *页数与预计讲述时长*
- Confidentiality:
- User structure authority: *用户指定了什么（主题/页数/素材）、授权重组什么——后续页面取舍以此为据*

## II. Narrative

- Narrative mode: `executive-report | technical-explainer | research-review | showcase | briefing`
- Core conclusion: *一两句**可辩护的完整判断**（含关键机制或数字），整套 deck 都在论证它；主题复述（「介绍 X 的原理与应用」）视为未填*
- Story/argument outline: *用「→」串出完整论证链，一步对应一到两页，能看出每章回答什么问题*
- Source fidelity: *按事实类别写来源边界：哪类事实只采用哪个来源的哪个版本，哪些是本报告自行整理并须标注*

## III. Template

- Template source:
- Canvas mode: `official-master | 10x5.625-compatible`
- Authoring / merge mode: `10x5.625-scaled + template-layout | official-size | source-layout`
- Content layout / footer mode:
- Cover / TOC / content / ending treatment: *四类页各怎么处理；省掉某类（如不设目录）要写原因*
- Fixed content-page summary/footer treatment:

## IV. Cross-page Design System

- Color behavior: *写「什么颜色承担什么语义」（强调色只用于哪里、辅助色区分什么、背景基调），不是罗列色值*
- Typography roles: *各文字角色的字号区间：标题 / 正文 / 来源 / 大数字*
- Readability floors: title / body / absolute minimum
- Image treatment: *裁切策略（no-crop / contain）、边框、图注的全 deck 统一做法*
- Chart/table treatment: *原生可编辑优先；数字逐字引用来源；禁 3D、截断轴等误导手法*
- Source/credit treatment: **默认不在幻灯片上画来源行**——包括论文原图页。来源一律记录在 `evidence-plan.json` 的 `source_rights` 和交付时的来源说明里，页面保持干净。只有用户明确要求页面显示出处时才画

## V. Evidence Resources

| ID | Page | Claim/Purpose | Kind | Acquire Via | Placement | File/query/paper | Status/Review | Source/license | Fallback |
|---|---|---|---|---|---|---|---|---|---|

本表解释设计意图，机器事实写入 `evidence-plan.json`。Claim/Purpose 写这份素材**支撑哪句论断**，不是写素材是什么。Status 只使用：`planned | acquiring | ready | used | needs-manual | skipped`。外部图片达到 `ready/used` 前必须是 `review.status: approved`。执行过程中改变视觉方案时，同步修改本表、evidence plan 和 execution lock。

## VI. Page Plan

### P01 — <title>

- Role: `fixed-cover | toc | section | standard-content | fixed-closing` — *首页用 `fixed-cover`、末页用 `fixed-closing`（模板结尾页），两者默认都要有，用官方模板页不在 content.pptx 里生成；中间是 standard-content，toc/section 按需*
- Hero: `yes | no` — *每 deck 恰好标 1-2 页 yes，选最核心的论证页；hero 页在构建通过后按工作流第 8 步做单页精修，其 Composition 必须写到元素级*
- Core message: *一句有主谓的完整判断——Executor 不看其他材料也知道本页要证明什么；不是标题换写*
- Summary banner: 本页一句话结论；标准内容页必填。*是结论句，不是标题重复*
- Rhythm: `anchor | navigation | dense`
- Content blocks: *列出真实信息点：数字、机制、对照关系、边界条件，Executor 可直接上版；「三个要点」「若干案例」这类占位视为未填。标准内容页的 blocks 合计要能支撑 400-700 视觉字的版面（quality-gates 密度锚点，交付前 `qa_density.py` 机检 <300 硬错误）——每个卡片/区域是 2-3 行完整陈述句，不是一个术语加半行注释；只够两三行话说明本字段没写完，回内容分析阶段补料*
  -
- Visual strategy:
  - Primary: `template | user-material | paper | web | native-drawing | component | svg | hybrid | none`
  - Reason:
  - Fallback:
- Layout: 从 `references/index.yaml` 选定的骨架 `name`
- Layout id: 该骨架的 `id`
- Layout ref: `<skill>/references/<id>.<ext>` — 写代码前必须打开这张图
- Layout variant: 使用了 `variants` 中的哪一条；未使用写 none
- Component policy: `optional | preferred | avoid`
- Composition: 这一页实际怎么摆。**这是决定产出的字段，标准内容页必须写到元素级**：分区比例 + 每区元素清单（组件/图/文本样式）+ 关键实文（区域标题、图题、KPI 数字、结论句的原文）。合格样例：「整页大圆角外框，框内副标题『性价比、延迟与两条修正路线』；左 50% 三节论证（红方块小标题：成本优势 / TTFT 局限 / 演进方向），节内箭头符号列论点；右 45% 上部 Figure 5 原图（contain，图题『性能-成本 Pareto 前沿』），下方 3 条数据解读，末行红色结论句『从最小聚合配置起步，按增益再扩』」。只有一句「左 X% 图、右 Y% 文」视为未填完——元素内容留白，Executor 就会现场发挥，声明的骨架也会落地走样。与骨架冲突时以本字段为准，但要回头把 `Layout` / `Layout id` 换成更贴合的那个
- Evidence visual: 类型、素材 ID 及在论证中的作用
- Layout waiver: 确实无骨架适用时写具体原因（骨架可自由偏离，此项只用于完全不参考版式库的情况）
- Sources: *具体到文档名/章节/表号（如「MoA 论文 Table 2/3/4」「官方 README」），不写「官网」「网络资料」*
- Speaker-note intent: *讲者备注要传达的一句话：澄清什么误解、强调什么边界*

为每页重复本节，**每页所有字段齐全**。不要只写占位式提纲；`content blocks` 应包含 Executor 能直接使用的真实信息。

`Layout ref` 是给 Executor 用的：写该页代码前打开这张图，对照 `index.yaml` 中该骨架的 `skeleton` 和 `rules` 落实分区。只写骨架名不写路径，等于没有传递版面信息——名字本身推不出几个区域、什么比例、有没有通栏带。

每页都必须在 `execution-lock.json.pages` 中有对应机器条目。设计稿解释为什么，execution lock 只保存最终决定；两者冲突时必须先同步，不能让 Executor 猜测。

## VII. QA Intent

- Representative content page: *选信息密度最高、素材类型最全的一页作视觉 QA 代表，并说明为什么*
- Known layout risks: *逐页点名可预见的拥挤、字号、图注风险（如「P03 矩阵六格文字易挤」）*
- Known sourcing risks: *数据时效、口径与快照限制（如「论文数据为 2024 模型快照，不能表述为当前排行榜」）*
- Required preservation checks:
- FULL / TEXT-ONLY expectations:
