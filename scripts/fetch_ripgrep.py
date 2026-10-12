#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fetch the official ripgrep binary that the desktop build bundles.

The binary is never committed. ``scripts/jiuwenswarm.spec`` calls
:func:`ensure_platform_binary` at build time, which reuses a verified copy under
``build/vendor/ripgrep/<platform>/`` or downloads the pinned official release,
checks the archive and the extracted binary against SHA-256 values pinned here,
and removes the macOS quarantine flag (a quarantined copy is killed on launch).

Usage::

    python scripts/fetch_ripgrep.py                 # current platform
    python scripts/fetch_ripgrep.py --all           # every supported platform
    python scripts/fetch_ripgrep.py --from DIR      # offline: copy DIR/<platform>/rg[.exe]
    python scripts/fetch_ripgrep.py --force         # replace an existing copy

``JIUWENSWARM_RG_FROM=DIR`` makes the build use the offline copy as well. The
local tree layout is ``<platform>/rg`` (``rg.exe`` on Windows), as in iCode's
``openjiuwen_icode/vendor/rg``; its binaries must match the pinned checksums.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform as _platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

RG_VERSION = "15.2.0"
BASE_URL = f"https://github.com/BurntSushi/ripgrep/releases/download/{RG_VERSION}"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = REPO_ROOT / "build" / "vendor" / "ripgrep"
FROM_ENV = "JIUWENSWARM_RG_FROM"


@dataclass(frozen=True)
class Asset:
    """One official release asset and the checksums it must match."""

    archive: str
    binary: str
    archive_sha256: str
    binary_sha256: str


# SHA-256 values of the official BurntSushi/ripgrep 15.2.0 release assets
# (they match the release's own .sha256 files) and of the rg binary inside each.
ASSETS: dict[str, Asset] = {
    "darwin-arm64": Asset(
        f"ripgrep-{RG_VERSION}-aarch64-apple-darwin.tar.gz",
        "rg",
        "3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4",
        "a326a1fb48074202e9ad41e4cd1e389eeea372c8c6f7d7e80da81176d5d9430e",
    ),
    "darwin-x86_64": Asset(
        f"ripgrep-{RG_VERSION}-x86_64-apple-darwin.tar.gz",
        "rg",
        "af7825fcc69a2afc7a7aea55fc9af90e26421d8f20fe59df32e233c0b8a231c1",
        "0c9a0066db0d26b640777db88045b0ccdd58509a746700e43e1c4ff8707a5ed0",
    ),
    "linux-x86_64": Asset(
        f"ripgrep-{RG_VERSION}-x86_64-unknown-linux-musl.tar.gz",
        "rg",
        "33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c",
        "e62198eb19b136b88c330af83647b5a962cb99b6b1f066758568f12de1974849",
    ),
    "linux-aarch64": Asset(
        f"ripgrep-{RG_VERSION}-aarch64-unknown-linux-musl.tar.gz",
        "rg",
        "800b1e7206afe799dfb5a6901f23147cfaabe0e52210538100f61e86e1740915",
        "c14cdb389f34e504d69e386cfc67d5c5d9a730a990de03ca6910b2a15e30386a",
    ),
    "windows-x86_64": Asset(
        f"ripgrep-{RG_VERSION}-x86_64-pc-windows-msvc.zip",
        "rg.exe",
        "71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5",
        "14231169855ec5205cf5a1b6f1db358ff4aed4247c86b69ce8aae647c77f6680",
    ),
}

Downloader = Callable[[str], bytes]


def platform_key(system: str | None = None, machine: str | None = None) -> str | None:
    """Map the build host to an :data:`ASSETS` key (``None`` if unsupported)."""
    sys_name = (system or _platform.system()).lower()
    mach = (machine or _platform.machine()).lower()
    if mach in {"x86_64", "amd64"}:
        arch = "x86_64"
    elif mach in {"arm64", "aarch64"}:
        arch = "arm64" if sys_name == "darwin" else "aarch64"
    else:
        return None
    if sys_name == "darwin":
        key = f"darwin-{arch}"
    elif sys_name == "linux":
        key = f"linux-{arch}"
    elif sys_name in {"windows", "win32"}:
        key = "windows-x86_64" if arch == "x86_64" else ""
    else:
        key = ""
    return key if key in ASSETS else None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check(data: bytes, expected: str, what: str) -> None:
    actual = _sha256(data)
    if actual != expected:
        raise ValueError(f"{what}: sha256 mismatch (expected {expected}, got {actual})")


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def _extract(archive: bytes, asset: Asset) -> bytes:
    if asset.archive.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for info in zf.infolist():
                if not info.is_dir() and Path(info.filename).name == asset.binary:
                    return zf.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tf:
            for member in tf.getmembers():
                if member.isfile() and Path(member.name).name == asset.binary:
                    extracted = tf.extractfile(member)
                    if extracted is not None:
                        return extracted.read()
    raise FileNotFoundError(f"{asset.binary} not found in {asset.archive}")


def strip_quarantine(path: Path) -> None:
    """Remove macOS's quarantine flag; a quarantined binary is killed on launch."""
    if sys.platform != "darwin" or not shutil.which("xattr"):
        return
    subprocess.run(
        ["xattr", "-d", "com.apple.quarantine", str(path)],
        capture_output=True,
        check=False,
    )


def _write(dest: Path, data: bytes, binary_name: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    if binary_name != "rg.exe":
        tmp_path.chmod(0o755)
    tmp_path.replace(dest)
    strip_quarantine(dest)


def fetch_platform(
    key: str,
    dest_root: Path = DEFAULT_DEST,
    *,
    from_dir: Path | None = None,
    force: bool = False,
    downloader: Downloader = _download,
    assets: dict[str, Asset] | None = None,
) -> Path:
    """Place the verified ``rg`` for ``key`` under ``dest_root/<key>/`` and return it."""
    table = assets if assets is not None else ASSETS
    if key not in table:
        raise KeyError(f"unsupported ripgrep platform: {key}")
    asset = table[key]
    dest = dest_root / key / asset.binary
    if dest.is_file() and not force:
        try:
            _check(dest.read_bytes(), asset.binary_sha256, f"existing {dest}")
        except ValueError:
            pass  # stale or tampered copy: replace it below
        else:
            strip_quarantine(dest)
            return dest
    if from_dir is not None:
        source = from_dir / key / asset.binary
        if not source.is_file():
            raise FileNotFoundError(f"{source} does not exist")
        data = source.read_bytes()
        _check(data, asset.binary_sha256, str(source))
    else:
        archive = downloader(f"{BASE_URL}/{asset.archive}")
        _check(archive, asset.archive_sha256, asset.archive)
        data = _extract(archive, asset)
        _check(data, asset.binary_sha256, f"{asset.binary} in {asset.archive}")
    _write(dest, data, asset.binary)
    return dest


def ensure_platform_binary(
    project_root: Path | None = None,
    *,
    key: str | None = None,
    downloader: Downloader = _download,
) -> Path:
    """Build hook: the verified ripgrep for the build host (download if needed)."""
    root = project_root or REPO_ROOT
    platform_name = key or platform_key()
    if platform_name is None:
        raise RuntimeError(
            f"no ripgrep {RG_VERSION} build for {_platform.system()} {_platform.machine()}"
        )
    from_env = (os.environ.get(FROM_ENV) or "").strip()
    return fetch_platform(
        platform_name,
        root / "build" / "vendor" / "ripgrep",
        from_dir=Path(from_env).expanduser() if from_env else None,
        downloader=downloader,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform", action="append", choices=sorted(ASSETS))
    parser.add_argument(
        "--all", action="store_true", help="fetch every supported platform"
    )
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument(
        "--from", dest="from_dir", type=Path, help="copy from a local vendor tree"
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.all:
        keys = sorted(ASSETS)
    elif args.platform:
        keys = args.platform
    else:
        current = platform_key()
        if current is None:
            parser.error("this host has no ripgrep build; pass --platform")
        keys = [current]
    for key in keys:
        path = fetch_platform(key, args.dest, from_dir=args.from_dir, force=args.force)
        print(f"{key}: {path} (ripgrep {RG_VERSION}, sha256 verified)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
