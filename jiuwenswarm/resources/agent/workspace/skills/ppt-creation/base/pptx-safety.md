# PptxGenJS 与 PPTX 安全边界

## 画布与坐标

- 现有兼容组件按 10 × 5.625 英寸创作；使用前设置 `pres.layout = "LAYOUT_16x9"`。
- 官方模板原始画布为 12196750 × 6858000 EMU（约 13.34 × 7.50 英寸）。默认合并流程会等比放大坐标与字号，并把内容页挂到官方 `slideLayout6.xml`；不要手工再缩放一次。
- 标准内容页必须预留固定总结条与页脚区；10 × 5.625 兼容坐标中，正文/图表/图片必须在 `y=4.65` 以上结束。
- 官方母版合并模式不得绘制独立页脚/Logo；使用 `footerMode: "master"`。兼容独立文件才使用默认 `footerMode: "drawn"`。
- 总结条与页码的坐标和样式不得由单页构图覆盖；模板不含 Logo，不得自行添加。

## 文本

- 为文本框预留真实换行高度，尤其是中文、长英文单词和混排内容。
- 优先重写、重排、扩大容器或拆页，不用极小字号挽救过载。
- 同一结构角色的字号和字体在全 deck 保持一致；局部大数字、引语和标签可建立独立角色。
- `addSlideTitle` 的 `subtitle` 是本页第二句话，不是文献条目：不得以 `Wang et al., 2024` 这类
  作者引用开头，要提论文就写全名；出处放页脚的 `addSourceNote`。详见
  `template-contract.md` 的「内容页副标题」，由 `audit_pptx.py --lock` 强制。
- 项目列表使用 PptxGenJS 的列表格式，不用 Unicode 圆点模拟列表。

## 图片

- 始终保持原始宽高比；明确选择 contain 或 crop，不直接拉伸。
- 论文图、公式、Logo 和信息图默认 contain；照片可在不损害主体的前提下 crop。
- 使用图片前确认路径存在、分辨率足够、方向正确。
- 不把整页内容栅格化为一张图；需要可编辑的图表、表格、形状和文本应保留原生对象。
- **嵌入 `assets/papers/` 下论文原图的页面，必须同页调用 `addSourceNote(pres, slide, { source })`。**
  来源文字取 `evidence-plan.json` 对应条目 `source` 字段的精简形式，例如
  `Wang et al., arXiv:2406.04692, Fig.1`；组件自动加 `来源：` 前缀，画在官方页脚行右端、
  页脚右端。一页多图时合并成一条引用，组件每页只允许调用一次。
- 同时传 `url`（取该条目的 `source_url`）可让引用可点击：只有引用正文带下划线和超链接，
  `来源：` 标签保持纯文本，颜色仍是页脚灰而非主题的超链接蓝——页脚 chrome 不应该比旁边
  的密级文字更抢眼。url 必须是绝对 http(s) 地址，否则组件直接报错。
- 该规则由 `audit_pptx.py --evidence-plan evidence-plan.json` 强制：它按图片字节哈希匹配
  论文图资产，命中却找不到来源文本的页面直接判为硬错误。自绘图/原生图表页不受此约束。

## 图表与表格

- 数值必须来自用户材料或可靠来源。
- 图表类型必须匹配数据关系；不为视觉效果选择会误导的轴、比例或形状。
- 表格行列、图表标签、图例和来源必须在最终文件中可读。
- 不用示例数据、组件 demo 数字或模板配色示意页的数据填充正式 deck。

## 形状与 SVG

- PptxGenJS 颜色使用不带 `#` 的 6 位 HEX；SVG 内使用带 `#` 的标准 HEX。
- 避免依赖 PowerPoint 版本不稳定的滤镜、复杂 mask 和不可预测的字体替换。
- 自绘 SVG 要有明确 viewBox、足够描边宽度和可嵌入字体策略。

## 选项对象不可复用（PptxGenJS 会就地改写）

PptxGenJS 生成 XML 时**直接修改传入的 options 对象**，而不是先拷贝。`shadow` 最典型：
`blur ×12700`、`offset ×12700`、`angle ×60000`、`opacity ×100000`。

所以**任何 options 子对象都不能在多个 `addShape` / `addText` / `addImage` 之间共享引用**。
共享会让第 2 个形状的数值被二次换算、第 3 个被三次换算，很快越过
`ST_PositiveFixedAngle`（上限 21600000）等类型上限；PowerPoint 打开时报
"found a problem with content"，修复时把整段属性删掉。

```js
// ✗ 单例常量：第 2 个卡片起数值成倍炸开
const CARD_SHADOW = { type: "outer", blur: 3, offset: 1, angle: 90, opacity: 0.3 };
cards.forEach(c => slide.addShape(SHAPE, { ..., shadow: CARD_SHADOW }));

// ✓ 每次新建
const cardShadow = () => ({ type: "outer", blur: 3, offset: 1, angle: 90, opacity: 0.3 });
cards.forEach(c => slide.addShape(SHAPE, { ..., shadow: cardShadow() }));
```

同样适用于被复用的 `line`、`fill`、`glow` 等配置对象——用工厂函数或 `{ ...PRESET }`
展开，不要直接传共享引用。

## 构建检查

- 非法参数、缺失资源、悬空关系、重复 ID、无效数值应立即报错。
- **打包必须走 `pack.py --original <模板>` 且不得使用 `--validate false`。** 这是唯一会对
  `ppt/slides/*.xml` 做 XSD 校验并自动修正 PptxGenJS 已知产物的关卡；绕过它，越界数值和
  段内重复 `<a:pPr>` 会一路带进交付文件，只有 PowerPoint 会告诉你。
- 交付前用 `validate.py <产物>.pptx` 复核一次，输出必须是 `All validations PASSED!`，且
  `-v` 下 `Skipped (no schema)` 应为 0——不为 0 说明有部件根本没被校验。
- 元素重叠、越界、图片变形、文本截断属于硬问题。
- 页面空白、密度和构图相似度属于设计判断，只作为软建议。
- 交付前运行 `audit_pptx.py`；官方模式少于模板的 4 个 Master / 7 个 Layout、画布不一致、显式字号低于 absolute floor 或标准内容页缺总结条都属于硬错误。
