#!/usr/bin/env python3
"""print_blocks.py — Print stage JSON blocks in a readable format for the agent.

Reads stage03.json if available (has final references/ paths), otherwise stage01.json.
"""
import argparse
import logging
import pathlib
import sys

import common

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    args = parser.parse_args()

    path = None
    found_stage = None
    for stage_file in ("stage03.json", "stage02.json", "stage01.json"):
        candidate = pathlib.Path("work") / args.slug / stage_file
        if candidate.exists():
            path = candidate
            found_stage = stage_file
            break

    if path is None:
        logger.error("[print_blocks] ERROR: no stage JSON found for slug '%s'", args.slug)
        sys.exit(1)

    data = common.load_json(path)
    logger.info("SOURCE: %s", found_stage)
    logger.info("TITLE: %s", data.get("title", ""))
    logger.info("VIDEO_URLS: %s", data.get("video_urls", []))
    logger.info("")

    for b in data.get("blocks", []):
        t = b["type"]
        src = b.get("source", "main")
        if t == "heading":
            indent = "  " * (b["level"] - 1)
            logger.info("%sH%d [%s]: %s", indent, b["level"], src, b["text"])
        elif t == "text":
            logger.info("  TEXT [%s]: %s", src, b["text"][:150])
        elif t == "image":
            path_field = b.get("path")
            if path_field:
                logger.info("  IMG  [%s]: path=%s  alt=%s", src, path_field, b.get("alt", "")[:80])
            else:
                logger.info("  IMG  [%s]: alt=%s", src, b.get("alt", "")[:80])


if __name__ == "__main__":
    main()
