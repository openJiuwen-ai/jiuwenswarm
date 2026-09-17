# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Command execution tools implemented with openjiuwen @tool style."""

from __future__ import annotations

import asyncio
import contextlib
import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from openjiuwen.core.foundation.tool import tool
from openjiuwen.core.sys_operation.shell_process_registry import (
    consume_shell_session_cancelled,
    register_shell_process,
    resolve_shell_session_id,
    terminate_shell_process,
    unregister_shell_process,
)

from jiuwenswarm.common.utils import get_agent_workspace_dir


class CommandCancelled(Exception):
    """Raised when a blocking command is terminated by user interrupt."""


# ── jiuwenswarm-tui 反复 spawn 护栏 ────────────────────────────────
#
# Background:
#   The agent has been observed entering reflexive loops where it spawns the
#   ``jiuwenswarm-tui`` binary (directly or via ``@microsoft/tui-test`` specs)
#   over and over, each subprocess opening a real WebSocket session against
#   the same gateway. Every spawn does full session init + teardown, burns
#   API tokens, floods the gateway with TUI sessions opening/closing within
#   seconds, and ends up confusing the user (the agent's child TUIs appear
#   to "mysteriously open and close" on screen).
#
# Scope:
#   Only ``mcp_exec_command`` invocations that *start* a TUI subprocess are
#   throttled. Reading docs, listing files, building wheels, etc. are
#   untouched. Throttling is per-session_id so two unrelated user sessions
#   never block each other.
#
# Policy:
#   Max ``TUI_SPAWN_LIMIT`` spawns per ``_TUI_SPAWN_WINDOW_SECONDS`` per
#   session. When exceeded we return an error string explaining the limit
#   and pointing the agent at non-spawning alternatives. No silent failure.
TUI_SPAWN_LIMIT = 3
_TUI_SPAWN_WINDOW_SECONDS = 300.0
_TUI_SPAWN_HISTORY: dict[str, list[float]] = {}
_TUI_SPAWN_LAST_PURGE: float = 0.0

_TUI_SPAWN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Direct invocation of the installed binary.
    re.compile(r"\bjiuwenswarm-tui(?:\s|$|['\"])", re.IGNORECASE),
    # @microsoft/tui-test runner driving a spec that launches the TUI.
    # We deliberately keep this loose; false positives just mean the agent
    # is asked to slow down on something that *probably* spawns TUIs.
    re.compile(r"\bnode\b[^|;&\n]*\.spec\.ts\b", re.IGNORECASE),
)


def _command_spawns_tui(command: str) -> bool:
    return any(p.search(command) for p in _TUI_SPAWN_PATTERNS)


def _purge_stale_tui_spawn_buckets(now: float) -> None:
    """Remove buckets whose entries have all expired.

    Called opportunistically from ``_enforce_tui_spawn_budget`` so the
    module-level ``_TUI_SPAWN_HISTORY`` dict does not grow without bound
    as sessions come and go.  We rate-limit the sweep to at most once per
    window; it is O(n_buckets) and only needed when traffic is heavy enough
    for stale keys to matter.
    """
    global _TUI_SPAWN_LAST_PURGE
    if now - _TUI_SPAWN_LAST_PURGE < _TUI_SPAWN_WINDOW_SECONDS:
        return
    _TUI_SPAWN_LAST_PURGE = now
    cutoff = now - _TUI_SPAWN_WINDOW_SECONDS
    stale_keys = [
        bucket
        for bucket, history in _TUI_SPAWN_HISTORY.items()
        if not history or history[-1] < cutoff
    ]
    for bucket in stale_keys:
        del _TUI_SPAWN_HISTORY[bucket]


def _enforce_tui_spawn_budget(command: str, session_id: str) -> str | None:
    """Return an error string if this session has exceeded the spawn budget; else None.

    ``session_id`` may be empty (no contextvar set). In that case we still
    throttle under a synthetic "__global__" bucket so unattached invocations
    can't trivially bypass the limit by clearing the session id.
    """
    if not _command_spawns_tui(command):
        return None
    bucket = (session_id or "").strip() or "__global__"
    now = time.monotonic()
    _purge_stale_tui_spawn_buckets(now)
    history = _TUI_SPAWN_HISTORY.get(bucket, [])
    cutoff = now - _TUI_SPAWN_WINDOW_SECONDS
    history = [t for t in history if t >= cutoff]
    if len(history) >= TUI_SPAWN_LIMIT:
        oldest = history[0]
        retry_in = max(1, int(_TUI_SPAWN_WINDOW_SECONDS - (now - oldest)))
        _TUI_SPAWN_HISTORY[bucket] = history
        return (
            f"jiuwenswarm-tui spawn budget exceeded for this session "
            f"({TUI_SPAWN_LIMIT} spawns / {int(_TUI_SPAWN_WINDOW_SECONDS)}s). "
            f"Retry in ~{retry_in}s, or — preferred — stop driving the TUI "
            f"via @microsoft/tui-test loops. To validate TUI behaviour, "
            f"inspect existing recordings/snapshots, exercise the gateway "
            f"with a non-interactive script, or ask the user to run the TUI "
            f"manually. Each real TUI spawn opens a full gateway session and "
            f"burns API tokens; treat it as expensive."
        )
    history.append(now)
    _TUI_SPAWN_HISTORY[bucket] = history
    return None


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
    (
        re.compile(r"\bpkill\b[^\n\r;|&]*jiuwenswarm", re.IGNORECASE),
        "blocked pattern: pkill targeting jiuwenswarm (includes user TUI)",
    ),
    (
        re.compile(r"\bkillall\b[^\n\r;|&]*jiuwenswarm", re.IGNORECASE),
        "blocked pattern: killall targeting jiuwenswarm (includes user TUI)",
    ),
    (
        re.compile(r"\bpkill\b[^\n\r;|&]*jiuwenclaw", re.IGNORECASE),
        "blocked pattern: pkill targeting jiuwenclaw backend",
    ),
    (
        re.compile(r"\bkillall\b[^\n\r;|&]*jiuwenclaw", re.IGNORECASE),
        "blocked pattern: killall targeting jiuwenclaw backend",
    ),
    (
        re.compile(r"\bkill\b[^\n\r;|&]*jiuwenswarm", re.IGNORECASE),
        "blocked pattern: kill targeting jiuwenswarm (includes user TUI)",
    ),
    (
        re.compile(
            r"jiuwenswarm[^\n\r;|&]{0,240}\|\s*xargs\s+kill\b",
            re.IGNORECASE,
        ),
        "blocked pattern: xargs kill pipeline targeting jiuwenswarm",
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
_POWERSHELL_EXECUTABLE_PATTERN = re.compile(r"^\s*(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)\b", re.IGNORECASE)
_POWERSHELL_COMMAND_ARG_PATTERN = re.compile(r"(?is)(?:^|\s)-(?:command|c)\s+(?P<script>.+)\s*$")
_POSIX_COMMANDS = frozenset({
    "ls", "grep", "egrep", "fgrep", "cat", "head", "tail", "find", "rm",
    "cp", "mv", "touch", "chmod", "chown", "sed", "awk", "gawk", "cut",
    "sort", "uniq", "wc", "du", "df", "pwd", "which", "mkdir",
})
_QUOTED_WINDOWS_PATH_PATTERN = re.compile(r"(?P<quote>['\"])(?P<path>[A-Za-z]:\\[^'\"]+)(?P=quote)")
_UNQUOTED_WINDOWS_PATH_PATTERN = re.compile(r"(?<![\w/])(?P<path>[A-Za-z]:\\[^\s|&;]+)")


def _translate_mkdir_p_to_powershell(segment: str) -> str | None:
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        return None
    if not tokens:
        return None
    base = _strip_matching_quotes(tokens[0]).rsplit("/", maxsplit=1)[-1].rsplit("\\", maxsplit=1)[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base != "mkdir":
        return None
    has_p = False
    paths: list[str] = []
    other_flags: list[str] = []
    for tok in tokens[1:]:
        stripped = _strip_matching_quotes(tok)
        if stripped == "-p" or stripped == "--parents":
            has_p = True
        elif stripped.startswith("-"):
            other_flags.append(stripped)
        else:
            paths.append(tok)
    if not has_p:
        return None
    if other_flags:
        return None
    if not paths:
        return None
    items = []
    for p in paths:
        bare = _strip_matching_quotes(p)
        escaped = bare.replace("'", "''")
        items.append(f"New-Item -ItemType Directory -Path '{escaped}' -Force")
    return "; ".join(items)


def _translate_posix_segment_for_powershell(segment: str) -> str | None:
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        return None
    if not tokens:
        return None
    base = _strip_matching_quotes(tokens[0]).rsplit("/", maxsplit=1)[-1].rsplit("\\", maxsplit=1)[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    if base == "mkdir":
        return _translate_mkdir_p_to_powershell(segment)
    return None


def _translate_posix_for_powershell(command: str) -> str | None:
    segments = _split_shell_segments(command)
    if len(segments) != 1:
        return None
    return _translate_posix_segment_for_powershell(segments[0])


def _clip_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return f"{value[:max_chars]}\n...[truncated]"


def _check_command_safety(command: str) -> str | None:
    for pattern, message in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(command):
            return message
    return None


# Options of `git worktree add` that consume the following token as a value.
_WORKTREE_ADD_VALUE_OPTS = frozenset({"-b", "-B", "--branch", "--no-track"})

# Cheap prescreen: only commands mentioning `worktree add` are worth the full
# shlex parse. This runs on every bash command, so it must be O(regex) for the
# common (non-worktree) case.
_WORKTREE_ADD_PRESCREEN = re.compile(r"\bworktree\s+add\b", re.IGNORECASE)


def _parse_worktree_add_target(command: str) -> str | None:
    """Return the target path token of a `git worktree add` command, else None.

    The target is the first non-option positional after `add`. Handles
    `-b <branch>` / `-B <branch>` / `--branch=<branch>` (value-bearing) and
    boolean options like `-f`/`--detach`/`--no-checkout`. Returns None if the
    command is not a worktree-add or no positional target is present.
    """
    if not _WORKTREE_ADD_PRESCREEN.search(command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    # Find `worktree add` (allow a leading `git` and `sudo git` etc.).
    try:
        add_idx = _find_worktree_add_index(tokens)
    except ValueError:
        return None
    i = add_idx + 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok in _WORKTREE_ADD_VALUE_OPTS:
            i += 2  # skip opt + its value
            continue
        if tok.startswith("-"):
            i += 1  # boolean option
            continue
        return tok  # first positional = worktree target path
    return None


def _find_worktree_add_index(tokens: list[str]) -> int:
    """Return the index of the `add` token in a `... worktree add` chain.

    Matches the positional `worktree add` pair regardless of a leading
    `git`/`sudo` prefix. Raises ValueError if not found.
    """
    lowered = [t.lower() for t in tokens]
    for i in range(len(lowered) - 1):
        if lowered[i] == "worktree" and lowered[i + 1] == "add":
            return i + 1
    raise ValueError("not a git worktree add command")


def _check_worktree_path_safety(command: str) -> str | None:
    """Block `git worktree add` whose target lands outside the project dir.

    team.code does not mount the `enter_worktree` tool, so the LLM may hand-run
    `git worktree add`. Without a constraint it tends to target the project's
    sibling directory (../). This refuses out-of-project targets and points the
    model at `.worktrees/<name>`. Self-gating: only triggers on a manual
    `git worktree add`; when the `enter_worktree` tool is mounted the LLM does
    not hand-run the command, so this never fires.
    """
    target = _parse_worktree_add_target(command)
    if target is None:
        return None
    try:
        project_root = _context_project_root()
    except Exception:
        return None  # no project context → do not risk false positives
    try:
        target_path = Path(target)
        if not target_path.is_absolute():
            target_path = project_root / target_path
        target_path = target_path.resolve()
    except Exception:
        return None
    if _is_relative_to(target_path, project_root):
        return None  # inside the project (e.g. .worktrees/<name>) → allow
    return (
        f"worktree target must live under the project dir at "
        f".worktrees/<name> (e.g. `git worktree add .worktrees/<name> "
        f"-b <branch> HEAD`); refused path outside the project: {target}. "
        f"Do NOT use `../` or sibling directories."
    )


def _context_cwd() -> Path:
    try:
        from openjiuwen.core.sys_operation.cwd import get_cwd

        return Path(get_cwd()).resolve()
    except Exception:
        return get_agent_workspace_dir().resolve()


def _context_project_root() -> Path:
    try:
        from openjiuwen.core.sys_operation.cwd import get_project_root

        return Path(get_project_root()).resolve()
    except Exception:
        return _context_cwd()


def _context_workspace_root() -> Path | None:
    try:
        from openjiuwen.core.sys_operation.cwd import get_workspace

        workspace = get_workspace()
        return Path(workspace).resolve() if workspace else None
    except Exception:
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_command_workdir(workdir: str) -> Path:
    current_cwd = _context_cwd()
    project_root = _context_project_root()
    candidate = Path(workdir) if workdir else current_cwd
    if not candidate.is_absolute():
        candidate = current_cwd / candidate
    candidate = candidate.resolve()

    allowed_roots = [project_root]
    workspace_root = _context_workspace_root()
    if workspace_root is not None:
        allowed_roots.append(workspace_root)
    allowed_roots.append(get_agent_workspace_dir().resolve())

    if not any(_is_relative_to(candidate, root) for root in allowed_roots):
        raise ValueError("workdir is outside project workspace")
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
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
        system_powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if system_powershell.exists():
            return str(system_powershell)

    for candidate in ("pwsh", "powershell", "powershell.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return "powershell"


def _is_wsl_bash_path(path: str) -> bool:
    normalized = os.path.normcase(os.path.normpath(path))
    system_root = os.path.normcase(os.path.normpath(os.environ.get("SystemRoot") or r"C:\Windows"))
    return normalized == os.path.join(system_root, "system32", "bash.exe") or (
        "\\microsoft\\windowsapps\\bash.exe" in normalized
    )


def _git_bash_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_path = os.environ.get("GIT_BASH") or os.environ.get("GIT_BASH_PATH")
    if env_path:
        candidates.append(Path(env_path))

    for root in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("LocalAppData") and str(Path(os.environ["LocalAppData"]) / "Programs"),
    ):
        if root:
            candidates.append(Path(root) / "Git" / "bin" / "bash.exe")

    git_path = shutil.which("git")
    if git_path:
        git_exe = Path(git_path)
        candidates.append(git_exe.parent.parent / "bin" / "bash.exe")

    return candidates


def _available_git_bash() -> str | None:
    if os.name != "nt":
        return None
    for candidate in _git_bash_candidates():
        if candidate.exists():
            return str(candidate)
    return None


def _available_bash(*, allow_wsl: bool = True) -> str | None:
    if os.name == "nt":
        git_bash = _available_git_bash()
        if git_bash:
            return git_bash
    resolved = shutil.which("bash")
    if resolved and (allow_wsl or not _is_wsl_bash_path(resolved)):
        return resolved
    return None


def _available_sh() -> str | None:
    if os.name == "nt":
        git_bash = _available_git_bash()
        if git_bash:
            sh_path = Path(git_bash).parent.parent / "usr" / "bin" / "sh.exe"
            if sh_path.exists():
                return str(sh_path)
    return shutil.which("sh")


def _strip_matching_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
    return stripped


def _unwrap_powershell_command(command: str) -> str | None:
    if not _POWERSHELL_EXECUTABLE_PATTERN.match(command or ""):
        return None
    remainder = _POWERSHELL_EXECUTABLE_PATTERN.sub("", command, count=1).strip()
    match = _POWERSHELL_COMMAND_ARG_PATTERN.search(remainder)
    if not match:
        return None
    script = _strip_matching_quotes(match.group("script"))
    return script or None


def _split_shell_segments(command: str) -> list[str]:
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if char in {'"', "'"}:
            quote = None if quote == char else char if quote is None else quote
        if quote is None and command.startswith(("&&", "||"), index):
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            index += 2
            continue
        if quote is None and char in {"|", ";", "\n", "\r"}:
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def _segment_base_command(segment: str) -> str:
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        return ""
    if not tokens:
        return ""
    base = _strip_matching_quotes(tokens[0]).rsplit("/", maxsplit=1)[-1].rsplit("\\", maxsplit=1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def _looks_like_posix(command: str) -> bool:
    return any(_segment_base_command(segment) in _POSIX_COMMANDS for segment in _split_shell_segments(command or ""))


def _normalize_windows_paths_for_bash(command: str) -> str:
    def replace_path(match: re.Match[str]) -> str:
        value = match.group("path").replace("\\", "/")
        quote = match.groupdict().get("quote")
        return f"{quote}{value}{quote}" if quote else value

    normalized = _QUOTED_WINDOWS_PATH_PATTERN.sub(replace_path, command)
    return _UNQUOTED_WINDOWS_PATH_PATTERN.sub(replace_path, normalized)


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
            powershell_command = _unwrap_powershell_command(command)
            if powershell_command is not None:
                exe = _available_powershell()
                return [exe, "-NoProfile", "-NonInteractive", "-Command", powershell_command], False, "powershell"
            if _looks_like_powershell(command):
                exe = _available_powershell()
                return [exe, "-NoProfile", "-NonInteractive", "-Command", command], False, "powershell"
            if _looks_like_posix(command):
                exe = _available_bash(allow_wsl=False)
                if exe:
                    return [exe, "-lc", _normalize_windows_paths_for_bash(command)], False, "bash"
                translated = _translate_posix_for_powershell(command)
                ps_exe = _available_powershell()
                if translated is not None and ps_exe:
                    return [ps_exe, "-NoProfile", "-NonInteractive", "-Command", translated], False, "powershell"
                if ps_exe:
                    return [ps_exe, "-NoProfile", "-NonInteractive", "-Command", command], False, "powershell"
            normalized = "cmd"
        if normalized == "powershell":
            exe = _available_powershell()
            command = _unwrap_powershell_command(command) or command
            return [exe, "-NoProfile", "-NonInteractive", "-Command", command], False, "powershell"
        if normalized == "cmd":
            return command, True, "cmd"
        if normalized in {"bash", "sh"}:
            exe = _available_bash() if normalized == "bash" else _available_sh()
            if not exe:
                raise RuntimeError(f"Requested shell '{normalized}' is not available on this system.")
            flag = "-lc" if normalized == "bash" else "-c"
            return [exe, flag, _normalize_windows_paths_for_bash(command)], False, normalized
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


def _resolve_encoding(resolved_shell: str) -> str:
    """Choose subprocess text encoding based on the resolved shell type.

    - bash / sh on Windows (e.g. Git Bash / MSYS2) output UTF-8 by default.
    - cmd uses the system code page (typically CP936/GBK on Chinese Windows).
    - PowerShell also uses the system code page by default; safest to use
      the system code page and rely on ``errors='replace'`` for edge cases.
    """
    if os.name == "nt" and resolved_shell in ("bash", "sh"):
        return "utf-8"
    return locale.getpreferredencoding(False) or "utf-8"


def _run_command_sync(
    command: str,
    timeout_seconds: int,
    workdir: Path,
    shell_type: str,
    session_id: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    plan, use_shell, resolved_shell = _resolve_execution_plan(command, shell_type)
    encoding = _resolve_encoding(resolved_shell)
    popen_kw: dict[str, Any] = {}
    if os.name != "nt":
        _jw_start_new_session = os.getenv("JW_START_NEW_SESSION", "true").strip().lower()
        if _jw_start_new_session not in ("0", "false", "no", "off"):
            popen_kw["start_new_session"] = True
    proc = subprocess.Popen(
        plan,
        shell=use_shell,
        cwd=str(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding=encoding,
        errors="replace",
        **popen_kw,
    )
    sid = (session_id or "").strip()
    if sid:
        register_shell_process(sid, proc)
    stdout: str = ""
    stderr: str = ""
    deadline = time.monotonic() + timeout_seconds
    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                terminate_shell_process(proc)
                raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_seconds) from None
            # Check cancel flag inside the poll loop for responsive cancellation.
            # Without this, a long-running command could block for up to 600s
            # after the user cancels, waiting for communicate() to drain pipes.
            if sid and consume_shell_session_cancelled(sid):
                terminate_shell_process(proc)
                raise CommandCancelled(command)
            time.sleep(0.1)
        # Child exited, but grandchildren may still hold pipe FDs open.
        # Use the remaining deadline as communicate() timeout to avoid blocking
        # forever when a grandchild inherits stdout/stderr (e.g. `cmd &` in shell).
        remaining = max(deadline - time.monotonic(), 1.0)
        try:
            stdout, stderr = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            terminate_shell_process(proc)
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_seconds) from None
        # Final cancel check after communicate drains — covers the case where
        # cancel arrived between the last poll iteration and communicate().
        if sid and consume_shell_session_cancelled(sid):
            raise CommandCancelled(command)
    except CommandCancelled:
        raise
    except Exception:
        terminate_shell_process(proc)
        raise
    finally:
        if sid:
            unregister_shell_process(sid, proc)
        # Close pipe FDs that communicate() would have closed on the
        # normal path.  Without this, exception paths (cancel / timeout)
        # leak FDs and trigger ResourceWarning.
        for stream in (proc.stdout, proc.stdin, proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
    return subprocess.CompletedProcess(
        args=plan,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
    ), resolved_shell


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
    popen_kw = {}
    if os.name != "nt":
        _jw_start_new_session = os.getenv("JW_START_NEW_SESSION", "true").strip().lower()
        if _jw_start_new_session not in ("0", "false", "no", "off"):
            popen_kw["start_new_session"] = True
    proc = subprocess.Popen(
        plan,
        shell=use_shell,
        cwd=str(workdir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        **popen_kw,
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
        "Set background=True to run non-blocking (e.g. start a server); "
        "returns immediately on success, error on failure. "
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

    # Guardrail: rate-limit jiuwenswarm-tui spawn loops. Returns a friendly
    # error (not a safety block) so the LLM can recover and pick an
    # alternative path. Bucket is per-session so unrelated sessions don't
    # interfere; resolution falls back to "__global__" when no session id
    # contextvar is set.
    spawn_block = _enforce_tui_spawn_budget(command, resolve_shell_session_id() or "")
    if spawn_block:
        return f"[ERROR]: {spawn_block}"

    try:
        resolved_workdir = _resolve_command_workdir(workdir)
    except Exception:
        return "[ERROR]: workdir is outside project workspace."

    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout_seconds = 300
    try:
        max_timeout_seconds = int(os.getenv("MCP_EXEC_COMMAND_MAX_TIMEOUT_SECONDS") or "600")
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
            resolve_shell_session_id(),
        )
    except CommandCancelled:
        payload = {
            "command": command,
            "cwd": str(resolved_workdir),
            "shell_type": normalized_shell_type,
            "resolved_shell": normalized_shell_type,
            "exit_code": -1,
            "stdout": "",
            "stderr": "[Interrupted] Command cancelled by user.",
            "cancelled": True,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
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


def reset_tui_spawn_history() -> None:
    """Clear the TUI spawn history (for testing)."""
    _TUI_SPAWN_HISTORY.clear()
