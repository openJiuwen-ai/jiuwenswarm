# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build a reproducible TTSE ZIP from the shipped plugin sources."""

from __future__ import annotations

import argparse
import json
import logging
import zipfile
from pathlib import Path

DEFAULT_SOURCE_DIR = Path(__file__).resolve().parents[1] / "jiuwenswarm/resources/plugins/ttse"


def package_ttse_plugin(output: Path, *, source: Path | None = None) -> Path:
    """Build a ZIP from an explicit source or the local plugin resources."""
    source = source if source is not None else DEFAULT_SOURCE_DIR
    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"TTSE source package is missing at {source}; provide a plugin source with --source")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["rails"]:
        if not (source / item["file"]).is_file():
            raise ValueError(f"Missing TTSE rail file: {item['file']}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in ("README.md", "manifest.json", "config.yaml", "rails/ttse_plugin_rail.py"):
            info = zipfile.ZipInfo(relative, date_time=(2026, 10, 27, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (source / relative).read_bytes())
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist/plugins/ttse_2.0.0.zip"))
    parser.add_argument("--source", type=Path, help="Plugin source directory; defaults to the local TTSE resources")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger(__name__).info("%s", package_ttse_plugin(args.output, source=args.source))
