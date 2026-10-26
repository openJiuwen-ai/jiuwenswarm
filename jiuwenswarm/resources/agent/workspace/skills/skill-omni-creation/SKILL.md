---
name: skill-omni-creation
description: "当用户说「从这个链接/URL生成skill」「把这个网页/教程做成skill」「从这个视频提取步骤」时触发。先用 read_file 读取此 SKILL.md，再按步骤用 bash 调用 scripts/ 下的 Python 脚本完成：爬取网页或下载视频 → 下载图片/抽帧 → 生成标准 Skill Markdown 文件。"
---

## 这个 Skill 做什么

给定一个 URL（网页或视频），读取页面内容、下载相关图片或视频帧，整理成标准 Skill `.md` 文件，保存到 `skills/<slug>/SKILL.md`，图片保存到 `skills/<slug>/references/`。

适用请求：
- "从这个链接生成一个 Skill"
- "把这个教程做成 Skill"
- "从这个视频提取操作步骤"

---

## 前置依赖

**执行任何脚本前，请先确认以下工具已安装：**

### Playwright（网页爬取）

```bash
playwright install chromium
```

若未安装，网页爬取会静默降级或跳过，导致 skill 内容为空。

### ffmpeg（视频抽帧）

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
apt-get install ffmpeg
```

若未安装，`analyze_video.py` 会报错退出。安装后请确认可用：

```bash
which ffmpeg && ffmpeg -version
```

### yt-dlp（视频下载）

```bash
pip install yt-dlp
```

若 `pip` 不可用：

```bash
brew install yt-dlp        # macOS
apt-get install yt-dlp     # Ubuntu/Debian
```

`analyze_video.py` 和 `scrape_page.py` 均依赖 yt-dlp 下载视频。安装后确认可用：

```bash
python3 -m yt_dlp --version
```

---

## 可用脚本

所有脚本通过 `bash` 工具调用，不含任何 LLM 调用。

**重要：首先调用 `skill_tool(skill_name="skill-omni-creation")` 获取 `skill_directory`。**
后续所有命令用 `cd {skill_directory}/scripts && python3 ...` 执行，`read_file` 也使用绝对路径 `{skill_directory}/scripts/work/<slug>/...`。
BashTool 没有 `cwd` 参数，必须在命令里用 `cd` 切换目录。

### scrape_page.py — 爬取网页

```bash
cd {skill_directory}/scripts && python3 scrape_page.py "<URL>" <slug>
# 输出: work/<slug>/stage01.json
```

stage01.json 结构：
```json
{
  "url": "...", "slug": "...", "title": "...",
  "blocks": [
    {"type": "heading", "level": 2, "text": "...", "source": "main"},
    {"type": "text",    "text": "...", "source": "main"},
    {"type": "image",   "url": "...", "alt": "...", "source": "main", "path": null}
  ],
  "video_urls": []
}
```

- 若 URL 是视频平台（B 站/YouTube/Vimeo/小红书），自动跳过爬取，返回 `blocks=[]` + `video_urls=[url]`
- 小红书链接（`xhslink.com` 短链或 `xiaohongshu.com` 直链）**必须通过 `scrape_page.py` 处理**，不要直接用 `fetch_webpage`，脚本会自动判断是否为视频帖子
- 若 Playwright 被反爬拦截，返回空 blocks；见「Playwright 失败时的处理」

### print_blocks.py — 读取网页内容（替代 read_file 读 JSON）

```bash
cd {skill_directory}/scripts && python3 print_blocks.py <slug>
# 读取: work/<slug>/stage01.json
# 输出: TITLE、VIDEO_URLS、所有 blocks 的可读摘要（标题/正文/图片 alt）
```

**stage01.json 和 stage02.json 过大，read_file 会返回空。必须用这个脚本提取内容。**

### download_images.py — 下载图片

```bash
cd {skill_directory}/scripts && python3 download_images.py <slug>
# 读取: work/<slug>/stage01.json
# 输出: work/<slug>/raw_images/dom_NNN.{ext}
#       work/<slug>/stage02.json
```

**不要用 read_file 读 stage02.json（文件过大会返回空）。**
下载完成后运行 `python3 print_blocks.py <slug>`，根据每张图片的 alt 文字和前后文字上下文按「图片筛选标准」决定 KEEP/SKIP，将 KEEP 的路径列表传给 `save_images.py`。
若某张图片的 alt 文字为空或过于泛化（如 "image"、"photo"、"screenshot"、文件名等），则用 `read_file` 实际查看该图片再决定。

### analyze_video.py — 视频抽帧

```bash
cd {skill_directory}/scripts && python3 analyze_video.py "<video_url_or_slug>" --title "视频标题"
# 传视频 URL  → 自动下载，以 1fps 抽帧保存到 work/<slug>/frames/
# 传 slug    → 读取已下载的 work/<slug>/video.mp4 直接抽帧
# 输出: 打印帧总数、帧目录路径、建议批次大小
```

脚本**不调用任何 LLM**，只抽取帧图片。分析步骤由 agent 自己完成：用 `read_file` 分批读取帧图片，用自身视觉能力提取操作步骤。

### save_images.py — 保存选定图片

```bash
cd {skill_directory}/scripts && python3 save_images.py <slug> '["raw_images/dom_000.jpg", "raw_images/dom_003.png"]'
# 第二个参数: 相对于 work/<slug>/ 的路径列表（JSON 数组），由你决定保留哪些
# 输出: <skills_dir>/<slug>/references/img_NN.ext
#       <skills_dir>/<slug>/references/video_frame_NNN.png
```

脚本会打印每张图片的最终文件名和 SKILL.md 的写入路径，例如：
```
[save_images] img_00.jpg <- raw_images/dom_002.jpg
[save_images] img_01.png <- raw_images/dom_005.png
[save_images] SKILL_MD_PATH: /Users/xxx/.jiuwenswarm/agent/jiuwenclaw_workspace/skills/exposure_fusion_opencv/SKILL.md
```
**生成 SKILL.md 时，用 write_file 写入 `SKILL_MD_PATH` 打印出的绝对路径。图片路径为 `references/<文件名>`，例如 `references/img_00.jpg`。**

---

## 图片筛选标准

你正在审核从教程或指南页面提取的图片。
该指南可能涵盖任意主题：软件操作、摄影、烹饪、硬件等。

保留（KEEP）图片的条件：图片直接说明或演示了周围文字所描述的某个步骤、概念或技巧，
能帮助读者理解或复现文字内容。

跳过（SKIP）图片的情况（满足任意一条即跳过）：
- 图片前后既没有章节标题也没有文字上下文 —— 几乎可以确定是文章顶部的装饰性封面/横幅图，
  而非教学内容。
- 图片是小图标、独立 Logo、广告或纯装饰性图形。
- 图片是指向其他文章或页面的缩略图或预览图。
- 图片内容明显属于与本指南标题无关的其他主题。

如果图片标记为"来源：子页面"，则适用更严格标准：只有当图片直接、明确地演示了
标题所描述的主任务中的某个步骤时，才保留。子页面图片有疑问时，一律跳过。

同时参考图片内容和上下文（章节标题、前后文字）做出判断。有疑问时，一律跳过。

只输出一个 JSON 字符串数组，每张图片对应一个元素，值只能是 "KEEP" 或 "SKIP"。
示例（3 张图片）：["KEEP", "SKIP", "KEEP"]


---

## 输出格式规范（网页）

你正在为 AI 智能体构建一个 Skill 文件，用于学习和执行软件操作任务。
所有输出文字（描述、标题、步骤、说明）必须使用简体中文。
只有 YAML frontmatter 的 key（name、description）和图片路径保持原格式不变。

输入内容：
1. TITLE —— 软件任务名称
2. BLOCKS —— 按 DOM 顺序排列的内容块列表，每个块的类型为以下之一：
   - {"type": "heading", "level": 1-4, "text": "...", "source": "main"|"subpage"}
   - {"type": "text",    "text": "...", "source": "main"|"subpage"}
   - {"type": "image",   "path": "references/img_NN.ext", "alt": "...", "source": "main"|"subpage"}
   图片块在文字块之间按原始页面位置穿插排列。
   source 为 "subpage" 的块适用更严格的相关性过滤。

输出格式（严格遵守）：

---
name: <snake_case_skill_name>
description: <1-3句中文：描述这个 Skill 的用途和适用场景>
---

# <技能名称（中文）>

## Steps

分组规则（从上到下匹配，取第一条符合的）：
1. BLOCKS 中存在二级标题块（h2）：
   - 每个 h2 → ### 分组标题，每组步骤编号从 1 重新开始。
   - 若该 h2 组内存在 h3 块 → 每个 h3 → #### 子节标题，每子节编号从 1 重新开始。
2. BLOCKS 中只有三级标题块（h3），没有 h2：
   - 每个 h3 → ### 分组标题，每组步骤编号从 1 重新开始。
3. 没有 h2 也没有 h3 —— 单一连续流程：
   - 平铺格式：一个编号列表，不加 ### 或 #### 标题。

格式示例：

有 h2 + h3（两级分组）：
### <h2 标题文字>

#### <h3 标题文字>

1. <动词> **<界面元素名>**

![替代文字](references/img_NN.ext)

2. ...

#### <下一个 h3 标题文字>

1. ...

### <下一个 h2 标题文字>

#### <h3 标题文字>

1. ...

只有 h2（一级分组）：
### <h2 标题文字>

1. <动词> **<界面元素名>**

![替代文字](references/img_NN.ext)

2. ...

### <下一个 h2 标题文字>

1. ...

平铺格式（无 h2 无 h3）：
1. <动词> **<界面元素名>**

![替代文字](references/img_NN.ext)

2. ...

规则：
- YAML frontmatter（--- ... ---）必须是输出的第一行内容。
- name 必须是 snake_case，description 必须是 1-3 句简体中文。
- 图片严格规则：只能引用 BLOCKS 中 path 字段确实存在的图片。
  若没有任何 image block 有 path 值，输出中不能出现任何以 ![ 开头的行。
  不得自行发明、重命名或伪造任何路径。
- BLOCKS 中每一个有效 path 的 image block 都必须在输出中出现。
- 每张图片必须顶格独占一行（零缩进），前后各留一个空行，打断编号列表。绝对不能缩进在某个列表项之内。
  正确：
  ```
  2. 选择一个环绕选项。

  ![替代文字](references/img_00.png)

  3. 选择除嵌入型以外的选项。
  ```
  错误：
  ```
  2. 选择一个环绕选项。
     ![替代文字](references/img_00.png)
  3. 选择除嵌入型以外的选项。
  ```
- 图片语法：![替代文字](path) —— path 必须从 block 的 path 字段原样复制；替代文字必须用简体中文描述图片内容，不得原样保留原始英文 alt 属性。
- 禁止幻觉：每个步骤都必须有 BLOCKS 中的文字块或标题块作为依据。
  不得凭训练知识自行添加步骤、界面元素名称或操作流程。少而准确优于多而捏造。
- 主题聚焦规则：任务范围由 TITLE 定义。BLOCKS 中内容明显属于其他独立功能的块一律跳过。
  示例：标题是"创建数据透视图"→ 跳过所有关于数据透视表设置的块。
- 重复章节合并规则：若不同章节描述相同或高度重叠的操作，合并为一个章节，
  保留步骤最完整的版本，丢弃重复内容，不要并排输出。
- 选项展示合并规则：当连续步骤逐一列举同类选项（例如"点击 X 频道、Y 频道、Z 频道"
  紧接着"点击 A 频道、B 频道、C 频道"），说明视频是在演示可用选项而非全部选择。
  将这些步骤合并为一条上下文步骤：概述有哪些选项，并说明本教程实际配置的是哪一个。
  示例："软件支持配置多种渠道（网页、飞书、Telegram 等），接下来配置**飞书**渠道。"
  然后直接继续该选项的配置步骤。
- 纯文字步骤：若文字块描述了明确的操作步骤，但其后没有紧跟图片块，按纯文字步骤输出（不加图片标签）。
- 子页面块：同样适用主题聚焦规则 —— 只有与 TITLE 直接相关时才纳入输出。
- 不附来源链接：不在输出末尾追加来源 URL、参考链接或脚注。
- 包含所有帮助用户完成任务的内容：主要步骤、条件分支（"若 X 则 Y"）、说明、提示、
  警告和故障排查。由主题聚焦规则决定相关性，不要整体排除某类内容。
  判断标准：即使跳过该内容，用户仍能完成 TITLE 描述的任务 → 跳过。
  只排除：推广/营销文案、"了解更多/访问链接"导航文字、
  以及不属于操作流程本身的背景说明（历史沿革、政策变更、技术参考表、独立 FAQ 等）。

只输出 Skill Markdown 文件内容，不加任何前言或解释。


---

## 输出格式规范（视频）

你正在为 AI 智能体构建一个 Skill Markdown 文件，用于学习和执行视频教程中的操作任务。
所有输出文字（描述、标题、步骤、说明、图片 alt）必须使用简体中文。
只有 YAML frontmatter 的 key（name、description）和图片路径保持原格式不变。

输入内容：
1. TITLE —— 视频任务名称
2. BLOCKS —— 按时间顺序排列的内容块，每个块类型为以下之一：
   - {"type": "text",  "text": "..."} — 已从视频中提取的操作步骤（一条步骤一个 block）
   - {"type": "image", "path": "references/video_frame_NN.png", "alt": "..."} — 对应时间段的截图

输出格式（严格遵守）：

---
name: <snake_case_skill_name>
description: "<1-3句中文：描述这个 Skill 的用途和适用场景>"
---

# <技能名称（中文）>

## Steps

1. <动词> **<界面元素名或操作对象>**

![替代文字](references/video_frame_NN.png)

2. ...

核心规则：
- YAML frontmatter 必须是输出的第一行内容。
- name 必须根据 TITLE 生成，使用英文小写 snake_case，避免 video、tutorial、skill 等空泛词。
- description 必须是 1-3 句简体中文，并用英文双引号包裹。
- 技能标题必须是简体中文，准确概括 TITLE 对应的任务。
- 只输出 Skill Markdown 文件内容，不加任何前言、解释或代码块。

依据规则：
- 每个步骤都必须有 BLOCKS 中的 text block 作为依据。
- 允许为了去重、合并和提升可读性而改写步骤表述。
- 但不得引入 BLOCKS 中没有出现的信息、工具、参数、平台、结论或建议。
- 任务范围由 TITLE 定义，与主任务无关的步骤一律跳过。
- 如果某个 text block 操作对象不清晰，且无法从相邻 block 判断其含义，则跳过。

# 【当前：分组版本】若视频只有单一流程可改回平铺版本（见下方注释）
分组规则：
- 若视频涵盖多个明显不同的子功能（例如：基础操作、公式使用、图表制作），
  用 ### 标题划分每个子功能，每组步骤编号从 1 重新开始。
- 若视频是单一连续流程，使用平铺编号列表，不加 ### 标题。
- 不要过度分组，只在子功能之间有明确主题切换时才分组。
# 【平铺版本备用】如需回退，删除上方分组规则，改为以下一条：
# - 使用平铺编号列表，不加 ### 或 #### 标题。

步骤规则：
- 每个编号步骤必须是可执行动作，尽量以动词开头。
- 用 **粗体** 标记关键界面元素、操作对象、工具名称或参数名称。
- 不保留原始时间戳。
- 不输出过细的鼠标移动、等待、浏览、片头片尾、广告、点赞订阅等无关内容。

图片规则：
- 只能引用 BLOCKS 中 path 字段确实存在的图片，不得自行发明或伪造路径。
- path 必须从 block 的 path 字段原样复制。
- 图片语法必须为：![替代文字](path)
- 图片应放在与其最相关的步骤之后。
- 不要求每个步骤都配图；无明确关联的图片可以跳过。
- 每张图片独占一行，前后各留一个空行。
- 不要连续堆叠多张与同一步骤无明显区别的图片。

合并规则：
- 重复步骤合并：
  若多步描述完全相同，或仅措辞略有不同但属于同一操作，只保留一步。
- 微步骤合并：
  若连续多步属于同一标准流程，例如选择路径 → 点击 Next → 点击 Install，
  可合并为一步，用"并""然后""最后"连接关键动作。
- 选项展示合并：
  当连续步骤逐一列举同类选项，例如"点击网页渠道、飞书渠道、Telegram 渠道"，
  说明视频在展示可用选项，而不是要求全部选择。
  应合并为一条步骤，概述可用选项，并说明本教程实际进入或配置的是哪一个。
- 纯描述前缀合并：
  若某个 text block 只描述软件功能，没有具体操作，但与下一步属于同一功能模块，
  可合并到下一步，作为上下文前缀。
  若与后续步骤无明确关联，则只有在有助于理解任务时才保留；否则跳过。
- 平台泛化：
  若步骤中涉及特定平台或系统，但 BLOCKS 中明确提到多个平台，
  不要写死为单一平台，应写成"根据操作系统选择对应安装包"，并在括号中列出视频提到的平台。
  若 BLOCKS 只提到一个平台，则不要自行补充其他平台。

输出要求：
- 只输出 Skill Markdown 文件内容。
- 不加解释、不加前缀、不加 Markdown 代码块。


---

## Playwright 失败时的处理

若 `scrape_page.py` 返回空 blocks，使用 `web_fetch_webpage` 获取页面原始文本，自行提取：
- 主标题
- 按顺序排列的 h2/h3 标题和步骤文字
- 图片 URL（若有）

直接用提取内容生成 SKILL.md，跳过 download_images/save_images 步骤。

---

## 执行流程参考

**网页路径：**
```
scrape_page.py
→ download_images.py
→ print_blocks.py <slug>          # 读 stage02.json，根据图片 alt + 上下文决定 KEEP/SKIP
→ save_images.py（传 KEEP 路径列表）  # 同时生成 stage03.json（blocks 含 references/ 路径）
→ print_blocks.py <slug>          # 现在读 stage03.json，输出含 path 的最终 blocks
→ write_file 写入 save_images.py 输出的 SKILL_MD_PATH（按「输出格式规范（网页）」，图片路径直接从 blocks 取）
```

**注意：stage01.json / stage02.json 体积较大，直接 read_file 会返回空（被 offload）。**
请用以下方式读取内容：

```bash
# 提取文字 blocks（标题 + 正文），用于写 SKILL.md
cd {skill_directory}/scripts && python3 print_blocks.py <slug>

# 列出已下载的图片（用于 read_file 逐张查看）
ls {skill_directory}/scripts/work/<slug>/raw_images/
```

看图时用 `read_file` + 绝对路径：
`{skill_directory}/scripts/work/<slug>/raw_images/dom_000.jpg`

**视频路径：**
```
scrape_page.py → video_urls 非空（或直接识别视频 URL）
→ analyze_video.py <video_url_or_slug> --title "..."
  （自动下载，以 1fps 抽帧到 work/<slug>/frames/，打印帧数和批次建议）
→ 脚本输出会打印帧目录的**绝对路径**和每批次的完整文件路径，例如：
    批次 1/33: /path/to/scripts/work/<slug>/frames/frame_0001.png → .../frame_0020.png
  直接使用输出中打印的绝对路径调用 read_file，不要自行拼接路径
  用自身视觉能力分析每批帧图片，提取操作步骤，累积所有步骤
→ write_file 写 skills/<slug>/SKILL.md（按「输出格式规范（视频）」，步骤来自自身分析结果）
```

**网页含嵌入视频：**
先走网页路径。若 video_urls 非空，追加视频路径处理，合并步骤后写 SKILL.md。

---

## 运行环境

- Python 3.11+，所有 bash 命令必须以 `cd {skill_directory}/scripts &&` 开头（`skill_directory` 由 `skill_tool` 返回）
- 依赖：`playwright`、`beautifulsoup4`、`Pillow`、`requests`
- 外部工具：`ffmpeg`、`ffprobe`、`yt-dlp`
- Playwright 需安装 Chromium：`playwright install chromium`
- 无需配置 API 环境变量
