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
    # Packaged runtimes must never silently switch to a host Git Bash.
    if (os.environ.get("CLAW_RUNTIME_SOURCE") or "").strip().lower() == "managed":
        for env_name in ("CLAW_GIT_BASH_EXE", "GIT_BASH"):
            env_value = os.environ.get(env_name)
            if not env_value:
                continue
            return _existing_executable(
                Path(os.path.expandvars(env_value.strip('"'))).expanduser()
            )
        return None

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


def _tool_routing_prompt(
    language: str,
    *,
    bash_available: bool = True,
    powershell_available: bool = True,
) -> str:
    """Return the shared command-tool routing contract."""
    if language == "cn":
        if bash_available:
            posix_rule = (
                "- 普通 POSIX 命令、Bash 脚本、`grep`、`find`、`sed`、`awk`、`mkdir -p` 使用 `bash`；"
                "`bash` 工具本身已经固定了 Shell，不需要 `shell_type`。"
            )
        else:
            posix_rule = (
                "- 当前未检测到可用 Bash；不要直接使用 POSIX 命令，改用可用的 Windows Shell 或专用工具。"
            )
        windows_rule = (
            "- PowerShell cmdlet、Windows 原生路径、注册表、服务和 `$env:` 变量使用 `powershell`。"
            if powershell_available
            else "- 当前未检测到 PowerShell；Windows 原生命令使用可用的 `cmd`。"
        )
        return f"""工具与 Shell 选择：
- 文件读取、搜索和编辑优先使用 `read_file`、`grep`、`glob`、`edit_file`、`write_file` 等专用工具，不要为了这些操作调用 Shell。
{posix_rule}
{windows_rule}
- 只有在需要显式参数化 Shell、后台启动或当前没有合适的专用 Shell 工具时，才使用 `mcp_exec_command`。
- 调用 `mcp_exec_command` 时必须同时提供 `command` 和 `shell_type`；`shell_type` 只能是 `bash`、`powershell`、`cmd`、`sh`，禁止省略，也禁止使用 `auto`。
- 不要在 Bash 中包裹 PowerShell，也不要在 PowerShell 中拼接 Bash 语法。"""

    if bash_available:
        posix_rule = (
            "- Use `bash` for ordinary POSIX commands, Bash scripts, `grep`, `find`, `sed`, `awk`, "
            "and `mkdir -p`; the `bash` tool already fixes the Shell and does not need `shell_type`."
        )
    else:
        posix_rule = "- Bash is unavailable; do not issue raw POSIX commands. Use an available Windows Shell or a dedicated tool."
    windows_rule = (
        "- Use `powershell` for PowerShell cmdlets, Windows-native paths, registry, services, and `$env:` variables."
        if powershell_available
        else "- PowerShell is unavailable; use the available `cmd` tool for Windows-native commands."
    )
    return f"""Tool and Shell selection:
- Prefer dedicated tools such as `read_file`, `grep`, `glob`, `edit_file`, and `write_file` for file reads, searches, and edits instead of invoking a Shell.
{posix_rule}
{windows_rule}
- Use `mcp_exec_command` only when explicit Shell parameterization, background execution, or a dedicated Shell tool is unavailable.
- Every `mcp_exec_command` call must provide both `command` and `shell_type`; `shell_type` must be `bash`, `powershell`, `cmd`, or `sh`. Do not omit it or use `auto`.
- Do not wrap PowerShell in Bash or mix Bash syntax into PowerShell."""


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
- PowerShell cmdlet 不要包在 bash 里执行，应直接使用 PowerShell。
- 安装 Python 依赖时，使用 `python -m pip install` 而非 `pip install`，确保依赖安装到与执行脚本相同的 Python 环境中。
{_tool_routing_prompt(language, bash_available=bool(git_bash or (path_bash and not _is_wsl_bash_path(path_bash))), powershell_available=bool(shutil.which("pwsh") or shutil.which("powershell")))}"""
        return f"""Shell capabilities:
- PowerShell: {_status(language, powershell)}
- Git Bash: {_status(language, git_bash)}
- PATH bash: {_path_bash_status(language, path_bash)}

Shell selection rules:
- On Windows, when Git Bash is available, or PATH bash is clearly not a WSL stub, use bash/Git Bash for POSIX commands such as `ls`, `grep`, `cat`, `mkdir -p`, and bash scripts.
- On Windows, when Git Bash is unavailable and PATH bash is unavailable or only a WSL stub, do not use POSIX commands; prefer PowerShell or cmd.
- Do not wrap PowerShell cmdlets in bash; invoke PowerShell directly.
- When installing Python dependencies, use `python -m pip install` instead of `pip install` to ensure packages are installed into the same Python environment that will execute the scripts.
- Use command and path syntax that matches the current platform.

{_tool_routing_prompt(language, bash_available=bool(git_bash or (path_bash and not _is_wsl_bash_path(path_bash))), powershell_available=bool(shutil.which("pwsh") or shutil.which("powershell")))}"""

    shell_path = shutil.which("bash") or shutil.which("sh")
    if language == "cn":
        return f"""Shell 能力：
- Bash/sh：{_status(language, shell_path)}

Shell 选择规则：
- Linux/macOS 使用 bash/sh 风格命令；bash/sh 不可用时，使用当前平台实际可用的 Shell 或专用工具。
- 使用与当前平台匹配的命令和路径语法。

{_tool_routing_prompt(language, bash_available=bool(shutil.which("bash")), powershell_available=bool(shutil.which("pwsh") or shutil.which("powershell")))}"""
    return f"""Shell capabilities:
- Bash/sh: {_status(language, shell_path)}

Shell selection rules:
- On Linux/macOS, use bash/sh-style commands; if bash/sh is unavailable, use the Shell actually available on the platform or a dedicated tool.
- Use command and path syntax that matches the current platform.

{_tool_routing_prompt(language, bash_available=bool(shutil.which("bash")), powershell_available=bool(shutil.which("pwsh") or shutil.which("powershell")))}"""
