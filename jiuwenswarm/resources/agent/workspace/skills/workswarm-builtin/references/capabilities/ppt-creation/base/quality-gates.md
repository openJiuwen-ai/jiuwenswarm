# 质量门禁

QA 检查结果是否合格，不替 Agent 决定页面必须采用哪种构图。

## 模式

先检测：

```bash
soffice --version >/dev/null 2>&1 && pdftoppm -v >/dev/null 2>&1 && echo FULL || echo TEXT-ONLY
```

- `FULL`：结构检查、文本检查、渲染检查。
- `TEXT-ONLY`：结构和文本检查；交付时声明未完成渲染视觉核查。

## 硬错误：必须清零

### 文件与结构

- PPTX 无法打开或页面数量错误；
- `execution-lock.json` 无法解析、未通过当前阶段校验，或页面计划与实际页数不一致；
- `evidence-plan.json` 缺失、存在 Pending/Failed 式未收敛状态、必需项未 ready/used、外部视觉未审核批准，或页面引用计划外证据；
- 资源、关系或媒体缺失；
- 占位符、Lorem、XXXX、示例数据或模板教学页残留；
- 用户要求保留的 Master/Layout、备注或批注被破坏。
- `official-master` 输出没有继承官方画布，或少于模板的 4 个 Master / 7 个 Layout；内容页仍挂在独立 PptxGenJS 空母版上。
- deck 缺少模板封面或模板结尾页（末页不是 Thank You 结尾页）——用户明确要求无结尾页时除外。`finalize_deck.py` 默认顺序已挂结尾页；手工指定 `--order` 时不要漏掉 `t5`。
- 封面主标题为空。官方模板的 `ctrTitle` 占位符出厂即为空且不渲染任何提示文字，漏填的封面看上去"完整"却没有标题。用 `finalize_deck.py --cover-title "..."` 填入设计阶段定下的主标题；该脚本收尾会做 cover-title 硬门禁，为空直接判失败。

### 几何与可读性

**几何缺陷由 `scripts/qa_geometry.py` 机械判定，不靠肉眼。** 它报出的每一条 error 都是硬错误，必须清零：

- `text-collision`：两处文字的实际墨迹重叠；
- `occlusion`：后画的不透明形状压住先画的文字（Z 序错误）；
- `duplicate-chrome`：页面自绘元素与母版继承的页脚/密级/Logo 位置重合，底部框架画了两遍；
- `out-of-bounds`：元素越出画布。

`axis-drift` 与 `summary-intrusion` 是 warning，结合设计意图判断：前者提示各区域各写各的魔数而没有共用轴线常量，后者提示内容压到总结条区域。

**这一步不能用缩略图总览代替。** 0.1 英寸级的重叠和错位在 contact sheet 分辨率下不可见，历史上多次通过人眼 QA 后仍然带着叠字交付。

**文字量由 `scripts/qa_density.py` 机械判定**（视觉字量：中文 1、英文/数字 0.5；`--lock` 读取页面角色，只判 standard-content 页）：

- 标准内容页视觉字量 <300 是硬错误——这种页渲染出来就是半空的；
- 300-400 为 warning，需结合版面说明或补料；
- 校准锚点来自真实内部 deck：内容页 400-700，中位约 480。生成时按这个量级规划内容，不要等 QA 兜底。

**字量不足和版面显空是两件事，处置不同。** `qa_density` 抓的是内容量本身不够（词不成句、盒子里只有术语加半行注释）——这只能靠补料，回内容分析阶段把机制/条件/量级/边界写成完整陈述句。而当内容量已经达标、只是视觉上还偏空时，再用版面手段填充：**放大字号与行距**（多数结构组件已内置内容稀疏时自动放大字号，也可显式调大，但不得超出模板字号上限、不得同页同类区域字号不一）、拉开区域间距撑满内容区、或补相关小图。次序是先保证内容量（过 qa_density），再用字号/版面把它铺满——不能反过来用大字号给稀薄内容撑门面，那种页字量仍低、仍判 EMPTY。

其余几何与可读性问题仍需人工判断：

- 标准内容页未填满内容区，出现大块空白（字量达标但堆在一角同样算）；
- 文字截断、不可读或依赖异常小字号；
- 显式字号低于 execution lock 的 absolute floor；标准内容标题低于 title floor；
- 图片、Logo、论文图被拉伸；
- 表格、图表标签无法阅读。

### 品牌与事实

- Logo、品牌色、字体、页脚或密级不符合模板契约；
- 标准内容页缺少或重复红色一句话总结条；总结条位置、颜色、文字样式被改变；
- 标准内容页缺少右下官方横版 Logo，Logo 位于右上角、被替换、变形或改变固定位置；
- 页面自绘了母版已经继承的页脚、密级、页码或 Logo，导致同一元素出现两次（`qa_geometry.py` 的 `duplicate-chrome`）。封面同样适用：挂在带页脚的内容版式上又自绘一套，就会叠字；
- 使用预制图标库或 `deck.icons.library` 声明非 `none`（内置图标库已移除；识别锚点只用 `initial` 首字母徽标或自绘 SVG）；
- 数据、引用、图表或结论与来源不一致；
- 需要署名的素材未署名；
- 用 AI/占位视觉冒充真实证据。

## 软建议：结合设计意图判断

- 页面视觉重心不明确；
- 连续多页构图过于相似；
- 红色强调过多、层级失效；
- 图片与文字没有共同支持核心信息；
- 参考图被机械照抄或完全没有吸收其设计原则；
- 组件虽然技术正确，但不适合本页内容。

软建议不得自动转化成固定字数、固定区域数量或固定内容组件配方。固定总结条与右下页脚 Logo 属于模板硬约束，不在此例。

## 推荐检查顺序

1. `validate-execution-lock.js --phase design`；
2. `validate-evidence-plan.js --phase design`，运行 `prepare_evidence.py`；
3. 查看 evidence contact sheet，批准合格项并执行两个 `--phase generate` 校验；
4. 生成固定封面和一张代表性复杂内容页；
5. 构建并处理所有硬错误；
6. FULL 模式下渲染这两页，确认模板与视觉方向；
7. 顺序生成剩余页面；
8. 运行完整结构/文本检查并生成缩略图总览；
9. 运行 `qa_geometry.py`，清零全部 error；它负责机械可判的几何缺陷，人眼不必也不可能在缩略图上完成这件事；随后运行 `qa_density.py --lock execution-lock.json`，清零全部 EMPTY；
10. 主 Agent 必须打开缩略图总览，逐页检查标题截断、视觉重心、构图重复、证据可读性和底部框架；只生成图片但不查看不算视觉 QA；
11. 几何 error 清零后，代表性页面还需单页高清渲染确认（`pdftoppm -r 130`）；缩略图只用于看整体节奏，不用于判断单页质量；
12. 修复硬错误，再处理真正影响表达的软建议；
13. 用 `python3 scripts/prepare_evidence.py . --used <id>` 将实际使用的证据标为 used，执行两个 `--phase deliver` 校验。

## 常用命令

```bash
node slides.js
python3 scripts/finalize_deck.py output/content.pptx output/final.pptx   # 合并官方模板一条命令
node scripts/validate-evidence-plan.js evidence-plan.json --phase deliver
python -m markitdown output.pptx
python scripts/audit_pptx.py output.pptx \
  --template references/template.pptx \
  --lock execution-lock.json \
  --evidence-plan evidence-plan.json \
  --report qa/structure.json
# --evidence-plan 打开论文图来源硬校验：按图片字节哈希找出嵌入了论文原图的页面，
# 该页若没有 addSourceNote 画出的“来源：…”文本，报 error 并以退出码 1 结束。
python scripts/opc/thumbnail.py output.pptx qa/contact --cols 4

# 几何自检：重叠 / 遮挡 / 重复底部框架 / 越界 / 轴线漂移
# 坐标已换算回 10×5.625，可直接对照 slides.js；有 error 时退出码为 1
python3 scripts/qa_geometry.py output.pptx

# 文字量自检：标准内容页 <300 视觉字为 EMPTY 硬错误，目标带 400-700
python3 scripts/qa_density.py output.pptx --lock execution-lock.json
python3 scripts/qa_geometry.py output.pptx --slide 6      # 只看某一页
python3 scripts/qa_geometry.py output.pptx --json         # 机器可读

soffice --headless --convert-to pdf output.pptx
pdftoppm -jpeg -r 150 output.pdf slide
```

最终说明 QA 模式、硬错误状态、未处理的实质性软风险和素材回退情况。
