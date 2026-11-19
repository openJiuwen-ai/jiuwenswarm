#!/usr/bin/env python3
"""Structural and readability audit for generated PPTX files.

Checks the package, canvas/master/layout inheritance, slide bounds,
font floors, page count, and the fixed red summary band declared by an
execution lock. The visual contact sheet still requires Agent inspection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import posixpath
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# Diagnostics go to stderr through logging; the audit report itself is program
# output and goes to stdout via emit().
LOGGER = logging.getLogger("audit_pptx")


# Program output (report bodies, --json payloads) goes to stdout, diagnostics
# to stderr. Both travel through logging; this logger owns stdout, keeps a bare
# "%(message)s" format so the text is unchanged, and does not propagate so the
# stderr root handler never sees it.
STDOUT_LOGGER = logging.getLogger("audit_pptx.stdout")
STDOUT_LOGGER.propagate = False
STDOUT_LOGGER.setLevel(logging.INFO)
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter("%(message)s"))
STDOUT_LOGGER.addHandler(_stdout_handler)


def emit(line: str) -> None:
    STDOUT_LOGGER.info(line)


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


def slide_size(zf: zipfile.ZipFile) -> tuple[int, int]:
    root = xml(zf, "ppt/presentation.xml")
    node = root.find("p:sldSz", NS)
    if node is None:
        return 12192000, 6858000
    return int(node.get("cx", 12192000)), int(node.get("cy", 6858000))


def slide_order(zf: zipfile.ZipFile) -> list[str]:
    rels = xml(zf, "ppt/_rels/presentation.xml.rels")
    by_id = {
        rel.get("Id"): posixpath.basename(rel.get("Target", ""))
        for rel in rels.findall("pr:Relationship", NS)
        if rel.get("Type", "").endswith("/slide")
    }
    presentation = xml(zf, "ppt/presentation.xml")
    return [
        by_id[node.get(f"{{{NS['r']}}}id")]
        for node in presentation.findall(".//p:sldId", NS)
        if node.get(f"{{{NS['r']}}}id") in by_id
    ]


SOURCE_NOTE_RE = re.compile(r"来源\s*[:：]|Source\s*:", re.IGNORECASE)


def paper_figure_digests(plan_path: Path) -> dict[str, str]:
    """sha256 -> evidence id, for every paper figure the plan marks as used.

    Matching on image bytes rather than on the plan's page field keeps the check
    independent of deck ordering and of the renaming merge_slides.py does
    (assets/papers/x.png becomes ppt/media/image7.png). merge copies with
    shutil.copy2, so the bytes survive intact.
    """
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    base = plan_path.parent
    asset_root = base / plan.get("asset_root", "assets")
    digests: dict[str, str] = {}
    for entry in plan.get("items", []):
        if entry.get("kind") != "paper-figure" or entry.get("status") != "used":
            continue
        rel = entry.get("path")
        if not rel:
            continue
        asset = base / rel
        if not asset.exists():
            asset = asset_root / "papers" / Path(rel).name
        if asset.exists():
            digests[hashlib.sha256(asset.read_bytes()).hexdigest()] = entry.get("id") or rel
    return digests


def slide_media(zf: zipfile.ZipFile, slide_name: str) -> list[str]:
    rel_name = f"ppt/slides/_rels/{slide_name}.rels"
    if rel_name not in zf.namelist():
        return []
    media = []
    for rel in xml(zf, rel_name).findall("pr:Relationship", NS):
        if rel.get("Type", "").endswith("/image"):
            target = posixpath.normpath(posixpath.join("ppt/slides", rel.get("Target", "")))
            media.append(target)
    return media


def slide_layout(zf: zipfile.ZipFile, slide_name: str) -> str | None:
    rel_name = f"ppt/slides/_rels/{slide_name}.rels"
    if rel_name not in zf.namelist():
        return None
    for rel in xml(zf, rel_name).findall("pr:Relationship", NS):
        if rel.get("Type", "").endswith("/slideLayout"):
            return posixpath.basename(rel.get("Target", ""))
    return None


def explicit_font_sizes(root: ET.Element) -> list[float]:
    sizes = []
    for node in root.iter():
        if local(node.tag) in {"rPr", "defRPr", "endParaRPr"} and node.get("sz"):
            sizes.append(int(node.get("sz")) / 100)
    return sizes


# Footer chrome starts here, as a fraction of canvas height. The official master
# puts its footer row at 6.95" on a 7.50" canvas (92.7%).
FOOTER_BAND_TOP = 0.90

# Components author at 10 x 5.625in; merge_slides scales the page (and its font
# sizes) up to the official canvas. Sizes below are expressed in authoring
# points and rescaled by width / COMPAT_CANVAS_W, so the same threshold works
# for merged decks and for standalone 10in files.
COMPAT_CANVAS_W = 9144000
COMPAT_CANVAS_H = 5143500
TOC_ROLES = {"toc", "fixed-toc", "contents"}
# Sits between addTocSlide's smallest sanctioned tier (13pt badge numbers) and
# the ~10pt description lines hand-drawn variants add, so neither rounding in
# the scale nor a point of tuning in either tier flips the verdict.
TOC_MIN_TIER_PT = 12


def compat_scale(width: int, height: int) -> float:
    """The factor merge_slides applied to authoring coordinates and font sizes."""
    return min(width / COMPAT_CANVAS_W, height / COMPAT_CANVAS_H)


# addSlideTitle draws the title at y=0.18 and the subtitle at y=0.68 h=0.24, so
# the subtitle is the only sanctioned text in this authoring-inch band.
SUBTITLE_BAND = (0.60, 0.95)
# "Wang et al., 2024" / "Li & Chen et al." -- an author citation used as the
# opening of the line, which is what turns a subtitle into a reference entry.
AUTHOR_LEAD_RE = re.compile(
    r"^\s*[A-Z][A-Za-z\-']+(?:\s*(?:&|and|,)\s*[A-Z][A-Za-z\-']+)*\s+et\s+al\.?",
)
# Reference furniture that carries no information about what the page argues.
CITATION_TOKEN_RE = re.compile(
    r"et\s+al\.?|arXiv\s*:\s*\d{4}\.\d{4,5}(?:v\d+)?|doi\s*:\s*\S+|"
    r"https?://\S+|[Ss]ection\s*\d+(?:\.\d+)*|§\s*\d+(?:\.\d+)*|\b(?:19|20)\d{2}\b",
    re.IGNORECASE,
)
SUBTITLE_MIN_SUBSTANCE = 6  # visual chars left once citation furniture is gone


def visual_len(text: str) -> float:
    """Same width model the components use: CJK 1em, ASCII ~0.55em."""
    return sum(1 if ord(ch) > 0x2E7F else 0.55 for ch in text)


def subtitle_text(root: ET.Element, height: int, scale: float) -> str | None:
    """Text of the shape sitting in the subtitle band, if any."""
    tree = root.find("./p:cSld/p:spTree", NS)
    if tree is None:
        return None
    low, high = (v * 914400 * scale for v in SUBTITLE_BAND)
    for shape in tree:
        if local(shape.tag) != "sp":
            continue
        rect = shape_rect(shape)
        if rect and low <= rect[1] < high:
            text = text_of(shape)
            if text:
                return text
    return None


def subtitle_defects(text: str) -> list[str]:
    """Why this subtitle reads as a citation rather than as a statement."""
    problems = []
    if AUTHOR_LEAD_RE.match(text):
        problems.append(
            "opens with an author citation; lead with the full work title "
            "(e.g. \"Mixture-of-Agents Enhances Large Language Model Capabilities\") "
            "and put the reference in the footer via addSourceNote"
        )
    substance = CITATION_TOKEN_RE.sub("", text)
    substance = re.sub(r"[\s,.;:()（）\[\]—–\-·/]+", "", substance)
    if visual_len(substance) < SUBTITLE_MIN_SUBSTANCE:
        problems.append(
            "carries only reference furniture; say what the page establishes, "
            "not where it came from"
        )
    return problems


def body_font_sizes(root: ET.Element, height: int) -> list[float]:
    """Font sizes outside the footer band.

    The body-minimum warning is about readability of actual content. Footer
    chrome -- page number, confidentiality line, source note -- is deliberately
    ~7pt and the official master's own footer runs at 9.74pt, below the same
    threshold. Counting it produced a warning on every page of every deck, which
    is the fastest way to teach everyone to ignore the warning column.
    """
    cutoff = height * FOOTER_BAND_TOP
    tree = root.find("./p:cSld/p:spTree", NS)
    if tree is None:
        return explicit_font_sizes(root)
    sizes = []
    # Top-level shapes only: sp, pic, graphicFrame (tables/charts) and grpSp all
    # carry their own transform, so one pass covers table and group text too.
    for shape in tree:
        if local(shape.tag) not in {"sp", "pic", "graphicFrame", "grpSp"}:
            continue
        rect = shape_rect(shape)
        if rect and rect[1] >= cutoff:
            continue
        for node in shape.iter():
            if local(node.tag) in {"rPr", "defRPr", "endParaRPr"} and node.get("sz"):
                sizes.append(int(node.get("sz")) / 100)
    return sizes


def shape_rect(shape: ET.Element) -> tuple[int, int, int, int] | None:
    transform = shape.find("./p:spPr/a:xfrm", NS)
    if transform is None:
        transform = shape.find("./p:picPr/a:xfrm", NS)
    if transform is None:
        transform = shape.find("./p:xfrm", NS)
    if transform is None:
        return None
    off = transform.find("a:off", NS)
    ext = transform.find("a:ext", NS)
    if off is None or ext is None:
        return None
    return tuple(int(node.get(attr, 0)) for node, attr in ((off, "x"), (off, "y"), (ext, "cx"), (ext, "cy")))


def text_of(shape: ET.Element) -> str:
    return "".join(node.text or "" for node in shape.findall(".//a:t", NS)).strip()


def summary_band(root: ET.Element, width: int, height: int, fill: str) -> bool:
    """True if a full-width filled band sits in the footer zone.

    `fill` comes from the lock's brand.summary_banner.fill, so retheming the
    deck does not require touching this check.
    """
    wanted = fill.upper().lstrip("#")
    for shape in root.findall(".//p:sp", NS):
        rect = shape_rect(shape)
        if not rect:
            continue
        x, y, w, h = rect
        color = shape.find("./p:spPr/a:solidFill/a:srgbClr", NS)
        value = (color.get("val", "").upper() if color is not None else "")
        if value != wanted:
            continue
        sits_in_footer_band = y > height * 0.75
        spans_page_width = w > width * 0.75
        thick_enough = h > height * 0.035
        if sits_in_footer_band and spans_page_width and thick_enough:
            return True
    return False


def audit(args: argparse.Namespace) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    pptx = Path(args.pptx).resolve()
    if not pptx.exists():
        raise FileNotFoundError(pptx)

    lock = None
    if args.lock:
        lock = json.loads(Path(args.lock).read_text(encoding="utf-8"))

    with zipfile.ZipFile(pptx) as zf:
        names = set(zf.namelist())
        required = {"[Content_Types].xml", "ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"}
        missing = sorted(required - names)
        if missing:
            errors.append(f"missing required package parts: {', '.join(missing)}")

        width, height = slide_size(zf)
        slides = slide_order(zf)
        masters = sorted(name for name in names if re.fullmatch(r"ppt/slideMasters/slideMaster\d+\.xml", name))
        layouts = sorted(name for name in names if re.fullmatch(r"ppt/slideLayouts/slideLayout\d+\.xml", name))

        template_layout_names: set[str] = set()
        if args.template:
            with zipfile.ZipFile(Path(args.template).resolve()) as tz:
                template_size = slide_size(tz)
                template_masters = [
                    name for name in tz.namelist()
                    if re.fullmatch(r"ppt/slideMasters/slideMaster\d+\.xml", name)
                ]
                template_layouts = [
                    name for name in tz.namelist()
                    if re.fullmatch(r"ppt/slideLayouts/slideLayout\d+\.xml", name)
                ]
                template_layout_names = {posixpath.basename(name) for name in template_layouts}
            if (width, height) != template_size:
                errors.append(
                    f"canvas {width}x{height} does not match official template "
                    f"{template_size[0]}x{template_size[1]}"
                )
            if len(masters) < len(template_masters):
                errors.append(f"only {len(masters)} masters; official template has {len(template_masters)}")
            if len(layouts) < len(template_layouts):
                errors.append(f"only {len(layouts)} layouts; official template has {len(template_layouts)}")

        if lock and len(slides) != len(lock.get("pages", [])):
            errors.append(
                f"slide count {len(slides)} does not match execution lock "
                f"page count {len(lock.get('pages', []))}"
            )

        paper_digests: dict[str, str] = {}
        if args.evidence_plan:
            paper_digests = paper_figure_digests(Path(args.evidence_plan).resolve())
        media_digest = {
            name: hashlib.sha256(zf.read(name)).hexdigest()
            for name in names
            if name.startswith("ppt/media/")
        }

        all_sizes: list[float] = []
        slide_reports = []
        for index, slide_name in enumerate(slides, 1):
            part = f"ppt/slides/{slide_name}"
            if part not in names:
                errors.append(f"slide {index} relationship points to missing {part}")
                continue
            root = xml(zf, part)
            slide_text = " ".join(node.text or "" for node in root.findall(".//a:t", NS))
            layout = slide_layout(zf, slide_name)
            if not layout:
                errors.append(f"slide {index} has no slideLayout relationship")
            elif template_layout_names and layout not in template_layout_names:
                errors.append(f"slide {index} uses non-template layout {layout}")

            # A pasted paper figure must carry its citation on the page itself.
            # Only runs when --evidence-plan is supplied, so existing callers
            # keep their current behaviour.
            cited = sorted({
                paper_digests[media_digest[part_name]]
                for part_name in slide_media(zf, slide_name)
                if media_digest.get(part_name) in paper_digests
            })
            if cited and not SOURCE_NOTE_RE.search(slide_text):
                errors.append(
                    f"slide {index} embeds paper figure(s) {', '.join(cited)} "
                    f"but carries no source note; call addSourceNote(pres, slide, "
                    f"{{ source: ... }}) with the evidence plan's source field"
                )

            sizes = explicit_font_sizes(root)
            all_sizes.extend(sizes)
            below_absolute = sorted(size for size in sizes if size < args.absolute_min_font)
            if below_absolute:
                errors.append(
                    f"slide {index} has explicit font size below "
                    f"{args.absolute_min_font}pt: {below_absolute[0]:g}pt"
                )
            # Footer chrome is excluded here on purpose -- see body_font_sizes.
            # The absolute floor above still applies to every run on the page.
            below_body = [size for size in body_font_sizes(root, height) if size < args.body_min_font]
            if below_body:
                warnings.append(f"slide {index} has {len(below_body)} text runs below {args.body_min_font}pt")

            out_of_bounds = 0
            top_text_sizes = []
            sp_tree = root.find(".//p:spTree", NS)
            if sp_tree is not None:
                for shape in list(sp_tree):
                    rect = shape_rect(shape)
                    if not rect:
                        continue
                    x, y, w, h = rect
                    starts_off_canvas = x < -1000 or y < -1000
                    ends_off_canvas = x + w > width + 1000 or y + h > height + 1000
                    if starts_off_canvas or ends_off_canvas:
                        out_of_bounds += 1
                    if text_of(shape) and y < height * 0.14:
                        top_text_sizes.extend(explicit_font_sizes(shape))
            if out_of_bounds:
                errors.append(f"slide {index} has {out_of_bounds} object(s) outside the canvas")

            role = None
            if lock and index <= len(lock.get("pages", [])):
                page = lock["pages"][index - 1]
                role = page.get("role")
                if role == "standard-content":
                    band_fill = lock.get("brand", {}).get("summary_banner", {}).get("fill", "4472C4")
                    if not summary_band(root, width, height, band_fill):
                        errors.append(f"slide {index} standard-content page is missing the fixed summary band")
                    typography = lock.get("deck", {}).get("typography_policy", {})
                    title_min = typography.get("title_min_pt", args.title_min_font)
                    if not top_text_sizes or max(top_text_sizes) < title_min:
                        errors.append(f"slide {index} content title is missing or below {title_min}pt")
                    # Under footer_mode "master" the page must not draw its own
                    # footer — the inherited master already has one, and two
                    # stacked footers is the classic duplicate-chrome bug.
                    # There is no fixed footer wording to match on, so this only
                    # fires when the lock declares a classification string:
                    # finding it in the slide's own text means the page drew it.
                    # (qa_geometry.py catches the general overlap case.)
                    marking = lock.get("brand", {}).get("footer", {}).get("classification")
                    if (
                        lock.get("template", {}).get("footer_mode") == "master"
                        and marking
                        and marking.lower() in slide_text.lower()
                    ):
                        errors.append(f"slide {index} draws a slide-level footer while footer_mode is master")
                    # The subtitle is the page's second sentence, not its
                    # bibliography. "Wang et al., 2024 (arXiv:2406.04692)" tells
                    # a reader nothing the footer citation does not already say.
                    subtitle = subtitle_text(root, height, compat_scale(width, height))
                    if subtitle:
                        for problem in subtitle_defects(subtitle):
                            errors.append(
                                f"slide {index} subtitle {problem} — got {subtitle[:60]!r}"
                            )
                elif role in TOC_ROLES:
                    # The official TOC has exactly three text tiers: the 22pt
                    # heading, the 13pt badge numbers and the 16pt section
                    # titles. Hand-drawn variants that copy addTocSlide's
                    # geometry and bolt a ~10pt grey description under each
                    # title are the recurring drift, so anything below the
                    # smallest sanctioned tier is an error.
                    floor = TOC_MIN_TIER_PT * compat_scale(width, height)
                    strays = sorted(
                        size for size in body_font_sizes(root, height) if size < floor
                    )
                    if strays:
                        errors.append(
                            f"slide {index} toc page has text at {strays[0]:g}pt, below the "
                            f"{floor:.1f}pt floor for TOC tiers — the official contents page carries "
                            f"only numbered badges and section titles. Drop the description line and "
                            f"call addTocSlide(pres, {{ title, sections: [{{num, title}}] }})"
                        )

            slide_reports.append({
                "index": index,
                "part": slide_name,
                "layout": layout,
                "role": role,
                "explicit_font_min_pt": min(sizes) if sizes else None,
                "explicit_font_max_pt": max(sizes) if sizes else None,
                "out_of_bounds": out_of_bounds,
            })

    report = {
        "pptx": str(pptx),
        "canvas_emu": [width, height],
        "slides": len(slides),
        "masters": len(masters),
        "layouts": len(layouts),
        "font_min_pt": min(all_sizes) if all_sizes else None,
        "font_median_pt": sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else None,
        "errors": errors,
        "warnings": warnings,
        "slide_reports": slide_reports,
    }
    return report


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="[%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("pptx")
    parser.add_argument("--template")
    parser.add_argument("--lock")
    parser.add_argument(
        "--evidence-plan",
        help="evidence-plan.json; enables the paper-figure source-note check",
    )
    parser.add_argument("--report")
    parser.add_argument("--title-min-font", type=float, default=24)
    parser.add_argument("--body-min-font", type=float, default=10)
    parser.add_argument("--absolute-min-font", type=float, default=7)
    args = parser.parse_args()

    try:
        report = audit(args)
    # ValueError already covers json.JSONDecodeError; listing both trips the
    # "parent and child exception" rule.
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as cause:
        LOGGER.error("audit failed: %s", cause)
        return 1

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for message in report["warnings"]:
        emit(f"[WARN] {message}")
    for message in report["errors"]:
        LOGGER.error("%s", message)
    emit(
        "pptx-audit: "
        f"{len(report['errors'])} error(s), {len(report['warnings'])} warning(s), "
        f"slides={report['slides']}, masters={report['masters']}, layouts={report['layouts']}, "
        f"font_min={report['font_min_pt']}pt"
    )
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
