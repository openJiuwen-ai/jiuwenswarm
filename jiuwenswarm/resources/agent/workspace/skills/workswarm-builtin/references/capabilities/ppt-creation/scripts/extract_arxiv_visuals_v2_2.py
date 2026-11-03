#!/usr/bin/env python3
"""
Cross-platform paper figure/table extractor.

Input:
  * arXiv abs/pdf URL
  * arXiv ID
  * generic HTTP(S) PDF URL
  * local PDF path

Output:
  source/paper.pdf
  figures/*.png
  figures_with_caption/*.png
  tables/*.png
  tables_with_caption/*.png
  pages/*.png                    (optional: --keep-pages)
  debug/*.png
  manifest.json
  contact_sheet.png

The script is intentionally Python-only: no Bash, curl, Java, Git, or WSL is
required. By default it installs missing Python packages into the current
Python environment. Use --no-auto-install to disable that behavior.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

SCRIPT_VERSION = "2.2.0"
DEFAULT_DPI = 300
DEFAULT_DEBUG_DPI = 120
DEPENDENCIES = {
    "pymupdf": "PyMuPDF>=1.24,<2",
    "PIL": "Pillow>=10,<13",
}


LOGGER = logging.getLogger("extract_arxiv_visuals")

# Configured at import time, not in main(): ensure_dependencies() below runs
# during module import and logs there. Diagnostics keep their historical stderr
# destination and exact wording; only the transport changes from print.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")


# Program output (report bodies, --json payloads) goes to stdout, diagnostics
# to stderr. Both travel through logging; this logger owns stdout, keeps a bare
# "%(message)s" format so the text is unchanged, and does not propagate so the
# stderr root handler never sees it.
STDOUT_LOGGER = logging.getLogger("extract_arxiv_visuals.stdout")
STDOUT_LOGGER.propagate = False
STDOUT_LOGGER.setLevel(logging.INFO)
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter("%(message)s"))
STDOUT_LOGGER.addHandler(_stdout_handler)


def emit(line: str) -> None:
    """Final summary is program output, not a diagnostic."""
    STDOUT_LOGGER.info(line)


def eprint(*args: object) -> None:
    LOGGER.info(" ".join(str(arg) for arg in args))


def log(message: str) -> None:
    eprint(f"[extract_arxiv_visuals] {message}")


def warn(message: str) -> None:
    eprint(f"[extract_arxiv_visuals WARNING] {message}")


def ensure_dependencies(auto_install: bool) -> None:
    missing: list[str] = []
    for module_name, pip_spec in DEPENDENCIES.items():
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(pip_spec)

    if not missing:
        return

    if not auto_install:
        joined = " ".join(missing)
        raise RuntimeError(
            "缺少 Python 依赖。请运行：\n"
            f'  "{sys.executable}" -m pip install {joined}'
        )

    log("检测到缺失依赖，正在安装：" + ", ".join(missing))
    command = [sys.executable, "-m", "pip", "install", *missing]
    try:
        subprocess.check_call(command)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "自动安装依赖失败。请手动运行：\n  " + " ".join(command)
        ) from exc
    importlib.invalidate_caches()


# Parse this flag before importing third-party packages.
AUTO_INSTALL = "--no-auto-install" not in sys.argv
ensure_dependencies(AUTO_INSTALL)

import pymupdf  # type: ignore  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # type: ignore  # noqa: E402


ARXIV_ID_RE = re.compile(r"^(?P<id>\d{4}\.\d{4,5}(?:v\d+)?)$")
ARXIV_URL_RE = re.compile(
    r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/"
    r"(?P<id>\d{4}\.\d{4,5}(?:v\d+)?)(?:\.pdf)?(?:[?#].*)?$",
    re.IGNORECASE,
)
CAPTION_RE = re.compile(
    r"^\s*(?P<kind>fig(?:ure)?|table)\s*\.?\s*"
    r"(?P<label>(?:[A-Za-z]?\d+(?:[.\-]\d+)*[A-Za-z]?|[IVXLCDM]+))\b",
    re.IGNORECASE,
)
INLINE_CAPTION_RE = re.compile(
    r"\b(?P<kind>fig(?:ure)?|table)\s*\.?\s*"
    r"(?P<label>(?:[A-Za-z]?\d+(?:[.\-]\d+)*[A-Za-z]?|[IVXLCDM]+))"
    r"\s*(?:\||:)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class InputResource:
    original_input: str
    source_kind: str
    paper_id: str
    pdf_url: str | None = None
    local_pdf: Path | None = None


@dataclass
class Candidate:
    kind: str
    label: str
    page_index: int
    region_bbox: tuple[float, float, float, float]
    caption_bbox: tuple[float, float, float, float] | None
    caption: str
    source: str
    confidence: float


@dataclass
class ExtractedVisual:
    id: str
    type: str
    label: str
    page_index: int
    page_number: int
    bbox: list[float]
    caption_bbox: list[float] | None
    caption: str
    image_path: str
    image_with_caption_path: str
    extraction_method: str
    confidence: float
    pixel_width: int
    pixel_height: int


def sanitize_name(value: str, fallback: str = "paper") -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("_")
    return clean or fallback


def safe_label(value: str) -> str:
    return sanitize_name(value, "unlabeled")


def resolve_input(value: str) -> InputResource:
    value = value.strip().strip('"').strip("'")
    local = Path(value).expanduser()
    if local.is_file():
        if local.suffix.lower() != ".pdf":
            raise ValueError(f"本地文件不是 PDF：{local}")
        return InputResource(
            original_input=value,
            source_kind="local_pdf",
            paper_id=sanitize_name(local.stem),
            local_pdf=local.resolve(),
        )

    match = ARXIV_ID_RE.match(value)
    if match:
        arxiv_id = match.group("id")
        return InputResource(
            original_input=value,
            source_kind="arxiv",
            paper_id=arxiv_id,
            pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        )

    match = ARXIV_URL_RE.match(value)
    if match:
        arxiv_id = match.group("id")
        return InputResource(
            original_input=value,
            source_kind="arxiv",
            paper_id=arxiv_id,
            pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        )

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        filename = Path(urllib.parse.unquote(parsed.path)).name
        paper_id = sanitize_name(Path(filename).stem if filename else "paper")
        return InputResource(
            original_input=value,
            source_kind="remote_pdf",
            paper_id=paper_id,
            pdf_url=value,
        )

    raise ValueError(
        "输入必须是 arXiv ID、arXiv abs/pdf 链接、HTTP(S) PDF 链接或本地 PDF 路径。"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_local_pdf(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def download_pdf(url: str, destination: Path, retries: int = 3) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "User-Agent": (
            "paper-visual-extractor/2.0 "
            "(cross-platform research workflow; contact: local-user)"
        ),
        "Accept": "application/pdf,*/*;q=0.8",
    }
    request = urllib.request.Request(url, headers=headers)
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        temp_path = destination.with_suffix(destination.suffix + ".part")
        try:
            log(f"下载 PDF（第 {attempt}/{retries} 次）：{url}")
            with urllib.request.urlopen(request, timeout=120) as response:
                with temp_path.open("wb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            with temp_path.open("rb") as handle:
                signature = handle.read(5)
            if signature != b"%PDF-":
                raise ValueError("下载内容不是 PDF，可能是 HTML 错误页或受限页面。")
            temp_path.replace(destination)
            return
        # OSError already covers urllib.error.URLError and TimeoutError; listing
        # them alongside it trips the "parent and child exception" rule.
        except (OSError, ValueError) as exc:
            last_error = exc
            temp_path.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"PDF 下载失败：{url}\n原因：{last_error}")


def prepare_output(output: Path, force: bool, keep_pages: bool) -> None:
    output = output.resolve()
    if output.exists():
        if not force:
            raise FileExistsError(
                f"输出目录已经存在：{output}\n请更换 --output，或加入 --force 覆盖。"
            )
        log(f"删除已有输出目录：{output}")
        shutil.rmtree(output)

    directories = [
        "source",
        "figures",
        "figures_with_caption",
        "tables",
        "tables_with_caption",
        "debug",
        "raw",
    ]
    if keep_pages:
        directories.append("pages")
    for name in directories:
        (output / name).mkdir(parents=True, exist_ok=True)


def rect_tuple(obj: Any) -> tuple[float, float, float, float] | None:
    if obj is None:
        return None
    if isinstance(obj, pymupdf.Rect):
        return float(obj.x0), float(obj.y0), float(obj.x1), float(obj.y1)
    if isinstance(obj, (list, tuple)) and len(obj) == 4:
        return tuple(float(value) for value in obj)  # type: ignore[return-value]
    if isinstance(obj, dict):
        variants = [
            ("x1", "y1", "x2", "y2"),
            ("x0", "y0", "x1", "y1"),
            ("left", "top", "right", "bottom"),
        ]
        for keys in variants:
            if all(key in obj for key in keys):
                return tuple(float(obj[key]) for key in keys)  # type: ignore[return-value]
    return None


def normalize_rect(
    rect: Sequence[float],
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = map(float, rect)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def union_rect(
    *rects: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    valid = [normalize_rect(rect) for rect in rects if rect is not None]
    if not valid:
        return None
    return (
        min(rect[0] for rect in valid),
        min(rect[1] for rect in valid),
        max(rect[2] for rect in valid),
        max(rect[3] for rect in valid),
    )


def area(rect: Sequence[float]) -> float:
    x0, y0, x1, y1 = normalize_rect(rect)
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def intersection_area(a: Sequence[float], b: Sequence[float]) -> float:
    ax0, ay0, ax1, ay1 = normalize_rect(a)
    bx0, by0, bx1, by1 = normalize_rect(b)
    x0, y0 = max(ax0, bx0), max(ay0, by0)
    x1, y1 = min(ax1, bx1), min(ay1, by1)
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    inter = intersection_area(a, b)
    if inter <= 0:
        return 0.0
    return inter / max(area(a) + area(b) - inter, 1e-9)


def horizontal_overlap_ratio(a: Sequence[float], b: Sequence[float]) -> float:
    ax0, _, ax1, _ = normalize_rect(a)
    bx0, _, bx1, _ = normalize_rect(b)
    overlap = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    return overlap / max(1.0, min(ax1 - ax0, bx1 - bx0))


def axis_gap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Return the non-overlapping gap between two 1D intervals."""
    return max(0.0, b0 - a1, a0 - b1)


def looks_like_page_margin_stamp(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    text: str,
) -> bool:
    """Detect narrow, tall marginal stamps such as vertical arXiv identifiers.

    These are page decorations, not part of a figure. The proximity check in
    infer_figure_region still allows legitimate axis labels that sit close to
    the detected graphics.
    """
    page_width = max(page.rect.width, 1.0)
    page_height = max(page.rect.height, 1.0)
    near_side = (
        rect.x0 <= page.rect.x0 + page_width * 0.09
        or rect.x1 >= page.rect.x1 - page_width * 0.09
    )
    narrow_and_tall = (
        rect.height >= max(70.0, rect.width * 3.0)
        and rect.width <= page_width * 0.10
        and rect.height >= page_height * 0.12
    )
    stamp_like_text = bool(
        re.search(r"(?:arxiv|\[[a-z.-]+\]|\d{4}\.\d{4,5}v?\d*)", text, re.I)
    )
    return near_side and narrow_and_tall and stamp_like_text


def sanitize_margin_artifacts(
    page: pymupdf.Page,
    bbox: Sequence[float],
) -> tuple[float, float, float, float]:
    """Remove page-edge stamps from a candidate crop before rendering.

    This runs for every detector path, including optional PDFFigures2 JSON.
    Earlier versions only prevented the caption heuristic from *adding* the
    arXiv stamp, but did not repair a bbox that already contained it.
    """
    rect = pymupdf.Rect(*normalize_rect(bbox))
    page_width = max(page.rect.width, 1.0)
    page_height = max(page.rect.height, 1.0)

    for block_rect, text in page_blocks(page):
        if not looks_like_page_margin_stamp(page, block_rect, text):
            continue

        # Only alter a crop when the stamp overlaps its vertical span and is
        # actually included at the corresponding side edge.
        vertical_overlap = max(
            0.0, min(rect.y1, block_rect.y1) - max(rect.y0, block_rect.y0)
        )
        if vertical_overlap <= 0:
            continue

        if block_rect.x0 <= page.rect.x0 + page_width * 0.10:
            if rect.x0 <= block_rect.x1 + 2.0 and block_rect.x1 < rect.x1:
                rect.x0 = min(rect.x1 - 2.0, block_rect.x1 + 3.0)
        elif block_rect.x1 >= page.rect.x1 - page_width * 0.10:
            if rect.x1 >= block_rect.x0 - 2.0 and block_rect.x0 > rect.x0:
                rect.x1 = max(rect.x0 + 2.0, block_rect.x0 - 3.0)

    # Safety clamp: a very wide figure crop should not start in the outermost
    # page gutter when a vertical stamp exists there. This leaves normal
    # single-column figures unchanged.
    has_left_stamp = any(
        looks_like_page_margin_stamp(page, block_rect, text)
        and block_rect.x0 <= page.rect.x0 + page_width * 0.10
        and max(0.0, min(rect.y1, block_rect.y1) - max(rect.y0, block_rect.y0)) > 0
        for block_rect, text in page_blocks(page)
    )
    if has_left_stamp and rect.width >= page_width * 0.60:
        rect.x0 = max(rect.x0, page.rect.x0 + page_width * 0.085)

    rect = rect & page.rect
    if rect.is_empty or rect.width <= 2 or rect.height <= 2:
        return normalize_rect(bbox)
    return normalize_rect(tuple(rect))


def trim_white_border(
    path: Path,
    threshold: int = 250,
    border: int = 2,
) -> tuple[int, int]:
    """Trim only near-white outer whitespace from a rendered PNG.

    The threshold is deliberately conservative so light-gray table rules and
    pale panels remain intact. It cannot remove internal white space.
    """
    with Image.open(path) as source:
        image = source.convert("RGB")
        pixels = image.load()
        width, height = image.size
        xs: list[int] = []
        ys: list[int] = []
        for y in range(height):
            for x in range(width):
                r, g, b = pixels[x, y]
                if min(r, g, b) < threshold:
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return width, height
        left = max(0, min(xs) - border)
        top = max(0, min(ys) - border)
        right = min(width, max(xs) + border + 1)
        bottom = min(height, max(ys) + border + 1)
        if (left, top, right, bottom) == (0, 0, width, height):
            return width, height
        cropped = image.crop((left, top, right, bottom))
        cropped.save(path)
        return cropped.size


def page_blocks(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, str]]:
    blocks: list[tuple[pymupdf.Rect, str]] = []
    for block in page.get_text("blocks", sort=True):
        x0, y0, x1, y1, text, *_ = block
        compact = " ".join(str(text).split())
        if compact:
            blocks.append((pymupdf.Rect(x0, y0, x1, y1), compact))
    return blocks


def parse_caption(text: str) -> tuple[str, str] | None:
    match = CAPTION_RE.match(text) or INLINE_CAPTION_RE.search(text)
    if not match:
        return None
    kind = "table" if match.group("kind").lower().startswith("table") else "figure"
    return kind, match.group("label")


def column_bounds(page: pymupdf.Page, caption: pymupdf.Rect) -> tuple[float, float]:
    page_width = page.rect.width
    mid = (page.rect.x0 + page.rect.x1) / 2
    crosses_mid_substantially = (
        caption.x0 < mid - page_width * 0.04
        and caption.x1 > mid + page_width * 0.04
    )
    if caption.width >= page_width * 0.68 or crosses_mid_substantially:
        return page.rect.x0, page.rect.x1
    center = (caption.x0 + caption.x1) / 2
    margin = page_width * 0.015
    if center < mid:
        return page.rect.x0 + margin, mid - margin
    return mid + margin, page.rect.x1 - margin


def graphical_regions(page: pymupdf.Page) -> list[tuple[float, float, float, float]]:
    regions: list[tuple[float, float, float, float]] = []
    # Both probes are best-effort enrichment: PyMuPDF raises on malformed or
    # unusual content streams, and one failing probe must not lose the other's
    # regions. Report which one dropped out instead of swallowing it silently.
    try:
        regions.extend(normalize_rect(rect) for rect in page.cluster_drawings())
    except Exception as cause:
        log(f"page {page.number}: cluster_drawings unavailable ({cause})")
    try:
        for info in page.get_image_info():
            bbox = rect_tuple(info.get("bbox"))
            if bbox is not None:
                regions.append(normalize_rect(bbox))
    except Exception as cause:
        log(f"page {page.number}: get_image_info unavailable ({cause})")
    return [rect for rect in regions if area(rect) >= 350]


def nearby_caption_for_table(
    page: pymupdf.Page,
    bbox: tuple[float, float, float, float],
) -> tuple[str, tuple[float, float, float, float] | None, str]:
    best_distance = math.inf
    best_label = ""
    best_caption = ""
    best_bbox: tuple[float, float, float, float] | None = None
    for block_rect, text in page_blocks(page):
        parsed = parse_caption(text)
        if not parsed or parsed[0] != "table":
            continue

        # Academic styles vary: some place table captions above, others below.
        caption_above_gap = bbox[1] - block_rect.y1
        caption_below_gap = block_rect.y0 - bbox[3]
        valid_distances = [
            gap
            for gap in (caption_above_gap, caption_below_gap)
            if -8 <= gap <= 260
        ]
        if not valid_distances:
            continue
        if horizontal_overlap_ratio(bbox, tuple(block_rect)) <= 0.12:
            continue
        distance = min(valid_distances)
        if distance < best_distance:
            best_distance = distance
            best_label = parsed[1]
            best_caption = text
            best_bbox = normalize_rect(tuple(block_rect))
    return best_label, best_bbox, best_caption


def find_table_candidates(
    doc: pymupdf.Document,
    include_uncaptioned: bool,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    fallback_counter = 0
    for page_index, page in enumerate(doc):
        tables: list[Any] = []
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                finder = page.find_tables()
            tables.extend(finder.tables)
        except Exception as exc:
            warn(f"第 {page_index + 1} 页表格检测失败：{exc}")

        for table in tables:
            bbox = normalize_rect(table.bbox)
            if area(bbox) < 900:
                continue
            if any(
                old.page_index == page_index and iou(old.region_bbox, bbox) > 0.55
                for old in candidates
            ):
                continue
            label, caption_bbox, caption = nearby_caption_for_table(page, bbox)
            if not label and not include_uncaptioned:
                continue
            fallback_counter += 1
            candidates.append(
                Candidate(
                    kind="table",
                    label=label or f"fallback_{fallback_counter:03d}",
                    page_index=page_index,
                    region_bbox=bbox,
                    caption_bbox=caption_bbox,
                    caption=caption,
                    source="pymupdf_find_tables",
                    confidence=0.82 if label else 0.66,
                )
            )
    return candidates


def infer_figure_region(
    page: pymupdf.Page,
    caption_rect: pymupdf.Rect,
    blocks: list[tuple[pymupdf.Rect, str]],
    graphics: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    left, right = column_bounds(page, caption_rect)
    page_height = page.rect.height

    compatible_graphics = []
    for graphic in graphics:
        gx0, gy0, gx1, gy1 = graphic
        if gy1 > caption_rect.y0 + 8:
            continue
        gap = caption_rect.y0 - gy1
        if gap > page_height * 0.56:
            continue
        if gx1 <= left or gx0 >= right:
            continue
        compatible_graphics.append(graphic)

    if compatible_graphics:
        compatible_graphics.sort(key=lambda rect: (caption_rect.y0 - rect[3], -area(rect)))
        seed = compatible_graphics[0]
        selected = [seed]
        current_top = seed[1]
        current_bottom = seed[3]
        for graphic in compatible_graphics[1:]:
            vertical_gap = current_top - graphic[3]
            if vertical_gap <= page_height * 0.055:
                selected.append(graphic)
                current_top = min(current_top, graphic[1])
                current_bottom = max(current_bottom, graphic[3])

        merged = union_rect(*selected)
        if merged is None:
            raise RuntimeError("union_rect returned None for a non-empty selection")
        x0 = max(left, merged[0])
        x1 = min(right, merged[2])
        y0 = max(page.rect.y0, merged[1])
        # Do not extend the crop toward the caption merely to fill whitespace.
        # Actual labels are added below only when they are spatially close to
        # the detected graphical region.
        y1 = min(caption_rect.y0 - 1, merged[3])

        # Include short labels / legends only when they are genuinely adjacent
        # to the visual. This prevents vertical arXiv stamps and page headers
        # from enlarging a full-width figure crop.
        base_graphic = normalize_rect(merged)
        max_horizontal_gap = max(10.0, page.rect.width * 0.025)
        max_vertical_gap = max(3.0, page.rect.height * 0.004)
        for block_rect, text in blocks:
            if block_rect.y1 < y0 - max_vertical_gap or block_rect.y0 > y1 + max_vertical_gap:
                continue
            if block_rect.x1 <= left or block_rect.x0 >= right:
                continue
            if len(text) > 120 or parse_caption(text):
                continue

            h_gap = axis_gap(block_rect.x0, block_rect.x1, base_graphic[0], base_graphic[2])
            v_gap = axis_gap(block_rect.y0, block_rect.y1, base_graphic[1], base_graphic[3])
            if h_gap > max_horizontal_gap or v_gap > max_vertical_gap:
                continue
            if looks_like_page_margin_stamp(page, block_rect, text) and h_gap > 2.0:
                continue

            x0 = min(x0, max(left, block_rect.x0))
            x1 = max(x1, min(right, block_rect.x1))
            y0 = min(y0, block_rect.y0)
            y1 = max(y1, min(caption_rect.y0 - 1, block_rect.y1))
        region = normalize_rect((x0, y0, x1, y1))
        return region if area(region) >= 1000 else None

    # Fallback: use the band between the nearest preceding body paragraph and caption.
    upper = page.rect.y0
    for block_rect, text in blocks:
        if block_rect.y1 > caption_rect.y0:
            break
        if block_rect.x1 <= left or block_rect.x0 >= right:
            continue
        if len(text) >= 120 or parse_caption(text):
            upper = max(upper, block_rect.y1)
    region = normalize_rect((left, upper + 2, right, caption_rect.y0 - 2))
    return region if area(region) >= 1400 else None


def looks_like_body_prose(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    text: str,
) -> bool:
    alnum = sum(char.isalnum() for char in text)
    digits = sum(char.isdigit() for char in text)
    digit_ratio = digits / max(alnum, 1)
    width_ratio = rect.width / max(page.rect.width, 1)
    near_body_margin = rect.x0 <= page.rect.x0 + page.rect.width * 0.18
    return (
        len(text) >= 90
        and width_ratio >= 0.60
        and near_body_margin
        and digit_ratio < 0.16
    )


def infer_table_region_from_caption(
    page: pymupdf.Page,
    caption_rect: pymupdf.Rect,
    blocks: list[tuple[pymupdf.Rect, str]],
) -> tuple[float, float, float, float] | None:
    left, right = column_bounds(page, caption_rect)

    def collect_below() -> tuple[list[pymupdf.Rect], float]:
        lower = page.rect.y1
        for block_rect, text in blocks:
            if block_rect.y0 <= caption_rect.y1:
                continue
            if block_rect.x1 <= left or block_rect.x0 >= right:
                continue
            if parse_caption(text) or looks_like_body_prose(page, block_rect, text):
                lower = min(lower, block_rect.y0)
                break
        content: list[pymupdf.Rect] = []
        for block_rect, text in blocks:
            if block_rect.y0 < caption_rect.y1 - 1 or block_rect.y1 > lower + 1:
                continue
            if block_rect.x1 <= left or block_rect.x0 >= right:
                continue
            if parse_caption(text) or looks_like_body_prose(page, block_rect, text):
                continue
            content.append(block_rect)
        return content, lower

    def collect_above() -> tuple[list[pymupdf.Rect], float]:
        upper = page.rect.y0
        for block_rect, text in blocks:
            if block_rect.y1 >= caption_rect.y0:
                break
            if block_rect.x1 <= left or block_rect.x0 >= right:
                continue
            if parse_caption(text) or looks_like_body_prose(page, block_rect, text):
                upper = max(upper, block_rect.y1)
        content: list[pymupdf.Rect] = []
        for block_rect, text in blocks:
            if block_rect.y0 < upper - 1 or block_rect.y1 > caption_rect.y0 + 1:
                continue
            if block_rect.x1 <= left or block_rect.x0 >= right:
                continue
            if parse_caption(text) or looks_like_body_prose(page, block_rect, text):
                continue
            content.append(block_rect)
        return content, upper

    below, lower = collect_below()
    above, upper = collect_above()

    candidates: list[tuple[float, tuple[float, float, float, float]]] = []
    if below:
        gap = max(0.0, min(rect.y0 for rect in below) - caption_rect.y1)
        region = (
            max(left, min(rect.x0 for rect in below) - 5),
            max(caption_rect.y1 + 1, min(rect.y0 for rect in below) - 4),
            min(right, max(rect.x1 for rect in below) + 5),
            min(lower - 1, max(rect.y1 for rect in below) + 5),
        )
        candidates.append((gap, normalize_rect(region)))
    if above:
        gap = max(0.0, caption_rect.y0 - max(rect.y1 for rect in above))
        region = (
            max(left, min(rect.x0 for rect in above) - 5),
            max(upper + 1, min(rect.y0 for rect in above) - 4),
            min(right, max(rect.x1 for rect in above) + 5),
            min(caption_rect.y0 - 1, max(rect.y1 for rect in above) + 5),
        )
        candidates.append((gap, normalize_rect(region)))

    valid = [(gap, region) for gap, region in candidates if area(region) >= 1000]
    if not valid:
        return None
    return min(valid, key=lambda item: item[0])[1]


def caption_heuristic_candidates(
    doc: pymupdf.Document,
    existing: list[Candidate],
) -> list[Candidate]:
    out: list[Candidate] = []
    for page_index, page in enumerate(doc):
        blocks = page_blocks(page)
        graphics = graphical_regions(page)
        for caption_rect, text in blocks:
            parsed = parse_caption(text)
            if not parsed:
                continue
            kind, label = parsed
            caption_bbox = normalize_rect(tuple(caption_rect))

            wanted_label = safe_label(label).lower()
            same_label = []
            for candidate in existing + out:
                if candidate.page_index != page_index or candidate.kind != kind:
                    continue
                if safe_label(candidate.label).lower() == wanted_label:
                    same_label.append(candidate)
            if same_label:
                for candidate in same_label:
                    if candidate.caption_bbox is None:
                        candidate.caption_bbox = caption_bbox
                    if not candidate.caption:
                        candidate.caption = text
                    candidate.label = label
                    candidate.confidence = max(candidate.confidence, 0.82)
                if kind == "figure":
                    continue
                combined_existing = union_rect(*(candidate.region_bbox for candidate in same_label))
                if combined_existing is None:
                    raise RuntimeError("union_rect returned None for a non-empty label group")
                above_gap = combined_existing[1] - caption_rect.y1
                below_gap = caption_rect.y0 - combined_existing[3]
                nearest_gap = min(
                    gap for gap in (above_gap, below_gap) if gap >= -8
                ) if any(gap >= -8 for gap in (above_gap, below_gap)) else math.inf
                # A large caption-to-detection gap usually means find_tables()
                # captured only a fragment. Generate a full caption-guided region.
                if nearest_gap <= 85:
                    continue

            if kind == "figure":
                region = infer_figure_region(page, caption_rect, blocks, graphics)
                source = "caption_figure_heuristic"
                confidence = 0.62
            else:
                region = infer_table_region_from_caption(page, caption_rect, blocks)
                source = "caption_table_heuristic"
                confidence = 0.55

            if region is None:
                continue
            out.append(
                Candidate(
                    kind=kind,
                    label=label,
                    page_index=page_index,
                    region_bbox=region,
                    caption_bbox=caption_bbox,
                    caption=text,
                    source=source,
                    confidence=confidence,
                )
            )
    return out


def load_pdffigures_json(path: Path | None) -> list[Candidate]:
    """Load optional PDFFigures2 JSON, without requiring Java in this script."""
    if path is None:
        return []
    if not path.is_file():
        raise FileNotFoundError(f"PDFFigures2 JSON 不存在：{path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("figures"), list):
        raw = raw["figures"]
    if not isinstance(raw, list):
        raise ValueError("PDFFigures2 JSON 格式不正确：顶层应为列表或含 figures 的对象。")

    candidates: list[Candidate] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        region = rect_tuple(item.get("regionBoundary"))
        if region is None:
            continue
        caption_bbox = rect_tuple(item.get("captionBoundary"))
        kind_raw = str(item.get("figType", "Figure"))
        kind = "table" if "table" in kind_raw.lower() else "figure"
        candidates.append(
            Candidate(
                kind=kind,
                label=str(item.get("name", len(candidates) + 1)),
                page_index=int(item.get("page", 0)),
                region_bbox=normalize_rect(region),
                caption_bbox=normalize_rect(caption_bbox) if caption_bbox else None,
                caption=str(item.get("caption", "")).strip(),
                source="pdffigures2_json",
                confidence=0.96,
            )
        )
    return candidates


def merge_same_label_tables(candidates: Iterable[Candidate]) -> list[Candidate]:
    groups: dict[tuple[int, str, str], list[Candidate]] = {}
    passthrough: list[Candidate] = []
    for candidate in candidates:
        label_key = safe_label(candidate.label).lower()
        if candidate.kind == "table" and not label_key.startswith("fallback_"):
            groups.setdefault((candidate.page_index, candidate.kind, label_key), []).append(candidate)
        else:
            passthrough.append(candidate)

    merged: list[Candidate] = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        region = union_rect(*(item.region_bbox for item in group))
        if region is None:
            raise RuntimeError("union_rect returned None for a non-empty table group")
        caption_item = next((item for item in group if item.caption_bbox), group[0])
        merged.append(
            Candidate(
                kind="table",
                label=caption_item.label,
                page_index=caption_item.page_index,
                region_bbox=region,
                caption_bbox=caption_item.caption_bbox,
                caption=caption_item.caption,
                source="merged_pymupdf_tables",
                confidence=max(item.confidence for item in group),
            )
        )
    return passthrough + merged


def deduplicate(candidates: Iterable[Candidate]) -> list[Candidate]:
    priority = {
        "pdffigures2_json": 5,
        "caption_table_heuristic": 4,
        "merged_pymupdf_tables": 3,
        "pymupdf_find_tables": 3,
        "caption_figure_heuristic": 2,
    }
    chosen: list[Candidate] = []
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            -priority.get(candidate.source, 0),
            candidate.page_index,
            candidate.region_bbox[1],
            candidate.region_bbox[0],
        ),
    )
    for candidate in ordered:
        is_duplicate = False
        for old in chosen:
            if old.page_index != candidate.page_index or old.kind != candidate.kind:
                continue
            same_label = (
                safe_label(old.label).lower() == safe_label(candidate.label).lower()
            )
            overlap = iou(old.region_bbox, candidate.region_bbox)
            containment = intersection_area(old.region_bbox, candidate.region_bbox) / max(
                1.0, min(area(old.region_bbox), area(candidate.region_bbox))
            )
            strong_overlap = overlap > 0.42
            mostly_contained = containment > 0.78
            same_label_touching = same_label and overlap > 0.04
            if strong_overlap or mostly_contained or same_label_touching:
                is_duplicate = True
                break
        if not is_duplicate:
            chosen.append(candidate)
    return sorted(
        chosen,
        key=lambda candidate: (
            candidate.page_index,
            candidate.region_bbox[1],
            candidate.region_bbox[0],
        ),
    )


def clipped_rect(
    page: pymupdf.Page,
    bbox: Sequence[float],
    padding: float,
    avoid_bbox: Sequence[float] | None = None,
) -> pymupdf.Rect:
    content = pymupdf.Rect(*normalize_rect(bbox))
    rect = pymupdf.Rect(content)
    rect.x0 -= padding
    rect.y0 -= padding
    rect.x1 += padding
    rect.y1 += padding

    # For the content-only export, padding must not leak into its caption.
    if avoid_bbox is not None:
        avoid = pymupdf.Rect(*normalize_rect(avoid_bbox))
        horizontal_overlap = max(0.0, min(rect.x1, avoid.x1) - max(rect.x0, avoid.x0))
        vertical_overlap = max(0.0, min(rect.y1, avoid.y1) - max(rect.y0, avoid.y0))
        if horizontal_overlap > 0:
            if avoid.y0 >= content.y1 - 1.0:
                rect.y1 = min(rect.y1, avoid.y0 - 0.5)
            elif avoid.y1 <= content.y0 + 1.0:
                rect.y0 = max(rect.y0, avoid.y1 + 0.5)
        if vertical_overlap > 0:
            if avoid.x0 >= content.x1 - 1.0:
                rect.x1 = min(rect.x1, avoid.x0 - 0.5)
            elif avoid.x1 <= content.x0 + 1.0:
                rect.x0 = max(rect.x0, avoid.x1 + 0.5)

    return rect & page.rect


@dataclass(frozen=True)
class RenderSettings:
    """Raster settings shared by every region render."""
    dpi: int
    padding: float


@dataclass(frozen=True)
class ExtractOptions:
    """Everything extract_visuals needs beyond the paper itself."""
    render: RenderSettings
    keep_pages: bool
    debug_dpi: int
    pdffigures_json: Path | None
    include_uncaptioned_tables: bool


def render_region(
    page: pymupdf.Page,
    bbox: Sequence[float],
    output: Path,
    settings: RenderSettings,
    avoid_bbox: Sequence[float] | None = None,
) -> tuple[int, int]:
    clip = clipped_rect(page, bbox, settings.padding, avoid_bbox=avoid_bbox)
    if clip.is_empty or clip.width <= 2 or clip.height <= 2:
        raise ValueError(f"无效裁剪区域：{list(bbox)}")
    pixmap = page.get_pixmap(
        dpi=settings.dpi,
        clip=clip,
        colorspace=pymupdf.csRGB,
        alpha=False,
        annots=False,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(output))
    return pixmap.width, pixmap.height


def load_font(size: int) -> ImageFont.ImageFont:
    candidates = []
    if os.name == "nt":
        candidates.extend(
            [
                Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
                Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc",
            ]
        )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
                Path("/System/Library/Fonts/PingFang.ttc"),
            ]
        )
    else:
        candidates.extend(
            [
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
            ]
        )
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError as cause:
                # Present but unusable (wrong format, unreadable): try the next
                # candidate rather than falling straight back to the tiny default.
                log(f"font unusable, skipping {path}: {cause}")
    return ImageFont.load_default()


def make_debug_pages(
    doc: pymupdf.Document,
    candidates: list[Candidate],
    output_dir: Path,
    dpi: int,
) -> None:
    by_page: dict[int, list[Candidate]] = {}
    for candidate in candidates:
        by_page.setdefault(candidate.page_index, []).append(candidate)
    font = load_font(max(12, int(dpi / 8)))

    for page_index, page_candidates in by_page.items():
        if not 0 <= page_index < doc.page_count:
            continue
        page = doc[page_index]
        pixmap = page.get_pixmap(
            dpi=dpi,
            colorspace=pymupdf.csRGB,
            alpha=False,
            annots=False,
        )
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        draw = ImageDraw.Draw(image)
        scale = dpi / 72.0
        for candidate in page_candidates:
            x0, y0, x1, y1 = candidate.region_bbox
            box = tuple(int(value * scale) for value in (x0, y0, x1, y1))
            draw.rectangle(box, outline="red", width=max(2, int(scale * 1.8)))
            label = f"{candidate.kind} {candidate.label} [{candidate.source}]"
            text_x, text_y = box[0] + 4, max(0, box[1] + 4)
            text_bbox = draw.textbbox((text_x, text_y), label, font=font)
            draw.rectangle(text_bbox, fill="white")
            draw.text((text_x, text_y), label, fill="black", font=font)
        image.save(output_dir / f"page_{page_index + 1:03d}_detections.png")


def make_contact_sheet(paths: list[Path], output: Path) -> None:
    thumbnails: list[Image.Image] = []
    font = load_font(18)
    for path in paths:
        try:
            with Image.open(path) as source:
                image = source.convert("RGB")
                image.thumbnail((520, 360))
                canvas = Image.new("RGB", (540, 420), "white")
                canvas.paste(image, ((540 - image.width) // 2, 10))
                draw = ImageDraw.Draw(canvas)
                name = path.name if len(path.name) <= 72 else path.name[:69] + "..."
                draw.text((12, 385), name, fill="black", font=font)
                thumbnails.append(canvas)
        except Exception as exc:
            warn(f"无法加入 contact sheet：{path.name}：{exc}")

    if not thumbnails:
        return
    columns = 3 if len(thumbnails) >= 3 else len(thumbnails)
    rows = math.ceil(len(thumbnails) / columns)
    sheet = Image.new("RGB", (columns * 540, rows * 420), "white")
    for index, thumbnail in enumerate(thumbnails):
        x = (index % columns) * 540
        y = (index // columns) * 420
        sheet.paste(thumbnail, (x, y))
    sheet.save(output)


def render_all_pages(doc: pymupdf.Document, output_dir: Path, dpi: int) -> None:
    for page_index, page in enumerate(doc):
        pixmap = page.get_pixmap(
            dpi=dpi,
            colorspace=pymupdf.csRGB,
            alpha=False,
            annots=False,
        )
        pixmap.save(str(output_dir / f"page_{page_index + 1:03d}.png"))


def extract_visuals(
    pdf_path: Path,
    output: Path,
    resource: InputResource,
    options: ExtractOptions,
) -> dict[str, Any]:
    dpi = options.render.dpi
    doc = pymupdf.open(pdf_path)
    if doc.page_count == 0:
        doc.close()
        raise ValueError("PDF 没有页面。")

    try:
        candidates: list[Candidate] = []
        candidates.extend(load_pdffigures_json(options.pdffigures_json))
        candidates.extend(find_table_candidates(doc, options.include_uncaptioned_tables))
        candidates.extend(caption_heuristic_candidates(doc, candidates))
        candidates = merge_same_label_tables(candidates)
        candidates = deduplicate(candidates)

        # Final detector-independent cleanup. This is intentionally applied
        # after deduplication so every candidate path receives the same crop
        # sanitation, including externally supplied PDFFigures2 boxes.
        for candidate in candidates:
            candidate.region_bbox = sanitize_margin_artifacts(
                doc[candidate.page_index], candidate.region_bbox
            )

        counters = {"figure": 0, "table": 0}
        extracted: list[ExtractedVisual] = []
        contact_paths: list[Path] = []
        failures: list[dict[str, Any]] = []

        for candidate in candidates:
            if not 0 <= candidate.page_index < doc.page_count:
                failures.append(
                    {
                        "candidate": asdict(candidate),
                        "error": "page_index out of range",
                    }
                )
                continue
            page = doc[candidate.page_index]
            ordinal = counters.get(candidate.kind, 0) + 1
            counters[candidate.kind] = ordinal
            label = safe_label(candidate.label)
            stem = f"{candidate.kind}_{ordinal:03d}_{label}"
            base_dir = output / ("tables" if candidate.kind == "table" else "figures")
            caption_dir = output / (
                "tables_with_caption"
                if candidate.kind == "table"
                else "figures_with_caption"
            )
            image_path = base_dir / f"{stem}.png"
            caption_path = caption_dir / f"{stem}_with_caption.png"

            try:
                width, height = render_region(
                    page,
                    candidate.region_bbox,
                    image_path,
                    options.render,
                    avoid_bbox=candidate.caption_bbox,
                )
                # Remove residual blank gutter after the PDF-coordinate crop.
                # This is what turns the post-stamp whitespace into a tight
                # figure image without touching internal white areas.
                width, height = trim_white_border(image_path)
                combined = union_rect(candidate.region_bbox, candidate.caption_bbox)
                if combined is not None:
                    try:
                        render_region(page, combined, caption_path, options.render)
                    except Exception:
                        shutil.copy2(image_path, caption_path)
                else:
                    shutil.copy2(image_path, caption_path)
            except Exception as exc:
                failures.append(
                    {
                        "candidate": asdict(candidate),
                        "error": str(exc),
                    }
                )
                continue

            contact_paths.append(image_path)
            extracted.append(
                ExtractedVisual(
                    id=stem,
                    type=candidate.kind,
                    label=candidate.label,
                    page_index=candidate.page_index,
                    page_number=candidate.page_index + 1,
                    bbox=list(candidate.region_bbox),
                    caption_bbox=(
                        list(candidate.caption_bbox) if candidate.caption_bbox else None
                    ),
                    caption=candidate.caption,
                    image_path=image_path.relative_to(output).as_posix(),
                    image_with_caption_path=caption_path.relative_to(output).as_posix(),
                    extraction_method=candidate.source,
                    confidence=candidate.confidence,
                    pixel_width=width,
                    pixel_height=height,
                )
            )

        if options.keep_pages:
            render_all_pages(doc, output / "pages", dpi)

        make_debug_pages(doc, candidates, output / "debug", options.debug_dpi)
        make_contact_sheet(contact_paths, output / "contact_sheet.png")

        metadata = doc.metadata or {}
        manifest: dict[str, Any] = {
            "schema_version": "1.0",
            "generator": {
                "name": "extract_arxiv_visuals.py",
                "version": SCRIPT_VERSION,
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "pymupdf": getattr(pymupdf, "__version__", "unknown"),
            },
            "paper": {
                "paper_id": resource.paper_id,
                "source_kind": resource.source_kind,
                "source_input": resource.original_input,
                "resolved_pdf_url": resource.pdf_url,
                "pdf_path": pdf_path.relative_to(output).as_posix(),
                "pdf_sha256": sha256_file(pdf_path),
                "page_count": doc.page_count,
                "title": metadata.get("title", ""),
                "author": metadata.get("author", ""),
                "dpi": dpi,
            },
            "summary": {
                "figure_count": sum(1 for item in extracted if item.type == "figure"),
                "table_count": sum(1 for item in extracted if item.type == "table"),
                "visual_count": len(extracted),
                "failure_count": len(failures),
            },
            "visuals": [asdict(item) for item in extracted],
            "failures": failures,
            "quality_note": (
                "PyMuPDF 表格检测与 Caption 启发式属于自动检测。"
                "请查看 debug/ 和 contact_sheet.png 进行视觉复核。"
            ),
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return manifest
    finally:
        doc.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "从 arXiv/PDF 链接或本地 PDF 中检测 Figure 和 Table，"
            "并按高分辨率保存为 PNG。"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "示例：\n"
            '  python extract_arxiv_visuals.py "https://arxiv.org/abs/2606.20515"\n'
            '  python extract_arxiv_visuals.py "https://arxiv.org/pdf/2606.20515" '
            "--output ./2606.20515-assets --dpi 400\n"
            '  python extract_arxiv_visuals.py "D:\\\\papers\\\\paper.pdf" --keep-pages\n'
        ),
    )
    parser.add_argument("input", help="arXiv ID、arXiv URL、PDF URL 或本地 PDF 路径")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="输出目录；默认：./paper-assets-<paper-id>",
    )
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="PNG 分辨率，默认 300")
    parser.add_argument(
        "--padding",
        type=float,
        default=4.0,
        help="裁剪区域四周留白，单位为 PDF point，默认 4",
    )
    parser.add_argument("--keep-pages", action="store_true", help="同时保存每一页完整 PNG")
    parser.add_argument("--force", action="store_true", help="覆盖已有输出目录")
    parser.add_argument(
        "--debug-dpi",
        type=int,
        default=DEFAULT_DEBUG_DPI,
        help="debug 标注页分辨率，默认 120",
    )
    parser.add_argument(
        "--include-uncaptioned-tables",
        action="store_true",
        help=(
            "保留没有 Table Caption 对应的 PyMuPDF 表格候选。默认关闭，"
            "因为流程图和仪表盘容易被误判为表格。"
        ),
    )
    parser.add_argument(
        "--pdffigures-json",
        type=Path,
        help=(
            "可选：读取现有 PDFFigures2 JSON，以提高复杂论文的检测准确率；"
            "本脚本本身不要求 Java。"
        ),
    )
    parser.add_argument(
        "--no-auto-install",
        action="store_true",
        help="禁止自动 pip 安装 PyMuPDF/Pillow",
    )
    parser.add_argument("--version", action="version", version=SCRIPT_VERSION)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.dpi <= 0:
        parser.error("--dpi 必须是正整数")
    if args.debug_dpi <= 0:
        parser.error("--debug-dpi 必须是正整数")
    if args.padding < 0:
        parser.error("--padding 不能为负数")

    try:
        log(f"版本 {SCRIPT_VERSION}；脚本路径：{Path(__file__).resolve()}")
        resource = resolve_input(args.input)
        output = (args.output or Path(f"paper-assets-{resource.paper_id}")).expanduser().resolve()
        prepare_output(output, args.force, args.keep_pages)

        pdf_path = output / "source" / "paper.pdf"
        if resource.local_pdf is not None:
            log(f"复制本地 PDF：{resource.local_pdf}")
            copy_local_pdf(resource.local_pdf, pdf_path)
        elif resource.pdf_url is not None:
            download_pdf(resource.pdf_url, pdf_path)
        else:
            raise RuntimeError("没有可用的 PDF 来源。")

        with pdf_path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise ValueError("输入文件不是有效 PDF。")

        log("开始检测并渲染 Figure / Table")
        manifest = extract_visuals(
            pdf_path=pdf_path,
            output=output,
            resource=resource,
            options=ExtractOptions(
                render=RenderSettings(dpi=args.dpi, padding=args.padding),
                keep_pages=args.keep_pages,
                debug_dpi=args.debug_dpi,
                pdffigures_json=args.pdffigures_json,
                include_uncaptioned_tables=args.include_uncaptioned_tables,
            ),
        )
        summary = manifest["summary"]
        emit(f"\n完成。输出目录：{output}")
        emit(
            "结果："
            f"Figure {summary['figure_count']}，"
            f"Table {summary['table_count']}，"
            f"总计 {summary['visual_count']}，"
            f"失败 {summary['failure_count']}"
        )
        emit(f"Manifest：{output / 'manifest.json'}")
        contact_sheet = output / "contact_sheet.png"
        if contact_sheet.exists():
            emit(f"总览图：{contact_sheet}")
        return 0
    except KeyboardInterrupt:
        eprint("\n用户中断。")
        return 130
    except Exception as exc:
        eprint(f"\n错误：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
