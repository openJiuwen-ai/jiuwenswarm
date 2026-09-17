#!/usr/bin/env python3
"""download_images.py — Download and deduplicate image blocks from stage01.json."""
import argparse
import hashlib
import io
import logging
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

# The shared environment gate runs before common.py or any third-party module.
# It selects/re-executes the project interpreter and repairs requests/Pillow.
from environment_gate import EnvironmentGateError, ensure_environment

requests = None
Image = None
common = None
_session = None


def _load_runtime_dependencies() -> None:
    global requests, Image, common, _session

    ensure_environment("images")

    import requests as requests_module
    from PIL import Image as ImageClass
    import common as common_module

    requests = requests_module
    Image = ImageClass
    common = common_module
    _session = requests.Session()
    _session.headers.update({"User-Agent": common.STEALTH_UA, "Referer": "https://www.google.com/"})


def _fetch_one(url: str) -> tuple[str, bytes | None, str | None]:
    try:
        resp = _session.get(url, timeout=common.OPERATION_TIMEOUT_SECONDS, stream=True)
        resp.raise_for_status()
        mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if mime not in common.SUPPORTED_MIMES:
            return url, None, None
        data = b""
        for chunk in resp.iter_content(8192):
            data += chunk
            if len(data) > common.MAX_IMAGE_BYTES:
                return url, None, None
        with Image.open(io.BytesIO(data)) as img:
            if img.width < common.MIN_DIMENSION and img.height < common.MIN_DIMENSION:
                return url, None, None
        return url, data, mime
    except Exception:
        return url, None, None


def download_image_blocks(blocks: list[dict]) -> tuple[list[dict], dict[str, tuple[bytes, str]]]:
    image_items = [(i, b) for i, b in enumerate(blocks) if b["type"] == "image"]
    urls = [b["url"] for _, b in image_items]

    raw: dict[str, tuple[bytes, str]] = {}
    with ThreadPoolExecutor(max_workers=common.FETCH_WORKERS) as executor:
        futures = {executor.submit(_fetch_one, url): url for url in urls}
        for future in as_completed(futures):
            url, data, mime = future.result()
            if data and mime:
                raw[url] = (data, mime)

    seen_hashes: set[str] = set()
    fetched: dict[str, tuple[bytes, str]] = {}
    valid_indices: set[int] = set()

    for idx, block in image_items:
        url = block["url"]
        result = raw.get(url)
        if result is None:
            logger.info("  [skip] download failed or too small: %s", url[:80])
            continue
        data, mime = result
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen_hashes:
            logger.info("  [skip] content duplicate: %s", url[:80])
            continue
        seen_hashes.add(digest)
        fetched[url] = (data, mime)
        valid_indices.add(idx)

    new_blocks = [b for i, b in enumerate(blocks) if b["type"] != "image" or i in valid_indices]
    return new_blocks, fetched


def main() -> None:
    parser = argparse.ArgumentParser(description="Download images from stage01.json.")
    parser.add_argument("slug", nargs="?", help="Skill slug — reads work/<slug>/stage01.json")
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="Run the shared image environment gate with auto-repair, then exit.",
    )
    args = parser.parse_args()

    try:
        _load_runtime_dependencies()
    except EnvironmentGateError:
        sys.exit(2)
    if args.check_deps:
        logger.info("[download_images] DEPENDENCIES_OK: %s", Path(sys.executable).resolve())
        return
    if not args.slug:
        parser.error("slug is required unless --check-deps is used")

    in_path = common.work_path(args.slug, "stage01.json")
    data = common.load_json(in_path)
    slug = data["slug"]

    out = Path(args.out) if args.out else common.work_path(slug, "stage02.json")
    asset_dir = common.work_path(slug, "raw_images")

    # A repeated slug must not expose images from an earlier run. Dependency
    # validation has already passed, so cleanup cannot turn a package problem
    # into a misleading empty references/ result.
    if asset_dir.exists():
        shutil.rmtree(asset_dir)
    if out.exists():
        out.unlink()

    blocks, fetched = download_image_blocks(data.get("blocks", []))
    asset_manifest = common.save_fetched_assets(fetched, asset_dir, "dom")

    # Persist the exact current-run file for each image. The first review pass
    # uses only alt/context and writes final KEEP/SKIP decisions into stage02.
    reviewed_blocks = []
    for block in blocks:
        if block.get("type") == "image":
            meta = asset_manifest.get(block.get("url", ""), {})
            raw_name = meta.get("path")
            reviewed_blocks.append({
                **block,
                "raw_path": f"raw_images/{raw_name}" if raw_name else None,
                "review_status": "UNREVIEWED",
            })
        else:
            reviewed_blocks.append(block)
    blocks = reviewed_blocks

    img_count = sum(1 for b in blocks if b["type"] == "image")
    common.write_json(out, {
        **data,
        "blocks": blocks,
        "fetched_assets": asset_manifest,
        "asset_dir": asset_dir.as_posix(),
    })
    logger.info("[download_images] wrote %s: %d unique image(s) in %s", out, img_count, asset_dir)


if __name__ == "__main__":
    main()
