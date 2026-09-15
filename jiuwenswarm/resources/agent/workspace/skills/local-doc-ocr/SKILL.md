---
name: local-doc-ocr
version: 1.0.0
author: 海河（李昂）
license: MIT
category: 文档处理 / OCR
platforms: [workbuddy, claude-code, cursor, ima, qclaw]
description: 本地离线 OCR——将扫描件 PDF/图片提取为文字。双引擎：(A) RapidOCR 纯本地离线一键出 .md（默认保底、不联网、不占 token、可批量）；(B) 支持多模态读图能力的 Agent 逐页读图（高质量，擅长表格/手写/印章，如 WorkBuddy 的 Read 工具）。全程本机处理，适合涉密/敏感与隐私资料。触发词：OCR、识别、读PDF、读一下这份、把这个读出来、转成文字、扫描件提取、图片转文字、识别扫描件、PDF转文字、手写识别、提取图片里的字、这份文件读不出来、资料入库（如喂给知识库）时遇到扫描件。注：用户说"识别隐患/安全隐患"属安全检查类技能，本技能只做文字提取。
agent_created: true
---

# 本地文档 OCR（扫描件 / 图片 → 文字）

## 30 秒快速开始
1. 准备一份扫描件 PDF 或图片（技能包内已附 `examples/sample_scan.png` / `sample_scan.pdf` 可直接试跑）。
2. 终端运行（首次会自动装好依赖，需联网一次）：
   ```bash
   python scripts/ocr_offline.py "examples/sample_scan.png"
   ```
3. 同目录生成 `sample_scan_OCR.md`，即为识别结果；`examples/sample_scan_OCR_example.md` 是预期效果示意，可对照。
4. 含表格 / 手写 / 印章、想要更高质量？走「路线 B：多模态读图」。
5. 涉密资料？全程用「路线 A（RapidOCR）」即可，数据不出本机。

## 运行环境要求
- **Python 3.8+**：本技能无法替你安装 Python 解释器，请先确保本机已安装。
- **能执行本地脚本的 Agent**：本技能由 Python 脚本驱动，需运行环境支持调用 `python` / `bash`（如 WorkBuddy、Claude Code、Cursor）。纯对话型 Agent（不能运行本地脚本）不适用。
- **引擎 B（多模态读图）**额外需要 Agent 支持图片输入（如 WorkBuddy 的 Read 工具）；不支持多模态时自动退回引擎 A。

## 依赖自动配置（首次使用）
> ⚠️ **透明说明**：本技能首次运行会**联网一次**，仅安装声明过的 4 个白名单依赖（`rapidocr-onnxruntime` / `pymupdf` / `pillow` / `numpy`），装好之后全程离线、不再联网，**绝不安装任何未知包**。如环境不允许自动安装，可手动 `pip install -r requirements.txt` 预装（效果等价）。

本技能带**环境自检 + 自动安装**机制（A 档）：
- 首次运行脚本时，`setup.py` 会检测 `rapidocr-onnxruntime / pymupdf / pillow / numpy` 四个依赖，**缺失哪个自动从 PyPI 安装哪个**（优先清华镜像，PyPI 官方兜底）。
- ⚠️ **首次自动安装需联网一次**（仅安装声明过的白名单依赖，不装任何未知包，透明可控）；装好之后全程离线、不再联网。
- 也可手动预装（等价效果）：
```bash
pip install -r requirements.txt
```
依赖清单（详见 `requirements.txt`）：
- `rapidocr-onnxruntime`：引擎 A 的离线 OCR 核心（ONNX，纯 CPU）
- `pymupdf`：PDF 渲染为图片
- `pillow`、`numpy`：图像处理与计算
- 若自动安装失败，按上方命令手动安装即可。

## 何时使用
- 用户发来一份 PDF，但 markitdown / pdfplumber 提取为 0 字（扫描件 / 图片型 PDF）。
- 用户发来照片、截图、扫描图片，需要提取其中文字（含手写体、表格、印章）。
- 用户用「识别」「读一下这份」「把这个读出来」「转成文字」等模糊说法要求处理 PDF / 图片时（⚠️ 若明确要求「识别隐患 / 安全隐患」则属安全检查类技能，本技能只做文字提取，不判安全）。
- 任何「读取这个文件/图片里的字」「OCR 一下」「把扫描件变成可编辑文字」类请求。
- 用户要将扫描件 PDF / 图片（文字提取为 0 字）整理进知识库时，本技能可作为预处理自动介入。
- 涉密 / 敏感或隐私资料，要求**本地处理、不联网、不外传**。

## 核心原理
扫描件 PDF 本质是「一页页图片打包」，传统文本提取工具读不到字。本技能用**双引擎**解决：

- **引擎 A · RapidOCR（离线保底）**：PyMuPDF 渲染 → RapidOCR(ONNX, 纯 CPU) 逐页识别 → 自动拼 `.md` 落盘。**不依赖多模态、不联网、不占 token、可批量**，是默认与涉密首选路线。
- **引擎 B · 多模态读图（高质量）**：PDF 渲染为 PNG → 由支持多模态读图能力的 Agent 逐张读取（如 WorkBuddy 的 Read 工具），模型「看图识字」。**擅长表格还原、手写签名、印章、模糊图**，质量高于引擎 A，但消耗多模态额度。**注意**：引擎 B 的隐私性取决于 Agent 的多模态实现是否本地运行；敏感资料请优先走引擎 A。

两步均在本机完成；图片与文字不上传任何外部服务，满足涉密与隐私要求。

## 双引擎路线速查

### 路线 A：离线 RapidOCR（默认、保底、涉密首选）
适用：常规段落 / 表单 / 印刷体；追求零外发、零 token、可批量。
```
# PDF / 单图 / 图片目录 → <同名>_OCR.md
python scripts/ocr_offline.py "<文件或目录>"
# 典型参数
python scripts/ocr_offline.py "扫描件.pdf" -o "输出.md" -d 300
python scripts/ocr_offline.py "扫描件.pdf" --start-page 3 --end-page 10
python scripts/ocr_offline.py "扫描件.pdf" --boxes-json   # 附原始文本框(供表格重建)
python scripts/ocr_offline.py "图片.png" --no-preprocess  # 跳过摆正
```
- 输出 `<同名>_OCR.md`（按页/按图分段，含页码标记 + 人工核对提示）。
- 自动做**摆正（EXIF 方向 + 倾斜角校正）**与灰度增强（见 `preprocess_img.py`）。
- 末尾打印每页 PNG 路径，供路线 B 做手写/表格增强。
- 局限：**表格只输出单元格文字（非结构化，阅读顺序可能交错）；手写体识别率有限**。表格/手写请走路线 B 或人工整理。

### 路线 B：多模态读图（高质量，表格/手写/印章）
适用：表格需还原为 Markdown、有手写签名/批注、有印章、图模糊倾斜严重。
```
# 1) 渲染 PDF 为 PNG（可摆正 / 分块便于读图）
python scripts/pdf_to_images.py "扫描件.pdf" -o "./pages" -d 300
python scripts/pdf_to_images.py "大图.pdf" --preprocess --tiles 2   # 摆正+切2x2块
python scripts/pdf_to_images.py "<PDF目录>" -b                        # 批量渲染
# 2) 由支持多模态读图能力的 Agent 逐张读取 pages/*.png（如 WorkBuddy 的 Read 工具，多模态自动生效），按文末提示词模板提取
# 3) 把各页文字聚合成 JSON，调用落盘工具：
python scripts/write_ocr_md.py result.json -o "输出_OCR.md"
```
- `write_ocr_md.py` 输入 JSON：`{"source":"x.pdf","engine":"多模态读图","pages":[{"label":"第1页","text":"…"}]}`（也可从 stdin 读 `-`）。
- 路线 B 由 Agent 临场读图，识别质量最高；表格可直接输出 Markdown 表。

## 工作流程（决策树）
1. **判断输入 & 选引擎**
   - 涉密 / 批量 / 纯印刷体 / 不确定 Agent 是否支持多模态 → **路线 A（RapidOCR）**。
   - 含表格、手写、印章、模糊图，且当前 Agent 支持多模态 → **路线 B（多模态读图）**，或 A 跑完再用 B 补强。
2. **PDF 先渲染**：路线 A 的 `ocr_offline.py` 内部已渲染；路线 B 显式调 `pdf_to_images.py`（超大页加 `--tiles 2`）。
3. **识别**：
   - 路线 A：`ocr_offline.py` 一键出 `.md`。
   - 路线 B：对每张 PNG 由支持多模态的 Agent 读取，套用下方提示词模板。
4. **汇总落盘**：路线 A 自动落盘；路线 B 用 `write_ocr_md.py`（或 Agent 直接 Write `.md`）。
5. **标注与提示**：手写/印章标注〔手写：〕〔印章：〕；末尾附「⚠️ OCR 结果由 AI 识别，关键数字与签名请人工核对」。

## 表格专项（路线 B 为主）
- **路线 A（RapidOCR）**：只给出单元格文字、阅读顺序可能交错，**不会输出规整 Markdown 表**。若用户只要"把字提出来"可接受；要规整表格必须走路线 B 或人工整理。`--boxes-json` 导出的原始框可供后续脚本重建，但本技能暂不自动建表。
- **路线 B（多模态）**：直接要求模型"将表格还原为 Markdown 表格"，准确率很高。
- **跨页表格**：逐页识别后由人工/模型拼接，注明"下接第 X 页"。
- **标注规范**：表格在正文以 Markdown 表呈现；若无法确认单元格归属，用〔？〕标注并说明。

## 手写 / 印章专项（路线 B 为主）
- 手写签名、批注、日期：**必须用〔手写：原文〕标注**，无法辨认写〔手写：难以辨识〕。
- 印章（圆形/方形红章）：**用〔印章：可见文字〕标注**，如〔印章：安全环保公司〕；仅见红圈无法辨字写〔印章：内容不清〕。
- 引擎 A 对印刷体稳，对手写弱——手写页务必走路线 B。

### 多模态读图提示词模板（逐页套用）
> 请用 OCR 模式读取这张扫描页（图片已附）。要求：
> 1) 按原版面提取**全部**文字，保持段落与标题层级；
> 2) **表格还原为 Markdown 表格**（含表头）；
> 3) 手写签名/批注用〔手写：原文〕标注，无法辨认写〔手写：难以辨识〕；
> 4) 印章用〔印章：可见文字〕标注；
> 5) 只输出该页纯文本（可含 Markdown 表），**不要加解释或总结**。

## 涉密与隐私（红线）
- **引擎 A（RapidOCR）全程离线**，数据不出本机，涉密首选。
- **引擎 B（多模态读图）的隐私性取决于当前 Agent 的多模态实现**：若其底层调用云端视觉模型，图片会经过网络。对数据外发敏感时，请优先使用引擎 A。
- **禁止**将涉密 / 敏感图片或 PDF 通过本技能以外的方式（外部 OCR 网站、联网视觉模型、微信/邮件外发）处理。
- 输出 `.md` 默认落在输入文件同目录或指定目录，均在本机磁盘；勿主动同步至联网云盘。

## 局限与备选
- **引擎 A 依赖 rapidocr-onnxruntime**（已列入 `requirements.txt`；换环境首次用需 `pip install -r requirements.txt`，脚本会提示）。表格/手写识别率有限，见上。
- **引擎 B 依赖 Agent 的多模态读图能力**：若当前模型不支持图片输入，提示用户切换支持多模态的模型（如 WorkBuddy），或退回引擎 A。
- 手写体、模糊、倾斜严重图片识别率会下降，务必人工核对。
- 超大 PDF（>50 页）建议分批（`--start-page/--end-page` 或拆目录）处理，避免单次上下文过长。

## 快速验证
```
# 路线 A：直接出 md（最稳，涉密首选）
python scripts/ocr_offline.py "测试.pdf"

# 路线 B：先渲染再看图
python scripts/pdf_to_images.py "测试.pdf" -o "./pages"
# 随后由支持多模态的 Agent 读取 ./pages/page_01.png 验证
```
