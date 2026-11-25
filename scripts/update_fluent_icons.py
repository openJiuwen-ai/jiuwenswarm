#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Build the icon set the Office conversations search and insert (``jiuwenswarm/office/icons.py``).

Input is the unpacked npm package ``@fluentui/svg-icons`` (MIT): ``npm pack @fluentui/svg-icons@<version>`` and
untar it. Only the 24 px regular and filled icons are kept, in one gzip JSON written with a fixed timestamp, so the
same package always gives the same bytes.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "jiuwenswarm" / "office" / "fluent_icons" / "fluent-24.json.gz"
ICON_FILE = re.compile(r"^(?P<name>[a-z0-9_]+)_24_(?P<style>regular|filled)\.svg$")


def build(package_dir: Path) -> dict:
    manifest = json.loads((package_dir / "package.json").read_text("utf-8"))
    if manifest["name"] != "@fluentui/svg-icons" or manifest["license"] != "MIT":
        raise ValueError(f"{package_dir} is not the MIT @fluentui/svg-icons package")
    icons: dict[str, dict[str, str]] = {}
    for path in sorted((package_dir / "icons").iterdir()):
        match = ICON_FILE.match(path.name)
        if match:
            icons.setdefault(match["name"], {})[match["style"]] = path.read_text("utf-8").strip()
    return {"package": manifest["name"], "version": manifest["version"], "icons": icons}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_dir", type=Path, help="the unpacked package/ directory of @fluentui/svg-icons")
    bundle = build(parser.parse_args().package_dir)
    data = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with OUTPUT.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as packed:
        packed.write(data)
    logging.info("%d icons of %s %s -> %s", len(bundle["icons"]), bundle["package"], bundle["version"], OUTPUT)


if __name__ == "__main__":
    main()
