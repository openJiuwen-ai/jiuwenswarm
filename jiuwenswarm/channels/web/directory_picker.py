# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""浏览器 / Web 通道使用的本机目录选择器（非 pywebview）。

策略：
- Windows：沿用 tkinter 文件夹对话框（当前实现）
- macOS：osascript（独立进程）
- Linux：zenity → kdialog → yad → tkinter 子进程（均在独立进程，
  避免 ``asyncio.to_thread`` 工作线程里创建 ``Tk()``）
- 均不可用时抛出 ``RuntimeError``，由前端回落到「手填绝对路径」
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_DIALOG_TITLE = "选择项目目录"
_FILE_DIALOG_TITLE = "Select executable file"


def _resolve_initial_dir(initial_dir: str | None) -> str:
    if initial_dir:
        candidate = Path(initial_dir).expanduser()
        if candidate.is_dir():
            try:
                return str(candidate.resolve())
            except Exception:  # noqa: BLE001
                return str(candidate)
    return str(Path.home())


def _normalize_selected(selected: str | None) -> str | None:
    if not selected:
        return None
    path = Path(str(selected).strip()).expanduser()
    if not str(path):
        return None
    try:
        return str(path.resolve())
    except Exception:  # noqa: BLE001
        return str(path)


def _run_capture(command: list[str], *, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _select_directory_windows_tk(initial_dir: str) -> str | None:
    """Windows：保留 tkinter 实现。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"directory picker unavailable: {exc}") from exc

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

        selected = filedialog.askdirectory(
            parent=root,
            initialdir=initial_dir,
            mustexist=True,
            title=_DIALOG_TITLE,
        )
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    return _normalize_selected(selected)


def _select_file_windows_tk(initial_dir: str, title: str) -> str | None:
    """Open a native file picker on Windows."""
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

        selected = filedialog.askopenfilename(
            parent=root,
            initialdir=initial_dir,
            title=title,
            filetypes=[("Executable files", "*.exe"), ("All files", "*.*")],
        )
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    return _normalize_selected(selected)


def _select_directory_macos(initial_dir: str) -> str | None:
    """macOS：AppleScript choose folder（独立进程，可在工作线程调用）。"""
    script_with_default = (
        f'set defaultFolder to POSIX file "{initial_dir}"\n'
        f'set chosenFolder to choose folder with prompt "{_DIALOG_TITLE}" default location defaultFolder\n'
        "POSIX path of chosenFolder"
    )
    script_plain = (
        f'set chosenFolder to choose folder with prompt "{_DIALOG_TITLE}"\n'
        "POSIX path of chosenFolder"
    )
    for script in (script_with_default, script_plain):
        try:
            completed = _run_capture(["osascript", "-e", script])
        except FileNotFoundError as exc:
            raise RuntimeError("directory picker unavailable: osascript not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("directory picker timed out") from exc

        if completed.returncode == 0:
            return _normalize_selected(completed.stdout)
        stderr = (completed.stderr or "").strip()
        if "User canceled" in stderr or "-128" in stderr:
            return None
        if script is script_with_default:
            continue
        raise RuntimeError(f"directory picker unavailable: {stderr or 'osascript failed'}")
    return None


def _select_file_macos(initial_dir: str, title: str) -> str | None:
    """Open a native file picker on macOS."""
    script_with_default = (
        f'set defaultFolder to POSIX file "{initial_dir}"\n'
        f'set chosenFile to choose file with prompt "{title}" default location defaultFolder\n'
        "POSIX path of chosenFile"
    )
    script_plain = (
        f'set chosenFile to choose file with prompt "{title}"\n'
        "POSIX path of chosenFile"
    )
    for script in (script_with_default, script_plain):
        try:
            completed = _run_capture(["osascript", "-e", script])
        except FileNotFoundError as exc:
            raise RuntimeError("file picker unavailable: osascript not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("file picker timed out") from exc

        if completed.returncode == 0:
            return _normalize_selected(completed.stdout)
        stderr = (completed.stderr or "").strip()
        if "User canceled" in stderr or "-128" in stderr:
            return None
        if script is script_with_default:
            continue
        raise RuntimeError(f"file picker unavailable: {stderr or 'osascript failed'}")
    return None


def _try_zenity(initial_dir: str) -> str | None:
    completed = _run_capture(
        [
            "zenity",
            "--file-selection",
            "--directory",
            f"--title={_DIALOG_TITLE}",
            f"--filename={initial_dir.rstrip(os.sep) + os.sep}",
        ],
    )
    # zenity: 0=ok, 1=cancel, 其他=错误
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "zenity failed")
    return _normalize_selected(completed.stdout)


def _try_kdialog(initial_dir: str) -> str | None:
    completed = _run_capture(
        ["kdialog", "--getexistingdirectory", initial_dir, "--title", _DIALOG_TITLE],
    )
    # kdialog: 0=ok, 1=cancel
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "kdialog failed")
    return _normalize_selected(completed.stdout)


def _try_yad(initial_dir: str) -> str | None:
    completed = _run_capture(
        [
            "yad",
            "--file",
            "--directory",
            f"--title={_DIALOG_TITLE}",
            f"--filename={initial_dir.rstrip(os.sep) + os.sep}",
        ],
    )
    # yad 与 zenity 类似：0=ok, 1=cancel
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "yad failed")
    return _normalize_selected(completed.stdout)


def _try_zenity_file(initial_dir: str, title: str) -> str | None:
    completed = _run_capture(
        [
            "zenity",
            "--file-selection",
            f"--title={title}",
            f"--filename={initial_dir.rstrip(os.sep) + os.sep}",
        ],
    )
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "zenity failed")
    return _normalize_selected(completed.stdout)


def _try_kdialog_file(initial_dir: str, title: str) -> str | None:
    completed = _run_capture(
        ["kdialog", "--getopenfilename", initial_dir, "--title", title],
    )
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "kdialog failed")
    return _normalize_selected(completed.stdout)


def _try_yad_file(initial_dir: str, title: str) -> str | None:
    completed = _run_capture(
        [
            "yad",
            "--file",
            f"--title={title}",
            f"--filename={initial_dir.rstrip(os.sep) + os.sep}",
        ],
    )
    if completed.returncode == 1:
        return None
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise RuntimeError(stderr or "yad failed")
    return _normalize_selected(completed.stdout)


def _try_tkinter_subprocess(initial_dir: str) -> str | None:
    """在独立子进程中跑 tkinter，保证 Tk 位于该进程主线程。

    可安全从 ``asyncio.to_thread`` 工作线程调用，适用于 Ubuntu 等未安装
    zenity/kdialog、但已安装 ``python3-tk`` 的桌面环境。
    """
    env = os.environ.copy()
    env["JIUWEN_DIR_PICKER_INITIAL"] = initial_dir
    env["JIUWEN_DIR_PICKER_TITLE"] = _DIALOG_TITLE
    script = r"""
import os, sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception as exc:
    sys.stderr.write(f"tkinter unavailable: {exc}\n")
    sys.exit(2)

initial = os.environ.get("JIUWEN_DIR_PICKER_INITIAL") or str(os.path.expanduser("~"))
title = os.environ.get("JIUWEN_DIR_PICKER_TITLE") or "Select Directory"
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
    selected = filedialog.askdirectory(
        parent=root,
        initialdir=initial,
        mustexist=True,
        title=title,
    )
finally:
    try:
        root.destroy()
    except Exception:
        pass

if selected:
    sys.stdout.write(selected)
    sys.exit(0)
sys.exit(1)
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
    return _normalize_selected(completed.stdout)


def _try_tkinter_file_subprocess(initial_dir: str, title: str) -> str | None:
    """Run tkinter in a separate process so Tk is created on that process main thread."""
    env = os.environ.copy()
    env["JIUWEN_FILE_PICKER_INITIAL"] = initial_dir
    env["JIUWEN_FILE_PICKER_TITLE"] = title
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
    selected = filedialog.askopenfilename(
        parent=root,
        initialdir=initial,
        title=title,
    )
finally:
    try:
        root.destroy()
    except Exception:
        pass

if selected:
    sys.stdout.write(selected)
    sys.exit(0)
sys.exit(1)
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
    return _normalize_selected(completed.stdout)


def _select_directory_linux(initial_dir: str) -> str | None:
    """Linux：依次尝试 zenity / kdialog / yad / tkinter 子进程。"""
    backends: list[tuple[str, Callable[[str], str | None]]] = []
    if shutil.which("zenity"):
        backends.append(("zenity", _try_zenity))
    if shutil.which("kdialog"):
        backends.append(("kdialog", _try_kdialog))
    if shutil.which("yad"):
        backends.append(("yad", _try_yad))
    # Ubuntu 桌面通常有 python3-tk；即使没有 zenity 也可弹窗
    backends.append(("tkinter", _try_tkinter_subprocess))

    errors: list[str] = []
    for name, fn in backends:
        try:
            return fn(initial_dir)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("directory picker timed out") from exc
        except Exception as exc:  # noqa: BLE001
            msg = f"{name}: {exc}"
            logger.warning("[directory_picker] linux backend failed: %s", msg)
            errors.append(msg)
            continue

    detail = "; ".join(errors) if errors else "no backend available"
    raise RuntimeError(
        f"directory picker unavailable on Linux: {detail}; "
        "fallback to typing an absolute path in the UI"
    )


def _select_file_linux(initial_dir: str, title: str) -> str | None:
    """Try Linux native file picker backends in order."""
    backends: list[tuple[str, Callable[[str, str], str | None]]] = []
    if shutil.which("zenity"):
        backends.append(("zenity", _try_zenity_file))
    if shutil.which("kdialog"):
        backends.append(("kdialog", _try_kdialog_file))
    if shutil.which("yad"):
        backends.append(("yad", _try_yad_file))
    backends.append(("tkinter", _try_tkinter_file_subprocess))

    errors: list[str] = []
    for name, fn in backends:
        try:
            return fn(initial_dir, title)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("file picker timed out") from exc
        except Exception as exc:  # noqa: BLE001
            msg = f"{name}: {exc}"
            logger.warning("[directory_picker] linux file backend failed: %s", msg)
            errors.append(msg)
            continue

    detail = "; ".join(errors) if errors else "no backend available"
    raise RuntimeError(
        f"file picker unavailable on Linux: {detail}; "
        "fallback to typing an absolute path in the UI"
    )


def select_directory_native(*, initial_dir: str | None = None) -> str | None:
    """打开本机系统文件夹对话框，返回选中目录的绝对路径。

    - Windows：tkinter
    - macOS：osascript
    - Linux：zenity / kdialog / yad / tkinter 子进程
    - 其它平台或工具缺失：抛 ``RuntimeError``，前端回落手填绝对路径

    用户取消时返回 ``None``。
    """
    start_dir = _resolve_initial_dir(initial_dir)

    if sys.platform == "win32":
        return _select_directory_windows_tk(start_dir)
    if sys.platform == "darwin":
        return _select_directory_macos(start_dir)
    if sys.platform.startswith("linux"):
        return _select_directory_linux(start_dir)

    raise RuntimeError(
        f"directory picker unavailable on platform {sys.platform!r}; "
        "fallback to typing an absolute path in the UI"
    )


def select_file_native(*, initial_dir: str | None = None, title: str | None = None) -> str | None:
    """Open a native file picker and return the selected absolute path.

    The caller is responsible for validating whether the selected file is a
    usable executable for its domain.
    """
    start_dir = _resolve_initial_dir(initial_dir)
    dialog_title = title.strip() if isinstance(title, str) and title.strip() else _FILE_DIALOG_TITLE

    if sys.platform == "win32":
        return _select_file_windows_tk(start_dir, dialog_title)
    if sys.platform == "darwin":
        return _select_file_macos(start_dir, dialog_title)
    if sys.platform.startswith("linux"):
        return _select_file_linux(start_dir, dialog_title)

    raise RuntimeError(
        f"file picker unavailable on platform {sys.platform!r}; "
        "fallback to typing an absolute path in the UI"
    )
