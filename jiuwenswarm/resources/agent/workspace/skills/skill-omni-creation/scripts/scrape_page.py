#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

# The environment gate uses only the standard library. It selects/re-executes
# the correct virtual-environment interpreter and repairs missing packages or
# Chromium before common.py (which imports requests) is loaded.
from environment_gate import EnvironmentGateError, ensure_environment

BeautifulSoup = None
common = None
_Stealth = None
_has_stealth = False


def _load_runtime_dependencies(*, require_web: bool) -> None:
    global BeautifulSoup, common, _Stealth, _has_stealth

    ensure_environment("web" if require_web else "requests")

    import common as common_module
    common = common_module

    if require_web:
        from bs4 import BeautifulSoup as BeautifulSoupClass
        BeautifulSoup = BeautifulSoupClass
        try:
            from playwright_stealth import Stealth as StealthClass
            _Stealth = StealthClass
            _has_stealth = True
        except ImportError:
            _Stealth = None
            _has_stealth = False

PLATFORM_PATTERNS = [
    r"youtube\.com/watch", r"youtu\.be/",
    r"bilibili\.com/video", r"vimeo\.com/\d+",
    r"twitter\.com/.+/status", r"x\.com/.+/status",
]

# XHS URLs are probed first (may be video or image post)
XHS_PATTERNS = [
    r"xiaohongshu\.com/explore",
    r"xiaohongshu\.com/discovery/item",
    r"xhslink\.com/",
]

COOKIE_SELECTORS = [
    "button#onetrust-accept-btn-handler",
    "button[id*='accept-all']",
    "button[id*='accept_all']",
    "button[class*='accept-all']",
    "button[aria-label*='Accept all']",
    "button[aria-label*='accept all']",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept cookies')",
    "button:has-text('I agree')",
    "button:has-text('Agree')",
]

NOISE_IDS = (
    "onetrust-consent-sdk", "onetrust-banner-sdk", "onetrust-pc-sdk",
    "cookie-law-info-bar", "gdpr-cookie-notice", "CybotCookiebotDialog",
)

NOISE_TABPANEL_LABELS = {
    "discover", "community", "contact us", "windows insiders",
    "related resources", "more resources",
}

NOISE_CLASSES = {
    "uhf", "c-uhfh", "c-footer", "c-nav", "breadcrumb",
    "feedback", "social", "c-heading-4", "ocr",
}

CUSTOM_EDITOR_CLASSES = {
    "monaco-editor", "codemirror", "cm-editor", "ace_editor",
}

STRUCTURED_TEXT_LIMITS = {
    "code": 4000,
    "table": 2500,
    "definition": 2000,
    "editor": 4000,
    "canvas": 2000,
    "js_text": 1200,
}

# Stage01 is the only place where full-page volume is bounded.  Stage02 and
# stage03 keep their original behavior and consume the bounded stage01 blocks.
STAGE01_MAX_BLOCKS = 420
STAGE01_MAX_TOTAL_TEXT_CHARS = 60_000
STAGE01_MAX_PAGE_OUTPUT_CHARS = 90_000
STAGE01_MIN_TEXT_ALLOCATION = 40


# ── URL helpers ───────────────────────────────────────────────────────────────

def is_platform_url(url: str) -> bool:
    return any(re.search(p, url) for p in PLATFORM_PATTERNS)


def is_xhs_url(url: str) -> bool:
    return any(re.search(p, url) for p in XHS_PATTERNS)


def fetch_video_title(url: str, fallback: str) -> str:
    """Fetch real video title. Tries yt-dlp first, then platform-specific APIs, falls back to slug."""
    import requests as _requests

    # 1. yt-dlp — try browser cookies first, then no-cookie fallback
    _ytdlp_base = [sys.executable, "-m", "yt_dlp", "--js-runtimes", "node", "--no-playlist", "--print", "title"]
    _browsers = ["chrome", "firefox", "edge"]
    for _browser in _browsers:
        try:
            result = subprocess.run(
                _ytdlp_base + ["--cookies-from-browser", _browser, url],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=common.OPERATION_TIMEOUT_SECONDS,
            )
            title = result.stdout.strip().splitlines()[0] if result.returncode == 0 else ""
            if title:
                logger.info("[scrape_page] video title (yt-dlp/%s): %r", _browser, title)
                return title
            if "cookie" in result.stderr.lower() or "could not copy" in result.stderr.lower():
                continue
        except Exception as exc:
            logger.debug("yt-dlp %s failed: %s", _browser, exc)
    try:
        result = subprocess.run(
            _ytdlp_base + [url],
            capture_output=True, text=True, timeout=common.OPERATION_TIMEOUT_SECONDS,
        )
        title = result.stdout.strip().splitlines()[0] if result.returncode == 0 else ""
        if title:
            logger.info("[scrape_page] video title (yt-dlp/no-cookie): %r", title)
            return title
    except Exception as exc:
        logger.debug("yt-dlp no-cookie failed: %s", exc)

    # 2. Bilibili public API (no auth required)
    bvid_match = re.search(r"bilibili\.com/video/(BV[A-Za-z0-9]+)", url)
    if bvid_match:
        try:
            resp = _requests.get(
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid_match.group(1)}",
                headers={"User-Agent": common.STEALTH_UA, "Referer": "https://www.bilibili.com/"},
                timeout=common.OPERATION_TIMEOUT_SECONDS,
            ).json()
            title = resp.get("data", {}).get("title", "")
            if title:
                logger.info("[scrape_page] video title (Bilibili API): %r", title)
                return title
        except Exception as exc:
            logger.warning("[scrape_page] Bilibili API title fetch failed: %s", exc)

    logger.info("[scrape_page] could not fetch title, using fallback: %r", fallback)
    return fallback


# ── DOM helpers ───────────────────────────────────────────────────────────────

def el_text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def is_content_img(img) -> bool:
    src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
    return bool(src) and not src.startswith("data:") and not src.endswith(".svg")


def _best_img_url(img, page_url: str) -> str:
    """Pick the best-resolution URL: srcset last entry, else src/data-src."""
    srcset = img.get("srcset", "")
    if srcset:
        candidates = [p.strip().split()[0] for p in srcset.split(",") if p.strip()]
        if candidates:
            return urljoin(page_url, candidates[-1])
    for attr in ("src", "data-src", "data-lazy-src"):
        val = img.get(attr, "")
        if val and not val.startswith("data:"):
            return urljoin(page_url, val)
    return ""


def resolve_remote_reference(img, soup) -> str:
    for attr in ("aria-describedby", "aria-labelledby"):
        ref_id = img.get(attr, "").strip()
        if ref_id:
            target = soup.find(id=ref_id)
            if target:
                return el_text(target)
    figure = img.find_parent("figure")
    if figure:
        caption = figure.find("figcaption")
        if caption:
            return el_text(caption)
    td = img.find_parent("td")
    if td:
        row = td.find_parent("tr")
        if row:
            sibling_texts = [
                el_text(cell)
                for cell in row.find_all("td")
                if cell is not td and el_text(cell)
            ]
            if sibling_texts:
                return " | ".join(sibling_texts)
    for attr, val in img.attrs.items():
        if attr.startswith("data-") and any(kw in attr for kw in ("caption", "label", "desc", "title")):
            text = str(val).strip()
            if text:
                return text
    return img.get("title", "").strip()


def _build_tabpanel_labels(root) -> dict[str, str]:
    labels: dict[str, str] = {}
    for tab in root.find_all(attrs={"role": "tab"}):
        label = el_text(tab).strip()
        if not label:
            continue
        controls = tab.get("aria-controls", "")
        if controls:
            labels[controls] = label
    for panel in root.find_all(attrs={"role": "tabpanel"}):
        panel_id = panel.get("id", "")
        if panel_id and panel_id not in labels:
            labelledby = panel.get("aria-labelledby", "")
            if labelledby:
                tab_el = root.find(id=labelledby)
                if tab_el:
                    labels[panel_id] = el_text(tab_el).strip()
    return labels


def _tabpanel_info(el, root, tabpanel_labels: dict) -> tuple[str, str]:
    """Return (panel_id, tab_label) for the innermost tabpanel containing el."""
    for parent in el.parents:
        if parent is root:
            break
        if parent.get("role") == "tabpanel":
            panel_id = parent.get("id", "")
            return panel_id, tabpanel_labels.get(panel_id, "")
    return "", ""


def _class_tokens(el) -> set[str]:
    return {str(cls).lower() for cls in el.get("class", [])}


def _is_custom_editor(el) -> bool:
    classes = _class_tokens(el)
    return bool(classes & CUSTOM_EDITOR_CLASSES) or el.get("role") == "code"


def _is_contenteditable(el) -> bool:
    value = el.get("contenteditable")
    return value is not None and str(value).lower() != "false"


def _is_inside_container(el, root, *, include_paragraphs: bool = False) -> bool:
    """Avoid duplicate text from nested code/table/editor structures."""
    container_tags = {"pre", "table", "dl", "textarea"}
    if include_paragraphs:
        container_tags.update({"p", "li"})
    for parent in el.parents:
        if parent is root:
            break
        if parent.name in container_tags or _is_custom_editor(parent):
            return True
    return False


def _table_text(table) -> str:
    rows: list[str] = []
    for row in table.find_all("tr"):
        cells = [el_text(cell) for cell in row.find_all(["th", "td"], recursive=False)]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows) or el_text(table)


def _definition_text(dl) -> str:
    lines: list[str] = []
    current_term = ""
    for item in dl.find_all(["dt", "dd"], recursive=True):
        text = el_text(item)
        if not text:
            continue
        if item.name == "dt":
            if current_term:
                lines.append(current_term)
            current_term = text
        elif current_term:
            lines.append(f"{current_term}: {text}")
            current_term = ""
        else:
            lines.append(text)
    if current_term:
        lines.append(current_term)
    return "\n".join(lines) or el_text(dl)


def _structured_text(el) -> tuple[str, str] | None:
    """Return (format, text) for code, tables, editors, canvas and JS text wrappers."""
    if el.name == "pre":
        return "code", el.get_text("\n", strip=True)
    if el.name == "code":
        return "code", el.get_text("\n", strip=True)
    if el.name == "table":
        return "table", _table_text(el)
    if el.name == "dl":
        return "definition", _definition_text(el)
    if el.name == "textarea" or _is_custom_editor(el) or _is_contenteditable(el):
        text = (
            el.get("data-skill-omni-editor-text", "")
            or el.get("value", "")
            or el.get_text("\n", strip=True)
        )
        return "editor", text
    if el.name == "canvas":
        text = (
            el.get("data-skill-omni-canvas-text", "")
            or el.get("aria-label", "")
            or el.get("title", "")
            or el.get_text(" ", strip=True)
        )
        return "canvas", text
    if el.name in {"div", "section", "article"}:
        return "js_text", el_text(el)
    return None


def _is_leaf_js_text_container(el) -> bool:
    """Capture JS-rendered prose that uses div/span wrappers instead of semantic tags."""
    if el.name not in {"div", "section", "article"}:
        return False
    if _is_custom_editor(el) or _is_contenteditable(el):
        return False
    semantic = [
        "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "img",
        "pre", "code", "table", "dl", "textarea", "canvas",
    ]
    if el.find(semantic):
        return False
    # Prefer the deepest useful wrapper to avoid emitting the same JS text repeatedly.
    for child in el.find_all(["div", "section", "article"], recursive=True):
        if len(el_text(child)) > 15:
            return False
    text = el_text(el)
    return 15 < len(text) <= 5000


# ── Core: build unified blocks[] ─────────────────────────────────────────────

_CANDIDATE_TAG_NAMES = {
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "img",
    "pre", "code", "table", "dl", "textarea", "canvas",
}
_CANDIDATE_ROLES = {"heading", "listitem", "code"}
_STRUCTURED_TAG_NAMES = {"pre", "code", "table", "dl", "textarea", "canvas"}


def _is_candidate_element(el) -> bool:
    return (
        el.name in _CANDIDATE_TAG_NAMES
        or el.get("role") in _CANDIDATE_ROLES
        or _is_contenteditable(el)
        or _is_custom_editor(el)
        or _is_leaf_js_text_container(el)
    )


def _is_structured_element(el) -> bool:
    return (
        el.name in _STRUCTURED_TAG_NAMES
        or _is_custom_editor(el)
        or _is_contenteditable(el)
        or _is_leaf_js_text_container(el)
    )


def build_blocks(soup, page_url: str, source: str) -> list[dict]:
    """Walk DOM in order, output interleaved heading / text / image blocks."""
    root = (
        soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find("article")
        or soup.body
        or soup
    )
    tabpanel_labels = _build_tabpanel_labels(root)
    injected_panels: set[str] = set()
    seen_text: set[str] = set()
    blocks: list[dict] = []

    candidates = [el for el in root.find_all(True, recursive=True) if _is_candidate_element(el)]

    for el in candidates:
        panel_id, tab_label = _tabpanel_info(el, root, tabpanel_labels)

        # Skip elements inside noise tabpanels entirely
        if tab_label and tab_label.lower() in NOISE_TABPANEL_LABELS:
            continue

        # Inject tab label as a level-2 heading on the first element of each content tabpanel
        if panel_id and panel_id not in injected_panels:
            injected_panels.add(panel_id)
            if tab_label:
                blocks.append({"type": "heading", "level": 2, "text": tab_label, "source": source})

        # Skip noise CSS classes
        if any(cls in " ".join(el.get("class", [])) for cls in NOISE_CLASSES):
            continue

        # A structured parent (pre/table/dl/editor) already emits its full text.
        # Keep nested images/canvas, but suppress duplicate nested text elements.
        if el.name not in {"img", "canvas"} and _is_inside_container(el, root):
            continue

        if el.name in ("h1", "h2", "h3", "h4", "h5", "h6") or el.get("role") == "heading":
            text = el_text(el)
            if text and text not in seen_text:
                seen_text.add(text)
                try:
                    level = int(el.name[1]) if el.name.startswith("h") else int(el.get("aria-level", 2))
                except (TypeError, ValueError):
                    level = 2
                level = max(1, min(level, 6))
                blocks.append({"type": "heading", "level": level, "text": text, "source": source})

        elif el.name == "img":
            if not is_content_img(el):
                continue
            url = _best_img_url(el, page_url)
            if not url:
                continue
            alt = el.get("alt", "").strip() or resolve_remote_reference(el, soup)
            blocks.append({"type": "image", "url": url, "alt": alt, "source": source, "path": None})

        elif _is_structured_element(el):
            if el.name != "canvas" and _is_inside_container(el, root, include_paragraphs=(el.name == "code")):
                continue
            structured = _structured_text(el)
            if not structured:
                continue
            fmt, text = structured
            text = text.strip()
            if text and len(text) > 1 and text not in seen_text:
                seen_text.add(text)
                limit = STRUCTURED_TEXT_LIMITS[fmt]
                blocks.append({"type": "text", "text": text[:limit], "format": fmt, "source": source})

        else:
            text = el_text(el)
            if text and len(text) > 15 and text not in seen_text:
                seen_text.add(text)
                blocks.append({"type": "text", "text": text[:400], "source": source})

    return blocks



def _evenly_spaced_indices(indices: list[int], count: int) -> list[int]:
    """Choose indices across the whole page instead of keeping only the prefix."""
    if count <= 0 or not indices:
        return []
    if count >= len(indices):
        return list(indices)
    if count == 1:
        return [indices[0]]
    last = len(indices) - 1
    chosen = {indices[round(i * last / (count - 1))] for i in range(count)}
    # Rounding can collapse adjacent positions; fill any gap deterministically.
    if len(chosen) < count:
        for idx in indices:
            chosen.add(idx)
            if len(chosen) == count:
                break
    return sorted(chosen)


def _limit_stage01_block_count(blocks: list[dict], max_blocks: int) -> list[dict]:
    """Keep structural/image evidence first, then sample remaining blocks globally."""
    if len(blocks) <= max_blocks:
        return [dict(block) for block in blocks]

    priority_groups = [
        [i for i, b in enumerate(blocks) if b.get("type") == "heading" and int(b.get("level", 6)) <= 2],
        [i for i, b in enumerate(blocks) if b.get("type") == "image"],
        [i for i, b in enumerate(blocks) if b.get("type") == "heading" and int(b.get("level", 6)) > 2],
    ]
    selected: set[int] = {0, len(blocks) - 1}

    for group in priority_groups:
        available = max_blocks - len(selected)
        if available <= 0:
            break
        remaining = [idx for idx in group if idx not in selected]
        selected.update(_evenly_spaced_indices(remaining, min(available, len(remaining))))

    available = max_blocks - len(selected)
    if available > 0:
        remaining = [i for i in range(len(blocks)) if i not in selected]
        selected.update(_evenly_spaced_indices(remaining, min(available, len(remaining))))

    return [dict(blocks[i]) for i in sorted(selected)]


def _text_weight(block: dict) -> int:
    if block.get("type") == "heading":
        return 2
    fmt = block.get("format", "text")
    if fmt in {"code", "editor"}:
        return 5
    if fmt in {"table", "definition"}:
        return 4
    if fmt in {"canvas", "js_text"}:
        return 3
    return 2


def _allocate_lengths(lengths: list[int], weights: list[int], budget: int, floor: int) -> list[int]:
    """Weighted water-filling with a small fair minimum for every retained block."""
    if not lengths:
        return []
    total = sum(lengths)
    if total <= budget:
        return list(lengths)
    budget = max(0, budget)
    base = min(floor, budget // len(lengths)) if lengths else 0
    allocated = [min(length, base) for length in lengths]
    remaining = budget - sum(allocated)
    active = {i for i, length in enumerate(lengths) if allocated[i] < length}

    while remaining > 0 and active:
        weight_sum = sum(weights[i] for i in active)
        progressed = False
        for i in list(active):
            share = max(1, remaining * weights[i] // max(1, weight_sum))
            add = min(share, lengths[i] - allocated[i], remaining)
            if add > 0:
                allocated[i] += add
                remaining -= add
                progressed = True
            if allocated[i] >= lengths[i]:
                active.discard(i)
            if remaining <= 0:
                break
        if not progressed:
            break
    return allocated


def _compact_stage01_text(blocks: list[dict], max_text_chars: int) -> list[dict]:
    result = [dict(block) for block in blocks]
    text_positions = [
        i for i, block in enumerate(result)
        if block.get("type") in {"heading", "text"} and isinstance(block.get("text"), str)
    ]
    lengths = [len(result[i].get("text", "")) for i in text_positions]
    if sum(lengths) <= max_text_chars:
        return result

    weights = [_text_weight(result[i]) for i in text_positions]
    allocations = _allocate_lengths(
        lengths,
        weights,
        max_text_chars,
        STAGE01_MIN_TEXT_ALLOCATION,
    )
    for pos, allowed in zip(text_positions, allocations):
        original = result[pos].get("text", "")
        if allowed >= len(original):
            continue
        if allowed <= 1:
            result[pos]["text"] = original[:allowed]
        elif allowed <= 4:
            result[pos]["text"] = original[:allowed]
        else:
            result[pos]["text"] = original[: allowed - 2].rstrip() + " …"
    return result


def _serialized_chars(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, indent=2))


def build_bounded_stage01_payload(
    *,
    url: str,
    slug: str,
    title: str,
    blocks: list[dict],
    video_urls: list[str],
) -> dict:
    """Build one bounded stage01 file; no paging or continuation state is created."""
    original_block_count = len(blocks)
    original_text_chars = sum(
        len(block.get("text", ""))
        for block in blocks
        if block.get("type") in {"heading", "text"}
    )

    retained = _limit_stage01_block_count(blocks, STAGE01_MAX_BLOCKS)
    retained = _compact_stage01_text(retained, STAGE01_MAX_TOTAL_TEXT_CHARS)

    def make_payload(current_blocks: list[dict]) -> dict:
        stored_text_chars = sum(
            len(block.get("text", ""))
            for block in current_blocks
            if block.get("type") in {"heading", "text"}
        )
        return {
            "url": url,
            "slug": slug,
            "title": title,
            "blocks": current_blocks,
            "video_urls": video_urls,
            "content_limits": {
                "original_blocks": original_block_count,
                "stored_blocks": len(current_blocks),
                "original_text_chars": original_text_chars,
                "stored_text_chars": stored_text_chars,
                "max_blocks": STAGE01_MAX_BLOCKS,
                "max_total_text_chars": STAGE01_MAX_TOTAL_TEXT_CHARS,
                "max_page_output_chars": STAGE01_MAX_PAGE_OUTPUT_CHARS,
                "truncated": (
                    len(current_blocks) < original_block_count
                    or stored_text_chars < original_text_chars
                ),
            },
        }

    payload = make_payload(retained)
    # Account for JSON keys, URLs, alt text and indentation, not only block text.
    if _serialized_chars(payload) > STAGE01_MAX_PAGE_OUTPUT_CHARS:
        empty_text_blocks = [
            {**block, "text": ""} if block.get("type") in {"heading", "text"} else dict(block)
            for block in retained
        ]
        non_text_overhead = _serialized_chars(make_payload(empty_text_blocks))
        adjusted_text_budget = max(
            1_000,
            STAGE01_MAX_PAGE_OUTPUT_CHARS - non_text_overhead - 1_000,
        )
        retained = _compact_stage01_text(retained, adjusted_text_budget)
        payload = make_payload(retained)

    # Extremely metadata-heavy pages may still exceed the serialized cap. Reduce
    # the retained block set globally, never by repeatedly exposing later batches.
    while _serialized_chars(payload) > STAGE01_MAX_PAGE_OUTPUT_CHARS and len(retained) > 1:
        next_count = max(1, int(len(retained) * 0.9))
        if next_count >= len(retained):
            next_count = len(retained) - 1
        retained = _limit_stage01_block_count(retained, next_count)
        empty_text_blocks = [
            {**block, "text": ""} if block.get("type") in {"heading", "text"} else dict(block)
            for block in retained
        ]
        non_text_overhead = _serialized_chars(make_payload(empty_text_blocks))
        adjusted_text_budget = max(
            0,
            STAGE01_MAX_PAGE_OUTPUT_CHARS - non_text_overhead - 1_000,
        )
        retained = _compact_stage01_text(retained, adjusted_text_budget)
        payload = make_payload(retained)

    payload["content_limits"]["serialized_chars"] = _serialized_chars(payload)
    return payload


def parse_page_html(html: str, page_url: str, source: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for noise_id in NOISE_IDS:
        el = soup.find(id=noise_id)
        if el:
            el.decompose()
    return build_blocks(soup, page_url, source)


# ── Video detection ───────────────────────────────────────────────────────────

def detect_video_urls_from_html(html: str) -> list[str]:
    video_urls = []
    for match in re.finditer(r"youtube\.com/embed/([A-Za-z0-9_-]+)", html):
        video_urls.append(f"https://www.youtube.com/watch?v={match.group(1)}")
    for match in re.finditer(r"bilibili\.com/video/(BV[A-Za-z0-9]+)", html):
        video_urls.append(f"https://www.bilibili.com/video/{match.group(1)}")
    for match in re.finditer(r"aid=(\d+)", html):
        video_urls.append(f"https://www.bilibili.com/video/av{match.group(1)}")
    for match in re.finditer(r"player\.vimeo\.com/video/(\d+)", html):
        video_urls.append(f"https://vimeo.com/{match.group(1)}")
    return list(dict.fromkeys(video_urls))


# ── Playwright scraping ───────────────────────────────────────────────────────

async def scrape_one_page(page, page_url: str, dismiss_cookie: bool = False) -> tuple[str, list[str]]:
    """Return HTML and embedded video URLs for the user-provided page only."""
    html = ""

    try:
        # Capture text drawn through the common 2D canvas APIs before page scripts run.
        # Bitmap-only/WebGL canvas still has no recoverable DOM text, but accessible
        # labels and normal fillText/strokeText calls are preserved for extraction.
        await page.add_init_script("""
            (() => {
              const patch = (name) => {
                const proto = window.CanvasRenderingContext2D && window.CanvasRenderingContext2D.prototype;
                if (!proto || typeof proto[name] !== 'function' || proto[name].__skillOmniPatched) return;
                const original = proto[name];
                const wrapped = function(text, ...args) {
                  try {
                    const canvas = this.canvas;
                    const value = String(text ?? '').trim();
                    if (canvas && value) {
                      const old = canvas.getAttribute('data-skill-omni-canvas-text') || '';
                      const parts = old ? old.split('\\n') : [];
                      if (!parts.includes(value)) {
                        canvas.setAttribute('data-skill-omni-canvas-text', [...parts, value].join('\\n').slice(0, 10000));
                      }
                    }
                  } catch (_) {}
                  return original.call(this, text, ...args);
                };
                wrapped.__skillOmniPatched = true;
                proto[name] = wrapped;
              };
              patch('fillText');
              patch('strokeText');
            })();
        """)
        # "commit" fires on the very first response byte — the earliest possible
        # signal. Sites like REI hang connections for "load"/"domcontentloaded"
        # when they detect a headless browser, but typically still send HTML bytes.
        resp = await page.goto(page_url, wait_until="commit", timeout=common.OPERATION_TIMEOUT_SECONDS * 1000)
        if resp and resp.status >= 400:
            logger.warning("      [scrape] HTTP %d for %s", resp.status, page_url)
            return html, []
        # Give JS time to run lazy-load initialization before we start scrolling
        await page.wait_for_timeout(2000)

        if dismiss_cookie:
            for selector in COOKIE_SELECTORS:
                try:
                    button = page.locator(selector).first
                    if await button.is_visible(timeout=common.OPERATION_TIMEOUT_SECONDS * 1000):
                        await button.click()
                        logger.info("      [scrape] Dismissed cookie banner (%s)", selector)
                        await page.wait_for_timeout(1500)
                        break
                except Exception as exc:
                    logger.debug("Cookie dismiss failed for %s: %s", selector, exc)
                    continue

        # Incremental scroll to trigger lazy-loaded images (scroll in viewport-sized
        # steps so images enter the viewport and their src gets populated)
        total_height = await page.evaluate("() => document.body.scrollHeight")
        scroll_step = 600
        pos = 0
        while pos < total_height:
            pos = min(pos + scroll_step, total_height)
            await page.evaluate(f"window.scrollTo(0, {pos})")
            await page.wait_for_timeout(300)
        await page.wait_for_timeout(1000)

        # Snapshot live editor values into serializable data attributes. page.content()
        # otherwise misses values held only in JS state (Monaco/CodeMirror/Ace/textarea).
        await page.evaluate("""
            () => {
              const selectors = [
                '.monaco-editor', '.CodeMirror', '.cm-editor', '.ace_editor',
                '[role="code"]', '[contenteditable]:not([contenteditable="false"])', 'textarea'
              ];
              for (const el of document.querySelectorAll(selectors.join(','))) {
                let text = '';
                if (el.matches('textarea')) text = el.value || el.textContent || '';
                if (!text) {
                  const textarea = el.querySelector('textarea');
                  text = textarea?.value || '';
                }
                if (!text && el.matches('.ace_editor')) {
                  text = [...el.querySelectorAll('.ace_line')].map(x => x.textContent || '').join('\\n');
                }
                if (!text) text = el.innerText || el.textContent || '';
                text = text.trim();
                if (text) el.setAttribute('data-skill-omni-editor-text', text.slice(0, 20000));
              }
            }
        """)
        html = await page.content()

        return html, detect_video_urls_from_html(html)

    except Exception as exc:
        logger.warning("      [scrape] Playwright error for %s: %s", page_url, exc)
        return html, []


async def scrape_page_playwright(page_url: str) -> tuple[list[dict], list[str], str]:
    """Scrape only the exact URL supplied by the user; never follow page links."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                # Force HTTP/1.1 — some sites (e.g. REI) reject Playwright's H2
                # fingerprint with ERR_HTTP2_PROTOCOL_ERROR
                "--disable-http2",
            ],
        )
        context = await browser.new_context(
            user_agent=common.STEALTH_UA,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        page = await context.new_page()
        if _has_stealth and _Stealth:
            await _Stealth().apply_stealth_async(page)
        try:
            html, video_urls = await scrape_one_page(page, page_url, dismiss_cookie=True)
            page_title = await page.title()
        finally:
            await page.close()
            await context.close()
            await browser.close()

    blocks = parse_page_html(html, page_url, "main") if html else []

    # Deduplicate image blocks by URL, preserving first occurrence and context.
    seen_img_urls: set[str] = set()
    deduped: list[dict] = []
    for block in blocks:
        if block["type"] == "image":
            if block["url"] in seen_img_urls:
                continue
            seen_img_urls.add(block["url"])
        deduped.append(block)

    video_urls = list(dict.fromkeys(video_urls))
    if is_platform_url(page_url) and page_url not in video_urls:
        video_urls.insert(0, page_url)

    return deduped, video_urls, page_title


def scrape_page(url: str) -> tuple[list[dict], list[str], str]:
    return asyncio.run(scrape_page_playwright(url))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 1 (new): scrape page into unified blocks[].")
    parser.add_argument("url", nargs="?")
    parser.add_argument("slug", nargs="?", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="Run the shared environment gate with auto-repair, then exit.",
    )
    args = parser.parse_args()

    if args.check_deps:
        try:
            _load_runtime_dependencies(require_web=True)
        except EnvironmentGateError:
            sys.exit(2)
        logger.info("[scrape_page] DEPENDENCIES_OK: %s", Path(sys.executable).resolve())
        return
    if not args.url:
        parser.error("url is required unless --check-deps is used")

    require_web = not is_platform_url(args.url)
    try:
        _load_runtime_dependencies(require_web=require_web)
    except EnvironmentGateError:
        sys.exit(2)

    if args.slug:
        slug = args.slug
    else:
        base_slug = common.url_to_slug(args.url)
        slug = base_slug
        counter = 2
        while common.work_path(slug, "stage01.json").exists():
            slug = f"{base_slug}_v{counter}"
            counter += 1
    out = Path(args.out) if args.out else common.work_path(slug, "stage01.json")

    # Pure video platform URLs (B站/YouTube/Vimeo): skip scraping entirely.
    # These pages have no useful text/image content for the pipeline;
    # stage_04b handles the video directly via yt-dlp.
    if is_platform_url(args.url):
        title = fetch_video_title(args.url, slug.replace("_", " "))
        common.write_json(out, {
            "url": args.url,
            "slug": slug,
            "title": title,
            "blocks": [],
            "video_urls": [args.url],
        })
        logger.info("[scrape_page] video platform URL — skipping scrape, handing off to stage_04b: %s", args.url)
        return

    # XHS URLs (xiaohongshu.com / xhslink.com): probe with yt-dlp first.
    # If yt-dlp finds a real title, it's a video post → hand off to stage_04b.
    # Otherwise it's an image/text post → fall through to Playwright scrape.
    if is_xhs_url(args.url):
        fallback = slug.replace("_", " ")
        title = fetch_video_title(args.url, fallback)
        if title != fallback:
            common.write_json(out, {
                "url": args.url,
                "slug": slug,
                "title": title,
                "blocks": [],
                "video_urls": [args.url],
            })
            logger.info("[scrape_page] XHS video confirmed: %r — handing off to stage_04b", title)
            return
        logger.info("[scrape_page] XHS URL appears to be image/text post — proceeding with Playwright scrape")

    try:
        blocks, video_urls, page_title = scrape_page(args.url)
    except Exception as exc:
        message = str(exc).lower()
        logger.error("[scrape_page] Playwright startup failed: %s", exc)
        logger.error(
            "[scrape_page] Environment gate had passed; this is a runtime "
            "browser/page failure, not a missing-package fallback."
        )
        raise SystemExit(4) from exc

    _blocked_markers = ("the request is blocked", "access denied", "403 forbidden", "enable javascript")
    img_blocks = [b for b in blocks if b["type"] == "image"]
    text_content = " ".join(b["text"].lower() for b in blocks if b["type"] in ("heading", "text"))
    _is_blocked = not img_blocks and any(m in text_content for m in _blocked_markers)

    if not blocks or _is_blocked:
        logger.warning("[scrape_page] WARNING: Playwright returned empty/blocked content for %s", args.url)
        logger.warning("[scrape_page] Tip: use web_fetch_webpage in the agent to fetch raw text as fallback")

    payload = build_bounded_stage01_payload(
        url=args.url,
        slug=slug,
        title=page_title,
        blocks=blocks,
        video_urls=video_urls,
    )
    bounded_blocks = payload["blocks"]
    img_count = sum(1 for b in bounded_blocks if b["type"] == "image")
    common.write_json(out, payload)
    limits = payload["content_limits"]
    logger.info(
        "[scrape_page] wrote %s: %d/%d blocks (%d images), %d/%d text chars, serialized=%d, title: %r",
        out,
        limits["stored_blocks"],
        limits["original_blocks"],
        img_count,
        limits["stored_text_chars"],
        limits["original_text_chars"],
        limits["serialized_chars"],
        page_title,
    )


if __name__ == "__main__":
    main()
