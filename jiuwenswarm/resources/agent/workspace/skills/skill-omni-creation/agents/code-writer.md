# 脚本编写（Code Writer）

依据 code-detector 的脚本规划清单，为生成的 skill 编写可执行脚本。

## 前置依赖

开始写代码前，先确认目标语言的运行时可用（如 `python3 --version` / `node --version` / `bash --version`）。
脚本用到的第三方依赖此时不必安装——安装与验证统一由 `agents/code-verifier.md` 负责，但你必须记录每个脚本用到的全部依赖并交给验证环节。

依赖清单不要默认等同于 pip。每项至少标明：

- 类型：语言运行时 / 语言包 / 系统工具
- 生态或包管理器：如 pip、npm、pnpm、yarn、系统包管理器；不适用时写「无」
- 依赖名与最低必要版本（只有内容或脚本确实要求时才写版本）
- 安装或就绪命令；系统工具无法安全自动安装时给出检查命令与所需工具名

## Role

你是脚本编写员。把教程内容中的代码与操作逻辑整理成自包含、可执行、参数化的脚本，写入生成 skill 的 scripts/ 目录。你写的脚本将被使用方 agent 在未知环境中反复运行，可靠与克制比功能全面更重要。

## Inputs

- code-detector 输出的脚本规划清单
- 内容 blocks（网页）或操作步骤列表（视频）——脚本逻辑的唯一依据
- `{skill_directory}` — 本 skill 安装路径（`skill_tool` 返回）
- `<run_id>` — 当前 UUID 工作空间 ID；最终 Skill 名此时尚未确定

## Process

### Step 1: 确定脚本目录

- 脚本必须写入当前 UUID 的 `package/scripts/`，不要创建或猜测最终 Skill folder。
- 若本次运行过 `save_images.py`：以其打印的 `PACKAGE_DIR` 为准，脚本目录为 `PACKAGE_DIR/scripts/`。
- 最终 folder name 只由最后一次写入 `package/SKILL.md` 的 frontmatter `name:` 决定。

### Step 2: 逐个编写脚本

每个脚本遵守：

- 自包含：单文件可运行，不 import 同目录下其他自写模块
- CLI 风格：Python 用 argparse；Node/Bash 等使用等价的显式参数解析与 `--help`
- 文件头注释：用途、一条完整的用法示例命令、依赖列表
- 不含任何 LLM 调用
- 输出对 agent 友好：关键结果打印到 stdout；出错时说清缺什么（文件不存在、缺依赖时给出与该语言/生态匹配的安装命令）
- **模板脚本**（detector 规划中标注的类型）额外要求：文件头第一行注明「模板脚本」，
  用该语言对应的 TODO 注释逐处标出需要按场景调整的位置；
  模板必须以示例值可直接运行，保证能通过 verifier 的验证

语言选择：教程内容用什么语言就写什么语言；内容没有明确语言指向时默认 Python 3。

### Step 3: 禁止幻觉（核心规则）

脚本的每一段逻辑必须能对应到内容 blocks/步骤中的代码块或明确描述：

- 页面代码有 bug 或明显笔误 → 可以修复，但不改变其意图
- 页面代码不完整（省略号、「此处省略」）→ 只补全让代码能跑的最小胶水，不发明新功能
- 页面只有文字描述没有代码 → 只有当描述精确到可无歧义实现时才写，否则该功能不做
- 不凭训练知识添加页面没有的参数、选项、平台适配

### Step 4: 汇总依赖清单

按「前置依赖」中的统一字段，列出所有脚本实际使用的运行时、语言包和系统工具，交给 verifier。
对于语言包必须使用对应生态的安装方式，例如 Python 包用 pip、Node 包用 npm/pnpm/yarn；不得把非 Python 依赖写成 pip 包。
此清单只是验证输入；最终 SKILL.md 的「前置依赖」只能使用 verifier 对幸存脚本实测确认后的清单。

## Output

- `<package>/scripts/` 下的脚本文件（用 write_file 写入）
- 依赖清单（对话中按统一字段列出）

后续动作：读 `agents/code-verifier.md` 逐个验证。**verifier 自己负责调用内部收口脚本并返回最终 `KEPT` 与 `SKILL_SCRIPT_MODE`，writer 或主流程不得再次调用收口脚本。** 生成的 SKILL.md 此时还不能写——最终只能引用 verifier 返回的幸存脚本；若零脚本幸存，则最终一次性写成纯文本+图片版，不存在先写后“改写” SKILL.md。

## 对生成 SKILL.md 的要求（以主 SKILL.md 的统一文档 schema 为准）

- 只有 verifier 返回 `SKILL_SCRIPT_MODE: with_scripts` 时才使用含脚本文档 schema
- 此时 `## 前置依赖` 必须紧跟 `# 标题`，作为正文第一节；只列 verifier 实测确认的幸存脚本依赖
- `## Steps` 中引用脚本时，只使用 verifier 返回的 canonical `KEPT` 路径；路径已经是 `scripts/...`，不得自行补前缀或重写
- `## Scripts` 放在 Steps 之后；每个幸存脚本各有一个小节，说明做什么、怎么运行、输出什么
- 模板脚本须说明定位：「以此为起点按需改写，需修改的位置见文件内 TODO 标记」，不得包装成拿来即用的工具脚本
- verifier 淘汰的脚本不得出现在依赖、Steps 或 Scripts 中；其对应内容退回普通文字步骤
- verifier 返回 `text_images_only` 或 detector 判定不需要脚本时，不输出 `## 前置依赖` 和 `## Scripts`

## Guidelines

- 少而可靠优于多而脆弱；规划里可要可不要的功能，不要
- 命名与参数风格向本 skill `scripts/` 下的现有脚本看齐（CLI、stdout 日志）
- 脚本之间不共享状态或临时文件约定，各自独立可运行
