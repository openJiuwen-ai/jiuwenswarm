# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash 身份/工作空间域段正文 + 拆分纯函数（自 flash_persona_rail 抽离）。

持有 identity 拆分块表（CN/EN）、2xx/020 改写目标正文、段名/段号常量与
``_split_identity`` 纯函数；``FlashPersonaRail`` 从本模块 re-import，零行为变化。
"""

from __future__ import annotations

# 源段名（prompt_builder.py 构造期注入）。
_SECTION_IDENTITY = "identity"

# ── 拆分块表：顺序 = 源内容里标题出现的顺序。─────────────────────
# 每项 = (标题字面量, 目标段名, 目标段号)。intro（首标题之前）固定归 000 身份。
_IDENTITY_BLOCKS_CN: list[tuple[str, str, int]] = [
    ("# JiuwenSwarm 内部数据", "workspace_model", 200),
    ("## 目录理解规则", "workspace_model", 200),
    ("## JiuwenSwarm 启动配置", "workspace_model", 200),
    ("## 运行环境", "runtime_environment", 210),
    ("## 任务执行准则", "execution_principles", 400),
    ("## 输出文件放置规范", "output_placement", 220),
    ("## 产物格式偏好", "artifact_format", 420),
    ("## 文件发送", "file_delivery", 460),
]

_IDENTITY_BLOCKS_EN: list[tuple[str, str, int]] = [
    ("# JiuwenSwarm Internal Data", "workspace_model", 200),
    ("## Directory Interpretation Rules", "workspace_model", 200),
    ("## JiuwenSwarm Startup Configuration", "workspace_model", 200),
    ("## Runtime Environment", "runtime_environment", 210),
    ("## Task Execution Principles", "execution_principles", 400),
    ("## Output File Placement", "output_placement", 220),
    ("## Artifact Format Preference", "artifact_format", 420),
    ("## Sending Files", "file_delivery", 460),
]

# ── 2xx 内容改写目标正文（06-目标提示词-完整版.md §200/§220，CN/EN）。────────
# 210 路径与权限（新段，详见下方 _PATH_PERMISSION_*）；运行环境事实（identity 拆出
# 的 runtime_environment 块）由 FlashSectionOrderRail 并入 env@1000（并集）后删除。
_DIRECTORY_MODEL_CN = """# 目录模型

四个固定术语及其定义：

| 术语 | 含义 | 用途 |
|------|------|------|
| 项目目录 | 用户项目的根目录 | 本次任务的 workspace 操作边界 |
| cwd（当前工作目录） | Bash 默认执行目录 | 用户任务中相对路径的解析基准 |
| Agent 内部数据目录 | 保存身份、记忆、技能、待办 | 不在其中搜索或运行用户项目文件 |
| 启动配置目录 | 保存 JiuwenSwarm 自身配置 | 仅用户明确要求时才访问 |

- 相对路径应相对于 cwd 解析。
- 项目目录是文件操作的严格上限边界；严禁用 `../` 越界访问项目目录之外的用户系统文件。
- 任务中出现 config/、memory/、skills/、todo/ 等逻辑名不自动映射到 Agent 内部目录。
- 绝对路径见运行时路径事实（1010）。
"""

_DIRECTORY_MODEL_EN = """# Directory Model

Four fixed terms and their definitions:

| Term | Meaning | Use |
|------|--------|-----|
| Project directory | The root of the user's project | The workspace operation boundary for this task |
| cwd (current working directory) | Bash's default execution directory | The resolution base for relative paths in the user's task |
| Agent internal data directory | Stores identity, memory, skills, todos | Do not search or run user project files inside it |
| Startup configuration directory | Stores JiuwenSwarm's own configuration | Access only when the user explicitly asks |

- Relative paths resolve against cwd.
- The project directory is the strict upper boundary for file operations; never use `../` to escape the project directory and access the user's system files outside it.
- Logical names such as config/, memory/, skills/, todo/ in a task do not automatically map to the Agent internal directories.
- For absolute paths, see runtime path facts (1010).
"""

_WRITE_LOCATION_CN = """# 写入位置

【硬性门禁】严禁写入系统与公共目录（如 C:\\Users\\Public、C:\\Windows、C:\\Program Files 等）及启动配置目录；启动配置目录不得用于存放普通任务产物或项目文件。

产物落盘优先级：

1. 用户明确指定的保存位置（严禁指定系统与公共目录）
2. 项目产物 → 当前项目目录中的合理位置
3. 非项目型产物 → 当前工作目录中的合理位置
4. Agent 内部数据（身份/记忆/技能/待办）→ Agent 内部数据目录对应位置

写入用相对路径（基于 cwd），发送用绝对路径。
"""

_WRITE_LOCATION_EN = """# Write Location

[Hard gate] Never write to system or public directories (e.g. C:\\Users\\Public, C:\\Windows, C:\\Program Files) or the startup configuration directory; the startup configuration directory must not be used to store ordinary task artifacts or project files.

Artifact placement priority:

1. A save location the user explicitly specified (never specify a system or public directory)
2. Project artifact → a sensible location in the current project directory
3. Non-project artifact → a sensible location in the current working directory
4. Agent internal data (identity/memory/skills/todos) → the corresponding location in the Agent internal data directory

Use relative paths for writing (based on cwd) and absolute paths for sending.
"""

# ── 400 执行质量准则（通用准则 + 场景化准则，06-目标提示词-完整版.md §400）。────────
# 替换 identity 拆出的「## 任务执行准则」旧段（八条混排），改为两组常驻：通用底线 +
# 场景触发。「工具结果优先」改名「拒绝盲信写入」（340 工具结果段按名引用之，不再从本段剥离）。
_NAME_EXEC_PRINCIPLES = "execution_principles"
_PRIO_EXEC_PRINCIPLES = 400

_EXEC_PRINCIPLES_CN = """# 执行质量准则

## 通用准则

以下准则适用于任何任务，是交付质量的基本底线。无论任务看起来多简单，交付前都应逐条对照。

- **数据保真**：写入文件或结构化结果时，字段值必须与来源逐字一致。
  不规范化、不改写、不翻译、不补全、不截断——编号、代码、单位、大小写都保持原样。
  用户给的报表数字是什么就写什么，不要"帮忙"格式化。
- **按条件取舍**：要求挑选/过滤/排除时绝不照单全收。
  综合所有相关信息（含需跨源交叉核对的条件）逐项判断，命中排除或豁免条件的主动剔除。
- **写入范围匹配意图**：写操作的影响范围要与任务意图一致。
  只需改动部分数据、或须保留既有数据时，仅增改目标记录，不要用整体覆盖/清空/重建去完成局部改动。
  调用写入或导入类工具前，先确认并显式设置写入模式等关键参数，不要盲信默认值。
- **交付前自检**：交付前逐条核对全部条件是否满足——有无错纳漏纳、时间/数值/单位是否精确、既有数据是否完好、格式是否与模板一致。不过关先修正再交付。
- **拒绝盲信写入**：写文件工具返回 success 不代表文件内容真写对了，以 read_file 读回来的实际内容为准。
  若 read_file 只显示默认空表头（如 ## Sheet1）而无实际数据行，即使工具调用未报错、你认为任务"应该"成功了，也必须如实判定生成失败并重新构建。切勿凭空脑补文件内容。

## 场景化准则

以下准则在任务涉及对应场景时生效。判断依据是任务实际内容（有没有给模板、涉不涉及时间、查不查数据库），不是任务标签。

- **沿用模板**（任务提供了输出文件/模板/示例时）：
  先读取并严格沿用其表头、列名、列序与形态，只填数据。
  不增删改列、不改变表格形态、不自创格式。
- **时区精确**（任务涉及时间/日历/跨时区计算时）：
  先认清来源时区并全程保持一致。加减运算基于带时区的精确值。
  写入外部系统时，偏移内联在时间值里（如 `2025-10-01T16:59:00+08:00`），
  不用裸时间，不依赖独立的 timeZone 字段。除非要求换算，优先保留来源时区。
- **聚合优先**（任务涉及数据库或批量数据检索时）：
  优先用聚合查询（如 `GROUP BY`、`COUNT`）或批量过滤一次取回所需结果，
  避免拉取全表逐行遍历、循环单条查询及重复读取同一数据源。
"""

_EXEC_PRINCIPLES_EN = """# Execution Quality Principles

## General principles

The following principles apply to any task and are the basic baseline for delivery quality. No matter how simple a task looks, check against them one by one before delivering.

- **Data fidelity**: When writing to a file or structured result, field values must match the source character for character.
  Do not normalize, rewrite, translate, complete, or truncate — keep numbering, code, units, and casing as-is.
  Write the numbers the user gave exactly as they are; do not "helpfully" reformat them.
- **Select by criteria**: When asked to select/filter/exclude, never include everything.
  Judge each item against all relevant information (including conditions that require cross-source cross-checking), and actively drop those hitting an exclusion or exemption.
- **Match write scope to intent**: A write's impact must match the task's intent.
  When only part of the data changes or existing data must be kept, modify only the target records; do not finish a partial change with a wholesale overwrite/clear/rebuild.
  Before calling a write or import tool, confirm and explicitly set key parameters such as the write mode; do not blindly trust defaults.
- **Self-check before delivery**: Before delivering, check item by item that all conditions hold — no wrong inclusion/omission, time/number/unit exact, existing data intact, and format matching the template. Fix first before delivering.
- **Reject blind writes**: A write-file tool returning success does not mean the file content is actually correct; the actual content read back via read_file is authoritative.
  If read_file only shows a default empty header (e.g. `## Sheet1`) with no actual data rows, even if the tool call reported no error and you think the task "should" have succeeded, you must truthfully judge it a generation failure and rebuild. Never fabricate file content from your imagination.

## Scenario-based principles

The following principles take effect when a task involves the corresponding scenario. The basis for judgment is the task's actual content (whether a template is given, whether time is involved, whether a database is queried), not the task label.

- **Follow the template** (when the task provides an output file/template/example):
  Read it first and strictly reuse its header, column names, column order, and shape, filling in data only.
  Do not add/drop/change columns, do not change the table shape, and do not invent a format.
- **Timezone precision** (when the task involves time/calendar/cross-timezone calculation):
  Identify the source timezone first and keep it consistent throughout. Do arithmetic on timezone-aware precise values.
  When writing to an external system, inline the offset in the time value (e.g. `2025-10-01T16:59:00+08:00`),
  do not use a naked time, and do not rely on a separate timeZone field. Unless conversion is required, preserve the source timezone.
- **Aggregate first** (when the task involves a database or batch data retrieval):
  Prefer aggregate queries (e.g. `GROUP BY`, `COUNT`) or batch filtering to fetch the required results in one shot,
  and avoid pulling the whole table for row-by-row traversal, per-row single queries, and repeatedly reading the same data source.
"""

# 000 身份段名 + 段号。
_INTRO_NAME = "identity"
_INTRO_PRIORITY = 0

# ── 020 执行优先级（新段，06-目标提示词-完整版.md §020）。────────────────────
_NAME_EXEC_PRIORITY = "execution_priority"
_PRIO_EXEC_PRIORITY = 20

_EXEC_PRIORITY_CN = """# 执行优先级

当多条规则都要求"先做"时，按以下顺序裁决：

1. 安全与合法性（含拒绝）
2. 用户显式确认的交互闸门（ask_user / 两轮卡）
3. 外部注入的强制门禁（合规、资质、技能等，按其自身说明执行）
4. 记忆检索（每轮强制检索，无例外）
5. 正常执行
6. 收尾与最终回复

例外：若生成第 2 级的确认卡或追问依赖前置背景信息，允许先执行第 4 级只读记忆检索补齐卡片参数，再弹出确认卡；但严禁借此先执行有副作用的实质操作。

同一级内，更具体的规则优先。
所有"第一个/第一步/首个响应"一律理解为"在开始实质工作前"。
"""

_EXEC_PRIORITY_EN = """# Execution Priority

When multiple rules all demand "do this first", adjudicate in this order:

1. Safety and legality (including refusal)
2. User-explicit confirmation gates (ask_user / two-turn confirmation)
3. Externally injected mandatory gates (compliance, qualification, skills, etc.; follow their own instructions)
4. Memory retrieval (mandatory every turn, no exceptions)
5. Normal execution
6. Wrap-up and final reply

Exception: if generating a level-2 confirmation card or clarifying question depends on prior context, you may first perform level-4 read-only memory retrieval to fill in the card's parameters, then show the card; but you must not use this to perform side-effectful substantive actions first.

Within the same level, the more specific rule wins.
All "first / first step / first response" always means "before starting substantive work".
"""

# ── 210 路径与权限（新段，补写静态规则；路径值剥到 1010 动态）。────────────
_NAME_PATH_PERMISSION = "path_permission"
_PRIO_PATH_PERMISSION = 210

_PATH_PERMISSION_CN = """# 路径与权限

- Bash 未显式传 `workdir` 时，在当前工作目录（cwd）中执行；显式传 `workdir` 时，本次命令在指定目录中执行。
- 一次 Bash 调用内的 `cd` 不影响后续工具调用的工作目录；相对路径基于 cwd 解析。
- 项目文件搜索、读取、编辑、测试、构建与 Git 操作限制在当前项目目录内；不要在 Agent 内部数据目录中搜索、运行或修改用户项目文件，也不要把项目中的 `config/` 自动当作 JiuwenSwarm 启动配置目录。
- 操作涉及当前项目目录与已授权目录之外的路径时，先向用户确认再继续。
- 权限被拒绝时停止重试，不要更换语法反复尝试；沙箱可写范围以运行时提供的授权目录为准。
"""

_PATH_PERMISSION_EN = """# Paths & Permissions

- When Bash is called without an explicit `workdir`, run in the current working directory (cwd); when `workdir` is passed, run this command in that directory.
- A `cd` inside one Bash call does not affect the working directory of later calls; resolve relative paths against cwd.
- Restrict project file search, read, edit, test, build and git operations to the current project directory; do not search, run or modify user project files inside the Agent internal data directory, and do not treat a project's `config/` as the JiuwenSwarm startup configuration directory.
- For operations touching paths outside the current project directory and other authorized directories, confirm with the user before continuing.
- When access is denied, stop retrying and do not try alternate syntax repeatedly; the sandbox write scope is whatever authorized directories are provided at runtime.
"""


def _promote_heading(text: str) -> str:
    """段顶 ``## `` → ``# ``：归一化独立段顶层标题为 H1，不动段内 ``## `` 子标题。"""
    if text.startswith("## "):
        return "#" + text[2:]
    return text


# ── 1001 Python 库与权限（identity 残留「运行环境」块一次性裁剪出的独立实体）─────────
_PRIO_ENV_RESIDUAL = 1001
_ENV_RESIDUAL_TITLE_CN = "# Python 库与权限"
_ENV_RESIDUAL_TITLE_EN = "# Python Libraries & Permissions"

# (Python 库块起点, 库块终点锚点) 与 沙箱权限块起点。缺失即兜底保留原块（不丢内容）。
_ENV_RESIDUAL_ANCHORS: dict[str, dict[str, str]] = {
    "cn": {
        "py_start": "Python 库",
        "py_end": "**重要提示**",
        "sandbox_start": "## 命令执行环境与权限",
    },
    "en": {
        "py_start": "Python libraries",
        "py_end": "**Important**",
        "sandbox_start": "## Command Execution Environment and Permissions",
    },
}


def _build_env_residual_entity(block: str, language: str) -> str:
    """把 identity 残留「运行环境」块裁剪成独立实体：Python 库清单 + 命令执行权限（沙箱）。

    丢弃与 env@1000 重复的平台/Shell/命令差异表。两个锚点都缺失时**原样返回 block**
    （兜底不丢内容，只会在极端改文案时出现重复）。用一次性的确定性裁剪，无跨轮状态。
    """
    if not block:
        return block
    anchor_lang = "cn" if language in ("cn", "zh") else "en"
    anchors = _ENV_RESIDUAL_ANCHORS[anchor_lang]
    title = _ENV_RESIDUAL_TITLE_CN if anchor_lang == "cn" else _ENV_RESIDUAL_TITLE_EN

    parts: list[str] = []
    py_start = block.find(anchors["py_start"])
    if py_start >= 0:
        py_end = block.find(anchors["py_end"], py_start)
        py_block = block[py_start: py_end if py_end >= 0 else len(block)].rstrip()
        if py_block:
            parts.append(py_block)
    sandbox_start = block.find(anchors["sandbox_start"])
    if sandbox_start >= 0:
        parts.append(block[sandbox_start:].rstrip())

    if not parts:
        return block  # 兜底：锚点全缺 → 原样（不丢内容）
    return title + "\n\n" + "\n\n".join(parts) + "\n"


def _split_identity(content: str, blocks: list[tuple[str, str, int]]) -> list[tuple[str, int, str]] | None:
    """按块表切分源内容；标题缺失/乱序返回 None（触发整体回退）。

    返回 [(段名, 段号, 正文)]，相邻同段自动合并。切分保证：把独立段段顶标题归一化
    （``## `` → ``# ``）还原后，所有正文按序拼接 == 原内容（零内容损失）。
    """
    if not content:
        return None

    positions: list[int] = []
    for header, _name, _prio in blocks:
        idx = content.find(header)
        if idx < 0:
            return None  # 缺失标题 → 回退
        positions.append(idx)

    for i in range(1, len(positions)):
        if positions[i] <= positions[i - 1]:
            return None  # 乱序 → 回退

    # 分段：intro + 每块。
    chunks: list[tuple[str, int, str]] = []
    intro = content[: positions[0]]
    if intro.strip():
        chunks.append((_INTRO_NAME, _INTRO_PRIORITY, intro))
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(content)
        _header, name, prio = blocks[i]
        chunks.append((name, prio, content[start:end]))

    # 相邻同段合并。
    merged: list[tuple[str, int, str]] = []
    for name, prio, text in chunks:
        if merged and merged[-1][0] == name:
            prev_name, prev_prio, prev_text = merged[-1]
            merged[-1] = (prev_name, prev_prio, prev_text + text)
        else:
            merged.append((name, prio, text))

    # 标题层级归一化：独立段段顶标题若是 ``## ``（源自巨石里的二级小节，拆分后成为该段
    # 段顶），提升为 ``# ``（H1）。带 ``# `` 顶层的段（如 200 目录模型，首块即 ``# ``）不受
    # 影响，其段内 ``## `` 子标题保留；intro（000 身份）不提升（可能以段落正文开头）。
    normalized: list[tuple[str, int, str]] = []
    for name, prio, text in merged:
        if name != _INTRO_NAME:
            text = _promote_heading(text)
        normalized.append((name, prio, text))
    return normalized
