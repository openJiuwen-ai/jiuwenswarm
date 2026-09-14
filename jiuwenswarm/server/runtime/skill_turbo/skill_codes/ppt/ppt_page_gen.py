from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.server.runtime.skill_turbo.plan_node import (
    AbortError,
    DisableThinkingMixin,
    PlanNode,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import (
    BashExecError,
    cli_path,
    combined_output,
    quote_path,
    run_bash,
)

logger = logging.getLogger(__name__)


_CHART_CANDIDATE_TYPES = {"data", "comparison", "technology", "trend"}
# designer.md L869 扩展词表（与原文一致，不增词）
_CHART_CANDIDATE_SEMANTIC_SIGNALS = (
    "数据需求",
    "图表",
    "趋势",
    "对比",
    "指标",
    "基准测试",
)
# P8.2：只读校验 / 单页 fix 硬超时，避免 read_file 或 bash 挂死拖死 gather。
_P82_READ_TIMEOUT_SECONDS = 60.0
_P82_FIX_ONE_TIMEOUT_SECONDS = 360.0
# P8.1：HTML 后处理（regex/DOM 校验）并发上限，避免多页 LLM 同时返回后在主 loop
# 上堆积同步 CPU 后处理导致协议 ping 饿死。页级 LLM 流式保持 asyncio.gather 全并发，
# 由 Executor 层 llm_concurrency_limit（若配置）单独限 LLM。
# skill_code 受 PlanCodeValidator 约束（禁 os 等），不可读环境变量；调参请改此常量。
_P8_1_POSTPROCESS_CONCURRENCY = 8

_postprocess_sem: asyncio.Semaphore | None = None


def _get_postprocess_sem() -> asyncio.Semaphore:
    """懒初始化后处理 Semaphore，避免 import 时绑定错误 event loop。"""
    global _postprocess_sem
    if _postprocess_sem is None:
        _postprocess_sem = asyncio.Semaphore(_P8_1_POSTPROCESS_CONCURRENCY)
    return _postprocess_sem


async def _run_postprocess(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """在线程池执行同步后处理，并用 Semaphore 限制同时 in-flight 的后处理路数。"""
    async with _get_postprocess_sem():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _extract_designer_section(
    text: str,
    *,
    include_charts: bool = False,
    for_content_template_fill: bool = False,
) -> str:
    """从新版 references/designer.md 提取当前生成链路需要的关键章节。

    文件 IO 由 PrepareNode 通过 read_file 工具完成后传入 text，
    skill_code 中禁止直接做文件 IO（校验器禁止 open/read_text 等）。

    for_content_template_fill=True（Stage 6 填槽）：不注入从零设计长章与 24k
    预算全书，改用密度短清单；图表候选页另附图表短片段。
    """
    if not text and not for_content_template_fill:
        return ""

    def _extract_bounded_section(header: str, end_markers: tuple[str, ...]) -> str:
        """提取指定标题到最近结束标记之间的内容，避免把无关长章节一并注入。"""
        start = text.find(header)
        if start == -1:
            return ""
        candidates = []
        for marker in end_markers:
            pos = text.find(marker, start + len(header))
            if pos != -1:
                candidates.append(pos)
        end = min(candidates) if candidates else len(text)
        return text[start:end].rstrip()

    if for_content_template_fill:
        parts = [_CONTENT_FILL_DENSITY_CHECKLIST]
        if include_charts and text:
            chart_section = _extract_bounded_section(
                "## 图表与数据可视化",
                ("\n### 激活 content-template", "\n## 图片使用规范"),
            )
            if chart_section:
                parts.append(chart_section)
            activation_section = _extract_bounded_section(
                "### 激活 content-template",
                (
                    "\n### custom 模式的图表骨架",
                    "\n### ECharts JavaScript",
                ),
            )
            if activation_section:
                parts.append(activation_section)
        return "\n\n".join(parts)

    if not text:
        return ""

    sections = [
        # 只认现行 designer.md：预算为加粗正文 **E. …**，终点「阶段 4」。
        _extract_bounded_section(
            "**E. 页面内容预算契约",
            ("\n### 阶段 4：交付",),
        ),
        _extract_bounded_section(
            "## 弹性布局模式",
            ("\n## HTML 代码规范",),
        ),
        _extract_bounded_section(
            "## 页面布局规范",
            ("\n## 视觉设计规范",),
        ),
        _extract_bounded_section(
            "## 视觉设计规范",
            ("\n## 图表与数据可视化",),
        ),
        # 只认现行终点「禁止事项」，避免吃到文末。
        _extract_bounded_section(
            "## 关键原则",
            ("\n## 禁止事项",),
        ),
    ]
    if include_charts:
        chart_section = _extract_bounded_section(
            "## 图表与数据可视化",
            ("\n### 激活 content-template", "\n## 图片使用规范"),
        )
        if chart_section:
            chart_section = chart_section.replace(
                "渲染器、`animation:false`、字体栈合并与容器高度兜底已由模板 CSS 与 "
                "CHART_SCAFFOLD 固化并强制执行；以下为骨架无法替你决策、需要自觉遵守的规则。",
                "当前 SkillTurbo 完整 HTML 分支由本提示和 P8 后置校验强制执行渲染器、"
                "`animation:false`、字体栈与容器高度规则；以下规则必须由页面显式遵守。",
            ).replace(
                "骨架已内置 `{ renderer: 'svg' }`，禁止改回 canvas。",
                "必须显式使用 `{ renderer: 'svg' }`，禁止改用 canvas。",
            )
        sections.append(chart_section)

    selected = [section for section in sections if section]
    if not selected:
        logger.warning("[P8.0] designer.md 未匹配到新版关键章节")
        return ""

    return (
        "兼容说明：以下 designer 规范中的 Grid 示例在本链路必须用等价 Flex 权重实现；"
        "不得违反当前提示词的 CSS Grid 禁令，但页面预算、纵向占用率、逐列验收、"
        "真实语义内容和图表规则保持不变。\n\n"
        + "\n\n".join(selected)
    )


# Stage 6 content 填槽用：替代 designer「E. 预算」全书（含 YAML/subagent，与填槽冲突）。
_CONTENT_FILL_DENSITY_CHECKLIST = """### PAGE_CONTENT 密度硬约束（填槽）
- 主区须填满：禁止大块空 `flex-1` 纯色或仅一行点缀。
- 高度链：可伸展容器带 `min-h-0`；子块用 `flex-shrink-0` / `flex-1 min-h-0` 分工。
- 数量与字号：卡片/要点克制（常见 ≤6 卡、核心点 ≤6）；正文 ≥14px，说明 ≥11px。
- 图表候选页须完成 CHART_SCAFFOLD 三步激活（见 designer §激活）；非图表页保持 scaffold 注释 dormant。
- 禁止先写 YAML 预算、开 subagent 或重写整页骨架。"""



_PRESET_STYLE_IDS = {"business-classic", "tech-minimal", "elegant-narrative", "industrial-tech"}
# Stage 6 §3.5/§3.6：预设四风格 + custom 走官方模板预铺填槽（结构页 + 内容页）。
_AGENDA_TEMPLATE_FILL_STYLE_IDS = _PRESET_STYLE_IDS | {"custom"}
_STRUCTURAL_TEMPLATE_PAGE_TYPES: dict[str, str] = {
    "cover": "cover",
    "intro": "cover",
    "agenda": "agenda",
    "section": "section",
    "chapter": "section",
    "ending": "ending",
    "conclusion": "ending",
    "transition": "ending",
}
_DEFAULT_GEN_RETRY_ROUND = 1
_MAX_PAGE_GENERATION_ATTEMPTS = 3
_UNFILLED_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CONTENT_PAGE_TYPES = frozenset({
    "content",
    "trend",
    "data",
    "case",
    "comparison",
    "technology",
})
_PLACEHOLDER_SLOP_VALUES = frozenset({
    "",
    "—",
    "–",
    "-",
    "n/a",
    "tbd",
    "暂无",
    "待补充",
    "待定",
    "占位",
})
_MAIN_OPEN_TAG_RE = re.compile(r"<main\b[^>]*>", re.IGNORECASE)
_MAIN_CLOSE_TAG_RE = re.compile(r"</main>", re.IGNORECASE)
_HEAD_BLOCK_RE = re.compile(r"<head\b[^>]*>.*?</head>", re.IGNORECASE | re.DOTALL)
_TITLE_TAG_RE = re.compile(r"(<title\b[^>]*>)(.*?)(</title>)", re.IGNORECASE | re.DOTALL)
_H1_INNER_TEXT_RE = re.compile(
    r"(<h1\b[^>]*>)(.*?)(</h1>)",
    re.IGNORECASE | re.DOTALL,
)
_CONTENT_SAFE_OPEN_RE = re.compile(r'<div class="content-safe"', re.IGNORECASE)
# footer 块：必须位于 </main> 之后，避免误匹配内容区 flex-shrink-0 + <p> 的卡片。
# 用捕获组(footer_div)只提取 footer div 本身，不含 </main> 与 main 内容之间的文本。
_FOOTER_BLOCK_RE = re.compile(
    r'</main>.*?(<div class="[^"]*\bflex-shrink-0\b[^"]*"[^>]*>\s*<p\b[^>]*>.*?</p>\s*</div>)',
    re.IGNORECASE | re.DOTALL,
)
_P_INNER_TEXT_RE = re.compile(r"(<p\b[^>]*>)(.*?)(</p>)", re.IGNORECASE | re.DOTALL)


def _normalize_template_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _outline_needs_research(outline_page: str) -> bool:
    return "✅" in outline_page and (
        "页研究查询" in outline_page
        or "数据需求" in outline_page
        or "研究需求" in outline_page
    )


def _uses_content_template_fill(style_id: str, page_type: str, outline_page: str) -> bool:
    """四预设 ∪ custom 内容页：官方 content-template 预铺后仅填槽。"""
    if style_id not in _AGENDA_TEMPLATE_FILL_STYLE_IDS:
        return False
    if page_type in _STRUCTURAL_TEMPLATE_PAGE_TYPES:
        return False
    return _outline_needs_research(outline_page)


def _uses_structural_template_fill(style_id: str, page_type: str) -> bool:
    """普通分支下结构页是否走官方模板预铺填槽。"""
    return (
        page_type in _STRUCTURAL_TEMPLATE_PAGE_TYPES
        and style_id in _AGENDA_TEMPLATE_FILL_STYLE_IDS
    )


def _resolve_style_page_template_path(
    pptx_root: str,
    style_id: str,
    *,
    page_type: str = "agenda",
) -> str:
    """解析 references/styles/{style_id}/{page_type}-template.html 绝对路径字符串。"""
    root = pptx_root.replace("\\", "/").rstrip("/")
    return f"{root}/references/styles/{style_id}/{page_type}-template.html"


def _has_unfilled_placeholders(html: str) -> bool:
    """检测是否残留 Stage 6 软门禁关心的 {{PLACEHOLDER}}。

    先剥离 HTML 注释和 CSS 注释再检测，避免模板注释中出现的
    {{PLACEHOLDER}} 文本被误判为未填槽（如 ending-template.html
    theme-contract 中的 CSS 注释）。
    """
    stripped = _HTML_COMMENT_RE.sub("", html or "")
    stripped = _CSS_COMMENT_RE.sub("", stripped)
    return bool(_UNFILLED_PLACEHOLDER_RE.search(stripped))


def _build_structural_template_fill_prompt(
    *,
    page_number: int,
    page_type: str,
    template_page_type: str,
    style_id: str,
    style_text: str,
    outline_page: str,
    outline_full: str,
    seed_html: str,
    user_query: str = "",
) -> str:
    """构造结构页官方模板填槽 prompt（仅替换 {{}}，不重写骨架）。"""
    user_query_section = ""
    if user_query:
        user_query_section = (
            "## 用户原始 query（指导内容方向，不改变本页范围）\n"
            f"{user_query}\n\n"
        )

    outline_full_section = ""
    if outline_full.strip() and outline_full.strip() != outline_page.strip():
        outline_full_section = (
            "### 大纲全文（用于核对章节标题与页码范围）\n"
            f"{outline_full}\n\n"
        )

    _placeholder_common_tail = (
        "禁止敷衍值：空串、`—`/`–`/`-`、`N/A`、`TBD`、`暂无`、`待补充`、`待定`、`占位`\n"
        "完成后不得残留任何 `{{[A-Z][A-Z0-9_]*}}`\n"
        "直接输出完整 HTML，禁止 Markdown 代码块包裹与解释文字\n"
    )

    if style_id == "custom":
        custom_rules: dict[str, str] = {
            "cover": (
                "1. 已预铺 `custom/cover-template.html` 脚手架：逐字保留 `.ppt-slide` 硬约束、"
                "`@layer utilities` 与 `theme-contract` 插槽结构\n"
                "2. 仅替换实际出现的占位符：`{{THEME_CSS_VARIABLES}}`、`{{THEME_CSS_RULES}}`、"
                "`{{PAGE_TITLE}}`、`{{PAGE_CONTENT}}` 及 STRUCTURAL_IMAGE 相关槽位\n"
                "3. `{{PAGE_CONTENT}}` 依据风格文件设计封面；禁止 ECharts/数据图表\n"
            ),
            "agenda": (
                "1. 已预铺 `custom/agenda-template.html` 脚手架：逐字保留 `.ppt-slide` 硬约束、"
                "`@layer utilities` 与 `theme-contract` 插槽结构\n"
                "2. 仅替换实际出现的占位符：`{{THEME_CSS_VARIABLES}}`、`{{THEME_CSS_RULES}}`、"
                "`{{PAGE_TITLE}}`、`{{PAGE_CONTENT}}`；未提供的主题槽替换为空\n"
                "3. `{{PAGE_CONTENT}}` 依据风格文件设计目录正文；目录条目的页码/导航标记必须"
                "**全部统一有或全部统一无**，禁止部分条目有、部分没有\n"
                "4. 禁止在模板未定义位置发明「四章·十二节」「章数汇总大号数字」等装饰元数据\n"
                "5. **条目编号规则**（按编号格式区分，参考大纲全文中各内容页的 `### P{N}:` 编号）：\n"
                "   - 若采用 `P0X` / `PX` 页码编码格式（如 `P03`、`P05`）：序号必须对应内容页在"
                "outline 中的实际页码（如第一个内容页为 P3，则第一条为 `P03` 而非 `P01`）\n"
                "   - 若采用纯数字或中文数字格式（如 `01`、`1`、`一`、`壹`）：从 `1`/`一` 开始"
                "的自然数递增编号，不对应实际页码\n"
            ),
            "section": (
                "1. 已预铺 `custom/section-template.html` 脚手架：逐字保留硬约束与 theme-contract\n"
                "2. 仅替换实际出现的主题槽与 `{{PAGE_TITLE}}`、`{{PAGE_CONTENT}}`\n"
                "3. `{{PAGE_CONTENT}}` 设计章节过渡页；禁止 ECharts/数据图表/内容页双栏布局\n"
            ),
            "ending": (
                "1. 已预铺 `custom/ending-template.html` 脚手架：逐字保留硬约束与 theme-contract\n"
                "2. 仅替换实际出现的主题槽与 `{{PAGE_CONTENT}}`\n"
                "3. `{{PAGE_CONTENT}}` 设计简洁结束页：主文案必须为「感谢聆听」或同等简短收束语；"
                "若 outline 标题为长总结句，只能作为一句副标语，不得做成内容页布局\n"
                "4. **禁止** ECharts、数据图表、双栏正文、在模板外追加独立「感谢聆听」页脚块\n"
            ),
        }
        fill_rules = (
            f"### 填充规则（custom {template_page_type}，对齐 Stage 6 §3.6）\n"
            f"{custom_rules.get(template_page_type, custom_rules['section'])}\n"
            f"{_placeholder_common_tail}"
        )
    else:
        preset_rules: dict[str, str] = {
            "cover": (
                "1. **字面拷贝已完成**：下方 HTML 即官方 `cover-template.html` 预铺结果；"
                "禁止重写整页、禁止改标题栏/页脚/CSS/`@layer`/装饰/SVG\n"
                "2. **只替换 `{{...}}`**：以模板内实际出现的占位符为准\n"
                "3. 主标题/副标题取自 outline；禁止引入 ECharts 或正文页布局\n"
            ),
            "agenda": (
                "1. **字面拷贝已完成**：下方 HTML 即官方 `agenda-template.html` 预铺结果；"
                "禁止重写整页、禁止改标题栏/页脚/CSS/`@layer`/装饰/SVG/编号锚点/Tailwind class 顺序\n"
                "2. **只替换 `{{...}}`**：以模板内实际出现的占位符为准"
                "（常见：`{{PAGE_TITLE}}`、`{{AGENDA_DESC}}`、`{{AGENDA_N_TITLE}}`、"
                "`{{AGENDA_N_DESC}}`、`{{AGENDA_N_PAGE}}`、`{{PAGE_FOOTER}}`）\n"
                "3. **禁止自创装饰块**：不得新增模板未定义的「四章 · 十二节」、章数汇总、"
                "大号装饰数字等元数据文案\n"
                "4. **页码标记一致性**：\n"
                "   - 若模板含 `{{AGENDA_N_PAGE}}`：每条都必须填写，格式统一"
                "（如 `P03` / `P03 — P04`）；单页条目也要填（如 `P10`），"
                "禁止部分有、部分空\n"
                "   - 若模板无独立 PAGE 槽：页码信息统一写入 `{{AGENDA_N_DESC}}`"
                "（全部带或不全部带，保持一致），来源为 outline 内容概要\n"
                "5. **条目数必须等于大纲内容章节（组）数**：目录条目取自大纲各内容页分组，"
                "一一对应，禁止把两个章节合并为一条来迁就 4 条默认槽位；"
                "条目多于 4 时按模板注释「可按实际增删」复制同构条目行并顺延编号；"
                "`{{AGENDA_DESC}}` 中「共 N 章/部分」等表述必须与实际条目数一致\n"
            ),
            "section": (
                "1. **字面拷贝已完成**：下方 HTML 即官方 `section-template.html` 预铺结果；"
                "禁止重写整页、禁止改固定骨架/CSS/`@layer`/装饰\n"
                "2. **只替换 `{{...}}`**：章节号/章节标题取自 outline\n"
                "3. 禁止引入 ECharts、数据图表或内容页双栏布局\n"
            ),
            "ending": (
                "1. **字面拷贝已完成**：下方 HTML 即官方 `ending-template.html` 预铺结果；"
                "禁止重写整页、禁止改标题栏/页脚/CSS/`@layer`/装饰/SVG\n"
                "2. **只替换 `{{...}}`**：以模板内实际出现的占位符为准"
                "（如 `{{ENDING_TITLE}}`、`{{ENDING_SUBTITLE}}`、`{{SUMMARY_STAT_*}}`、"
                "`{{REPORTER_*}}`、`{{PAGE_FOOTER_*}}`）\n"
                "3. **ENDING_TITLE 主标语**：优先填入「感谢聆听」；若 outline 标题为长总结句，"
                "应放入 `{{ENDING_SUBTITLE}}` 或 `{{SUMMARY_STAT_*}}` 槽位，"
                "不得把总结句当作内容页 h1 并引入图表/双栏正文\n"
                "4. **禁止内容页元素**：不得引入 ECharts、数据图表、content 页双栏布局，"
                "或在模板结构外追加独立「感谢聆听」页脚块\n"
                "5. 可用 outline 内容概要提炼最多 4 条简短 SUMMARY_STAT 回响；不得臆造定量数据\n"
            ),
        }
        fill_rules = (
            f"### 填充规则（预设风格 {template_page_type}，对齐 Stage 6 §3.5）\n"
            f"{preset_rules.get(template_page_type, preset_rules['section'])}\n"
            "每个占位符必须填有意义内容；"
            f"{_placeholder_common_tail}"
        )

    page_type_label = page_type or template_page_type
    return (
        f"{user_query_section}"
        f"## 任务：填充第 {page_number} 页 {page_type_label} 官方模板占位符\n"
        f"style_id=`{style_id}`，模板=`{template_page_type}-template.html`。"
        "你是模板填充师，不是自由排版设计师。\n\n"
        f"{fill_rules}\n"
        "## 风格文件（配色/字体权威；不得把风格元数据写成观众可见装饰）\n"
        f"{style_text}\n\n"
        f"### 大纲 — 本页规划（{page_type_label}）\n"
        f"{outline_page}\n\n"
        f"{outline_full_section}"
        "### 预铺模板 HTML（只填槽，勿重写）\n"
        f"{seed_html}\n"
    )


def _build_agenda_template_fill_prompt(
    *,
    page_number: int,
    style_id: str,
    style_text: str,
    outline_page: str,
    outline_full: str,
    seed_html: str,
    user_query: str = "",
) -> str:
    """构造 agenda 官方模板填槽 prompt（仅替换 {{}}，不重写骨架）。"""
    return _build_structural_template_fill_prompt(
        page_number=page_number,
        page_type="agenda",
        template_page_type="agenda",
        style_id=style_id,
        style_text=style_text,
        outline_page=outline_page,
        outline_full=outline_full,
        seed_html=seed_html,
        user_query=user_query,
    )


def _extract_main_open_tag(html: str) -> str:
    match = _MAIN_OPEN_TAG_RE.search(html or "")
    return match.group(0) if match else ""


def _extract_main_inner_html(html: str) -> str:
    open_match = _MAIN_OPEN_TAG_RE.search(html or "")
    if not open_match:
        return ""
    close_match = _MAIN_CLOSE_TAG_RE.search(html or "", open_match.end())
    if not close_match:
        return ""
    return (html or "")[open_match.end():close_match.start()]


def _normalize_h1_text_only(html: str) -> str:
    return _H1_INNER_TEXT_RE.sub(r"\1__PAGE_TITLE__\3", html or "", count=1)


def _normalize_title_tag_text_only(html: str) -> str:
    return _TITLE_TAG_RE.sub(r"\1__PAGE_TITLE__\3", html or "", count=1)


def _extract_head_block(html: str) -> str:
    match = _HEAD_BLOCK_RE.search(html or "")
    return match.group(0) if match else ""


# custom 模板 head 内的占位符标签块：填槽后必然被替换，比对前需移除
_PLACEHOLDER_STYLE_BLOCK_RE = re.compile(
    r'<style\s+id="(?:theme-contract|theme-rules)"[^>]*>.*?</style>',
    re.DOTALL | re.IGNORECASE,
)
_PLACEHOLDER_ATTR_RE = re.compile(
    r'\s*(?:data-pptx-image-(?:present|policy)|data-pptx-role)'
    r'\s*=\s*"[^"]*"',
    re.IGNORECASE,
)
_PLACEHOLDER_TAG_RE = re.compile(
    r'\{\{[A-Z][A-Z0-9_]*\}\}',
    re.IGNORECASE,
)
# HTML 注释 + CSS 注释（custom 脚手架的说明注释会被 LLM 删除，属合法差异）
_HEAD_HTML_COMMENT_RE = re.compile(r"<!--.*?-->|/\*.*?\*/", re.DOTALL)
# __LOCAL_ASSET__ 注入行（导出期按页注入，非页面契约）
_LOCAL_ASSET_LINK_RE = re.compile(
    r'<link[^>]*href="__LOCAL_ASSET__[^"]*"[^>]*/?\s*>',
    re.IGNORECASE,
)


def _head_chrome_signature(html: str) -> str:
    """head chrome 签名：归一化空白 + title 内文占位化后的 head 全文。

    结构页/内容页模板填槽的 chrome（head 内 tailwind.config、防溢出 CSS、
    CDN script/link）要求逐字一致，唯一合法差异是每页 <title> 文字。
    该签名用于与 seed 模板比对，检测 LLM 违规改 chrome 或流式输出污染
    （bad case 46 的 page-1 `%20` 转义、page-9 config 断裂均落在 head）。

    custom 风格模板的 head 含 ``{{THEME_CSS_VARIABLES}}`` /
    ``{{THEME_CSS_RULES}}`` / ``{{STRUCTURAL_IMAGE_*}}`` 等占位符，
    LLM 填槽后必然替换它们（设计意图），替换后 head 文本与 seed 不一致。
    比对前移除占位符标签块（``<style id="theme-contract">`` /
    ``<style id="theme-rules">``）、``{{...}}`` 占位符、HTML/CSS 注释、
    ``__LOCAL_ASSET__`` 注入行及 ``data-pptx-image-*`` 属性，
    只比较结构性 chrome（CDN 引用、硬约束 CSS、style 标签结构）。
    """
    head = _extract_head_block(html)
    # 移除 custom 占位符标签块（theme-contract / theme-rules）
    head = _PLACEHOLDER_STYLE_BLOCK_RE.sub("", head)
    # 移除 {{...}} 占位符（seed 中保留、filled 中已替换均归一化为空）
    head = _PLACEHOLDER_TAG_RE.sub("", head)
    # 移除 HTML 注释和 CSS 注释（LLM 删除/保留属合法差异）
    head = _HEAD_HTML_COMMENT_RE.sub("", head)
    # 移除 __LOCAL_ASSET__ 注入行（导出期按页注入）
    head = _LOCAL_ASSET_LINK_RE.sub("", head)
    # 移除 data-pptx-image-* 属性（custom 模板占位符属性，填槽后值变化）
    head = _PLACEHOLDER_ATTR_RE.sub("", head)
    return _normalize_template_whitespace(
        _normalize_title_tag_text_only(head)
    )


def _structural_chrome_matches_seed(seed_html: str, filled_html: str) -> bool:
    """结构页填槽后 head chrome 必须与 seed 模板逐字一致（title 文字除外）。

    返回 False 表示 chrome 被改动或输出损坏，应触发重试而非落盘。
    """
    seed_head = _head_chrome_signature(seed_html)
    filled_head = _head_chrome_signature(filled_html)
    return bool(seed_head) and seed_head == filled_head


# --- 跨页 head 指纹投票（P8.1 gather 后） ---
# 检测目标：LLM 流式输出被污染（bad case 35 page-7、46 首尾页）。
# 指纹只取跨页"理应一致"的不变量，避免 custom 风格各页合法差异误报：
# head 内共享 CDN URL 集合（script src / link href，剔除本地资产替换
# __LOCAL_ASSET__:tailwind__ --导出阶段按页可选注入，非页面契约）
_HEAD_URL_ATTR_RE = re.compile(
    r"<(?:script|link)\b[^>]*?(?:src|href)\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_LOCAL_ASSET_URL_PREFIX = "__LOCAL_ASSET__"


def _extract_head_url_fingerprint(html: str) -> frozenset[str]:
    """head 内共享 CDN URL 集合（剔除 __LOCAL_ASSET__ 本地替换与 title）。"""
    head = _extract_head_block(html)
    if not head:
        return frozenset()
    urls = (
        u for u in _HEAD_URL_ATTR_RE.findall(head)
        if not u.startswith(_LOCAL_ASSET_URL_PREFIX)
    )
    return frozenset(urls)


# agenda 模板注释锚点：预设模板默认保留 `<!-- 条目 N -->` / `<!-- 01 -->` / `<!-- Ⅰ -->`
_AGENDA_ITEM_COMMENT_RE = re.compile(
    r"<!--\s*(?:条目\s*\d+|0*\d+|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+)\s*-->",
    re.IGNORECASE,
)
# 大纲中研究需求 ✅ 行模式
_OUTLINE_RESEARCH_REQ_RE = re.compile(r"\*\*研究需求\*\*.*?✅")


def _count_outline_content_chapters(outline_text: str) -> int:
    """从大纲中统计内容页数（研究需求为 ✅ 的页面）。"""
    pages = _split_md_pages(outline_text)
    if not pages:
        return 0
    count = 0
    for _, block in pages.items():
        if _OUTLINE_RESEARCH_REQ_RE.search(block):
            count += 1
    return count


def _find_agenda_page_num(outline_text: str) -> int:
    """从大纲中找到 agenda 页的页码，未找到返回 0。"""
    pages = _split_md_pages(outline_text)
    for page_num, block in pages.items():
        for line in block.splitlines():
            if "**类型**" in line and "agenda" in line.lower():
                return page_num
    return 0


def _count_agenda_items(html: str) -> int:
    """从 agenda 页 HTML 中统计条目数。

    对齐 pptx-craft：目录条目允许随风格使用 01/罗马数字/P03 等不同展示形式，
    因此这里只按条目结构做宽松统计，不把编号字面量当作硬门禁。
    """
    if not html:
        return 0

    comment_hits = _AGENDA_ITEM_COMMENT_RE.findall(html)
    if comment_hits:
        return len(set(comment_hits))

    main_match = _MAIN_BLOCK_RE.search(html)
    scan_html = main_match.group(0) if main_match else html
    item_count = 0
    for match in _VISIBLE_TEXT_LEAF_RE.finditer(scan_html):
        marker = _normalize_page_marker_text(match.group("text"))
        if _VISIBLE_PAGE_MARKER_RE.fullmatch(marker):
            item_count += 1
    return item_count


def _validate_agenda_item_count(
    outline_text: str,
    page_htmls: list[dict[str, Any]],
) -> list[int]:
    """校验 agenda 页条目数与大纲内容章节数是否一致。

    返回条目数不匹配的 agenda 页码列表（空列表表示通过/不适用）。
    """
    agenda_page = _find_agenda_page_num(outline_text)
    if not agenda_page:
        return []

    content_chapters = _count_outline_content_chapters(outline_text)
    if content_chapters == 0:
        return []

    for p in page_htmls:
        if int(p.get("page_num", 0)) == agenda_page:
            item_count = _count_agenda_items(str(p.get("html") or ""))
            if item_count != content_chapters:
                logger.warning(
                    "[P8.1] agenda 条目数(%d) ≠ 大纲内容章节数(%d) page=%d",
                    item_count, content_chapters, agenda_page,
                )
                return [agenda_page]
            break
    return []


def _vote_head_fingerprints(
    pages: list[dict[str, Any]],
) -> list[int]:
    """跨页 head 指纹投票：返回偏离多数派的页码列表。

    原理（bad case 35/46 校准）：
    - 多数派 URL 集 = 出现于 >半数页的 head 内 CDN URL（剔除
      __LOCAL_ASSET__ 导出期注入与页面自加渲染色等合法差异）
    - 报告条件：page 含多数派之外的"多余 URL" --流式输出损坏复制的
      特征（bad case 35 page-7 的 head 同时含损坏与完好两份 CDN 引用）
    - 仅缺失多数 URL 不报告：custom 脚手架结构页合法缺少
      fontawesome 等资源（bad case 35 的 page-1/page-15）
    - <3 页无票可投（单例结构页由 seed 比对负责）
    """
    deviant: list[int] = []
    valid = [p for p in pages if str(p.get("html") or "")]

    if len(valid) < 3:
        return deviant

    urls_by_page = {id(p): _extract_head_url_fingerprint(str(p["html"])) for p in valid}
    # 多数派 URL 集 = 出现于 >半数页的 URL（skill_codes 禁止 import collections，用 dict 计数）
    url_counter: dict[str, int] = {}
    for sig in urls_by_page.values():
        for u in sig:
            url_counter[u] = url_counter.get(u, 0) + 1
    majority_urls = frozenset(
        u for u, cnt in url_counter.items() if cnt * 2 > len(valid)
    )
    # 无多数 URL（全散）时投票不生效
    if not majority_urls:
        return deviant

    # 签名 = (是否缺失多数 URL, 是否含多余 URL)
    def _sig(page: dict[str, Any]) -> tuple[bool, bool]:
        urls = urls_by_page[id(page)]
        missing_majority = bool(majority_urls - urls)
        extra_urls = bool(urls - majority_urls)
        return missing_majority, extra_urls

    counter: dict[tuple[bool, bool], list[dict[str, Any]]] = {}
    for page in valid:
        counter.setdefault(_sig(page), []).append(page)

    for key, members in counter.items():
        _, extra_urls = key
        if not extra_urls:
            continue
        for page in members:
            deviant.append(int(page.get("page_num", 0)))
            logger.warning(
                "[P8.1] head 指纹投票偏离多数派（疑似流式输出损坏）"
                " page=%s majority=%d/%d extra_urls=%s",
                page.get("page_num"),
                max(len(m) for m in counter.values()),
                len(valid),
                sorted(urls_by_page[id(page)] - majority_urls),
            )
    return deviant


def _extract_header_block(html: str) -> str:
    content_safe_match = _CONTENT_SAFE_OPEN_RE.search(html or "")
    main_match = _MAIN_OPEN_TAG_RE.search(html or "")
    if not content_safe_match or not main_match or main_match.start() <= content_safe_match.start():
        return ""
    return (html or "")[content_safe_match.start():main_match.start()]


def _extract_footer_block(html: str) -> str:
    matches = list(_FOOTER_BLOCK_RE.finditer(html or ""))
    if not matches:
        return ""
    # 模板结构保证 </main> 之后只有一个 footer div，取第一个匹配即可。
    return matches[0].group(1)


def _normalize_footer_text_only(html: str) -> str:
    footer_block = _extract_footer_block(html)
    if not footer_block:
        return ""
    return _P_INNER_TEXT_RE.sub(r"\1__PAGE_FOOTER__\3", footer_block, count=1)


def _has_placeholder_slop(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    return normalized in _PLACEHOLDER_SLOP_VALUES


def _plain_text_fragment(html_fragment: str) -> str:
    return re.sub(r"<[^>]+>", "", html_fragment or "").strip()


def _extract_filled_title_inner(filled_html: str) -> str:
    match = _H1_INNER_TEXT_RE.search(filled_html or "")
    if match:
        return match.group(2).strip()
    match = _TITLE_TAG_RE.search(filled_html or "")
    return match.group(2).strip() if match else ""


def _extract_filled_footer_inner(filled_html: str) -> str:
    footer_block = _extract_footer_block(filled_html)
    if not footer_block:
        return ""
    match = _P_INNER_TEXT_RE.search(footer_block)
    return match.group(2).strip() if match else ""


def _replace_main_inner_html(html: str, new_inner: str) -> str:
    open_match = _MAIN_OPEN_TAG_RE.search(html or "")
    if not open_match:
        return html or ""
    close_match = _MAIN_CLOSE_TAG_RE.search(html or "", open_match.end())
    if not close_match:
        return html or ""
    return (html or "")[:open_match.end()] + new_inner + (html or "")[close_match.start():]


_REPAIRABLE_CONTENT_TEMPLATE_REASONS = frozenset({
    "content_template_chrome_changed",
    "head_chrome_changed",
    "header_chrome_changed",
    "footer_chrome_changed",
    "main_tag_changed",
})


def _is_chart_candidate_page(
    page_type: str,
    *,
    outline_page: str = "",
    research_page: str = "",
) -> bool:
    """pptx-craft designer L869：默认四类 + outline/research 语义扩展；结构页永不升格。"""
    normalized = (page_type or "").strip().lower()
    if normalized in _STRUCTURAL_TEMPLATE_PAGE_TYPES:
        return False
    if normalized in _CHART_CANDIDATE_TYPES:
        return True
    combined = f"{outline_page}\n{research_page}"
    return any(signal in combined for signal in _CHART_CANDIDATE_SEMANTIC_SIGNALS)


def _html_chart_scaffold_script_region(html_no_comments: str) -> str:
    """路径 B 扫描区：与 content-template 物理布局一致（</main> 后、</body> 前）。"""
    body_close = html_no_comments.lower().rfind("</body>")
    before_body = html_no_comments[:body_close] if body_close != -1 else html_no_comments
    main_close = before_body.lower().rfind("</main>")
    if main_close == -1:
        return ""
    return before_body[main_close:]


def _filled_chart_scaffold_is_progressed(filled_html: str) -> bool:
    """filled 中 scaffold 已填 option 或已暴露为活跃 script（非纯 dormant）。"""
    if not filled_html:
        return False
    for match in _COMMENTED_CHART_SCAFFOLD_BLOCK_RE.finditer(filled_html):
        body = match.group(2) or ""
        if _chart_scaffold_option_populated(body):
            return True
    html_no_comments = _HTML_COMMENT_RE.sub("", filled_html)
    scaffold_region = _html_chart_scaffold_script_region(html_no_comments)
    for match in _SCRIPT_BODY_RE.finditer(scaffold_region):
        body = match.group(1) or ""
        if "echarts.init" in body.lower() and _chart_scaffold_option_populated(body):
            return True
    return False


def _extract_chart_scaffold_region(filled_html: str) -> str | None:
    """从 filled 提取可合并的 scaffold 区域（注释内已填 option 或已激活 script）。"""
    for match in _COMMENTED_CHART_SCAFFOLD_BLOCK_RE.finditer(filled_html):
        body = match.group(2) or ""
        if _chart_scaffold_option_populated(body):
            return body.strip()
    html_no_comments = _HTML_COMMENT_RE.sub("", filled_html)
    scaffold_region = _html_chart_scaffold_script_region(html_no_comments)
    for match in reversed(list(_SCRIPT_BODY_RE.finditer(scaffold_region))):
        body = match.group(1) or ""
        if "echarts.init" in body.lower() and _chart_scaffold_option_populated(body):
            return match.group(0).strip()
    return None


def _merge_chart_scaffold_from_filled(seed_html: str, filled_html: str) -> str:
    """chrome repair 时保留 filled 对 scaffold 的激活/填 option 进度，避免回滚 seed dormant。"""
    if not _filled_chart_scaffold_is_progressed(filled_html):
        return seed_html
    filled_scaffold = _extract_chart_scaffold_region(filled_html)
    if not filled_scaffold:
        return seed_html
    match = _COMMENTED_CHART_SCAFFOLD_BLOCK_RE.search(seed_html)
    if match:
        return seed_html[:match.start()] + filled_scaffold + seed_html[match.end():]
    body_close = seed_html.lower().rfind("</body>")
    if body_close == -1:
        return seed_html
    prefix = seed_html[:body_close]
    for script_match in reversed(list(_SCRIPT_BODY_RE.finditer(prefix))):
        body = script_match.group(1) or ""
        if "echarts.init" in body.lower():
            return (
                seed_html[:script_match.start()]
                + filled_scaffold
                + seed_html[script_match.end():]
            )
    return seed_html[:body_close] + filled_scaffold + seed_html[body_close:]


def _repair_content_template_chrome(seed_html: str, filled_html: str) -> str | None:
    """Restore Page Chrome from seed; keep filled title/content/footer slot values.

    When the model rewrites head/header/footer/`<main>` chrome but still fills usable
    slots, reassemble onto the seed skeleton instead of forcing a full LLM retry.
    Returns None when filled output lacks extractable slot content.
    """
    if not (seed_html or "").strip() or not (filled_html or "").strip():
        return None

    title_inner = _extract_filled_title_inner(filled_html)
    if not title_inner or _has_placeholder_slop(_plain_text_fragment(title_inner)):
        return None

    main_inner = _extract_main_inner_html(filled_html)
    if not main_inner.strip() or "{{PAGE_CONTENT}}" in main_inner:
        return None

    footer_inner = _extract_filled_footer_inner(filled_html)
    if not footer_inner or _has_placeholder_slop(_plain_text_fragment(footer_inner)):
        return None

    out = seed_html
    if "{{PAGE_TITLE}}" in out:
        out = out.replace("{{PAGE_TITLE}}", title_inner)
    else:
        out = _TITLE_TAG_RE.sub(
            lambda m: f"{m.group(1)}{title_inner}{m.group(3)}",
            out,
            count=1,
        )
        out = _H1_INNER_TEXT_RE.sub(
            lambda m: f"{m.group(1)}{title_inner}{m.group(3)}",
            out,
            count=1,
        )

    if "{{PAGE_CONTENT}}" in out:
        out = out.replace("{{PAGE_CONTENT}}", main_inner)
    else:
        out = _replace_main_inner_html(out, main_inner)

    if "{{PAGE_FOOTER}}" in out:
        out = out.replace("{{PAGE_FOOTER}}", footer_inner)
    else:
        # fallback：seed 模板无 {{PAGE_FOOTER}} 占位符时，直接在 footer div 的
        # <p> 标签内替换文本。_P_INNER_TEXT_RE 只匹配 count=1（footer block 内
        # 第一个 <p>），不会双重替换——footer_inner 是纯文本，不含 <p> 标签。
        seed_footer = _extract_footer_block(out)
        if not seed_footer:
            return None
        repaired_footer = _P_INNER_TEXT_RE.sub(
            lambda m: f"{m.group(1)}{footer_inner}{m.group(3)}",
            seed_footer,
            count=1,
        )
        out = out.replace(seed_footer, repaired_footer, 1)

    out = _merge_chart_scaffold_from_filled(out, filled_html)
    return out


def _repair_structural_page_chrome(seed_html: str, filled_html: str) -> str | None:
    """结构页 chrome 修复：从 seed 恢复 head，保留 filled 的 body 内容。

    与 _repair_content_template_chrome 不同，结构页（cover/agenda/section/
    ending）没有 <main>/<footer> 标签，不能用 main_inner 提取。
    本函数从 filled 的 <body> 中提取 .ppt-slide 整块内容（含已填的
    {{PAGE_TITLE}}/{{PAGE_CONTENT}}/{{PAGE_FOOTER}} 等槽位值），
    替换 seed 的对应占位符，保留 seed 的 head chrome 不变。

    返回 None 表示无法提取有效 body 内容。
    """
    if not (seed_html or "").strip() or not (filled_html or "").strip():
        return None

    # 从 filled 提取 <body> 内的 .ppt-slide 整块
    body_match = re.search(
        r"<body\b[^>]*>(.*?)</body>",
        filled_html,
        re.DOTALL | re.IGNORECASE,
    )
    if not body_match:
        return None
    body_inner = body_match.group(1).strip()
    if not body_inner or "{{" in body_inner:
        # body 仍有未填占位符，修复无意义
        return None

    # 从 filled 提取 <title> 文字（用于替换 seed 的 {{PAGE_TITLE}}）
    title_match = re.search(
        r"<title\b[^>]*>(.*?)</title>",
        filled_html,
        re.DOTALL | re.IGNORECASE,
    )
    title_text = title_match.group(1).strip() if title_match else ""

    # 从 seed 的 body 中提取 .ppt-slide 整块（含占位符），
    # 用 filled 的 body 内容替换
    seed_body_match = re.search(
        r"(<body\b[^>]*>)(.*?)(</body>)",
        seed_html,
        re.DOTALL | re.IGNORECASE,
    )
    if not seed_body_match:
        return None

    # 组装：seed head + seed body 开标签 + filled body 内容 + seed body 闭标签
    # head 从 seed 取（chrome 不变），body 内容从 filled 取（已填槽）
    head_end = seed_html.find("</head>")
    if head_end < 0:
        return None
    head_end += len("</head>")

    out = seed_html[:head_end]  # seed head（chrome 不变）
    out += f"\n{seed_body_match.group(1)}\n"
    out += body_inner
    out += f"\n{seed_body_match.group(3)}\n"

    # 如果 seed 中有 {{PAGE_TITLE}} 占位符且 filled 有 title 文字，替换
    if title_text and "{{PAGE_TITLE}}" in out:
        out = out.replace("{{PAGE_TITLE}}", title_text)

    return out


def _validate_content_template_fill_output(seed_html: str, filled_html: str) -> tuple[bool, str]:
    """Stage 6 软门禁：内容页须基于 seed 填槽；head/header/footer 骨架不可改。

    图表候选页允许修改 footer 之后的 CHART_SCAFFOLD（不在 head/header/footer 比对范围内）。
    """
    if not _is_valid_html(filled_html):
        return False, "invalid_html"
    if _has_unfilled_placeholders(filled_html):
        return False, "unfilled_placeholders"
    if _normalize_template_whitespace(seed_html) == _normalize_template_whitespace(filled_html):
        return False, "seed_not_modified"

    seed_main_tag = _extract_main_open_tag(seed_html)
    filled_main_tag = _extract_main_open_tag(filled_html)
    if not seed_main_tag or seed_main_tag != filled_main_tag:
        return False, "main_tag_changed"

    seed_head = _normalize_template_whitespace(_normalize_title_tag_text_only(_extract_head_block(seed_html)))
    filled_head = _normalize_template_whitespace(_normalize_title_tag_text_only(_extract_head_block(filled_html)))
    if not seed_head or seed_head != filled_head:
        return False, "head_chrome_changed"

    seed_header = _normalize_template_whitespace(_normalize_h1_text_only(_extract_header_block(seed_html)))
    filled_header = _normalize_template_whitespace(_normalize_h1_text_only(_extract_header_block(filled_html)))
    if not seed_header or seed_header != filled_header:
        return False, "header_chrome_changed"

    seed_footer = _normalize_template_whitespace(_normalize_footer_text_only(seed_html))
    filled_footer = _normalize_template_whitespace(_normalize_footer_text_only(filled_html))
    if not seed_footer or seed_footer != filled_footer:
        return False, "footer_chrome_changed"

    main_inner_html = _extract_main_inner_html(filled_html)
    if not main_inner_html.strip():
        return False, "empty_page_content"
    if "{{PAGE_CONTENT}}" in main_inner_html:
        return False, "page_content_unfilled"

    title_match = _H1_INNER_TEXT_RE.search(filled_html)
    if not title_match or _has_placeholder_slop(re.sub(r"<[^>]+>", "", title_match.group(2))):
        return False, "title_invalid"

    footer_block = _extract_footer_block(filled_html)
    if not footer_block:
        return False, "footer_missing"
    footer_text = re.sub(r"<[^>]+>", "", footer_block).strip()
    if _has_placeholder_slop(footer_text):
        return False, "footer_invalid"

    if not _validate_slide_dom(filled_html):
        return False, "invalid_dom"
    if not _validate_chart_height_chain(filled_html):
        return False, "invalid_chart_height_chain"
    # mount 错配不在此硬拒：对齐 pptx-craft（落盘后由 check-layout 等软修），禁止进 missing
    return True, ""


def _validate_custom_content_template_fill_output(
    seed_html: str, filled_html: str
) -> tuple[bool, str]:
    """custom 内容页填槽轻量校验（对齐结构页；不做 head 全等 / chrome repair）。"""
    if not _is_valid_html(filled_html):
        return False, "invalid_html"
    if _has_unfilled_placeholders(filled_html):
        return False, "unfilled_placeholders"
    seed_main_tag = _extract_main_open_tag(seed_html)
    filled_main_tag = _extract_main_open_tag(filled_html)
    if not seed_main_tag or seed_main_tag != filled_main_tag:
        return False, "main_tag_changed"
    main_inner_html = _extract_main_inner_html(filled_html)
    if not main_inner_html.strip():
        return False, "empty_page_content"
    if "{{PAGE_CONTENT}}" in main_inner_html:
        return False, "page_content_unfilled"
    if not _validate_slide_dom(filled_html):
        return False, "invalid_dom"
    if not _validate_chart_height_chain(filled_html):
        return False, "invalid_chart_height_chain"
    # mount 错配不在此硬拒：对齐 pptx-craft，禁止因图表 id 进 missing
    return True, ""


def _build_content_layout_template(page_type: str) -> str:
    layout = _PAGE_LAYOUT_TEMPLATES.get(page_type, "")
    if not layout:
        return ""
    return (
        layout
        .replace('<div class="content-safe flex flex-col">\n', "")
        .replace('  <header class="flex-shrink-0">4-6 个关键数字卡片，flex</header>\n', "")
        .replace('  <header class="flex-shrink-0">3 个关键数字卡片</header>\n', "")
        .replace('  <header class="flex-shrink-0">4 个关键数字卡片</header>\n', "")
        .replace('  <footer class="flex-shrink-0">数据来源汇总条</footer>\n', "")
        .replace('  <footer class="flex-shrink-0">案例素材详细描述 + 数据来源页脚</footer>\n', "")
        .replace("</div>\n```\n", "```\n")
    )


def _build_content_template_fill_prompt(
    *,
    page_number: int,
    style_id: str,
    style_text: str,
    outline_page: str,
    research_page: str,
    outline_full: str,
    seed_html: str,
    image_map_page: str = "",
    designer_md_text: str = "",
    user_query: str = "",
    total_pages: int = 0,
    rewrite_hint: str = "",
    original_html: str = "",
) -> str:
    """内容页 content-template 预铺填槽 prompt（四预设三槽；custom 含 THEME_*）。"""
    user_query_section = ""
    if user_query:
        user_query_section = (
            "## 用户原始 query（用于指导内容方向和视觉风格要求）\n"
            f"{user_query}\n"
            f"⚠️ 用户 query 中的页数/总量要求已由大纲规划完成，本步骤**仅填充第 {page_number} 页内容页模板**。\n\n"
        )
    # outline_full 故意不注入：本页 outline_page + research 已够；全文易胀 prompt、诱长推理。
    _ = outline_full
    page_type = _detect_page_type(outline_page)
    is_chart_candidate = _is_chart_candidate_page(
        page_type,
        outline_page=outline_page,
        research_page=research_page,
    )
    page_number_rule = _build_visible_page_number_rule(
        user_query,
        page_number,
        total_pages or page_number,
    )
    designer_section = ""
    designer_md = _extract_designer_section(
        designer_md_text or "",
        include_charts=is_chart_candidate,
        for_content_template_fill=True,
    )
    if designer_md:
        if is_chart_candidate:
            designer_section = (
                f"\n## skill designer 约束（PAGE_CONTENT 布局 + CHART_SCAFFOLD 激活）\n"
                f"{designer_md}\n"
            )
        else:
            designer_section = (
                f"\n## skill designer 约束（PAGE_CONTENT 布局）\n{designer_md}\n"
            )
    layout_template = _build_content_layout_template(page_type)
    rewrite_section = ""
    if rewrite_hint:
        rewrite_section = (
            "\n## 重写指引（必须修复的问题）\n"
            f"{rewrite_hint}\n"
            "⚠️ 仅修复上述不通过项，不要改动其他正常部分。\n"
        )
        if original_html:
            rewrite_section += (
                "⚠️ 本轮必须以“上次产物（原始 HTML）”为编辑基底做定点修复，"
                "不要回退为从预铺 seed 模板重新整页填充。\n"
                "⚠️ `seed_html` 仅用于约束骨架/Chrome/占位符边界；"
                "`original_html` 才是当前页面已形成状态的来源。\n"
            )
        if is_chart_candidate:
            if style_id == "custom":
                rewrite_section += (
                    "图表候选页重填时：`CHART_SCAFFOLD` 内 formatter/IIFE/init 包装不得改写，"
                    "仅替换 `const option = null` 与 `CHART_FONT_FAMILY`。\n"
                )
            else:
                rewrite_section += (
                    "图表候选页重填时：`CHART_SCAFFOLD` 内 formatter/IIFE/init 包装不得改写，"
                    "仅替换 `const option = null`。\n"
                )
    if style_id == "custom":
        page_content_rule = (
            "5. `{{PAGE_CONTENT}}` 必须作为 `<main class=\"page-main\">` 的至少两个直接子块"
            "（例如结论条/关键指标 + 主体图表或分栏）；高度归属写在各直接子块上"
            "（如 `flex-shrink-0` / `flex-1 min-h-0`）；需要分栏时只在其中一个直接子块内部"
            "使用 grid/flex-row，禁止再用唯一根容器包住全部内容\n"
        )
        task_line = (
            f"## 任务：填充第 {page_number} 页 custom content-template 官方模板\n"
        )
        custom_rule_2 = (
            "2. 仅替换实际出现的占位符：`{{THEME_CSS_VARIABLES}}`、`{{THEME_CSS_RULES}}`、"
            "`{{PAGE_TITLE}}`、`{{PAGE_CONTENT}}`、`{{PAGE_FOOTER}}`；未提供的主题槽替换为空；"
            "图表候选页还须编辑 `</body>` 前 `CHART_SCAFFOLD` 块（删定界符、填 option）\n"
            if is_chart_candidate
            else
            "2. 仅替换实际出现的占位符：`{{THEME_CSS_VARIABLES}}`、`{{THEME_CSS_RULES}}`、"
            "`{{PAGE_TITLE}}`、`{{PAGE_CONTENT}}`、`{{PAGE_FOOTER}}`；未提供的主题槽替换为空\n"
        )
        custom_rule_8 = (
            "8. 图表候选页：按 designer §激活 完成 scaffold；"
            "除 option 与 `CHART_FONT_FAMILY` 外不得改动骨架代码；"
            "禁止在 `{{PAGE_CONTENT}}` 另写第二套 `echarts.init`\n"
            if is_chart_candidate
            else
            "8. 非图表候选页：`CHART_SCAFFOLD` 保持 dormant 注释，禁止修改 scaffold\n"
        )
        fill_rules = (
            "## 填充规则（对齐 Stage 6 §3.6，严格遵守）\n"
            "1. 已预铺 `custom/content-template.html` 脚手架：逐字保留 `.ppt-slide` 硬约束、"
            "`.content-safe` / `.page-header` / `.page-main` / `.page-footer`、"
            "`@layer utilities` 与 theme-contract 插槽结构\n"
            f"{custom_rule_2}"
            "3. `{{PAGE_TITLE}}` 只填写本页标题文字；不得改 `<h1>` / `.page-title` 的 class\n"
            "4. `{{PAGE_FOOTER}}` 只填写来源/备注；不得追加运行页码\n"
            f"{page_content_rule}"
            "6. 不得修改预铺模板 `<main>` 的 class；所有布局变化仅在 `{{PAGE_CONTENT}}` 内完成\n"
            "7. PAGE_* 占位符须填有意义内容；THEME 槽无内容时替换为空；"
            "禁止用 `—`/`–`/`-`、`N/A`、`TBD`、`暂无`、`待补充`、`待定`、`占位` 敷衍 PAGE_*\n"
            f"{custom_rule_8}"
            "9. 直接输出完整 HTML，禁止 Markdown 代码块包裹与解释文字\n\n"
        )
        if is_chart_candidate:
            chrome_section = (
                "## 框架约束\n"
                "- 框架与 `@layer utilities`、theme-contract **插槽结构**逐字保留；"
                "允许填入 `{{THEME_CSS_VARIABLES}}` / `{{THEME_CSS_RULES}}`\n"
                "- 允许改动：主题槽、`<title>`/`<h1>` 文字、`<main>` 内部、footer 首个 `<p>` 文字、"
                "`CHART_SCAFFOLD` 块（删定界符 + 填 option + 替换 `CHART_FONT_FAMILY`）\n"
                "- `CHART_SCAFFOLD` 不在 Page Chrome 锁内\n"
                "- 禁止增删/重排框架节点，禁止改框架 class，禁止把内容挪到 header/footer/`<head>`\n"
                "- 不要“重新生成一版更美观的同款页面”\n\n"
            )
        else:
            chrome_section = (
                "## 框架约束\n"
                "- 框架与 `@layer utilities`、theme-contract **插槽结构**逐字保留；"
                "允许填入 `{{THEME_CSS_VARIABLES}}` / `{{THEME_CSS_RULES}}`\n"
                "- 允许改动：主题槽、`<title>`/`<h1>` 文字、`<main>` 内部、footer 首个 `<p>` 文字\n"
                "- 禁止增删/重排框架节点，禁止改框架 class，禁止把内容挪到 header/footer/`<head>`\n"
                "- 不要“重新生成一版更美观的同款页面”\n\n"
            )
        if is_chart_candidate:
            seed_caption = (
                "## 预铺模板 HTML（只填槽，勿重写框架；须填满模板中实际出现的 {{...}}；"
                "骨架代码除 option 与 `CHART_FONT_FAMILY`（须按风格文件 frontmatter 完整字体栈替换）"
                "外不得改动）\n"
            )
        else:
            seed_caption = (
                "## 预铺模板 HTML（只填槽，勿重写框架；须填满模板中实际出现的 {{...}}）\n"
            )
    else:
        page_content_rule = (
            "5. `{{PAGE_CONTENT}}` 必须替换为一个且仅一个首层根容器，"
            "根容器必须带 `w-full flex-1 min-h-0`\n"
        )
        task_line = (
            f"## 任务：填充第 {page_number} 页预设风格 content-template 官方模板\n"
        )
        preset_rule_2 = (
            "2. **允许替换的可编辑区**：`{{PAGE_TITLE}}`、`{{PAGE_CONTENT}}`、`{{PAGE_FOOTER}}`；"
            "图表候选页还须编辑 `</body>` 前 `CHART_SCAFFOLD` 块（删定界符、填 `const option = {…}`）\n"
            if is_chart_candidate
            else
            "2. **只允许替换 3 类占位符**：`{{PAGE_TITLE}}`、`{{PAGE_CONTENT}}`、`{{PAGE_FOOTER}}`\n"
        )
        preset_rule_8 = (
            "8. 图表候选页：按 designer §激活 完成 scaffold；"
            "除 option 外不得改动骨架代码；"
            "预设模板 `CHART_FONT_FAMILY` 已按 style.md 预置，**禁止修改**；"
            "禁止在 `{{PAGE_CONTENT}}` 另写第二套 `echarts.init`\n"
            if is_chart_candidate
            else
            "8. 非图表候选页：`CHART_SCAFFOLD` 保持 dormant 注释，禁止修改 scaffold\n"
        )
        fill_rules = (
            "## 填充规则（对齐 Stage 6 §3.5，严格遵守）\n"
            "1. **字面拷贝已完成**：下方 HTML 即官方 `content-template.html` 预铺结果；"
            "禁止重写整页、禁止改标题栏/页脚/CSS/`@layer utilities`/装饰/SVG/Tailwind class 顺序\n"
            f"{preset_rule_2}"
            "3. `{{PAGE_TITLE}}` 只填写本页标题文字；不得改 `<h1>` 的 class、字号、字重、字体、装饰线、padding\n"
            "4. `{{PAGE_FOOTER}}` 只填写来源/备注；不得追加运行页码\n"
            f"{page_content_rule}"
            "6. 不得修改预铺模板 `<main>` 的 class；所有布局变化仅在 `{{PAGE_CONTENT}}` 内完成\n"
            "7. 每个占位符必须填有意义内容；禁止空串、`—`/`–`/`-`、`N/A`、`TBD`、`暂无`、`待补充`、`待定`、`占位`\n"
            f"{preset_rule_8}"
            "9. 直接输出完整 HTML，禁止 Markdown 代码块包裹与解释文字\n\n"
        )
        if is_chart_candidate:
            chrome_section = (
                "## Page Chrome 硬锁（违反将导致校验失败 `content_template_chrome_changed`）\n"
                "- **Chrome = 除可编辑区以外的一切**：`<head>`（含 script/link/style/`tailwind.config`）、"
                "`.content-safe` 到 `<main>` 之前的 header 带、`<main>` 开标签、"
                "footer 骨架（`</main>` 后至 `CHART_SCAFFOLD` 之前的 footer div）。"
                "**不含** `CHART_SCAFFOLD_BEGIN … END` 块\n"
                "- **允许改动的可编辑区**：\n"
                "  - `<title>` / `<h1>` 内文字 ← `{{PAGE_TITLE}}`\n"
                "  - footer 内首个 `<p>` 文字 ← `{{PAGE_FOOTER}}`\n"
                "  - `<main>` **内部** HTML ← `{{PAGE_CONTENT}}`\n"
                "  - `CHART_SCAFFOLD` 块：删定界符 + 填 option（`CHART_FONT_FAMILY` 已预置，禁止改）\n"
                "- `CHART_SCAFFOLD` 不在 Chrome 锁内\n"
                "- **禁止**增删/重排 chrome 节点，禁止改 chrome 上的 class/style/属性/注释/空白结构，"
                "禁止把图表、卡片、遮罩、装饰线挪到 header/footer/`<head>`\n"
                "- **操作方式**：以预铺 HTML 为底稿，填可编辑区后原样输出；"
                "不要“重新生成一版更美观的同款页面”\n"
                "- **自检**：输出前对比预铺稿——若除上述可编辑区外仍有任何差异，必须撤回重填\n\n"
            )
            seed_caption = (
                "## 预铺模板 HTML（只填槽，勿重写；Chrome 须与下方稿一致，"
                "除三槽与 CHART_SCAFFOLD 激活外；骨架代码除 option 外不得改动）\n"
            )
        else:
            chrome_section = (
                "## Page Chrome 硬锁（违反将导致校验失败 `content_template_chrome_changed`）\n"
                "- **Chrome = 除 `{{PAGE_CONTENT}}` 以外的一切**：`<head>`（含 script/link/style/`tailwind.config`）、"
                "`.content-safe` 到 `<main>` 之前的 header 带、`<main>` 开标签、footer 骨架及 "
                "`CHART_SCAFFOLD` dormant 注释块\n"
                "- **允许改动的仅是占位符文本**：\n"
                "  - `<title>` / `<h1>` 内文字 ← `{{PAGE_TITLE}}`\n"
                "  - footer 内首个 `<p>` 文字 ← `{{PAGE_FOOTER}}`\n"
                "  - `<main>` **内部** HTML ← `{{PAGE_CONTENT}}`\n"
                "- **禁止**增删/重排 chrome 节点，禁止改 chrome 上的 class/style/属性/注释/空白结构，"
                "禁止把图表、卡片、遮罩、装饰线挪到 header/footer/`<head>`\n"
                "- **操作方式**：以预铺 HTML 为底稿，只做三处字符串级替换后原样输出；"
                "不要“重新生成一版更美观的同款页面”\n"
                "- **自检**：输出前对比预铺稿——若除上述三处文本/main 内部外仍有任何差异，必须撤回重填\n\n"
            )
            seed_caption = (
                "## 预铺模板 HTML（只填槽，勿重写；Chrome 必须与下方稿逐字节一致，除三处占位符外）\n"
            )
    original_html_section = ""
    if original_html:
        original_html_section = (
            "\n## 上次产物（原始 HTML，作为本轮定点修复基底）\n"
            "```html\n"
            f"{original_html}\n"
            "```\n"
        )
    return (
        f"{user_query_section}"
        f"{task_line}"
        f"style_id=`{style_id}`，模板=`content-template.html`。你是模板填充师，不是自由排版设计师。\n\n"
        f"{fill_rules}"
        f"{chrome_section}"
        "## 风格文件（正文区配色/字体/组件权威；不得把风格元数据写成观众可见文字）\n"
        f"{style_text}\n\n"
        "## 大纲 — 本页规划\n"
        f"{outline_page}\n\n"
        "## 研究报告 — 本页素材\n"
        f"{research_page}\n"
        f"{_build_image_section(image_map_page)}\n"
        f"{page_number_rule}"
        f"{_EDITABLE_LAYERING_RULES}"
        f"{designer_section}"
        f"{layout_template}\n"
        f"{rewrite_section}"
        f"{original_html_section}"
        f"{seed_caption}"
        f"{seed_html}\n"
    )


def _build_content_template_fill_system_prompt(
    *,
    style_id: str,
    page_type: str,
    outline_page: str = "",
    research_page: str = "",
) -> str:
    """Stage 6 填槽 system prompt；图表候选页显式豁免 CHART_SCAFFOLD 出 Chrome 锁。"""
    is_chart = _is_chart_candidate_page(
        page_type,
        outline_page=outline_page,
        research_page=research_page,
    )
    if style_id == "custom":
        prompt = (
            "你是 PPT 内容页模板填充师，不是设计师。"
            "替换预铺 HTML 中实际出现的占位符（含 {{THEME_CSS_VARIABLES}}、"
            "{{THEME_CSS_RULES}}、{{PAGE_TITLE}}、{{PAGE_CONTENT}}、{{PAGE_FOOTER}}；"
            "未提供的主题槽填空）。保留框架 class/@layer/theme-contract 插槽结构；"
        )
        if is_chart:
            prompt += (
                "图表候选页还须编辑 </body> 前 CHART_SCAFFOLD（删定界符、填 option）；"
                "CHART_SCAFFOLD 不在 Page Chrome 锁内。"
            )
        prompt += "只输出完整 HTML 原文，不要解释、不要 Markdown 代码块。"
        return prompt
    prompt = (
        "你是 PPT 内容页模板填充师，不是设计师。"
        "唯一任务：在预铺 HTML 上替换 {{PAGE_TITLE}}、{{PAGE_CONTENT}}、{{PAGE_FOOTER}} 三处占位符。"
    )
    if is_chart:
        prompt += (
            "图表候选页还须编辑 </body> 前 CHART_SCAFFOLD（删定界符、填 option）。"
            "Page Chrome（head/header/`<main>` 开标签/footer 骨架）须与预铺稿一致；"
            "CHART_SCAFFOLD 不在 Chrome 锁内。"
        )
    else:
        prompt += (
            "Page Chrome（head/header/`<main>` 开标签/footer 骨架/class/script/style）"
            "必须与预铺稿保持一致；"
        )
    prompt += (
        "改 chrome 会触发 content_template_chrome_changed 校验失败。"
        "只输出完整 HTML 原文，不要解释、不要 Markdown 代码块。"
    )
    return prompt


_VISIBLE_PAGE_NUMBER_RULE = (
    "- 可见运行页码禁令（所有页型）：页码只用于文件名、任务定位和完整性校验，"
    "不得成为观众可见文字；禁止在 header、footer、封面、结束页或其他 Page Chrome 中"
    "生成 `P3`、`P03 / 10`、`Page 3`、`3 / 10`、`第 3 页 / 共 10 页` 等运行页码。"
    "用户要求“生成 N 页”只表示页数，不等于要求显示页码；agenda 正文中的章节目标页码"
    "属于导航内容，可以保留。\n"
)

_EDITABLE_LAYERING_RULES = (
    "- 可编辑图层约束（所有页型）：半透明遮罩仅在本页确有背景 `<img>`、且当前 "
    "style.md 明确允许时使用；DOM 与层级顺序必须是“背景图片 → 遮罩 → `relative z-10` "
    "内容层”。遮罩只能覆盖背景图片，禁止放在 main、header、footer、图表、表格、卡片、"
    "文字等语义内容上方，禁止给语义内容的父容器设置 `opacity`，也禁止把全页透明元素作为"
    "内容容器或选择代理层。\n"
    "- 全页栅格化遮罩禁令（所有页型）：禁止用 `position:absolute;inset:0`（或 "
    "`width:100%`+`height:100%`）的全页装饰元素承载 `repeating-linear-gradient`、"
    "`mix-blend-mode`、`radial-gradient`、`conic-gradient`；这些不在 html-to-pptx 支持范围"
    "内，转换器会将其栅格化为覆盖整页的图片，落在内容之上即成为挡住下层编辑的“透明罩”。"
    "背景装饰 `z-index` 必须 ≤0（低于内容层）；扫描线/故障条请用多个独立的小尺寸 "
    "solid-color `<div>` 实现，禁止用全页渐变罩。\n"
    "- 图表可编辑性约束：ECharts 必须使用 SVG renderer；禁止在 `areaStyle.color`、"
    "`itemStyle.color`、`lineStyle.color`、`visualMap.inRange.color` 等配置中使用 "
    "`colorStops`、渐变 `type: 'linear'/'radial'` 或 "
    "`echarts.graphic.LinearGradient/RadialGradient`，这些写法会使图表在 PPTX 中转成位图；"
    "需要透明面积色时使用当前风格允许的纯色 `rgba(...)`。\n"
)


@dataclass(frozen=True)
class _PageNumberPolicy:
    """用户显式可见页码要求；默认关闭。"""

    enabled: bool
    position: str = "bottom-right"
    format_kind: str = "fraction"
    zero_pad: bool = False


_PAGE_NUMBER_NEGATIVE_RE = re.compile(
    r"(?:不(?:要|需要|显示|生成|添加|保留)|无需|无须|禁止|取消|去掉|移除|删除|隐藏|关闭)"
    r".{0,8}(?:页码|页面编号)"
    r"|(?:页码|页面编号).{0,8}(?:不要|无需|无须|禁止|取消|去掉|移除|删除|隐藏|关闭)"
    r"|\b(?:no|without|hide|remove)\s+(?:page|slide)\s+numbers?\b",
    re.IGNORECASE,
)
_PAGE_NUMBER_POSITIVE_RE = re.compile(
    r"(?:显示|生成|添加|加上|加入|带上?|保留|标注|放置|设置).{0,10}(?:页码|页面编号)"
    r"|(?:页码|页面编号).{0,12}(?:显示|生成|添加|加上|加入|保留|标注|放在|放到|位于|置于|设置)"
    r"|(?:右下角|左下角|右上角|左上角).{0,8}(?:页码|页面编号)"
    r"|(?:页码|页面编号).{0,8}(?:右下角|左下角|右上角|左上角)"
    r"|\b(?:show|add|include|display)\s+(?:page|slide)\s+numbers?\b",
    re.IGNORECASE,
)


def _resolve_page_number_policy(user_query: str) -> _PageNumberPolicy:
    """仅把明确的可见页码请求识别为开启；“生成 N 页”不触发。"""
    query = str(user_query or "").strip()
    if not query or _PAGE_NUMBER_NEGATIVE_RE.search(query):
        return _PageNumberPolicy(enabled=False)
    if not _PAGE_NUMBER_POSITIVE_RE.search(query):
        return _PageNumberPolicy(enabled=False)

    if "左下" in query:
        position = "bottom-left"
    elif "右上" in query:
        position = "top-right"
    elif "左上" in query:
        position = "top-left"
    else:
        # 未指定位置时使用稳定默认值；也覆盖用户常见的“右下角页码”要求。
        position = "bottom-right"

    if re.search(r"第\s*(?:N|\d+)\s*页.{0,8}(?:共|总共|总页)", query, re.IGNORECASE):
        format_kind = "chinese-total"
    elif re.search(r"第\s*(?:N|\d+)\s*页", query, re.IGNORECASE):
        format_kind = "chinese"
    elif re.search(r"(?:格式|样式).{0,8}\bPage\s*(?:N|\d+)", query, re.IGNORECASE):
        format_kind = "english"
    elif re.search(r"(?:格式|样式).{0,8}\bP\s*(?:N|\d+)\b", query, re.IGNORECASE):
        format_kind = "p-prefix"
    elif re.search(r"(?:仅|只).{0,6}(?:当前页|数字)|不显示.{0,4}总页数", query):
        format_kind = "current"
    else:
        format_kind = "fraction"

    zero_pad = bool(re.search(r"两位|2\s*位|补零|零填充|(?:^|\D)0[1-9](?:\D|$)", query))
    return _PageNumberPolicy(
        enabled=True,
        position=position,
        format_kind=format_kind,
        zero_pad=zero_pad,
    )


def _format_visible_page_number(
    policy: _PageNumberPolicy,
    page_number: int,
    total_pages: int,
) -> str:
    total = max(int(total_pages or 0), int(page_number or 0), 1)
    width = max(2, len(str(total))) if policy.zero_pad else 1
    current_text = str(page_number).zfill(width)
    total_text = str(total).zfill(width)
    if policy.format_kind == "chinese-total":
        return f"第 {current_text} 页 / 共 {total_text} 页"
    if policy.format_kind == "chinese":
        return f"第 {current_text} 页"
    if policy.format_kind == "english":
        return f"Page {current_text}"
    if policy.format_kind == "p-prefix":
        return f"P{current_text}"
    if policy.format_kind == "current":
        return current_text
    return f"{current_text} / {total_text}"


def _build_visible_page_number_rule(
    user_query: str,
    page_number: int,
    total_pages: int,
) -> str:
    policy = _resolve_page_number_policy(user_query)
    if not policy.enabled:
        return _VISIBLE_PAGE_NUMBER_RULE
    marker = _format_visible_page_number(policy, page_number, total_pages)
    position_label = {
        "bottom-right": "右下角",
        "bottom-left": "左下角",
        "top-right": "右上角",
        "top-left": "左上角",
    }[policy.position]
    return (
        "- 用户显式页码要求（优先于默认禁令）：最终页面必须且只能显示 1 个运行页码，"
        f"固定在{position_label}，文字逐字为 `{marker}`。当前页面生成阶段不得自行创建页码，"
        "避免产生重复或格式漂移；"
        "agenda 正文中的章节目标页码仍属于导航内容，不计入这个运行页码。"
        "SkillTurbo 会在写盘前插入统一的可编辑文本页码，禁止用图片或透明罩模拟页码。\n"
    )


# 页面类型 → 模板 ID 默认映射（当 manifest 无 page_intents 时兜底）
_PAGE_TYPE_TO_TEMPLATE: dict[str, str] = {
    "cover": "cover-base",
    "intro": "cover-base",
    "agenda": "section-base",
    "chapter": "section-base",
    "section": "section-base",
    "conclusion": "section-base",
    "ending": "section-base",
    "data": "content-default",
    "trend": "content-default",
    "case": "content-cards",
    "comparison": "content-two-column",
    "technology": "content-default",
}

# 结构页类型集合（与 Charlie 分支一致）
_TEMPLATE_STRUCTURAL_TYPES = {
    "cover", "intro", "agenda", "chapter", "section", "conclusion", "ending",
}


def _looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value or value.endswith(".html")


_DEFAULT_STRUCTURAL_PAGES = 2

_CDN_HEAD_SNIPPET = (
    "### <head> CDN 引用（必须逐字使用，禁止替换为其他 CDN）\n"
    "```html\n"
    "<!-- Tailwind CSS（必选） -->\n"
    '<script src="https://cdn.digitalhumanai.top/slidagent/pptx-craft/assets/vendors/tailwind.js"></script>\n'
    "\n"
    "<!-- 字体引用 -->\n"
    '<link href="https://cdn.digitalhumanai.top/slidagent/pptx-craft/assets/css/fonts.css" rel="stylesheet" />\n'
    "\n"
    "<!-- FontAwesome 图标 -->\n"
    '<link href="https://cdn.digitalhumanai.top/slidagent/pptx-craft/assets/vendors/fontawesome/css/all.min.css"\n'
    ' rel="stylesheet" />\n'
    "\n"
    "<!-- ECharts 图表库 -->\n"
    '<script src="https://cdn.digitalhumanai.top/slidagent/pptx-craft/assets/vendors/echarts.min.js"></script>\n'
    "```\n"
    "⚠️ **禁止使用 cdn.tailwindcss.com、cdn.jsdelivr.net、cdnjs.cloudflare.com 等公共 CDN，"
    "必须使用上述 cdn.digitalhumanai.top 地址。**\n"
)


_DESIGN_RULES_DIGEST = (
    "### 视觉与布局硬约束（精选 22 条）\n"
    "1. 容器：`.ppt-slide { width:1280px; height:720px; overflow:hidden; box-sizing:border-box }`\n"
    "2. 安全区：`.content-safe { width:1220px; height:660px; margin:30px auto }`，主要内容必须放在安全区内；"
    "安全区高度固定为 660px，禁止在 `content-safe` 上添加 `h-full`、`h-[720px]` 或 `height:100%`；"
    "子元素禁止额外加 padding，否则导致双重边距\n"
    "3. 字号：严格使用风格规范文件中定义的字号值（如 business-classic.md 定义主标题 37px、正文 19px 等），"
    "不自行调整字号范围（除非用户在原始 query 中明确指定了字号，此时以用户指定值为准）；"
    "同级卡片字号必须一致，禁止个别卡片字号放大导致内容溢出\n"
    "4. 图表类型：时序数据→折线图(line)；对比数据→柱状图/分组柱状图(bar)；"
    "占比数据→饼图(pie)；多维能力对比→雷达图(radar)；禁止用图片占位\n"
    "4.1 图表渲染器（强制）：ECharts 必须用 `echarts.init(document.getElementById('xxx'), null, {renderer:'svg'})` "
    "单行初始化，禁止用变量赋值（如 `var chartDom=...; echarts.init(chartDom)`），"
    "禁止 Canvas 渲染器（会导致转 PPTX 后变位图）；"
    "初始化脚本必须写在目标图表容器之后、紧邻 `</body>`，禁止写入 `<head>`（否则 getElementById 得到 null）\n"
    "4.2 图表最小高度（强制）：图表容器实际渲染高度必须 ≥ 160px（防塌缩下限），"
    "用 `min-h-[160px]` 或 `flex-1` 确保图表区域能初始化渲染；"
    "建议图表可读高度 ≥ 300px，由页面预算保证\n"
    "4.2.1 图表高度链（强制）：包含 chart div 的最近 `flex flex-col` 祖先容器必须带 `flex-1 min-h-0`（或 `flex-[N] min-h-0`），"
    "chart div 用 `flex-1 min-h-0 w-full` 或 `w-full h-full`；"
    "标准写法 `div.flex-1.min-h-0.flex.flex-col > div#chart-1.w-full.h-full`；"
    "禁止在无 `min-h-0`/`flex-1` 的 flex-col 父容器内放置 chart div\n"
    "4.3 图表颜色（强制）：图表数据系列颜色必须来自风格文件的图表配色表，禁止使用相近色；"
    "坐标轴标签用深色，分割线用浅色\n"
    "4.4 图表标签防重叠：建议为 ECharts series 设置 `labelLayout:{moveOverlap:'shiftY'}` 防止同系列标签重叠；"
    "同一分类上的跨系列数据标签文字框必须保留至少 12px 的上下/左右安全距离；"
    "双轴柱线图必须按各自 yAxis min/max 换算视觉高度后检查，`insideTop` 与 `top` 不同也不代表安全；"
    "距离不足时调整 `position/offset/distance` 或仅隐藏碰撞点标签，且不得缩小字号；"
    "`labelLayout` 不能替代跨系列标签间距检查\n"
    "4.5 图例防叠字：图例项 ≥5 个时建议设 `legend:{type:'scroll'}` 或 `legend:{orient:'vertical'}`；"
    "横向图例还必须检查文字长度：任一中文标签超过 6 个字或全部标签合计超过 12 个字时，"
    "优先在不改变含义的前提下缩短标签，或改为纵向图例；若仍横排，`itemGap` 至少 24，"
    "并增大 `grid.top` 为图例预留空间；顶部横向图例与 yAxis.name 文字框的净间距必须 ≥18px；"
    "图表单位只能写在 ECharts `yAxis.name` / `xAxis.name`，禁止在图表卡片头部 HTML（含标题行右上角）"
    "再写单位文案，否则与轴名称形成双单位；右上角 HTML 文本仅放时间范围、来源等非单位元数据；"
    "双 Y 轴时左/右轴各自在 yAxis.name 写本单位；"
    "若图例仍与轴名冲突，通过增大 `grid.top` 或移动图例分开两条通道\n"
    "4.6 图表分割线：`splitLine` 建议使用浅色虚线，避免实线在 PPTX 中过于突兀；颜色由风格文件决定\n"
    "5. 步骤/流程页 → 用 HTML/CSS 绘制节点+连线+文字，禁止纯文字描述\n"
    "6. 关键数字必须有放大数字卡片，结论必须有摘要高亮；"
    "数据可视化量化阈值：内容页必须 ≥1 个 ECharts 图表 或 ≥3 个数据卡片"
    "（no_search 模式且页面标注'数据有限'时可降至 2 个数据卡片），"
    "否则密度检查判定为'缺数据可视化'触发重写\n"
    "6.1 核心要点量化：内容页必须有 6-10 个列表项或卡片（含数据卡片、论点卡片、要点列表），"
    "低于 6 个判定为'核心要点不足'，超过 10 个需合并精简\n"
    "6.2 装饰图标量化：内容页必须 ≥3 个 FontAwesome 图标（class 含 `fa-`），"
    "用于辅助视觉表达（如卡片标题前缀等），低于 3 个判定为'缺装饰图标'\n"
    "6.3 空白率量化：内容页估算空白率必须 < 30%，"
    "即 1220×660px 内容区内实际有内容（文字/图表/卡片/图标）的面积占比 ≥ 70%；"
    "留白 > 30% 判定为'空白率过高'；通过增加卡片、图表、列表项填充内容，而非放大字号\n"
    "7. 防溢出：单行文字不超容器宽度；连续段落 ≤ 100 字（超过必须拆列表）；"
    "文本容器（p、span、div）必须加 `break-words` 类防止中英混排时英文/数字处不换行溢出\n"
    "8. 布局结构：严格遵循标准 HTML 骨架——main 用 `flex gap-3`，"
    "恰好 2 个 `<section>` 子元素；"
    "header/main/footer 纵向排列在 content-safe 内\n"
    "8.1 禁止使用 CSS Grid：html-to-pptx 转换器不支持 `display:grid`（Grid 仅检测不转换，视为非文本容器），"
    "所有布局必须用 Flexbox（`flex`、`flex-col`、`flex-[N]`）替代 `grid grid-cols-*`；"
    "左右分栏用 `flex` + `flex-[3]` / `flex-[2]` 比例分配，不用 `grid grid-cols-[3fr_2fr]`\n"
    "8.2 `flex-[N]` 是 Tailwind 类名，必须写在 `class` 属性中（如 `class=\"flex-[3]\"`）；"
    "禁止在 `style` 属性中使用 `flex:[N]`（如 `style=\"flex:[3]\"`）——"
    "方括号是 Tailwind 语法而非有效 CSS 值，浏览器无法解析会导致 flex 比例失效、布局塌缩和内容重叠；"
    "如必须在 inline style 中设置 flex，使用 `style=\"flex: 3;\"`\n"
    "9. 需要填满父容器剩余空间的主内容区、图表区可使用 `flex-1 min-h-0 min-w-0`（水平布局）"
    "或 `flex-1 min-h-0`（垂直布局）；内容较少的纯文字卡片不强制使用 `flex-1`；"
    "禁止使用 `overflow-hidden` 隐藏核心内容\n"
    "10. flex-col 中需要填满剩余高度的主区域使用 `flex-1 min-h-0`；"
    "header、footer 和内容较少的纯文字卡片使用 `flex-shrink-0`；禁止使用 `overflow-hidden` 隐藏核心内容；"
    "注意：`overflow-hidden` 在浏览器中裁剪溢出内容，但 PPTX 导出时不被尊重——"
    "超出容器边界的内容会直接溢出；因此卡片内容必须通过控制行数和行高确保不超出容器高度\n"
    "10.1 内容预算：flex-col 中有多个子元素时，禁止把大块内容（如完整表格）设 `flex-shrink-0`，"
    "否则会挤压其他 `flex-1` 兄弟元素至高度为 0；大块内容也要参与弹性收缩或拆分\n"
    "10.2 多栏等高卡片防空白：当使用 `grid-rows-N` + `flex-1` 布局多卡片时，"
    "每个卡片内容（文字行+图标+数据）必须填充容器高度的 60% 以上；"
    "若内容不足，改用 `flex-shrink-0` 让卡片按内容自适应高度，"
    "或将 `grid-rows-N` 改为 `grid-rows-[auto]` 让容器收缩包裹内容；"
    "仅含标题和不超过 3 行正文的纯文字卡片，其卡片组和卡片本身均不得使用 `flex-1`，"
    "剩余高度优先分配给同栏的图表、图片或表格区域\n"
    "10.3 多栏卡片防溢出与行高禁令："
    "① 卡片内正文禁止使用 `leading-loose`（line-height:2），该类使文字高度翻倍，"
    "在 PPTX 导出时极易导致内容超出卡片边界；正文统一使用 `leading-snug`（1.25）或 `leading-normal`（1.5）\n"
    "② 多栏等高卡片（如 `grid-rows-N`）中每个卡片的实际内容行数不得超过容器可容纳行数"
    "（按 660px 内容区 ÷ 行数 - padding 估算）；宁可精简文字，不可溢出\n"
    "③ 禁止通过添加 `mt-auto` 底部子元素（色块标签、badge 行等）来填充空白——"
    "这些子元素增加总内容高度，在 PPTX 导出时 `overflow-hidden` 不被尊重会导致溢出\n"
    "10.4 标签归属与卡片闭合：摘要标签、badge、结论条必须完整位于其语义所属卡片的边框内，"
    "不得越过父卡片底边、贴到下一张卡片或与下一卡片标题重叠；"
    "高度不足时优先把标签并入所属卡片的标题行（`justify-between`）或表格摘要行，"
    "其次减小该卡片内部 padding/gap、调整同栏卡片 Flex 比例；"
    "禁止只删除 `mt-auto` 而不重新核算内容高度，也禁止用绝对定位、负 margin 或 `overflow-hidden` 掩盖越界\n"
    "10.5 内容页装饰边界：禁止临时创造风格文件未定义的大面积角落装饰；"
    "明确的背景装饰节点（如 `data-pptx-role=\"decoration\"`、`bg-deco*`、`bg-decoration*`）"
    "不得使用负 `top/right/bottom/left` 坐标或依赖 `.ppt-slide` 的 `overflow:hidden` 裁切；"
    "若风格明确要求角落图形，应直接绘制完整位于 1280×720 画布内、边角坐标为 0 的角形，"
    "不得把完整方形或圆形移出画布后截取一部分\n"
    "11. 配色与字体严格来自风格规范文件，禁止使用未定义的颜色或字体"
    "（除非用户在原始 query 中明确指定了字体或配色，此时以用户指定值为准）；"
    "所有页面 `<body>` 背景色必须统一，从风格规范中取一致的背景色，禁止部分页面用浅灰/灰色背景而其他页用白色；"
    "同组、同层级卡片通常保持协调的表面色与强调色；若风格允许深浅区域交替，可继续灵活使用深色卡片，"
    "但深浅变化应服务于清晰的叙事强调或分组关系，并保持字体、强调色和边界处理一致，避免无缘由的随机跳色；"
    "**字体强制声明**：风格规范文件 frontmatter 中的 `font-family` 字段声明了字体栈（如 `Noto Sans SC, WenYuan Sans SC, sans-serif`），"
    "每个页面的 `<style>` 块中必须在 `body` 选择器或 `.ppt-slide` 选择器上声明该完整字体栈，例如："
    "`body { font-family: 'Noto Sans SC', 'WenYuan Sans SC', sans-serif; }`；"
    "禁止仅在 CSS 中声明而漏掉 HTML 元素上的字体继承——所有文本元素必须继承 `body` 的 `font-family`，不得单独使用其他字体；"
    "**风格 CSS 类强制声明**：页面 `<style>` 块中必须声明本页使用的所有风格自定义 CSS 类"
    "（如 `.brandRed`、`.bg-brandRed`、`.bg-brandRedBg`、`.border-brandRed`、`.text-gray1`、"
    "`.text-grayDeep`、`.border-gray3`、`.bg-gray4` 等），色值从风格规范文件中取；"
    "缺少这些类定义会导致品牌色和灰度色全部失效，页面退化为白底黑字\n"
    "12. 页脚：底部必须有数据来源汇总条（如'数据来源：央行、财政部、...'），即使卡片内已有来源标注也必须保留页脚；"
    "禁止在页脚追加任何运行页码；页数只用于任务定位和文件完整性校验\n"
    "13. 布局实现：仅 main、图表或图片等需要承接剩余空间的区域使用 `flex-1 min-h-0`；"
    "纯文字卡片按内容自适应高度，禁止为了等高而制造大片空白；"
    "禁止使用 `overflow-hidden` 隐藏核心内容（标题/正文/图表/数据卡片等）\n"
    "13.1 表格禁用 CSS Grid：html-to-pptx 引擎不支持 `display:grid` 渲染表格，grid 表格会被转为低质量截图；"
    "数据表格必须用 `<table><tr><td>` 原生标签或 `flex` 布局替代 `grid grid-cols-N`\n"
    "14. 全局禁止 `rounded-*` 类，所有元素 border-radius:0（饼图/环形图的圆形不受此限制）\n"
    "15. 内容页根节点必须同时携带 `class=\"ppt-slide\"`、`type=\"content\"` 与 `data-page-role=\"content\"`；"
    "`data-page-role` 不是旧 `type` 属性的替代品，两者并存\n"
    "16. 标题栏、页脚为跨页锚点片段（见风格文件「四、组件样式库」开头的「跨页锚点片段」说明），"
    "必须逐字复用 HTML 结构/class/间距，只改文字内容，禁止自行重新设计\n"
    "17. 标题、正文、图表标签、数据来源和数据卡片必须完整显示，禁止裁切或隐藏；"
    "禁止在核心内容容器上使用 `overflow-hidden`（仅允许在 `.ppt-slide` 画布边界使用）；"
    "PPTX 导出不尊重 overflow-hidden，卡片内容超出边界会直接溢出，"
    "必须通过控制文字行数和行高（禁止 leading-loose）确保内容不溢出\n"
    "18. 遮罩层≠底色：`bg-black/50`、`from-black/*`、`bg-gradient-*` 等是遮罩层(overlay)，"
    "必须配合底层 `<img>` 背景图使用以保证文字可读，不是页面/卡片底色；"
    "页面底色必须严格遵循风格规范文件定义的背景色，禁止使用与风格不符的底色\n"
    "\n"
    "### html-to-pptx 转换器限制（以下规则源于转换器实际能力，非设计偏好）\n"
    "19. padding/border 转换缩放：html-to-pptx 转换器对 padding 缩放 0.85（减少 15%）、border-width 缩放 0.65（减少 35%），"
    "生成 HTML 时需预留余量，避免 PPTX 中内容因缩放溢出或边框过细\n"
)


_HTML_SKELETON = (
    "### 标准 HTML 骨架（所有页面必须遵循，禁止改动结构）\n"
    "```html\n"
    '<div class="ppt-slide" type="content" data-page-role="content">\n'
    '  <div class="content-safe flex flex-col">\n'
    '    <header class="flex-shrink-0">标题区</header>\n'
    '    <main class="flex-1 min-h-0 flex gap-3">\n'
    '      <section class="flex-1 min-h-0 min-w-0">左侧内容</section>\n'
    '      <section class="flex-1 min-h-0 min-w-0">右侧内容</section>\n'
    '    </main>\n'
    '    <footer class="flex-shrink-0">数据来源页脚</footer>\n'
    '  </div>\n'
    '</div>\n'
    "```\n"
    "规则：\n"
    "- 根节点必须同时携带 `class=\"ppt-slide\"`、`type=\"content\"`、`data-page-role=\"content\"`\n"
    "- `content-safe` 用 `flex flex-col` 纵向排列 header/main/footer 三段；高度由安全区固定为 660px，禁止添加 `h-full`\n"
    "- `main` 用 `flex` 左右分列（禁止使用 `grid grid-cols-*`，html-to-pptx 转换器不支持 CSS Grid），恰好 2 个 `<section>` 直接子元素\n"
    "- 禁止把 header/footer 放进 main 内部；禁止 main 只有 1 个子元素\n"
    "- 禁止在子元素上使用 `overflow-hidden` 隐藏核心内容（标题/正文/图表标签/数据卡片等）；overflow-hidden 仅允许用于 `.ppt-slide` 画布边界\n"
)


_AUDIENCE_VISIBLE_TEXT_RULES = (
    "### 观众可见文字来源契约（所有页型，强制）\n"
    "1. 观众可见文字只能来自用户原始 query、本页 outline、本页 research（结构页无 research）"
    "或已批准模板中的固定文案；风格名、模型提示词和制作过程信息不得写入页面。\n"
    "2. 页眉、页脚、角标、徽章、状态条、导航标签和装饰性文字同样受来源契约约束；"
    "输入没有提供对应语义时直接省略，不得为了构图、填白或强化可信感自行补充。\n"
    "3. 不得根据幻灯片页码、页面类型、研究完成状态、数据存在性、内部核对结果或生成流程"
    "推导任何观众可见标签、编号、状态或制作标记。\n"
    "4. section/chapter 仅在用户 query 或 outline 明确给出章节层级或要求显示章节编号时才显示"
    "章节导航信息；编号必须来自章节顺序语义，禁止把幻灯片页码改写为章节编号。非章节页不得"
    "自行创建章节导航信息。\n"
)


_STRUCTURAL_DESIGN_RULES = (
    "### 视觉与布局硬约束（结构页精选 8 条）\n"
    "1. 容器：`.ppt-slide { width:1280px; height:720px; overflow:hidden; box-sizing:border-box }`\n"
    "2. 安全区：`.content-safe { width:1220px; height:660px; margin:30px auto }`；"
    "高度固定为 660px，禁止在 `content-safe` 上添加 `h-full`、`h-[720px]` 或 `height:100%`\n"
    "3. 字号：封面标题 48-64px / 副标题 24-28px / 日期 18px；"
    "结束页标题 42-48px / 正文 22px"
    "（除非用户在原始 query 中明确指定了字号，此时以用户指定值为准）\n"
    "4. 防溢出：单行文字不超容器宽度\n"
    "5. 配色与字体严格来自风格规范文件，禁止使用未定义的颜色或字体"
    "（除非用户在原始 query 中明确指定了字体或配色，此时以用户指定值为准）；"
    "所有页面背景色必须统一，从风格规范中取一致的背景色，禁止部分页面自行使用不同背景色；"
    "页面背景色必须与风格规范一致，深色主题用深色底色、浅色主题用浅色底色，"
    "禁止自行使用与风格不符的渐变或底色；"
    "封面/结束页如使用图片背景，`from-black/*` 渐变层是遮罩(overlay)非底色；"
    "**字体强制声明**：风格规范文件 frontmatter 中的 `font-family` 字段声明了字体栈，"
    "每个页面的 `<style>` 块中必须在 `body` 或 `.ppt-slide` 选择器上声明该完整字体栈，例如："
    "`body { font-family: 'Noto Sans SC', 'WenYuan Sans SC', sans-serif; }`\n"
    "5.1 **风格 CSS 类强制声明**：结构页 `<style>` 块中必须声明本页使用的所有风格自定义 CSS 类"
    "（如 `.brandRed`、`.bg-brandRed`、`.bg-brandRedBg`、`.border-brandRed`、`.text-gray1`、"
    "`.text-grayDeep`、`.border-gray3`、`.bg-gray4` 等），色值从风格规范文件中取；"
    "缺少这些类定义会导致品牌色和灰度色全部失效，页面退化为白底黑字\n"
    "6. 布局：居中排列（`flex flex-col items-center justify-center`），"
    "不强制 grid-cols-2 双栏\n"
    "7. 留白：允许较高留白，**禁止堆砌数据卡片**：封面页最多保留 3 个数据卡，结束页最多保留 4 个数据回响卡；"
    "结构页核心是标题+副标题+日期/汇报人信息，不得塞入研究报告中的详细数据或图表\n"
    "8. 全局禁止 `rounded-*` 类，所有元素 border-radius:0\n"
)

_STRUCTURAL_HTML_SKELETON = (
    "### 标准 HTML 骨架（结构页专用）\n"
    "```html\n"
    "<style>\n"
    "body { font-family: 'Noto Sans SC', 'WenYuan Sans SC', sans-serif; margin: 0; }\n"
    ".ppt-slide { width: 1280px; height: 720px; overflow: hidden; box-sizing: border-box; }\n"
    "/* 必须从 style.md 取色值，声明本页使用的所有风格自定义 CSS 类，例如：\n"
    ".brandRed { color: #D33941; }  .bg-brandRed { background-color: #D33941; }\n"
    ".bg-brandRedBg { background-color: #FFF1EF; }  .border-brandRed { border-color: #D33941; }\n"
    ".text-gray1 { color: #898989; }  .text-grayDeep { color: #555757; }\n"
    ".border-gray3 { border-color: #DDDDDD; }  .bg-gray4 { background-color: #F5F5F5; } */\n"
    "</style>\n"
    '<div class="ppt-slide">\n'
    '  <div class="content-safe flex flex-col items-center justify-center">\n'
    "    <h1 class=\"text-center\">标题</h1>\n"
    "    <p class=\"text-center mt-4\">副标题</p>\n"
    "  </div>\n"
    '</div>\n'
    "```\n"
    "- 居中布局，不使用 grid-cols-2\n"
    "- 无需 header/main/footer 三段式，无需数据来源页脚\n"
    "- **font-family 必须从风格规范文件 frontmatter 中取完整字体栈**，在 <style> 中声明\n"
)


_PAGE_TYPE_RE = re.compile(r"类型\*{0,2}[：:]\s*(\w+)", re.IGNORECASE)

_PAGE_LAYOUT_TEMPLATES = {
    "data": (
        "### 参考布局（data 类型，可根据内容调整布局比例和区域数量）\n"
        "```html\n"
        '<div class="content-safe flex flex-col">\n'
        '  <header class="flex-shrink-0">4-6 个关键数字卡片，flex</header>\n'
        '  <main class="flex-1 min-h-0 flex gap-3">\n'
        '    <section class="flex-[3] min-h-0 min-w-0">6 个核心论点卡片，flex flex-col</section>\n'
        '    <section class="flex-[2] min-h-0 min-w-0">ECharts 图表 + 对比表格</section>\n'
        '  </main>\n'
        '  <footer class="flex-shrink-0">数据来源汇总条</footer>\n'
        '</div>\n'
        "```\n"
        "- ECharts 图表类型根据数据形态选择（柱状图/饼图/雷达图）\n"
    ),
    "trend": (
        "### 参考布局（trend 类型，可根据内容调整布局比例和区域数量）\n"
        "```html\n"
        '<div class="content-safe flex flex-col">\n'
        '  <header class="flex-shrink-0">3 个关键数字卡片</header>\n'
        '  <main class="flex-1 min-h-0 flex gap-3">\n'
        '    <section class="flex-1 min-h-0 min-w-0">ECharts 折线图（趋势数据）</section>\n'
        '    <section class="w-[40%] min-h-0 min-w-0 flex flex-col gap-2">\n'
        '      <div class="flex-1 min-h-0">对比表格或迷你图表，承接剩余高度</div>\n'
        '      <div class="flex-shrink-0">2-4 个紧凑洞察卡片；纯文字卡片不得使用 flex-1</div>\n'
        '    </section>\n'
        '  </main>\n'
        '  <footer class="flex-shrink-0">数据来源汇总条</footer>\n'
        '</div>\n'
        "```\n"
        "- ECharts 默认折线图(line)，数据形态更适合其他类型时可切换\n"
    ),
    "comparison": (
        "### 参考布局（comparison 类型，可根据内容调整布局比例和区域数量）\n"
        "```html\n"
        '<div class="content-safe flex flex-col">\n'
        '  <main class="flex-1 min-h-0 flex flex-col">\n'
        '    <div class="flex flex-1 min-h-0 gap-3">\n'
        '      <section class="flex-1 min-h-0 min-w-0">对比对象 A 的卡片（flex flex-col）</section>\n'
        '      <section class="flex-1 min-h-0 min-w-0">对比对象 B 的卡片（flex flex-col）</section>\n'
        '    </div>\n'
        '    <section class="flex-shrink-0">对比表格</section>\n'
        '  </main>\n'
        '  <footer class="flex-shrink-0">数据来源汇总条</footer>\n'
        '</div>\n'
        "```\n"
        "- ECharts 默认分组柱状图(grouped bar)，占比数据用饼图\n"
    ),
    "case": (
        "### 参考布局（case 类型，可根据内容调整布局比例和区域数量）\n"
        "```html\n"
        '<div class="content-safe flex flex-col">\n'
        '  <header class="flex-shrink-0">3 个关键数字卡片</header>\n'
        '  <main class="flex-1 min-h-0 flex gap-3">\n'
        '    <section class="flex-[2] min-h-0 min-w-0">6 个核心论点卡片，flex-col</section>\n'
        '    <section class="flex-[3] min-h-0 min-w-0">ECharts 图表 + 关键数据表格</section>\n'
        '  </main>\n'
        '  <footer class="flex-shrink-0">案例素材详细描述 + 数据来源页脚</footer>\n'
        '</div>\n'
        "```\n"
    ),
    "technology": (
        "### 参考布局（technology 类型，可根据内容调整布局比例和区域数量）\n"
        "```html\n"
        '<div class="content-safe flex flex-col">\n'
        '  <header class="flex-shrink-0">4 个关键数字卡片</header>\n'
        '  <main class="flex-1 min-h-0 flex flex-col gap-3">\n'
        '    <section class="flex-1 min-h-0 min-w-0">ECharts 图表 + 对比表格</section>\n'
        '    <section class="flex-1 min-h-0 min-w-0">6 个核心论点卡片，flex flex-wrap</section>\n'
        '  </main>\n'
        '  <footer class="flex-shrink-0">数据来源汇总条</footer>\n'
        '</div>\n'
        "```\n"
        "- ECharts 图表选型（与 skill charts.md 数据类型表一致，直接按数据形态选）："
        "比较类别→柱状图(bar)；时间序列→折线图(line)；类别占比→饼图(pie)；"
        "多维数据比较→雷达图(radar)；两变量关系→散点图(scatter)；"
        "单一变量分布→直方图(histogram)；数据分布/离群值→箱线图(boxplot)；"
        "层次结构→树状图(treemap)；矩阵数据→热力图(heatmap)\n"
    ),
    "cover": (
        "### 推荐布局（cover 类型，封面页）\n"
        "```html\n"
        '<div class="content-safe flex flex-col items-center justify-center">\n'
        '  <h1 class="text-[48px] font-bold text-center">演示标题</h1>\n'
        '  <p class="text-[24px] text-center mt-4">副标题</p>\n'
        '  <p class="text-[18px] text-center mt-2">日期</p>\n'
        '</div>\n'
        "```\n"
        "- 低密度页面，允许较高留白，不要求双栏、数据卡片或图表\n"
    ),
    # cover/agenda/ending 等结构页在预设四风格 + custom 下走官方模板填槽，不注入自由生成布局权威。
}


def _detect_page_type(outline_page: str) -> str:
    if not outline_page:
        return ""
    match = _PAGE_TYPE_RE.search(outline_page)
    if match:
        return match.group(1).strip().lower()
    return ""


_STRUCTURAL_DENSITY_CHECKLIST = (
    "### 结构页密度检查（5 项，全部必须通过）\n"
    "1. 完整显示：核心内容未被裁切、滚动、折叠或省略\n"
    "2. 无大段文字：无连续 > 100 字段落\n"
    "3. 视觉层级：标题 → 副标题 → 正文 层级清晰\n"
    "4. 留白质量：留白服务于视觉聚焦，非空洞\n"
    "5. 溢出风险：卡片/容器内容未超出边界，无 `leading-loose` 导致的高度翻倍\n"
)

_DENSITY_CHECKLIST_DIGEST = (
    "### 内容密度检查（17 项，全部必须通过）\n"
    "1. 数据可视化：≥1 个 ECharts 图表 或 ≥3 个数据卡片（no_search 模式且页面为'数据有限'时可降至 2 个数据卡片）\n"
    "2. 核心要点：6-10 个列表项或卡片\n"
    "3. 装饰图标：≥3 个 FontAwesome 图标（class 含 `fa-`）\n"
    "4. 留白质量：留白是否服务于层级、聚焦或阅读节奏；"
    "检查 flex-1 或 grid-rows-N 容器内的每个卡片/子元素，"
    "若内容（文字行+图表+图标）填充不足容器高度的 50%，判定为'局部空白失衡'；"
    "若纯文字卡片仅含标题和不超过 3 行正文却使用 `flex-1`，无需估算高度，直接判定为'局部空白失衡'；"
    "纯文字 `ul/ol` 或卡片组只有 1-5 个短条目、没有图表/图片/表格却使用 `flex-1` 拉满高区域时，"
    "也直接判定为'局部空白失衡'，不得把空容器、背景或装饰计作内容占用\n"
    "5. 数据来源：页脚有标注（机构名 / 资料名）\n"
    "6. 无大段文字：无连续 > 100 字段落\n"
    "7. 视觉层级：标题 → 副标题 → 正文 → 注释 层级清晰\n"
    "8. 布局正确：main 元素采用双区域布局（如 `flex` + `flex-[3]`/`flex-[2]` 等），"
    "且恰好 2 个直接子元素（`<section>` 或 `<div>`）；"
    "禁止使用 `grid grid-cols-*`（html-to-pptx 不支持 CSS Grid）；"
    "禁止所有页面使用相同布局，需根据内容叙事选择不同布局比例和方向\n"
    "9. 完整显示：核心内容未使用 line-clamp、省略号、滚动或折叠隐藏；"
    "核心内容容器（div/section/main 等）禁止使用 `overflow-hidden`（仅 `.ppt-slide` 画布边界允许）\n"
    "10. 内容完整：标题、正文、图表标签、数据来源和数据卡片全部完整显示，无裁切\n"
    "11. ECharts SVG 检查：所有 echarts.init 调用必须包含 `{renderer:'svg'}` 参数，"
    "且使用 `document.getElementById('xxx')` 直接传参，禁止变量赋值\n"
    "12. grid-cols 合法性：禁止使用 `grid-cols-*`（CSS Grid 不被转换器支持，改用 Flexbox）\n"
    "13. 字号一致性：同级别卡片/模块必须使用相同字号，字号值来自风格文件\n"
    "14. 图表颜色：数据系列颜色来自风格文件图表配色表，坐标轴标签用深色，分割线用浅色\n"
    "15. 图表标签防重叠：建议为 ECharts series 设置 `labelLayout:{moveOverlap:'shiftY'}`；"
    "同一分类上的跨系列标签文字框上下/左右安全距离不足 12px 时，"
    "无论 position 是否不同，均判定为'图表数据标签重叠风险'；"
    "图例项 ≥5 个时建议设 `legend:{type:'scroll'}` 或 `legend:{orient:'vertical'}`；"
    "横向图例任一中文标签超过 6 个字或总长度超过 12 个字时，若未缩短标签、改为纵向布局，"
    "或仍使用小于 24 的 `itemGap`，判定为'图例与轴标题重叠'；"
    "顶部横向图例与双Y轴 name 的净间距不足 18px 时也判定为该项\n"
    "16. 溢出风险：检查所有 `flex-col` 或 `flex-1` 容器内的卡片，"
    "若存在 `leading-loose`（line-height:2）或 `mt-auto` 底部子元素（色块标签/badge 行），"
    "且卡片内容总行数可能超过容器可容纳行数，判定为'内容溢出'；"
    "若 `flex-[N] min-h-0 flex-col` 高度受限卡片内已有不可收缩表格，"
    "其后又追加 `flex-shrink-0` 标签/结论块，也直接判定为'内容溢出'；"
    "所有标签必须留在语义所属卡片边框内，不得跨越父卡片底边或覆盖下一张卡片；"
    "PPTX 导出不尊重 overflow-hidden，超出边界的内容会直接溢出\n"
    "17. 装饰边界：内容页中 `data-pptx-role=\"decoration\"`、`bg-deco*`、`bg-decoration*` 等"
    "背景装饰不得使用负边界坐标或依赖画布裁切；若存在则判定为'装饰元素越界'\n"
)


def _strip_html_fence(text: str) -> str:
    """剥掉 LLM 偶尔加的 ```html ... ``` 包裹。"""
    if not text:
        return ""
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


# 匹配含 ppt-slide 的 div 开始标签（兼容单双引号）。
_PPT_SLIDE_DIV_RE = re.compile(
    r"<div[^>]*\bclass\s*=\s*(?:\"[^\"]*\bppt-slide\b[^\"]*\"|'[^']*\bppt-slide\b[^']*')",
    re.IGNORECASE,
)


def _is_valid_html(text: str) -> bool:
    if not text or len(text) < 200:
        return False
    lower = text.lower()
    if "<html" not in lower and "<!doctype html" not in lower:
        return False
    # pptx-craft：每页 HTML 有且仅有一个 .ppt-slide；多 slide 不得截断冒充成功
    if len(_PPT_SLIDE_DIV_RE.findall(text)) != 1:
        return False
    # 检测内容安全过滤截断：LLM 输出被安全过滤器中断时会嵌入审查消息
    if "sensitive information" in lower or "try a new topic" in lower:
        return False
    # 检测 HTML 结构完整性：完整文档必须包含 body 和 html 闭合标签，
    # 缺失说明输出被截断（如 max_tokens 截断、内容安全过滤中断等）
    if "</body>" not in lower or "</html>" not in lower:
        return False
    return True


_MALFORMED_HTML_RE = re.compile(
    r"</\.>|border@none|style=\"\.>",
    re.IGNORECASE,
)
_PPT_SLIDE_OPEN_RE = re.compile(
    r'<div\b[^>]*\bclass="[^"]*\bppt-slide\b',
    re.IGNORECASE,
)


def _ppt_slide_bounds(html: str) -> tuple[int, int] | None:
    """返回 .ppt-slide 内容区 [start, end) 偏移；解析失败时返回 None。"""
    slide_match = _PPT_SLIDE_OPEN_RE.search(html)
    if not slide_match:
        return None
    tag_end = html.find(">", slide_match.start())
    if tag_end < 0:
        return None
    content_start = tag_end + 1
    depth = 1
    pos = content_start
    lower = html.lower()
    while pos < len(html) and depth > 0:
        next_open = lower.find("<div", pos)
        next_close = lower.find("</div>", pos)
        if next_close == -1:
            return None
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + 4
        else:
            depth -= 1
            if depth == 0:
                return content_start, next_close
            pos = next_close + 6
    return None


def _main_inside_ppt_slide(html: str) -> bool:
    """内容页的 <main> 必须落在 .ppt-slide 容器内；无 <main> 时视为通过。"""
    main_match = re.search(r"<main\b", html, re.IGNORECASE)
    if not main_match:
        return True
    bounds = _ppt_slide_bounds(html)
    if bounds is None:
        return False
    start, end = bounds
    return start <= main_match.start() < end


def _validate_no_escaped_content(html: str) -> bool:
    """快速静态检测：内容块是否逃逸到 .ppt-slide 容器之外。

    Skill 原文等价物：pptx-craft check-layout 的 ``escaped`` 检测器
   （check-layout-u9kFnFH0.js#L1094-L1129），原文通过 Playwright 渲染后
    检查 ``[data-block]`` / ``.content-safe`` / flow anchors 是否跑到了
    ``.ppt-slide`` 之外。本函数用纯文本正则做同等效力的静态检测，
    避免渲染开销（<1ms vs 渲染 3s/页），适用于 skill turbo 快路径。

    原理：``_ppt_slide_bounds`` 已算出 ``.ppt-slide`` div 的 [start, end)。
    若 ``</main>``、``<section``、``<footer``、``<header`` 等内容块标签
    出现在 end 之后、``</body>`` 之前，说明它们是 slide 提前闭合后的孤儿元素
    （流式输出 token 损坏的典型表现：``</div></main></div></div>`` 提前闭合
    所有外层容器，后续内容变成脱流孤儿）。
    """
    bounds = _ppt_slide_bounds(html)
    if bounds is None:
        return True  # 无法定位 slide 边界时不拦截，交给其他校验
    _, slide_end = bounds
    body_close = html.lower().rfind("</body>")
    if body_close == -1:
        return True  # 无 </body> 由其他校验拦截
    # slide 闭合后到 </body> 之前的区域不应有内容块标签
    tail = html[slide_end:body_close]
    # 匹配内容块标签（开标签或闭合标签），排除注释内的
    _escaped_content_tags_re = re.compile(
        r"</?(?:main|section|footer|header|article|aside)\b",
        re.IGNORECASE,
    )
    # 去除 HTML 注释内容后再检测，避免注释里的标签误报
    tail_no_comments = re.sub(r"<!--.*?-->", "", tail, flags=re.DOTALL)
    if _escaped_content_tags_re.search(tail_no_comments):
        return False
    return True


def _validate_slide_dom(html: str) -> bool:
    """P8.1 写盘前校验：拦截 LLM 畸形片段、main 滑出 slide 及内容逃逸到 slide 之外。"""
    if _MALFORMED_HTML_RE.search(html):
        return False
    if not _main_inside_ppt_slide(html):
        return False
    if not _validate_no_escaped_content(html):
        return False
    return True


def _is_slide_exportable(html: str) -> bool:
    """P8.2 fix 后校验：仅确认导出边界内的结构未被破坏。"""
    return _main_inside_ppt_slide(html)


def _is_tool_failure_envelope(result: Any) -> bool:
    """识别 read_file 工具级失败信封（success=False）。"""
    if hasattr(result, "success") and result.success is False:
        return True
    if isinstance(result, dict) and result.get("success") is False:
        return True
    if isinstance(result, str):
        stripped = result.strip()
        return stripped.startswith("success=False") or stripped.startswith(
            "success= False"
        )
    return False


_CHART_DIV_RE = re.compile(
    r'<div\b[^>]*\bid\s*=\s*["\'][^"\']*chart[^"\']*["\'][^>]*>',
    re.IGNORECASE,
)
_FLEX_COL_DIV_RE = re.compile(
    r'<div\b[^>]*\bclass="[^"]*\bflex-col\b[^"]*"[^>]*>',
    re.IGNORECASE,
)
_CHART_WRAPPER_HEIGHT_RE = re.compile(
    r"\bmin-h-0\b|\bflex-1\b|\bflex-\[\d+\]",
    re.IGNORECASE,
)
_FLEX_GROW_RE = re.compile(
    r"\bflex-1\b|\bflex-\[\d+\]",
    re.IGNORECASE,
)


def _chart_wrapper_has_height_chain(wrapper_tag: str) -> bool:
    """designer.md 图表高度链：包装器须参与纵向高度分配（min-h-0 或 flex-1/flex-[N]）。"""
    return bool(_CHART_WRAPPER_HEIGHT_RE.search(wrapper_tag))


def _validate_chart_height_chain(html: str) -> bool:
    """P8.1 写盘前校验：ECharts 图表外层 flex-col 卡片须具备高度分配类。

    仅拦截高置信坏案（如 page-5：包装器只有 flex flex-col、无 min-h-0/flex-1）；
    无法定位包装器时不拦截，避免误伤。
    """
    if "echarts.init" not in html.lower():
        return True
    for chart_match in _CHART_DIV_RE.finditer(html):
        before = html[max(0, chart_match.start() - 2000):chart_match.start()]
        wrappers = list(_FLEX_COL_DIV_RE.finditer(before))
        if not wrappers:
            continue
        if not _chart_wrapper_has_height_chain(wrappers[-1].group(0)):
            return False
    return True


def _inject_class_into_chart_wrappers(
    html: str,
    class_name: str,
    *,
    needs_inject: Callable[[str], bool],
) -> tuple[str, int]:
    """向满足条件的图表 flex-col 包装器注入 class；已含该类则跳过。"""
    if "echarts.init" not in html.lower():
        return html, 0

    chart_divs = list(_CHART_DIV_RE.finditer(html))
    if not chart_divs:
        return html, 0

    repair_offsets: set[tuple[int, int]] = set()
    for chart_match in chart_divs:
        window_start = max(0, chart_match.start() - 2000)
        before = html[window_start:chart_match.start()]
        wrappers = list(_FLEX_COL_DIV_RE.finditer(before))
        if not wrappers:
            continue
        last_wrapper = wrappers[-1]
        if needs_inject(last_wrapper.group(0)):
            abs_start = window_start + last_wrapper.start()
            abs_end = window_start + last_wrapper.end()
            repair_offsets.add((abs_start, abs_end))

    if not repair_offsets:
        return html, 0

    result = html
    injected = 0
    class_re = re.compile(rf"\b{re.escape(class_name)}\b")
    for abs_start, abs_end in sorted(repair_offsets, reverse=True):
        wrapper_tag = result[abs_start:abs_end]
        if class_re.search(wrapper_tag):
            continue
        fixed_tag = re.sub(
            r'(class="[^"]*)"',
            rf'\1 {class_name}"',
            wrapper_tag,
            count=1,
        )
        if fixed_tag == wrapper_tag:
            continue
        result = result[:abs_start] + fixed_tag + result[abs_end:]
        injected += 1

    return result, injected


def _fix_chart_height_chain(html: str) -> str:
    """写盘前修复图表高度链：缺高度链类时注入 min-h-0，再对缺 flex-1/flex-[N] 的包装器注入 flex-1。"""
    result, count_1 = _inject_class_into_chart_wrappers(
        html,
        "min-h-0",
        needs_inject=lambda tag: not _chart_wrapper_has_height_chain(tag),
    )
    if count_1:
        logger.info(
            "[P8.1] repaired=chart_height_chain 注入 min-h-0 %d 处",
            count_1,
        )

    result, count_2 = _inject_class_into_chart_wrappers(
        result,
        "flex-1",
        needs_inject=lambda tag: not bool(_FLEX_GROW_RE.search(tag)),
    )
    if count_2:
        logger.info(
            "[P8.1] repaired=chart_height_chain 注入 flex-1 %d 处",
            count_2,
        )

    return result


_CHART_SCAFFOLD_GET_ELEMENT_RE = re.compile(
    r'document\.getElementById\(\s*(["\'])([^"\']+)\1\s*\)'
)
_CHART_SCAFFOLD_NULL_OPTION_RE = re.compile(r"\bconst\s+option\s*=\s*null\b")
_CHART_SCAFFOLD_OPTION_ASSIGN_RE = re.compile(r"\bconst\s+option\s*=")
_JS_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_JS_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _strip_js_comments(js: str) -> str:
    """去掉 JS 块注释与行注释，供 option 填充检测（忽略说明文字中的 const option = null）。"""
    without_block = _JS_BLOCK_COMMENT_RE.sub("", js or "")
    return _JS_LINE_COMMENT_RE.sub("", without_block)


def _chart_scaffold_target_id(script_body: str) -> str:
    match = _CHART_SCAFFOLD_GET_ELEMENT_RE.search(script_body or "")
    return match.group(2) if match else ""


def _html_has_element_id(html: str, element_id: str) -> bool:
    """与 pptx-craft hasChartContainer 同口径：页内存在 id="…"。"""
    if not html or not element_id:
        return False
    return (
        re.search(
            rf'(?:^|\s)id\s*=\s*(["\']){re.escape(element_id)}\1',
            html,
            re.IGNORECASE,
        )
        is not None
    )


_SCRIPT_BODY_RE = re.compile(
    r"<script\b[^>]*>(.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _validate_chart_mount_references(html: str) -> bool:
    """诊断用：活跃 echarts 脚本中 getElementById 是否在页内有对应 id。

    仅扫描去掉 HTML 注释后的 script 块，避免 dormant CHART_SCAFFOLD 误报。
    对齐 pptx-craft designer checklist；**不得**作为写盘/missing 硬门禁
    （错配最多软警告，由后续 check-layout 等处理，禁止因缺图丢页）。
    """
    if not html or "echarts.init" not in html.lower():
        return True
    html_no_comments = _HTML_COMMENT_RE.sub("", html)
    for match in _SCRIPT_BODY_RE.finditer(html_no_comments):
        script_body = match.group(1) or ""
        if "echarts.init" not in script_body.lower():
            continue
        for gid_match in _CHART_SCAFFOLD_GET_ELEMENT_RE.finditer(script_body):
            element_id = gid_match.group(2)
            if element_id and not _html_has_element_id(html, element_id):
                return False
    return True


def _warn_chart_mount_mismatch_soft(html: str, *, page_num: int | None = None) -> None:
    """mount 错配仅记软警告，不阻断写盘、不进 missing_pages。"""
    if _validate_chart_mount_references(html):
        return
    if page_num is not None:
        logger.warning(
            "[P8.1] 图表容器 id 与 getElementById 不一致（软警告，不阻断写盘） page=%d",
            page_num,
        )
    else:
        logger.warning(
            "[P8.1] 图表容器 id 与 getElementById 不一致（软警告，不阻断写盘）"
        )


def _chart_scaffold_option_populated(script_body: str) -> bool:
    """与 pptx-craft hasOptionAssignment ∧ ¬hasNullOption 同口径（仅看可执行代码）。"""
    code = _strip_js_comments(script_body or "")
    if _CHART_SCAFFOLD_NULL_OPTION_RE.search(code):
        return False
    return _CHART_SCAFFOLD_OPTION_ASSIGN_RE.search(code) is not None


def _fix_chart_scaffold_activation(html: str) -> str:
    """写盘前修复：LLM 填了 option 但忘删 CHART_SCAFFOLD 注释定界符时自动激活。

    与 pptx-craft activateTemplateChartScaffolds 前置条件一致：仅当该注释块
    option 非 null 且 getElementById 对应容器存在时，才剥该块定界符。
    """
    if not html or "CHART_SCAFFOLD" not in html:
        return html
    activated = 0
    pieces: list[str] = []
    last = 0
    for match in _COMMENTED_CHART_SCAFFOLD_BLOCK_RE.finditer(html):
        body = match.group(2) or ""
        pieces.append(html[last:match.start()])
        target_id = _chart_scaffold_target_id(body)
        if _chart_scaffold_option_populated(body) and _html_has_element_id(
            html, target_id
        ):
            pieces.append(body.strip())
            activated += 1
        else:
            pieces.append(match.group(0))
        last = match.end()
    if not activated:
        return html
    pieces.append(html[last:])
    logger.info("[P8.1] repaired=chart_scaffold_activation 激活图表骨架")
    return "".join(pieces)


def _extract_backup_timestamp(path: str) -> str:
    match = re.search(r"_backup[/\\](\d+)[/\\]", path.replace("\\", "/"))
    return match.group(1) if match else ""


_VISIBLE_PAGE_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"第\s*0*\d+\s*页(?:\s*(?:(?:[/／]|of)\s*(?:共\s*)?0*\d+\s*页|"
    r"[（(]\s*共\s*0*\d+\s*页\s*[）)]))?"
    r"|(?:page|p|页码)\s*[:：#]?\s*0*\d+(?:\s*(?:[/／]|of)\s*0*\d+)?"
    r"|0*\d+\s*[/／]\s*0*\d+"
    r")\s*$",
    re.IGNORECASE,
)
_VISIBLE_TEXT_LEAF_RE = re.compile(
    r"<(?P<tag>span|p|div|small)\b[^>]*>"
    r"(?P<text>[^<>]*)</(?P=tag)>",
    re.IGNORECASE,
)
_MAIN_BLOCK_RE = re.compile(r"<main\b[^>]*>.*?</main\s*>", re.IGNORECASE | re.DOTALL)
_PAGE_MARKER_SPACE_ENTITY_RE = re.compile(
    r"&(?:nbsp|#0*(?:32|160)|#x0*(?:20|a0));",
    re.IGNORECASE,
)
_PAGE_MARKER_SLASH_ENTITY_RE = re.compile(
    r"&(?:sol|#0*47|#x0*2f);",
    re.IGNORECASE,
)


def _normalize_page_marker_text(text: str) -> str:
    """仅还原页码识别所需的空白与斜杠实体，不依赖白名单外模块。"""
    normalized = _PAGE_MARKER_SPACE_ENTITY_RE.sub(" ", text)
    normalized = _PAGE_MARKER_SLASH_ENTITY_RE.sub("/", normalized)
    return normalized.replace("\xa0", " ").strip()


def _strip_visible_page_markers(html_text: str) -> str:
    """移除 Page Chrome 中的可见运行页码，保留 main 内的导航/业务内容。"""
    if not html_text:
        return html_text

    main_ranges = tuple(
        (match.start(), match.end())
        for match in _MAIN_BLOCK_RE.finditer(html_text)
    )
    removed_markers: list[str] = []

    def _replace_marker(match: re.Match[str]) -> str:
        # agenda 章节目标页码和可能的业务型号位于 main，不能按运行页码误删。
        if any(start <= match.start() < end for start, end in main_ranges):
            return match.group(0)
        marker = _normalize_page_marker_text(match.group("text"))
        if not _VISIBLE_PAGE_MARKER_RE.fullmatch(marker):
            return match.group(0)
        removed_markers.append(marker)
        return ""

    normalized = _VISIBLE_TEXT_LEAF_RE.sub(_replace_marker, html_text)
    if removed_markers:
        logger.info("[P8.1] 已移除可见运行页码 markers=%s", removed_markers)
    return normalized


_DIV_TAG_RE = re.compile(r"<div\b[^>]*>|</div\s*>", re.IGNORECASE)
_PAGE_NUMBER_POSITION_CSS = {
    "bottom-right": "right:30px;bottom:16px;text-align:right;",
    "bottom-left": "left:30px;bottom:16px;text-align:left;",
    "top-right": "right:30px;top:16px;text-align:right;",
    "top-left": "left:30px;top:16px;text-align:left;",
}
_PAGE_NUMBER_STYLE = {
    "business-classic": ("#898989", 12),
    "tech-minimal": ("rgba(0,0,0,0.8)", 12),
    "industrial-tech": ("#898989", 14),
    "elegant-narrative": ("#87867f", 14),
}


def _insert_visible_page_marker(
    html_text: str,
    marker_text: str,
    policy: _PageNumberPolicy,
    style_id: str,
) -> str:
    """在 ppt-slide 根容器末尾插入一个普通、可编辑的页码文本元素。"""
    root_match = _PPT_SLIDE_DIV_RE.search(html_text)
    if root_match is None:
        logger.warning("[P8.1] 可见页码插入失败：未找到 ppt-slide 根容器")
        return html_text

    depth = 0
    insertion_index: int | None = None
    for tag_match in _DIV_TAG_RE.finditer(html_text, root_match.start()):
        if tag_match.group(0).lower().startswith("</div"):
            depth -= 1
            if depth == 0:
                insertion_index = tag_match.start()
                break
        else:
            depth += 1
    if insertion_index is None:
        logger.warning("[P8.1] 可见页码插入失败：未找到 ppt-slide 闭合标签")
        return html_text

    position_css = _PAGE_NUMBER_POSITION_CSS[policy.position]
    color, font_size = _PAGE_NUMBER_STYLE.get(style_id, ("#666666", 12))
    marker = (
        "\n<span data-skill-turbo-page-number=\"true\" "
        f"data-position=\"{policy.position}\" "
        'style="position:absolute;z-index:30;min-width:48px;'
        f"{position_css}font-family:inherit;font-size:{font_size}px;"
        f"line-height:1;font-weight:400;color:{color};white-space:nowrap;"
        'background:transparent;border:0;padding:0;margin:0;">'
        # marker_text 仅由固定格式文字和整数页码组成，不包含用户原始 HTML。
        f"{marker_text}</span>\n"
    )
    return html_text[:insertion_index] + marker + html_text[insertion_index:]


def _apply_visible_page_number_policy(
    html_text: str,
    *,
    user_query: str,
    page_number: int,
    total_pages: int,
    style_id: str,
) -> str:
    """默认移除运行页码；用户明确要求时确定性统一为一个可编辑页码。"""
    policy = _resolve_page_number_policy(user_query)
    normalized = _strip_visible_page_markers(html_text)
    if not policy.enabled:
        return normalized
    marker_text = _format_visible_page_number(policy, page_number, total_pages)
    normalized = _insert_visible_page_marker(
        normalized,
        marker_text,
        policy,
        style_id,
    )
    if 'data-skill-turbo-page-number="true"' in normalized:
        logger.info(
            "[P8.1] 已统一可见页码 page=%d total=%d position=%s format=%s",
            page_number,
            total_pages,
            policy.position,
            policy.format_kind,
        )
    return normalized


# 匹配 <h1>/<h2> 中「第X页」占位符（X 为数字或中文数字）
_PLACEHOLDER_HEADING_RE = re.compile(
    r'(<(h[12])[^>]*>)\s*第\s*([\d一二三四五六七八九十]+)\s*页\s*(</\2>)',
    re.IGNORECASE,
)


def _extract_title_from_outline(outline_page: str) -> str:
    """从 outline 片段中提取页面标题，用于替换「第X页」占位符。

    outline 片段格式示例：
      ### P3: 类型*data | 标题*xxx | 研究需求*✅
      ### P3: xxx标题
    """
    if not outline_page:
        return ""
    for line in outline_page.splitlines():
        stripped = line.strip()
        if not stripped.startswith("### P"):
            continue
        # 去掉 "### P{N}:" 前缀
        rest = stripped.split(":", 1)[-1].strip() if ":" in stripped else ""
        if not rest:
            continue
        # 格式1: "类型*data | 标题*xxx | 研究需求*✅"
        if "标题" in rest:
            for seg in rest.split("|"):
                seg = seg.strip()
                if seg.startswith("标题"):
                    val = seg.split("*", 1)[-1].strip() if "*" in seg else seg.split("：", 1)[-1].strip()
                    val = val.strip("*").strip()
                    if val and val != "标题":  # 排除空值和独立"标题"segment
                        return val
            # 标题字段存在但值为空或字面量"标题"，格式异常，跳过格式2 fallback
            continue
        # 格式2: 直接是标题文本
        if rest and not rest.startswith("类型"):
            return rest
    return ""


def _replace_placeholder_headings(html: str, outline_page: str) -> str:
    """后置校验：将 <h1>/<h2> 中的「第X页」占位符替换为 outline 中的实际标题。"""
    title = _extract_title_from_outline(outline_page)
    if not title:
        return html

    def _replacer(m: re.Match) -> str:
        return f"{m.group(1)}{title}{m.group(4)}"

    return _PLACEHOLDER_HEADING_RE.sub(_replacer, html)


# 匹配 echarts.init(xxx) 未带 renderer 参数的单参数调用
# 支持两种传参：变量名 或 document.getElementById('xxx') 直接传参
# 多参数调用（含 renderer 等）天然不匹配，无需额外排除
_ECHARTS_INIT_NO_SVG_RE = re.compile(
    r"echarts\.init\(\s*"
    r"(?:"
    r"(\w+)"                                              # 形式1: 变量名
    r"|(document\.getElementById\(\s*['\"][^'\"]+['\"]\s*\))"  # 形式2: getElementById
    r")\s*\)"
)


def _fix_echarts_svg_renderer(html: str) -> str:
    """后置校验：确保所有 echarts.init 调用使用 SVG 渲染器。

    匹配两种单参数调用：变量名 或 document.getElementById('xxx')，
    自动补充 {renderer:'svg'} 参数。
    已有 renderer 参数或多参数调用不处理。
    """
    def _replacer(m: re.Match) -> str:
        arg = (m.group(1) or m.group(2) or "").strip()
        return f"echarts.init({arg}, null, {{renderer:'svg'}})"

    return _ECHARTS_INIT_NO_SVG_RE.sub(_replacer, html)


# 匹配 echarts-static-svg 容器块（用于检测空 SVG）
# 约定：容器内有且仅有一个 <svg> 根元素，且其后紧跟容器闭合 </div>
# 这样可避免容器内嵌套 <div>（图例/标题/布局包装等）导致 .*?</div> 提前截断真实 SVG 内容
_STATIC_SVG_BLOCK_RE = re.compile(
    r'<div class="echarts-static-svg"[^>]*>.*?</svg>\s*</div>',
    re.IGNORECASE | re.DOTALL,
)
# SVG 内有实际图形内容的元素
_SVG_CONTENT_TAGS = re.compile(
    r'<(?:path|rect|circle|ellipse|line|polyline|polygon|text|tspan|image|use)\b',
    re.IGNORECASE,
)


def _has_empty_chart_svg(html: str) -> bool:
    """检测是否存在空的 echarts-static-svg（有容器但 SVG 内无图形元素）。"""
    for m in _STATIC_SVG_BLOCK_RE.finditer(html):
        svg_block = m.group(0)
        if not _SVG_CONTENT_TAGS.search(svg_block):
            return True
    return False


# --- ECharts 图表容器缺少 echarts.init 初始化检测（P8.1 阶段，P8.2 fix 之前） ---
# 场景：LLM 生成了 <div id="xxxChart"> + echarts 脚本引用，但遗漏 echarts.init 调用，
# 导致 P8.2 cli.js fix 将未初始化图表转为空 SVG（页面出现大片空白）。
# 仅检测"有 ECharts 库但完全没有 echarts.init 调用"——这是最可靠的信号，
# 不依赖容器 id 命名约定或 init 调用格式，避免误报。
_ECHARTS_LIB_RE = re.compile(r'<script[^>]*echarts[\w.-]*\.js', re.IGNORECASE)
_ECHARTS_INIT_RE = re.compile(r'echarts\.init\s*\(', re.IGNORECASE)


def _has_chart_without_init(html: str) -> bool:
    """检测 ECharts 图表容器缺少 echarts.init 初始化脚本。

    在 P8.1 密度检查阶段（P8.2 cli.js fix 之前）运行。
    检测条件：HTML 引入了 ECharts 库脚本但完全没有 echarts.init 调用。
    不依赖容器 id 命名或 init 调用格式，避免误报。
    """
    if not _ECHARTS_LIB_RE.search(html):
        return False
    return not _ECHARTS_INIT_RE.search(html)


# --- 图表卡片头部 HTML 写单位检测（与 ECharts 轴名称形成双单位） ---
# 场景：LLM 在图表标题行右上角 span 又写了一遍「单位：克/日」，与 setOption 中
# yAxis.name 形成左上角轴名 + 右上角 HTML 单位的双单位。单位唯一来源应为
# ECharts yAxis.name / xAxis.name，图表卡片头部 HTML 不得再写单位文案。
_CHART_HEADER_SPAN_RE = re.compile(
    r"<span\b[^>]*>(?P<text>.*?)</span\s*>",
    re.IGNORECASE | re.DOTALL,
)
_CHART_UNIT_LABEL_RE = re.compile(
    r"^[\s（(]*单位\s*[：:].+",
    re.IGNORECASE,
)
# 仅匹配带斜杠的复合量纲，避免误伤「40%」「1.2亿元」等数据值 span
_CHART_UNIT_TOKEN_RE = re.compile(
    r"(?:千卡/(?:时|小时)|克/日|g/日|kg/日|吨/日?|元/(?:日|年|月|吨)"
    r"|次/(?:日|周|月)|小时/天|分钟/天|克/毫升)",
    re.IGNORECASE,
)
_CHART_HEADER_WINDOW = 400


def _strip_chart_header_unit(html: str) -> str:
    """剥离图表卡片头部 HTML 中的单位 span，避免与 ECharts yAxis.name 形成双单位。

    单位最终应由 yAxis.name / xAxis.name 承载。仅移除图表容器 div 前 400 字符窗口内、
    文本为「单位：xx」前缀或含复合量纲（如 千卡/小时、克/日）的 span；不处理纯数字
    数据值 span，避免误伤数据卡片单位 chip。为确定性后置修复，不触发 LLM 重写。
    """
    chart_starts = [m.start() for m in _CHART_DIV_RE.finditer(html)]
    if not chart_starts:
        return html

    stripped = 0

    def _is_chart_header_unit(match: re.Match[str]) -> bool:
        nonlocal stripped
        span_end = match.end()
        if not any(span_end <= cs <= span_end + _CHART_HEADER_WINDOW for cs in chart_starts):
            return False
        text = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", match.group("text")))
        if not text:
            return False
        if _CHART_UNIT_LABEL_RE.match(text) or _CHART_UNIT_TOKEN_RE.search(text):
            stripped += 1
            return True
        return False

    new_html = _CHART_HEADER_SPAN_RE.sub(
        lambda m: "" if _is_chart_header_unit(m) else m.group(0),
        html,
    )
    if stripped:
        logger.info(
            "[P8.1] 剥离图表卡片头部重复单位 span %d 处（单位改由 yAxis.name 承载）",
            stripped,
        )
    return new_html


# 检测 CSS Grid 布局使用（html-to-pptx 不支持 Grid）
_GRID_USAGE_RE = re.compile(r'\bgrid\s+grid-cols-\S+', re.IGNORECASE)


def _has_grid_layout(html: str) -> bool:
    """检测是否使用了 CSS Grid 布局（html-to-pptx 转换器不支持 Grid）。"""
    return bool(_GRID_USAGE_RE.search(html))


# 检测核心内容容器上的 overflow-hidden（不应裁切核心内容）
_OVERFLOW_HIDDEN_RE = re.compile(
    r'<(?:div|section|main|article|aside|header|footer)[^>]*\boverflow-hidden\b[^>]*>',
    re.IGNORECASE,
)


def _has_overflow_hidden_on_content(html: str) -> bool:
    """检测核心内容容器（div/section/main 等）上是否使用了 overflow-hidden。

    overflow-hidden 仅允许用于 .ppt-slide 画布边界，不应用于核心内容容器。
    """
    # 排除 .ppt-slide 容器本身（画布边界 overflow-hidden 是允许的）
    matches = _OVERFLOW_HIDDEN_RE.findall(html)
    for m in matches:
        if 'ppt-slide' not in m.lower():
            return True
    return False


# 检测字号一致性：提取所有 text-[Npx] 值
_FONT_SIZE_RE = re.compile(r'text-\[(\d+)px\]')


def _check_font_size_consistency(html: str) -> bool:
    """检测同页字号是否一致。返回 True 表示不一致。

    规则：同级别的卡片/模块应使用相同字号。
    如果同页出现 >3 种不同正文字号，判定为不一致。
    """
    sizes = [int(m) for m in _FONT_SIZE_RE.findall(html)]
    if not sizes:
        return False
    # 过滤出正文字号范围（14-24px），标题字号（37px+）不参与一致性检查
    body_sizes = [s for s in sizes if 14 <= s <= 24]
    if len(set(body_sizes)) > 3:
        return True
    return False


_LIST_BLOCK_RE = re.compile(
    r"<(?P<tag>ul|ol)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_CLASS_ATTR_RE = re.compile(
    r"\bclass\s*=\s*(?P<quote>[\"'])(?P<classes>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_LIST_ITEM_RE = re.compile(r"<li\b[^>]*>(.*?)</li\s*>", re.IGNORECASE | re.DOTALL)
_NON_TEXT_VISUAL_RE = re.compile(
    r"<(?:img|picture|table|svg|canvas)\b|echarts(?:-static-svg|\.init)",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)


def _has_sparse_flex_text_list(html: str) -> bool:
    """检测高置信度的稀疏 flex-1 文字列表，避免把正常长列表误判为空白风险。"""
    for match in _LIST_BLOCK_RE.finditer(html):
        class_match = _CLASS_ATTR_RE.search(match.group("attrs"))
        if not class_match:
            continue
        classes = set(class_match.group("classes").split())
        if "flex-1" not in classes:
            continue

        body = match.group("body")
        if _NON_TEXT_VISUAL_RE.search(body):
            continue
        items = _LIST_ITEM_RE.findall(body)
        if not 1 <= len(items) <= 5:
            continue

        visible_items = [
            re.sub(r"\s+", "", _HTML_TAG_RE.sub(" ", item)).replace("&nbsp;", "")
            for item in items
        ]
        if (
            all(visible_items)
            and max(map(len, visible_items)) <= 80
            and sum(map(len, visible_items)) <= 240
        ):
            return True
    return False


@dataclass
class _ConstrainedCardState:
    """高度受限 Flex 卡片的解析状态。"""

    tag: str
    depth: int
    has_fixed_table: bool = False


_HTML_TAG_TOKEN_RE = re.compile(
    r"<(?P<closing>/)?(?P<tag>[a-zA-Z][\w:-]*)(?P<attrs>[^>]*)>",
    re.DOTALL,
)
_HTML_CLASS_RE = re.compile(
    r"\bclass\s*=\s*(?P<quote>[\"'])(?P<classes>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_HTML_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def _classes_from_tag_attrs(attrs: str) -> set[str]:
    match = _HTML_CLASS_RE.search(attrs)
    return set(match.group("classes").split()) if match else set()


def _is_constrained_flex_card(classes: set[str]) -> bool:
    if "flex-col" not in classes or "min-h-0" not in classes:
        return False
    return "flex-1" in classes or any(
        cls.startswith("flex-[") and cls.endswith("]")
        for cls in classes
    )


def _has_risky_trailing_content_in_constrained_card(html: str) -> bool:
    """检测本次坏例对应的高置信度卡片越界结构。"""
    tag_stack: list[str] = []
    card_stack: list[_ConstrainedCardState] = []

    for match in _HTML_TAG_TOKEN_RE.finditer(html):
        tag = match.group("tag").lower()
        if match.group("closing"):
            current_depth = len(tag_stack) - 1
            if (
                card_stack
                and card_stack[-1].tag == tag
                and card_stack[-1].depth == current_depth
            ):
                card_stack.pop()
            if tag_stack:
                tag_stack.pop()
            continue

        attrs = match.group("attrs") or ""
        classes = _classes_from_tag_attrs(attrs)
        if card_stack:
            card = card_stack[-1]
            if tag == "table" and "flex-shrink-0" in classes:
                card.has_fixed_table = True
            elif (
                card.has_fixed_table
                and tag in {"div", "p", "span", "aside"}
                and "flex-shrink-0" in classes
            ):
                return True
            if "mt-auto" in classes and "flex-shrink-0" in classes:
                return True

        is_void = tag in _HTML_VOID_TAGS or attrs.rstrip().endswith("/")
        if is_void:
            continue
        tag_stack.append(tag)
        if _is_constrained_flex_card(classes):
            card_stack.append(
                _ConstrainedCardState(tag=tag, depth=len(tag_stack) - 1)
            )
    return False


_HTML_STYLE_RE = re.compile(
    r"\bstyle\s*=\s*(?P<quote>[\"'])(?P<style>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_DECORATION_ROLE_RE = re.compile(
    r"\bdata-pptx-role\s*=\s*([\"'])decoration\1",
    re.IGNORECASE,
)
_DECORATION_CLASS_RE = re.compile(
    r"^(?:bg[-_])?(?:deco|decoration)(?:[-_].*)?$",
    re.IGNORECASE,
)
_NEGATIVE_EDGE_STYLE_RE = re.compile(
    r"(?:^|[;{])\s*(?:top|right|bottom|left)\s*:\s*"
    r"-\s*(?:\d+(?:\.\d+)?|\.\d+)\s*(?:px|%|rem|em)\b",
    re.IGNORECASE,
)
_NEGATIVE_EDGE_CLASS_RE = re.compile(
    r"^(?:top|right|bottom|left)-\[-(?:\d+(?:\.\d+)?|\.\d+)(?:px|%|rem|em)\]$",
    re.IGNORECASE,
)
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(?P<css>.*?)</style>", re.IGNORECASE | re.DOTALL)
_CSS_RULE_RE = re.compile(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", re.DOTALL)


def _is_explicit_decoration(attrs: str, classes: set[str]) -> bool:
    """仅识别明确标注的背景装饰，避免误判普通绝对定位内容。"""
    return bool(_DECORATION_ROLE_RE.search(attrs)) or any(
        _DECORATION_CLASS_RE.fullmatch(cls)
        for cls in classes
    )


def _has_negative_edge_in_css(html: str, classes: set[str]) -> bool:
    """检查装饰类对应 CSS 规则是否将图形部分移出画布。"""
    if not classes:
        return False
    for style_match in _STYLE_BLOCK_RE.finditer(html):
        css = style_match.group("css")
        for rule_match in _CSS_RULE_RE.finditer(css):
            selectors = rule_match.group("selectors")
            if not any(
                re.search(
                    rf"(?<![\w-])\.{re.escape(cls)}(?![\w-])",
                    selectors,
                )
                for cls in classes
            ):
                continue
            if _NEGATIVE_EDGE_STYLE_RE.search("{" + rule_match.group("body")):
                return True
    return False


def _has_off_canvas_decoration(html: str) -> bool:
    """检测内容页中依赖负坐标和画布裁切的明确背景装饰。"""
    for match in _HTML_TAG_TOKEN_RE.finditer(html):
        if match.group("closing"):
            continue
        attrs = match.group("attrs") or ""
        classes = _classes_from_tag_attrs(attrs)
        if not _is_explicit_decoration(attrs, classes):
            continue
        if any(_NEGATIVE_EDGE_CLASS_RE.fullmatch(cls) for cls in classes):
            return True
        style_match = _HTML_STYLE_RE.search(attrs)
        if style_match and _NEGATIVE_EDGE_STYLE_RE.search("{" + style_match.group("style")):
            return True
        decoration_classes = {
            cls for cls in classes if _DECORATION_CLASS_RE.fullmatch(cls)
        }
        if _has_negative_edge_in_css(html, decoration_classes):
            return True
    return False


# --- 全页栅格化装饰遮罩（“透明罩”）修复 ---
# html-to-pptx 转换器对 `inset:0` + 渐变背景的元素会栅格化为 PNG 图片
# （见 css-whitelist.md `inset` 条目“仅用于渐变背景检测”）。而
# repeating-linear-gradient / mix-blend-mode / radial-gradient / conic-gradient
# 不在 css-whitelist 支持范围内，配合全页 inset:0 会生成覆盖整页的不可编辑图片，
# 落在内容层之上即挡住下层元素的编辑（“透明罩”）。
_UNSUPPORTED_RASTER_BG_RE = re.compile(
    r"repeating-linear-gradient|mix-blend-mode|radial-gradient|conic-gradient",
    re.IGNORECASE,
)
_EMPTY_DIV_RE = re.compile(r"<div\b[^>]*>\s*</div\s*>", re.IGNORECASE)
_OPEN_DIV_TAG_RE = re.compile(r"<div\b[^>]*>", re.IGNORECASE)


def _is_fullpage_overlay_style(style_text: str) -> bool:
    """判断一段 CSS 文本是否表示全页覆盖定位。

    `inset:0` 是全页覆盖的最简表达；`position:absolute` + 100% 宽高为等价写法。
    """
    if re.search(r"inset\s*:\s*0\b", style_text, re.IGNORECASE):
        return True
    return (
        bool(re.search(r"position\s*:\s*absolute", style_text, re.IGNORECASE))
        and bool(re.search(r"width\s*:\s*100%", style_text, re.IGNORECASE))
        and bool(re.search(r"height\s*:\s*100%", style_text, re.IGNORECASE))
    )


def _strip_unsupported_fullpage_overlays(html: str) -> str:
    """移除使用 css-whitelist 不支持栅格化背景的全页空装饰遮罩。

    只删除同时满足以下三条件的 `<div>`：
    1. 全页覆盖（`inset:0` 或绝对定位 + 100% 宽高）；
    2. 背景使用 `repeating-linear-gradient`/`mix-blend-mode`/`radial-gradient`/
       `conic-gradient`（不在 css-whitelist 支持范围，会被栅格化为图片）；
    3. 无内容子节点（空 div，如 `<div class="scanlines"></div>`）。
    不影响独立小尺寸 scanline 条（非全页）、图表、卡片或内容容器；CSS 规则保留不动
    （移除元素后即为无害死规则）。
    """
    if not html or not _UNSUPPORTED_RASTER_BG_RE.search(html):
        return html

    overlay_classes: set[str] = set()
    for style_match in _STYLE_BLOCK_RE.finditer(html):
        css = style_match.group("css")
        for rule_match in _CSS_RULE_RE.finditer(css):
            body = rule_match.group("body")
            if _UNSUPPORTED_RASTER_BG_RE.search(body) and _is_fullpage_overlay_style(
                body
            ):
                overlay_classes.update(
                    re.findall(r"\.([A-Za-z_][\w-]*)", rule_match.group("selectors"))
                )

    def _is_overlay_open_tag(open_tag: str) -> bool:
        if overlay_classes and (
            _classes_from_tag_attrs(open_tag) & overlay_classes
        ):
            return True
        style_match = _HTML_STYLE_RE.search(open_tag)
        if style_match:
            style_val = style_match.group("style")
            if _UNSUPPORTED_RASTER_BG_RE.search(
                style_val
            ) and _is_fullpage_overlay_style(style_val):
                return True
        return False

    removed = 0

    def _maybe_strip(match: re.Match) -> str:
        nonlocal removed
        open_tag = _OPEN_DIV_TAG_RE.match(match.group(0))
        if open_tag and _is_overlay_open_tag(open_tag.group(0)):
            removed += 1
            return ""
        return match.group(0)

    result = _EMPTY_DIV_RE.sub(_maybe_strip, html)
    if removed:
        logger.info("[P8.1] 移除全页栅格化装饰遮罩 %d 个（透明罩修复）", removed)
    return result


_JS_DELIMITER_PAIRS = {"(": ")", "[": "]", "{": "}"}
_SET_OPTION_RE = re.compile(r"\.setOption\s*\(", re.IGNORECASE)
_SERIES_ARRAY_RE = re.compile(r"\bseries\s*:\s*\[", re.IGNORECASE)
_LABEL_OBJECT_RE = re.compile(r"\blabel\s*:\s*\{", re.IGNORECASE)
_SERIES_TYPE_RE = re.compile(r"\btype\s*:\s*([\"'])(?P<type>bar|line)\1", re.IGNORECASE)
_LABEL_SHOW_RE = re.compile(r"\bshow\s*:\s*true\b", re.IGNORECASE)
_LABEL_POSITION_RE = re.compile(
    r"\bposition\s*:\s*([\"'])(?P<position>[^\"']+)\1",
    re.IGNORECASE,
)
_LABEL_OFFSET_RE = re.compile(r"\boffset\s*:\s*(?P<value>\[[^\]]*\])", re.IGNORECASE)
_LABEL_DISTANCE_RE = re.compile(
    r"\bdistance\s*:\s*(?P<value>-?(?:\d+(?:\.\d+)?|\.\d+))",
    re.IGNORECASE,
)
_LABEL_FONT_SIZE_RE = re.compile(
    r"\bfontSize\s*:\s*(?P<value>-?(?:\d+(?:\.\d+)?|\.\d+))",
    re.IGNORECASE,
)
_LABEL_LINE_HEIGHT_RE = re.compile(
    r"\blineHeight\s*:\s*(?P<value>-?(?:\d+(?:\.\d+)?|\.\d+))",
    re.IGNORECASE,
)
_NON_PRIMARY_Y_AXIS_RE = re.compile(r"\byAxisIndex\s*:\s*[1-9]\d*\b", re.IGNORECASE)
_NUMBER_LITERAL = r"-?(?:\d+(?:\.\d+)?|\.\d+)"
_Y_AXIS_INDEX_RE = re.compile(r"\byAxisIndex\s*:\s*(?P<value>\d+)\b", re.IGNORECASE)
_CHART_LABEL_REFERENCE_PLOT_HEIGHT_PX = 300.0
_CHART_LABEL_MIN_GAP_PX = 12.0
_CHART_TOP_LANE_MIN_GAP_PX = 18.0


@dataclass(frozen=True)
class _ChartLabelPlacement:
    """ECharts 数据标签的垂直几何参数。"""

    position: str
    offset_x: float
    offset_y: float
    distance: float
    font_size: float
    line_height: float


def _find_matching_js_delimiter(text: str, start: int) -> int:
    """返回 JS 对象/数组/调用的闭合位置；忽略字符串和注释中的括号。"""
    if start >= len(text) or text[start] not in _JS_DELIMITER_PAIRS:
        return -1
    stack = [text[start]]
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = start + 1
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            index += 1
            continue
        if char in _JS_DELIMITER_PAIRS:
            stack.append(char)
        elif char in _JS_DELIMITER_PAIRS.values():
            if not stack or _JS_DELIMITER_PAIRS[stack[-1]] != char:
                return -1
            stack.pop()
            if not stack:
                return index
        index += 1
    return -1


def _extract_set_option_blocks(html: str) -> list[str]:
    blocks: list[str] = []
    for match in _SET_OPTION_RE.finditer(html):
        open_index = match.end() - 1
        close_index = _find_matching_js_delimiter(html, open_index)
        if close_index != -1:
            blocks.append(html[open_index + 1:close_index])
    return blocks


def _extract_named_js_array(text: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    array_start = match.end() - 1
    array_end = _find_matching_js_delimiter(text, array_start)
    if array_end == -1:
        return ""
    return text[array_start:array_end + 1]


def _extract_named_js_object(text: str, name: str) -> str:
    pattern = re.compile(rf"\b{re.escape(name)}\s*:\s*\{{", re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        return ""
    object_start = match.end() - 1
    object_end = _find_matching_js_delimiter(text, object_start)
    if object_end == -1:
        return ""
    return text[object_start:object_end + 1]


def _extract_string_property(text: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*:\s*([\"'])(?P<value>.*?)\1",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group("value") if match else ""


def _extract_top_level_js_objects(array_text: str) -> list[str]:
    if not array_text.startswith("["):
        return []
    objects: list[str] = []
    index = 1
    array_end = len(array_text) - 1
    while index < array_end:
        if array_text[index] != "{":
            index += 1
            continue
        object_end = _find_matching_js_delimiter(array_text, index)
        if object_end == -1 or object_end > array_end:
            return []
        objects.append(array_text[index:object_end + 1])
        index = object_end + 1
    return objects


def _extract_series_objects(option_block: str) -> list[str]:
    return _extract_top_level_js_objects(
        _extract_named_js_array(option_block, _SERIES_ARRAY_RE)
    )


def _extract_numeric_property(text: str, name: str) -> float | None:
    match = re.search(
        rf"\b{re.escape(name)}\s*:\s*(?P<value>{_NUMBER_LITERAL})\b",
        text,
        re.IGNORECASE,
    )
    return float(match.group("value")) if match else None


def _extract_series_data(series: str) -> list[float | None]:
    data_pattern = re.compile(r"\bdata\s*:\s*\[", re.IGNORECASE)
    array_text = _extract_named_js_array(series, data_pattern)
    if not array_text:
        return []
    values: list[float | None] = []
    index = 1
    array_end = len(array_text) - 1
    while index < array_end:
        while index < array_end and (array_text[index].isspace() or array_text[index] == ","):
            index += 1
        if index >= array_end:
            break
        if array_text[index] == "{":
            object_end = _find_matching_js_delimiter(array_text, index)
            if object_end == -1:
                return []
            value = _extract_numeric_property(array_text[index:object_end + 1], "value")
            values.append(value)
            index = object_end + 1
            continue
        item_end = array_text.find(",", index, array_end)
        if item_end == -1:
            item_end = array_end
        token = array_text[index:item_end].strip()
        if token.lower() == "null":
            values.append(None)
        elif re.fullmatch(_NUMBER_LITERAL, token):
            values.append(float(token))
        else:
            return []
        index = item_end + 1
    return values


def _extract_y_axis_bounds(option_block: str) -> list[tuple[float, float]]:
    y_axis_pattern = re.compile(r"\byAxis\s*:\s*\[", re.IGNORECASE)
    axis_objects = _extract_top_level_js_objects(
        _extract_named_js_array(option_block, y_axis_pattern)
    )
    bounds: list[tuple[float, float]] = []
    for axis in axis_objects:
        maximum = _extract_numeric_property(axis, "max")
        minimum = _extract_numeric_property(axis, "min")
        if maximum is None or re.search(r"\bscale\s*:\s*true\b", axis, re.IGNORECASE):
            return []
        minimum = 0.0 if minimum is None else minimum
        if maximum <= minimum:
            return []
        bounds.append((minimum, maximum))
    return bounds


def _label_placement_signature(series: str) -> _ChartLabelPlacement | None:
    """提取启用标签的定位参数，用于估算跨系列文字框。"""
    for match in _LABEL_OBJECT_RE.finditer(series):
        object_start = match.end() - 1
        object_end = _find_matching_js_delimiter(series, object_start)
        if object_end == -1:
            continue
        label = series[object_start:object_end + 1]
        if not _LABEL_SHOW_RE.search(label):
            continue
        position_match = _LABEL_POSITION_RE.search(label)
        if not position_match:
            continue
        offset_match = _LABEL_OFFSET_RE.search(label)
        distance_match = _LABEL_DISTANCE_RE.search(label)
        font_size_match = _LABEL_FONT_SIZE_RE.search(label)
        line_height_match = _LABEL_LINE_HEIGHT_RE.search(label)
        offset_values = (
            [float(value) for value in re.findall(_NUMBER_LITERAL, offset_match.group("value"))]
            if offset_match
            else []
        )
        font_size = float(font_size_match.group("value")) if font_size_match else 12.0
        line_height = (
            float(line_height_match.group("value"))
            if line_height_match
            else font_size * 1.2
        )
        return _ChartLabelPlacement(
            position=position_match.group("position").lower(),
            offset_x=offset_values[0] if offset_values else 0.0,
            offset_y=offset_values[1] if len(offset_values) > 1 else 0.0,
            distance=float(distance_match.group("value")) if distance_match else 5.0,
            font_size=font_size,
            line_height=line_height,
        )
    return None


def _label_vertical_interval(
    anchor_height: float,
    placement: _ChartLabelPlacement,
) -> tuple[float, float] | None:
    """按300px参考绘图区估算标签文字框的归一化垂直区间。"""
    plot_height = _CHART_LABEL_REFERENCE_PLOT_HEIGHT_PX
    anchor = anchor_height - placement.offset_y / plot_height
    distance = max(0.0, placement.distance) / plot_height
    label_height = max(placement.font_size, placement.line_height) / plot_height
    if placement.position == "top":
        return anchor + distance, anchor + distance + label_height
    if placement.position in {"insidetop", "bottom"}:
        return anchor - distance - label_height, anchor - distance
    if placement.position in {"left", "right"}:
        half_height = label_height / 2
        return anchor - half_height, anchor + half_height
    return None


def _vertical_interval_gap(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    if first[1] < second[0]:
        return second[0] - first[1]
    if second[1] < first[0]:
        return first[0] - second[1]
    return 0.0


def _has_dual_axis_combo_label_collision_risk(html: str) -> bool:
    """检测同一双轴柱线图中安全距离不足的数据标签。"""
    for option_block in _extract_set_option_blocks(html):
        series_objects = _extract_series_objects(option_block)
        if not series_objects or not any(
            _NON_PRIMARY_Y_AXIS_RE.search(series)
            for series in series_objects
        ):
            continue
        axis_bounds = _extract_y_axis_bounds(option_block)
        if len(axis_bounds) < 2:
            continue
        entries: dict[
            str,
            list[tuple[_ChartLabelPlacement, int, list[float | None]]],
        ] = {
            "bar": [],
            "line": [],
        }
        for series in series_objects:
            type_match = _SERIES_TYPE_RE.search(series)
            if not type_match:
                continue
            placement = _label_placement_signature(series)
            data = _extract_series_data(series)
            if not placement or not data:
                continue
            axis_match = _Y_AXIS_INDEX_RE.search(series)
            axis_index = int(axis_match.group("value")) if axis_match else 0
            if axis_index >= len(axis_bounds):
                continue
            entries[type_match.group("type").lower()].append(
                (placement, axis_index, data)
            )
        for bar_placement, bar_axis, bar_data in entries["bar"]:
            for line_placement, line_axis, line_data in entries["line"]:
                bar_min, bar_max = axis_bounds[bar_axis]
                line_min, line_max = axis_bounds[line_axis]
                for bar_value, line_value in zip(bar_data, line_data):
                    if bar_value is None or line_value is None:
                        continue
                    bar_height = (bar_value - bar_min) / (bar_max - bar_min)
                    line_height = (line_value - line_min) / (line_max - line_min)
                    bar_interval = _label_vertical_interval(bar_height, bar_placement)
                    line_interval = _label_vertical_interval(line_height, line_placement)
                    if not bar_interval or not line_interval:
                        continue
                    gap_px = _vertical_interval_gap(bar_interval, line_interval) * (
                        _CHART_LABEL_REFERENCE_PLOT_HEIGHT_PX
                    )
                    if gap_px < _CHART_LABEL_MIN_GAP_PX:
                        return True
    return False


def _has_chart_top_lane_collision_risk(html: str) -> bool:
    """检测顶部横向图例与双Y轴名称之间的垂直安全距离。"""
    y_axis_pattern = re.compile(r"\byAxis\s*:\s*\[", re.IGNORECASE)
    for option_block in _extract_set_option_blocks(html):
        axis_objects = _extract_top_level_js_objects(
            _extract_named_js_array(option_block, y_axis_pattern)
        )
        named_axes = [axis for axis in axis_objects if _extract_string_property(axis, "name")]
        if len(axis_objects) < 2 or not named_axes:
            continue
        legend = _extract_named_js_object(option_block, "legend")
        grid = _extract_named_js_object(option_block, "grid")
        if not legend or not grid:
            continue
        if _extract_string_property(legend, "orient").lower() == "vertical":
            continue
        legend_top = _extract_numeric_property(legend, "top")
        grid_top = _extract_numeric_property(grid, "top")
        if legend_top is None or grid_top is None:
            continue
        legend_text_style = _extract_named_js_object(legend, "textStyle")
        legend_font_size = _extract_numeric_property(legend_text_style, "fontSize") or 12.0
        axis_font_sizes = []
        for axis in named_axes:
            name_style = _extract_named_js_object(axis, "nameTextStyle")
            axis_font_sizes.append(
                _extract_numeric_property(name_style, "fontSize") or 12.0
            )
        axis_name_font_size = max(axis_font_sizes)
        available_gap = grid_top - axis_name_font_size - (
            legend_top + legend_font_size
        )
        if available_gap < _CHART_TOP_LANE_MIN_GAP_PX:
            return True
    return False


def _post_check_data_viz(html: str, failed_items: list[str], search_mode: str) -> list[str]:
    """程序化后置校验：对 LLM 判定的'缺数据可视化'做二次确认，移除误判。"""
    if "缺数据可视化" not in failed_items:
        return failed_items
    has_echarts = "echarts" in html.lower()
    # 改进卡片计数：只匹配 class 属性中的 card，不匹配文本内容
    card_count = len(re.findall(r'class="[^"]*\bcard\b[^"]*"', html, re.IGNORECASE))
    threshold = 2 if search_mode == "no_search" else 3
    if has_echarts or card_count >= threshold:
        failed_items = [x for x in failed_items if x != "缺数据可视化"]
    return failed_items


def _post_check_layout_issues(html: str, failed_items: list[str]) -> list[str]:
    """程序化后置校验：检测 Grid、裁切、字号、边界和碰撞风险等布局问题。

    leading-loose（line-height:2）使文字高度翻倍，在 PPTX 导出时极易导致内容超出卡片边界。
    高度受限卡片中的固定表格后追加不可收缩标签，也会把标签挤出父卡片。
    PPTX 不尊重 overflow-hidden，超出边界的内容会直接溢出。
    """
    # 检测 CSS Grid 使用
    if _has_grid_layout(html) and "使用了不支持的Grid布局" not in failed_items:
        failed_items.append("使用了不支持的Grid布局")
    # 检测核心内容容器上的 overflow-hidden
    if _has_overflow_hidden_on_content(html) and "核心内容被overflow-hidden裁切" not in failed_items:
        failed_items.append("核心内容被overflow-hidden裁切")
    # 检测字号不一致
    if _check_font_size_consistency(html) and "字号不一致" not in failed_items:
        failed_items.append("字号不一致")
    # 只检测高置信度场景：1-5 个短条目的纯文字列表自身使用 flex-1 拉满高度。
    # 该结果进入现有重试/low_density 兜底，不新增硬阻断。
    if _has_sparse_flex_text_list(html) and "局部空白失衡" not in failed_items:
        failed_items.append("局部空白失衡")
    # 检测溢出风险：行高翻倍，或固定表格后追加不可收缩尾部内容。
    if "内容溢出" not in failed_items and (
        "leading-loose" in html
        or _has_risky_trailing_content_in_constrained_card(html)
    ):
        failed_items.append("内容溢出")
    if _has_off_canvas_decoration(html) and "装饰元素越界" not in failed_items:
        failed_items.append("装饰元素越界")
    if (
        _has_dual_axis_combo_label_collision_risk(html)
        and "图表数据标签重叠风险" not in failed_items
    ):
        failed_items.append("图表数据标签重叠风险")
    if (
        _has_chart_top_lane_collision_risk(html)
        and "图例与轴标题重叠" not in failed_items
    ):
        failed_items.append("图例与轴标题重叠")
    return failed_items


_SEARCH_NEEDED_ITEMS = frozenset({"缺数据可视化", "缺案例", "缺数据来源"})

_SEARCH_QUERY_TEMPLATES: dict[str, list[str]] = {
    "缺数据可视化": [
        "{topic} 市场规模 数据",
        "{topic} 增长率 百分比 统计",
        "{topic} 渗透率 市场份额 报告",
    ],
    "缺案例": [
        "{topic} 应用案例 实践",
        "{topic} 成功案例 最佳实践",
    ],
    "缺数据来源": [
        "{topic} 行业报告",
        "{topic} 研究 数据 来源",
    ],
}


_REWRITE_ACTIONS = {
    "缺数据可视化": (
        "在页面底部（footer 之前）插入一个 ECharts 图表，按以下规则选择图表类型："
        "时间序列数据用折线图，占比/构成数据用饼图，对比/排名数据用柱状图。"
        "直接使用页面中已有的数字作为数据点，不要修改现有卡片和布局结构。"
        "如果页面已存在图表容器（如 <div id=\"xxxChart\">）但缺少初始化脚本，"
        "必须在该容器之后紧邻 </body> 补充完整的"
        " echarts.init(document.getElementById('xxx'), null, {renderer:'svg'}).setOption({...}) 初始化代码"
    ),
    "核心要点不足": "将段落拆分为 6-10 个列表项或卡片，每条 1-2 行加图标",
    "缺装饰图标": "为每个核心要点/卡片添加相关 FontAwesome 图标（class 含 fa-）",
    "空白率过高": (
        "优先重排并放大已有图表、表格、图片或数据卡片的有效展示区域，"
        "其次添加包含 1-2 句真实结论的总结框或引用块；"
        "禁止用背景装饰、空容器或 spacer 冒充内容占用"
    ),
    "局部空白失衡": (
        "优先重排已有语义内容（第一选择）：同时移除短文字卡片组及卡片本身不必要的 flex-1，"
        "改为 flex-shrink-0 按内容自适应高度；1-5 个短条目可改为 2×2/分组布局，"
        "或用明确 Flex 权重、justify-between 合理分布真实条目；"
        "同栏已有图表、图片或表格时，将剩余高度分配给这些主区域，或减少等高行数、缩小该区域占比；"
        "禁止插入空 spacer、空容器或纯装饰元素冒充内容占用\n"
        "若必须补充内容（第二选择）：在卡片内追加 1-2 行精简描述即可，"
        "但禁止使用 leading-loose（line-height:2 会翻倍高度导致溢出），"
        "禁止添加 mt-auto 底部子元素（色块标签/badge 行等，会增加总高度），"
        "禁止增加已有文字的行高；"
        "注意：PPTX 导出时不尊重 overflow-hidden，卡片内容超出边界会直接溢出"
    ),
    "缺数据来源": "在页脚标注'数据来源：XXX'（机构名或资料名）",
    "大段文字": "拆分为多个列表项/小节，添加小标题",
    "视觉层级混乱": "调整字号梯度，建立明确的标题→副标题→正文→注释层级",
    "布局错误": "main 改为 `flex gap-3`，恰好 2 个 `<section>` 子元素；header/footer 放在 main 外部的 content-safe 内",
    "内容被隐藏": "移除 line-clamp、text-overflow:ellipsis、overflow:auto/scroll、max-height 限制等隐藏手段，确保核心内容完整可见",
    "核心内容缺失": "检查标题、正文、图表标签、数据来源和数据卡片是否全部完整显示，补充缺失的内容元素",
    "使用了不支持的Grid布局": "将所有 `grid grid-cols-*` 改为 Flexbox 布局（`flex` + `flex-[N]` 比例分配），因为 html-to-pptx 转换器不支持 CSS Grid",
    "核心内容被overflow-hidden裁切": (
        "移除核心内容容器（div/section/main 等）上的 `overflow-hidden` 类，"
        "仅保留 `.ppt-slide` 画布边界上的 overflow-hidden"
    ),
    "字号不一致": "统一同级别卡片/模块的字号，使用风格文件定义的字号值，确保同级元素字号一致",
    "图例与轴标题重叠": (
        "同时按图例项数量和文字长度处理：任一中文标签超过 6 个字或总长度超过 12 个字时，"
        "优先在不改变含义的前提下缩短标签，或改为 `legend:{orient:'vertical'}`；"
        "若仍横排，将 `itemGap` 调到至少 24；"
        "增大 `grid.top` 或移动图例分开两条通道，"
        "确保图例文字框底边与轴名称文字框顶边至少相隔 18px；不得缩小字号掩盖"
    ),
    "图表数据标签重叠风险": (
        "仅调整同一双轴柱线图的数据标签定位，不改变数据、坐标轴或图表尺寸："
        "按各自 yAxis min/max 比较同一分类的数据点视觉高度，并结合 fontSize、lineHeight、"
        "position、distance、offset 确保两个标签文字框上下/左右至少相隔 12px；"
        "即使柱形为 `insideTop`、折线为 `top` 也必须验算；"
        "优先只移动碰撞系列或碰撞数据点，必要时仅隐藏次要点标签；"
        "保留原字号，`labelLayout` 仅作为补充"
    ),
    "装饰元素越界": (
        "只处理明确的背景装饰节点，不改标题、图表、卡片和正文："
        "若风格文件没有定义该角落装饰，直接删除；若风格明确要求保留，"
        "将其重绘为完整位于 1280×720 画布内、相应边角坐标为 0 的角形，"
        "禁止负 top/right/bottom/left、负 margin 或依赖 overflow-hidden 裁切，"
        "也不得引入风格文件禁止的渐变或随机形状"
    ),
    "内容溢出": (
        "移除 `leading-loose`（改为 `leading-snug` 或 `leading-normal`），"
        "禁止仅删除 `mt-auto` 后把同一标签留在卡片尾部；"
        "若受限卡片内已有表格/多行正文，将尾部标签移入所属卡片的标题行"
        "（使用 `justify-between`）或合并为表格摘要行，确保标签四边都在父卡片边框内；"
        "随后按需减小卡片内部 padding/gap 或调整同栏卡片 Flex 比例，"
        "精简每张卡片的文字行数使其不超过容器可容纳行数；"
        "若内容确实需要更多空间，将 `flex-col` 改为更少的子元素或改为 `flex-shrink-0` 自适应高度；"
        "不得用绝对定位、负 margin 或 `overflow-hidden` 掩盖越界"
    ),
    # 页面生成校验失败 reason → 定向重试补救动作
    "invalid_chart_height_chain": (
        "修复图表容器高度链：包含 chart div 的最近 flex-col 祖先容器"
        "必须带 flex-1 min-h-0（或 flex-[N] min-h-0），"
        "标准写法 div.flex-1.min-h-0.flex.flex-col > div#chart-1.w-full.h-full"
    ),
    "chart_mount_id_mismatch": (
        "图表容器 id 必须与脚本中 document.getElementById 引用完全一致；"
        "禁止在修复或再填时改写图表 div 的 id 或 getElementById 参数字符串"
    ),
    "invalid_html": (
        "输出完整合法 HTML 文档，须含闭合 </body></html>，"
        "且仅含 1 个 .ppt-slide 容器，禁止多页拼进同一文件、截断或夹杂解释文字"
    ),
    "invalid_dom": "修复 DOM：消除畸形片段，确保 <main> 位于 .ppt-slide 内",
    "unfilled_placeholders": "填完所有 {{...}} / PAGE_* 占位符，禁止残留未替换标记",
    "seed_not_modified": "必须基于预铺模板填入本页标题、正文与页脚，禁止原样返回 seed",
    "main_tag_changed": "禁止改动 <main> 开标签（class/属性）；仅替换 main 内部 PAGE_CONTENT",
    "content_template_chrome_changed": (
        "禁止改动模板 chrome（<head>、header 结构、footer 结构）；"
        "仅替换 PAGE_TITLE / PAGE_CONTENT / PAGE_FOOTER 三处占位内容"
    ),
    "head_chrome_changed": "禁止改动 <head> 块（含 title 文字、script/style 引用）；仅填 body 内占位符",
    "header_chrome_changed": "禁止改动 header 结构（content-safe 到 main 之间）；仅替换 PAGE_TITLE",
    "footer_chrome_changed": "禁止改动 footer 结构（main 之后的 flex-shrink-0 div）；仅替换 PAGE_FOOTER",
    "empty_page_content": "在 main 内填入本页正文内容，禁止空的 PAGE_CONTENT",
    "page_content_unfilled": "将 {{PAGE_CONTENT}} 替换为本页实际 HTML 内容",
    "title_invalid": "将 PAGE_TITLE 替换为大纲中的真实标题，禁止占位敷衍文案",
    "footer_missing": "保留 footer 结构并填入 PAGE_FOOTER",
    "footer_invalid": "将 PAGE_FOOTER 替换为有效页脚文案，禁止占位敷衍文案",
    "llm_failed": "重新生成完整页面 HTML，确保输出可解析",
}

# 图表候选页 chrome 重试：与对齐-1 一致，CHART_SCAFFOLD 不在 Chrome 锁内。
_CHART_CANDIDATE_CHROME_REWRITE_REASONS = frozenset({
    "content_template_chrome_changed",
    "head_chrome_changed",
    "header_chrome_changed",
    "footer_chrome_changed",
})
_CHART_CANDIDATE_REWRITE_ACTIONS: dict[str, str] = {
    "content_template_chrome_changed": (
        "禁止改动模板 chrome（<head>、header 结构、footer 骨架）；"
        "可编辑区：PAGE_TITLE / PAGE_CONTENT / PAGE_FOOTER 三槽，"
        "以及 </body> 前 CHART_SCAFFOLD（删定界符 + 填 option）；"
        "CHART_SCAFFOLD 不在 Chrome 锁内"
    ),
    "head_chrome_changed": (
        "禁止改动 <head> 块（script/style/link 引用）；"
        "可填 body 内三槽与 CHART_SCAFFOLD（图表候选页）；"
        "<title> 文字属于 PAGE_TITLE 可编辑区"
    ),
    "header_chrome_changed": (
        "禁止改动 header 结构（content-safe 到 main 之间）；"
        "仅替换 PAGE_TITLE；图表 scaffold 在 footer 之后单独可编辑"
    ),
    "footer_chrome_changed": (
        "禁止改动 footer 结构（main 之后的 flex-shrink-0 div）；"
        "仅替换 PAGE_FOOTER 文字；CHART_SCAFFOLD 在其后、不在 footer 锁内"
    ),
}


def _rewrite_action_for(
    reason: str,
    *,
    page_type: str = "",
    outline_page: str = "",
    research_page: str = "",
) -> str:
    if (
        _is_chart_candidate_page(
            page_type,
            outline_page=outline_page,
            research_page=research_page,
        )
        and reason in _CHART_CANDIDATE_CHROME_REWRITE_REASONS
    ):
        return _CHART_CANDIDATE_REWRITE_ACTIONS[reason]
    return _REWRITE_ACTIONS.get(reason, "针对性优化该项")


def _build_rewrite_hint(
    failed_items: list[str],
    *,
    page_type: str = "",
    outline_page: str = "",
    research_page: str = "",
) -> str:
    if not failed_items:
        return ""
    lines = ["不通过项与补救动作："]
    for item in failed_items:
        action = _rewrite_action_for(
            item,
            page_type=page_type,
            outline_page=outline_page,
            research_page=research_page,
        )
        lines.append(f"- {item} → {action}")
    return "\n".join(lines)


def _build_page_gen_rewrite_hint(
    reason: str,
    *,
    page_type: str = "",
    outline_page: str = "",
    research_page: str = "",
) -> str:
    """定向重试指引：按真实校验 reason 生成，避免一律导向图表高度链。"""
    reason = (reason or "").strip()
    if not reason:
        return (
            "上一轮生成的 HTML 校验失败。"
            "请对照模板填槽/DOM/图表高度链等校验规则修复后重试。"
        )
    body = _build_rewrite_hint(
        [reason],
        page_type=page_type,
        outline_page=outline_page,
        research_page=research_page,
    )
    return f"上一轮生成的 HTML 校验失败（reason={reason}）。\n{body}"


_CHECK_LAYOUT_DENSITY = "lean"
_ACTIVATE_TEMPLATE_CHART_TIMEOUT_SECONDS = 5
_CHECK_LAYOUT_HARD_TAGS = (
    "slide-boundary-overflow",
    "footer-intrusion",
    "chart-label-overlap",
    "overflow",
    "whitespace",
    "v-gap",
)
_CHECK_LAYOUT_SOFT_WARNING_RE = re.compile(r"[\w-]+-warning\b", re.IGNORECASE)
_CHECK_LAYOUT_PAGE_REF_RE = re.compile(
    r"(?:page[-\s#:]?|\"page\"?\s*:\s*)(\d+)",
    re.IGNORECASE,
)


def _page_qualifies_for_check_layout(page_type: str) -> bool:
    """内容页 + agenda；cover/section/ending 等其余结构页排除。"""
    normalized = (page_type or "").strip().lower()
    if normalized == "agenda":
        return True
    if normalized in _STRUCTURAL_TEMPLATE_PAGE_TYPES:
        return False
    return True


def _page_qualifies_for_chart_gate(page_type: str) -> bool:
    """纯内容页（可能有 CHART_SCAFFOLD）；排除 cover/agenda/section/ending。"""
    normalized = (page_type or "").strip().lower()
    return normalized not in _STRUCTURAL_TEMPLATE_PAGE_TYPES


_COMMENTED_CHART_SCAFFOLD_BLOCK_RE = re.compile(
    r"<!--\s*(CHART_SCAFFOLD(?:_\d+)?_BEGIN)\s*([\s\S]*?)(CHART_SCAFFOLD(?:_\d+)?_END)\s*-->",
    re.IGNORECASE,
)
_CANONICAL_ACTIVE_CHART_SCAFFOLD_RE = re.compile(
    r'<script\b[^>]*\bdata-pptx-chart-scaffold\s*=\s*(["\'])v1\1',
    re.IGNORECASE,
)


def _html_requires_activate_template_chart(html: str) -> bool:
    """是否应调用 pptx-craft activate-template-chart（与 skill 调用时机对齐）。

    需要 CLI 的情况（对齐 activate-template-chart / designer 激活契约）：
    1. 注释内 CHART_SCAFFOLD 已填 option（待 CLI 成对删除定界符）；
    2. 已暴露的 canonical active scaffold（data-pptx-chart-scaffold=v1）待验收。

    纯 dormant（option=null），即使有图表容器也不调用——CLI 对 null option
    会 exit 1；见 CHECK-LAYOUT.md §11.5。已激活且无 canonical marker 的普通
    content-template 页也不调用（避免二次 CLI 误报 no canonical）。
    """
    if not html:
        return False
    blocks = list(_COMMENTED_CHART_SCAFFOLD_BLOCK_RE.finditer(html))
    if blocks:
        for block in blocks:
            body = block.group(2) or ""
            if _chart_scaffold_option_populated(body):
                return True
        # 注释块均为纯 dormant：去掉注释 scaffold 后再查页内已暴露的 canonical
        html_outside_comments = _COMMENTED_CHART_SCAFFOLD_BLOCK_RE.sub("", html)
        return _CANONICAL_ACTIVE_CHART_SCAFFOLD_RE.search(html_outside_comments) is not None
    return _CANONICAL_ACTIVE_CHART_SCAFFOLD_RE.search(html) is not None


def _collect_check_layout_page_nums(
    successful_pages: list[int],
    outline_pages: dict[int, str],
) -> list[int]:
    """收集已通过静态校验并成功落盘、需做 check-layout 的页码。"""
    selected: list[int] = []
    for page_num in sorted(successful_pages):
        outline_page = str(outline_pages.get(page_num) or "")
        page_type = _detect_page_type(outline_page)
        if _page_qualifies_for_check_layout(page_type):
            selected.append(page_num)
    return selected


def _coerce_check_layout_page_num(value: Any) -> int | None:
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    page_num = int(match.group(1))
    return page_num if page_num > 0 else None


def _extract_hard_tags_from_check_layout_line(line: str) -> list[str]:
    if _CHECK_LAYOUT_SOFT_WARNING_RE.search(line):
        return []
    lower = line.lower()
    found: list[str] = []
    for tag in _CHECK_LAYOUT_HARD_TAGS:
        if tag in lower:
            found.append(tag)
    if "slide-boundary-overflow" in found and "overflow" in found:
        found = [tag for tag in found if tag != "overflow"]
    return found


def _extract_hard_issues_from_check_layout_value(value: Any) -> list[str]:
    issues: list[str] = []
    if isinstance(value, str):
        for tag in _extract_hard_tags_from_check_layout_line(value):
            if tag not in issues:
                issues.append(tag)
        return issues
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                for tag in _extract_hard_tags_from_check_layout_line(item):
                    if tag not in issues:
                        issues.append(tag)
            elif isinstance(item, dict):
                tag = str(item.get("type") or item.get("code") or item.get("id") or "")
                summary = str(item.get("message") or item.get("summary") or tag)
                for hard_tag in _extract_hard_tags_from_check_layout_line(
                    f"{tag} {summary}"
                ):
                    if hard_tag not in issues:
                        issues.append(hard_tag)
    elif isinstance(value, dict):
        for key in ("issues", "failures", "hard", "errors"):
            nested = value.get(key)
            if nested is not None:
                issues.extend(_extract_hard_issues_from_check_layout_value(nested))
        tag = str(value.get("type") or value.get("code") or value.get("id") or "")
        summary = str(value.get("message") or value.get("summary") or tag)
        for hard_tag in _extract_hard_tags_from_check_layout_line(f"{tag} {summary}"):
            if hard_tag not in issues:
                issues.append(hard_tag)
    return issues


def _parse_check_layout_json_payload(payload: dict[str, Any]) -> dict[int, list[str]]:
    failures: dict[int, list[str]] = {}
    pages_obj = (
        payload.get("pages")
        or payload.get("results")
        or payload.get("failures")
        or payload.get("pageResults")
    )
    if isinstance(pages_obj, dict):
        for key, value in pages_obj.items():
            page_num = _coerce_check_layout_page_num(key)
            issues = _extract_hard_issues_from_check_layout_value(value)
            if page_num and issues:
                failures[page_num] = issues
    failed_pages = payload.get("failedPages") or payload.get("failed_pages")
    if isinstance(failed_pages, list):
        for item in failed_pages:
            if isinstance(item, dict):
                page_num = _coerce_check_layout_page_num(
                    item.get("page") or item.get("pageNum") or item.get("page_num")
                )
                issues = _extract_hard_issues_from_check_layout_value(item)
            else:
                page_num = _coerce_check_layout_page_num(item)
                issues = ["overflow"] if page_num else []
            if page_num and issues:
                failures[page_num] = issues
    return failures


def _parse_check_layout_hard_failures(
    output: str,
    page_nums: list[int] | None = None,
) -> dict[int, list[str]]:
    """解析 check-layout CLI 输出中的硬项失败页（忽略 *-warning 软警告）。"""
    text = (output or "").strip()
    if not text:
        return {}

    stripped = text
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, count=1, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped, count=1)

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        parsed = _parse_check_layout_json_payload(payload)
        if parsed:
            return parsed

    failures: dict[int, list[str]] = {}
    current_page: int | None = None
    for line in text.splitlines():
        if _CHECK_LAYOUT_SOFT_WARNING_RE.search(line):
            continue
        page_match = _CHECK_LAYOUT_PAGE_REF_RE.search(line)
        if page_match:
            current_page = int(page_match.group(1))
        tags = _extract_hard_tags_from_check_layout_line(line)
        if not tags:
            continue
        target_page = current_page
        if target_page is None and page_nums and len(page_nums) == 1:
            target_page = page_nums[0]
        if target_page is None:
            continue
        bucket = failures.setdefault(target_page, [])
        for tag in tags:
            if tag not in bucket:
                bucket.append(tag)
    return failures


def _check_layout_timeout_seconds(page_count: int) -> int:
    batches = max((page_count + 3) // 4, 1)
    return max(30, batches * 3 + 15)


def _build_check_layout_rewrite_hint(
    page_num: int,
    issues: list[str],
    *,
    page_type: str = "",
    outline_page: str = "",
    research_page: str = "",
) -> str:
    issue_lines = "\n".join(f"- {issue}" for issue in issues if issue)
    next_steps = _build_rewrite_hint(
        issues,
        page_type=page_type,
        outline_page=outline_page,
        research_page=research_page,
    )
    parts = [
        f"check-layout 渲染检测未通过（page-{page_num}，density={_CHECK_LAYOUT_DENSITY}）。",
        "硬项问题：",
        issue_lines or "- overflow",
    ]
    if next_steps:
        parts.extend(["", next_steps])
    return "\n".join(parts)


async def _run_check_layout(
    node: PlanNode,
    *,
    pages_dir: str,
    pptx_root: str,
    page_nums: list[int],
    density: str = _CHECK_LAYOUT_DENSITY,
) -> tuple[dict[int, list[str]], bool]:
    """运行 pptx-craft check-layout；返回 (硬项失败页, 是否跳过)。"""
    if not page_nums or not pages_dir or not pptx_root:
        return {}, False

    pages_arg = ",".join(str(page_num) for page_num in sorted(page_nums))
    timeout_seconds = _check_layout_timeout_seconds(len(page_nums))
    try:
        check_layout_cli = cli_path("check-layout", pptx_root)
    except BashExecError as exc:
        logger.warning("[P8.1] check-layout CLI 不可用，降级跳过: %s", exc)
        return {}, True
    cmd = (
        f"{check_layout_cli} {quote_path(pages_dir)} "
        f"--pages {pages_arg} --density {density}"
    )
    try:
        result = await run_bash(
            node,
            cmd,
            timeout_seconds=timeout_seconds,
            required=False,
            workdir=pptx_root,
        )
    except BashExecError as exc:
        logger.warning("[P8.1] check-layout CLI 不可用，降级跳过: %s", exc)
        return {}, True
    except Exception as exc:
        if isinstance(exc, AbortError):
            raise
        logger.warning("[P8.1] check-layout 异常，降级跳过: %s", exc)
        return {}, True

    detail = combined_output(result)
    failures = _parse_check_layout_hard_failures(detail, page_nums)
    if failures:
        logger.warning(
            "[P8.1] check-layout 硬项未通过 pages=%s density=%s",
            sorted(failures),
            density,
        )
        return failures, False
    if result.exit_code != 0:
        logger.warning(
            "[P8.1] check-layout exit=%d 但未解析到硬项失败，降级跳过: %s",
            result.exit_code,
            detail[:500],
        )
        return {}, True

    logger.info(
        "[P8.1] check-layout 通过 pages=%s density=%s",
        pages_arg,
        density,
    )
    return {}, False


async def _run_activate_template_chart_page(
    node: PlanNode,
    *,
    pages_dir: str,
    pptx_root: str,
    page_num: int,
) -> tuple[bool, str, bool]:
    """运行 pptx-craft activate-template-chart；返回 (通过, 失败详情, 是否跳过)。"""
    if not pages_dir or not pptx_root or page_num <= 0:
        return True, "", True
    page_path = f"{pages_dir}/page-{page_num}.pptx.html"
    try:
        chart_cli = cli_path("activate-template-chart", pptx_root)
    except BashExecError as exc:
        logger.warning("[P8.1] activate-template-chart 跳过：%s", exc)
        return True, "", True
    cmd = f"{chart_cli} --file {quote_path(page_path)}"
    try:
        result = await run_bash(
            node,
            cmd,
            timeout_seconds=_ACTIVATE_TEMPLATE_CHART_TIMEOUT_SECONDS,
            required=False,
            workdir=pptx_root,
        )
    except BashExecError as exc:
        logger.warning("[P8.1] activate-template-chart 跳过：%s", exc)
        return True, "", True
    except Exception as exc:
        if isinstance(exc, AbortError):
            raise
        logger.warning("[P8.1] activate-template-chart 异常，跳过：%s", exc)
        return True, "", True

    detail = combined_output(result).strip()
    if result.exit_code == 0:
        logger.info("[P8.1] activate-template-chart 通过 page=%d", page_num)
        return True, "", False
    logger.warning(
        "[P8.1] activate-template-chart 未通过 page=%d exit=%d detail=%s",
        page_num,
        result.exit_code,
        detail[:500],
    )
    fail_reason = detail or f"activate-template-chart exit={result.exit_code}"
    return False, fail_reason, False


def _extract_page_keywords(research_page: str) -> list[str]:
    if not research_page:
        return []
    keywords: list[str] = []
    for line in research_page.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            header = stripped.lstrip("#").strip()
            if ":" in header:
                header = header.split(":", 1)[1].strip()
            if header and len(header) <= 30:
                keywords.append(header)
            continue
        if any(stripped.startswith(prefix) for prefix in ("- 核心论点", "- 关键数据", "- 案例", "- 标题")):
            content = stripped.lstrip("- ").split("：", 1)[-1].strip()
            if content and len(content) <= 30:
                keywords.append(content)
    if keywords:
        return keywords[:3]
    if len(research_page) > 20:
        first_line = research_page.splitlines()[0].strip().lstrip("#").strip()
        if first_line and len(first_line) <= 30:
            return [first_line]
    return []


def _build_search_queries(
    templates: list[str],
    *,
    topic: str,
    page_keywords: list[str],
) -> list[str]:
    queries: list[str] = []
    if page_keywords:
        for kw in page_keywords[:2]:
            for tpl in templates[:1]:
                queries.append(tpl.format(topic=kw))
    if topic:
        for tpl in templates[:1]:
            q = tpl.format(topic=topic)
            if q not in queries:
                queries.append(q)
    return queries


def _extract_search_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result[:2000]
    if isinstance(result, list):
        parts: list[str] = []
        for item in result[:5]:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("snippet", "content", "text", "title"):
                    v = item.get(key)
                    if isinstance(v, str) and v.strip():
                        parts.append(v)
                        break
        return "\n".join(parts)[:2000]
    if isinstance(result, dict):
        for key in ("results", "items", "data"):
            v = result.get(key)
            if isinstance(v, list):
                return _extract_search_text(v)
        content = result.get("content") or result.get("text") or ""
        if isinstance(content, str):
            return content[:2000]
    return str(result)[:2000]


_PAGE_HEADING_RE = re.compile(r"^###\s+P(\d+)\s*:", re.MULTILINE)


def _strip_leading_non_section_lines(text: str) -> str:
    """剥离首部非 ``### P`` 章节开头的行。

    兼容 image_watermark 等扩展在文件首行注入的标记文本（如 "AI生成"），
    避免严格的 ``startswith("### P")`` 校验误判有效 research 文件为无效。
    """
    lines = text.splitlines(keepends=True)
    idx = 0
    while idx < len(lines) and not lines[idx].lstrip().startswith("### P"):
        idx += 1
    if idx == 0:
        return text
    if idx >= len(lines):
        return ""
    return "".join(lines[idx:])


def _split_md_pages(text: str) -> dict[int, str]:
    """按 `### P{N}:` 章节拆分 Markdown，返回 {页码: 该页片段}。"""
    matches = list(_PAGE_HEADING_RE.finditer(text))
    if not matches:
        return {}
    pages: dict[int, str] = {}
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        page_num = int(match.group(1))
        pages[page_num] = text[start:end].strip()
    return pages


# 按图片数量选择布局模板（精简自 SKILL.md 图片布局规范）
_IMAGE_LAYOUT_TEMPLATES: dict[int, str] = {
    1: (
        "### 图片布局（1 张图）\n"
        "- `usage=cover` → 全幅背景图，文字用 `z-10` 叠加\n"
        "- `usage=content` → 单图占一侧，另一侧文字\n"
        "```html\n"
        '<img src="..." class="w-full h-full object-contain" />\n'
        "```\n"
    ),
    2: (
        "### 图片布局（2 张图）\n"
        "- 推荐左右对半分或一大一小\n"
        "```html\n"
        '<div class="flex gap-3 flex-1 min-h-0">\n'
        '  <img src="..." class="flex-1 h-full object-contain" />\n'
        '  <img src="..." class="flex-1 h-full object-contain" />\n'
        '</div>\n'
        "```\n"
    ),
    3: (
        "### 图片布局（3 张图）\n"
        "- 推荐「左 1 右 2」：左侧大图，右侧上下两小图\n"
        "```html\n"
        '<div class="flex gap-3 flex-1 min-h-0">\n'
        '  <div class="flex-[2] h-full"><img src="..." class="w-full h-full object-contain" /></div>\n'
        '  <div class="flex-1 flex flex-col min-h-0 gap-2">\n'
        '    <img src="..." class="w-full flex-1 min-h-0 object-contain" />\n'
        '    <img src="..." class="w-full flex-1 min-h-0 object-contain" />\n'
        '  </div>\n'
        '</div>\n'
        "```\n"
    ),
    4: (
        "### 图片布局（4 张图）\n"
        "- 推荐 2×2 布局（用 flex flex-wrap 替代 grid）\n"
        "```html\n"
        '<div class="flex flex-wrap gap-3 flex-1 min-h-0">\n'
        '  <img src="..." class="w-[48%] h-[48%] object-contain" />\n'
        '  <img src="..." class="w-[48%] h-[48%] object-contain" />\n'
        '  <img src="..." class="w-[48%] h-[48%] object-contain" />\n'
        '  <img src="..." class="w-[48%] h-[48%] object-contain" />\n'
        '</div>\n'
        "```\n"
    ),
}

_IMAGE_LAYOUT_TEMPLATE_MANY = (
    "### 图片布局（{n} 张图）\n"
    "- 推荐用 flex flex-wrap 布局，每行 3-4 张（禁止使用 grid grid-cols-N）\n"
    "```html\n"
    '<div class="flex flex-wrap gap-3 flex-1 min-h-0">\n'
    '  <!-- {n} 张图片，每张 w-[31%] object-contain -->\n'
    '</div>\n'
    "```\n"
)


def _build_image_section(image_map_page: str) -> str:
    """根据本页图片素材描述和图片数量，构造图片素材 section。"""
    if not image_map_page:
        # 无图片素材：禁止自行绘制/编造任何图片（含外链图、伪造 CDN URL、radial-gradient 盘体等）。
        # html-to-pptx 转换器不支持 radial-gradient/url()，自行产图会导致导出后透明空区。
        return (
            "\n### 图片素材：无（本页无任何图片素材来源）\n"
            "- 禁止使用任何 `<img src=\"http...\">` 外链图片（含伪造 CDN 路径，"
            "如 cdn.digitalhumanai.top/.../assets/... 等，该类图片路径不存在）\n"
            "- 禁止 `background-image: url(http...)` 外链背景图\n"
            "- 禁止用 `radial-gradient()` 绘制任何圆形盘体/图片位"
            "（html-to-pptx 转换器不支持 radial-gradient，导出后会变成透明空区）\n"
            "- 所有视觉表达改用：纯色 + box-shadow 模拟立体感、linear-gradient 渐变、"
            "ECharts 图表、数据卡片、FontAwesome 图标，不得出现真实图片位\n"
        )
    # 统计图片数量（每行一个 "- path:" 开头）
    img_count = image_map_page.count("- path:")
    layout = _IMAGE_LAYOUT_TEMPLATES.get(img_count)
    if layout is None:
        cols = 4 if img_count >= 7 else 3
        layout = _IMAGE_LAYOUT_TEMPLATE_MANY.format(n=img_count, cols=cols)

    return (
        "\n### 图片素材（必须使用）\n"
        f"{image_map_page}\n"
        "- `usage=cover` → 用作全幅背景图："
        "`<img src=\"...\" class=\"absolute inset-0 w-full h-full object-cover\">`，"
        "文字内容用 `z-10` 叠加在上\n"
        "- `usage=content` → 用作内容配图："
        "`<img src=\"...\" class=\"w-full h-full object-contain\">`\n"
        "- 使用 `<img>` 标签引用 `path` 字段指定的路径（相对路径，直接使用）\n"
        "- 图片容器用 `min-h-0` 防溢出（禁止使用 `overflow-hidden`，图片内容需完整显示）\n"
        f"\n{layout}"
    )


def _build_page_prompt(
    page_number: int,
    style_id: str,
    style_text: str,
    outline_page: str,
    research_page: str,
    *,
    designer_md_text: str = "",
    outline_is_full: bool = False,
    research_is_full: bool = False,
    rewrite_hint: str = "",
    original_html: str = "",
    image_map_page: str = "",
    user_query: str = "",
    total_pages: int = 0,
) -> str:
    # 用户原始 query 段（用于指导内容方向/格式/风格，不改变本任务的页面范围）
    user_query_section = ""
    if user_query:
        user_query_section = (
            "## 用户原始 query（用于指导内容方向和视觉风格要求）\n"
            f"{user_query}\n"
            "⚠️ 用户 query 中的页数/总量要求已由大纲规划完成，本步骤**仅生成第 "
            f"{page_number} 页**，不生成其他页面。\n\n"
        )

    preset_clause = ""
    if style_id in _PRESET_STYLE_IDS:
        preset_clause = (
            "\n**强制性设计规范**：当前为预设风格，禁止自由发挥配色和字体，"
            "必须严格遵循风格文件中的所有定义。\n"
        )

    rewrite_section = ""
    if rewrite_hint:
        original_section = ""
        if original_html:
            original_section = (
                "\n### 上次产物（原始 HTML）\n"
                "```html\n"
                f"{original_html}\n"
                "```\n"
            )
        rewrite_constraints = (
            "⚠️ **重写约束**：\n"
            "- 仅修复上述不通过项，不要改动其他正常部分\n"
            "- 如果布局使用了 CSS Grid（`grid grid-cols-*`），必须改为 Flexbox（`flex`）布局，"
            "因为 html-to-pptx 转换器不支持 CSS Grid\n"
            "- 如果子元素使用了 `overflow-hidden`，且该元素包含核心内容（标题/正文/图表/数据卡片），"
            "必须移除 `overflow-hidden`\n"
            "- 已通过的检查项对应的代码不要修改\n"
            "- 只在不通过项相关的代码区域做修改，其余部分保持原样\n"
        )
        if original_html:
            rewrite_constraints += "- 必须基于上次产物做针对性修改，不要从零重新生成\n"
        rewrite_section = (
            "\n## 重写指引（必须修复的问题）\n"
            f"{rewrite_hint}\n"
            f"{rewrite_constraints}"
            f"{original_section}"
        )

    outline_label = "大纲 — 全文（请从中定位 ### P{N}: 章节）" if outline_is_full else "大纲 — 本页规划"
    research_label = "研究报告 — 全文（请从中定位 ### P{N}: 章节）" if research_is_full else "研究报告 — 本页素材"

    no_outline = not outline_page.strip()
    no_research = not research_page.strip()

    page_type = _detect_page_type(outline_page)
    # 与 skill SKILL.md「页面研究契约」一致：用研究需求字段判断是否结构页
    # 大纲中格式为「✅ 页研究查询: ...」或「✅ 数据需求: ...」，有则为内容页
    # 无上述字段则为结构页（仅依据大纲）
    has_research_need = "✅" in outline_page and (
        "页研究查询" in outline_page or "数据需求" in outline_page or "研究需求" in outline_page
    )
    is_structural = not has_research_need

    if no_outline:
        outline_label = "大纲（未提供，请根据重写指引和搜索补充数据自行推断页面类型与布局）"
    if is_structural:
        research_label = "（结构页，无需研究素材，仅依据大纲内容生成）"
    elif no_research:
        research_label = "研究报告（未提供，请根据重写指引和搜索补充数据自行生成内容）"

    if is_structural:
        fusion_rules = (
            "- 本页为结构页，内容仅从大纲提取标题、副标题等\n"
            "- 无需研究报告、搜索补充或数据可视化\n"
            "- 保持低密度，允许较高留白\n"
        )
    elif outline_is_full or research_is_full:
        fusion_rules = (
            f"- 以下素材为完整文档，你**仅负责第 {page_number} 页**，"
            f"请从全文中定位 `### P{page_number}:` 章节，仅使用该页内容\n"
            "- 大纲提供页面类型与数据需求，决定页面布局和内容方向\n"
            "- 研究报告提供核心论点、关键数据、案例素材，决定页面具体内容\n"
            "- 严禁将其他页面的内容混入本页\n"
        )
    elif no_outline or no_research:
        fusion_rules = (
            "- 部分素材缺失，请根据重写指引和搜索补充数据生成内容\n"
            "- 严格遵循视觉风格规范和布局硬约束\n"
            "- 确保所有文字为真实内容，禁止占位文本\n"
        )
    else:
        fusion_rules = (
            "- 大纲提供页面类型与数据需求，决定页面布局和内容方向\n"
            "- 研究报告提供核心论点、关键数据、案例素材，决定页面具体内容\n"
            "- 上述大纲 + 研究报告中的全部信息点都必须体现\n"
        )

    layout_template = _PAGE_LAYOUT_TEMPLATES.get(page_type, "")
    page_number_rule = _build_visible_page_number_rule(
        user_query,
        page_number,
        total_pages or page_number,
    )

    density_checklist = _STRUCTURAL_DENSITY_CHECKLIST if is_structural else _DENSITY_CHECKLIST_DIGEST
    design_rules = _STRUCTURAL_DESIGN_RULES if is_structural else _DESIGN_RULES_DIGEST
    html_skeleton = _STRUCTURAL_HTML_SKELETON if is_structural else _HTML_SKELETON

    # 注入新版 skill designer 规范；图表候选页从同一 designer.md 追加图表章节。
    # 文件内容由 PrepareNode 通过 read_file 工具读取后传入
    designer_section = ""
    if designer_md_text:
        designer_md = _extract_designer_section(
            designer_md_text,
            include_charts=_is_chart_candidate_page(
                page_type,
                outline_page=outline_page,
                research_page=research_page,
            ),
        )
        if designer_md:
            designer_section = f"\n### skill designer 约束（必须遵守）\n{designer_md}\n"

    # 布局多样性约束：禁止连续两页相同布局
    diversity_rule = ""
    if not is_structural:
        diversity_rule = (
            "\n### 布局多样性约束\n"
            "- 禁止连续两页使用完全相同的 main 布局结构（flex 比例、子元素数量、分栏方向）\n"
            "- 主动使用不同的布局比例（如 `flex-[3]`/`flex-[2]`、`flex-[5]`/`flex-[4]`、`flex-[2]`/`flex-[3]` 等）\n"
            "- 根据内容叙事选择布局，而非机械套用模板\n"
        )

    return (
        f"{user_query_section}"
        "## 0. 输出要求（最高优先级）\n"
        f"- 输出**第 {page_number} 页**完整 HTML（含 <!DOCTYPE>、<html>、<head>、<body>）\n"
        "- 严禁任何解释、注释、Markdown 代码块包裹，只输出 HTML 原文\n"
        "- 页面尺寸严格 1280×720px\n"
        '- 必须包含 `<div class="ppt-slide">` 容器\n'
        f"{page_number_rule}"
        f"{_EDITABLE_LAYERING_RULES}"
        "- 禁止在思考过程中反复计算像素或纠结布局，参考下方布局示例并根据内容调整\n"
        "- 一次性输出完整 HTML，禁止输出'final code''truly final'等反复确认语句\n"
        "\n"
        "### 思考预算（强制约束）\n"
        "- 布局规划**上限 300 字思考**：用 flex/grid 权重和比例描述布局意图即可，"
        "禁止逐元素计算像素高度、padding、font-size 数值\n"
        "- 禁止做「计算→验证→调整→重算」循环；若布局估算不收敛，直接采用布局示例中的默认比例\n"
        "- 优先使用 `flex-1`/`flex-[N]`/`min-h-0` 自适应布局，让浏览器自动分配空间，而非手动算尺寸\n"
        "- 内容预算（§4）一次过，禁止反复推演；若内容超量，直接提炼或删减辅助细节\n"
        "- 思考阶段产出 ≤500 tokens 即可开始写 HTML；超过此量说明陷入了过度规划，应立即停止思考并输出代码\n"
        "\n"
        "## 1. 视觉风格规范（强制遵守）\n"
        f"{style_text}\n"
        f"{preset_clause}"
        "\n"
        f"{_CDN_HEAD_SNIPPET}"
        "\n"
        f"{design_rules}"
        f"{_AUDIENCE_VISIBLE_TEXT_RULES}"
        f"{designer_section}"
        f"{diversity_rule}"
        "\n"
        f"{html_skeleton}"
        "\n"
        f"{layout_template}"
        "\n"
        f"{density_checklist}"
        "\n"
        "## 2. 内容素材\n"
        "\n"
        f"### {outline_label}\n"
        f"{outline_page}\n"
        "\n"
        f"### {research_label}\n"
        f"{research_page}\n"
        f"{_build_image_section(image_map_page)}"
        "\n"
        "## 3. 内容融合规则\n"
        f"{fusion_rules}"
        f"{rewrite_section}"
        "\n"
        "## 4. 页面内容预算（写 HTML 前必须先完成）\n"
        "- 逐项识别核心结论、关键数据、必要论据和可舍弃的辅助细节\n"
        "- 制定预算：页面类型、密度、标题行数、区域比例、卡片/要点上限、正文行数、最小字号、目标留白区间\n"
        "- 预留至少 8% 的垂直缓冲，用于字体差异、图表标签和 PPTX 转换误差\n"
        "- 若核心内容超过预算，先提炼与重排；仍无法容纳时拆页，禁止裁切或持续缩小字号\n"
        "\n"
        "## 5. 任务\n"
        f"你负责生成**第 {page_number} 页** HTML。仅生成该页，直接输出 HTML 原文。"
        "**HTML 中必须只包含 1 个 `<div class=\"ppt-slide\">` 容器**，"
        "禁止生成多个 slide 页面。"
        "先产出可运行 HTML，再按密度检查清单做小步修正；禁止在写文件前反复做像素级完整规划。"
        "生成时必须同时满足上述「内容密度检查」全部要求，"
        "确保首次生成即通过密度检查，避免后续重写。"
    )


@dataclass
class PageGenContext:
    """单页生成使用的只读上下文。"""

    page_num: int
    style_id: str
    style_text: str
    outline_page: str
    research_page: str
    outline_is_full: bool
    image_map_page: str  # 本页图片素材描述（空串=无图）
    designer_md_text: str  # references/designer.md 原文（由 PrepareNode 通过 read_file 读取）
    user_query: str = ""  # 用户原始 query（由 collect_user_text 提取）
    total_pages: int = 0
    pptx_root: str = ""  # pptx-craft 根目录（agenda 官方模板预铺用）
    outline_full: str = ""  # outline.md 全文（agenda 核对章节页码用）


def _postprocess_generated_html(raw_html: str, ctx: PageGenContext) -> tuple[str, str, str]:
    """LLM 全文生成后的同步 HTML 校验/修复（移出事件循环执行）。"""
    html = _strip_html_fence(raw_html or "")
    if not _is_valid_html(html):
        logger.warning("[P8.1] 页面 %d HTML 校验失败", ctx.page_num)
        return "", html, "invalid_html"
    html = _replace_placeholder_headings(html, ctx.outline_page)
    html = _apply_visible_page_number_policy(
        html,
        user_query=ctx.user_query,
        page_number=ctx.page_num,
        total_pages=ctx.total_pages,
        style_id=ctx.style_id,
    )
    html = _fix_echarts_svg_renderer(html)
    html = _strip_unsupported_fullpage_overlays(html)
    html = _strip_chart_header_unit(html)
    html = _fix_chart_scaffold_activation(html)
    html = _fix_chart_height_chain(html)
    if not _validate_slide_dom(html):
        logger.warning("[P8.1] 页面 %d DOM 结构校验失败", ctx.page_num)
        return "", html, "invalid_dom"
    if not _validate_chart_height_chain(html):
        logger.warning("[P8.1] 页面 %d 图表容器高度链校验失败", ctx.page_num)
        return "", html, "invalid_chart_height_chain"
    _warn_chart_mount_mismatch_soft(html, page_num=ctx.page_num)
    return html, "", ""


def _postprocess_content_template_fill(
    raw_html: str,
    *,
    seed_html: str,
    ctx: PageGenContext,
) -> tuple[str, str, str]:
    """内容页填槽后的同步校验/修复（移出事件循环执行）。"""
    html = _strip_html_fence(raw_html or "")
    html = _replace_placeholder_headings(html, ctx.outline_page)
    html = _apply_visible_page_number_policy(
        html,
        user_query=ctx.user_query,
        page_number=ctx.page_num,
        total_pages=ctx.total_pages,
        style_id=ctx.style_id,
    )
    html = _fix_echarts_svg_renderer(html)
    html = _strip_unsupported_fullpage_overlays(html)
    html = _strip_chart_header_unit(html)
    html = _fix_chart_scaffold_activation(html)
    html = _fix_chart_height_chain(html)
    if ctx.style_id == "custom":
        ok, reason = _validate_custom_content_template_fill_output(seed_html, html)
    else:
        ok, reason = _validate_content_template_fill_output(seed_html, html)
    if (
        ctx.style_id != "custom"
        and not ok
        and reason in _REPAIRABLE_CONTENT_TEMPLATE_REASONS
    ):
        repaired = _repair_content_template_chrome(seed_html, html)
        if repaired:
            repaired = _fix_echarts_svg_renderer(repaired)
            repaired = _strip_unsupported_fullpage_overlays(repaired)
            repaired = _strip_chart_header_unit(repaired)
            repaired = _fix_chart_scaffold_activation(repaired)
            repaired = _fix_chart_height_chain(repaired)
            ok_repaired, reason_repaired = _validate_content_template_fill_output(
                seed_html,
                repaired,
            )
            if ok_repaired:
                _warn_chart_mount_mismatch_soft(repaired, page_num=ctx.page_num)
                logger.info(
                    "[P8.1] repaired=content_template_chrome page=%d style=%s "
                    "from_reason=%s",
                    ctx.page_num,
                    ctx.style_id,
                    reason,
                )
                return repaired, "", ""
            logger.warning(
                "[P8.1] 内容页 chrome 自动修复后仍失败 page=%d style=%s "
                "from_reason=%s repair_reason=%s",
                ctx.page_num,
                ctx.style_id,
                reason,
                reason_repaired,
            )
    if not ok:
        logger.warning(
            "[P8.1] 内容页填槽校验失败 page=%d style=%s reason=%s",
            ctx.page_num,
            ctx.style_id,
            reason,
        )
        return "", html, reason
    _warn_chart_mount_mismatch_soft(html, page_num=ctx.page_num)
    logger.info(
        "[P8.1] 内容页官方模板填槽完成 page=%d style=%s",
        ctx.page_num,
        ctx.style_id,
    )
    return html, "", ""


def _postprocess_structural_template_fill(
    raw_html: str,
    *,
    seed_html: str,
    page_type: str,
    ctx: PageGenContext,
) -> str:
    """结构页填槽后的同步校验/修复（移出事件循环执行）。"""
    html = _strip_html_fence(raw_html or "")
    if not _is_valid_html(html):
        logger.warning(
            "[P8.1] 结构页填槽 HTML 校验失败 page=%d type=%s",
            ctx.page_num,
            page_type,
        )
        return ""
    if _has_unfilled_placeholders(html):
        logger.warning(
            "[P8.1] 结构页填槽残留占位符 page=%d type=%s placeholders=%s",
            ctx.page_num,
            page_type,
            _UNFILLED_PLACEHOLDER_RE.findall(html)[:8],
        )
        return ""
    if not _structural_chrome_matches_seed(seed_html, html):
        logger.warning(
            "[P8.1] 结构页 chrome 偏离 seed（违规修改或流式输出损坏）"
            " page=%d type=%s seed_head_len=%d filled_head_len=%d"
            " -> 尝试 chrome 自动修复",
            ctx.page_num,
            page_type,
            len(_head_chrome_signature(seed_html)),
            len(_head_chrome_signature(html)),
        )
        repaired = _repair_structural_page_chrome(seed_html, html)
        if repaired and _structural_chrome_matches_seed(seed_html, repaired):
            logger.info(
                "[P8.1] 结构页 chrome 自动修复成功 page=%d type=%s",
                ctx.page_num,
                page_type,
            )
            html = repaired
        else:
            logger.warning(
                "[P8.1] 结构页 chrome 自动修复失败 page=%d type=%s",
                ctx.page_num,
                page_type,
            )
            return ""

    html = _apply_visible_page_number_policy(
        html,
        user_query=ctx.user_query,
        page_number=ctx.page_num,
        total_pages=ctx.total_pages,
        style_id=ctx.style_id,
    )
    if not _validate_slide_dom(html):
        logger.warning(
            "[P8.1] 结构页 DOM 校验失败 page=%d type=%s",
            ctx.page_num,
            page_type,
        )
        return ""
    logger.info(
        "[P8.1] 结构页官方模板填槽完成 page=%d style=%s type=%s",
        ctx.page_num,
        ctx.style_id,
        page_type,
    )
    return html


class PrepareNode(PlanNode):
    """P8.0 — 读取素材并按页拆分，产出共享只读数据供 per-page worker 复用。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p8_0_prepare",
            instruction=(
                "## P8.0 素材预处理\n"
                "\n"
                "### 前置条件\n"
                "- `read_file` 工具可用\n"
                "- `outline.md` / `research-P{N}.md` / 风格文件均已落盘\n"
                "\n"
                "### 输入\n"
                "- `page_count`（必填）: N 页\n"
                "- `output_dir`（必填）: 工作目录（用于读 outline/research-P{N}.md）\n"
                "- `style_file_path`（必填）: 风格文件绝对路径\n"
                "\n"
                "### 输出\n"
                "- `prepare_status`: ok / failed\n"
                "- `outline_pages`: 按页拆分的 {页码: 片段}（拆分失败为空 dict，下游回退全文）\n"
                "- `research_pages`: 逐页读取的 {页码: research-P{N}.md 内容}（文件缺失时该页缺失）\n"
                "- `outline_text` / `style_text`: 全文（供下游回退与重写复用）\n"
                "- `all_pages`: 1..N 页码列表\n"
                "\n"
                "### 执行流程\n"
                "1. 读取 outline.md / style_file_path（任一失败 → prepare_status=failed）\n"
                "2. 按 `### P{N}:` 章节拆分 outline，每页只取对应片段；拆分失败时回退全文\n"
                "3. 逐页读取 research-P{N}.md（1..page_count），文件缺失时该页 research_pages 缺失\n"
                "4. 返回共享只读数据，供 P8.1 per-page worker 复用\n"
                "\n"
                "### 失败兜底\n"
                "- 读 outline/style 失败：prepare_status=failed，根节点直接终止，不进入 P8.1\n"
                "- 某页 research-P{N}.md 缺失：该页 research_pages 缺失，下游 worker 仅依据 outline 生成\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        page_count = int(inputs.get("page_count") or 0)
        output_dir = str(inputs.get("output_dir") or "").strip()
        style_file_path = str(inputs.get("style_file_path") or "").strip()

        outline_text = await self._read_file(f"{output_dir}/outline.md")
        style_text = await self._read_file(style_file_path)

        outline_pages = _split_md_pages(outline_text)
        total_pages = PptCommon.resolve_total_pages(
            page_count=page_count,
            total_pages=inputs.get("total_pages"),
            outline_text=outline_text,
            outline_pages=outline_pages,
            default_structural_pages=_DEFAULT_STRUCTURAL_PAGES,
        )

        if not outline_text or not style_text:
            logger.error(
                "[P8.0] 资料读取失败 outline=%d style=%d",
                len(outline_text),
                len(style_text),
            )
            return {
                "prepare_status": "failed",
                "outline_pages": {},
                "research_pages": {},
                "outline_text": outline_text,
                "style_text": style_text,
                "all_pages": list(range(1, total_pages + 1)) if total_pages > 0 else [],
                "total_pages": total_pages,
            }

        if not outline_pages:
            logger.warning("[P8.0] outline.md 未拆分到任何页面章节，下游回退全文")

        # 遍历 total_pages（含结构页），❌ 页无 research 文件会跳过
        all_pages = list(range(1, total_pages + 1)) if total_pages > 0 else sorted(outline_pages.keys())
        research_pages: dict[int, str] = {}
        for p in all_pages:
            research_path = f"{output_dir}/research-P{p}.md"
            research_text_p = await self._read_file(research_path)
            # 剥离首部非 ### P 行（兼容 image_watermark 等扩展注入的首行标记），再校验
            if research_text_p:
                research_text_p = _strip_leading_non_section_lines(research_text_p)
            # 校验内容是有效的 research 片段（以 ### P 开头），过滤 read_file 错误消息
            if research_text_p and research_text_p.lstrip().startswith("### P"):
                research_pages[p] = research_text_p
            else:
                logger.warning("[P8.0] research-P%d.md 不存在或内容无效", p)

        # 读取 image_map.json（P6.5 产出，供 P8 注入图片素材）
        image_map_path = str(inputs.get("image_map_path") or "").strip()
        image_map: dict[str, Any] = {}
        if image_map_path:
            raw = await self._read_file(image_map_path)
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        # 只保留页码 key（过滤 metadata），转为 {str(page_num): [img, ...]}
                        for key, value in parsed.items():
                            if key != "metadata" and isinstance(value, list):
                                image_map[key] = value
                except Exception as e:
                    if isinstance(e, AbortError):
                        raise
                    logger.warning("[P8.0] image_map.json 解析失败: %s", e)

        # 读取 skill designer 规范文件（通过 read_file 工具，skill_code 禁止直接 IO）
        pptx_root = str(inputs.get("pptx_root") or "").strip()
        designer_md_text = await self._read_file(f"{pptx_root}/references/designer.md")

        logger.info(
            "[P8.0] 预处理完成 outline_pages=%d research_pages=%d image_map_pages=%d total_pages=%d",
            len(outline_pages),
            len(research_pages),
            len(image_map),
            total_pages,
        )
        return {
            "prepare_status": "ok",
            "outline_pages": outline_pages,
            "research_pages": research_pages,
            "outline_text": outline_text,
            "style_text": style_text,
            "all_pages": all_pages,
            "image_map": image_map,
            "designer_md_text": designer_md_text,
            "total_pages": total_pages,
        }

    async def _read_file(self, path: str) -> str:
        if not path:
            return ""
        if not self.has_tool("read_file"):
            logger.warning("[P8.0] read_file 工具不可用 %s", path)
            return ""
        try:
            result = await self.call_tool("read_file", file_path=path)
            content = PptCommon.parse_tool_file_content(result)
            return content
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.0] 读取文件失败 %s: %s", path, e)
            return ""

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        result = await self._execute(inputs)
        ok = result.get("prepare_status") == "ok"
        yield {
            **result,
            "node": self.plan_name,
            "status": "ok" if ok else "error",
            "message": "素材预处理完成" if ok else "素材读取失败",
        }


class PageWorkerNode(DisableThinkingMixin, PlanNode):
    """P8.1 — 按新版 pptx-craft 规则并发生成并校验每页 HTML。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p8_1_page_worker",
            instruction=(
                "## P8.1 per-page 页面生成\n"
                "\n"
                "### 前置条件\n"
                "- `write_file` 工具可用\n"
                "- P8.0 已产出共享只读数据（outline_pages/research_pages/全文/style_text）\n"
                "- `pages_dir` 已存在\n"
                "\n"
                "### 输入\n"
                "- `page_count`（必填）: N 页\n"
                "- `pages_dir`（必填）: HTML 输出目录绝对路径\n"
                "- `style_id`（必填）: 用于判定是否预设风格强约束\n"
                "- `outline_pages` / `research_pages`（来自 P8.0）: 按页拆分片段\n"
                "- `outline_text` / `style_text`（来自 P8.0）: 全文，拆分失败时回退\n"
                "- `all_pages`（来自 P8.0）: 1..N 页码列表\n"
                "- `gen_retry_round`（可选，默认 1；含首次生成在内最多尝试 3 次）\n"
                "\n"
                "### 输出\n"
                "- `page_files`: 实际产出的 page-*.pptx.html 列表\n"
                "- `missing_pages`: 仍缺失的页码（用于上层标 partial）\n"
                "- `low_density_pages` / `density_report`: 兼容历史输出，固定为空\n"
                "- `outline_text` / `style_text`（透传给 P8.2）\n"
                "\n"
                "### 执行流程（N 页 asyncio.gather 并发）\n"
                "对每一页独立执行：\n"
                "1. 若 style_id ∈ 预设四风格∪custom 且页型为 agenda：读取官方 "
                "`references/styles/{style_id}/agenda-template.html` 预铺，LLM 仅替换 `{{}}` "
                "占位符；残留占位符判失败；不走整页自由生成\n"
                "2. 其余页：用该页 outline 片段 + research 片段 + 风格规范 + 视觉与布局硬约束构造 prompt，"
                "调 LLM 生成 HTML；剥 ```html 包裹 → 校验（含 <!DOCTYPE> + 恰好 1 个 ppt-slide）"
                "→ write_file 落盘\n"
                "   - 多 ppt-slide / 非法 HTML：本轮失败，按 gen_retry_round 重试（仅本页）\n"
                "   - 重试后仍失败 → 进 missing_pages\n"
                "3. 校验通过的单 ppt-slide 页直接落盘；生成后不再调用 LLM 核查、搜索或整页重写\n"
                "\n"
                "### 失败兜底\n"
                "- 生成 LLM 调用 raise / 返回空 / HTML 校验失败：进 missing_pages\n"
                "- agenda 模板缺失或填槽后残留 `{{...}}`：进 missing_pages\n"
                "- 首次落盘 write_file 异常：进 missing_pages\n"
                "- 重试后仍缺失：透传给根节点\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        pages_dir = str(inputs.get("pages_dir") or "").strip()
        style_id = str(inputs.get("style_id") or "").strip()
        gen_retry_round = int(inputs.get("gen_retry_round") or _DEFAULT_GEN_RETRY_ROUND)

        outline_pages: dict[int, str] = inputs.get("outline_pages") or {}
        research_pages: dict[int, str] = inputs.get("research_pages") or {}
        outline_full = str(inputs.get("outline_text") or "")
        style_text = str(inputs.get("style_text") or "")
        all_pages: list[int] = list(inputs.get("all_pages") or [])
        image_map: dict[str, Any] = inputs.get("image_map") or {}
        designer_md_text = str(inputs.get("designer_md_text") or "")
        user_query = PptCommon.collect_user_text(inputs)
        pptx_root = str(inputs.get("pptx_root") or "").strip()

        if not pages_dir or not all_pages:
            logger.error("[P8.1] 必填输入缺失，跳过生成")
            return {
                "page_files": [],
                "missing_pages": list(all_pages),
                "low_density_pages": [],
                "density_report": {},
                "outline_text": outline_full,
                "style_text": style_text,
            }

        total_pages = int(inputs.get("total_pages") or max(all_pages))
        logger.info(
            "[P8.1] per-page 并发生成 pages=%d postprocess_concurrency=%d",
            len(all_pages),
            _P8_1_POSTPROCESS_CONCURRENCY,
        )

        tasks = [
            self._run_page_pipeline(
                page_num=p,
                pages_dir=pages_dir,
                style_id=style_id,
                style_text=style_text,
                outline_page=outline_pages.get(p, outline_full),
                research_page=research_pages.get(p, ""),
                outline_is_full=p not in outline_pages,
                gen_retry_round=gen_retry_round,
                image_map=image_map,
                designer_md_text=designer_md_text,
                user_query=user_query,
                total_pages=total_pages,
                pptx_root=pptx_root,
                outline_full=outline_full,
            )
            for p in all_pages
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, (AbortError, asyncio.CancelledError)):
                raise result

        missing_pages: list[int] = []
        vote_pages: list[dict[str, Any]] = []
        for p, r in zip(all_pages, results):
            if isinstance(r, BaseException):
                logger.warning("[P8.1] 页面 %d 生成异常: %s", p, r)
                missing_pages.append(p)
                continue
            if r.get("missing"):
                missing_pages.append(p)
                continue
            # 收集落盘页用于跨页 head 指纹投票
            vote_pages.append({
                "page_num": int(r.get("page_num") or p),
                "html": str(r.get("html") or ""),
            })

        # 跨页 head 指纹投票：偏离多数派的页判定损坏，进 missing_pages 走补写通道
        vote_deviant = _vote_head_fingerprints(vote_pages)
        if vote_deviant:
            missing_pages.extend(
                p for p in vote_deviant if p not in missing_pages
            )
            logger.warning(
                "[P8.1] head 指纹投票发现损坏页 pages=%s，转 missing 走补写",
                sorted(vote_deviant),
            )

        # agenda 条目数校验：目录页条目数必须等于大纲内容章节数
        agenda_deviant = _validate_agenda_item_count(outline_full, vote_pages)
        if agenda_deviant:
            missing_pages.extend(
                p for p in agenda_deviant if p not in missing_pages
            )
            logger.warning(
                "[P8.1] agenda 条目数与大纲内容章节数不匹配 pages=%s，转 missing 走补写",
                sorted(agenda_deviant),
            )

        successful_pages = [p for p in all_pages if p not in missing_pages]
        page_files = [f"page-{p}.pptx.html" for p in successful_pages]

        layout_meta = await self._apply_check_layout_pass(
            pages_dir=pages_dir,
            pptx_root=pptx_root,
            successful_pages=successful_pages,
            outline_pages=outline_pages,
            research_pages=research_pages,
            outline_full=outline_full,
            style_id=style_id,
            style_text=style_text,
            image_map=image_map,
            designer_md_text=designer_md_text,
            user_query=user_query,
            total_pages=total_pages,
        )

        logger.info(
            "[P8.1] per-page 生成完成 success=%d/%d missing=%d",
            len(successful_pages),
            len(all_pages),
            len(missing_pages),
        )
        return {
            "page_files": page_files,
            "missing_pages": missing_pages,
            "low_density_pages": [],
            "density_report": {},
            "outline_text": outline_full,
            "style_text": style_text,
            **layout_meta,
        }


    async def _apply_check_layout_pass(
        self,
        *,
        pages_dir: str,
        pptx_root: str,
        successful_pages: list[int],
        outline_pages: dict[int, str],
        research_pages: dict[int, str],
        outline_full: str,
        style_id: str,
        style_text: str,
        image_map: dict[str, Any],
        designer_md_text: str,
        user_query: str,
        total_pages: int,
    ) -> dict[str, Any]:
        """P8.1 末尾：内容页+agenda 做 check-layout，至多一轮再填槽+复检。"""
        empty_result = {
            "layout_check_skipped": False,
            "layout_warning_pages": [],
            "layout_retry_pages": [],
        }
        check_pages = _collect_check_layout_page_nums(successful_pages, outline_pages)
        if not check_pages:
            return empty_result
        if not pptx_root:
            logger.warning("[P8.1] check-layout 跳过：缺少 pptx_root")
            return {**empty_result, "layout_check_skipped": True}

        failures, skipped = await _run_check_layout(
            self,
            pages_dir=pages_dir,
            pptx_root=pptx_root,
            page_nums=check_pages,
        )
        if skipped:
            return {**empty_result, "layout_check_skipped": True}
        if not failures:
            return empty_result

        layout_warning_pages: list[int] = []
        layout_retry_pages: list[int] = []
        retried_pages: list[int] = []

        async def _rewrite_one_failed_page(page_num: int) -> tuple[int, bool]:
            """单页再填槽；返回 (page_num, 是否写盘成功)。与首轮 gather 同页隔离。"""
            issues = failures.get(page_num) or []
            path = f"{pages_dir}/page-{page_num}.pptx.html"
            previous_html = await self._read_file(path)
            outline_page = str(outline_pages.get(page_num) or outline_full)
            research_page = str(research_pages.get(page_num) or "")
            rewrite_hint = _build_check_layout_rewrite_hint(
                page_num,
                issues,
                page_type=_detect_page_type(outline_page),
                outline_page=outline_page,
                research_page=research_page,
            )

            page_images = image_map.get(str(page_num), [])
            image_map_page = ""
            if page_images:
                lines = []
                for img in page_images:
                    path_val = str(img.get("path", ""))
                    lines.append(
                        f"- path: {path_val}, usage: {img.get('usage', 'content')}, "
                        f"description: {img.get('description', '')}, type: {img.get('type', '')}"
                    )
                image_map_page = "\n".join(lines)

            ctx = PageGenContext(
                page_num=page_num,
                style_id=style_id,
                style_text=style_text,
                outline_page=outline_page,
                research_page=research_page,
                outline_is_full=page_num not in outline_pages,
                image_map_page=image_map_page,
                designer_md_text=designer_md_text,
                user_query=user_query,
                total_pages=total_pages,
                pptx_root=pptx_root,
                outline_full=outline_full,
            )
            html, _, _ = await self._generate_one(
                ctx,
                rewrite_hint=rewrite_hint,
                original_html=previous_html,
            )
            if html and await self._write_file(path, html):
                # 与首写对齐：按需 chart CLI；失败回退旧 HTML（layout 路径不进 missing）
                page_type = _detect_page_type(outline_page)
                if _page_qualifies_for_chart_gate(page_type) and pptx_root:
                    if _html_requires_activate_template_chart(html):
                        passed, detail, skipped = await _run_activate_template_chart_page(
                            self,
                            pages_dir=pages_dir,
                            pptx_root=pptx_root,
                            page_num=page_num,
                        )
                        if not skipped and not passed:
                            logger.warning(
                                "[P8.1] check-layout 再填后 chart gate 未通过 "
                                "page=%d detail=%s，回退上一版 HTML",
                                page_num,
                                detail,
                            )
                            if previous_html:
                                await self._write_file(path, previous_html)
                            return page_num, False
                return page_num, True

            logger.warning(
                "[P8.1] check-layout 再填槽失败 page=%d，保留上一版 HTML",
                page_num,
            )
            if previous_html:
                await self._write_file(path, previous_html)
            return page_num, False

        # 与首轮填槽一致：失败页 asyncio.gather 并发再填（仍仅一轮）
        rewrite_results = await asyncio.gather(
            *[_rewrite_one_failed_page(page_num) for page_num in sorted(failures)],
            return_exceptions=True,
        )
        for result in rewrite_results:
            if isinstance(result, (AbortError, asyncio.CancelledError)):
                raise result
            if isinstance(result, BaseException):
                raise result
            page_num, ok = result
            if ok:
                retried_pages.append(page_num)
                layout_retry_pages.append(page_num)
            else:
                layout_warning_pages.append(page_num)

        retried_pages = sorted(retried_pages)
        layout_retry_pages = sorted(layout_retry_pages)

        if retried_pages:
            recheck_failures, recheck_skipped = await _run_check_layout(
                self,
                pages_dir=pages_dir,
                pptx_root=pptx_root,
                page_nums=retried_pages,
            )
            if recheck_skipped:
                return {
                    "layout_check_skipped": True,
                    "layout_warning_pages": sorted(set(layout_warning_pages)),
                    "layout_retry_pages": layout_retry_pages,
                }
            for page_num in retried_pages:
                if page_num in recheck_failures:
                    if page_num not in layout_warning_pages:
                        layout_warning_pages.append(page_num)
                    logger.warning(
                        "[P8.1] check-layout 复检仍失败 page=%d issues=%s",
                        page_num,
                        recheck_failures.get(page_num),
                    )

        return {
            "layout_check_skipped": False,
            "layout_warning_pages": sorted(set(layout_warning_pages)),
            "layout_retry_pages": layout_retry_pages,
        }


    async def _run_page_pipeline(
        self,
        *,
        page_num: int,
        pages_dir: str,
        style_id: str,
        style_text: str,
        outline_page: str,
        research_page: str,
        outline_is_full: bool,
        gen_retry_round: int,
        image_map: dict[str, Any],
        designer_md_text: str = "",
        user_query: str = "",
        total_pages: int = 0,
        pptx_root: str = "",
        outline_full: str = "",
    ) -> dict[str, Any]:
        """生成并校验单页；仅生成失败时按预算重试。"""
        path = f"{pages_dir}/page-{page_num}.pptx.html"

        # 从 image_map 中提取本页图片素材描述
        page_images = image_map.get(str(page_num), [])
        image_map_page = ""
        if page_images:
            lines = []
            for img in page_images:
                path_val = str(img.get("path", ""))
                lines.append(
                    f"- path: {path_val}, usage: {img.get('usage', 'content')}, "
                    f"description: {img.get('description', '')}, type: {img.get('type', '')}"
                )
            image_map_page = "\n".join(lines)

        ctx = PageGenContext(
            page_num=page_num,
            style_id=style_id,
            style_text=style_text,
            outline_page=outline_page,
            research_page=research_page,
            outline_is_full=outline_is_full,
            image_map_page=image_map_page,
            designer_md_text=designer_md_text,
            user_query=user_query,
            total_pages=total_pages,
            pptx_root=pptx_root,
            outline_full=outline_full or outline_page,
        )

        page_type = _detect_page_type(outline_page)
        needs_chart_gate = _page_qualifies_for_chart_gate(page_type)

        final_html = ""
        last_raw_html = ""
        last_fail_reason = ""
        attempt_count = min(
            max(gen_retry_round + 1, 1),
            _MAX_PAGE_GENERATION_ATTEMPTS,
        )
        for attempt in range(attempt_count):
            rewrite_hint = ""
            original_html = ""
            if attempt > 0:
                logger.info("[P8.1] 页面 %d 第 %d 轮生成重试", page_num, attempt + 1)
                if last_raw_html or last_fail_reason:
                    rewrite_hint = _build_page_gen_rewrite_hint(
                        last_fail_reason,
                        page_type=_detect_page_type(ctx.outline_page),
                        outline_page=ctx.outline_page,
                        research_page=ctx.research_page,
                    )
                    original_html = last_raw_html
            html, last_raw_html, gen_fail_reason = await self._generate_one(
                ctx, rewrite_hint=rewrite_hint, original_html=original_html
            )
            if not html:
                if gen_fail_reason:
                    last_fail_reason = gen_fail_reason
                continue

            if not await self._write_file(path, html):
                last_fail_reason = "write_file_failed"
                continue

            if needs_chart_gate:
                if not pptx_root:
                    logger.warning(
                        "[P8.1] activate-template-chart 跳过：缺少 pptx_root page=%d",
                        page_num,
                    )
                    final_html = html
                    break
                if not _html_requires_activate_template_chart(html):
                    final_html = html
                    break
                passed, detail, skipped = await _run_activate_template_chart_page(
                    self,
                    pages_dir=pages_dir,
                    pptx_root=pptx_root,
                    page_num=page_num,
                )
                if skipped or passed:
                    final_html = html
                    break
                last_fail_reason = detail or "activate-template-chart"
                await self._delete_page_file(path)
                continue

            final_html = html
            break

        if not final_html:
            await self._delete_page_file(path)
            return {"missing": True, "low_density": False, "report": {}}

        return {
            "missing": False,
            "low_density": False,
            "report": {},
            # 跨页 head 指纹投票用：落盘页的原始 HTML 与页码
            "html": final_html,
            "page_num": page_num,
        }

    async def _read_file(self, path: str) -> str:
        if not path:
            return ""
        if not self.has_tool("read_file"):
            logger.warning("[P8.1] read_file 工具不可用 %s", path)
            return ""
        try:
            result = await self.call_tool("read_file", file_path=path)
            return PptCommon.parse_tool_file_content(result)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.1] 读取文件失败 %s: %s", path, e)
            return ""

    async def _generate_structural_template_fill(
        self,
        ctx: PageGenContext,
        page_type: str,
    ) -> str:
        """预设/custom 结构页：官方模板预铺 + 仅填 {{}}。"""
        if not ctx.pptx_root:
            logger.error(
                "[P8.1] 结构页填槽缺少 pptx_root page=%d type=%s",
                ctx.page_num,
                page_type,
            )
            return ""

        template_page_type = _STRUCTURAL_TEMPLATE_PAGE_TYPES.get(page_type, page_type)
        template_path = _resolve_style_page_template_path(
            ctx.pptx_root,
            ctx.style_id,
            page_type=template_page_type,
        )
        seed_html = await self._read_file(template_path)
        if not seed_html.strip():
            logger.error(
                "[P8.1] 结构页官方模板缺失或为空 page=%d style=%s type=%s path=%s",
                ctx.page_num,
                ctx.style_id,
                page_type,
                template_path,
            )
            return ""

        prompt = _build_structural_template_fill_prompt(
            page_number=ctx.page_num,
            page_type=page_type,
            template_page_type=template_page_type,
            style_id=ctx.style_id,
            style_text=ctx.style_text,
            outline_page=ctx.outline_page,
            outline_full=ctx.outline_full,
            seed_html=seed_html,
            user_query=ctx.user_query,
        )
        node_suffix = f"{template_page_type}_fill_{ctx.page_num}"
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt=(
                    "你是 PPT 模板填充师。只替换模板中的 {{...}} 占位符，"
                    "直接输出完整 HTML 原文，不输出任何解释。"
                ),
                node_name=f"p8_1_{node_suffix}",
                concurrent=True,
            )
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning(
                "[P8.1] 结构页填槽 LLM 失败 page=%d type=%s: %s",
                ctx.page_num,
                page_type,
                e,
            )
            return ""

        return await _run_postprocess(
            _postprocess_structural_template_fill,
            result or "",
            seed_html=seed_html,
            page_type=page_type,
            ctx=ctx,
        )

    async def _generate_content_template_fill(
        self, ctx: PageGenContext, *, rewrite_hint: str = "", original_html: str = ""
    ) -> tuple[str, str, str]:
        """四预设 ∪ custom 内容页：官方 content-template 预铺填槽。

        返回 (校验通过的 html 或空串, 最后一次产物 html 或空串, 失败 reason 或空串)。
        """
        if not ctx.pptx_root:
            logger.error("[P8.1] 内容页填槽缺少 pptx_root page=%d", ctx.page_num)
            return "", "", ""

        template_path = _resolve_style_page_template_path(
            ctx.pptx_root,
            ctx.style_id,
            page_type="content",
        )
        seed_html = await self._read_file(template_path)
        if not seed_html.strip():
            logger.error(
                "[P8.1] 内容页官方模板缺失或为空 page=%d style=%s path=%s",
                ctx.page_num,
                ctx.style_id,
                template_path,
            )
            return "", "", ""

        try:
            result = await self.stream_llm_collect(
                prompt=_build_content_template_fill_prompt(
                    page_number=ctx.page_num,
                    style_id=ctx.style_id,
                    style_text=ctx.style_text,
                    outline_page=ctx.outline_page,
                    research_page=ctx.research_page,
                    outline_full=ctx.outline_full,
                    seed_html=seed_html,
                    image_map_page=ctx.image_map_page,
                    designer_md_text=ctx.designer_md_text,
                    user_query=ctx.user_query,
                    total_pages=ctx.total_pages,
                    rewrite_hint=rewrite_hint,
                    original_html=original_html,
                ),
                system_prompt=_build_content_template_fill_system_prompt(
                    style_id=ctx.style_id,
                    page_type=_detect_page_type(ctx.outline_page),
                    outline_page=ctx.outline_page,
                    research_page=ctx.research_page,
                ),
                node_name=f"p8_1_content_fill_{ctx.page_num}",
                concurrent=True,
            )
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.1] 内容页填槽 LLM 失败 page=%d: %s", ctx.page_num, e)
            return "", "", "llm_failed"

        return await _run_postprocess(
            _postprocess_content_template_fill,
            result or "",
            seed_html=seed_html,
            ctx=ctx,
        )

    async def _generate_one(
        self, ctx: PageGenContext, *, rewrite_hint: str = "", original_html: str = ""
    ) -> tuple[str, str, str]:
        """生成单页 HTML，返回 (校验通过的 html 或空串, 最后一次产物 html 或空串, 失败 reason 或空串)。

        rewrite_hint / original_html 非空时，prompt 中追加重写指引，用于定向重试。
        """
        page_type = _detect_page_type(ctx.outline_page)
        if _uses_structural_template_fill(ctx.style_id, page_type):
            filled = await self._generate_structural_template_fill(ctx, page_type)
            return (filled or "", "", "")
        if _uses_content_template_fill(ctx.style_id, page_type, ctx.outline_page):
            return await self._generate_content_template_fill(
                ctx, rewrite_hint=rewrite_hint, original_html=original_html
            )

        try:
            result = await self.stream_llm_collect(
                prompt=_build_page_prompt(
                    ctx.page_num,
                    style_id=ctx.style_id,
                    style_text=ctx.style_text,
                    outline_page=ctx.outline_page,
                    research_page=ctx.research_page,
                    outline_is_full=ctx.outline_is_full,
                    research_is_full=False,
                    image_map_page=ctx.image_map_page,
                    designer_md_text=ctx.designer_md_text,
                    user_query=ctx.user_query,
                    total_pages=ctx.total_pages,
                    rewrite_hint=rewrite_hint,
                    original_html=original_html,
                ),
                system_prompt="你是资深演示文稿设计师，直接输出完整 HTML 原文，不输出任何解释。",
                node_name=f"p8_1_page_{ctx.page_num}",
                concurrent=True,
            )
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.1] 页面 %d 生成 LLM 失败: %s", ctx.page_num, e)
            return "", "", "llm_failed"
        return await _run_postprocess(
            _postprocess_generated_html, result or "", ctx
        )

    async def _write_file(self, path: str, content: str) -> bool:
        if not self.has_tool("write_file"):
            logger.error("[P8.1] write_file 工具不可用 %s", path)
            return False
        try:
            await self.call_tool("write_file", file_path=path, content=content)
            # 让出事件循环，保证 WebSocket 协议 ping/pong 能被调度。
            await asyncio.sleep(0)
            return True
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.error("[P8.1] 写入文件失败 %s: %s", path, e)
            return False

    async def _delete_page_file(self, path: str) -> None:
        """删除落盘 HTML，防止 convert 整目录扫入坏页（skill_code 禁 direct unlink）。"""
        if not path:
            return
        try:
            await run_bash(
                self,
                f"rm -f {quote_path(path)}",
                timeout_seconds=10,
                required=False,
            )
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.1] 删除页面文件失败 path=%s err=%s", path, e)

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        result = await self._execute(inputs)
        missing = result.get("missing_pages", [])
        status = "ok" if not missing else "warning"
        yield {
            **result,
            "node": self.plan_name,
            "status": status,
            "message": (
                f"per-page 生成完成，成功 {len(result.get('page_files', []))} 页，"
                f"缺失 {len(missing)} 页"
            ),
        }


class QAFixNode(PlanNode):
    """P8.2 — 按新版 pptx-craft Stage 6 做完整性检查与官方 fix。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p8_2_qa_fix",
            instruction=(
                "## P8.2 页面完整性检查与基础修复\n"
                "\n"
                "### 前置条件\n"
                "- `bash` 工具可用\n"
                "- `list_dir` / `glob` 工具可用（用于完整性检查）\n"
                "- pages_dir 已存在\n"
                "\n"
                "### 输入\n"
                "- `pages_dir` / `page_count`\n"
                "\n"
                "### 输出\n"
                "- `qa_status`: ok / partial / failed\n"
                "- `final_page_files`: 修复后的最终文件清单\n"
                "- `fix_report`: cli.js fix 输出摘要\n"
                "\n"
                "### 执行流程\n"
                "1. 完整性检查：列 pages_dir 下 page-*.pptx.html，比对数量与 page_count\n"
                "2. 基础修复：node cli.js fix {pages_dir}/ --fix --style {style_file_path}\n"
                "3. Stage 6 到此结束；check-layout 已在 P8.1 对内容页与 agenda 执行（--density lean）\n"
                "\n"
                "### 失败兜底\n"
                "- bash 不可用：跳过 fix，仅做完整性检查\n"
                "- cli.js fix 报错：qa_status = failed，page_files 仍返回\n"
                "- list_dir 不可用：completeness_ok = unknown，不阻塞\n"
            ),
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        pages_dir = str(inputs.get("pages_dir") or "").strip()
        page_count = int(inputs.get("page_count") or 0)
        total_pages = PptCommon.resolve_total_pages(
            page_count=page_count,
            total_pages=inputs.get("total_pages"),
            outline_text=str(inputs.get("outline_text") or ""),
            default_structural_pages=_DEFAULT_STRUCTURAL_PAGES,
        )

        if not pages_dir:
            logger.error("[P8.2] pages_dir 为空")
            return {
                "qa_status": "failed",
                "final_page_files": [],
                "fix_report": "pages_dir empty",
            }

        completeness_ok, page_files = await self._check_completeness(pages_dir, total_pages)
        qa_status = "ok" if completeness_ok else "partial"

        fix_report_parts: list[str] = []
        try:
            pptx_root = str(inputs.get("pptx_root") or "").strip()
            style_file_path = str(inputs.get("style_file_path") or "").strip()
            # 按页并发 fix（1.1.19a+ 支持 --pages 参数）
            page_nums = [int(f.replace("page-", "").replace(".pptx.html", ""))
                         for f in page_files if f.startswith("page-") and f.endswith(".pptx.html")]
            page_nums.sort()
            if page_nums:
                results = await self._fix_pages(
                    page_nums,
                    pages_dir=pages_dir,
                    pptx_root=pptx_root,
                    style_file_path=style_file_path,
                )
                failed_pages = [
                    r[0] for r in results
                    if isinstance(r, tuple) and not r[1]
                ] if results else []
                # 处理异常情况
                exc_pages = [page_nums[i] for i, r in enumerate(results) if isinstance(r, Exception)]
                if exc_pages:
                    logger.error("[P8.2] fix 异常页: %s", exc_pages)
                    qa_status = "partial"
                if failed_pages:
                    logger.warning("[P8.2] fix 失败页: %s", failed_pages)
                    qa_status = "partial"
                else:
                    logger.info("[P8.2] cli.js fix 完成 (per-page 并发 %d 页)", len(page_nums))
                fix_parts = []
                for r in results:
                    if isinstance(r, tuple):
                        pn, ok, _ = r
                    else:
                        pn, ok = 0, False
                    fix_parts.append(f"page-{pn}: {'ok' if ok else 'fail'}")
                fix_report_parts.append("fix=" + ",".join(fix_parts))
            else:
                fix_report_parts.append("fix=no pages")
        except BashExecError as e:
            logger.error("[P8.2] cli.js fix 异常: %s", e)
            qa_status = "failed"
            fix_report_parts.append(f"bash_error: {e}")

        return {
            "qa_status": qa_status,
            "final_page_files": page_files,
            "fix_report": "; ".join(fix_report_parts),
        }

    async def _read_page_file(self, path: str) -> str | None:
        """读取页面文件；超时/异常/工具级失败信封返回 None，区别于「读到空内容」。"""
        if not path or not self.has_tool("read_file"):
            return None
        try:
            result = await asyncio.wait_for(
                self.call_tool("read_file", file_path=path),
                timeout=_P82_READ_TIMEOUT_SECONDS,
            )
            # 工具级失败信封：read_file 对路径错误/文件过大/token 超限等不抛异常，
            # 而是返回 success=False（对象/dict）或 "success=False" 前缀 str。
            # 必须归入 None：parse_tool_file_content 会把信封折叠成 ""，
            # 而 "" 会伪装成「读到空内容」通过 after_html is not None 检查，
            # 触发 backup 误回退，把 fix 成功的页面毁掉。
            if _is_tool_failure_envelope(result):
                logger.warning("[P8.2] 读取页面返回失败信封 path=%s", path)
                return None
            return PptCommon.parse_tool_file_content(result)
        except TimeoutError:
            logger.warning(
                "[P8.2] 读取页面超时 path=%s timeout=%.0fs",
                path,
                _P82_READ_TIMEOUT_SECONDS,
            )
            return None
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.2] 读取页面失败 %s: %s", path, e)
            return None

    async def _write_page_file(self, path: str, content: str) -> bool:
        if not path or not self.has_tool("write_file"):
            return False
        try:
            await self.call_tool("write_file", file_path=path, content=content)
            return True
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.2] 写入页面失败 %s: %s", path, e)
            return False

    async def _find_latest_backup_path(self, pages_dir: str, page_num: int) -> str:
        if not self.has_tool("glob"):
            return ""
        try:
            result = await self.call_tool(
                "glob",
                pattern=f"_backup/*/page-{page_num}.pptx.html",
                path=pages_dir,
            )
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.2] 查找 backup 失败 page=%d: %s", page_num, e)
            return ""
        # 不能用 _parse_listing：它会把结果裁成裸文件名，丢失 _backup/<ts>/ 目录，
        # 直接从原始返回中提取时间戳，重建以 pages_dir 为锚点的完整路径。
        timestamps = re.findall(
            rf"_backup[/\\]+(\d+)[/\\]+page-{page_num}\.pptx\.html",
            str(result),
        )
        if not timestamps:
            return ""
        paths = [
            f"{pages_dir}/_backup/{ts}/page-{page_num}.pptx.html"
            for ts in set(timestamps)
        ]
        return max(paths, key=_extract_backup_timestamp)

    async def _fix_pages(
        self,
        page_nums: list[int],
        *,
        pages_dir: str,
        pptx_root: str,
        style_file_path: str,
    ) -> list[tuple[int, bool, str] | BaseException]:
        """仅对指定页面并发执行新版 pptx-craft fix。"""
        # fix（pptx-craft cli.js）是纯 Node CPU 密集任务：正则/HTML DOM/JS AST
        # 全量重扫，stabilize 模式最多 12 轮收敛。并发过高会吃满宿主机 CPU，
        # Python 服务进程被 OS 饿死（loop lag 6-8s，曾导致 read_file 60s 锁
        # 等待超时）。3 路并发已接近桌面机吞吐上限。
        sem = asyncio.Semaphore(3)

        async def _fix_one_body(page_num: int) -> tuple[int, bool, str]:
            page_path = f"{pages_dir}/page-{page_num}.pptx.html"
            before_html = await self._read_page_file(page_path)
            before_ok = bool(before_html) and _is_slide_exportable(before_html)

            style_arg = (
                f" --style {quote_path(style_file_path)}"
                if style_file_path
                else ""
            )
            cmd = (
                f"{cli_path('fix', pptx_root)} {quote_path(pages_dir + '/')} "
                f"--fix --pages {page_num}{style_arg}"
            )
            result = await run_bash(
                self,
                cmd,
                timeout_seconds=300,
                required=False,
                workdir=pptx_root,
            )
            output = combined_output(result)[:500]
            ok = result.exit_code == 0
            if not ok:
                logger.warning(
                    "[P8.2] page-%d fix 失败 exit=%d",
                    page_num,
                    result.exit_code,
                )

            after_html = await self._read_page_file(page_path)
            after_ok = after_html is not None and _is_slide_exportable(after_html)
            # 读失败（None）时无法判定 DOM 是否被破坏，禁止用 backup 覆盖 ——
            # 否则会把 fix 成功的页面误回退。
            if before_ok and after_html is not None and not after_ok:
                backup_path = await self._find_latest_backup_path(pages_dir, page_num)
                if backup_path:
                    backup_html = await self._read_page_file(backup_path)
                    if backup_html and _is_slide_exportable(backup_html):
                        if await self._write_page_file(page_path, backup_html):
                            logger.warning(
                                "[P8.2] page-%d fix 破坏 DOM，已回退 backup",
                                page_num,
                            )
                            output = f"{output} dom_restored_from_backup"
            return page_num, ok, output

        async def _fix_one(page_num: int) -> tuple[int, bool, str]:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        _fix_one_body(page_num),
                        timeout=_P82_FIX_ONE_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    logger.error(
                        "[P8.2] page-%d fix 整体超时 timeout=%.0fs",
                        page_num,
                        _P82_FIX_ONE_TIMEOUT_SECONDS,
                    )
                    return page_num, False, "fix_one_timeout"
                except AbortError:
                    raise
                except asyncio.CancelledError:
                    raise

        results = await asyncio.gather(
            *[_fix_one(page_num) for page_num in page_nums],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, (AbortError, asyncio.CancelledError)):
                raise result
        return results

    async def _check_completeness(
        self,
        pages_dir: str,
        page_count: int,
    ) -> tuple[bool, list[str]]:
        files: list[str] = []
        logger.debug(
            "[P8.2] _check_completeness start pages_dir=%s page_count=%d has_list_dir=%s has_glob=%s",
            pages_dir,
            page_count,
            self.has_tool("list_dir"),
            self.has_tool("glob"),
        )
        if self.has_tool("list_dir"):
            try:
                result = await self.call_tool("list_dir", path=pages_dir)
                files = self._parse_listing(result)
                logger.debug("[P8.2] list_dir 解析结果 files=%d", len(files))
            except Exception as e:
                if isinstance(e, AbortError):
                    raise
                logger.warning("[P8.2] list_dir 失败，回退 glob: %s", e)
                files = []

        if not files and self.has_tool("glob"):
            try:
                result = await self.call_tool(
                    "glob",
                    pattern="page-*.pptx.html",
                    path=pages_dir,
                )
                files = self._parse_listing(result)
                logger.debug("[P8.2] glob 解析结果 files=%d", len(files))
            except Exception as e:
                if isinstance(e, AbortError):
                    raise
                logger.warning("[P8.2] glob 失败: %s", e)
                files = []

        page_files = sorted(
            {f for f in files if f.startswith("page-") and f.endswith(".pptx.html")}
        )

        if page_count <= 0:
            return bool(page_files), page_files

        completeness_ok = len(page_files) == page_count
        if not completeness_ok:
            logger.warning(
                "[P8.2] 完整性不足 actual=%d expected=%d",
                len(page_files),
                page_count,
            )
        return completeness_ok, page_files

    def _parse_listing(self, result: Any) -> list[str]:
        if result is None:
            return []
        logger.debug(
            "[P8.2] _parse_listing input type=%s repr=%.500s",
            type(result).__name__,
            repr(result),
        )
        if isinstance(result, list):
            return [self._basename(self._extract_path_from_item(x)) for x in result]
        if isinstance(result, dict):
            for key in ("entries", "files", "filenames", "items", "result", "matches", "paths"):
                v = result.get(key)
                if isinstance(v, list):
                    return [self._basename(self._extract_path_from_item(x)) for x in v]
            content = result.get("content")
            if isinstance(content, str):
                return self._parse_listing_text(content)
        if hasattr(result, "data"):
            data = result.data
            if isinstance(data, list):
                return [self._basename(self._extract_path_from_item(x)) for x in data]
            if isinstance(data, dict):
                for key in ("entries", "files", "filenames", "items", "result", "matches", "paths"):
                    v = data.get(key)
                    if isinstance(v, list):
                        return [self._basename(self._extract_path_from_item(x)) for x in v]
                content = data.get("content")
                if isinstance(content, str):
                    return self._parse_listing_text(content)
            if isinstance(data, str):
                return self._parse_listing_text(data)
        if hasattr(result, "model_dump"):
            dumped = result.model_dump(mode="json")
            if isinstance(dumped, dict):
                for key in ("entries", "files", "filenames", "items", "result", "data", "matches", "paths"):
                    v = dumped.get(key)
                    if isinstance(v, list):
                        return [self._basename(self._extract_path_from_item(x)) for x in v]
                    if isinstance(v, dict):
                        for sub_key in ("entries", "files", "filenames", "items", "result", "matches", "paths"):
                            sv = v.get(sub_key)
                            if isinstance(sv, list):
                                return [self._basename(self._extract_path_from_item(x)) for x in sv]
                content = dumped.get("content")
                if isinstance(content, str):
                    return self._parse_listing_text(content)
            if isinstance(dumped, list):
                return [self._basename(self._extract_path_from_item(x)) for x in dumped]
            if isinstance(dumped, str):
                return self._parse_listing_text(dumped)
        if isinstance(result, str):
            return self._parse_listing_text(result)
        logger.warning(
            "[P8.2] _parse_listing 无法解析 result type=%s repr=%.300s",
            type(result).__name__,
            repr(result),
        )
        return []

    @staticmethod
    def _extract_path_from_item(item: Any) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            for key in ("path", "name", "file", "filename", "filepath", "href", "url"):
                v = item.get(key)
                if isinstance(v, str) and v:
                    return v
            for v in item.values():
                if isinstance(v, str) and _looks_like_path(v):
                    return v
        return str(item)

    def _parse_listing_text(self, text: str) -> list[str]:
        return [self._basename(line.strip()) for line in text.splitlines() if line.strip()]

    @staticmethod
    def _basename(path: str) -> str:
        path = path.replace("\\", "/").rstrip("/")
        return path.rsplit("/", 1)[-1] if "/" in path else path

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        result = await self._execute(inputs)
        status_map = {"ok": "ok", "partial": "warning", "failed": "error"}
        yield {
            **result,
            "node": self.plan_name,
            "status": status_map.get(result.get("qa_status", ""), "warning"),
            "message": f"QA 完成 status={result.get('qa_status')}",
        }


def _extract_page_number(filename: str) -> int:
    m = re.search(r"page-(\d+)\.pptx\.html$", filename)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


class PPTPageGenNode(PlanNode):
    """P8 — 幻灯片生成根节点。"""

    def __init__(self) -> None:
        super().__init__(
            plan_name="p8_ppt_page_gen",
            instruction=(
                "## P8 幻灯片生成\n"
                "\n"
                "### 节点职责\n"
                "1. 把 outline.md + research-P{N}.md + 风格文件转成 N 个 page-{N}.pptx.html\n"
                "2. 三阶段串行编排：预处理 → per-page 生成 → 页面完整性检查与官方 fix\n"
                "   - per-page 生成内部 N 页 asyncio.gather 并发，只有生成失败或 HTML 非法时才重试\n"
                "3. 不区分单 Agent 模式，LLM 并发度由框架 semaphore 控制\n"
                "4. `style_mode == template_canvas` 时走模板画布分支：跳过普通三阶段，改由 `_execute_template_pack` 用模板包 + LLM 填充生成页面\n"
                "\n"
                "### 输入\n"
                "- `output_dir`（必填）: 工作目录（含 outline.md / research-P{N}.md）\n"
                "- `pages_dir`（必填）: HTML 输出目录\n"
                "- `style_file_path`（普通分支必填）: P7 落盘的风格文件；`template_canvas` 分支为空，改用 `pack_dir`\n"
                "- `pack_dir`（`template_canvas` 分支必填）: 模板包目录绝对路径\n"
                "- `style_id`（普通分支必填）: 用于预设风格强约束\n"
                "- `page_count`（必填）: 大纲页数 N\n"
                "- `gen_retry_round`（可选，默认 1；含首次生成在内最多尝试 3 次）\n"
                "\n"
                "### 输出\n"
                "```json\n"
                '{\n'
                '  "pages_dir": "...",\n'
                '  "page_files": ["page-1.pptx.html", ...],\n'
                '  "missing_pages": [],\n'
                '  "low_density_pages": [],\n'
                '  "ppt_gen_status": "ok | partial | failed"\n'
                '}\n'
                "```\n"
                "\n"
                "### 执行流程\n"
                "1. 输入校验：必填字段任一空 → failed\n"
                "2. `style_mode == template_canvas` 时走 `_execute_template_pack`：用模板包 + LLM 填充生成页面，不进入普通三阶段\n"
                "3. 调用 P8.0 PrepareNode → 读资料 + 按页拆分，产出共享只读数据；prepare_status=failed → 直接 failed\n"
                "4. 调用 P8.1 PageWorkerNode → per-page 并发生成与 HTML 合法性校验"
                "→ page_files / missing_pages / low_density_pages\n"
                "5. 调用 P8.2 QAFixNode → 完整性检查 + 官方 fix，产出 qa_status / final_page_files / fix_report\n"
                "6. 汇总状态：missing 空 + low 空 + qa=ok → ok；qa=failed → failed；其余 partial\n"
                "\n"
                "### 失败兜底\n"
                "- 必填校验失败：直接返回 failed，不进入子节点\n"
                "- `template_canvas` 分支 `pack_dir` 为空或 `page_count` 非法：直接返回 failed\n"
                "- P8.0 prepare_status=failed：直接返回 failed，不进入 P8.1\n"
                "- 子节点透传错误，根节点不阻塞，按汇总规则归并状态\n"
            ),
            sub_plans=[
                PrepareNode(),
                PageWorkerNode(),
                QAFixNode(),
            ],
        )

    async def _execute(self, inputs: dict[str, Any]) -> dict[str, Any]:
        # 模板画布分支优先判断：style_mode == template_canvas 时走模板画布流程
        style_mode = str(inputs.get("style_mode") or "").strip()
        if style_mode == "template_canvas":
            # template_canvas 分支只需要 pack_dir，不需要 style_file_path
            pack_dir = str(inputs.get("pack_dir") or "").strip()
            if not pack_dir:
                logger.error("[P8] template_canvas 分支必填字段 pack_dir 为空")
                return {
                    "pages_dir": str(inputs.get("pages_dir") or ""),
                    "page_files": [],
                    "missing_pages": [],
                    "low_density_pages": [],
                    "ppt_gen_status": "failed",
                }
            page_count = int(inputs.get("page_count") or 0)
            if page_count <= 0:
                logger.error("[P8] page_count 非法 (%s)", inputs.get("page_count"))
                return {
                    "pages_dir": str(inputs.get("pages_dir") or ""),
                    "page_files": [],
                    "missing_pages": [],
                    "low_density_pages": [],
                    "ppt_gen_status": "failed",
                }
            total_pages = PptCommon.resolve_total_pages(
                page_count=page_count,
                total_pages=inputs.get("total_pages"),
                default_structural_pages=_DEFAULT_STRUCTURAL_PAGES,
            )
            return await self._execute_template_pack(inputs, page_count, total_pages)

        # 非 template_canvas 分支：需要 style_file_path 等字段
        required_fields = (
            "output_dir",
            "pages_dir",
            "style_file_path",
            "style_id",
        )
        for field in required_fields:
            if not str(inputs.get(field) or "").strip():
                logger.error("[P8] 必填字段 %s 为空，无法生成幻灯片", field)
                return {
                    "pages_dir": str(inputs.get("pages_dir") or ""),
                    "page_files": [],
                    "missing_pages": [],
                    "low_density_pages": [],
                    "ppt_gen_status": "failed",
                }

        page_count = int(inputs.get("page_count") or 0)
        if page_count <= 0:
            logger.error("[P8] page_count 非法 (%s)", inputs.get("page_count"))
            return {
                "pages_dir": str(inputs.get("pages_dir") or ""),
                "page_files": [],
                "missing_pages": [],
                "low_density_pages": [],
                "ppt_gen_status": "failed",
            }

        total_pages = PptCommon.resolve_total_pages(
            page_count=page_count,
            total_pages=inputs.get("total_pages"),
            default_structural_pages=_DEFAULT_STRUCTURAL_PAGES,
        )
        inputs = {**inputs, "total_pages": total_pages}

        prep_result = await self.execute_subplan(self.sub_plans[0], inputs)
        if not isinstance(prep_result, dict) or prep_result.get("prepare_status") != "ok":
            logger.error("[P8] P8.0 预处理失败，终止生成")
            return {
                "pages_dir": str(inputs.get("pages_dir") or ""),
                "page_files": [],
                "missing_pages": list(range(1, total_pages + 1)),
                "low_density_pages": [],
                "ppt_gen_status": "failed",
            }

        worker_inputs = {**inputs, **prep_result}
        worker_result = await self.execute_subplan(self.sub_plans[1], worker_inputs)
        qa_inputs = {**worker_inputs, **worker_result} if isinstance(worker_result, dict) else worker_inputs
        missing_pages = (
            list(worker_result.get("missing_pages") or [])
            if isinstance(worker_result, dict)
            else []
        )
        low_density_pages = (
            list(worker_result.get("low_density_pages") or [])
            if isinstance(worker_result, dict)
            else []
        )
        page_files = (
            list(worker_result.get("page_files") or [])
            if isinstance(worker_result, dict)
            else []
        )

        qa_result = await self.execute_subplan(self.sub_plans[2], qa_inputs)
        qa_status = "ok"
        final_page_files = page_files
        fix_report = ""
        if isinstance(qa_result, dict):
            qa_status = str(qa_result.get("qa_status") or "ok")
            final_page_files = list(qa_result.get("final_page_files") or page_files)
            fix_report = str(qa_result.get("fix_report") or "")

        if missing_pages:
            outline_pages_for_clean = worker_inputs.get("outline_pages") or {}
            final_file_set = set(final_page_files)

            def _is_structural_on_disk(page_num: int) -> bool:
                if f"page-{page_num}.pptx.html" not in final_file_set:
                    return False
                page_type = _detect_page_type(
                    str(outline_pages_for_clean.get(page_num) or "")
                )
                return page_type in _STRUCTURAL_TEMPLATE_PAGE_TYPES

            missing_pages = [p for p in missing_pages if not _is_structural_on_disk(p)]

        if qa_status == "failed":
            ppt_gen_status = "failed"
        elif missing_pages or low_density_pages or qa_status == "partial":
            ppt_gen_status = "partial"
        else:
            ppt_gen_status = "ok"

        logger.info(
            "[P8] 完成 status=%s page=%d missing=%d low_density=%d",
            ppt_gen_status,
            len(final_page_files),
            len(missing_pages),
            len(low_density_pages),
        )

        return {
            "pages_dir": str(inputs.get("pages_dir") or ""),
            "page_files": final_page_files,
            "missing_pages": missing_pages,
            "low_density_pages": low_density_pages,
            "fix_report": fix_report,
            "ppt_gen_status": ppt_gen_status,
            "__artifact__": {
                "info": {
                    "ppt_gen_status": ppt_gen_status,
                    "total_pages": len(final_page_files),
                    "missing_count": len(missing_pages),
                },
                "files": [{"path": f, "desc": "PPT页面"} for f in final_page_files] if final_page_files else [],
            },
        }

    async def _execute_template_pack(
        self,
        inputs: dict[str, Any],
        page_count: int,
        total_pages: int,
    ) -> dict[str, Any]:
        """模板包分支：调用 template-filler 脚本 + LLM 填充生成页面。

        流程：preflight → 逐页(seed → LLM 填充) → check
        """
        import json as _json

        pack_dir = str(inputs.get("pack_dir") or "").strip()
        output_dir = str(inputs.get("output_dir") or "").strip()
        pages_dir = str(inputs.get("pages_dir") or "").strip()
        pptx_root = str(inputs.get("pptx_root") or "").strip()

        if not pack_dir or not pages_dir:
            logger.error("[P8-TP] pack_dir 或 pages_dir 为空")
            return {
                "pages_dir": pages_dir,
                "page_files": [],
                "missing_pages": list(range(1, total_pages + 1)),
                "low_density_pages": [],
                "ppt_gen_status": "failed",
            }

        # 1. preflight 预检
        try:
            preflight_cmd = (
                f"{cli_path('preflight', pptx_root)} "
                f"{quote_path(pack_dir)} {quote_path(output_dir)} {quote_path(pages_dir)}"
            )
            await run_bash(
                self, preflight_cmd,
                timeout_seconds=60, required=True, workdir=pptx_root,
            )
            logger.info("[P8-TP] preflight 通过")
        except BashExecError as e:
            logger.error("[P8-TP] preflight 失败: %s", e)
            return {
                "pages_dir": pages_dir,
                "page_files": [],
                "missing_pages": list(range(1, total_pages + 1)),
                "low_density_pages": [],
                "ppt_gen_status": "failed",
            }

        # 2. 读取 outline.md 并按页拆分
        outline_text = await self._read_file(f"{output_dir}/outline.md")
        if not outline_text:
            logger.error("[P8-TP] outline.md 读取失败")
            return {
                "pages_dir": pages_dir,
                "page_files": [],
                "missing_pages": list(range(1, total_pages + 1)),
                "low_density_pages": [],
                "ppt_gen_status": "failed",
            }
        outline_pages = _split_md_pages(outline_text)

        # 3. 读取 template-manifest.json 获取模板列表
        manifest = await self._load_template_manifest(pack_dir, pptx_root)

        # 4. 逐页 seed + LLM 填充（并发）
        all_pages = list(range(1, total_pages + 1))
        tasks = [
            self._template_fill_one(
                page_num=p,
                pack_dir=pack_dir,
                pages_dir=pages_dir,
                pptx_root=pptx_root,
                outline_page=outline_pages.get(p, ""),
                output_dir=output_dir,
                manifest=manifest,
            )
            for p in all_pages
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        missing_pages: list[int] = []
        page_files: list[str] = []
        for p, r in zip(all_pages, results):
            if isinstance(r, Exception):
                logger.warning("[P8-TP] 页面 %d 填充异常: %s", p, r)
                missing_pages.append(p)
                continue
            if r:
                page_files.append(f"page-{p}.pptx.html")
            else:
                missing_pages.append(p)

        # 5. check 自检 + 失败恢复循环（最多 2 轮）
        check_ok = False
        for retry_round in range(2):
            check_ok = True
            try:
                check_cmd = (
                f"{cli_path('check', pptx_root)} "
                f"{quote_path(pack_dir)} {quote_path(pages_dir)}"
            )
                check_result = await run_bash(
                    self, check_cmd,
                    timeout_seconds=300, required=False, workdir=pptx_root,
                )
                if check_result.exit_code != 0:
                    check_ok = False
                    check_output = check_result.stdout + "\n" + check_result.stderr
                    logger.warning("[P8-TP] fill.js check 第 %d 轮失败 exit=%d", retry_round + 1, check_result.exit_code)

                    # 解析失败的页码（跳过 manifest 声明类误报，re-seed 无法修复）
                    # check 输出格式：page 行（含 page-N）后跟 HARD/WARN 行（不含 page-N），
                    # 需用"当前页面"追踪方式把 HARD 行关联到最近的 page 行
                    failed_pages: list[int] = []
                    manifest_decl_pages: set[int] = set()
                    current_page: int | None = None
                    for line in check_output.splitlines():
                        m = re.search(r'page-(\d+)', line)
                        if m:
                            current_page = int(m.group(1))
                        if "HARD" not in line.upper():
                            continue
                        if current_page is None:
                            continue
                        p = current_page
                        # "template-id 未在 manifest 中声明" 是 manifest 声明问题，
                        # re-seed 不会修复（换模板也可能不在 manifest 中），跳过
                        if "manifest" in line.lower() and "声明" in line:
                            manifest_decl_pages.add(p)
                            continue
                        if p not in failed_pages:
                            failed_pages.append(p)

                    # 只报 manifest 声明问题的页不算失败
                    manifest_only = manifest_decl_pages - set(failed_pages)
                    if manifest_only:
                        logger.info(
                            "[P8-TP] 页面 %s 仅有 manifest 声明类警告，跳过 re-seed",
                            sorted(manifest_only),
                        )

                    if not failed_pages:
                        if manifest_decl_pages:
                            # 所有 HARD 错误都是 manifest 声明类，内容本身没问题
                            logger.info("[P8-TP] check 仅剩 manifest 声明类警告，视为通过")
                            check_ok = True
                            break
                        # 无法解析失败页，取所有已生成页重试
                        failed_pages = [p for p in all_pages if f"page-{p}.pptx.html" in page_files]

                    if not failed_pages:
                        logger.error("[P8-TP] check 失败但无法定位失败页，放弃重试")
                        break

                    logger.info("[P8-TP] 第 %d 轮恢复：重新 seed+填充 %d 个失败页 %s",
                                retry_round + 1, len(failed_pages), failed_pages)

                    # 重新 seed + 填充失败页（用 content-base 兜底模板）
                    retry_tasks = [
                        self._template_fill_one(
                            page_num=p,
                            pack_dir=pack_dir,
                            pages_dir=pages_dir,
                            pptx_root=pptx_root,
                            outline_page=outline_pages.get(p, ""),
                            output_dir=output_dir,
                            manifest=manifest,
                            force_template_id="content-base",
                        )
                        for p in failed_pages
                    ]
                    retry_results = await asyncio.gather(
                        *retry_tasks, return_exceptions=True
                    )
                    for p, r in zip(failed_pages, retry_results):
                        if isinstance(r, Exception) or not r:
                            logger.warning("[P8-TP] 页面 %d 恢复失败", p)
                        else:
                            logger.info("[P8-TP] 页面 %d 恢复成功", p)
                else:
                    logger.info("[P8-TP] fill.js check 第 %d 轮通过", retry_round + 1)
                    break
            except BashExecError as e:
                logger.error("[P8-TP] fill.js check 异常: %s", e)
                check_ok = False
                break

        # agenda 条目数校验：读取生成的 agenda 页 HTML，比对大纲内容章节数
        agenda_page_num = _find_agenda_page_num(outline_text)
        if agenda_page_num and agenda_page_num not in missing_pages:
            agenda_path = f"{pages_dir}/page-{agenda_page_num}.pptx.html"
            agenda_html = await self._read_file(agenda_path)
            if agenda_html:
                content_chapters = _count_outline_content_chapters(outline_text)
                item_count = _count_agenda_items(agenda_html)
                if content_chapters > 0 and item_count != content_chapters:
                    logger.warning(
                        "[P8-TP] agenda 条目数(%d) ≠ 大纲内容章节数(%d) page=%d，转 missing 走补写",
                        item_count, content_chapters, agenda_page_num,
                    )
                    missing_pages.append(agenda_page_num)

        ppt_gen_status = "ok"
        if missing_pages:
            ppt_gen_status = "partial"
        if not check_ok:
            ppt_gen_status = "partial" if page_files else "failed"

        logger.info(
            "[P8-TP] 模板填充完成 status=%s success=%d/%d",
            ppt_gen_status, len(page_files), total_pages,
        )

        return {
            "pages_dir": pages_dir,
            "page_files": page_files,
            "missing_pages": missing_pages,
            "low_density_pages": [],
            "fix_report": "template-filler check " + ("passed" if check_ok else "failed"),
            "ppt_gen_status": ppt_gen_status,
            "__artifact__": {
                "info": {
                    "ppt_gen_status": ppt_gen_status,
                    "total_pages": len(page_files),
                    "missing_count": len(missing_pages),
                },
                "files": [{"path": f, "desc": "PPT页面"} for f in page_files] if page_files else [],
            },
        }

    async def _read_file(self, path: str) -> str:
        """读取文件内容（PPTPageGenNode 自身用，模板分支）。"""
        if not path:
            return ""
        if not self.has_tool("read_file"):
            logger.warning("[P8-TP] read_file 工具不可用 %s", path)
            return ""
        try:
            result = await self.call_tool("read_file", file_path=path)
            content = PptCommon.parse_tool_file_content(result)
            return content
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8-TP] 读取文件失败 %s: %s", path, e)
            return ""

    async def _write_file(self, path: str, content: str) -> bool:
        """写入文件内容（PPTPageGenNode 自身用，模板分支）。"""
        if not self.has_tool("write_file"):
            logger.error("[P8-TP] write_file 工具不可用 %s", path)
            return False
        try:
            await self.call_tool("write_file", file_path=path, content=content)
            # 让出事件循环，保证 WebSocket 协议 ping/pong 能被调度。
            await asyncio.sleep(0)
            return True
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.error("[P8-TP] 写入文件失败 %s: %s", path, e)
            return False

    async def _load_template_manifest(
        self, pack_dir: str, pptx_root: str,
    ) -> dict[str, Any]:
        """读取模板包的 template-manifest.json。"""
        manifest_path = f"{pack_dir}/template-manifest.json"
        content = await self._read_file(manifest_path)
        if not content:
            logger.warning("[P8-TP] template-manifest.json 不存在或为空")
            return {}
        try:
            import json as _json
            return _json.loads(content)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8-TP] template-manifest.json 解析失败: %s", e)
            return {}

    @staticmethod
    def _select_template_id(
        page_type: str,
        manifest: dict[str, Any],
        outline_page: str = "",
        research_page: str = "",
    ) -> str:
        """根据页面类型 + 内容形状选择模板 ID（容量感知）。

        原版 skill 让 LLM 根据内容形状选模板，这里用 Python 做内容形状检测：
        - 结构页（cover/agenda/chapter/conclusion）→ 固定映射
        - 内容页：分析 research/outline 中的并列项数、对比模式、数据量
          - 2 项对比 → content-two-column
          - 3-5 并列 → content-cards
          - 单主题 → content-default
          - 超容量（>5 项或大量数据）→ content-base（自由排版）
        """
        # 构建 manifest 中已声明的 template_id 集合（layouts + bases）
        valid_template_ids: set[str] = set()
        for layout in (manifest.get("layouts") or []):
            if isinstance(layout, dict):
                tid = layout.get("template_id") or ""
                if tid:
                    valid_template_ids.add(tid)
        for base in (manifest.get("bases") or []):
            if isinstance(base, dict):
                tid = base.get("template_id") or base.get("page_role") or ""
                if tid:
                    valid_template_ids.add(tid)

        def _find_in_manifest(intent: str, fallback_id: str) -> str:
            """从 manifest.page_intents 查找模板，找不到则用 fallback。

            如果 page_intents 指向的 template 不在 layouts/bases 已声明集合中，
            尝试按 page_role 找一个已声明的 layout 替代。
            """
            page_intents = manifest.get("page_intents") or []
            selected = ""
            selected_file = ""
            if isinstance(page_intents, list):
                for entry in page_intents:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("intent") == intent:
                        selected = entry.get("template") or ""
                        selected_file = entry.get("file") or ""
                        break
            if not selected:
                return fallback_id

            # 已在 manifest 声明集合中，直接返回
            if selected in valid_template_ids:
                return selected

            # 不在已声明集合中（base 模板未声明 template_id），
            # 尝试按 page_role 找一个已声明的 layout 替代
            base_page_role = ""
            for base in (manifest.get("bases") or []):
                if isinstance(base, dict) and base.get("file") == selected_file:
                    base_page_role = base.get("page_role") or ""
                    break

            if base_page_role:
                for layout in (manifest.get("layouts") or []):
                    if isinstance(layout, dict) and layout.get("page_role") == base_page_role:
                        alt_tid = layout.get("template_id") or ""
                        if alt_tid and alt_tid in valid_template_ids:
                            logger.info(
                                "[P8-TP] 模板 %s 未在 manifest 声明，按 page_role=%s 替换为 %s",
                                selected, base_page_role, alt_tid,
                            )
                            return alt_tid

            # 找不到替代，保留原模板（fill.js seed 仍可使用，check 会报 manifest 声明警告）
            logger.debug("[P8-TP] 模板 %s 未在 manifest layouts 中声明，保留使用", selected)
            return selected

        # 结构页：固定映射，不走内容感知
        if page_type in _TEMPLATE_STRUCTURAL_TYPES:
            type_to_intent = {
                "intro": "cover", "cover": "cover",
                "agenda": "toc",
                "chapter": "section", "section": "section",
                "conclusion": "closing", "ending": "closing",
            }
            target_intent = type_to_intent.get(page_type, "section")
            return _find_in_manifest(
                target_intent,
                _PAGE_TYPE_TO_TEMPLATE.get(page_type, "section-base"),
            )

        # 内容页：内容形状检测
        content_text = (research_page or "") + "\n" + (outline_page or "")

        # 检测对比模式
        comparison_patterns = [" vs ", "对比", "相较", " versus ", "compared to", " VS "]
        is_comparison = any(p in content_text for p in comparison_patterns)

        # 统计并列项数
        bullet_count = 0
        for line in content_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- ") or stripped.startswith("• ") or stripped.startswith("* "):
                bullet_count += 1
            elif stripped and stripped[0].isdigit() and "." in stripped[:3]:
                bullet_count += 1

        # 检测数据密度
        numbers = re.findall(r'\d+\.?\d*%?', content_text)
        data_density = len(numbers)

        # 内容形状 → 模板选择
        if is_comparison and bullet_count >= 2:
            shape = "comparison"
        elif bullet_count >= 6 or data_density >= 15:
            shape = "overflow"
        elif 3 <= bullet_count <= 5:
            shape = "cards"
        else:
            shape = "single"

        shape_to_intent = {
            "comparison": "comparison",
            "overflow": "general",
            "cards": "image_text",
            "single": "general",
        }
        shape_to_fallback = {
            "comparison": "content-two-column",
            "overflow": "content-base",
            "cards": "content-cards",
            "single": "content-default",
        }
        target_intent = shape_to_intent.get(shape, "general")
        fallback_id = shape_to_fallback.get(shape, "content-default")
        return _find_in_manifest(target_intent, fallback_id)

    async def _template_fill_one(
        self,
        *,
        page_num: int,
        pack_dir: str,
        pages_dir: str,
        pptx_root: str,
        outline_page: str,
        output_dir: str,
        manifest: dict[str, Any],
        force_template_id: str = "",
    ) -> bool:
        """单页模板填充：seed → LLM 填充 → write_file。返回是否成功。"""
        # 检测页面类型
        page_type = _detect_page_type(outline_page)

        # 3. 读取本页 research（提前读取，供选模板和填充使用）
        research_path = f"{output_dir}/research-P{page_num}.md"
        research_text = await self._read_file(research_path)

        # 容量感知选模板：根据内容形状选模板（或使用强制兜底模板）
        if force_template_id:
            template_id = force_template_id
        else:
            template_id = self._select_template_id(
                page_type, manifest,
                outline_page=outline_page,
                research_page=research_text,
            )

        # 1. seed 种子化
        page_path = f"{pages_dir}/page-{page_num}.pptx.html"
        try:
            seed_cmd = (
                f"{cli_path('seed', pptx_root)} "
                f"{quote_path(pack_dir)} {template_id} {quote_path(page_path)} copy"
            )
            await run_bash(
                self, seed_cmd,
                timeout_seconds=60, required=True, workdir=pptx_root,
            )
        except BashExecError as e:
            logger.error("[P8-TP] 页面 %d seed 失败 template=%s: %s", page_num, template_id, e)
            return False
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.error("[P8-TP] 页面 %d seed 异常: %s", page_num, e)
            return False

        # 2. 读取种子 HTML
        seed_html = await self._read_file(page_path)
        if not seed_html:
            logger.error("[P8-TP] 页面 %d 种子 HTML 读取失败", page_num)
            return False

        # 4. LLM 填充内容
        is_structural = page_type in _TEMPLATE_STRUCTURAL_TYPES
        filled_html = await self._llm_fill_template(
            page_num=page_num,
            seed_html=seed_html,
            outline_page=outline_page,
            research_page=research_text,
            is_structural=is_structural,
        )
        if not filled_html:
            logger.error("[P8-TP] 页面 %d LLM 填充失败", page_num)
            return False

        # 5. 写入填充后的 HTML
        ok = await self._write_file(page_path, filled_html)
        if not ok:
            logger.error("[P8-TP] 页面 %d 写入失败", page_num)
            return False

        logger.info("[P8-TP] 页面 %d 填充完成 template=%s", page_num, template_id)
        return True

    async def _llm_fill_template(
        self,
        *,
        page_num: int,
        seed_html: str,
        outline_page: str,
        research_page: str,
        is_structural: bool,
    ) -> str:
        """调用 LLM 填充模板 HTML 中的 data-slot 占位文字。"""
        research_section = ""
        if research_page and not is_structural:
            research_section = f"\n### 本页研究素材（research-P{page_num}.md）\n{research_page}\n"
        elif is_structural:
            research_section = "\n（结构页，无需研究素材，仅依据大纲内容填充标题/副标题）\n"

        prompt = (
            f"你是 PPT 模板填充师。请将以下模板种子 HTML 中的 data-slot 占位文字替换为真实内容，并做顺势增强。\n\n"
            f"### 大纲 — 本页规划\n{outline_page}\n"
            f"{research_section}\n"
            "### 填充规则（必须遵守）\n"
            "1. **保住 DNA**：不动 :root CSS 变量（配色/字号）、--font-*、.template-bg-image、.content-layer 骨架\n"
            "2. **守容量**：文字长度/行数照模板该槽位的容量约束（max_chars/max_lines）\n"
            "3. **安全写入**：内容中的 &、<、>、引号等特殊字符不得破坏 HTML 标签结构\n"
            "4. **禁止改模板结构**：不得改 grid-template-columns 列数、不得删 overflow:hidden、不得降字号到 14px 以下\n"
            "5. **所有文字必须是真实内容**，禁止占位文本（TODO、xxx 等）\n"
            "6. **结构性页面必须填标题**：封面/章节/结尾页的 data-slot=\"title\" 必须写入大纲给出的标题\n"
            "\n"
            "### 顺势增强（内容页必做，结构页跳过）\n"
            "在填满 data-slot 后，如果内容区仍有留白，按以下顺序增强（仅用 :root 变量和模板已有 CSS class）：\n"
            "1. **从 research 多挖细节**填进现有槽位（研究通常比框能装的多——挖，不编造）\n"
            "2. **加总结框**：主色左边框的小框，标题「关键洞察/核心总结」，对已有要点 1-2 句概括重述（非新事实），≤2 行\n"
            "3. **内容→视觉转换**（轻量、纯 HTML/CSS、守 DNA）：\n"
            "   - 一组数字/KPI → 大数字卡 / KPI 横条\n"
            "   - 并列要点 → 图标 + 文字列表\n"
            "   - 结论/金句 → 引用块 blockquote\n"
            "   - 对比 → 左右对照卡\n"
            "   - 流程/时序 → CSS 节点+连线 / 时间线\n"
            "4. 仍空 → 把该页内容区用足，确保内容分散占据 .content-layer 高度（不要全堆到上半屏）\n"
            "\n"
            "### 超容量决策树（内容超出模板槽位时，按序处理）\n"
            "1. 精简措辞压进槽位 → 2. 只保留核心要点，次要内容移到注释 → 3. 实在塞不下就少填，不要硬塞导致溢出\n"
            "禁止：改 grid-template-columns 列数、调高 -webkit-line-clamp、删 overflow:hidden、降字号到 14px 以下\n"
            "\n"
            "### 内容页留白契约\n"
            "- 内容必须分散占据 .content-layer 高度，不要全堆到顶部 30%\n"
            "- 正文最小字号 14px；来源/注脚允许 12px 但必须用含 note/source/footnote 的 class\n"
            "- 至少 8% 的垂直缓冲\n"
            "\n"
            "直接输出完整的填充后 HTML，不要输出解释或代码块包裹。\n\n"
            f"### 模板种子 HTML\n{seed_html}"
        )
        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt="你是 PPT 模板填充师，直接输出填充后的完整 HTML 原文，不输出任何解释。",
                node_name=f"p8_tp_page_{page_num}",
                concurrent=True,
            )
            html = _strip_html_fence(result or "")
            if not html or len(html) < 200:
                logger.warning("[P8-TP] 页面 %d LLM 填充产物过短", page_num)
                return ""
            return html
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8-TP] 页面 %d LLM 填充失败: %s", page_num, e)
            return ""

    async def _execute_stream(self, inputs: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        result = await self._execute(inputs)
        status_map = {"ok": "ok", "partial": "warning", "failed": "error"}
        page_files = result.get("page_files") or []
        missing = result.get("missing_pages") or []
        status = result.get("ppt_gen_status", "")
        message = f"PPT 页面生成 status={status} 成功 {len(page_files)} 页"
        if missing:
            message += f"，缺失 {len(missing)} 页（不可按成功交付）"
        yield {
            **result,
            "node": self.plan_name,
            "status": status_map.get(status, "warning"),
            "message": message,
        }
