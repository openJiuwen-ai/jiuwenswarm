# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Declare common Windows runtimes from trusted service startup inputs."""

import json
import logging
import os
from pathlib import Path

from jiuwenswarm.server.runtime.pip_env import resolve_python_base, verified_runtime_python_base

logger = logging.getLogger(__name__)


def declare_windows_service_read_roots(env: dict[str, str], source_dir: Path | None) -> None:
    """Declare the known runner source independently of optional tool discovery.

    Never interpret arbitrary PYTHONPATH entries as permission requests. These
    roots add no command PATH prefixes and do not change explicit tool choices.
    """
    roots = json.loads((env.get("JIUWENBOX_SERVICE_READ_ROOTS") or "").strip() or "[]")
    if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
        raise ValueError("JIUWENBOX_SERVICE_READ_ROOTS requires a list of paths")
    unique = {}
    for value in roots:
        path = Path(value)
        key = os.path.normcase(str(path.resolve())) if path.is_absolute() else value
        unique.setdefault(key, value)
    if source_dir is not None and source_dir.is_absolute() and source_dir.is_dir():
        root = str(source_dir.resolve())
        unique.setdefault(os.path.normcase(root), root)
    if unique:
        env["JIUWENBOX_SERVICE_READ_ROOTS"] = json.dumps(list(unique.values()))


def _find_executable(name: str, env: dict[str, str]) -> Path | None:
    """Search absolute service PATH entries without Windows' implicit cwd lookup."""
    for entry in env.get("PATH", "").split(os.pathsep):
        directory = Path(entry.strip().strip('"'))
        if directory.is_absolute():
            executable = directory / name
            if executable.is_file():
                return executable
    return None


def declare_windows_runtime_tools(env: dict[str, str]) -> None:
    """Keep explicit declarations authoritative; otherwise describe bounded runtime roots.

    Only the runner's service environment is accepted, never command-request env.
    Managed venv Python precedes the runner, without changing an explicitly selected
    tool interpreter. JiuwenBox still owns ACL preparation and session exclusions.
    """
    if "JIUWENBOX_TOOL_PATHS" in env:
        return
    roots: list[str] = []
    directories: list[str] = []

    def add(root: Path, executables: list[Path]) -> None:
        root = root.resolve()
        if root == Path(root.anchor) or not root.is_dir():
            return
        if str(root) not in roots:
            roots.append(str(root))
        for directory in executables:
            directory = directory.resolve()
            if directory.is_dir() and directory.is_relative_to(root) and str(directory) not in directories:
                directories.append(str(directory))

    def add_python(executable: Path, *, prefer: bool = False, managed: bool = False) -> None:
        if (executable.name.lower() not in {"python.exe", "pythonw.exe"}
                or not executable.is_absolute() or not executable.is_file()):
            if managed:
                raise RuntimeError(f"Managed venv Python is missing or invalid: {executable}")
            return
        root = executable.parent
        venv_root = root.parent if root.name.lower() == "scripts" else root
        base = (verified_runtime_python_base(venv_root)
                if managed or (venv_root / "pyvenv.cfg").is_file() else resolve_python_base(executable))
        if (venv_root / "pyvenv.cfg").is_file():
            add(venv_root, [root] if prefer else [])
        add(base.parent, [base.parent] if prefer and base == executable.resolve() else [])

    def add_optional_python(executable: Path, *, prefer: bool = False) -> None:
        try:
            add_python(executable, prefer=prefer)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("Skipping optional Python tool declaration %s: %s", executable, exc)

    venv = (env.get("JIUWENBOX_VENV_DIR") or "").strip()
    if venv:
        executable = Path(venv) / "Scripts" / "python.exe"
        add_python(executable, prefer=True, managed=True)
    if not directories:
        executable = _find_executable("python.exe", env)
        if executable:
            add_optional_python(Path(executable), prefer=True)
    bundled = (env.get("JIUWENBOX_BUNDLED_PYTHON") or "").strip()
    if bundled:
        add_optional_python(Path(bundled) / "python.exe", prefer=not directories)
    runner = (env.get("JIUWENBOX_RUNNER_PYTHON") or "").strip()
    if runner and Path(runner).name.lower() in {"python.exe", "pythonw.exe"}:
        add_optional_python(Path(runner))

    executable = _find_executable("node.exe", env)
    if executable and Path(executable).is_absolute():
        node = Path(executable).resolve()
        # Standard Node distributions carry npm.cmd beside node.exe. Unknown
        # wrappers/standalone binaries require an explicit root declaration.
        if node.is_file() and (node.parent / "npm.cmd").is_file():
            add(node.parent, [node.parent])

    executable = _find_executable("git.exe", env)
    if executable and Path(executable).is_absolute():
        git = Path(executable).resolve()
        for root in (git.parent.parent, git.parent.parent.parent):
            if root == Path(root.anchor):
                continue
            relative = git.relative_to(root).as_posix().lower()
            if relative not in {"cmd/git.exe", "bin/git.exe", "mingw64/bin/git.exe", "mingw32/bin/git.exe"}:
                continue
            if git.is_file() and (root / "usr" / "bin" / "bash.exe").is_file():
                add(root, [root / "cmd", root / "bin", root / "usr" / "bin"])
                break
    if roots:
        env["JIUWENBOX_TOOL_PATHS"] = json.dumps({"read_roots": roots, "executable_dirs": directories})
