---
name: skill-omni-creation
description: "当用户说「从这个链接/URL生成skill」「把这个网页/教程做成skill」「从这个视频提取步骤」时触发。先读取此 SKILL.md，再按步骤调用 scripts/ 下的 Python 脚本完成：爬取网页或下载视频 → 下载图片/抽帧 → 生成标准 Skill Markdown 文件；若内容需要编程实现，自动编写并验证配套脚本（scripts/）。"
---

## 这个 Skill 做什么

给定一个 URL（网页或视频），先在 `skills/.skill-omni-creation/<run_id>/` 的 UUID 临时工作空间中完成抓取、审核、脚本生成与验证；最后只写一次 `package/SKILL.md`，其 kebab-case `name:` 是唯一正式名称来源。Python 随后发布到 `skills/<name>/`，保证 folder name 与 frontmatter `name` 完全一致，并清除本次临时空间。

适用请求：
- "从这个链接生成一个 Skill"
- "把这个教程做成 Skill"
- "从这个视频提取操作步骤"

---

## 执行总览（先看这里，再动手）

无论网页还是视频、无论是否走降级路径，完整流程都是固定的六段，缺一不可：

1. **获取内容** — `scrape_page.py`（视频走 `analyze_video.py` 抽帧；被反爬则按「Playwright 失败时的处理」构造 stage01.json）
2. **图片环节** — 有图时 `prepare_images.py` → `image_review.py` 串行处理，并且只依据 alt 与周围文字做一次 `KEEP` / `SKIP` 审核（有疑问一律 `SKIP`，不得查看图片），再执行 `save_images.py`；无图时直接执行 `save_images.py <run_id> --keep`；最终图片进入本次 `package/references/`
3. **代码粗筛（必经）** — 图片环节完成后按「代码生成（agents/）」一节判断是否需要生成脚本；命中粗筛必须读 `agents/code-detector.md` 细判。**这一步没做完，不允许写 SKILL.md**
4. **【判定需要时】编写并验证脚本** — `agents/code-writer.md` → `agents/code-verifier.md`；verifier 内部负责脚本收口并返回最终 `KEPT` / `SKILL_SCRIPT_MODE`，主流程不得重复收口；验证失败只淘汰未通过脚本，不阻塞主 Skill
5. **一次性写最终 SKILL.md** — 写到 UUID 的 `package/SKILL.md`；`name:` 此时第一次确定，必须为英文小写 kebab-case，且它是最终 folder name 的唯一来源
6. **发布并清理** — 运行 `finalize_skill.py <run_id>`；代码读取并校验 `name:`，直接覆盖 `skills/<name>/`，验证 folder/name 一致，然后删除当前 UUID；若 `.skill-omni-creation/` 已空也一并删除

向用户展示执行计划时，计划里必须列出第 3 步（代码粗筛），不得省略。

---

## 环境门禁（网页/图片/视频）

网页、图片和视频依赖统一由 `scripts/environment_gate.py` 在代码层处理，不要求 Agent 手工判断 `.venv` 路径。B 站/YouTube/Vimeo 纯视频 URL 在 `scrape_page.py` 创建 UUID/stage 之前直接使用 `video` profile，不要求 Playwright/BeautifulSoup；小红书先在创建 UUID 前用轻量 `video-probe`（`yt-dlp` + Node.js）判断帖子类型，确认视频后再进入完整 `video` profile，否则进入 `web` profile。`analyze_video.py` 自身也会再次调用完整 `video` profile 作为兜底。

门禁在对应链路真正写入 stage、下载视频或抽帧之前执行，并按以下规则处理：

1. 选择已激活虚拟环境、当前虚拟环境或项目最近的 `.venv`；都不存在时，尝试在项目目录创建 `.venv`。
2. 若选中的解释器不是当前解释器，自动使用该解释器重新执行当前脚本。
3. 自动安装当前 profile 缺失的 Python 包：网页/图片按需使用 `beautifulsoup4`、`requests`、`Pillow`、`playwright`；`video-probe` 使用 `requests`、`yt-dlp`；完整视频 profile 使用 `requests`、`Pillow`、`yt-dlp`。
4. 网页 profile 自动安装并真实启动一次 Playwright Chromium。
5. `video-probe` 在代码层检查 Node.js；完整 `video` profile 同时检查 Node.js、`ffmpeg`、`ffprobe` 是否都在 `PATH` 且可执行。Node.js 和 FFmpeg 都属于系统级工具，不做跨平台静默安装；缺失或不可执行时直接输出 `ENVIRONMENT_BLOCKED` 和对应平台安装提示。
6. Debian/Ubuntu 类 Linux 在具备 root 或免密 sudo 时自动补齐 Chromium 系统库；其他 Linux 或无提权权限时返回非零并输出所需修复命令。
7. 自动修复或依赖验证仍失败时输出 `ENVIRONMENT_BLOCKED` 并立即停止；环境诊断状态只写系统临时目录，不写入本 Skill；不得继续对应的抓取、视频下载/抽帧、图片审核、`save_images.py --keep` 或最终化。

`scrape_page.py`、`analyze_video.py`、`prepare_images.py`、`download_images.py`、`print_blocks.py`、`image_review.py` 和 `save_images.py` 都会自动调用同一个 `environment_gate.py`，按任务选择不同 profile，因此正常流程无需 Agent 手工安装 Python 依赖。

可选诊断命令：

```bash
{bootstrap_python} "{skill_directory}/scripts/environment_gate.py" --profile web-images --check
{bootstrap_python} "{skill_directory}/scripts/environment_gate.py" --profile video --check
```

其中 `{bootstrap_python}` 只需是当前 shell 中任何能启动 Python 3 的命令；门禁会自行寻找或创建项目虚拟环境。去掉 `--check` 时，门禁会尝试自动修复 Python 依赖；视频的 Node.js、`ffmpeg`、`ffprobe` 只验证，不自动安装系统工具。

### Node.js（yt-dlp JavaScript runtime）

视频命令固定使用 `yt-dlp --js-runtimes node`，因此 `video-probe` 与完整 `video` profile 都会在代码层检查 `node` 是否在 `PATH` 且可执行。缺失时以 `ENVIRONMENT_BLOCKED` 停止；请使用当前系统的软件包管理器或 Node.js 官方安装方式安装，并确保 `node --version` 可正常执行。

### ffmpeg / ffprobe（视频下载合并与抽帧）

完整 `video` profile 会在代码层同时检查 `ffmpeg` 与 `ffprobe`。若缺失，流程会在下载/抽帧前以 `ENVIRONMENT_BLOCKED` 停止。按系统安装 FFmpeg（通常同时提供 ffprobe）：

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt-get update
sudo apt-get install -y ffmpeg

# Windows
# 使用 winget/chocolatey/scoop 或 FFmpeg 发行包安装，并确保 ffmpeg.exe、ffprobe.exe 在 PATH
```

### yt-dlp（视频下载）

`yt-dlp` 同时属于 `video-probe` 与完整 `video` profile 的 Python 依赖；缺失时门禁会尝试自动安装到同一个选定 Python 环境。可用以下完整视频 profile 命令只做诊断：

```bash
{bootstrap_python} "{skill_directory}/scripts/environment_gate.py" --profile video --check
```

---

## 可用脚本

所有脚本通过当前系统可用的 shell/code 工具调用，脚本本身不含任何 LLM 调用。

**重要：** `skill_tool` 返回的 `skill_dir` 即 `{skill_directory}`。网页/图片链路脚本允许用当前 shell 中可用的 Python 3 作为启动命令；代码门禁会自动切换到同一个项目解释器并重新执行。所有脚本仍用绝对路径调用，不要 `cd`、不要拼接 `&&`。运行开始时只确定 UUID `run_id`，中间产物写到 `<skills>/.skill-omni-creation/<run_id>/runtime/`，最终候选包写到同一 run 的 `package/`。**不要根据 URL 或 TITLE 预先决定最终 Skill 名。最终 folder name 只在一次性写 `package/SKILL.md` 时由 frontmatter `name:` 决定。**

### scrape_page.py — 爬取网页

```bash
{python} "{skill_directory}/scripts/scrape_page.py" "<URL>"
# 输出日志: [scrape_page] RUN_ID: <run_id>
# 输出文件: <skills>/.skill-omni-creation/<run_id>/runtime/stage01.json
```

stage01.json 结构：
```json
{
  "url": "...", "run_id": "...", "title": "...",
  "blocks": [
    {"type": "heading", "level": 2, "text": "...", "source": "main"},
    {"type": "text",    "text": "...", "source": "main"},
    {"type": "image",   "url": "...", "alt": "...", "source": "main", "path": null}
  ],
  "video_urls": []
}
```

- 首次运行必须使用脚本输出的 `RUN_ID` 作为后续所有 `<run_id>` 参数；同一 URL 若存在未完成 UUID workspace，脚本会复用它。`run_id` 只是运行身份，不是最终 Skill name。
- 若 URL 是 B 站/YouTube/Vimeo 纯视频平台，先通过完整 `video` 环境门禁，再建立/复用 UUID workspace 并获取真实视频 TITLE；B 站识别到 `BV...` 时优先使用 Bilibili Public API 获取 TITLE，API 失败后才按 `yt-dlp cookies → yt-dlp no-cookie` 回退。所有 `yt-dlp` 子进程统一强制 Python 子进程 UTF-8 stdio，并由父进程按 UTF-8 解码。成功后 stage01 返回 `blocks=[]` + `video_urls=[url]`。TITLE 不参与最终 folder 命名。
- 小红书链接（`xhslink.com` 短链或 `xiaohongshu.com` 直链）**必须通过 `scrape_page.py` 处理**，不要直接用 `fetch_webpage`。脚本会在创建 UUID 前以 `video-probe` 判断帖子类型：视频帖再通过完整 `video` 门禁并进入视频路径；图文帖通过 `web` 门禁后进入网页路径。
- Twitter/X status URL 按普通网页处理，不属于纯视频平台快捷分流；若页面实际包含视频，由网页抓取后的 `video_urls` 再进入视频路径。
- 正文抽取覆盖标题、段落、列表、`pre/code/table/dl`、常见代码编辑器、JS 文本容器及可恢复的 Canvas 文字
- stage01 在脚本内部施加整页 block、正文字符和序列化大小硬上限；超限时从整页范围保留结构与代表内容，不创建分页文件
- 只有代码门禁输出 `ENVIRONMENT_READY` 后，页面仍被反爬并返回空 blocks，才进入「Playwright 失败时的处理」；此时保留已有 UUID workspace，降级路径继续写同一 run 的 stage01。`ENVIRONMENT_BLOCKED` 必须停止，不能降级

### print_blocks.py — 读取网页内容（替代 read_file 读 JSON）

```bash
{python} "{skill_directory}/scripts/print_blocks.py" <run_id> --stage stage01
# 读取: <skills>/.skill-omni-creation/<run_id>/runtime/stage01.json
# 输出: TITLE、VIDEO_URLS、一次性有界的全页代表视图（标题/正文/图片 alt）
```

**stage01.json、stage02.json 和 stage03.json 都不要直接读取。必须用这个脚本提取内容。**

stage01/stage02/stage03 都输出一次相同全局预算的受限代表视图，不提供 `offset`、下一批游标或继续翻页入口；不要直接读取这些 JSON，也不要尝试补读被预算省略的内容。

### prepare_images.py / image_review.py — 串行下载与单层图片审核

```bash
{python} "{skill_directory}/scripts/prepare_images.py" <run_id>
# 严格串行执行 download_images.py，成功后才执行 print_blocks.py --stage stage02
```

只根据输出中的图片 alt 与周围文字完成一次审核，并把每张图片最终标为 `KEEP` 或 `SKIP`。无法确定图片是否有用时，一律标为 `SKIP`，不要调用 `read_file` 查看图片：

```bash
{python} "{skill_directory}/scripts/image_review.py" <run_id> --first-pass KEEP SKIP SKIP
```

`image_review.py` 会把最终状态直接写入当前 `stage02.json`，并立即输出 `KEEP_PATHS_ARGS`；把其后的 `--keep ...` 参数原样传给 `save_images.py`。不要直接读取 `stage02.json`，不要扫描或列举整个 `raw_images/`，也不要使用 `read_file`、`read_file_stream`、shell、base64 或字节模式查看待审核图片。

### analyze_video.py — 视频抽帧

```bash
{python} "{skill_directory}/scripts/analyze_video.py" "<video_url_or_run_id>" --title "视频标题"
# 可选显式指定同一工作目录：追加 --run-id <run_id>；省略时脚本会按 URL 自动解析当前未完成 run
# 传视频 URL  → 自动复用同一 UUID run_id；原始帧保存到 runtime/frames/，模型只读取 runtime/review_frames/ 的 JPEG
# Bilibili 下载使用持久化 .part 文件与 HTTP Range 断点续传；重跑会继续未完成下载
# 传 run_id → 优先复用 UUID runtime/video.mp4 或 runtime/downloads/video.*；若尚未下载，则从该 run 的 stage01/run metadata 自动恢复视频 URL、下载后继续粗扫抽帧
# 短视频按 0.5fps；长视频均匀覆盖全片且总帧数最多 90；固定每批最多 8 帧
# 输出: 首次只打印第 1 批的 8 个精确审核帧路径；完成后用 --next-review-batch 获取下一批
```

脚本**不调用任何 LLM**，只进行一次粗扫抽帧，不执行细扫。分析时只读取脚本当前打印的 8 个 `review_frames/*.jpg`；完成后运行同一命令并追加 `--next-review-batch` 获取下一批。选定关键帧后用相同编号的 `frames/frame_NNNN.png` 保存。

### save_images.py — 保存选定图片

```bash
{python} "{skill_directory}/scripts/save_images.py" <run_id> --keep raw_images/dom_000.jpg raw_images/dom_003.png
# --keep 后是相对于 <skills>/.skill-omni-creation/<run_id>/runtime/ 的路径；无图时只传 --keep
# 输出: <skills>/.skill-omni-creation/<run_id>/package/references/img_NN.ext
#       <skills>/.skill-omni-creation/<run_id>/package/references/video_frame_NNNN.png
```

脚本会打印每张图片的最终文件名和 SKILL.md 的写入路径，例如：
```
[save_images] img_00.jpg <- raw_images/dom_002.jpg
[save_images] img_01.png <- raw_images/dom_005.png
[save_images] SKILL_MD_PATH: /Users/xxx/.jiuwenswarm/agent/workspace/skills/.skill-omni-creation/<run_id>/package/SKILL.md
```
**生成 SKILL.md 时，用 write_file 写入 `SKILL_MD_PATH` 打印出的绝对路径。图片路径为 `references/<文件名>`，例如 `references/img_00.jpg`。**
页面无图时也必须执行 `save_images.py <run_id> --keep`；脚本会从 stage02（不存在则 stage01）生成 stage03，并打印同一个 `SKILL_MD_PATH`。

### finalize_skill.py — 用 SKILL.md name 发布最终目录

```bash
{python} "{skill_directory}/scripts/finalize_skill.py" <run_id>
```

此脚本只做机械发布：读取 `package/SKILL.md` 的 frontmatter `name:`，严格校验 kebab-case，然后直接覆盖 `skills/<name>/`。它不会重新起名、不会生成 `-v2`、不会修改已经一次性写好的 SKILL.md。发布后再次验证 `folder name == frontmatter.name`，再清除当前 UUID；若 `.skill-omni-creation/` 已空则一并删除。

---

## 图片筛选标准

你正在审核从教程或指南页面提取的图片。该指南可能涵盖任意主题：软件操作、摄影、烹饪、硬件等。

只依据图片 alt 与周围文字判断，每张图必须给出以下两种最终状态之一：
- `KEEP`：上下文已能明确证明图片直接说明步骤、概念或技巧，能帮助读者理解或复现。
- `SKIP`：图片是小图标、Logo、广告、纯装饰图、其他页面缩略图、与指南主题无关，或者仅凭 alt 与周围文字无法可靠判断其价值。

**有任何疑问时一律 `SKIP`。不得查看图片后再决定，也不得输出其他状态。**

决策数量必须与 stage02 中图片数量完全一致，例如：
`KEEP SKIP SKIP`


---

## 最终名称与发布契约

- 在最终 `package/SKILL.md` 写入之前，pipeline 只有 UUID `run_id`，**不存在正式 Skill folder name**。
- 最终 `SKILL.md` 只允许 `write_file` **一次**；其 YAML `name:` 是唯一正式名称来源。
- `name` 必须是英文小写 kebab-case，例如 `parallel-ppt-generation-team`。
- 写完后立即运行 `finalize_skill.py <run_id>`；Python 只读取/验证 `name`，不替 LLM 改名。
- 最终强制满足：`basename(skills/<name>/) == SKILL.md frontmatter.name`。
- 若 `skills/<name>/` 已存在，直接覆盖；不生成 `-v2`，不询问，不保留旧版本。
- 成功发布后删除当前 UUID；若 `skills/.skill-omni-creation/` 已空，则连临时根目录一起删除。

---

## 最终 SKILL.md 文档 Schema（网页/视频共用）

最终 `package/SKILL.md` 仍只允许 `write_file` 一次。写入前先根据代码阶段结果选择且只选择一种正文 schema：

**A. 有幸存脚本**（verifier 返回 `SKILL_SCRIPT_MODE: with_scripts`）：

```markdown
---
name: <kebab-case-skill-name>
description: <1-3句中文>
---

# <技能名称（中文）>

## 前置依赖

<只列 verifier 对 KEPT 脚本实测确认的运行时、语言包和系统工具；使用对应生态的安装/检查命令>

## Steps

<正文步骤；调用脚本时只使用 verifier 返回的 canonical scripts/... 路径>

## Scripts

### `scripts/example.py`

<做什么、完整运行命令、输出什么；模板脚本还要说明 TODO 修改位置>
```

**B. 无脚本**（detector 判定不需要，或 verifier 返回 `SKILL_SCRIPT_MODE: text_images_only`）：

```markdown
---
name: <kebab-case-skill-name>
description: <1-3句中文>
---

# <技能名称（中文）>

## Steps

<正文步骤>
```

统一规则：

- 只要存在幸存脚本，`## 前置依赖` **必须紧跟 `# 标题`，是正文第一节**；随后才是 `## Steps`。
- 有幸存脚本时，`## Scripts` 固定放在 Steps 之后，每个 `KEPT` 脚本一个 `###` 小节。
- `## 前置依赖` 只允许使用 verifier 对幸存脚本实测确认的依赖；不得带入淘汰脚本依赖。依赖按真实生态描述，例如 Python 包用 pip、Node 包用 npm/pnpm/yarn，系统工具给实际检查/安装命令。
- 即使幸存脚本没有第三方语言包，也仍保留 `## 前置依赖`，写明已验证的语言运行时以及“无需额外第三方语言包”；若有系统工具则同时列出。
- verifier 返回的 `KEPT` 已统一为 package-relative canonical `scripts/...`。Steps、Scripts、示例命令都直接使用该路径，**不得自行补 `scripts/`、删前缀、改名或改成绝对路径**。
- detector 判定不需要脚本或 verifier 返回 `text_images_only` 时，完全省略 `## 前置依赖` 与 `## Scripts`；不是先写后删除。

---

## 输出格式规范（网页）

你正在为 AI 智能体构建一个 Skill 文件，用于学习和执行软件操作任务。
所有自然语言输出（描述、标题、步骤、说明）必须使用简体中文。
YAML frontmatter 的 key（name、description）、代码/命令、依赖名、图片路径和 canonical `scripts/...` 路径保持其技术原格式，不做中文化。

输入内容：
1. TITLE —— 软件任务名称
2. BLOCKS —— 按 DOM 顺序排列的内容块列表，每个块的类型为以下之一：
   - {"type": "heading", "level": 1-4, "text": "...", "source": "main"}
   - {"type": "text",    "text": "...", "source": "main"}
   - {"type": "image",   "path": "references/img_NN.ext", "alt": "...", "source": "main"}
   图片块在文字块之间按原始页面位置穿插排列。

输出 frontmatter 与标题（严格遵守）：

```markdown
---
name: <kebab-case-skill-name>
description: <1-3句中文：描述这个 Skill 的用途和适用场景>
---

# <技能名称（中文）>
```

标题之后的正文结构必须严格按「最终 SKILL.md 文档 Schema（网页/视频共用）」选择。下面只规定 `## Steps` 内部的网页步骤组织方式；有幸存脚本时必须先输出 `## 前置依赖`，并在 Steps 后追加 `## Scripts`。

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
- name 必须是英文小写 kebab-case（仅 `a-z`、`0-9` 和单个连字符，示例 `parallel-ppt-generation-team`），description 必须是 1-3 句简体中文。
- 标题后的章节必须服从共用文档 schema：有幸存脚本时为 `## 前置依赖 → ## Steps → ## Scripts`；无脚本时直接 `## Steps`。
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
所有自然语言输出（描述、标题、步骤、说明、图片 alt）必须使用简体中文。
YAML frontmatter 的 key（name、description）、代码/命令、依赖名、图片路径和 canonical `scripts/...` 路径保持其技术原格式，不做中文化。

输入内容：
1. TITLE —— 视频任务名称
2. BLOCKS —— 按时间顺序排列的内容块，每个块类型为以下之一：
   - {"type": "text",  "text": "..."} — 已从视频中提取的操作步骤（一条步骤一个 block）
   - {"type": "image", "path": "references/video_frame_NNNN.png", "alt": "..."} — 对应时间段的截图

输出 frontmatter 与标题（严格遵守）：

```markdown
---
name: <kebab-case-skill-name>
description: "<1-3句中文：描述这个 Skill 的用途和适用场景>"
---

# <技能名称（中文）>
```

标题之后的正文结构必须严格按「最终 SKILL.md 文档 Schema（网页/视频共用）」选择。下面只展示 `## Steps` 内部的视频步骤格式；有幸存脚本时必须先输出 `## 前置依赖`，并在 Steps 后追加 `## Scripts`。

`## Steps` 内容格式：

```markdown
1. <动词> **<界面元素名或操作对象>**

![替代文字](references/video_frame_NNNN.png)

2. ...
```

核心规则：
- YAML frontmatter 必须是输出的第一行内容。
- name 必须根据完整理解后的中心任务生成，使用英文小写 kebab-case（示例 `parallel-ppt-generation-team`），避免 video、tutorial、skill 等空泛词。该值也是最终 folder name。
- description 必须是 1-3 句简体中文，并用英文双引号包裹。
- 技能标题必须是简体中文，准确概括 TITLE 对应的任务。
- 标题后的章节必须服从共用文档 schema：有幸存脚本时为 `## 前置依赖 → ## Steps → ## Scripts`；无脚本时直接 `## Steps`。
- 只输出 Skill Markdown 文件内容，不加任何前言、解释或代码块。

依据规则：
- 每个步骤都必须有 BLOCKS 中的 text block 作为依据。
- 允许为了去重、合并和提升可读性而改写步骤表述。
- 但不得引入 BLOCKS 中没有出现的信息、工具、参数、平台、结论或建议。
- 任务范围由 TITLE 定义，与主任务无关的步骤一律跳过。
- 如果某个 text block 操作对象不清晰，且无法从相邻 block 判断其含义，则跳过。

步骤组织规则：
- 若视频涵盖多个明显不同的子功能（例如：基础操作、公式使用、图表制作），
  用 ### 标题划分每个子功能，每组步骤编号从 1 重新开始。
- 若视频是单一连续流程，使用平铺编号列表，不加 ### 标题。
- 不要过度分组，只在子功能之间有明确主题切换时才分组。

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

本节只处理**代码门禁已经输出 `ENVIRONMENT_READY`，但目标网页仍返回空内容、403 或验证页**的情形。若任一脚本输出 `ENVIRONMENT_BLOCKED` 或返回非零，必须停止当前网页/图片流程；不得使用本节绕过环境失败。

在上述前提下，若 `scrape_page.py` 返回空 blocks，使用 `web_fetch_webpage` 获取页面原始文本，自行提取：
- 主标题
- 按顺序排列的 h2/h3 标题和步骤文字
- 图片 URL（若有）

**提取后不要直接写 SKILL.md**。降级抓取继续使用 `scrape_page.py` 已输出的同一 `<run_id>`，把真实 TITLE 与内容写入 `<skills>/.skill-omni-creation/<run_id>/runtime/stage01.json`；不要在此阶段决定最终 Skill name。stage01 结构与「scrape_page.py」一节展示的完全一致：顶层含 `url`/`run_id`/`title`/`blocks`/`video_urls`，image block 必须带 `url` 字段。然后回到正常链路继续：

```bash
# 用 write_file 写好 <skills>/.skill-omni-creation/<run_id>/runtime/stage01.json 后
{python} "{skill_directory}/scripts/prepare_images.py" <run_id>
# 有图：image_review.py → save_images.py；无图：直接 save_images.py <run_id> --keep；之后代码粗筛 → 写 SKILL.md
```

- 若页面确实没有图片，可跳过 prepare_images，但必须执行 `save_images.py <run_id> --keep` 以生成 stage03 并确定最终输出路径
- 「代码生成（agents/）」的粗筛与后续流程在此路径上同样必须执行——降级只改变内容获取方式，不改变流程本身

---

## 代码生成（agents/）

有些内容描述的任务本质上要靠代码完成，纯文字步骤不足以复现。图片环节完成后（包括无图时执行 `save_images.py <run_id> --keep`），做一次粗筛，满足任意一条 → 读 `agents/code-detector.md` 细判：

- 正文含成段代码块（约 5 行以上）
- 视频中出现与中心任务直接相关的代码/命令（无行数门槛）
- 任务本质需编程完成（批量处理、API 调用、数据转换、算法实现）
- 内容是 CLI 命令组合的教程

粗筛与细判的纪律：

- 粗筛是必经步骤：无论内容来自 scrape_page、fetch_webpage 降级还是视频帧分析，读完内容后都要先做粗筛，才能进入写 SKILL.md 的环节
- 粗筛命中后**必须读 `agents/code-detector.md`**，按文档标准细判——不得凭自身感觉替代文档判断
- 判定由你独立完成且是终局的：不询问用户要不要脚本，不提议添加内容中不存在的脚本；判定不需要就直接按原流程写 SKILL.md

detector 判定需要脚本后，严格按此顺序执行：

1. 读 `agents/code-writer.md`，把脚本写入当前 UUID 的 `package/scripts/`（以 `save_images.py` 打印的 `PACKAGE_DIR` 为准；若无图片，也先运行 `save_images.py <run_id> --keep` 建立 package 契约）
2. 读 `agents/code-verifier.md`，逐个验证。**verifier 内部负责且只负责一次脚本收口**，并返回最终 canonical `KEPT`、`SKILL_SCRIPT_MODE` 与幸存脚本依赖清单；主流程不得再做脚本收口
3. 只写一次生成的 SKILL.md：`with_scripts` 按共用 schema 输出 `前置依赖 → Steps → Scripts`，且只引用 verifier 返回的 `KEPT`；`text_images_only` 则一次性写纯文本+图片版，不输出脚本依赖或调用

纯 GUI 点击教程不满足粗筛 → 跳过本节，按原流程写 SKILL.md。

---

## 执行流程参考

**网页路径：**
```
scrape_page.py
→ print_blocks.py <run_id> --stage stage01  # 先确认代表视图中是否存在 image block
→ 【有图】prepare_images.py <run_id>        # 严格串行 download_images.py → print_blocks.py --stage stage02
→ 【有图】image_review.py 只根据 alt 与周围文字一次性标记 KEEP/SKIP（有疑问一律 SKIP；不得查看图片）
→ 【有图】save_images.py（原样传 KEEP_PATHS_ARGS）
→ 【无图】跳过 prepare_images.py / image_review.py，直接 save_images.py <run_id> --keep
→ print_blocks.py <run_id> --stage stage03  # 输出最终 blocks；无图时保持纯文字 blocks
→ 代码粗筛                         # 见「代码生成（agents/）」；命中则读 agents/code-detector.md 细判
→ 【判定需要脚本时】agents/code-writer.md 编写 → agents/code-verifier.md 验证并内部收口，返回 canonical KEPT / SKILL_SCRIPT_MODE
→ write_file **只写一次** save_images.py 输出的 package/SKILL.md（按共用文档 schema +「输出格式规范（网页）」；此时 frontmatter `name:` 第一次确定最终 kebab-case 名称）
→ {python} "{skill_directory}/scripts/finalize_skill.py" <run_id>  # 读取 name → 覆盖发布到 skills/<name>/ → 校验 folder==name → 清理 UUID/空临时根
```

**注意：stage01.json / stage02.json / stage03.json 不要直接读取。** 三个 stage 的模型可见输出都由 `print_blocks.py` 施加同一全局预算；网页图片只依据 stage02 代表视图中的 alt 与周围文字审核，并只使用 `image_review.py` 输出的 `KEEP_PATHS_ARGS`，禁止扫描、列举或读取整个 `raw_images/`。

**视频路径：**
```
scrape_page.py → video_urls 非空（或直接识别视频 URL）
→ analyze_video.py <video_url_or_run_id> --title "..."
  （只执行单阶段粗扫：短视频按 0.5fps；长视频自动降低频率并均匀覆盖全片；总帧数最多 90；每批最多 8 帧）
  （脚本自动复用当前 UUID run_id；传 run_id 且本地尚无完成视频时会从该 run 的 metadata 自动恢复视频 URL 并下载；Bilibili 中断后通过持久化 .part 文件续传）
→ 脚本首次只打印当前批次的 8 个精确 `review_frames/*.jpg` 绝对路径
  只读取这 8 个路径；完成后运行 `analyze_video.py <run_id> --next-review-batch` 获取下一批
  选择后映射到同编号的 frames PNG，并记录步骤对应的帧编号
→ 【必须执行】save_images.py — 将选用帧复制到 references/ 后再写 SKILL.md
  选出最能说明各步骤的帧（建议每个关键步骤 1 张），收集相对路径列表，例如：
    ["frames/frame_0040.png", "frames/frame_0080.png", ...]
  运行：
    {python} "{skill_directory}/scripts/save_images.py" <run_id> --keep frames/frame_0040.png ...
  脚本输出：
    [save_images] video_frame_0040.png <- frames/frame_0040.png   ← 保留原帧编号
    [save_images] SKILL_MD_PATH: /absolute/path/to/.skill-omni-creation/<run_id>/package/SKILL.md
  记录 SKILL_MD_PATH（后续 write_file 用此路径）
  图片路径格式：references/video_frame_NNNN.png（原帧编号，4 位）
→ 代码粗筛                         # 见「代码生成（agents/）」；命中则读 agents/code-detector.md 细判
→ 【detector 判定需要脚本时】agents/code-writer.md 写 package/scripts/ → agents/code-verifier.md 验证并内部收口，返回 canonical KEPT / SKILL_SCRIPT_MODE
→ write_file **只写一次** SKILL_MD_PATH 指向的 `package/SKILL.md`（按共用文档 schema +「输出格式规范（视频）」；frontmatter `name:` 在这里第一次确定最终 kebab-case 名称；含脚本时只引用 verifier 返回的 canonical KEPT）
→ {python} "{skill_directory}/scripts/finalize_skill.py" <run_id>  # 直接覆盖 skills/<name>/，校验 folder==name，清理当前 UUID；临时根为空时也删除
```

**网页含嵌入视频：**
先走网页路径。若 video_urls 非空，追加视频路径处理，合并步骤后写 SKILL.md。

---

## 运行环境

- Python 3.11+；网页、图片、视频链路统一由 `environment_gate.py` 选择或创建项目虚拟环境，并在必要时重新执行当前脚本
- Python 依赖按 profile 自动检查/安装：`playwright`、`beautifulsoup4`、`Pillow`、`requests`、`yt-dlp`
- Playwright Chromium 与可安全处理的 Linux 系统库由门禁尽可能自动修复
- 视频外部工具 Node.js、`ffmpeg`、`ffprobe` 由 `video-probe` / `video` profile 在代码层按需检查可执行性；缺失时阻断并给出平台安装提示，不静默安装系统工具
- `ENVIRONMENT_BLOCKED` 后不得继续对应的 stage、视频下载/抽帧、图片审核、保存结果或最终化
- 无需配置 API 环境变量
