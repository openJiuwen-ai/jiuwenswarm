---
name: ppt-creation
description: "当任务要求创建、读取、解析、提取、编辑、修改、合并、拆分、转换或检查 PPT / 幻灯片 / 演示文稿 / deck / slides / presentation / .pptx 文件时使用。本 skill 以内置模板为版面底座，让 Agent 在内容区自由设计；支持原生 PptxGenJS 绘制、可选组件、用户素材、论文原图和合规网络素材。"
---

# PPT Creation

## 核心模型

本 skill 生成的 PPT 由三层组成：

1. **模板母版底座**：画布、主题、字体、页脚及框架页。必须遵守。
2. **Agent 自主设计的内容区**：按内容关系、叙事目的和证据形态自由构图。
3. **参考与组件工具箱**：帮助判断或加速实现，但不规定页面必须长什么样。

始终遵循：

```text
slide = template shell + agent-designed content + optional references/components
```

不要把内容组件配方误认为模板。模板的空白内容区有意保持自由。但标准内容页的底部框架固定：一条主题色一句话总结条、左下页码（密级文字可选）。图文分栏、卡片和系统图等内容结构仍不强制。

新建 deck 默认走 `template-master`：先在 10×5.625 兼容坐标中创作可编辑内容，再用 `scripts/finalize_deck.py` 一条命令合并进模板（内部完成解包、等比放大、挂 `slideLayout7.xml`、清理与打包，无需手工操作拆包目录）。页脚和页码由母版继承，生成代码不得再画第二份；只有明确的兼容场景才使用独立 10×5.625 文件。

## 开始前必须读取

根据任务读取以下文件：

- 所有新建或重设计任务：先读 [base/template-contract.md](base/template-contract.md)。
- 实际生成 PPTX：再读 [base/pptx-safety.md](base/pptx-safety.md)。
- 使用外部事实、论文或图片：再读 [base/sourcing.md](base/sourcing.md)。
- 新建 deck：读 [workflows/create-deck.md](workflows/create-deck.md)。
- 编辑用户提供的既有 PPTX：读 [workflows/edit-existing.md](workflows/edit-existing.md)。
- 选择论文图、网络图或自绘路径：读 [workflows/acquire-visuals.md](workflows/acquire-visuals.md)。
- 交付前：读 [base/quality-gates.md](base/quality-gates.md)。

只在确定要使用某类组件后读取 [components/index.md](components/index.md) 和对应实现说明。先完成页面设计判断，再选择工具。

## 不可违反的边界

### 品牌与模板

- 以 [references/template.pptx](references/template.pptx) 为模板事实来源。它是中性白板模板，由 `scripts/make_blank_template.py` 生成。
- 保持主题色、字体层级、页脚和页码语义。
- 每张标准内容页必须且只能出现一次固定底部框架：主题色一句话总结条与左下页码。模板不含 Logo，不得自行添加。
- 封面主标题必须是主题色 `#4472C4` 且保持单行。自绘或复刻封面时同样适用，不得退回模板占位符的深色默认值。
- 封面、目录、空白内容页、结束页应继承模板语言；若用户提供自己的模板，以用户模板为准。
- `template-master` 输出必须保留模板自身的 Master / Layout 数量；`finalize_deck.py` 以模板为基线校验，丢失即判失败。

### 内容与证据

- 不得编造事实、数据、引用、产品能力或论文结论。
- 数字、图表和外部判断要能回溯到用户材料或可靠来源。
- 论文原图保持原意和比例；不得为了统一风格篡改图内颜色或内容。
- 网络素材要记录来源和使用权；无法确认时使用占位或自绘，不假装已验证。
- **"可回溯"指记录在 `evidence-plan.json` 和交付时的来源说明里，不指在幻灯片上画来源行。** 默认不在页面上放来源文字（论文原图页同样如此），只有用户明确要求时才画。

### 工程质量

- 输出必须可打开、可编辑、无重叠、无越界、无截断、无拉伸图片和占位残留。
- 文字必须可读；不得用极小字号解决内容过载。
- 原生 PptxGenJS 形状、图表和表格优先保持可编辑性。

## 明确不是硬规则的事项

以下都由内容决定，不得写成 deck 级默认门禁：

- 默认左图右文或固定 60/40 比例；
- 每页至少两个区域；
- 所有内容页套用同一种固定版式；
- 所有结构内容必须使用 `addSystemDiagram`；
- 默认至少 10 页；
- 固定组件使用比例或组件调用次数；
- 固定字数、卡片数量或系统图节点数量；
- 一律禁止或一律要求分节页。

## 新建 deck 的主流程

### 1. 理解内容

优先读取用户材料。只有用户没有提供足够事实且任务允许外部调研时才联网补充。

明确：

- 目标受众；
- 使用场景；
- 核心结论；
- 交付用途：阅读型、平衡型或演讲型；
- 是否需要严格沿用用户结构；
- 页数或时长约束。

### 2. Strategist：先设计叙事，再设计页面

选择一个主叙事模式：

- `executive-report`：结论先行，证据与建议支撑；
- `technical-explainer`：概念拆解、机制、边界与实现；
- `research-review`：问题、方法、证据、局限与启示；
- `showcase`：一页一个重点，视觉主导；
- `briefing`：中性、完整、便于扫描。

用户给定的页面顺序和标题优先。模式是组织建议，不得覆盖用户明确结构。

为每页规划：

```yaml
page: P03
role: content
title: 页面标题
core_message: 本页唯一必须记住的结论
summary: 放入固定红色总结条的一句话结论
rhythm: anchor | navigation | dense
content_blocks:
  - 完整事实或论证块
visual_strategy:
  primary: template | user-material | paper | web | native-drawing | component | svg | hybrid | none
  reason: 为什么这种表达最适合本页
  fallback: 首选失败后的替代路径
component_policy: optional | preferred | avoid
layout: 从 references/index.yaml 选定的骨架 name
layout_id: 该骨架的 id
layout_ref: <skill>/references/<id>.<ext>
```

`rhythm` 只控制页面节奏，不绑定版式：

- `anchor`：封面、转折、重大结论或核心视觉；
- `navigation`：目录、路线或结构导航；
- `dense`：数据、架构、对比、研究证据。

### 3. 生成设计契约

在工作目录保存：

- `design-spec.md`：受众、叙事、页面计划、设计理由和素材计划；每 deck 恰好标记 1-2 页 `Hero: yes`，选最核心的论证页；
- `evidence-plan.json`：逐项记录图片/图表/截图要证明什么、怎样获取、审核状态和回退；
- `execution-lock.json`：机器可校验的执行契约，记录模板、品牌固定层、逐页节奏/视觉策略，并通过 `asset_ids` 只允许使用 evidence plan 中的证据。

从 [base/execution-lock-reference.json](base/execution-lock-reference.json) 开始，并使用 [base/execution-lock.schema.json](base/execution-lock.schema.json)。不要锁定单页坐标、固定布局比例或强制组件名；`component_policy` 只表达组件是否适合，不指定必须调用哪个函数。

从 [base/evidence-plan.json](base/evidence-plan.json) 和对应 schema 开始。写完后立即运行：

```bash
node scripts/validate-evidence-plan.js <project>/evidence-plan.json --phase design
node scripts/validate-execution-lock.js <project>/execution-lock.json --phase design
```

校验未通过时不得开始页面生成。不要把 JSON 改写成 YAML 或 Markdown。

### 4. 获取视觉素材

按 [workflows/acquire-visuals.md](workflows/acquire-visuals.md) 逐项处理 evidence plan。优先级不是固定排名，而是证据价值、真实性、可编辑性和表达效率的综合判断。

论文 `paper-figure` 路线必须复用 `scripts/extract_arxiv_visuals_v2_2.py`：准备脚本会读取其 manifest，按 Figure/Table 标签和 Caption 关键词选择；选择不唯一时查看论文 contact sheet 后再批准。不得重新实现一套论文裁图器。

素材状态只使用 `planned / acquiring / ready / used / needs-manual / skipped`。运行 `prepare_evidence.py` 后必须查看 `analysis/evidence-contact-sheet.jpg` 或论文专属 contact sheet，再用 `--approve <id>` 将项目置为 ready。素材实际进入 PPT 后用 `--used <id>` 记录使用状态。视觉方案发生变化时同步修改 `design-spec.md`、`evidence-plan.json` 与 `execution-lock.json`，不允许资源仍标记为 planned 而页面静默改走其他路径。

### 5. Executor：在空白内容母版内逐页生成

- 先实现模板外壳，再设计内容区。
- 标准内容页完成内容后，调用 `Brand.addContentChrome(...)` 一次性添加固定总结条与页脚；内容必须结束在总结条上方。
- 逐页顺序生成，保持同一主 Agent 的上下文和跨页一致性。
- 每页先读取 `execution-lock.json` 中对应的 `core_message`、`rhythm`、`visual_strategy`、`component_policy`、素材状态和候选参考，再决定构图。
- **写每页代码前，先打开该页 `Layout ref` 指向的参考图**，并对照 `references/index.yaml` 中该骨架的 `skeleton` 与 `rules`，理解它的分区、比例和填满方式。只看骨架名不看图，等于没有版面信息。参考图用来建立版面直觉，不要求逐像素复刻。
- 骨架给出分区、比例和区域数量的参考方案；`composition` 决定这一页实际怎么摆。两者冲突时以 `composition` 为准——它更贴近本页内容；但要同步更新 `design-spec.md` 里的骨架选择，别让声明的 `layout id` 和实际产出长期不符。
- 每页只允许引用 `evidence_visual.asset_ids` 列出的证据；AI 插画只能作为 hero/support/background/inline，不能声明为事实 evidence。
- `composition` 必须明确主视觉、次证据和阅读顺序；`evidence_visual` 必须说明真实截图/论文图/数据图/原生关系图承担什么作用。密集或锚点内容页若不使用参考图，必须写具体 waiver，不能让整套 `reference_ids` 默认为空。
- 可以使用裸 PptxGenJS、自定义 SVG、原生图表/表格、可选组件或图片组合。
- 不使用预制小图标库；卡片/列表的识别锚点用 `initial` 首字母徽标或自绘 SVG，不引入装饰性图标。
- 组件与内容不匹配时立即放弃组件，自己绘制。
- **每个卡片、区域、节点里写完整陈述句，不是术语加半行注释。** 单词式短语只当表头或标签用，正文要把机制/条件/量级/边界说透——一张卡两三行、一节两三句。参照量级：标准内容页视觉字量 400-700（真实内部 deck 的密度锚点），容器画得足却只填几个词，交付前 `qa_density.py` 会判 EMPTY 硬错误。宁可少放一个空盒子，也不要让盒子半空。内容量达标后若版面仍显空，再靠放大字号/行距（多数组件已内置稀疏时自动放大）、拉开区域间距或补小图铺满——但先过字量，别用大字号给稀薄内容撑门面。

推荐先生成封面和一张最具代表性的复杂内容页，合并到官方模板并渲染验证后再继续整套 deck，避免系统性错误复制到所有页面。通过目录、章节和锚点结论改变阅读节奏，而不是机械插空页。

生成前运行 `--phase generate`；交付前运行 `--phase deliver`。必要素材未就绪时可以按设计稿中的 fallback 更新方案，但必须同步契约后重新校验。

### 6. Hero 页精修

全 deck 构建通过、几何硬错误清零后，对 design-spec 中标记 `Hero: yes` 的 1-2 页按 [workflows/create-deck.md](workflows/create-deck.md) 第 8 步做单页升格精修：打开该页渲染图与 `references/complex-slide.jpg` 及骨架参考图并排对比，列出差距后重构主区域，至多迭代两轮；改动同步回 `design-spec.md` 的该页 Composition。精修只动 hero 页，不回头重排其他页面。

### 7. QA 与交付

按 [base/quality-gates.md](base/quality-gates.md) 区分硬错误和软建议。硬错误必须清零；软建议由 Agent 结合设计意图判断，不得自动把页面改回固定组件配方。

交付前必须运行 `python3 scripts/qa_geometry.py <output.pptx>` 并清零全部 error。它机械判定重叠、遮挡、重复底部框架和越界——这些缺陷普遍在 0.1 英寸量级，在缩略图总览上根本看不见，只靠人眼 QA 会反复漏掉。

## 版式库使用方式

[references/index.yaml](references/index.yaml) 是版面骨架库，每一项是一种可复用的版面骨架。**它是选型参考，不是强制模板**：用它避免从零构图和自创劣质版面，但页面最终长什么样由内容决定。

建议动作：

1. **通读全部 `name`**，先建立"有哪些骨架可选"的认识。整份文件一次读完，不要只挑几项看。
2. 阅读 `common_rules`，它对所有版式生效。
3. 按内容形态为每页选一个骨架，在 `design-spec.md` 中同时记录 `name`、`id` 和 `image` 的完整路径。**只记名字不记路径，Executor 阶段就拿不到版面信息。**
4. 打开该项的 `image` 确认细节，再对照 `skeleton` 和 `rules` 理解它的分区与比例意图。
5. 内容与骨架不完全吻合时，先看 `variants` 有没有现成变化；仍不吻合就换骨架，或在 `composition` 里写出自己的分区。

骨架按每页的内容形态选。内容形态不同，自然会落到不同骨架上；如果整份 deck 都用同一个骨架，通常说明这一步被省略了，回头确认各页的内容形态是否真的相同。不要因为某个骨架先跑通就把它套到所有页面。相邻内容页连用「同一骨架 + 同一右栏文本样式」是同类信号——连续三四页都是左边一个视觉区、右边一列 1/2/3 编号条目，阅读节奏就塌了；用骨架 `variants`、错开右栏样式（numbered/sectioned）或换骨架打破重复。论文图、实验数据这类证据页优先评估 `architecture-evidence`（外框内左文右图）：论证分节、原图与数据解读、红色结论句在同一个外框里对齐，比「左图 + 右列表」更有表达力。

版式库提供的是：版面分区、区域比例、对齐方式和填满策略的**参考方案**。

版式库不规定：坐标、颜色、区域数量、条目数量和具体内容——这些由 `avoid_when` 之外的内容需求决定。

**松紧边界**：骨架是软的，可以偏离且不需要写 waiver；`common_rules`、品牌固定层和[工程质量要求](#工程质量)是硬的——撑满内容区、对齐轴线、不侵入页脚、不重叠、不越界、不截断，无论选哪个骨架都必须满足。产出好不好看以页面本身判断，不以是否忠实复刻骨架判断。

## 组件使用原则

组件是可选工具。选择顺序：

1. 判断内容关系和证据形态；
2. 决定页面视觉策略；
3. 查看是否存在高度匹配的组件；
4. 匹配则使用或改造，不匹配则裸绘。

品牌框架组件与内容组件分开导入：

```javascript
const Brand = require("./components/brand.js");
const Charts = require("./components/charts.js");
const Diagrams = require("./components/diagrams.js");
const Content = require("./components/content.js");
```

原有 `scripts/components.js` 仍保持兼容，可继续整体导入。

## 编辑既有 PPTX

若用户要求编辑已有 `.pptx`，以现有文件的 Master、Layout、主题和内容为事实来源，走 [workflows/edit-existing.md](workflows/edit-existing.md)。不要为了套用新建 deck 的组件系统而破坏原有模板结构。

## 输出要求

最终至少交付：

- `.pptx` 文件；
- QA 模式和结果；
- 使用外部素材时的来源说明；
- 未能获取的素材、占位或已采用的回退方案。
