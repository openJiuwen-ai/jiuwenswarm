---
name: poster-layered
version: 0.1.0
author: openjiuwen
description: 分层海报：无字/少字底图 + 程序叠字 + OCR 对照放行，减少整图重抽与人工错字校对
tags: [poster, multimodal, ocr, mvp]
allowed_tools:
  - generate_image
  - visual_question_answering
  - bash
  - read_file
  - write_file
---

# 分层海报 Skill（MVP）

当用户要做海报、宣传图、封面且文案必须正确时，**必须**使用本技能。  
目标：错字不触发整图重抽；交付前自动 OCR 对照；人工只审风格。

## 硬约束（违反即失败）

1. **文案先定稿**：未得到用户确认的 `copy.md`（或等价定稿），禁止调用 `generate_image`。
2. **底图禁止烤正文**：生图 prompt 必须含「no text / no letters / no Chinese characters / blank text areas」或中文等价说明；标题若必须艺术字，单行 ≤ 4 字且后续仍以 OCR 校验；长文案、副标题、小字、名单 **一律程序叠字**。
3. **禁止用「改 prompt 整图重抽」修错字**。错字只走：改叠字参数 → 重新叠字 → 再 OCR。
4. **次数上限**：
   - `max_full_regen`（整图）= **2**
   - `max_text_rerender`（叠字）= **5**
   - 超限则停止自动重试，向用户报告失败原因与已生成的底图/叠字稿路径，请用户改文案或布局。

## 工作区约定

在当前 session/workspace 下创建目录（若不存在则创建）：

```text
poster_job/<job_id>/
  copy.md           # 定稿文案（唯一真相源）
  layout.json       # 叠字布局
  base.png          # 无字/少字底图
  poster.png        # 当前候选成片
  ocr_last.txt      # 最近一次 OCR 原文
  manifest.json     # 状态与重试计数
```

`layout.json` 最小字段示例见 `references/layout.schema.md`。

## SOP

### Step 0 — 收集与定稿

1. 向用户确认：尺寸（如 `1080x1920` / `1080x1080`）、风格关键词、必须出现的文案（主标题/副标题/正文/脚注）。
2. 用 `write_file` 写入 `copy.md`，结构建议：

```markdown
# copy
## title
...
## subtitle
...
## body
...
## footer
...
```

3. **展示文案请用户确认**。未确认不得进入 Step 1。
4. 初始化 `manifest.json`：`full_regen=0`, `text_rerender=0`, `status=copy_locked`。

### Step 1 — 无字/少字底图

1. 根据风格写生图 prompt，**明确要求画面中无文字**，可预留空白安全区给标题/正文。
2. 调用 `generate_image`（需已配置 `models.image_gen` / `IMAGE_GEN_*`）。
3. 将结果保存/复制为 `base.png`（若工具返回路径，复制到 `poster_job/<job_id>/base.png`）。
4. `full_regen += 1`。若 `full_regen >= max_full_regen`，停止并报告。
5. （可选）对 `base.png` 调一次 `visual_question_answering`：  
   `question = "图中是否出现任何可读文字或字母？只回答 YES 或 NO。"`  
   若 YES：不得进入叠字交付；应改 prompt 再抽底图，或接受轻微残留但 **正文仍只以叠字为准**（MVP 推荐直接重抽底图，计入 `full_regen`）。

### Step 2 — 程序叠字

1. 根据 `copy.md` 与用户意向写/更新 `layout.json`（坐标、字号、颜色、对齐、字体路径）。
2. 运行叠字脚本（优先）：

```bash
python scripts/overlay_text.py \
  --base poster_job/<job_id>/base.png \
  --layout poster_job/<job_id>/layout.json \
  --out poster_job/<job_id>/poster.png
```

脚本相对本技能目录；若工作目录不同，用绝对路径调用。依赖：`Pillow`（`pip install pillow`）。系统需有可用中文字体（脚本会尝试常见路径，也可在 `layout.json` 里指定 `font_path`）。

3. `text_rerender += 1`。若 `text_rerender >= max_text_rerender`，停止并报告。

### Step 3 — OCR 对照放行

1. 调用 `visual_question_answering`：
   - `image_path_or_url` = `poster.png` 的可访问路径/URL（沙箱路径若不可用，先复制到工具可读位置）。
   - `question` = 使用下方「OCR 放行提问」模板，并把 `copy.md` 全文贴进提问。
2. 将工具返回的 OCR 段写入 `ocr_last.txt`。
3. **放行条件（全部满足）**：
   - `copy.md` 中 **title / subtitle / body / footer** 每一段的规范文本（去首尾空白、合并连续空白）均能在 OCR 结果中找到对应子串；或 Agent 做字符级比对后 **无增删改字**（允许 OCR 常见标点/全半角差异，见下）。
   - 允许忽略：全角/半角标点、多余空格、换行位置。
   - **不允许忽略**：错字、漏字、多字、数字错误、英文大小写若文案中有明确要求。
4. **不放行**：只调整 `layout.json`（字号、位置、描边、对比度）或 `copy.md`（若用户同意改文案），回到 **Step 2**；**禁止**为修错字调用 `generate_image`。
5. 放行后：`status=released`，向用户交付 `poster.png` 路径，并说明「文案已 OCR 对照通过；请仅审风格与构图」。

### OCR 放行提问模板

```text
请对图片做精确 OCR，按阅读顺序列出所有可见文字。
然后对照下面「定稿文案」，逐段判断是否完全一致（忽略空格与换行、全半角标点）。
对每一段输出：PASS 或 FAIL，FAIL 时指出差异。
最后一行只输出：OVERALL_PASS 或 OVERALL_FAIL。

【定稿文案】
<粘贴 copy.md 全文>
```

仅当最后一行为 `OVERALL_PASS`（或等价明确通过）时才可交付。

## 失败路由（摘要）

| 现象 | 动作 | 计数 |
|------|------|------|
| 底图仍有大段字 | 改「无字」prompt，整图重抽 | `full_regen` |
| 叠字后 OCR FAIL | 改 layout/对比度，重新叠字 | `text_rerender` |
| 用户要改文案 | 更新 copy.md → 确认 → Step 2（不重抽底图） | `text_rerender` |
| 用户要改风格/构图 | 可 Step 1 重抽底图，文案不变 | `full_regen` |
| 超限 | 停止自动重试，交人决策 | — |

## 不适用本 MVP 时

- 必须把长文艺术字「烤进」画面且无法叠字：说明限制，建议降级为短词艺术字 + 其余叠字，或改用外部设计工具。
- 未配置 `generate_image` / vision OCR：先提示配置 `models.image_gen` 与 `models.vision`。
