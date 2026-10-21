# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compile the host driver and collect the frozen Computer Use runtime."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

RESOURCE_DESTINATION = "computer_use/_resources"


def import_engine(root: Path):
    from jiuwenswarm.cua_engine.hosting import import_engine as load

    return load(root)


def bundle_data(project_root: Path) -> list[tuple[str, str]]:
    """Build before Analysis, and ship runtime assets without compiler inputs."""
    if sys.platform not in ("darwin", "win32"):
        return []
    if sys.version_info < (3, 11):
        raise RuntimeError("Desktop packages with cua_repl must be built using Python 3.11+.")
    root = project_root / "jiuwenswarm" / "cua_engine"
    import_engine(root)
    from computer_use.scripts.native import build_native

    executable = build_native(root=root, configuration="release")
    if not executable.is_file():
        raise RuntimeError(f"Computer Use build output is missing: {executable}")
    files = []
    if sys.platform == "win32":
        files = [executable, executable.with_name("computer-use-capture.exe"),
                 executable.with_name("Interop.NativeUIA.dll")]
        for required in files:
            if not required.is_file():
                raise RuntimeError(f"Computer Use build output is missing: {required}")
    for directory in ("skills", "assets"):
        files.extend((root / directory).rglob("*"))
    return [(str(filename), str(Path(RESOURCE_DESTINATION) / filename.relative_to(root).parent))
            for filename in sorted(files) if filename.is_file()]


def finalize_macos_bundle(project_root: Path, collection_root: Path, app_bundle: Path) -> None:
    """Keep the signed helper intact instead of letting BUNDLE split its files."""
    relative = Path("backends/macos/.build/ComputerUseDriver.app")
    source = project_root / "jiuwenswarm/cua_engine" / relative
    for runtime_root in (collection_root, app_bundle / "Contents/Resources"):
        destination = runtime_root / RESOURCE_DESTINATION / relative
        if destination.is_symlink():
            destination.unlink()
        elif destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=True)
    # The installer scripts apply the release identity later. Keep the local
    # frozen app's ad-hoc seal valid after adding this nested application.
    subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app_bundle)], check=True)


