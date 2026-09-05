from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from jiuwenclaw.agentserver.skill_turbo.plan_node import AbortError, PlanNode
from jiuwenclaw.agentserver.skill_turbo.skill_codes.ppt.ppt_common import PptCommon
from jiuwenclaw.agentserver.skill_turbo.skill_codes.ppt.utils.bash_utils import (
    BashExecError,
    cli_path,
    combined_output,
    quote_path,
    run_bash,
)

logger = logging.getLogger(__name__)


_CHART_CANDIDATE_TYPES = {"data", "comparison", "technology", "trend"}


def _extract_designer_section(
    text: str,
    *,
    include_charts: bool = False,
    for_content_template_fill: bool = False,
    for_custom_content_fill: bool = False,
) -> str:
    """从新版 references/designer.md 提取当前生成链路需要的关键章节。

    文件 IO 由 PrepareNode 通过 read_file 工具完成后传入 text，
    skill_code 中禁止直接做文件 IO（校验器禁止 open/read_text 等）。
    """
    if not text:
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

    # custom 内容页只注入官方要求的预算契约与按需图表章，
    # 不注入「页面布局规范」里 section/chapter 的 PART 示例，避免内容页误用。
    if for_custom_content_fill:
        sections = [
            _extract_bounded_section(
                "### 页面内容预算契约",
                ("\n### 阶段 4：交付",),
            ),
        ]
    else:
        sections = [
            _extract_bounded_section(
                "### 页面内容预算契约",
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
            _extract_bounded_section(
                "## 关键原则",
                ("\n## 质量控制清单",),
            ),
        ]
    if include_charts:
        chart_section = _extract_bounded_section(
            "## 图表与数据可视化",
            ("\n### 激活 content-template", "\n## 图片使用规范"),
        )
        if chart_section and not for_content_template_fill:
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

    if for_content_template_fill or for_custom_content_fill:
        return "\n\n".join(selected)

    return (
        "兼容说明：以下 designer 规范中的 Grid 示例在本链路必须用等价 Flex 权重实现；"
        "不得违反当前提示词的 CSS Grid 禁令，但页面预算、纵向占用率、逐列验收、"
        "真实语义内容和图表规则保持不变。\n\n"
        + "\n\n".join(selected)
    )


_PRESET_STYLE_IDS = {"business-classic", "tech-minimal", "elegant-narrative", "industrial-tech"}
# Stage 6 §3.5/§3.6：预设四风格 + custom 的结构页走官方模板预铺填槽，禁止整页自由重写。
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
_FOOTER_BLOCK_RE = re.compile(
    r'<div class="[^"]*\bflex-shrink-0\b[^"]*"[^>]*>\s*<p\b[^>]*>.*?</p>\s*</div>',
    re.IGNORECASE | re.DOTALL,
)
_P_INNER_TEXT_RE = re.compile(r"(<p\b[^>]*>)(.*?)(</p>)", re.IGNORECASE | re.DOTALL)
_CSS_FENCE_RE = re.compile(r"```css\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_THEME_CONTRACT_STYLE_RE = re.compile(
    r'<style\b[^>]*id=["\']theme-contract["\'][^>]*>.*?</style>',
    re.IGNORECASE | re.DOTALL,
)
_THEME_RULES_STYLE_RE = re.compile(
    r'<style\b[^>]*id=["\']theme-rules["\'][^>]*>.*?</style>',
    re.IGNORECASE | re.DOTALL,
)
_CSS_CUSTOM_PROP_RE = re.compile(r"(--[A-Za-z0-9-]+)\s*:\s*([^;]+);")
_SLOT_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CHART_SCAFFOLD_BLOCK_RE = re.compile(
    r"<!--\s*CHART_SCAFFOLD((?:_\d+)?)_BEGIN\b(.*?)CHART_SCAFFOLD\1_END\s*-->",
    re.DOTALL,
)
_OPTION_NULL_RE = re.compile(r"const\s+option\s*=\s*null\s*;")
_CHART_FONT_FAMILY_CONST_RE = re.compile(
    r'(const\s+CHART_FONT_FAMILY\s*=\s*)(["\'])(.*?)\2'
)
_ECHARTS_INIT_RE = re.compile(r"echarts\s*\.\s*init\s*\(", re.IGNORECASE)
_OFFICIAL_FORMATTER_STRING_RE = re.compile(
    r'("formatter"\s*:\s*)"(format(?:AxisNumber|AxisPercent|LabelNumber|LabelPercent))"'
)
_FRONTMATTER_FONT_LIST_RE = re.compile(
    r"(?m)^font-family:\s*\n((?:^[ \t]+-[ \t].+\n?)+)"
)
_FRONTMATTER_FONT_LINE_RE = re.compile(
    r'(?m)^font-family:\s*["\']?(.+?)["\']?\s*$'
)
_HTML_DOCUMENT_RE = re.compile(
    r"^\s*(?:<!--.*?-->\s*)*(?:<!DOCTYPE\s+html|<html[\s>])",
    re.IGNORECASE | re.DOTALL,
)
_SLIDE_DESIGNER_THINKING_OFF = (
    "本任务使用 `off` 思考模式：优先速度，不过度展开推导。"
    "直接给出填槽 JSON，禁止复述模板 HTML 或逐步论证骨架。\n"
)


def _normalize_template_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _outline_needs_research(outline_page: str) -> bool:
    return "✅" in outline_page and (
        "页研究查询" in outline_page
        or "数据需求" in outline_page
        or "研究需求" in outline_page
    )


def _uses_content_template_fill(style_id: str, page_type: str, outline_page: str) -> bool:
    """普通分支内容页：官方 content-template 预铺后仅填槽。

    预设四风格走 Stage 6 §3.5；custom 走 Stage 6 §3.6。结构页不走此路径。
    """
    if style_id not in _PRESET_STYLE_IDS and style_id != "custom":
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
                "5. **条目数 ≠ 默认 4**：仅允许按模板注释增删同构条目槽位并同步编号；"
                "不得改其他结构或发明新布局\n"
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
        f"{_VISIBLE_PAGE_NUMBER_RULE}"
        "## 风格文件（配色/字体权威；不得把风格元数据写成观众可见装饰）\n"
        f"{style_text}\n\n"
        f"### 大纲 — 本页规划（{page_type_label}）\n"
        f"{outline_page}\n\n"
        f"{outline_full_section}"
        "### 预铺模板 HTML（只填槽，勿重写）\n"
        f"{seed_html}\n"
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
    return matches[-1].group(0)


def _normalize_footer_text_only(html: str) -> str:
    footer_block = _extract_footer_block(html)
    if not footer_block:
        return ""
    return _P_INNER_TEXT_RE.sub(r"\1__PAGE_FOOTER__\3", footer_block, count=1)


def _has_placeholder_slop(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    return normalized in _PLACEHOLDER_SLOP_VALUES


def _normalize_slot_name(key: Any) -> str:
    name = str(key or "").strip().strip("{}").strip()
    return name if _SLOT_NAME_RE.fullmatch(name) else ""


def _looks_like_html_document(text: str) -> bool:
    return bool(_HTML_DOCUMENT_RE.match(text or ""))


def _parse_fill_slots(raw: str) -> dict[str, Any] | None:
    """解析填槽 JSON。官方占位符为 {{NAME}}；完整 HTML 文档不走此路径。"""
    payload = PptCommon.parse_json_payload(raw)
    if not isinstance(payload, dict) or not payload:
        return None
    slots: dict[str, Any] = {}
    for key, value in payload.items():
        name = _normalize_slot_name(key)
        if name:
            slots[name] = value
    return slots or None


def _slot_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _apply_template_slots(seed_html: str, slots: dict[str, Any]) -> str:
    """把 JSON 槽值写入预铺模板，不改占位符以外的骨架。"""
    html = seed_html or ""
    for name, value in slots.items():
        if name.startswith("CHART_"):
            continue
        html = html.replace("{{" + name + "}}", _slot_text(value))
    return html


def _revive_official_formatter_refs(js_literal: str) -> str:
    """把 JSON 里的官方 formatter 字符串还原为骨架内置函数引用。"""
    return _OFFICIAL_FORMATTER_STRING_RE.sub(r"\1\2", js_literal)


def _style_frontmatter_font_stack(style_text: str) -> str:
    """读取 style.md / style-custom.md frontmatter 的完整字体栈。"""
    text = style_text or ""
    list_match = _FRONTMATTER_FONT_LIST_RE.search(text)
    if list_match:
        fonts = [
            item.strip().strip("\"'")
            for item in re.findall(r"^[ \t]+-[ \t]+(.+)$", list_match.group(1), re.M)
            if item.strip()
        ]
        return ", ".join(fonts)
    line_match = _FRONTMATTER_FONT_LINE_RE.search(text)
    if line_match:
        return line_match.group(1).strip().strip("\"'")
    return ""


def _chart_option_literal(value: Any) -> str | None:
    if value is None or value is False:
        return None
    if isinstance(value, (dict, list)):
        dumped = json.dumps(value, ensure_ascii=False)
        if dumped in {"{}", "[]", "null"}:
            return None
        return _revive_official_formatter_refs(dumped)
    text = str(value).strip()
    if not text or text.casefold() in {"null", "none", "undefined", "{}", "[]"}:
        return None
    return _revive_official_formatter_refs(text)


def _option_assignment(value: Any) -> str | None:
    literal = _chart_option_literal(value)
    if not literal:
        return None
    stripped = literal.strip()
    if stripped.startswith("const ") or stripped.startswith("let ") or stripped.startswith("var "):
        return stripped if stripped.endswith(";") else f"{stripped};"
    return f"const option = {stripped};"


def _chart_option_for_suffix(slots: dict[str, Any], suffix: str) -> Any:
    aliases = ["CHART_OPTION", "CHART_OPTION_1"] if suffix == "" else [f"CHART_OPTION{suffix}"]
    for key in aliases:
        if key in slots:
            return slots[key]
    return None


def _activate_chart_scaffolds(html: str, slots: dict[str, Any]) -> str:
    """Stage 6 §3.5 唯一骨架例外：成对去掉 CHART_SCAFFOLD 注释定界符并填 option。"""
    if not html or not slots:
        return html
    visible = _HTML_COMMENT_RE.sub("", html)
    if _ECHARTS_INIT_RE.search(visible):
        return html

    def _replace(match: re.Match[str]) -> str:
        suffix = match.group(1) or ""
        inner = match.group(2) or ""
        assignment = _option_assignment(_chart_option_for_suffix(slots, suffix))
        if not assignment:
            return match.group(0)
        if _OPTION_NULL_RE.search(inner):
            inner = _OPTION_NULL_RE.sub(lambda _m: assignment, inner, count=1)
        elif "const option" not in inner:
            return match.group(0)
        font = str(slots.get("CHART_FONT_FAMILY") or "").strip()
        if font and font.casefold() not in {"null", "none"}:
            inner = _CHART_FONT_FAMILY_CONST_RE.sub(
                lambda m: f"{m.group(1)}{m.group(2)}{font}{m.group(2)}",
                inner,
                count=1,
            )
        return inner.strip()

    return _CHART_SCAFFOLD_BLOCK_RE.sub(_replace, html)


_GETELEMENTBYID_LITERAL_RE = re.compile(
    r"document\.getElementById\(\s*(['\"])([^'\"]+)\1\s*\)",
    re.IGNORECASE,
)
_CHART_CONTAINER_ID_ATTR_RE = re.compile(
    r"(<div\b[^>]*\bid\s*=\s*)(['\"])([^'\"]*chart[^'\"]*)\2",
    re.IGNORECASE,
)


def _chart_lookup_ids(html: str) -> list[str]:
    """已激活骨架里 getElementById 的入参；忽略 HTML / JS 块注释中的说明文字。"""
    visible = _CSS_COMMENT_RE.sub("", _HTML_COMMENT_RE.sub("", html or ""))
    ordered: list[str] = []
    seen: set[str] = set()
    for match in _GETELEMENTBYID_LITERAL_RE.finditer(visible):
        lookup_id = match.group(2).strip()
        if not lookup_id or "chart" not in lookup_id.casefold():
            continue
        if lookup_id in seen:
            continue
        seen.add(lookup_id)
        ordered.append(lookup_id)
    return ordered


def _visible_has_element_id(html: str, element_id: str) -> bool:
    visible = _HTML_COMMENT_RE.sub("", html or "")
    return bool(
        re.search(
            rf"\bid\s*=\s*(['\"]){re.escape(element_id)}\1",
            visible,
        )
    )


def _chart_host_is_empty(html: str, open_match: re.Match[str]) -> bool:
    tag_end = html.find(">", open_match.start())
    if tag_end < 0:
        return False
    return bool(re.match(r"\s*</div\b", html[tag_end + 1:], re.IGNORECASE))


def _align_chart_container_ids_to_lookups(html: str) -> str:
    """容器 id 必须等于已激活骨架 getElementById 入参；不改冻结的骨架 JS。"""
    if not html:
        return html
    lookup_ids = _chart_lookup_ids(html)
    if not lookup_ids:
        return html
    result = html
    lookup_set = set(lookup_ids)
    for lookup_id in lookup_ids:
        if _visible_has_element_id(result, lookup_id):
            continue
        comment_spans = [(m.start(), m.end()) for m in _HTML_COMMENT_RE.finditer(result)]
        chosen: re.Match[str] | None = None
        for match in _CHART_CONTAINER_ID_ATTR_RE.finditer(result):
            if any(start <= match.start() < end for start, end in comment_spans):
                continue
            current_id = match.group(3)
            if current_id == lookup_id:
                continue
            if current_id in lookup_set and _visible_has_element_id(result, current_id):
                continue
            if not _chart_host_is_empty(result, match):
                continue
            chosen = match
            break
        if chosen is None:
            continue
        quote = chosen.group(2)
        replacement = f"{chosen.group(1)}{quote}{lookup_id}{quote}"
        result = result[: chosen.start()] + replacement + result[chosen.end():]
    return result


def _materialize_template_fill(
    seed_html: str,
    llm_raw: str,
    extra_slots: dict[str, Any] | None = None,
) -> str:
    """内容页填槽：优先 JSON 槽值拼进 seed；完整 HTML 仍按旧路径验收，避免效果回退。"""
    text = _strip_html_fence(llm_raw or "")
    if _looks_like_html_document(text):
        return _align_chart_container_ids_to_lookups(text)
    slots = _parse_fill_slots(text)
    if not slots:
        return text
    if extra_slots:
        merged = dict(extra_slots)
        merged.update(slots)
        slots = merged
    filled = _activate_chart_scaffolds(_apply_template_slots(seed_html, slots), slots)
    return _align_chart_container_ids_to_lookups(filled)


def _validate_content_template_fill_output(seed_html: str, filled_html: str) -> tuple[bool, str]:
    """Stage 6 软门禁：内容页必须基于 seed 填槽，不能改 chrome。"""
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
        return False, "content_template_chrome_changed"

    seed_header = _normalize_template_whitespace(_normalize_h1_text_only(_extract_header_block(seed_html)))
    filled_header = _normalize_template_whitespace(_normalize_h1_text_only(_extract_header_block(filled_html)))
    if not seed_header or seed_header != filled_header:
        return False, "content_template_chrome_changed"

    seed_footer = _normalize_template_whitespace(_normalize_footer_text_only(seed_html))
    filled_footer = _normalize_template_whitespace(_normalize_footer_text_only(filled_html))
    if not seed_footer or seed_footer != filled_footer:
        return False, "content_template_chrome_changed"

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

    if not _validate_chart_height_chain(filled_html):
        return False, "invalid_chart_height_chain"
    return True, ""


def _css_custom_properties(css_text: str) -> str:
    props = [
        f"{name.strip()}: {value.strip()};"
        for name, value in _CSS_CUSTOM_PROP_RE.findall(css_text or "")
    ]
    return "\n      ".join(props)


def _extract_theme_css_slots(style_text: str) -> tuple[str, str]:
    """从 style-custom.md 提取 Stage 5 的变量块与可选全局规则块。"""
    fences = [block.strip() for block in _CSS_FENCE_RE.findall(style_text or "") if block.strip()]
    if not fences:
        return "", ""
    variables = _css_custom_properties(fences[0])
    rules = ""
    if len(fences) > 1:
        rules = fences[1]
    else:
        leftover = _CSS_CUSTOM_PROP_RE.sub("", fences[0])
        leftover = re.sub(r":root\s*\{|\}\s*$", "", leftover).strip()
        leftover = re.sub(r"\n{3,}", "\n\n", leftover).strip()
        if leftover and "{" in leftover:
            rules = leftover
    return variables, rules


def _apply_custom_theme_slots(seed_html: str, style_text: str) -> str:
    """按 Stage 6 §3.6 从风格文件逐字填入主题槽；仅用于 custom 内容页脚手架。"""
    variables, rules = _extract_theme_css_slots(style_text)
    html = seed_html or ""
    html = html.replace("{{THEME_CSS_VARIABLES}}", variables)
    html = html.replace("{{THEME_CSS_RULES}}", rules)
    return html


def _style_block_text(html: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(html or "")
    return match.group(0) if match else ""


def _css_props_map(css_text: str) -> dict[str, str]:
    return {
        name.strip(): re.sub(r"\s+", " ", value.strip())
        for name, value in _CSS_CUSTOM_PROP_RE.findall(css_text or "")
    }


def _theme_contract_vars_preserved(seed_html: str, filled_html: str) -> bool:
    """官方硬约束：theme-contract 插槽仍在，且预填变量名+值未被改写。"""
    seed_block = _style_block_text(seed_html, _THEME_CONTRACT_STYLE_RE)
    filled_block = _style_block_text(filled_html, _THEME_CONTRACT_STYLE_RE)
    if not seed_block or not filled_block:
        return False
    seed_props = _css_props_map(seed_block)
    if not seed_props:
        return True
    filled_props = _css_props_map(filled_block)
    return all(filled_props.get(name) == value for name, value in seed_props.items())


def _theme_rules_core_preserved(seed_html: str, filled_html: str) -> bool:
    """§3.6：已逐字填入的 THEME_CSS_RULES 必须仍在；未提供规则时空槽不是硬门禁。"""
    seed_block = _style_block_text(seed_html, _THEME_RULES_STYLE_RE)
    seed_inner = ""
    if seed_block:
        seed_inner = re.sub(
            r"^<style\b[^>]*>|</style>$",
            "",
            seed_block,
            flags=re.IGNORECASE | re.DOTALL,
        )
        seed_inner = _CSS_COMMENT_RE.sub("", seed_inner).strip()
    if not seed_inner:
        return True
    filled_block = _style_block_text(filled_html, _THEME_RULES_STYLE_RE)
    if not filled_block:
        return False
    filled_norm = _normalize_template_whitespace(_CSS_COMMENT_RE.sub("", filled_block))
    return _normalize_template_whitespace(seed_inner) in filled_norm


def _custom_slide_inner_markup(inner_html: str) -> str:
    text = re.sub(r"<!--.*?-->", "", inner_html or "", flags=re.DOTALL)
    text = re.sub(r"<script\b[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style\b[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()


def _custom_slide_has_page_content(html: str) -> bool:
    """§3.6：页面全部内容在 .ppt-slide 的 PAGE_CONTENT 内；不要求预设 h1/字数。"""
    bounds = _ppt_slide_bounds(html)
    if bounds is None:
        return False
    return bool(_custom_slide_inner_markup(html[bounds[0]:bounds[1]]))


_CUSTOM_ORPHAN_MAIN_RE = re.compile(r"<main\b[^>]*>.*?</main\s*>", re.IGNORECASE | re.DOTALL)


def _relocate_orphan_main_into_custom_slide(html: str) -> str:
    """custom 内容页专用：slide 内无内容且唯一 main 在 slide 外时，把 main 子节点搬进 slide。"""
    if not html or _custom_slide_has_page_content(html):
        return html
    mains = list(_CUSTOM_ORPHAN_MAIN_RE.finditer(html))
    if len(mains) != 1:
        return html
    main = mains[0]
    bounds = _ppt_slide_bounds(html)
    if bounds is None:
        return html
    start, end = bounds
    if start <= main.start() < end:
        return html
    main_inner = re.sub(
        r"^<main\b[^>]*>|</main\s*>$",
        "",
        main.group(0),
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    if not main_inner:
        return html
    html_wo_main = html[: main.start()] + html[main.end():]
    relocated = _ppt_slide_bounds(html_wo_main)
    if relocated is None:
        return html
    return html_wo_main[: relocated[0]] + main_inner + html_wo_main[relocated[1]:]


def _validate_custom_content_slide_dom(html: str) -> str:
    """custom 内容页：只拦官方「PAGE_CONTENT 为空」。

    畸形 token / `.ppt-slide` 边界解析失败不是 Stage 6 硬门禁；
    解析不到边界时不得误判为空，交给 P8.2 `cli.js fix`。
    """
    bounds = _ppt_slide_bounds(html or "")
    if bounds is None:
        return ""
    if not _custom_slide_inner_markup(html[bounds[0]:bounds[1]]):
        return "empty_slide_content"
    return ""


def _validate_custom_content_template_fill_output(
    seed_html: str,
    filled_html: str,
) -> tuple[bool, str]:
    """Stage 6 §3.6：官方脚手架语义硬约束，不另加标题文案黑名单。"""
    if not _is_valid_html(filled_html):
        return False, "invalid_html"
    if _has_unfilled_placeholders(filled_html):
        return False, "unfilled_placeholders"
    if _normalize_template_whitespace(seed_html) == _normalize_template_whitespace(filled_html):
        return False, "seed_not_modified"
    if "{{PAGE_CONTENT}}" in (filled_html or ""):
        return False, "page_content_unfilled"
    if not _theme_contract_vars_preserved(seed_html, filled_html):
        return False, "theme_contract_changed"
    if not _theme_rules_core_preserved(seed_html, filled_html):
        return False, "theme_rules_changed"
    if "@layer utilities" in (seed_html or "") and "@layer utilities" not in (filled_html or ""):
        return False, "utilities_layer_missing"
    dom_reason = _validate_custom_content_slide_dom(filled_html)
    if dom_reason:
        return False, dom_reason
    if not _ppt_slide_has_flex_col(filled_html):
        return False, "ppt_slide_not_flex_col"
    if not _validate_chart_height_chain(filled_html):
        return False, "invalid_chart_height_chain"
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
) -> str:
    """预设四风格内容页：content-template 预铺后仅填三处占位符。"""
    user_query_section = ""
    if user_query:
        user_query_section = (
            "## 用户原始 query（用于指导内容方向和视觉风格要求）\n"
            f"{user_query}\n"
            f"⚠️ 用户 query 中的页数/总量要求已由大纲规划完成，本步骤**仅填充第 {page_number} 页内容页模板**。\n\n"
        )
    outline_full_section = ""
    if outline_full.strip() and outline_full.strip() != outline_page.strip():
        outline_full_section = (
            "### 大纲全文（仅用于核对本页章节与上下文，不得混入其他页内容）\n"
            f"{outline_full}\n\n"
        )
    page_type = _detect_page_type(outline_page)
    page_number_rule = _VISIBLE_PAGE_NUMBER_RULE
    designer_section = ""
    if designer_md_text:
        designer_md = _extract_designer_section(
            designer_md_text,
            include_charts=page_type in _CHART_CANDIDATE_TYPES,
            for_content_template_fill=True,
        )
        if designer_md:
            designer_section = f"\n## skill designer 约束（仅作用于 `{{PAGE_CONTENT}}`）\n{designer_md}\n"
    layout_template = _build_content_layout_template(page_type)
    return (
        f"{user_query_section}"
        f"## 任务：填充第 {page_number} 页预设风格 content-template 官方模板\n"
        f"style_id=`{style_id}`，模板=`content-template.html`。你是模板填充师，不是自由排版设计师。\n\n"
        "## 填充规则（对齐 Stage 6 §3.5，严格遵守）\n"
        "1. **字面拷贝已完成**：下方 HTML 即官方 `content-template.html` 预铺结果；"
        "禁止重写整页、禁止改标题栏/页脚/CSS/`@layer utilities`/装饰/SVG/Tailwind class 顺序\n"
        "2. **只允许替换 3 类占位符**：`{{PAGE_TITLE}}`、`{{PAGE_CONTENT}}`、`{{PAGE_FOOTER}}`\n"
        "3. `{{PAGE_TITLE}}` 只填写本页标题文字；不得改 `<h1>` 的 class、字号、字重、字体、装饰线、padding\n"
        "4. `{{PAGE_FOOTER}}` 只填写来源/备注；不得追加运行页码\n"
        "5. `{{PAGE_CONTENT}}` 必须替换为一个且仅一个首层根容器，根容器必须带 `w-full flex-1 min-h-0`\n"
        "6. 不得修改预铺模板 `<main>` 的 class；所有布局变化仅在 `{{PAGE_CONTENT}}` 内完成\n"
        "7. 每个占位符必须填有意义内容；禁止空串、`—`/`–`/`-`、`N/A`、`TBD`、`暂无`、`待补充`、`待定`、`占位`\n"
        "8. 图表候选页必须优先激活模板内 `CHART_SCAFFOLD`：PAGE_CONTENT 只放图表容器，"
        "`CHART_OPTION` 填写 option 对象；由系统按模板注释成对去掉定界符并替换 `const option = null`。"
        "option 中的 formatter 只引用骨架内置函数，写成字符串 "
        "`formatAxisNumber` / `formatAxisPercent` / `formatLabelNumber` / `formatLabelPercent`，"
        "禁止内联函数体。禁止在 PAGE_CONTENT 内手写第二套 `echarts.init`；"
        "非图表页 `CHART_OPTION` 为 null，保留注释\n"
        "9. 禁止输出完整 HTML、禁止回显预铺骨架（标题栏/页脚结构/CSS/`@layer`/装饰/SVG）。"
        "只输出一个 JSON 对象，键名与占位符一致：`PAGE_TITLE`、`PAGE_CONTENT`、`PAGE_FOOTER`，"
        "图表页另给 `CHART_OPTION`（对象或 null）\n"
        f"{_SLIDE_DESIGNER_THINKING_OFF}\n"
        "## 风格文件（正文区配色/字体/组件权威；不得把风格元数据写成观众可见文字）\n"
        f"{style_text}\n\n"
        "## 大纲 — 本页规划\n"
        f"{outline_page}\n\n"
        f"{outline_full_section}"
        "## 研究报告 — 本页素材\n"
        f"{research_page}\n"
        f"{_build_image_section(image_map_page)}\n"
        f"{page_number_rule}"
        f"{_EDITABLE_LAYERING_RULES}"
        f"{designer_section}"
        f"{layout_template}\n"
        "## 预铺模板 HTML（只填槽，勿重写）\n"
        f"{seed_html}\n"
    )


def _build_custom_content_template_fill_prompt(
    *,
    page_number: int,
    style_text: str,
    outline_page: str,
    research_page: str,
    outline_full: str,
    seed_html: str,
    image_map_page: str = "",
    designer_md_text: str = "",
    user_query: str = "",
) -> str:
    """custom 内容页：对齐 Stage 6 §3.6 与 generate-slide-designer-tasks 通用规范。"""
    user_query_section = ""
    if user_query:
        user_query_section = (
            "## 用户原始 query（用于指导内容方向和视觉风格要求）\n"
            f"{user_query}\n"
            f"⚠️ 用户 query 中的页数/总量要求已由大纲规划完成，本步骤**仅填充第 {page_number} 页内容页**。\n\n"
        )
    outline_full_section = ""
    if outline_full.strip() and outline_full.strip() != outline_page.strip():
        outline_full_section = (
            "### 大纲全文（仅用于核对本页章节与上下文，不得混入其他页内容）\n"
            f"{outline_full}\n\n"
        )
    page_type = _detect_page_type(outline_page)
    designer_section = ""
    if designer_md_text:
        designer_md = _extract_designer_section(
            designer_md_text,
            include_charts=page_type in _CHART_CANDIDATE_TYPES,
            for_custom_content_fill=True,
        )
        if designer_md:
            designer_section = (
                "\n## skill designer 约束（写 HTML 前先完成页面内容预算；"
                "图表候选页另读图表章节）\n"
                f"{designer_md}\n"
            )
    return (
        f"{user_query_section}"
        f"## 任务：填充第 {page_number} 页 custom 内容页官方脚手架\n"
        "style_id=`custom`，模板=`references/styles/custom/content-template.html`。\n"
        "对齐 Stage 6 §3.6。\n\n"
        "## 填充规则（Stage 6 §3.6）\n"
        "1. 已预铺 `custom/content-template.html`：逐字保留 `@layer utilities` 安全块"
        "（`.ppt-slide` 1280×720、`overflow: hidden`）与 `theme-contract` 插槽。"
        "画布容器须为 `generate-slide-designer-tasks` 强制的 "
        "`<div class=\"ppt-slide flex flex-col\" type=\"...\">`"
        "（系统写盘时补上 `flex flex-col`；禁止改 `@layer` 中的 `.ppt-slide` 规则）\n"
        "2. `{{THEME_CSS_VARIABLES}}` 与 `{{THEME_CSS_RULES}}` 已从风格文件逐字填入"
        "（风格文件未提供规则块时该槽为空）；不得逐页重新选择全局颜色或字体，"
        "不得改写已注入的变量名与取值\n"
        "3. `{{PAGE_TITLE}}` 填写本页大纲标题；页面全部内容（含标题、正文、页脚）"
        "在 `{{PAGE_CONTENT}}` 内依据风格文件设计；全部可见内容必须写在 `.ppt-slide` 内，"
        "若使用 `<main>` 必须放在该容器内部。"
        "header / main / footer 必须参与同一个纵向 flex 布局"
        "（`generate-slide-designer-tasks` 页面纵向结构）\n"
        "4. 新增样式只引用风格文件已定义变量，不得另立视觉权威，不得覆盖 `@layer utilities`，"
        "不得用 `*`、`body` 或根 `:root` 重定义颜色/字体\n"
        "5. 图表候选页必须优先激活模板内 `CHART_SCAFFOLD`：PAGE_CONTENT 只放图表容器，"
        "`CHART_OPTION` 填写 option 对象；由系统按脚手架注释激活。"
        "option 中的 formatter 只引用骨架内置函数（写成对应函数名字符串），禁止内联函数体。"
        "禁止额外手写第二套 `echarts.init`；非图表页保留注释。"
        "`CHART_FONT_FAMILY` 由系统写入风格文件 frontmatter 完整字体栈，无需在 JSON 中改脚手架代码。"
        "容器高度链须遵从 designer.md / 脚手架注释："
        "`div.flex.flex-col` → `div.flex-1.min-h-0` → `div#chart-1.w-full.h-full`"
        "（可读高度建议 ≥300px 由页面预算保证；模板 CSS 已兜底 min-height:160px）\n"
        "6. 完成后不得残留任何 `{{[A-Z][A-Z0-9_]*}}`\n"
        "7. 禁止输出完整 HTML、禁止回显脚手架骨架。只输出一个 JSON 对象，"
        "键为 `PAGE_TITLE`、`PAGE_CONTENT`，图表页另给 `CHART_OPTION`（对象或 null）\n"
        f"{_SLIDE_DESIGNER_THINKING_OFF}\n"
        "## 可见文字来源契约（generate-slide-designer-tasks）\n"
        "所有观众可见文字只能来自用户原始需求、outline.md、本页 research-P{N}.md "
        "或已批准模板的固定文案。"
        "禁止为营造氛围自行添加与叙事无关的英文、随机数字或制作编号。\n\n"
        "## 风格文件（本次演示唯一视觉权威）\n"
        f"{style_text}\n\n"
        "## 大纲 — 本页规划\n"
        f"{outline_page}\n\n"
        f"{outline_full_section}"
        "## 研究报告 — 本页素材\n"
        f"{research_page}\n"
        f"{_build_image_section(image_map_page)}\n"
        f"{designer_section}"
        f"{_VISIBLE_PAGE_NUMBER_RULE}"
        "## 预铺模板 HTML（主题槽已按风格文件填入，只填 PAGE_TITLE / PAGE_CONTENT）\n"
        f"{seed_html}\n"
    )


_VISIBLE_PAGE_NUMBER_RULE = (
    "- 禁止页脚出现页码（所有页型）：所有页面（content / cover / agenda / section / "
    "chapter / ending）的页脚一律禁止「第 N 页」「Page N」「N / M」「P N」等任何页码表述；"
    "唯一允许的是数据来源/备注/日期。"
    "agenda 正文中的章节目标页码属于导航内容，可以保留。\n"
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
    "4.2.1 图表高度链（强制，对齐 designer.md CHART_SCAFFOLD）："
    "`flex flex-col`（父）→ `flex-1 min-h-0`（包装器）→ `w-full h-full`（chart）；"
    "也允许等价合并为外层卡片 `flex-1 min-h-0 flex flex-col`（或 `flex-[N] min-h-0 flex flex-col`）直接包住 chart；"
    "禁止外层仅有 `flex flex-col` 且无中间高度包装时直接挂 chart\n"
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
    "禁止在页脚追加任何运行页码\n"
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


def _is_valid_html(text: str) -> bool:
    if not text or len(text) < 200:
        return False
    lower = text.lower()
    if "<html" not in lower and "<!doctype html" not in lower:
        return False
    if "ppt-slide" not in lower:
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
    r"<div\b[^>]*\bclass\s*=\s*(?:\"[^\"]*\bppt-slide\b[^\"]*\"|'[^']*\bppt-slide\b[^']*')",
    re.IGNORECASE,
)
_PPT_SLIDE_CLASS_RE = re.compile(
    r"(<div\b[^>]*\bclass\s*=\s*)([\"'])([^\"']*\bppt-slide\b[^\"']*)\2",
    re.IGNORECASE,
)


def _ppt_slide_class_tokens(class_value: str) -> set[str]:
    return {token.casefold() for token in (class_value or "").split() if token}


def _ppt_slide_has_flex_col(html: str) -> bool:
    """generate-slide-designer-tasks：画布须为 `ppt-slide flex flex-col`。"""
    match = _PPT_SLIDE_CLASS_RE.search(html or "")
    if not match:
        return False
    tokens = _ppt_slide_class_tokens(match.group(3))
    return "flex" in tokens and "flex-col" in tokens


def _ensure_ppt_slide_flex_col(html: str) -> str:
    """custom seed 不带纵向 flex；写盘时补官方强制容器 class，不改 @layer。"""
    if not html:
        return html
    match = _PPT_SLIDE_CLASS_RE.search(html)
    if not match:
        return html
    tokens = match.group(3).split()
    lower = _ppt_slide_class_tokens(match.group(3))
    if "flex-row" in lower:
        return html
    changed = False
    if "flex" not in lower:
        tokens.append("flex")
        changed = True
    if "flex-col" not in lower:
        tokens.append("flex-col")
        changed = True
    if not changed:
        return html
    quote = match.group(2)
    replacement = f"{match.group(1)}{quote}{' '.join(tokens)}{quote}"
    return html[: match.start()] + replacement + html[match.end():]


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


def _slide_dom_soft_issue(html: str) -> str:
    """返回 Stage 6 不作为写盘硬拒的 DOM 问题，供日志与 P8.2 fix。"""
    if _MALFORMED_HTML_RE.search(html or ""):
        return "malformed_tokens"
    if _ppt_slide_bounds(html or "") is None:
        return "ppt_slide_unparsed"
    if not _main_inside_ppt_slide(html or ""):
        return "main_outside_slide"
    return ""


def _is_slide_exportable(html: str) -> bool:
    """P8.2 fix 后校验：仅确认导出边界内的结构未被破坏。"""
    return _main_inside_ppt_slide(html)


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


def _chart_wrapper_has_height_chain(wrapper_tag: str) -> bool:
    """designer.md 图表高度链：包装器须参与纵向高度分配（min-h-0 或 flex-1/flex-[N]）。"""
    return bool(_CHART_WRAPPER_HEIGHT_RE.search(wrapper_tag))


_CHART_HEIGHT_DIV_OPEN_RE = re.compile(
    r'<div\b[^>]*\bclass="[^"]*"[^>]*>',
    re.IGNORECASE,
)


def _segment_has_height_wrapper(segment: str) -> bool:
    """flex-col 与 chart 之间是否存在带高度分配类的中间包装 div。"""
    for match in _CHART_HEIGHT_DIV_OPEN_RE.finditer(segment or ""):
        if _chart_wrapper_has_height_chain(match.group(0)):
            return True
    return False


def _validate_chart_height_chain(html: str) -> bool:
    """P8.1 写盘前校验：对齐 designer.md / CHART_SCAFFOLD 高度链。

    官方契约：`flex flex-col`（父）→ `flex-1 min-h-0`（包装器）→ `w-full h-full`（chart）。
    等价合并写法：`flex-1 min-h-0 flex flex-col` 直接包住 chart 也通过。

    仅拦截高置信坏案：最近 flex-col 自身无高度类，且与 chart 之间也无高度包装；
    无法定位 flex-col 时不拦截，避免误伤。
    """
    if "echarts.init" not in html.lower():
        return True
    for chart_match in _CHART_DIV_RE.finditer(html):
        before = html[max(0, chart_match.start() - 2000):chart_match.start()]
        wrappers = list(_FLEX_COL_DIV_RE.finditer(before))
        if not wrappers:
            continue
        nearest = wrappers[-1]
        if _chart_wrapper_has_height_chain(nearest.group(0)):
            continue
        # 官方三层：高度类在 flex-col 与 chart 之间的中间包装上，不要求写在 flex-col 自身。
        between = before[nearest.end():]
        if _segment_has_height_wrapper(between):
            continue
        return False
    return True


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
_FOOTER_OPEN_RE = re.compile(
    r"<(?P<tag>footer|div|section|p|span)\b(?P<attrs>[^>]*)>",
    re.IGNORECASE,
)
_FOOTER_ROLE_ATTR_RE = re.compile(
    r"\bdata-pptx-role\s*=\s*['\"]footer['\"]",
    re.IGNORECASE,
)
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


def _find_matching_close(html_text: str, inner_start: int, tag: str) -> int | None:
    """从 inner_start 起按标签名做括号匹配，返回闭合标签之后的下标。"""
    open_re = re.compile(rf"<{re.escape(tag)}\b[^>]*>", re.IGNORECASE)
    close_re = re.compile(rf"</{re.escape(tag)}\s*>", re.IGNORECASE)
    depth = 1
    pos = inner_start
    while pos < len(html_text):
        open_match = open_re.search(html_text, pos)
        close_match = close_re.search(html_text, pos)
        if close_match is None:
            return None
        if open_match is not None and open_match.start() < close_match.start():
            depth += 1
            pos = open_match.end()
            continue
        depth -= 1
        pos = close_match.end()
        if depth == 0:
            return pos
    return None


def _footer_ranges(html_text: str) -> tuple[tuple[int, int], ...]:
    """官方页脚范围：`<footer>` 或 `data-pptx-role="footer"`。"""
    ranges: list[tuple[int, int]] = []
    for match in _FOOTER_OPEN_RE.finditer(html_text):
        tag = match.group("tag")
        attrs = match.group("attrs") or ""
        is_footer_tag = tag.lower() == "footer"
        is_footer_role = bool(_FOOTER_ROLE_ATTR_RE.search(attrs))
        if not (is_footer_tag or is_footer_role):
            continue
        if any(start <= match.start() < end for start, end in ranges):
            continue
        end = _find_matching_close(html_text, match.end(), tag)
        if end is None:
            continue
        ranges.append((match.start(), end))
    return tuple(ranges)


def _strip_visible_page_markers(html_text: str) -> str:
    """仅移除页脚内的运行页码，对齐 designer.md；header / 角标页码不删。"""
    if not html_text:
        return html_text

    footer_ranges = _footer_ranges(html_text)
    if not footer_ranges:
        return html_text

    removed_markers: list[str] = []

    def _replace_marker(match: re.Match[str]) -> str:
        if not any(start <= match.start() < end for start, end in footer_ranges):
            return match.group(0)
        marker = _normalize_page_marker_text(match.group("text"))
        if not _VISIBLE_PAGE_MARKER_RE.fullmatch(marker):
            return match.group(0)
        removed_markers.append(marker)
        return ""

    normalized = _VISIBLE_TEXT_LEAF_RE.sub(_replace_marker, html_text)
    if removed_markers:
        logger.info("[P8.1] 已移除页脚运行页码 markers=%s", removed_markers)
    return normalized


# 匹配含 ppt-slide 的 div 开始标签（兼容单双引号）。
_PPT_SLIDE_DIV_RE = re.compile(
    r"<div[^>]*\bclass\s*=\s*(?:\"[^\"]*\bppt-slide\b[^\"]*\"|'[^']*\bppt-slide\b[^']*')",
    re.IGNORECASE,
)


def _truncate_to_single_slide(html: str) -> str:
    """如果 HTML 包含多个 ppt-slide 容器，截取第一个并保留 HTML 骨架。

    LLM 偶尔会忽略单页约束，将全部页面写入一个 HTML 文件。
    此函数检测到多 slide 时截取第一个，丢弃其余，并补全闭合标签。
    """
    matches = list(_PPT_SLIDE_DIV_RE.finditer(html))
    if len(matches) <= 1:
        return html

    # 从第二个 ppt-slide div 往前找 <div 起始位置
    second_match = matches[1]
    div_start = html.rfind("<div", 0, second_match.start())
    if div_start == -1:
        div_start = second_match.start()

    # 还需要往回找注释标记（如 <!-- P2 ... -->）
    comment_pos = html.rfind("<!--", 0, div_start)
    cut_pos = min(comment_pos, div_start) if comment_pos != -1 else div_start

    truncated = html[:cut_pos].rstrip()
    # 补全闭合标签
    if "</body>" not in truncated.lower():
        truncated += "\n</body>\n</html>\n"

    logger.warning(
        "[P8.1] 检测到 %d 个 ppt-slide 容器，已截取第一个 slide，丢弃其余 %d 个",
        len(matches),
        len(matches) - 1,
    )
    return truncated


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


_HTML_CLASS_RE = re.compile(
    r"\bclass\s*=\s*(?P<quote>[\"'])(?P<classes>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)


def _classes_from_tag_attrs(attrs: str) -> set[str]:
    match = _HTML_CLASS_RE.search(attrs)
    return set(match.group("classes").split()) if match else set()


_HTML_STYLE_RE = re.compile(
    r"\bstyle\s*=\s*(?P<quote>[\"'])(?P<style>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(?P<css>.*?)</style>", re.IGNORECASE | re.DOTALL)
_CSS_RULE_RE = re.compile(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", re.DOTALL)


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
    page_number_rule = _VISIBLE_PAGE_NUMBER_RULE

    density_checklist = _STRUCTURAL_DENSITY_CHECKLIST if is_structural else _DENSITY_CHECKLIST_DIGEST
    design_rules = _STRUCTURAL_DESIGN_RULES if is_structural else _DESIGN_RULES_DIGEST
    html_skeleton = _STRUCTURAL_HTML_SKELETON if is_structural else _HTML_SKELETON

    # 注入新版 skill designer 规范；图表候选页从同一 designer.md 追加图表章节。
    # 文件内容由 PrepareNode 通过 read_file 工具读取后传入
    designer_section = ""
    if designer_md_text:
        designer_md = _extract_designer_section(
            designer_md_text,
            include_charts=page_type in _CHART_CANDIDATE_TYPES,
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


class PageWorkerNode(PlanNode):
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
                "1. 若 style_id ∈ 预设四风格∪custom 且页型为 agenda/cover/ending/section："
                "读取官方结构页模板预铺，LLM 仅替换 `{{}}`；残留占位符判失败\n"
                "2. 若为内容页且 style_id ∈ 预设四风格∪custom：读取官方 content-template 预铺填槽"
                "（custom 先按风格文件逐字填入 THEME_CSS 槽，再只填 PAGE_TITLE/PAGE_CONTENT）\n"
                "3. 其余页：用该页 outline 片段 + research 片段 + 风格规范构造 prompt 生成 HTML\n"
                "   - 剥 ```html 包裹 → 校验 → write_file 落盘\n"
                "   - 失败按 gen_retry_round 重试（仅本页）\n"
                "   - 重试后仍失败 → 进 missing_pages\n"
                "4. 成功页只保留一个 ppt-slide 容器并直接落盘；生成后不再调用 LLM 核查、搜索或整页重写\n"
                "\n"
                "### 失败兜底\n"
                "- 生成 LLM 调用 raise / 返回空 / HTML 校验失败：进 missing_pages\n"
                "- agenda 模板缺失或填槽后残留 `{{...}}`：进 missing_pages\n"
                "- 畸形 token / main 滑出 slide：软警告并仍落盘，交 P8.2 fix，不进 missing_pages\n"
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
        for p, r in zip(all_pages, results):
            if isinstance(r, BaseException):
                logger.warning("[P8.1] 页面 %d 生成异常: %s", p, r)
                missing_pages.append(p)
                continue
            if r.get("missing"):
                missing_pages.append(p)

        successful_pages = [p for p in all_pages if p not in missing_pages]
        page_files = [f"page-{p}.pptx.html" for p in successful_pages]

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

        html = ""
        attempt_count = min(
            max(gen_retry_round + 1, 1),
            _MAX_PAGE_GENERATION_ATTEMPTS,
        )
        for attempt in range(attempt_count):
            if attempt > 0:
                logger.info("[P8.1] 页面 %d 第 %d 轮生成重试", page_num, attempt + 1)
            html = await self._generate_one(ctx)
            if html:
                break
        if not html:
            return {"missing": True, "low_density": False, "report": {}}

        # 防御：LLM 偶尔忽略单页约束，生成多个 slide，截取第一个
        html = _truncate_to_single_slide(html)

        ok = await self._write_file(path, html)
        if not ok:
            return {"missing": True, "low_density": False, "report": {}}

        return {
            "missing": False,
            "low_density": False,
            "report": {},
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

        html = _strip_html_fence(result or "")
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

        html = _strip_visible_page_markers(html)
        if ctx.style_id == "custom":
            html = _ensure_ppt_slide_flex_col(html)
        dom_issue = _slide_dom_soft_issue(html)
        if dom_issue:
            logger.warning(
                "[P8.1] 结构页 DOM 软警告 page=%d type=%s issue=%s，仍落盘交 P8.2 fix",
                ctx.page_num,
                page_type,
                dom_issue,
            )
        logger.info(
            "[P8.1] 结构页官方模板填槽完成 page=%d style=%s type=%s",
            ctx.page_num,
            ctx.style_id,
            page_type,
        )
        return html

    async def _generate_content_template_fill(self, ctx: PageGenContext) -> str:
        """普通分支内容页：官方 content-template 预铺后填槽。"""
        if not ctx.pptx_root:
            logger.error("[P8.1] 内容页填槽缺少 pptx_root page=%d", ctx.page_num)
            return ""

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
            return ""

        is_custom = ctx.style_id == "custom"
        if is_custom:
            seed_html = _apply_custom_theme_slots(seed_html, ctx.style_text)
            prompt = _build_custom_content_template_fill_prompt(
                page_number=ctx.page_num,
                style_text=ctx.style_text,
                outline_page=ctx.outline_page,
                research_page=ctx.research_page,
                outline_full=ctx.outline_full,
                seed_html=seed_html,
                image_map_page=ctx.image_map_page,
                designer_md_text=ctx.designer_md_text,
                user_query=ctx.user_query,
            )
            system_prompt = (
                "你是 PPT custom 内容页脚手架填充师。主题 CSS 已按风格文件填入；"
                "只替换 PAGE_TITLE 与 PAGE_CONTENT，只输出 JSON，禁止回显完整 HTML。"
            )
        else:
            prompt = _build_content_template_fill_prompt(
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
            )
            system_prompt = (
                "你是 PPT 内容页模板填充师。只替换 PAGE_TITLE、PAGE_CONTENT、PAGE_FOOTER，"
                "只输出 JSON，禁止回显或重写预铺骨架。"
            )

        try:
            result = await self.stream_llm_collect(
                prompt=prompt,
                system_prompt=system_prompt,
                node_name=f"p8_1_content_fill_{ctx.page_num}",
                concurrent=True,
            )
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.1] 内容页填槽 LLM 失败 page=%d: %s", ctx.page_num, e)
            return ""

        extra_slots: dict[str, Any] = {}
        if is_custom:
            font_stack = _style_frontmatter_font_stack(ctx.style_text)
            if font_stack:
                extra_slots["CHART_FONT_FAMILY"] = font_stack
        html = _materialize_template_fill(seed_html, result or "", extra_slots or None)
        html = _replace_placeholder_headings(html, ctx.outline_page)
        html = _strip_visible_page_markers(html)
        html = _fix_echarts_svg_renderer(html)
        html = _strip_unsupported_fullpage_overlays(html)
        html = _strip_chart_header_unit(html)
        if is_custom:
            html = _relocate_orphan_main_into_custom_slide(html)
            html = _ensure_ppt_slide_flex_col(html)
            ok, reason = _validate_custom_content_template_fill_output(seed_html, html)
        else:
            ok, reason = _validate_content_template_fill_output(seed_html, html)
        if not ok:
            logger.warning(
                "[P8.1] 内容页填槽校验失败 page=%d style=%s reason=%s",
                ctx.page_num,
                ctx.style_id,
                reason,
            )
            return ""
        dom_issue = _slide_dom_soft_issue(html)
        if dom_issue:
            logger.warning(
                "[P8.1] 内容页 DOM 软警告 page=%d style=%s issue=%s，仍落盘交 P8.2 fix",
                ctx.page_num,
                ctx.style_id,
                dom_issue,
            )
        logger.info(
            "[P8.1] 内容页官方模板填槽完成 page=%d style=%s",
            ctx.page_num,
            ctx.style_id,
        )
        return html

    async def _generate_one(self, ctx: PageGenContext) -> str:
        """生成单页 HTML，返回校验通过的 html 或空串。"""
        page_type = _detect_page_type(ctx.outline_page)
        if _uses_structural_template_fill(ctx.style_id, page_type):
            return await self._generate_structural_template_fill(ctx, page_type)
        if _uses_content_template_fill(ctx.style_id, page_type, ctx.outline_page):
            return await self._generate_content_template_fill(ctx)

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
                ),
                system_prompt="你是资深演示文稿设计师，直接输出完整 HTML 原文，不输出任何解释。",
                node_name=f"p8_1_page_{ctx.page_num}",
                concurrent=True,
            )
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.1] 页面 %d 生成 LLM 失败: %s", ctx.page_num, e)
            return ""
        html = _strip_html_fence(result or "")
        if not _is_valid_html(html):
            logger.warning("[P8.1] 页面 %d HTML 校验失败", ctx.page_num)
            return ""
        # 后置校验：替换「第X页」标题占位符为 outline 中的实际标题
        html = _replace_placeholder_headings(html, ctx.outline_page)
        html = _strip_visible_page_markers(html)
        html = _fix_echarts_svg_renderer(html)
        html = _strip_unsupported_fullpage_overlays(html)
        html = _strip_chart_header_unit(html)
        dom_issue = _slide_dom_soft_issue(html)
        if dom_issue:
            logger.warning(
                "[P8.1] 页面 %d DOM 软警告 issue=%s，仍落盘交 P8.2 fix",
                ctx.page_num,
                dom_issue,
            )
        if not _validate_chart_height_chain(html):
            logger.warning("[P8.1] 页面 %d 图表容器高度链校验失败", ctx.page_num)
            return ""
        return html

    async def _write_file(self, path: str, content: str) -> bool:
        if not self.has_tool("write_file"):
            logger.error("[P8.1] write_file 工具不可用 %s", path)
            return False
        try:
            await self.call_tool("write_file", file_path=path, content=content)
            return True
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.error("[P8.1] 写入文件失败 %s: %s", path, e)
            return False

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
                "3. Stage 6 到此结束；可选 check-layout 按 skill 规则仅在首次导出后且显式启用时执行\n"
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

    async def _read_page_file(self, path: str) -> str:
        if not path or not self.has_tool("read_file"):
            return ""
        try:
            result = await self.call_tool("read_file", file_path=path)
            return PptCommon.parse_tool_file_content(result)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.2] 读取页面失败 %s: %s", path, e)
            return ""

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
            paths = self._parse_listing(result)
        except Exception as e:
            if isinstance(e, AbortError):
                raise
            logger.warning("[P8.2] 查找 backup 失败 page=%d: %s", page_num, e)
            return ""
        if not paths:
            return ""
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
        sem = asyncio.Semaphore(10)

        async def _fix_one(page_num: int) -> tuple[int, bool, str]:
            async with sem:
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
                after_ok = bool(after_html) and _is_slide_exportable(after_html)
                if before_ok and not after_ok:
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
                    "page_count": len(final_page_files),
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
                    retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)
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
                    "page_count": len(page_files),
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
        yield {
            **result,
            "node": self.plan_name,
            "status": status_map.get(result.get("ppt_gen_status", ""), "warning"),
            "message": (
                f"PPT 生成完成 status={result.get('ppt_gen_status')} "
                f"成功 {len(result.get('page_files', []))} 页"
            ),
        }
