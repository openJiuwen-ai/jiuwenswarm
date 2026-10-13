# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Build dynamic shell environment prompt fragments."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


logger = logging.getLogger(__name__)

_PROBE_WAIT_SECONDS = 0.2
_PROBE_RETRY_SECONDS = 30.0
_PROBE_ENV_NAMES = (
    "PATH", "PATHEXT", "SystemRoot", "GIT_BASH", "GIT_BASH_PATH",
    "ProgramFiles", "ProgramFiles(x86)", "LocalAppData", "HOME",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "NoDefaultCurrentDirectoryInExePath",
)


@dataclass(frozen=True)
class ShellCapabilities:
    """A completed capability snapshot, or an explicitly unverified fallback."""

    detected: bool = False
    path_bash: str | None = None
    powershell: str | None = None
    git_bash: str | None = None
    posix_shell: str | None = None


def _environment_key(os_type: str) -> tuple[str | None, ...]:
    # which() can search the process cwd as well as PATH. Expanding the Git
    # overrides also tracks custom environment variables referenced in them.
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    overrides = tuple(
        os.path.expandvars(os.environ.get(name, ""))
        for name in ("GIT_BASH", "GIT_BASH_PATH")
    )
    # Missing PATH uses the OS default, whereas an explicitly empty PATH
    # does not. Preserve presence as well as value in the cache identity.
    values = tuple(os.environ.get(name) for name in _PROBE_ENV_NAMES)
    return (os_type, cwd, *values, *overrides)


@dataclass
class _ShellProbe:
    key: tuple[str | None, ...]
    started: float
    result: ShellCapabilities | None = None
    timeout_logged: bool = False


class _ShellCapabilityCache:
    """Share one bounded probe across members, threads and asyncio loops.

    A timed-out filesystem call cannot be cancelled. Keep its single-flight
    slot until it really returns, and use a daemon so it cannot hold process
    shutdown hostage. No work is queued in asyncio's default executor.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._probe: _ShellProbe | None = None
        self._cached_key: tuple[str | None, ...] | None = None
        self._cached = ShellCapabilities()
        self._retry_at = 0.0

    async def get(self, os_type: str) -> ShellCapabilities:
        key = _environment_key(os_type)
        now = time.monotonic()
        start = False
        with self._lock:
            if self._cached_key == key and (
                self._cached.detected or now < self._retry_at
            ):
                return self._cached
            probe = self._probe
            if probe is None:
                probe = _ShellProbe(key, now)
                self._probe = probe
                start = True
            elif probe.key != key:
                # A configuration change must not bypass the one-probe limit.
                return ShellCapabilities()

        if start:
            try:
                threading.Thread(
                    target=self._run,
                    args=(probe, os_type),
                    name="shell-capability-probe",
                    daemon=True,
                ).start()
            except Exception as exc:  # noqa: BLE001
                self._finish(probe, ShellCapabilities(), os_type)
                logger.warning("Shell capability probe could not start: %s", type(exc).__name__)

        # Polling only in-memory state avoids sharing loop-owned Futures.
        # Cancellation of a caller leaves the shared probe/cache untouched.
        while True:
            with self._lock:
                result = probe.result
            if result is not None:
                return result if key == _environment_key(os_type) else ShellCapabilities()
            remaining = probe.started + _PROBE_WAIT_SECONDS - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    log_timeout = not probe.timeout_logged
                    probe.timeout_logged = True
                if log_timeout:
                    logger.warning(
                        "Shell capability probe exceeded %.1fs; continuing with unverified "
                        "capabilities while the single background probe remains active",
                        _PROBE_WAIT_SECONDS,
                    )
                return ShellCapabilities()
            await asyncio.sleep(min(0.01, remaining))

    def _run(self, probe: _ShellProbe, os_type: str) -> None:
        result = ShellCapabilities()
        try:
            result = _detect_shell_capabilities(os_type)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Shell capability probe failed: %s", type(exc).__name__)
        finally:
            self._finish(probe, result, os_type)

    def _finish(
        self, probe: _ShellProbe, result: ShellCapabilities, os_type: str
    ) -> None:
        unchanged = probe.key == _environment_key(os_type)
        now = time.monotonic()
        with self._lock:
            # Never label a result gathered across an environment change as
            # verified for either configuration.
            probe.result = result if unchanged else ShellCapabilities()
            if unchanged:
                self._cached_key = probe.key
                self._cached = result
                self._retry_at = now + _PROBE_RETRY_SECONDS
            self._probe = None
        if now - probe.started > _PROBE_WAIT_SECONDS:
            logger.info("Shell capability probe finished after %.3fs", now - probe.started)


_CAPABILITY_CACHE = _ShellCapabilityCache()


async def get_shell_capabilities(os_type: str) -> ShellCapabilities:
    """Get shared capabilities without running executable discovery on the loop."""
    return await _CAPABILITY_CACHE.get(os_type)


def _is_wsl_bash_path(path: str) -> bool:
    normalized = path.replace("/", "\\").lower()
    return (
        normalized.endswith("\\system32\\bash.exe")
        or normalized.endswith("\\sysnative\\bash.exe")
        or "\\windowsapps\\bash.exe" in normalized
    )


def _existing_executable(path: Path) -> Optional[str]:
    try:
        if path.is_file():
            return str(path)
    except OSError:
        return None
    return None


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    seen = set()
    result = []
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _git_bash_candidates() -> list[Path]:
    candidates = []

    for env_name in ("GIT_BASH", "GIT_BASH_PATH"):
        env_value = os.environ.get(env_name)
        if env_value:
            candidates.append(Path(os.path.expandvars(env_value.strip('"'))).expanduser())

    for base_env in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base_path = os.environ.get(base_env)
        if base_path:
            git_root = Path(base_path) / "Git"
            candidates.extend([
                git_root / "bin" / "bash.exe",
                git_root / "usr" / "bin" / "bash.exe",
            ])

    git_exe = shutil.which("git")
    if git_exe:
        git_path = Path(git_exe)
        for parent in git_path.parents:
            if parent.name.lower() == "git":
                candidates.extend([
                    parent / "bin" / "bash.exe",
                    parent / "usr" / "bin" / "bash.exe",
                ])
                break

    return _dedupe_paths(candidates)


def _available_git_bash() -> Optional[str]:
    for candidate in _git_bash_candidates():
        executable = _existing_executable(candidate)
        if executable:
            return executable
    return None


def _available_powershell() -> Optional[str]:
    for command in ("pwsh", "powershell"):
        executable = shutil.which(command)
        if executable:
            return executable

    system_root = os.environ.get("SystemRoot")
    if system_root:
        executable = _existing_executable(
            Path(system_root)
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if executable:
            return executable
    return None


def _status(language: str, path: Optional[str]) -> str:
    if path:
        return f"可用，路径 `{path}`" if language == "cn" else f"available at `{path}`"
    return "不可用" if language == "cn" else "unavailable"


def _path_bash_status(language: str, path: Optional[str]) -> str:
    if not path:
        return "不可用" if language == "cn" else "unavailable"
    if _is_wsl_bash_path(path):
        if language == "cn":
            return f"`{path}`（WSL stub，不作为 Git Bash 使用）"
        return f"`{path}` (WSL stub; do not treat it as Git Bash)"
    return f"可用，路径 `{path}`" if language == "cn" else f"available at `{path}`"


def _detect_shell_capabilities(os_type: str) -> ShellCapabilities:
    """Perform synchronous OS probes only on the dedicated background thread."""
    path_bash = shutil.which("bash")
    if os_type.startswith("win"):
        return ShellCapabilities(
            detected=True,
            path_bash=path_bash,
            powershell=_available_powershell(),
            git_bash=_available_git_bash(),
        )
    return ShellCapabilities(
        detected=True, path_bash=path_bash, posix_shell=path_bash or shutil.which("sh")
    )


def build_shell_environment_prompt(
    language: str, os_type: str, *, capabilities: ShellCapabilities
) -> str:
    """Render a capability snapshot without probing the filesystem."""
    if not capabilities.detected:
        if language == "cn":
            return (
                "Shell 能力：尚未确认。请使用当前平台的命令和路径语法；"
                "不要假定 PowerShell、Git Bash 或 bash/sh 已安装。"
            )
        return (
            "Shell capabilities: not yet verified. Use commands and paths appropriate "
            "for this platform; do not assume PowerShell, Git Bash or bash/sh is installed."
        )

    if os_type.startswith("win"):
        powershell = capabilities.powershell
        git_bash = capabilities.git_bash
        path_bash = capabilities.path_bash
        if language == "cn":
            return f"""Shell 能力：
- PowerShell：{_status(language, powershell)}
- Git Bash：{_status(language, git_bash)}
- PATH bash：{_path_bash_status(language, path_bash)}

Shell 选择规则：
- Windows 且 Git Bash 可用，或 PATH bash 明确不是 WSL stub 时，可以使用 bash/Git Bash 执行 POSIX 命令，例如 `ls`、`grep`、`cat`、`mkdir -p`、bash 脚本。
- Windows 且 Git Bash 不可用、PATH bash 也不可用或只是 WSL stub 时，不要使用 POSIX 命令；优先使用 PowerShell 或 cmd。
- PowerShell cmdlet 不要包在 bash 里执行，应直接使用 PowerShell。
- 使用与当前平台匹配的命令和路径语法。"""
        return f"""Shell capabilities:
- PowerShell: {_status(language, powershell)}
- Git Bash: {_status(language, git_bash)}
- PATH bash: {_path_bash_status(language, path_bash)}

Shell selection rules:
- On Windows, when Git Bash is available, or PATH bash is clearly not a WSL stub, use bash/Git Bash for POSIX commands such as `ls`, `grep`, `cat`, `mkdir -p`, and bash scripts.
- On Windows, when Git Bash is unavailable and PATH bash is unavailable or only a WSL stub, do not use POSIX commands; prefer PowerShell or cmd.
- Do not wrap PowerShell cmdlets in bash; invoke PowerShell directly.
- Use command and path syntax that matches the current platform."""

    shell_path = capabilities.posix_shell
    if language == "cn":
        return f"""Shell 能力：
- Bash/sh：{_status(language, shell_path)}

Shell 选择规则：
- Linux/macOS 使用 bash/sh 风格命令；bash/sh 不可用时，使用当前平台实际可用的 Shell 或专用工具。
- 使用与当前平台匹配的命令和路径语法。"""
    return f"""Shell capabilities:
- Bash/sh: {_status(language, shell_path)}

Shell selection rules:
- On Linux/macOS, use bash/sh-style commands; if bash/sh is unavailable, use the Shell actually available on the platform or a dedicated tool.
- Use command and path syntax that matches the current platform."""
