#!/usr/bin/env python3
"""save_images.py — Copy agent-selected images to skills/<slug>/references/ and write stage03.json."""
import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

import common

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy selected images from work/<slug>/ to <skills_dir>/<slug>/references/."
    )
    parser.add_argument("slug")
    parser.add_argument(
        "keep_json",
        help='JSON array of paths relative to work/<slug>/, '
             'e.g. ["raw_images/dom_000.jpg", "frames/frame_002.png"]',
    )
    _default_skills_dir = Path(__file__).resolve().parent.parent.parent
    parser.add_argument("--skills-dir", default=str(_default_skills_dir))
    args = parser.parse_args()

    keep_paths: list[str] = json.loads(args.keep_json)
    work_dir = common.work_path(args.slug, "")
    ref_dir = Path(args.skills_dir) / args.slug / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)

    img_counter = 0
    frame_counter = 0
    manifest: dict[str, str] = {}  # rel_path → references/img_NN.ext

    for rel_path in keep_paths:
        src = work_dir / rel_path
        if not src.exists():
            logger.warning("[save_images] WARNING: not found, skipping: %s", rel_path)
            continue
        ext = src.suffix.lower()
        if not ext:
            ext = ".png"
        if "frame" in src.parts[-1]:
            dest_name = f"video_frame_{frame_counter:03d}{ext}"
            frame_counter += 1
        else:
            dest_name = f"img_{img_counter:02d}{ext}"
            img_counter += 1
        dest = ref_dir / dest_name
        shutil.copy2(src, dest)
        manifest[rel_path] = f"references/{dest_name}"
        logger.info("[save_images] %s <- %s", dest_name, rel_path)

    skill_dir = ref_dir.parent.resolve()
    logger.info("[save_images] saved %d file(s) to %s", len(manifest), ref_dir)
    logger.info("[save_images] SKILL_DIR: %s", skill_dir)
    logger.info("[save_images] SKILL_MD_PATH: %s", skill_dir / "SKILL.md")

    # Write stage03.json: blocks with image paths filled in (kept) or removed (skipped)
    stage02_path = common.work_path(args.slug, "stage02.json")
    if stage02_path.exists():
        stage02 = common.load_json(stage02_path)

        # Build reverse map: dom filename → url
        fetched_assets = stage02.get("fetched_assets", {})
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
        for b in stage02.get("blocks", []):
            if b["type"] == "image":
                ref = url_to_ref.get(b.get("url", ""))
                if ref:
                    new_blocks.append({**b, "path": ref})
                # else: image was skipped, drop from blocks
            else:
                new_blocks.append(b)

        stage03_path = common.work_path(args.slug, "stage03.json")
        common.write_json(stage03_path, {
            "url": stage02.get("url", ""),
            "slug": stage02.get("slug", args.slug),
            "title": stage02.get("title", ""),
            "blocks": new_blocks,
        })
        logger.info("[save_images] wrote %s: %d blocks", stage03_path, len(new_blocks))


if __name__ == "__main__":
    main()
