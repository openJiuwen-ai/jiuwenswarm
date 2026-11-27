# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extract path-like tokens from shell commands (shlex + redirects)."""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PATH_AWARE_COMMANDS = frozenset({
    "cd", "rm", "cp", "mv", "mkdir", "touch", "chmod", "chown", "cat",
    "ls", "dir", "type", "del", "rd", "copy", "move", "md",
    "head", "tail", "more", "less", "vim", "nano", "gedit", "notepad",
})

_INTERPRETER_BASENAMES = frozenset({
    "python", "python3", "pythonw", "py",
    "node", "nodejs", "bash", "sh", "dash", "zsh", "fish",
    "pwsh", "powershell",
})


def _looks_like_path(token: str) -> bool:
    if token.startswith(("\\\\", "./", "../")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", token):
        return True
    return "\\" in token or "/" in token


def _basename_lower(cmd: str) -> str:
    base = Path(cmd.replace("\\", "/")).name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


_READ_CMDS = frozenset({
    "cat", "ls", "dir", "type", "head", "tail", "more", "less",
})
_WRITE_CMDS = frozenset({
    "rm", "mkdir", "touch", "chmod", "chown", "del", "rd", "md",
})
_TRANSFER_CMDS = frozenset({"cp", "copy", "mv", "move"})


def extract_paths_from_command(command: str, workdir: str | Path) -> list[Path]:
    """Backward-compatible: path-only list from path-aware commands."""
    return [p for p, _ in extract_path_aware_command_accesses(command, workdir)]


def extract_path_aware_command_accesses(
        command: str,
        workdir: str | Path,
) -> list[tuple[Path, str]]:
    """Extract (path, read|write) from path-aware shell commands."""
    if not command or not isinstance(command, str):
        return []
    try:
        tokens = shlex.split(command.strip(), posix=False)
    except ValueError:
        tokens = command.strip().split()
    if not tokens:
        return []
    cmd0 = _basename_lower(tokens[0])
    logger.debug(
        "[file_guard.extract] command=%s cmd0=%s path_aware=%s",
        command[:120],
        cmd0,
        cmd0 in _PATH_AWARE_COMMANDS,
    )
    if cmd0 not in _PATH_AWARE_COMMANDS:
        return []
    base = Path(workdir).resolve()
    path_tokens: list[tuple[Path, int]] = []
    for idx, tok in enumerate(tokens[1:]):
        tok = tok.strip().strip('"').strip("'")
        if not tok or tok.startswith("-"):
            continue
        if not _looks_like_path(tok):
            continue
        p = Path(tok)
        if not p.is_absolute():
            p = base / tok
        try:
            path_tokens.append((p.resolve(), idx))
        except (OSError, RuntimeError):
            continue
    if not path_tokens:
        return []

    results: list[tuple[Path, str]] = []
    if cmd0 in _TRANSFER_CMDS and len(path_tokens) >= 2:
        results.append((path_tokens[0][0], "read"))
        for p, _ in path_tokens[1:]:
            results.append((p, "write"))
    elif cmd0 in _WRITE_CMDS:
        for p, _ in path_tokens:
            results.append((p, "write"))
    elif cmd0 in _READ_CMDS:
        for p, _ in path_tokens:
            results.append((p, "read"))
    else:
        # Default: treat as read (e.g. vim path — user may write; conservative: write)
        for p, _ in path_tokens:
            results.append((p, "write"))
    logger.debug("[file_guard.extract] path_aware_accesses=%s", results)
    return results


def extract_shell_path_accesses(
        command: str,
        workdir: str | Path,
) -> list[tuple[Path, str]]:
    """Return list of (resolved_path, action) for shell inspection.

    action is ``read`` or ``write`` (redirect targets are writes; ``<`` is read).
    """
    if not command or not isinstance(command, str):
        return []
    base = Path(workdir).resolve()
    results: list[tuple[Path, str]] = []

    def _resolve(tok: str) -> Path | None:
        tok = tok.strip().strip('"').strip("'")
        if not tok or not _looks_like_path(tok):
            return None
        p = Path(tok)
        if not p.is_absolute():
            p = base / tok
        try:
            return p.resolve()
        except (OSError, RuntimeError):
            return None

    for p, act in extract_path_aware_command_accesses(command, workdir):
        results.append((p, act))

    # Redirects: crude classify by operator before token
    for m in re.finditer(r"(?:^|[\s;|&])(\d*>>?|\d*<|&>)\s*([^\s;|&<>]+)", command):
        op, target = m.group(1), m.group(2)
        rp = _resolve(target)
        if rp is None:
            continue
        if "<" in op and ">" not in op:
            results.append((rp, "read"))
        else:
            results.append((rp, "write"))

    # Interpreter + script: exec on script path
    try:
        tokens = shlex.split(command.strip(), posix=False)
    except ValueError:
        tokens = command.strip().split()
    if len(tokens) >= 2:
        cmd0 = _basename_lower(tokens[0])
        if cmd0 in _INTERPRETER_BASENAMES:
            script_tok = tokens[1].strip('"').strip("'")
            if script_tok and not script_tok.startswith("-"):
                rp = _resolve(script_tok)
                if rp is not None:
                    results.append((rp, "exec"))

    return results


def iter_config_tool_bindings(file_guard_cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return tool_bindings map from config ``permissions.file_guard.tool_bindings``."""
    raw = (file_guard_cfg or {}).get("tool_bindings") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(spec, dict):
            out[name.strip()] = [spec]
        elif isinstance(spec, list):
            out[name.strip()] = [x for x in spec if isinstance(x, dict)]
    return out
