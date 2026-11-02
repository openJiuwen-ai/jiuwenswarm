# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Build dynamic shell environment prompt fragments."""

from __future__ import annotations

import importlib.util
import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

# 场景常用库：(展示标签, import 名, 包名)。
# 只做 find_spec 探测，不真正 import；未命中的一律不出现在提示词里，
# 避免在依赖不同的部署环境里给出"写了但没装"的错误承诺。
# 注意：xlrd / pypdf 不在标准部署的直接依赖内，标准部署下恒不渲染；
# 保留是为了覆盖预装了更多库的发行形态。
_LIB_CANDIDATES: tuple[tuple[str, str, str], ...] = (
    ("Word", "docx", "python-docx"),
    ("Excel", "openpyxl", "openpyxl"),
    ("Excel (.xls)", "xlrd", "xlrd"),
    ("Excel 写图表", "xlsxwriter", "XlsxWriter"),
    ("数据处理", "pandas", "pandas"),
    ("PPT", "pptx", "python-pptx"),
    ("PDF", "pypdf", "pypdf"),
    ("PDF", "pdfplumber", "pdfplumber"),
    ("图片", "PIL", "Pillow"),
    ("XML/HTML", "lxml", "lxml"),
)

_PDF_FALLBACK_MODULES: tuple[str, ...] = ("pypdf", "pdfplumber")


def _spec_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001 — 探测位于提示词热路径，任何异常都不得中断构建
        return False


@lru_cache(maxsize=2)
def build_python_library_prompt(language: str) -> str:
    """List Python libraries located via importlib spec probing (cached per process)."""
    found = [
        (label, module, package)
        for label, module, package in _LIB_CANDIDATES
        if _spec_available(module)
    ]
    fitz_available = _spec_available("fitz")
    # 兜底建议必须来自真实探测，不能无条件承诺未安装的库。
    pdf_fallbacks = [package for _label, module, package in found if module in _PDF_FALLBACK_MODULES]

    if language == "cn":
        lines = ["Python 库（运行时探测，仅列已探测到的）："]
        lines.extend(f"- {label}：{package}（import {module}）" for label, module, package in found)
        if fitz_available:
            lines.append("- PDF：PyMuPDF 可用（import fitz）")
        elif pdf_fallbacks:
            lines.append(f"- PDF：未探测到 fitz/PyMuPDF，改用 {' 或 '.join(pdf_fallbacks)}")
        else:
            lines.append("- PDF：未探测到 fitz/PyMuPDF、pypdf、pdfplumber，按实际 import 结果选择")
        if found:
            lines.append("以上为已探测到的库；其余以实际 import 结果为准，失败再改用上方清单。")
        else:
            lines.append("未探测到预置的常用库；以实际 import 结果为准，缺失时先安装再使用。")
        return "\n".join(lines)

    lines = ["Python libraries (runtime probe; detected only):"]
    lines.extend(f"- {label}: {package} (import {module})" for label, module, package in found)
    if fitz_available:
        lines.append("- PDF: PyMuPDF available (import fitz)")
    elif pdf_fallbacks:
        lines.append(f"- PDF: fitz/PyMuPDF not detected; use {' or '.join(pdf_fallbacks)}")
    else:
        lines.append("- PDF: none of fitz/PyMuPDF, pypdf, pdfplumber detected; trust the actual import result")
    if found:
        lines.append("The list above was detected in this runtime; for anything else trust the actual import result.")
    else:
        lines.append("No preset Office library detected; trust the actual import result and install what is missing.")
    return "\n".join(lines)


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


def _build_shell_core(language: str, os_type: str) -> str:
    """Return shell capability and selection guidance for the current host."""
    path_bash = shutil.which("bash")

    if os_type.startswith("win"):
        powershell = _available_powershell()
        git_bash = _available_git_bash()
        if git_bash:
            heredoc_cn = (
                "- heredoc 仅在 bash/Git Bash 中使用；PowerShell/cmd 不支持，"
                "其 here-string `@\"...\"@` 会展开变量。\n"
                "  写文件优先用 write_file，改文件用 edit_file。"
            )
            heredoc_en = (
                "- Use heredocs only in bash/Git Bash; PowerShell/cmd does not support them, "
                "and its here-string `@\"...\"@` expands variables.\n"
                "  Prefer write_file to create files and edit_file to modify them."
            )
        else:
            heredoc_cn = (
                "- 不要用 heredoc 写文件（`cat <<EOF`、`<<'EOF'`）：当前没有可用的 Git Bash，"
                "PowerShell/cmd 不支持；\n"
                "  其 here-string `@\"...\"@` 还会展开变量。写文件用 write_file，改文件用 edit_file。"
            )
            heredoc_en = (
                "- Do not write files with heredocs (`cat <<EOF`, `<<'EOF'`): no Git Bash is available, "
                "PowerShell/cmd does not support them,\n"
                "  and its here-string `@\"...\"@` expands variables. Use write_file and edit_file instead."
            )
        if language == "cn":
            return f"""Shell 能力：
- PowerShell：{_status(language, powershell)}
- Git Bash：{_status(language, git_bash)}
- PATH bash：{_path_bash_status(language, path_bash)}

Shell 选择规则：
- Windows 且 Git Bash 可用，或 PATH bash 明确不是 WSL stub 时，可以使用 bash/Git Bash 执行 POSIX 命令，例如 `ls`、`grep`、`cat`、`mkdir -p`、bash 脚本。
- Windows 且 Git Bash 不可用、PATH bash 也不可用或只是 WSL stub 时，不要使用 POSIX 命令；优先使用 PowerShell 或 cmd。
- PowerShell cmdlet 不要包在 bash 里执行，应直接使用 PowerShell。
{heredoc_cn}"""
        return f"""Shell capabilities:
- PowerShell: {_status(language, powershell)}
- Git Bash: {_status(language, git_bash)}
- PATH bash: {_path_bash_status(language, path_bash)}

Shell selection rules:
- On Windows, when Git Bash is available, or PATH bash is clearly not a WSL stub, use bash/Git Bash for POSIX commands such as `ls`, `grep`, `cat`, `mkdir -p`, and bash scripts.
- On Windows, when Git Bash is unavailable and PATH bash is unavailable or only a WSL stub, do not use POSIX commands; prefer PowerShell or cmd.
- Do not wrap PowerShell cmdlets in bash; invoke PowerShell directly.
{heredoc_en}"""

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


def build_shell_environment_prompt(
    language: str, os_type: str, *, include_libraries: bool = True
) -> str:
    """Return shell guidance, optionally followed by the runtime library list.

    The list belongs to the static identity section. Callers that append a
    second env section to the same system prompt pass ``include_libraries=False``
    when the identity section already carries it, so it is emitted exactly once.
    """
    core = _build_shell_core(language, os_type)
    if not include_libraries:
        return core
    return f"{core}\n\n{build_python_library_prompt(language)}"
