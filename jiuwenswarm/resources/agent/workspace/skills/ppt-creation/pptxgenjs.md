# PptxGenJS 与组件 API

先完成页面设计判断，再查本文件选择实现工具。品牌契约见 [base/template-contract.md](base/template-contract.md)，工程边界见 [base/pptx-safety.md](base/pptx-safety.md)。

## 导入

按需导入：

```javascript
const Brand = require("./components/brand.js");
const Charts = require("./components/charts.js");
const Diagrams = require("./components/diagrams.js");
const Content = require("./components/content.js");
```

完整兼容入口：

```javascript
const C = require("./components/index.js");
// 或旧路径：require("./scripts/components.js")
```

现有组件按 10 × 5.625 英寸坐标系实现。官方母版流程由合并脚本统一等比放大并继承模板版式，不要在生成代码中手工换算到 13.34 英寸。

## 品牌组件

| API | 说明 |
|---|---|
| `addSlideTitle(pres, slide, {title, subtitle?, fontSize?})` | 内容页标题 |
| `addSlideFooter(pres, slide, {pageNum?, confidentialityText?})` | 页脚、页码、Logo |
| `addSummaryBanner(pres, slide, {text})` | 固定红色一句话总结条 |
| `addContentChrome(pres, slide, {summary, pageNum?, confidentialityText?, footerMode?})` | 标准内容页固定底部框架；官方母版用 `footerMode:"master"`，独立兼容文件用默认 `drawn` |
| `addOpeningSlide(pres, opts)` | 当前封面实现 |
| `addTocSlide(pres, {title, sections})` | 当前目录实现 |
| `addClosingSlide(pres)` | 当前结束页实现 |

品牌组件负责模板外壳，不决定内容区构图。标准内容页必须调用一次 `addContentChrome`；不要单页改写总结条或右下 Logo 的坐标、颜色与样式。`master` 模式只画总结条，页码/密级/Logo 在合并后由官方版式继承。

## 数据与图表

| API | 适用边界 |
|---|---|
| `addDeckChart(pres, slide, {type, data, x, y, w, h, ...})` | 有可靠量化数据的原生图表；多余参数透传 pptxgenjs 并覆盖默认样式 |
| `addChartWithInsights(pres, slide, {chart, insights, caveats})` | 一个主图表配解释与边界；只是可选现成构图 |
| `addComparisonTable(pres, slide, {headers, rows, x, y, w, h, ...})` | 真实的多对象/多维度对照 |
| `addKpiRow(pres, slide, {items, x, y, w, rings?})` | 少量可比较指标 |
| `addSpectrumAxis(pres, slide, {ticks, x, y, w, ...})` | 连续程度或定位表达 |

图表数据形状：

```javascript
[{ name: "系列", labels: ["A", "B"], values: [10, 20] }]
```

不得使用 demo、模板教学页或 skill 文档中的数字作为正式数据。

柱图默认紧凑柱宽（`barGapWidthPct: 140`），且数据全为非负时数值轴锚定 0（`valAxisMinVal: 0`）——放任渲染器自动缩放会把轴起点抬到数据最小值附近，造成截断轴。确需放大差异时显式传 `valAxisMinVal`/`valAxisMaxVal` 覆盖，并保证差异解读仍然诚实。图表区建议放在白色圆角卡上并用 `addRegionHeading` 配黑色居中区域标题（或直接用 `addChartWithInsights`），不要让图表裸浮在一个细灰描边矩形里。

## 图示

| API | 表达模型 |
|---|---|
| `addPipelineDiagram` | 少量线性阶段；复杂分支不适用 |
| `addSystemDiagram` | band/group/item 的声明式系统图；分层/管线/架构/闭环结构做主视觉时先试它，层间关系画成边（含反馈回边），表达不了再自绘 |
| `addHubSpoke` | 一个中心与多个平级关联面 |
| `addTintMatrix` | 淡彩分组头三行分类矩阵（族群/子类/条目式层级） |
| `addConvergeFunnel` | 半透明漏斗+下箭头：上方内容汇聚到下方 |
| `addChevronStages` | chevron 阶段带，每阶段可挂白色输出卡 |

`addTintMatrix` → `addConvergeFunnel` → `addChevronStages` 自上而下组合，就是参考 deck「整页一张结构大图」的招牌页型（分类版图汇聚为行动路径）。三者也可单用：矩阵单独作分类页主区域、阶段带单独作路线区域。淡彩只出现在矩阵分组头，正文卡片保持白底红强调。

### System Diagram IR

```javascript
const spec = {
  title: "",
  subtitle: "",
  core_message: "",
  diagram_type: "process",
  bands: [
    Diagrams.bandNode("input", "输入", [
      Diagrams.item("request", "用户请求"),
    ], "neutral", "full_width"),
  ],
  edges: [],
};

Diagrams.addSystemDiagram(pres, slide, { spec, x, y, w, h });
```

IR 支持 `bands`、`groups`、`nodes/items` 和 `edges`。它适合结构能自然映射到这些概念的页面；不匹配时直接使用 PptxGenJS/SVG 自绘。不要为了 IR 合并、删除或改写事实。

## 内容与图片组件

| API | 说明 |
|---|---|
| `addTextBlock` | 结构化文字区域 |
| `addPanelList` | 带标题的列表区域 |
| `addCardGrid` | 真实平级分组的卡片网格 |
| `addIconCards` | 带识别锚点的平级内容；锚点默认是 `initial` 首字母徽标，本技能不内置图标库，需要图形时用 `svgToPng` 转自绘 SVG 传 `iconData` |
| `addTimeline` | 有真实日期/时间的里程碑 |
| `addFigurePanel` | 1–3 张论文图或证据图的等比排版 |
| `addRegionHeading` | 可选区域标题 |
| `addPanel` | 基础矩形容器 |

`addFigurePanel` 的 `figures`：

```javascript
[
  { path: "assets/paper/figure.png", label: "方法框架" },
]
```

图片保持比例；原图内部不按本 deck 色板重绘。

## SVG 转位图

```javascript
const imageData = await Content.svgToPng(svgString, 512, 512);
```

本技能不内置预制图标库。卡片/列表的识别锚点用 `initial` 首字母徽标；确有必要的图形用自绘 SVG 经 `svgToPng` 转入，语义必须准确，不作装饰或页面填充。旧的 `Content.iconToPng` 只用于兼容既有 React icon 代码。

## 裸 PptxGenJS 是正常路径

组件不适合时，直接使用：

- `slide.addText`；
- `slide.addShape`；
- `slide.addImage`；
- `slide.addChart`；
- `slide.addTable`；
- 自定义 SVG。

使用 `Brand.HW`、`Brand.TY` 和模板契约保持品牌一致，但构图可以完全自定义。

## 检查行为

始终启用：

- 参数和数据合法性；
- 缺失资源；
- 几何重叠提示；
- 文本/组件明显超限提示；
- Diagram IR 无效结构。

默认关闭纯审美密度提示。需要探索性建议时：

```bash
PPT_ADVISORY_QA=1 node slides.js
```

这些建议不能作为交付门禁，也不能覆盖页面的 `rhythm` 和设计意图。
