"""Use the installed native driver, or build a user-owned source checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

import portalocker

from jiuwenswarm.common.utils import get_user_workspace_dir

from . import engine_root, import_engine


def prepare_bundle(source: Path, cache: Path) -> Path:
    """Copy only runtime sources; never compile into site-packages."""
    roots = ("src", "backends/macos/Sources", "backends/windows", "assets", "skills")
    files = [source / "pyproject.toml", source / "backends/macos/Package.swift"]
    for name in roots:
        for path in (source / name).rglob("*"):
            parts = path.relative_to(source).parts
            if path.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in parts):
                files.append(path)
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / digest.hexdigest()[:24]
    with portalocker.Lock(str(target) + ".lock", timeout=180):
        if not target.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".prepare-", dir=cache))
            try:
                for path in files:
                    destination = temporary / path.relative_to(source)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
                temporary.rename(target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
    return target


def prepare_native() -> Path:
    """Resolve an immutable installed driver; only source runs may compile."""
    frozen = getattr(sys, "frozen", False)
    root = engine_root()
    if not frozen:
        root = prepare_bundle(root, Path(get_user_workspace_dir()) / "computer_use/runtime")
        import_engine(root)
        os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root / "src"), os.environ.get("PYTHONPATH")]))
    from computer_use.paths import native_driver

    executable = Path(native_driver(root=root)["command"])
    if not executable.is_file():
        if frozen:
            raise RuntimeError("The installed Computer Use driver is missing; reinstall the application.")
        with portalocker.Lock(str(root) + ".build.lock", timeout=180):
            if not executable.is_file():
                from computer_use.scripts.native import build_cli

                # stdout is exclusively MCP JSON-RPC, including during builds.
                descriptor = os.dup(1)
                try:
                    sys.stdout.flush()
                    os.dup2(2, 1)
                    build_cli([])
                finally:
                    sys.stdout.flush()
                    os.dup2(descriptor, 1)
                    os.close(descriptor)
    return executable


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-native", action="store_true")
    args = parser.parse_args()
    if not args.prepare_native and sys.version_info < (3, 12):
        raise RuntimeError("cua_repl requires Python 3.12+. Set react.subagents.cua_agent.repl_python.")
    executable = prepare_native()
    if args.prepare_native:
        # MacOSBackend parses this single JSON response at subprocess EOF.
        json.dump({"executable": str(executable)}, sys.stdout)
        return
    from computer_use.mcp.server import cli

    cli([])


if __name__ == "__main__":
    main()
