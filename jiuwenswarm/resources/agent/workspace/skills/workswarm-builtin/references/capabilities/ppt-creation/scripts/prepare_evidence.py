#!/usr/bin/env python3
"""Prepare evidence assets declared in evidence-plan.json.

Deterministic routes are handled locally. The paper route delegates to the
existing extract_arxiv_visuals_v2_2.py and selects from its manifest. Web rows
accept a confirmed direct URL; AI rows intentionally remain Needs-Manual until
the Agent generates and reviews the requested illustration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

# Diagnostics go to stderr through logging; the completion line is program
# output and goes to stdout via emit().
LOGGER = logging.getLogger("prepare_evidence")


# Program output (report bodies, --json payloads) goes to stdout, diagnostics
# to stderr. Both travel through logging; this logger owns stdout, keeps a bare
# "%(message)s" format so the text is unchanged, and does not propagate so the
# stderr root handler never sees it.
STDOUT_LOGGER = logging.getLogger("prepare_evidence.stdout")
STDOUT_LOGGER.propagate = False
STDOUT_LOGGER.setLevel(logging.INFO)
_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(logging.Formatter("%(message)s"))
STDOUT_LOGGER.addHandler(_stdout_handler)


def emit(line: str) -> None:
    STDOUT_LOGGER.info(line)


FILE_COPY_ROUTES = {
    "user", "source-extracted", "product-screenshot", "code-screenshot",
    "official-logo", "formula", "manual",
}

# Routes that land a file on disk, so their "path" must stay under the asset root.
PATH_BEARING_ROUTES = FILE_COPY_ROUTES | {"paper-figure", "web", "ai-illustration"}

# A planned item is (re)prepared only from one of these states.
PREPARABLE_STATUSES = {"planned", "acquiring", "needs-manual"}


def nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def resolve(root: Path, value: str | None) -> Path | None:
    return (root / value).resolve() if nonempty(value) else None


def save_plan(path: Path, plan: dict) -> None:
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_asset(source: Path, destination: Path, force: bool) -> None:
    if not source.exists():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        if hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(destination.read_bytes()).digest():
            return
        raise FileExistsError(f"destination exists with different content: {destination}")
    shutil.copy2(source, destination)


def download_asset(url: str, destination: Path, force: bool) -> None:
    if destination.exists() and not force:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "jiuwenswarm-evidence/1.0"})
    temp = destination.with_suffix(destination.suffix + ".part")
    with urllib.request.urlopen(request, timeout=120) as response, temp.open("wb") as output:
        shutil.copyfileobj(response, output)
    temp.replace(destination)


def normalize_label(value: str | None) -> str:
    normalized = re.sub(r"^(?:fig(?:ure)?|table)\s*\.?\s*", "", str(value or ""), flags=re.I)
    return normalized.strip().strip(":.、 ").lower()


def select_paper_visual(item: dict, manifest: dict) -> tuple[dict | None, list[dict]]:
    selector = item.get("paper_selector") or {}
    label = normalize_label(selector.get("label"))
    keywords = [str(value).lower() for value in selector.get("caption_keywords", [])]
    # The bundled extractor assigns 0.62 to many valid caption-derived figures.
    # Confidence narrows candidates; approval remains the hard visual gate.
    minimum = float(selector.get("min_confidence", 0.55))
    expected_type = "table" if item.get("kind") == "paper-table" else "figure"
    ranked = []
    for visual in manifest.get("visuals", []):
        if visual.get("type") != expected_type or float(visual.get("confidence", 0)) < minimum:
            continue
        caption = str(visual.get("caption", "")).lower()
        score = float(visual.get("confidence", 0))
        if label:
            if normalize_label(visual.get("label")) != label:
                continue
            score += 2
        hits = sum(1 for keyword in keywords if keyword in caption)
        if keywords and hits == 0:
            continue
        score += hits * 0.5
        ranked.append((score, visual))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    candidates = [visual for _, visual in ranked]
    if not candidates:
        return None, []
    if label or keywords or len(candidates) == 1:
        if len(ranked) == 1 or ranked[0][0] > ranked[1][0] + 0.15:
            return ranked[0][1], candidates
    return None, candidates


def prepare_paper(item: dict, root: Path, analysis_root: Path, force: bool) -> None:
    paper_input = item.get("source_path") or item.get("source_url") or item.get("source")
    if not nonempty(paper_input):
        raise ValueError("paper route requires source_path, source_url, or source")
    local_input = resolve(root, paper_input) if item.get("source_path") else None
    actual_input = str(local_input if local_input else paper_input)
    output = analysis_root / "papers" / item["id"]
    extractor = Path(__file__).with_name("extract_arxiv_visuals_v2_2.py")
    command = [sys.executable, str(extractor), actual_input, "--output", str(output), "--dpi", "300"]
    if force or output.exists():
        command.append("--force")
    subprocess.run(command, check=True)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected, candidates = select_paper_visual(item, manifest)
    item["result"] = {
        "manifest": manifest_path.relative_to(root).as_posix(),
        "contact_sheet": (output / "contact_sheet.png").relative_to(root).as_posix(),
        "candidate_ids": [candidate.get("id") for candidate in candidates],
    }
    if selected is None:
        item["status"] = "needs-manual"
        item["review"] = {
            "status": "pending",
            "notes": "Automatic selection was ambiguous; inspect the paper contact "
                     "sheet and set a label/caption selector.",
        }
        return
    include_caption = bool((item.get("paper_selector") or {}).get("include_caption", False))
    key = "image_with_caption_path" if include_caption else "image_path"
    source_image = output / selected[key]
    destination = resolve(root, item.get("path"))
    if destination is None:
        raise ValueError("selected paper evidence requires path")
    copy_asset(source_image, destination, force)
    item["status"] = "acquiring"
    item["review"] = {
        "status": "pending",
        "notes": f"Selected {selected.get('type')} {selected.get('label')} from "
                 f"page {selected.get('page_number')}; visual review required.",
    }
    item["result"].update({
        "selected_id": selected.get("id"),
        "caption": selected.get("caption"),
        "page_number": selected.get("page_number"),
        "confidence": selected.get("confidence"),
    })


def prepare_item(item: dict, root: Path, analysis_root: Path, force: bool) -> None:
    route = item.get("acquire_via")
    if route == "paper-figure":
        prepare_paper(item, root, analysis_root, force)
        return
    if route in FILE_COPY_ROUTES:
        source = resolve(root, item.get("source_path"))
        destination = resolve(root, item.get("path"))
        if source is None or destination is None:
            raise ValueError(f"{route} requires source_path and path")
        copy_asset(source, destination, force)
        item["status"] = "acquiring"
        item["review"] = {"status": "pending", "notes": "File prepared; visual review required."}
        return
    if route == "web":
        destination = resolve(root, item.get("path"))
        if destination is None or not nonempty(item.get("source_url")):
            raise ValueError("web requires a confirmed source_url and path")
        download_asset(item["source_url"], destination, force)
        item["status"] = "acquiring"
        item["review"] = {
            "status": "pending",
            "notes": "Downloaded from confirmed URL; verify identity, quality, and rights.",
        }
        return
    if route == "native-chart":
        source = resolve(root, item.get("source_path"))
        if source is None or not source.exists():
            raise ValueError("native-chart requires an existing source_path")
        item["status"] = "ready"
        item["review"] = {
            "status": "not-required",
            "notes": "Data source is ready; chart rendering is handled in slides.js.",
        }
        return
    if route == "native-drawing":
        item["status"] = "ready"
        item["review"] = {"status": "not-required", "notes": "Relationship is encoded as editable native drawing."}
        return
    if route == "ai-illustration":
        prompt_dir = analysis_root / "ai-prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / f"{item['id']}.md"
        prompt_path.write_text(item.get("query") or item.get("reference") or item.get("purpose"), encoding="utf-8")
        item["status"] = "needs-manual"
        item["review"] = {
            "status": "pending",
            "notes": f"Generate with the host image tool using "
                     f"{prompt_path.relative_to(root)}; AI cannot serve as factual evidence.",
        }
        return
    raise ValueError(f"unsupported acquire_via: {route}")


def approve_item(item: dict, root: Path) -> None:
    if item.get("acquire_via") in {"native-chart", "native-drawing"}:
        item["status"] = "ready"
        item["review"] = {"status": "not-required", "notes": item.get("review", {}).get("notes")}
        return
    destination = resolve(root, item.get("path"))
    if destination is None or not destination.exists():
        raise FileNotFoundError(f"cannot approve missing asset: {item.get('path')}")
    item["status"] = "ready"
    item["review"] = {"status": "approved", "notes": "Approved after visual review."}


def use_item(item: dict, root: Path) -> None:
    if item.get("acquire_via") in {"native-chart", "native-drawing"}:
        item["status"] = "used"
        item["review"] = {"status": "not-required", "notes": item.get("review", {}).get("notes")}
        return
    destination = resolve(root, item.get("path"))
    if destination is None or not destination.exists():
        raise FileNotFoundError(f"cannot mark missing asset as used: {item.get('path')}")
    if item.get("review", {}).get("status") != "approved":
        raise ValueError("asset must be visually approved before it can be marked used")
    item["status"] = "used"


def make_contact_sheet(plan: dict, root: Path, analysis_root: Path) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    entries = []
    for item in plan.get("items", []):
        path = resolve(root, item.get("path"))
        if path and path.exists():
            try:
                entries.append((item["id"], path, Image.open(path).convert("RGB")))
            except (OSError, ValueError) as cause:
                # A single unreadable asset must not sink the whole sheet; name it
                # on stderr so the missing tile is traceable.
                LOGGER.warning("contact sheet skipped %s: %s", item.get("id"), cause)
    if not entries:
        return
    cell_w, cell_h, label_h, cols = 360, 220, 34, 3
    rows = (len(entries) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * (cell_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (label, _, image) in enumerate(entries):
        image.thumbnail((cell_w - 20, cell_h - 20))
        x = (index % cols) * cell_w + (cell_w - image.width) // 2
        y = (index // cols) * (cell_h + label_h) + (cell_h - image.height) // 2
        sheet.paste(image, (x, y))
        label_x = (index % cols) * cell_w + 10
        label_y = (index // cols) * (cell_h + label_h) + cell_h + 5
        draw.text((label_x, label_y), label, fill="black")
    analysis_root.mkdir(parents=True, exist_ok=True)
    sheet.save(analysis_root / "evidence-contact-sheet.jpg", quality=92)


def main() -> int:
    # Keep the historical "[WARN]" tag rather than logging's default "WARNING".
    logging.addLevelName(logging.WARNING, "WARN")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="[%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("--plan", default="evidence-plan.json")
    parser.add_argument("--item", action="append", default=[])
    parser.add_argument("--approve", action="append", default=[])
    parser.add_argument("--used", action="append", default=[])
    parser.add_argument("--reject", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    root = Path(args.project).resolve()
    plan_path = resolve(root, args.plan)
    if plan_path is None or not plan_path.exists():
        LOGGER.error("evidence plan not found: %s", plan_path)
        return 1
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    asset_root = resolve(root, plan.get("asset_root")) or root / "assets"
    analysis_root = resolve(root, plan.get("analysis_root")) or root / "analysis"
    for directory, name in ((asset_root, "asset_root"), (analysis_root, "analysis_root")):
        try:
            directory.relative_to(root)
        except ValueError:
            LOGGER.error("%s must stay inside project: %s", name, directory)
            return 1
    selected = set(args.item)
    approvals = set(args.approve)
    used = set(args.used)
    rejections = set(args.reject)
    errors = []
    # --approve / --used / --reject act on named items only; a bare run prepares.
    review_pass = bool(approvals or used or rejections)
    for item in plan.get("items", []):
        item_id = item.get("id")
        in_scope = not selected or item_id in selected
        try:
            if item.get("acquire_via") in PATH_BEARING_ROUTES and nonempty(item.get("path")):
                destination = resolve(root, item.get("path"))
                if destination is not None:
                    destination.relative_to(asset_root)
            if item_id in used:
                use_item(item, root)
            elif item_id in approvals:
                approve_item(item, root)
            elif item_id in rejections:
                item["status"] = "needs-manual"
                item["review"] = {"status": "rejected", "notes": "Rejected during visual review."}
            elif not review_pass and in_scope:
                status = item.get("status")
                requested_retry = bool(selected and item_id in selected)
                preparable = status in PREPARABLE_STATUSES
                wanted = status == "planned" or requested_retry or args.force
                if preparable and wanted:
                    prepare_item(item, root, analysis_root, args.force)
        except Exception as cause:
            item["status"] = "needs-manual"
            item["review"] = {"status": "pending", "notes": str(cause)}
            errors.append(f"{item.get('id')}: {cause}")
    save_plan(plan_path, plan)
    make_contact_sheet(plan, root, analysis_root)
    for message in errors:
        LOGGER.warning("%s", message)
    emit(f"evidence preparation complete: {len(plan.get('items', []))} item(s), "
         f"{len(errors)} warning(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
