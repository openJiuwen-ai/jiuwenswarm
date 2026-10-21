import hashlib
import os
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path


def checkout_root():
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() and (root / "backends").is_dir() else None


@lru_cache(maxsize=1)
def repository_root():
    assets = Path(__file__).parent / "_resources"
    if getattr(sys, "frozen", False):
        # Frozen distributions ship an immutable, already compiled runtime.
        return assets
    root = checkout_root()
    if root is not None:
        return root
    # Wheels carry build inputs. Materialize an atomic, versioned user-owned
    # working tree so native builds never write into system site-packages.
    manifest = (assets / "manifest.json").read_bytes()
    key = hashlib.sha256(manifest).hexdigest()[:20]
    directory = Path.home() / ".computer-use/runtime"
    target = directory / key
    if target.is_dir():
        return target
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".prepare-", dir=directory))
    try:
        shutil.copytree(assets, temporary, dirs_exist_ok=True)
        try:
            os.rename(temporary, target)
        except OSError:
            if not target.is_dir():
                raise
        return target
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def native_driver(platform=None, root=None):
    platform, root = platform or sys.platform, Path(root) if root is not None else repository_root()
    if platform == "darwin":
        return dict(
            command=str(root / "backends/macos/.build/ComputerUseDriver.app/Contents/MacOS/computer-use-driver"),
            args=[])
    if platform == "win32":
        return dict(command=str(root / "backends/windows/.build/computer-use-driver.exe"), args=[])
    raise RuntimeError(f"platform {platform} is not implemented")
