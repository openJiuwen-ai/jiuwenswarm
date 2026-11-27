# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Command execution tools implemented with openjiuwen @tool style."""

from __future__ import annotations

import asyncio
import json
import locale
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from openjiuwen.core.foundation.tool import tool

from jiuwenclaw.utils import get_agent_workspace_dir


_DANGEROUS_COMMAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+-rf\b", re.IGNORECASE), "blocked pattern: rm -rf"),
    (re.compile(r"\bdel\s+/[a-z]*[fsq][a-z]*\b", re.IGNORECASE), "blocked pattern: del /f /s /q"),
    (re.compile(r"\brd\s+/s\s+/q\b", re.IGNORECASE), "blocked pattern: rd /s /q"),
    (re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE), "blocked pattern: format drive"),
    (re.compile(r"\bshutdown\b", re.IGNORECASE), "blocked pattern: shutdown"),
    (re.compile(r"\breboot\b", re.IGNORECASE), "blocked pattern: reboot"),
    (re.compile(r"\bdiskpart\b", re.IGNORECASE), "blocked pattern: diskpart"),
    (re.compile(r"\bmkfs\b", re.IGNORECASE), "blocked pattern: mkfs"),
    (re.compile(r"\breg\s+delete\b", re.IGNORECASE), "blocked pattern: reg delete"),
    (
        re.compile(r"\bremove-item\b[^\n\r]*-recurse[^\n\r]*-force", re.IGNORECASE),
        "blocked pattern: Remove-Item -Recurse -Force",
    ),
]

_POWERSHELL_TOKENS = (
    "powershell ",
    "powershell.exe ",
    "pwsh ",
    "pwsh.exe ",
    "get-childitem",
    "set-location",
    "remove-item",
    "test-path",
    "join-path",
    "select-object",
    "where-object",
    "foreach-object",
    "invoke-webrequest",
    "invoke-restmethod",
    "out-file",
    "start-process",
    "$env:",
    "$psversiontable",
    "$null",
    "$true",
    "$false",
)

_VALID_SHELL_TYPES = {"auto", "cmd", "powershell", "bash", "sh"}


def _clip_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}\n...[truncated]"


def _check_command_safety(command: str) -> str | None:
    for pattern, message in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(command):
            return message
    return None


def _resolve_command_workdir(workdir: str) -> Path:
    project_root = get_agent_workspace_dir()
    candidate = Path(workdir) if workdir else project_root
    if not candidate.is_absolute():
        candidate = project_root / candidate
    candidate = candidate.resolve()
    candidate.relative_to(project_root)
    return candidate


def _normalize_shell_type(shell_type: str) -> str:
    value = (shell_type or "auto").strip().lower()
    return value if value in _VALID_SHELL_TYPES else "auto"


def _looks_like_powershell(command: str) -> bool:
    lowered = (command or "").strip().lower()
    if not lowered:
        return False
    if any(token in lowered for token in _POWERSHELL_TOKENS):
        return True
    if "@'" in command or '@"' in command:
        return True
    if re.search(r"(^|[\s;(])\$[A-Za-z_][A-Za-z0-9_]*", command):
        return True
    return False


def _available_powershell() -> str:
    for candidate in ("pwsh", "powershell", "powershell.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return "powershell"


def _available_unix_shell(prefer_bash: bool) -> Sequence[str]:
    if prefer_bash:
        bash = shutil.which("bash")
        if bash:
            return [bash, "-lc"]
    sh = shutil.which("sh") or "/bin/sh"
    return [sh, "-lc" if prefer_bash else "-c"]


def _resolve_execution_plan(command: str, shell_type: str) -> tuple[list[str] | str, bool, str]:
    normalized = _normalize_shell_type(shell_type)
    is_windows = os.name == "nt"

    if is_windows:
        if normalized == "auto":
            normalized = "powershell" if _looks_like_powershell(command) else "cmd"
        if normalized == "powershell":
            exe = _available_powershell()
            return [exe, "-NoProfile", "-NonInteractive", "-Command", command], False, "powershell"
        if normalized == "cmd":
            return command, True, "cmd"
        if normalized in {"bash", "sh"}:
            exe = shutil.which("bash") if normalized == "bash" else shutil.which("sh")
            if not exe:
                raise RuntimeError(f"Requested shell '{normalized}' is not available on this system.")
            flag = "-lc" if normalized == "bash" else "-c"
            return [exe, flag, command], False, normalized
        raise RuntimeError(f"Unsupported shell_type for Windows: {normalized}")

    if normalized == "auto":
        normalized = "bash" if shutil.which("bash") else "sh"
    if normalized == "powershell":
        exe = shutil.which("pwsh") or shutil.which("powershell")
        if not exe:
            raise RuntimeError("Requested shell 'powershell' is not available on this system.")
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command], False, "powershell"
    if normalized == "cmd":
        raise RuntimeError("shell_type 'cmd' is only supported on Windows.")
    if normalized == "bash":
        exe, flag = _available_unix_shell(prefer_bash=True)
        return [exe, flag, command], False, "bash"
    if normalized == "sh":
        exe, flag = _available_unix_shell(prefer_bash=False)
        return [exe, flag, command], False, "sh"
    raise RuntimeError(f"Unsupported shell_type: {normalized}")


def _run_command_sync(
    command: str,
    timeout_seconds: int,
    workdir: Path,
    shell_type: str,
) -> tuple[subprocess.CompletedProcess[str], str]:
    plan, use_shell, resolved_shell = _resolve_execution_plan(command, shell_type)
    # Windows 下 cmd 的输出通常是系统代码页（常见 CP936/GBK），
    # 这里不要强行按 UTF-8 解码，否则会出现中文乱码（如 .lnk 名称）。
    encoding = locale.getpreferredencoding(False) or "utf-8"
    result = subprocess.run(
        plan,
        shell=use_shell,
        cwd=str(workdir),
        text=True,
        encoding=encoding,
        errors='replace',
        capture_output=True,
        timeout=timeout_seconds,
    )
    return result, resolved_shell


def _run_command_background(
    command: str,
    workdir: Path,
    shell_type: str,
    grace_seconds: float = 5.0,
) -> tuple[int, str, str | None]:
    """Start command in background. Returns (pid, resolved_shell, error_msg).
    error_msg is None on success.
    """
    plan, use_shell, resolved_shell = _resolve_execution_plan(command, shell_type)
    proc = subprocess.Popen(
        plan,
        shell=use_shell,
        cwd=str(workdir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        exit_code = proc.wait(timeout=grace_seconds)
        if exit_code != 0:
            return proc.pid, resolved_shell, f"Process exited with code {exit_code}"
    except subprocess.TimeoutExpired:
        pass  # Still running after grace period -> success
    return proc.pid, resolved_shell, None


@tool(
    name="mcp_exec_command",
    description=(
        "Execute simple cross-platform command-line command in project workspace. "
        "Supports Windows cmd/PowerShell and macOS/Linux bash/sh. "
        "Optional shell_type=auto|cmd|powershell|bash|sh. "
        "Set background=True to run non-blocking (e.g. start a server); returns immediately on success, error on failure. "
        "Set max_output_chars=0 to disable output clipping. "
        "Use a larger timeout_seconds for long-running commands. "
        "Returns JSON: exit_code/stdout/stderr (blocking) or pid/status (background)."
    ),
)
async def mcp_exec_command(
    command: str,
    timeout_seconds: int = 300,
    workdir: str = ".",
    max_output_chars: int = 0,
    shell_type: str = "auto",
    background: bool = False,
) -> str:
    command = (command or "").strip()
    if not command:
        return "[ERROR]: command cannot be empty."

    blocked_reason = _check_command_safety(command)
    if blocked_reason:
        return f"[ERROR]: command rejected for safety ({blocked_reason})."

    try:
        resolved_workdir = _resolve_command_workdir(workdir)
    except Exception:
        return "[ERROR]: workdir is outside project workspace."

    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout_seconds = 300
    try:
        max_timeout_seconds = int(os.getenv("MCP_EXEC_COMMAND_MAX_TIMEOUT_SECONDS") or "3600")
    except ValueError:
        max_timeout_seconds = 3600
    max_timeout_seconds = max(1, max_timeout_seconds)
    timeout_seconds = max(1, min(timeout_seconds, max_timeout_seconds))

    try:
        max_output_chars = int(max_output_chars)
    except (TypeError, ValueError):
        max_output_chars = 0
    if max_output_chars < 0:
        max_output_chars = 0
    normalized_shell_type = _normalize_shell_type(shell_type)

    if background:
        try:
            pid, resolved_shell, err = await asyncio.to_thread(
                _run_command_background,
                command,
                resolved_workdir,
                normalized_shell_type,
            )
        except Exception as exc:
            return f"[ERROR]: command failed to start: {exc}"
        if err:
            return f"[ERROR]: background command failed: {err}"
        payload = {
            "command": command,
            "cwd": str(resolved_workdir),
            "shell_type": normalized_shell_type,
            "resolved_shell": resolved_shell,
            "background": True,
            "pid": pid,
            "status": "started",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        result, resolved_shell = await asyncio.to_thread(
            _run_command_sync,
            command,
            timeout_seconds,
            resolved_workdir,
            normalized_shell_type,
        )
    except subprocess.TimeoutExpired:
        return f"[ERROR]: command timed out after {timeout_seconds}s."
    except Exception as exc:
        return f"[ERROR]: command execution failed: {exc}"

    payload = {
        "command": command,
        "cwd": str(resolved_workdir),
        "shell_type": normalized_shell_type,
        "resolved_shell": resolved_shell,
        "exit_code": result.returncode,
        "stdout": _clip_text(result.stdout or "", max_output_chars),
        "stderr": _clip_text(result.stderr or "", max_output_chars),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
