"""Template pre-seed and slot-fill helpers for SlideDesignerWorker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
_PAGE_TYPE_PATTERNS = (
    re.compile(r"页面类型[：:]\s*(\S+)", re.IGNORECASE),
    re.compile(r"类型\*{0,2}[：:]\s*(\w+)", re.IGNORECASE),
    re.compile(r"页类型[：:]\s*(\S+)", re.IGNORECASE),
)

_PRESET_STYLE_IDS = frozenset(
    {"business-classic", "tech-minimal", "elegant-narrative", "industrial-tech"}
)

_STRUCTURAL_PAGE_TYPES: dict[str, str] = {
    "cover": "cover",
    "intro": "cover",
    "agenda": "agenda",
    "section": "section",
    "chapter": "section",
    "ending": "ending",
    "conclusion": "ending",
    "transition": "ending",
}

_TEMPLATE_BY_PAGE_TYPE: dict[str, str] = {
    "cover": "cover-template.html",
    "intro": "cover-template.html",
    "agenda": "agenda-template.html",
    "section": "section-template.html",
    "chapter": "section-template.html",
    "ending": "ending-template.html",
    "conclusion": "ending-template.html",
    "transition": "ending-template.html",
}
_DEFAULT_CONTENT_TEMPLATE = "content-template.html"


class FillMode:
    PRESET_TEMPLATE = "preset_template"
    CUSTOM_TEMPLATE = "custom_template"
    FREE_GENERATE = "free_generate"


@dataclass(frozen=True)
class PageGenPolicy:
    """Per-deck HTML generation policy for SlideDesignerWorker."""

    allow_free_gen_fallback: bool = False
    max_fill_attempts: int = 3
    max_layout_attempts: int = 2
    run_check_layout: bool = True

    @classmethod
    def from_inputs(cls, inputs: dict[str, Any], *, style_id: str = "") -> PageGenPolicy:
        raw = inputs.get("page_gen_policy")
        if isinstance(raw, PageGenPolicy):
            return raw
        if isinstance(raw, dict):
            return cls(
                allow_free_gen_fallback=bool(raw.get("allow_free_gen_fallback", False)),
                max_fill_attempts=int(raw.get("max_fill_attempts") or 3),
                max_layout_attempts=int(raw.get("max_layout_attempts") or 2),
                run_check_layout=bool(raw.get("run_check_layout", True)),
            )
        style_mode = str(inputs.get("style_mode") or "").strip()
        allow_fallback = style_mode == "custom" or style_id == "custom"
        return cls(allow_free_gen_fallback=allow_fallback)


def detect_page_type(outline_page: str) -> str:
    if not outline_page:
        return ""
    for pattern in _PAGE_TYPE_PATTERNS:
        match = pattern.search(outline_page)
        if match:
            return match.group(1).strip().lower()
    return ""


def template_filename_for_page_type(page_type: str) -> str:
    if page_type in _STRUCTURAL_PAGE_TYPES:
        mapped = _STRUCTURAL_PAGE_TYPES[page_type]
        return _TEMPLATE_BY_PAGE_TYPE.get(mapped, f"{mapped}-template.html")
    return _DEFAULT_CONTENT_TEMPLATE


def build_page_template_map(
    outline_pages: dict[int, str],
    total_pages: int,
) -> str:
    """Build ensure-output-dir --page-templates map string."""
    if total_pages <= 0:
        return ""
    parts: list[str] = []
    for page_num in range(1, total_pages + 1):
        outline_page = outline_pages.get(page_num, "")
        page_type = detect_page_type(outline_page)
        template_name = template_filename_for_page_type(page_type)
        parts.append(f"{page_num}={template_name}")
    return ",".join(parts)


def scan_placeholders(html: str) -> list[str]:
    if not html:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(html):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def apply_template_slots(html: str, slots: dict[str, str]) -> str:
    out = html
    for key, value in slots.items():
        placeholder = f"{{{{{key}}}}}"
        if placeholder in out:
            out = out.replace(placeholder, value or "")
    return out


_CSS_FENCE_RE = re.compile(r"```css\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_theme_blocks(style_text: str) -> tuple[str, str]:
    """Extract THEME_CSS_VARIABLES / THEME_CSS_RULES from style-custom.md."""
    if not style_text.strip():
        return "", ""

    variables = ""
    rules = ""

    css_blocks = [m.group(1).strip() for m in _CSS_FENCE_RE.finditer(style_text)]
    for block in css_blocks:
        if ":root" in block and not variables:
            variables = block
        elif block and block != variables:
            rules = block if not rules else f"{rules}\n{block}"

    if not variables:
        root_match = re.search(r"(:root\s*\{.*?\})", style_text, re.DOTALL | re.IGNORECASE)
        if root_match:
            variables = root_match.group(1).strip()

    return variables, rules


def count_page_main_direct_children(page_content_html: str) -> int:
    """Heuristic: count top-level block tags in PAGE_CONTENT fragment."""
    if not page_content_html.strip():
        return 0
    pattern = re.compile(
        r"<\s*(section|div|article|aside|ul|ol|table|figure|blockquote)\b",
        re.IGNORECASE,
    )
    return len(pattern.findall(page_content_html))


def resolve_fill_mode(
    *,
    style_mode: str,
    style_id: str,
    pages_seeded: bool,
    page_type: str,
    policy: PageGenPolicy,
) -> FillMode:
    if style_mode == "template_canvas":
        raise ValueError("template_canvas must not use SlideDesignerWorker")

    if pages_seeded and style_id in _PRESET_STYLE_IDS:
        return FillMode.PRESET_TEMPLATE
    if pages_seeded and (style_mode == "custom" or style_id == "custom"):
        return FillMode.CUSTOM_TEMPLATE

    if style_id in _PRESET_STYLE_IDS:
        return FillMode.PRESET_TEMPLATE
    if style_mode == "custom" or style_id == "custom":
        return FillMode.CUSTOM_TEMPLATE

    return FillMode.FREE_GENERATE


def should_seed_pages(style_mode: str) -> bool:
    return style_mode in {"preset", "custom"}


def resolve_template_dir(pptx_root: str, style_mode: str, style_id: str) -> str:
    root = pptx_root.replace("\\", "/").rstrip("/")
    if style_mode == "custom" or style_id == "custom":
        return f"{root}/references/styles/custom"
    return f"{root}/references/styles/{style_id}"


def resolve_skill_root(pptx_root: str) -> str:
    """Return parent skills directory for CLI --skill-root."""
    from pathlib import PurePosixPath, PureWindowsPath

    normalized = (pptx_root or "").rstrip("\\/")
    if not normalized:
        return ""
    if "\\" in normalized or re.match(r"^[A-Za-z]:", normalized):
        root = PureWindowsPath(normalized)
    else:
        root = PurePosixPath(normalized)
    if root.name == "pptx-craft":
        return str(root.parent)
    return str(root)
