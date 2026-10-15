"""Prepare a user-owned Python 3.12 environment and exec the bundled MCP.

Uses uv's venv/pip commands, never syncs the JiuwenSwarm environment. All
preparation output goes to stderr; stdout remains exclusively MCP JSON-RPC.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import portalocker

from .bundle import runtime_files, source_digest
from .config import toolkit_root


def _uv() -> str:
    command = shutil.which("uv")
    if command:
        return command
    import uv

    return uv.find_uv_bin()


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(
        command,
        cwd=cwd,
        stdout=sys.stderr,
        stderr=sys.stderr,
        text=True,
        check=True,
    )


def prepare(root: Path | None = None, cache: Path | None = None) -> Path:
    if sys.platform not in {"darwin", "win32"}:
        raise RuntimeError("The bundled desktop runtime supports macOS and Windows only. "
                           "Configure computer_use.mcp.command/args to connect to a supported host.")
    root = root or toolkit_root()
    files = runtime_files(root)
    key = source_digest(root, files)
    cache = cache or Path.home() / ".jiuwenswarm" / "computer_use" / "runtime"
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / key
    with portalocker.Lock(str(cache / (key + ".lock")), timeout=180):
        project = destination / "project"
        if not project.is_dir():
            temporary = Path(tempfile.mkdtemp(prefix=".prepare-", dir=cache))
            try:
                for file in files:
                    output = temporary / "project" / file.relative_to(root)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, output)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        environment = destination / "venv"
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        uv = _uv()
        if not python.is_file():
            _run([uv, "venv", "--python", "3.12", str(environment)])
        installed = destination / "installed.json"
        if not installed.is_file():
            _run([
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--editable",
                str(project),
            ])
            installed.write_text(json.dumps({"source": key}), encoding="utf-8")
        built = destination / ("native-" + sys.platform)
        if not built.is_file():
            _run(
                [
                    str(python),
                    "-c",
                    'from computer_use.scripts.native import build_cli; build_cli()',
                ],
                cwd=project,
            )
            built.touch()
    return python


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare or start the bundled Computer Use MCP.")
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Prepare the runtime without starting MCP.",
    )
    options = parser.parse_args()
    try:
        python = prepare()
    except Exception as error:
        logging.getLogger(__name__).error("Computer Use preparation failed: %s", error)
        raise SystemExit(1) from error
    if options.prepare:
        sys.stderr.write(f"Computer Use runtime ready: {python}\n")
        return
    os.execv(str(python), [str(python), "-m", "computer_use.mcp"])


if __name__ == "__main__":
    main()
