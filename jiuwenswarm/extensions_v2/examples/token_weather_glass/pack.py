# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build the independent Token Weather package from an explicit source allowlist."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

SOURCE = Path(__file__).resolve().parent


def build_package(output: Path) -> Path:
    """Place the manifest at the archive root without generated Python caches."""
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    files = [SOURCE / name for name in ("manifest.json", "README.md", "README_EN.md")]
    files.extend(sorted((SOURCE / "token_weather_glass").glob("*.py")))
    files.extend(SOURCE / "assets" / name for name in ("details.html", "details.css"))
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(SOURCE))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build_package(args.output))


if __name__ == "__main__":
    main()
