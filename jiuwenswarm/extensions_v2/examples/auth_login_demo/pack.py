# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build the installable package, excluding generated account/session data."""

from __future__ import annotations

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

SOURCE = Path(__file__).resolve().parent


def build_package(output: Path) -> Path:
    """Write an explicit source allowlist with manifest.json at the ZIP root."""
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    files = [
        SOURCE / name
        for name in ("manifest.json", "settings.json", "README.md", "README_EN.md")
    ]
    files.extend(sorted((SOURCE / "auth_login_demo").glob("*.py")))
    files.extend(sorted((SOURCE / "assets").iterdir()))
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in files:
            if path.is_file():
                archive.write(path, path.relative_to(SOURCE))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build_package(args.output))


if __name__ == "__main__":
    main()
