# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""浏览器 / Web 通道使用的本机文件选择器（非 pywebview）。

对齐 ``directory_picker.py``（commit 87cdbaf3）：
- Windows：进程内 tkinter 文件对话框（与选目录一致；不可用 ``sys.executable -c``，
  冻结 exe 下 ``sys.executable`` 不是 Python 解释器）
- macOS：osascript（独立进程）
- Linux：zenity → kdialog → yad → tkinter 子进程
- 均不可用时抛出 ``RuntimeError``

对话框过滤器故意省略黑名单扩展名（与浏览器 accept= / 桌面端白名单一致），
选中后仍通过 ``describe_local_file`` 做二次校验。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DIALOG_TITLE = "选择文件"
# Persist last successful picker directory under the user workspace so the next
# open starts where the user left off (desktop + path.select_files share this).
_LAST_DIR_FILENAME = "last_file_picker_dir.txt"
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".jfif"})
MAX_IMAGE_BYTES = 10 * 1024 * 1024
FORBIDDEN_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".exe",
        ".dll",
        ".msi",
        ".scr",
        ".bat",
        ".cmd",
        ".ps1",
        ".vbs",
        ".wsf",
        ".hta",
        ".jar",
        ".lnk",
        ".bin",
        ".so",
        ".dylib",
        ".app",
        ".dmg",
        ".pkg",
        ".command",
        ".scpt",
        ".scptd",
        ".workflow",
        ".xpc",
        ".bundle",
        ".framework",
        ".kext",
        ".prefpane",
        ".saver",
        ".component",
    }
)
# Keep in sync with InputArea ATTACHMENT_ACCEPT / desktop ATTACHMENT_DIALOG_EXTENSIONS.
# Intentionally omits FORBIDDEN_DOCUMENT_EXTENSIONS (no *.* either).
ATTACHMENT_DIALOG_EXTENSIONS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".svg",
    ".ico",
    ".jfif",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".rtf",
    ".odt",
    ".ods",
    ".odp",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".py",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".sql",
    ".ipynb",
    ".toml",
    ".ini",
    ".log",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".mp3",
    ".wav",
    ".flac",
    ".aac",
    ".ogg",
    ".m4a",
    ".wma",
    ".mp4",
    ".avi",
    ".mov",
    ".mkv",
    ".webm",
    ".wmv",
    ".flv",
)


def _last_dir_state_path() -> Path:
    try:
        from jiuwenswarm.common.utils import get_user_workspace_dir

        base = get_user_workspace_dir()
    except Exception:  # noqa: BLE001
        base = Path.home() / ".jiuwenswarm"
    return base / _LAST_DIR_FILENAME


def get_last_file_picker_dir() -> str | None:
    """Return the last remembered picker directory if it still exists."""
    try:
        raw = _last_dir_state_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_dir():
        return None
    try:
        return str(candidate.resolve())
    except Exception:  # noqa: BLE001
        return str(candidate)


def remember_file_picker_dir(path: str | Path | None) -> None:
    """Remember a directory (or the parent of a selected file) for the next open."""
    if path is None:
        return
    try:
        candidate = Path(path).expanduser()
        directory = candidate if candidate.is_dir() else candidate.parent
        if not directory.is_dir():
            return
        try:
            stored = str(directory.resolve())
        except Exception:  # noqa: BLE001
            stored = str(directory)
        state_path = _last_dir_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(stored + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[file_picker] failed to remember directory: %s", exc)


def resolve_file_picker_initial_dir(initial_dir: str | None = None) -> str:
    """Resolve dialog start directory: explicit → last remembered → home."""
    for raw in (initial_dir, get_last_file_picker_dir()):
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if candidate.is_dir():
            try:
                return str(candidate.resolve())
            except Exception:  # noqa: BLE001
                return str(candidate)
    return str(Path.home())


def _resolve_initial_dir(initial_dir: str | None) -> str:
    return resolve_file_picker_initial_dir(initial_dir)


def _normalize_selected_path(selected: str | None) -> str | None:
    if not selected:
        return None
    path = Path(str(selected).strip().strip('"')).expanduser()
    if not str(path):
        return None
    try:
        return str(path.resolve())
    except Exception:  # noqa: BLE001
        return str(path)


def _normalize_selected_paths(raw_paths: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if raw_paths is None:
        return []
    if isinstance(raw_paths, str):
        candidates = [raw_paths]
    else:
        candidates = list(raw_paths)
    results: list[str] = []
    for item in candidates:
        normalized = _normalize_selected_path(item)
        if normalized:
            results.append(normalized)
    return results


def _tk_filetypes() -> list[tuple[str, str]]:
    patterns = " ".join(f"*{ext}" for ext in ATTACHMENT_DIALOG_EXTENSIONS)
    return [("Allowed files", patterns)]


def _run_capture(command: list[str], *, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def describe_local_file(raw_path: str | Path) -> dict[str, Any] | None:
    """把本机路径描述成前端 ``LocalFilePick`` 同形字典。"""
    try:
        path = Path(raw_path).expanduser().resolve()
    except Exception:  # noqa: BLE001
        path = Path(raw_path).expanduser()

    if not path.is_file():
        logger.warning("[file_picker] selected path is not a file: %s", path)
        return None

    filename = path.name
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError as exc:
        logger.warning("[file_picker] failed to stat selected file %s: %s", path, exc)
        return None

    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    absolute = str(path)
    if ext in IMAGE_EXTENSIONS:
        if size > MAX_IMAGE_BYTES:
            return {
                "path": absolute,
                "filename": filename,
                "size": size,
                "mime_type": mime_type,
                "kind": "image",
                "error": "image_too_large",
            }
        try:
            payload = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            logger.warning("[file_picker] failed to read image %s: %s", path, exc)
            return {
                "path": absolute,
                "filename": filename,
                "size": size,
                "mime_type": mime_type,
                "kind": "image",
                "error": "read_failed",
            }
        return {
            "path": absolute,
            "filename": filename,
            "size": size,
            "mime_type": mime_type,
            "kind": "image",
            "base64": payload,
        }

    if ext in FORBIDDEN_DOCUMENT_EXTENSIONS:
        return {
            "path": absolute,
            "filename": filename,
            "size": size,
            "mime_type": mime_type,
            "kind": "document",
            "error": "forbidden",
        }

    return {
        "path": absolute,
        "filename": filename,
        "size": size,
        "mime_type": mime_type,
        "kind": "document",
    }


def describe_local_files(paths: list[str]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for raw in paths:
        item = describe_local_file(raw)
        if item is not None:
            results.append(item)
    return results


def _select_files_windows_tk(*, initial_dir: str, allow_multiple: bool) -> list[str] | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"file picker unavailable: {exc}") from exc

    root = tk.Tk()
    root.withdraw()
    try:
        try:
            root.attributes("-topmost", True)
        except Exception:  # noqa: BLE001
            pass
        try:
            root.lift()
            root.focus_force()
        except Exception:  # noqa: BLE001
            pass

        kwargs = {
            "parent": root,
            "initialdir": initial_dir,
            "title": _DIALOG_TITLE,
            "filetypes": _tk_filetypes(),
        }
        if allow_multiple:
            selected = filedialog.askopenfilenames(**kwargs)
        else:
            selected = filedialog.askopenfilename(**kwargs)
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    if not selected:
        return None
    paths = _normalize_selected_paths(selected)
    return paths or None


def _select_files_macos(*, initial_dir: str, allow_multiple: bool) -> list[str] | None:
    multi = " with multiple selections allowed" if allow_multiple else ""
    # Handle both single alias and list return values from `choose file`.
    body = (
        "set posixList to {}\n"
        "if class of chosen is list then\n"
        "repeat with f in chosen\n"
        "set end of posixList to POSIX path of f\n"
        "end repeat\n"
        "else\n"
        "set end of posixList to POSIX path of chosen\n"
        "end if\n"
        "set AppleScript's text item delimiters to linefeed\n"
        "return posixList as text"
    )
    script_with_default = (
        f'set defaultFolder to POSIX file "{initial_dir}"\n'
        f'set chosen to choose file with prompt "{_DIALOG_TITLE}" '
        f"default location defaultFolder{multi}\n"
        f"{body}"
    )
    script_plain = (
        f'set chosen to choose file with prompt "{_DIALOG_TITLE}"{multi}\n'
        f"{body}"
    )

    for script in (script_with_default, script_plain):
        try:
            completed = _run_capture(["osascript", "-e", script])
        except FileNotFoundError as exc:
            raise RuntimeError("file picker unavailable: osascript not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("file picker timed out") from exc

        if completed.returncode == 0:
            lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
            paths = _normalize_selected_paths(lines)
            return paths or None
        stderr = (completed.stderr or "").strip()
        if "User canceled" in stderr or "-128" in stderr:
            return None
        if script is script_with_default:
            continue
        raise RuntimeError(f"file picker unavailable: {stderr or 'osascript failed'}")
    return None


def _zenity_file_filter() -> str:
    patterns = " ".join(f"*{ext}" for ext in ATTACHMENT_DIALOG_EXTENSIONS)
    return f"Allowed files | {patterns}"


def _try_zenity_files(*, initial_dir: str, allow_multiple: bool) -> list[str] | None:
    command = [
        "zenity",
        "--file-selection",
        f"--title={_DIALOG_TITLE}",
        f"--filename={initial_dir.rstrip(os.sep) + os.sep}",
        f"--file-filter={_zenity_file_filter()}",
    ]
    if allow_multiple:
        command.append("--multiple")
        command.append("--separator=\n")
    completed = _run_capture(command)
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "zenity failed")
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    paths = _normalize_selected_paths(lines)
    return paths or None


def _try_kdialog_files(*, initial_dir: str, allow_multiple: bool) -> list[str] | None:
    command = ["kdialog", "--getopenfilename", initial_dir, "--title", _DIALOG_TITLE]
    if allow_multiple:
        command.append("--multiple")
    # Filter string: '*.txt *.md|Allowed files'
    patterns = " ".join(f"*{ext}" for ext in ATTACHMENT_DIALOG_EXTENSIONS)
    command.append(f"{patterns}|Allowed files")
    completed = _run_capture(command)
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "kdialog failed")
    # kdialog --multiple separates with spaces that may break paths; prefer newline if present.
    raw = (completed.stdout or "").strip()
    if "\n" in raw:
        candidates = raw.splitlines()
    else:
        candidates = [raw] if raw else []
    paths = _normalize_selected_paths(candidates)
    return paths or None


def _try_yad_files(*, initial_dir: str, allow_multiple: bool) -> list[str] | None:
    command = [
        "yad",
        "--file",
        f"--title={_DIALOG_TITLE}",
        f"--filename={initial_dir.rstrip(os.sep) + os.sep}",
        f"--file-filter={_zenity_file_filter()}",
    ]
    if allow_multiple:
        command.append("--multiple")
    completed = _run_capture(command)
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "yad failed")
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    # yad may separate with |
    if len(lines) == 1 and "|" in lines[0]:
        lines = [part for part in lines[0].split("|") if part.strip()]
    paths = _normalize_selected_paths(lines)
    return paths or None


def _try_tkinter_subprocess_files(*, initial_dir: str, allow_multiple: bool) -> list[str] | None:
    # Frozen builds (PyInstaller etc.): sys.executable is the app binary, not CPython.
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            return _select_files_windows_tk(
                initial_dir=initial_dir,
                allow_multiple=allow_multiple,
            )
        raise RuntimeError(
            "file picker subprocess unavailable in frozen build "
            f"(sys.executable={sys.executable!r})"
        )

    env = os.environ.copy()
    env["JIUWEN_FILE_PICKER_INITIAL"] = initial_dir
    env["JIUWEN_FILE_PICKER_TITLE"] = _DIALOG_TITLE
    env["JIUWEN_FILE_PICKER_MULTIPLE"] = "1" if allow_multiple else "0"
    env["JIUWEN_FILE_PICKER_PATTERNS"] = " ".join(f"*{ext}" for ext in ATTACHMENT_DIALOG_EXTENSIONS)
    script = r"""
import os, sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as exc:
    sys.stderr.write(f"tkinter unavailable: {exc}\n")
    sys.exit(2)

initial = os.environ.get("JIUWEN_FILE_PICKER_INITIAL") or str(os.path.expanduser("~"))
title = os.environ.get("JIUWEN_FILE_PICKER_TITLE") or "Select File"
multiple = os.environ.get("JIUWEN_FILE_PICKER_MULTIPLE") == "1"
patterns = os.environ.get("JIUWEN_FILE_PICKER_PATTERNS") or "*.*"
filetypes = [("Allowed files", patterns)]
root = tk.Tk()
root.withdraw()
try:
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        root.lift()
        root.focus_force()
    except Exception:
        pass
    kwargs = dict(parent=root, initialdir=initial, title=title, filetypes=filetypes)
    selected = filedialog.askopenfilenames(**kwargs) if multiple else filedialog.askopenfilename(**kwargs)
finally:
    try:
        root.destroy()
    except Exception:
        pass

if not selected:
    sys.exit(1)
if isinstance(selected, str):
    sys.stdout.write(selected)
else:
    sys.stdout.write("\n".join(selected))
sys.exit(0)
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=600.0,
        env=env,
    )
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "tkinter subprocess failed")
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    paths = _normalize_selected_paths(lines)
    return paths or None


def _select_files_linux(*, initial_dir: str, allow_multiple: bool) -> list[str] | None:
    backends: list[tuple[str, Callable[..., list[str] | None]]] = []
    if shutil.which("zenity"):
        backends.append(("zenity", _try_zenity_files))
    if shutil.which("kdialog"):
        backends.append(("kdialog", _try_kdialog_files))
    if shutil.which("yad"):
        backends.append(("yad", _try_yad_files))
    backends.append(("tkinter", _try_tkinter_subprocess_files))

    errors: list[str] = []
    for name, fn in backends:
        try:
            return fn(initial_dir=initial_dir, allow_multiple=allow_multiple)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("file picker timed out") from exc
        except Exception as exc:  # noqa: BLE001
            msg = f"{name}: {exc}"
            logger.warning("[file_picker] linux backend failed: %s", msg)
            errors.append(msg)
            continue

    detail = "; ".join(errors) if errors else "no backend available"
    raise RuntimeError(
        f"file picker unavailable on Linux: {detail}; "
        "fallback to HTML file input"
    )


def select_files_native(
    *,
    allow_multiple: bool = True,
    initial_dir: str | None = None,
) -> list[str] | None:
    """打开本机系统文件对话框，返回选中文件的绝对路径列表。

    用户取消时返回 ``None``。
    """
    start_dir = _resolve_initial_dir(initial_dir)

    if sys.platform == "win32":
        # 与 directory_picker 一致：Windows 上在 asyncio.to_thread 里用进程内 Tk。
        # 旧实现用 subprocess([sys.executable, "-c", ...])，在 jiuwenswarm.exe
        # 冻结包下会失败（executable 不是 Python），表现为浏览器「添加文件」无弹窗。
        return _select_files_windows_tk(
            initial_dir=start_dir,
            allow_multiple=allow_multiple,
        )
    if sys.platform == "darwin":
        return _select_files_macos(initial_dir=start_dir, allow_multiple=allow_multiple)
    if sys.platform.startswith("linux"):
        return _select_files_linux(initial_dir=start_dir, allow_multiple=allow_multiple)

    raise RuntimeError(
        f"file picker unavailable on platform {sys.platform!r}; "
        "fallback to HTML file input"
    )


def select_and_describe_files(
    *,
    allow_multiple: bool = True,
    initial_dir: str | None = None,
) -> list[dict[str, Any]] | None:
    """弹窗选文件并返回前端可用的元数据列表；取消返回 ``None``。"""
    selected = select_files_native(allow_multiple=allow_multiple, initial_dir=initial_dir)
    if selected is None:
        return None
    if selected:
        remember_file_picker_dir(selected[0])
    return describe_local_files(selected)
