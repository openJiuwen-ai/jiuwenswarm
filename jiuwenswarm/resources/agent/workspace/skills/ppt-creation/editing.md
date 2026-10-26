# 编辑现有演示文稿（XML 兜底路径）

**仅当用户明确要求编辑一份现有 `.pptx` 时才走本工作流。**新 deck 一律用 pptxgenjs——见 [SKILL.md](SKILL.md) 和 [base/template-contract.md](base/template-contract.md)。

pptxgenjs 生成的页面不要用 XML 编辑；合并时也不要把 pptxgenjs 页面重新挂到模板 layout 上。

编辑现有演示文稿的步骤：

1. **分析现有页面**：
   ```bash
   python scripts/opc/thumbnail.py existing.pptx
   python -m markitdown existing.pptx
   ```
   看 `thumbnails.jpg` 了解各页版式，看 markitdown 输出了解占位文本。

2. **规划页面映射**：给每个内容章节选一页模板页。

   ⚠️ **版式要多样**——单调的演示文稿是常见失败模式。不要默认全用「标题 + 要点」页。主动找：
   - 多栏版式（两栏、三栏）
   - 图文组合
   - 满幅图片配文字叠层
   - 引用页 / 醒目标注页
   - 分节页
   - 大数字/指标页
   - 图标网格或「图标 + 文字」横排

   **避免：**每一页都重复同一种文字密集版式。

   按内容类型匹配版式（如：要点 → 列表页，团队信息 → 多栏页，用户评价 → 引用页）。

3. **解包**：`python scripts/opc/unpack.py existing.pptx unpacked/`

4. **搭建演示文稿结构**（自己做，不要交给 subagent）：
   - 删除不需要的页（从 `<p:sldIdLst>` 移除）
   - 复制要复用的页（`add_slide.py`）
   - 在 `<p:sldIdLst>` 里调整页序
   - **所有结构性改动必须在第 5 步之前完成**

5. **编辑内容**：逐个更新 `slide{N}.xml` 里的文字。
   **这一步可以用 subagent**——每页是独立的 XML 文件，可以并行编辑。

6. **清理**：`python scripts/opc/prune.py unpacked/`

7. **打包**：`python scripts/opc/pack.py unpacked/ output.pptx --original existing.pptx`

---

## 脚本

| 脚本 | 用途 |
|--------|---------|
| `unpack.py` | 解包 PPTX 并格式化 XML |
| `opc/add_slide.py` | 复制页面或从 layout 新建页面 |
| `opc/prune.py` | 删除未被引用的部分 |
| `pack.py` | 带校验地重新打包 |
| `opc/thumbnail.py` | 生成页面缩略图网格 |

### unpack.py

```bash
python scripts/opc/unpack.py input.pptx unpacked/
```

解包 PPTX，格式化 XML，转义弯引号。

### opc/add_slide.py

```bash
python scripts/opc/add_slide.py unpacked/ slide2.xml      # 复制页面
python scripts/opc/add_slide.py unpacked/ slideLayout2.xml # 从 layout 新建
```

打印出 `<p:sldId>`，自己插到 `<p:sldIdLst>` 的目标位置。

### opc/prune.py

```bash
python scripts/opc/prune.py unpacked/
```

删除不在 `<p:sldIdLst>` 里的页面、未引用的媒体、孤儿 rels。

### pack.py

```bash
python scripts/opc/pack.py unpacked/ output.pptx --original input.pptx
```

校验、修复、压缩 XML，重新编码弯引号。

### opc/thumbnail.py

```bash
python scripts/opc/thumbnail.py input.pptx [output_prefix] [--cols N]
```

生成带页面文件名标注的 `thumbnails.jpg`。默认 3 列，每张网格最多 12 页。

**只用于模板分析**（挑版式）。视觉 QA 要用 `soffice` + `pdftoppm` 生成全分辨率单页图——见 SKILL.md。

---

## 页面操作

页序在 `ppt/presentation.xml` → `<p:sldIdLst>`。

**调序**：重排 `<p:sldId>` 元素。

**删除**：移除 `<p:sldId>`，然后跑 `clean.py`。

**新增**：用 `add_slide.py`。绝不手工复制页面文件——脚本会处理备注引用、Content_Types.xml 和关系 ID，手工复制必漏。

---

## 编辑内容

**Subagent：**如果可用，就在这一步用（第 4 步完成之后）。每页是独立 XML 文件，可以并行编辑。给 subagent 的提示词里要包含：
- 要编辑的页面文件路径
- **「所有修改用 Edit 工具」**
- 下面的排版规则和常见坑

每一页：
1. 读该页 XML
2. 找出全部占位内容——文字、图片、图表、图标、说明文字
3. 逐个替换成最终内容

**用 Edit 工具，不用 sed 或 Python 脚本。**Edit 工具强制你明确「替换什么、在哪里」，可靠性更高。

### 排版规则

- **所有标题、小标题、行内标签都加粗**：在 `<a:rPr>` 上用 `b="1"`。包括：
  - 页标题
  - 页内小节标题
  - 行首的行内标签（如「状态：」「说明：」）
- **绝不用 Unicode 圆点（•）**：用正规的列表格式 `<a:buChar>` 或 `<a:buAutoNum>`
- **项目符号保持一致**：让符号继承 layout 的设置，只在需要时指定 `<a:buChar>` 或 `<a:buNone>`。

---

## 常见坑

### 模板适配

源内容比模板条目少时：
- **把多余元素整个删掉**（图片、形状、文本框），不能只清空文字
- 清空文字后检查有没有落单的视觉元素
- 跑视觉 QA 抓数量不匹配

替换成不同长度的文字时：
- **变短**：通常安全
- **变长**：可能溢出或意外折行
- 改完文字跑视觉 QA
- 必要时截短或拆分内容，迁就模板的设计约束

**模板槽位 ≠ 源条目数**：模板有 4 个团队成员而源数据只有 3 人时，把第 4 人的整组元素（图片 + 文本框）都删掉，不能只删文字。

### 多条目内容

源内容有多个条目（编号列表、多个小节）时，每条建独立的 `<a:p>` 元素——**绝不拼成一个字符串**。

**❌ 错误**——所有条目挤在一个段落里：
```xml
<a:p>
  <a:r><a:rPr .../><a:t>Step 1: Do the first thing. Step 2: Do the second thing.</a:t></a:r>
</a:p>
```

**✅ 正确**——独立段落，标题加粗：
```xml
<a:p>
  <a:pPr algn="l"><a:lnSpc><a:spcPts val="3919"/></a:lnSpc></a:pPr>
  <a:r><a:rPr lang="en-US" sz="2799" b="1" .../><a:t>Step 1</a:t></a:r>
</a:p>
<a:p>
  <a:pPr algn="l"><a:lnSpc><a:spcPts val="3919"/></a:lnSpc></a:pPr>
  <a:r><a:rPr lang="en-US" sz="2799" .../><a:t>Do the first thing.</a:t></a:r>
</a:p>
<a:p>
  <a:pPr algn="l"><a:lnSpc><a:spcPts val="3919"/></a:lnSpc></a:pPr>
  <a:r><a:rPr lang="en-US" sz="2799" b="1" .../><a:t>Step 2</a:t></a:r>
</a:p>
<!-- 后续依此类推 -->
```

从原段落拷 `<a:pPr>` 保住行距。标题用 `b="1"`。

### 弯引号

unpack/pack 会自动处理。但 Edit 工具会把弯引号转成 ASCII。

**新增带引号的文字时，用 XML 实体：**

```xml
<a:t>the &#x201C;Agreement&#x201D;</a:t>
```

| 字符 | 名称 | Unicode | XML 实体 |
|-----------|------|---------|------------|
| `“` | 左双引号 | U+201C | `&#x201C;` |
| `”` | 右双引号 | U+201D | `&#x201D;` |
| `‘` | 左单引号 | U+2018 | `&#x2018;` |
| `’` | 右单引号 | U+2019 | `&#x2019;` |

### 其他

- **空白**：`<a:t>` 里有首尾空格时加 `xml:space="preserve"`
- **XML 解析**：用 `defusedxml.minidom`，不用 `xml.etree.ElementTree`（会破坏命名空间）

---

## 合并工作流

**仅**在 XML 编辑页与 pptxgenjs 生成页需要混排时使用。这是混合路径——不是默认路径。若只是把生成内容并入官方模板（新建 deck 的标准场景），用 `scripts/finalize_deck.py` 一条命令即可，不走本节。

### 阶段 A — XML 编辑页

按 上文工作流 完成结构调整和内容更新，但**先不要 pack**。保留解包目录等待合并：

```bash
python scripts/opc/unpack.py existing.pptx unpacked/
# ... 编辑页面 ...
ls unpacked/ppt/slides/slide*.xml | sort -V
```

### 阶段 B — pptxgenjs 内容页

在一个独立的 pptxgenjs 演示文稿里生成内容页：

```bash
node content_slides.js                  # 生成 content.pptx
python scripts/opc/unpack.py content.pptx unpacked_content/
ls unpacked_content/ppt/slides/slide*.xml | sort -V
```

### 第 1 步 — 规划页面顺序

列出两个来源的页面，决定最终顺序。用 `tN`（XML 编辑页，按排序第 N 页）和 `sN`（pptxgenjs 页，第 N 页）写一个逗号分隔的字符串：

```
示例："t1,t2,t3,s1,t4,s2,s3,t5,t6"
  t1 = 开场页（XML 编辑）
  t2 = 第 1 节分节页（XML 编辑）
  t3 = 简单内容页（XML 编辑）
  s1 = 复杂内容页 1（pptxgenjs）
  t4 = 第 2 节分节页（XML 编辑）
  s2 = 复杂内容页 2（pptxgenjs）
  s3 = 复杂内容页 3（pptxgenjs）
  t5 = 简单内容页（XML 编辑）
  t6 = 结尾页（XML 编辑）
```

### 第 2 步 — 执行合并

```bash
python scripts/merge_slides.py \
  --target unpacked/ \
  --source unpacked_content/ \
  --order "t1,t2,t3,s1,t4,s2,s3,t5,t6"
```

合并脚本会把 pptxgenjs 页面连同其自带的 slide layout、master、theme、media 一起拷入目标 deck。源页面（`sN`）必须保持与目标 deck 的 layout 相互独立；不要手动改指向，否则模板图形会透进生成页。

脚本还会把拷入的 pptxgenjs 页面 XML 从源演示文稿尺寸缩放到目标画布尺寸。

### 第 3 步 — 核对源页面独立性

```bash
grep -R "slideLayouts" unpacked/ppt/slides/_rels/slide*.xml.rels
```

如果某个生成页出现了不该有的模板图形，检查该页的 `.rels` 文件。它应该引用合并时拷入的 pptxgenjs layout，而不是目标 deck 原有的 layout。

### 第 4 步 — 清理并打包

```bash
python scripts/opc/prune.py unpacked/
python scripts/opc/pack.py unpacked/ output.pptx --original existing.pptx
```

