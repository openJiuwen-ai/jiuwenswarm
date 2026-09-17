# 可选组件索引

组件用于加速实现，不负责决定页面设计。先完成页面的 `core_message`、`rhythm` 和 `visual_strategy`，再选择是否加载组件。

## 品牌底座

导入 `brand.js`：

- `HW`、`TY`、`LAYOUT`、`RES`；
- `addSlideTitle`；
- `addSlideFooter`；
- `addSummaryBanner`；
- `addContentChrome`；
- `addOpeningSlide`；
- `addTocSlide`（目录页唯一入口，不得自绘；只有编号徽标 + 章节标题两层，`sections` 只接受 `{num, title}`。章节标题写描述性短语而非四字标签，详见 `../base/template-contract.md`）；
- `addClosingSlide`。

这些函数是当前 10 × 5.625 兼容坐标系中的模板实现。标准内容页优先只调用 `addContentChrome`，由它一次性添加固定总结条、页码、密级和右下 Logo；不要自行覆盖坐标或样式。需要真实 Master/Layout 时使用原始模板工作流。

## 图表与数据

导入 `charts.js`：

- `addDeckChart`：原生柱、线、饼、环图；
- `addChartWithInsights`：图表配解释区的现成实现；
- `addComparisonTable`：可编辑对比表；
- `addKpiRow`：指标行；
- `addSpectrumAxis`：定位轴。

只有在数据和关系匹配时使用。`addChartWithInsights` 是可选现成构图，不是内容页默认结构。

## 图示

导入 `diagrams.js`：

- `addSystemDiagram`：声明式 band/group 系统图；
- `addPipelineDiagram`：线性管线；
- `addHubSpoke`：中心辐射；
- `addTintMatrix`：淡彩分组头分类矩阵；
- `addConvergeFunnel`：汇聚漏斗；
- `addChevronStages`：chevron 阶段带（可挂输出卡）；
- Diagram IR 构造器 `item/group/bandNode/groupBand`。

`addTintMatrix → addConvergeFunnel → addChevronStages` 自上而下组合是参考 deck 的「整页结构大图」页型；demo 第 11 页有成品示例。

这些组件各自表达有限结构。内容超出其模型时，用原生 PptxGenJS 或自定义 SVG 自绘，不要压缩、合并或删减事实迎合组件。

## 内容块与图片

导入 `content.js`：

- `addTextBlock`、`addPanelList`；
- `addCardGrid`、`addIconCards`；
- `addTimeline`；
- `addFigurePanel`；
- `addSourceNote`（引用论文原图的页必须调用，见下）；
- `addRegionHeading`、`addPanel`；
- `iconToPng`、`svgToPng`。

凡是嵌入 `assets/papers/` 下论文原图的页面，必须同页调用 `addSourceNote(pres, slide, { source })`，来源文字取 `evidence-plan.json` 对应条目 `source` 字段的精简形式（如 `Wang et al., arXiv:2406.04692, Fig.1`）。再传 `url`（取 `source_url`）即可让引用可点击。它画在页脚行右侧，不与可选的密级文字冲突。漏调用会被 `audit_pptx.py --evidence-plan` 判为硬错误。

卡片和面板只在真实分组关系存在时使用。本技能不内置预制图标库；需要识别锚点时用 `initial` 首字母徽标，或用 `svgToPng` 转自绘 SVG。

## 兼容入口

旧代码可以继续：

```javascript
const C = require("./scripts/components.js");
```

新代码建议按需：

```javascript
const Brand = require("./components/brand.js");
const Charts = require("./components/charts.js");
const Diagrams = require("./components/diagrams.js");
const Content = require("./components/content.js");
```

或需要全部能力时：

```javascript
const C = require("./components/index.js");
```

完整参数说明见 [../pptxgenjs.md](../pptxgenjs.md)。
