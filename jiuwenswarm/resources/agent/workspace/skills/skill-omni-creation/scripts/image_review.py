#!/usr/bin/env python3
"""Persist final KEEP/SKIP image decisions and print selected paths."""
import argparse
import json
import logging
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

ALLOWED = {"KEEP", "SKIP"}


def _images(data: dict) -> list[dict]:
    return [b for b in data.get("blocks", []) if b.get("type") == "image"]


def _safe_current_path(slug: str, block: dict) -> Path:
    raw_path = block.get("raw_path")
    if not raw_path:
        raise ValueError("image block has no raw_path")
    work_dir = common.work_path(slug, "").resolve()
    candidate = (work_dir / raw_path).resolve()
    try:
        candidate.relative_to(work_dir)
    except ValueError as exc:
        raise ValueError(f"unsafe raw_path outside current work dir: {raw_path}") from exc
    if not candidate.is_file():
        raise ValueError(f"current-run image not found: {candidate}")
    return candidate


def _print_final(images: list[dict]) -> None:
    keep_paths = [b["raw_path"] for b in images if b.get("review_status") == "KEEP"]
    logger.info("KEEP_PATHS_JSON: %s", json.dumps(keep_paths, ensure_ascii=False))
    logger.info("KEEP_PATHS_ARGS: --keep%s", "" if not keep_paths else " " + " ".join(keep_paths))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    parser.add_argument(
        "--first-pass",
        nargs="+",
        required=True,
        help="Final decisions aligned to stage02 images: KEEP or SKIP",
    )
    args = parser.parse_args()

    stage02 = common.work_path(args.slug, "stage02.json")
    if not stage02.exists():
        logger.error("[image_review] ERROR: current stage02.json is missing; run prepare_images.py first")
        raise SystemExit(1)
    data = common.load_json(stage02)
    images = _images(data)

    # Also accept a one-token JSON array for backward compatibility.
    if len(args.first_pass) == 1 and args.first_pass[0].lstrip().startswith("["):
        decisions = json.loads(args.first_pass[0])
    else:
        decisions = args.first_pass
    if not isinstance(decisions, list) or len(decisions) != len(images):
        logger.error(
            "[image_review] ERROR: expected %d decisions, got %s",
            len(images),
            len(decisions) if isinstance(decisions, list) else "non-list",
        )
        raise SystemExit(2)

    normalized = [str(x).upper() for x in decisions]
    invalid = [x for x in normalized if x not in ALLOWED]
    if invalid:
        logger.error("[image_review] ERROR: decisions must be KEEP or SKIP: %s", invalid)
        raise SystemExit(2)

    for block, decision in zip(images, normalized):
        _safe_current_path(args.slug, block)
        block["review_status"] = decision

    common.write_json(stage02, data)
    _print_final(images)


if __name__ == "__main__":
    main()
