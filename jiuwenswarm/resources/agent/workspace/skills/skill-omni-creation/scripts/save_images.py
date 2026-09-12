#!/usr/bin/env python3
"""save_images.py — Copy selected assets into the UUID package/references/ and write stage03.json."""
import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from environment_gate import ensure_environment

# Keep every web/image-stage script on the same selected interpreter.
ensure_environment("requests")

import common

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy selected images from UUID runtime/ into package/references/."
    )
    parser.add_argument("run_id")
    parser.add_argument(
        "keep_json",
        nargs="?",
        help='Backward-compatible JSON array of paths relative to this run runtime/.',
    )
    parser.add_argument(
        "--keep",
        nargs="*",
        default=None,
        help="Cross-platform path list; use --keep with no values for an empty selection.",
    )
    args = parser.parse_args()

    if args.keep is not None:
        keep_paths: list[str] = args.keep
    elif args.keep_json is not None:
        keep_paths = json.loads(args.keep_json)
    else:
        parser.error("provide keep_json or --keep")
    work_dir = common.work_path(args.run_id, "").resolve()

    # Web-image selections must come from a fully resolved current stage02.
    stage02_for_validation = common.work_path(args.run_id, "stage02.json")
    if stage02_for_validation.exists():
        stage02_data = common.load_json(stage02_for_validation)
        image_blocks = [b for b in stage02_data.get("blocks", []) if b.get("type") == "image"]
        unresolved = [b.get("raw_path") for b in image_blocks if b.get("review_status") in {None, "UNREVIEWED"}]
        if unresolved:
            logger.error(
                "[save_images] ERROR: image review is unresolved; "
                "finish image_review.py first: %s",
                unresolved,
            )
            raise SystemExit(2)
        allowed_raw = {b.get("raw_path") for b in image_blocks if b.get("review_status") == "KEEP"}
        invalid_raw = [p for p in keep_paths if p.startswith("raw_images/") and p not in allowed_raw]
        if invalid_raw:
            logger.error("[save_images] ERROR: raw image is not KEEP in current stage02: %s", invalid_raw)
            raise SystemExit(2)

    ref_dir = common.package_dir(args.run_id) / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)

    img_counter = 0
    manifest: dict[str, str] = {}  # rel_path → references/img_NN.ext

    for rel_path in keep_paths:
        requested = Path(rel_path)
        if requested.is_absolute():
            parser.error(f"--keep paths must be relative to this run runtime/: {rel_path}")
        src = (work_dir / requested).resolve()
        try:
            src.relative_to(work_dir)
        except ValueError:
            parser.error(f"--keep path escapes this run runtime/: {rel_path}")
        if not src.exists():
            logger.warning("[save_images] WARNING: not found, skipping: %s", rel_path)
            continue
        ext = src.suffix.lower()
        if not ext:
            ext = ".png"
        if "frame" in src.parts[-1]:
            # Preserve original frame number so SKILL.md paths match before save_images runs.
            dest_name = f"video_{src.stem}{ext}"
        else:
            dest_name = f"img_{img_counter:02d}{ext}"
            img_counter += 1
        dest = ref_dir / dest_name
        shutil.copy2(src, dest)
        manifest[rel_path] = f"references/{dest_name}"
        logger.info("[save_images] %s <- %s", dest_name, rel_path)

    package_dir = ref_dir.parent.resolve()
    logger.info("[save_images] saved %d file(s) to %s", len(manifest), ref_dir)
    logger.info("[save_images] PACKAGE_DIR: %s", package_dir)
    logger.info("[save_images] SKILL_MD_PATH: %s", package_dir / "SKILL.md")

    # Write stage03.json: blocks with image paths filled in (kept) or removed (skipped).
    # For pages with no images, stage02 may be intentionally absent; stage01 is a
    # valid fallback so this script still closes the output-path contract.
    stage02_path = common.work_path(args.run_id, "stage02.json")
    stage01_path = common.work_path(args.run_id, "stage01.json")
    source_stage_path = stage02_path if stage02_path.exists() else stage01_path
    if source_stage_path.exists():
        source_stage = common.load_json(source_stage_path)

        # Build reverse map: dom filename → url
        fetched_assets = source_stage.get("fetched_assets", {})
        dom_to_url: dict[str, str] = {
            v["path"]: url for url, v in fetched_assets.items()
        }

        # Build url → references/img_NN.ext
        url_to_ref: dict[str, str] = {}
        for rel_path, ref_path in manifest.items():
            dom_name = Path(rel_path).name
            url = dom_to_url.get(dom_name)
            if url:
                url_to_ref[url] = ref_path

        # Rebuild blocks: kept images get path field, skipped images removed
        new_blocks = []
        for b in source_stage.get("blocks", []):
            if b["type"] == "image":
                ref = url_to_ref.get(b.get("url", ""))
                if ref:
                    new_blocks.append({**b, "path": ref})
                # else: image was skipped, drop from blocks
            else:
                new_blocks.append(b)

        stage03_path = common.work_path(args.run_id, "stage03.json")
        common.write_json(stage03_path, {
            "url": source_stage.get("url", ""),
            "run_id": source_stage.get("run_id", args.run_id),
            "title": source_stage.get("title", ""),
            "blocks": new_blocks,
            "video_urls": source_stage.get("video_urls", []),
        })
        logger.info("[save_images] wrote %s from %s: %d blocks", stage03_path, source_stage_path.name, len(new_blocks))
    else:
        logger.error("[save_images] ERROR: neither stage02.json nor stage01.json exists for run_id '%s'", args.run_id)
        sys.exit(1)


if __name__ == "__main__":
    main()
