# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Build dynamic shell environment prompt fragments."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable, Optional


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


def build_shell_environment_prompt(language: str, os_type: str) -> str:
    """Return shell capability and selection guidance for the current host."""
    path_bash = shutil.which("bash")

    if os_type.startswith("win"):
        powershell = _available_powershell()
        git_bash = _available_git_bash()
        if language == "cn":
            return f"""Shell 能力：
- PowerShell：{_status(language, powershell)}
- Git Bash：{_status(language, git_bash)}
- PATH bash：{_path_bash_status(language, path_bash)}

Shell 选择规则：
- Windows 且 Git Bash 可用，或 PATH bash 明确不是 WSL stub 时，可以使用 bash/Git Bash 执行 POSIX 命令，例如 `ls`、`grep`、`cat`、`mkdir -p`、bash 脚本。
- Windows 且 Git Bash 不可用、PATH bash 也不可用或只是 WSL stub 时，不要使用 POSIX 命令；优先使用 PowerShell 或 cmd。
- PowerShell cmdlet 不要包在 bash 里执行，应直接使用 PowerShell。"""
        return f"""Shell capabilities:
- PowerShell: {_status(language, powershell)}
- Git Bash: {_status(language, git_bash)}
- PATH bash: {_path_bash_status(language, path_bash)}

Shell selection rules:
- On Windows, when Git Bash is available, or PATH bash is clearly not a WSL stub, use bash/Git Bash for POSIX commands such as `ls`, `grep`, `cat`, `mkdir -p`, and bash scripts.
- On Windows, when Git Bash is unavailable and PATH bash is unavailable or only a WSL stub, do not use POSIX commands; prefer PowerShell or cmd.
- Do not wrap PowerShell cmdlets in bash; invoke PowerShell directly."""

    shell_path = shutil.which("bash") or shutil.which("sh")
    if language == "cn":
        return f"""Shell 能力：
- Bash/sh：{_status(language, shell_path)}

Shell 选择规则：
- Linux/macOS 通常使用 bash/sh 风格命令。
- 如果 bash/sh 不可用，使用当前平台可用的 shell 或专用工具。"""
    return f"""Shell capabilities:
- Bash/sh: {_status(language, shell_path)}

Shell selection rules:
- Linux/macOS usually use bash/sh-style commands.
- If bash/sh is unavailable, use the current platform's available shell or dedicated tools."""
