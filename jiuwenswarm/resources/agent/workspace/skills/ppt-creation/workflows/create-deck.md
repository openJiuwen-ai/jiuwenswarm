# 新建 Deck

## 1. 建立任务工作区

**工作区必须建在 skill 目录之外。** `skills/ppt-creation/` 内的任何位置都不允许创建任务文件——skill 目录是所有任务共享的参考材料，任务残留会污染后续生成，且同步工具只维护该目录内的 skill 自身内容。在 workspace 根目录下新建，如 `<workspace>/projects/<task-name>/`。design 阶段的两个校验脚本会直接拒绝位于 skill 目录内的工作区。

建议结构：

```text
<project>/
├── sources/              # 用户材料和调研材料
├── analysis/             # 提取文本、数据和素材清单
├── assets/               # 本 deck 使用的图片、论文图、图标
├── design-spec.md
├── evidence-plan.json
├── evidence-plan.schema.json
├── execution-lock.json
├── execution-lock.schema.json
├── slides.js
├── output/
└── qa/
```

任务下载物、生成图和输出 PPTX 一律留在工作区内，不进入 skill 目录的任何子目录。

## 2. 内容分析

- 提取用户材料的正文、数据、图表、图片和结构。
- 区分来源事实、用户观点、Agent 推断和待验证内容。
- 明确受众、场景、页数/时长、语言、保密级别和核心行动。
- 用户已有结构时保留其顺序和标题，除非用户允许重组。

## 3. Strategist 规划

先建立整套叙事，再规划单页。避免把章节菜单机械转换成页面。

每页至少明确：

- 页面角色；
- 标题；
- 核心信息；
- 固定红色总结条使用的一句话结论；
- 真实内容块；
- 页面节奏；
- 视觉策略与回退；
- 来源和候选参考。

页面数量由内容和用户约束决定，目录页和分节页的取舍自由（这里说的「无硬性配额」指它们，不指结尾页）。但**模板封面和模板结尾页是标准收尾，默认都要有**：deck 首页是 `fixed-cover`、末页是 `fixed-closing`（模板 Thank You 页），两者都在设计阶段就写进页面计划，只有用户明确不要结尾页时才省略。每张内容页都应有明确叙事作用。

## 4. 写设计产物

### design-spec.md

复制 `../base/design-spec-reference.md` 的完整结构——I 到 VII 全部章节、每页全部字段——逐字段填入实际内容。**不许自拟大纲、不许省略字段**；确实不适用的字段显式写 `none` 并给一句原因。缺章节或缺字段视为本阶段未完成。

写完先对照 reference 自检一遍，不合格先回改，再进入后续 JSON 校验：

1. I–VII 章节齐全，每页字段齐全；
2. 每个标准内容页的 Composition 达到元素级（分区比例 + 每区元素清单 + 关键实文，见 reference 中该字段的合格样例）；只有一句「左 X% 图右 Y% 文」的页面必须回改——这类页面在执行阶段最容易偏离声明的骨架。

### evidence-plan.json

复制 `../base/evidence-plan.json` 和 `../base/evidence-plan.schema.json`。每个证据项必须说明：页面、要支撑的 claim、purpose、kind、acquire_via、placement、目标路径、来源/权利、状态、审核结果和 fallback。

论文项使用 `acquire_via: paper-figure`，并提供论文来源与 `paper_selector.label` / `caption_keywords`。产品、代码和官方 Logo 优先使用用户或官方本地文件；网络项必须先确认直接 URL 和权利信息；AI 只用于非事实性视觉。

### execution-lock.json

复制 `../base/execution-lock-reference.json` 为 `execution-lock.json`，同时复制 schema，并保持 `evidence_plan: "evidence-plan.json"`。页面通过 `evidence_visual.asset_ids` 引用 evidence plan，不再在 lock 内重复维护素材表。

记录：

- 模板来源与画布模式；
- 不可改变的主题色、字体、页脚和总结条；
- narrative mode 与 image treatment；
- 字号 floor：`deck.typography_policy` 的 `title_min_pt` / `body_min_pt` / `absolute_min_pt` 三档；
- 每页 role、core message、rhythm、visual strategy、component policy 和 reference IDs；
- 每页 composition、evidence visual，以及必要时的 reference waiver；
- 素材状态、来源、权利说明与 fallback。

不锁单页坐标、区域比例或强制组件名。写完立即执行：

```bash
node ../scripts/validate-execution-lock.js execution-lock.json --phase design
node ../scripts/validate-evidence-plan.js evidence-plan.json --phase design
```

## 5. 参考选择

通读 `../references/index.yaml` 的全部 `name`，建立可选骨架的认识，再为每页选定一个骨架并记录其 `name`。随后打开该项 `image` 确认细节，参考 `skeleton` 与 `rules` 规划分区和比例。内容不吻合时先看 `variants`，仍不吻合就换骨架，或在 `composition` 里写出自己的分区——骨架是选型参考，不是强制模板。

逐页选完后整体看一遍节奏：相邻内容页避免连用「同一骨架 + 同一右栏文本样式」（连续几页都是左视觉区 + 右侧编号列表就是典型信号），用 `variants`、错开文本样式或换骨架打破重复；论文图/实验证据页优先评估 `architecture-evidence`（外框内左文右图）。

## 6. 素材准备

按 `acquire-visuals.md` 获取用户素材、论文图、网络图或其他视觉。先执行：

```bash
python3 ../scripts/prepare_evidence.py .
```

论文路线会调用现有 Figure/Table 提取器并生成 `analysis/papers/<id>/contact_sheet.png`；通用素材总览为 `analysis/evidence-contact-sheet.jpg`。主 Agent 必须查看总览，再批准合格项：

```bash
python3 ../scripts/prepare_evidence.py . --approve <evidence-id>
node ../scripts/validate-evidence-plan.js evidence-plan.json --phase generate
```

## 7. 生成

1. 用 10×5.625 兼容坐标生成内容，模板目标锁定为 `official-master` / `slideLayout6.xml`；
2. 生成封面；
3. 选择一张代表性复杂内容页生成并渲染；
4. 修复系统性问题；
5. 同一主 Agent 顺序生成剩余页面；
6. 每页动手前先打开该页 `Layout ref` 的参考图，对照 `skeleton` 与 `rules` 落实分区；
7. 每页根据设计计划选择原生绘制、组件或图片；
8. 每张标准内容页最后调用一次 `Brand.addContentChrome(pres, slide, { summary, pageNum, footerMode: "master" })`；
9. 保持跨页字体角色、色彩和页脚一致。

**封面和结尾页不在 `content.pptx` 里生成**——它们是官方模板页，由下一步 `finalize_deck.py` 的默认顺序 `t1,s*,t5` 自动挂上（t1 官方封面、t5 官方结尾页）。`content.pptx` 只含标准内容页。但 `execution-lock.json.pages` 仍必须登记 `fixed-cover` 和 `fixed-closing` 两个条目（visual_strategy 用 `template`），否则合并出的最终页数与 lock 计划不一致，交付校验会报页数不符。

生成内容文件后，用一条命令合并进官方模板并打包：

```bash
python3 ../scripts/finalize_deck.py output/content.pptx output/final.pptx \
  --cover-title "Mixture-of-Agents 多智能体模式洞察" \
  --cover-meta "研发部|吴云凯|2026-07-22"
```

它封装了 unpack → merge_slides → fill_cover → clean → pack 全流程，默认顺序 `t1,s*,t5`（官方封面 + 全部内容页 + 官方结束页），等比放大内容并继承官方页脚、Logo 和版式，结束时自检并打印页数/母版/版式摘要。**正常流程不手工解包模板或内容、不阅读拆包 XML**；合并失败需要定位时才加 `--keep-workdir` 保留中间目录。需要目录页时用 `--order "t1,t2,s*,t5"` 这类写法调整。不得把 `--source-layout-mode source` 当作新建 deck 默认值。

**`--cover-title` 不是可选项。** 模板封面的主标题占位符（`ctrTitle`）出厂就是空的，PowerPoint 对空占位符不显示任何提示文字，合并流程本身也不写它——漏传就会交付一张没有标题的封面，且肉眼看上去"完整"。设计阶段定下的封面主标题必须在这一步传进去，取值与 `design-spec` 的封面文案一致，样式按 `base/template-contract.md`（主题色、单行）。`finalize_deck.py` 结束时有 cover-title 硬门禁，为空直接判失败。`--cover-meta` 按 部门|作者|日期 顺序追加到模板已有标签后面，留空的段保持标签原样。合并后若还要改这些文字，走 `editing.md` 的解包-编辑-打包流程。

不使用预制图标库（内置图标资源已从本 skill 移除）。需要识别锚点时用 `initial` 首字母徽标或自绘 SVG，且不得取代架构关系、证据或页面叙事。

不得让不同执行者各自自由设计一批页面后直接拼接。研究或素材检索可以独立，最终页面构图由同一执行上下文统一完成。

## 8. 代表页精修（hero pages）

全 deck 构建通过、QA 硬错误清零后，对 design-spec 中标记 `Hero: yes` 的 1-2 页做一轮升格精修——这两页是整套 deck 被评价的门面，值得单页级投入。

**精修要达成的就是版式库成品参考图的完成度。** 动手前必须打开 `references/complex-slide.jpg`（矩阵分组·漏斗汇聚·路径带，精修的标杆样例）和该 hero 页骨架对应的参考图实际看一遍——整页一张结构大图、淡彩只在分组头、白卡挂阴影、箭头表达汇聚与流转、无空白象限。每张参考图的分区骨架与绘制规则在 `references/index.yaml` 对应条目里，精修时逐条对照落实：

1. 打开该页当前渲染图与上述参考图并排对比，列出差距（结构表达力、分组配色、流转关系、卡片质感）；
2. 精修不受"只用组件"限制：优先用 `addTintMatrix` / `addConvergeFunnel` / `addChevronStages` 等结构组件重构主区域；组件表达不了的关系，直接用原生 shapes 逐元素绘制（淡彩分组头、汇聚漏斗、chevron 带、带阴影白卡是参考 deck 的核心词汇）；
3. 精修后重新渲染该页并与参考图再对比一次，仍有明显差距再迭代一轮（精修每页至多两轮，不无限循环）；
4. 精修只动 hero 页，不回头重排其他页面；改动同步回 `design-spec.md` 的该页 Composition。

## 9. QA 与交付

按 `../base/quality-gates.md` 执行。页面真正使用素材后运行 `python3 ../scripts/prepare_evidence.py . --used <evidence-id>`。交付前运行 evidence plan、execution lock、`audit_pptx.py` 和缩略图总览检查，输出 PPTX、QA 说明、素材来源和回退清单。
