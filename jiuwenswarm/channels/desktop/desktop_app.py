from __future__ import annotations

import argparse
import base64
import ctypes
import http.client
import json
import logging
import mimetypes
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable

from logging.handlers import RotatingFileHandler

import webview

from jiuwenswarm.common._build_config import (
    APP_BUNDLE_NAME,
    BUNDLE_IDENTIFIER,
    DISPLAY_NAME,
    EXECUTABLE_NAME,
)
from jiuwenswarm.common.startup_diagnostics import (
    DOCTOR_FLAG,
    DOCTOR_OUTPUT_FLAG,
    DOCTOR_TIMEOUT_SECONDS,
    STARTUP_DIAGNOSTICS_DIR_ENV,
    is_native_startup_failure,
    load_doctor_result,
    load_startup_failures,
    select_blocking_doctor_check,
    select_startup_failure,
)
from jiuwenswarm.common.utils import (
    get_user_workspace_dir,
    get_logs_dir,
    prepare_runtime_workspace,
    wait_for_pid_exit,
    wait_for_tcp_port,
)
from jiuwenswarm.instance_manager.config import (
    BASE_PORTS,
    PORT_TYPES,
    find_available_ports,
)


BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = int(BASE_PORTS["web"])
FRONTEND_HOST = "127.0.0.1"
FRONTEND_PORT = int(BASE_PORTS["frontend"])
DESKTOP_PORT_SCAN_RANGE = 10
APP_CHILD_FLAG = "--desktop-run-app"
WEB_CHILD_FLAG = "--desktop-run-web"
AGENT_CHILD_FLAG = "--desktop-run-agent"
GATEWAY_CHILD_FLAG = "--desktop-run-gateway"
UPDATE_HELPER_FLAG = "--desktop-install-update"
DESKTOP_ENV_FLAG = "JIUWENSWARM_DESKTOP"
STARTUP_TIMEOUT_SECONDS = 45.0
STARTUP_DOCTOR_TIMEOUT_SECONDS = DOCTOR_TIMEOUT_SECONDS + 15.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DESKTOP_BLOB_CHUNK_SIZE = 1024 * 1024
MAX_JAVASCRIPT_SAFE_INTEGER = 9_007_199_254_740_991


@dataclass(frozen=True)
class _DataUrlExportSpec:
    allowed_suffixes: frozenset[str]
    allowed_parameters: frozenset[str]
    file_types: tuple[str, ...]


@dataclass
class _BlobSaveTransfer:
    expected_size: int
    export_file: BinaryIO
    mime_type: str
    target_path: Path
    temp_path: Path
    bytes_written: int = 0
    signature: bytes = b""


DATA_URL_EXPORT_SPECS = {
    "image/png": _DataUrlExportSpec(
        allowed_suffixes=frozenset({".png"}),
        allowed_parameters=frozenset(),
        file_types=("PNG Image (*.png)",),
    ),
    "image/svg+xml": _DataUrlExportSpec(
        allowed_suffixes=frozenset({".svg"}),
        allowed_parameters=frozenset({"charset=utf-8"}),
        file_types=("SVG Image (*.svg)",),
    ),
    "text/plain": _DataUrlExportSpec(
        allowed_suffixes=frozenset({".mmd"}),
        allowed_parameters=frozenset({"charset=utf-8"}),
        file_types=("Mermaid Diagram (*.mmd)",),
    ),
    "application/json": _DataUrlExportSpec(
        allowed_suffixes=frozenset({".json"}),
        allowed_parameters=frozenset({"charset=utf-8"}),
        file_types=("JSON Archive (*.json)",),
    ),
}
DesktopSaveResult = dict[str, bool]
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".jfif"})
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# Keep in sync with frontend/document_attachments forbidden list.
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
# Dialog allow-list (UI filter only). Keep in sync with InputArea ATTACHMENT_ACCEPT.
# Intentionally omits FORBIDDEN_DOCUMENT_EXTENSIONS and does NOT include *.* so
# Windows/macOS pickers hide blacklist types the same way the browser accept= does.
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
    # audio/* / video/* from browser accept=
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
UPDATE_CLEANUP_PATTERNS = (
    "*.exe",
    "*.dmg",
    "*.tar.gz",
    "*.exe.part",
    "*.dmg.part",
    "*.tar.gz.part",
    "_install_helper.ps1",
    "_install_helper.sh",
)


def _setup_logger() -> logging.Logger:
    logs_dir = get_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)

    desktop_logger = logging.getLogger("jiuwenswarm.channels.desktop")
    desktop_logger.setLevel(logging.INFO)
    desktop_logger.propagate = False

    for handler in desktop_logger.handlers[:]:
        handler.close()
        desktop_logger.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        filename=logs_dir / "desktop.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    desktop_logger.addHandler(stream_handler)
    desktop_logger.addHandler(file_handler)
    return desktop_logger


logger = _setup_logger()


def attachment_open_file_types() -> tuple[str, ...]:
    """pywebview OPEN dialog filters that hide blacklist extensions.

    Format must be ``Description (*.ext1;*.ext2)``. Do not append
    ``All files (*.*)`` — that would re-expose ``.exe`` etc.
    """
    patterns = ";".join(f"*{ext}" for ext in ATTACHMENT_DIALOG_EXTENSIONS)
    return (f"Allowed files ({patterns})",)


def _format_ports_for_log(ports: dict[str, int]) -> str:
    return ", ".join(f"{name}={ports.get(name, 0)}" for name in PORT_TYPES)


def resolve_desktop_ports(
    host: str = "127.0.0.1",
    scan_range: int = DESKTOP_PORT_SCAN_RANGE,
) -> dict[str, int]:
    """Pick a free port group for this desktop session (no config persistence).

    Reuses ``find_available_ports`` (base + index * 1000). Result lives only in
    process memory / child env for this launch.
    """
    if scan_range < 1:
        raise RuntimeError(
            f"invalid desktop port scan_range={scan_range}; must be >= 1"
        )

    result = find_available_ports(
        base_index=0,
        host=host,
        scan_range=scan_range,
    )
    if result is None:
        logger.error(
            "[desktop] no free port group within scan_range=%s "
            "(tried indices 0..%s on %s). Free ports or raise "
            "JIUWENSWARM_*_PORT base overrides, then retry.",
            scan_range,
            scan_range - 1,
            host,
        )
        raise RuntimeError(
            f"No available desktop port group within scan_range={scan_range}"
        )

    ports, index = result
    if index == 0:
        logger.info("[desktop] using ports: %s", _format_ports_for_log(ports))
    else:
        logger.warning(
            "[desktop] default ports busy; using alternate group index=%s: %s",
            index,
            _format_ports_for_log(ports),
        )
    return ports


def _cleanup_stale_update_artifacts() -> None:
    updates_dir = get_user_workspace_dir() / ".updates"
    if not updates_dir.is_dir():
        return

    removed = 0
    for pattern in UPDATE_CLEANUP_PATTERNS:
        for path in updates_dir.glob(pattern):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                logger.warning("[desktop] failed to remove stale update artifact %s: %s", path, exc)

    if removed:
        logger.info("[desktop] cleaned %d stale update artifact(s) from %s", removed, updates_dir)


def _desktop_save_result(ok: bool, cancelled: bool = False) -> DesktopSaveResult:
    return {"ok": ok, "cancelled": cancelled}


def _creationflags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _build_child_command(name: str, extra_args: list[str] | None = None) -> list[str]:
    if getattr(sys, "frozen", False):
        if name == "web":
            flag = WEB_CHILD_FLAG
        elif name == "agent":
            flag = AGENT_CHILD_FLAG
        elif name == "gateway":
            flag = GATEWAY_CHILD_FLAG
        elif name == "app":
            flag = APP_CHILD_FLAG
        else:
            flag = UPDATE_HELPER_FLAG
        base = [sys.executable, flag]
    elif name == "agent":
        base = [sys.executable, "-m", "jiuwenswarm.server.app_agentserver"]
    elif name == "gateway":
        base = [sys.executable, "-m", "jiuwenswarm.gateway.app_gateway"]
    elif name == "app":
        base = [sys.executable, "-m", "jiuwenswarm.app"]
    elif name == "web":
        base = [sys.executable, "-m", "jiuwenswarm.channels.web.app_web"]
    else:
        base = [sys.executable, "-m", "jiuwenswarm.channels.desktop.desktop_app", UPDATE_HELPER_FLAG]
    if extra_args:
        base.extend(extra_args)
    return base


def _build_child_env(
    name: str,
    ports: dict[str, int],
    startup_diagnostics_dir: Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env[DESKTOP_ENV_FLAG] = "1"
    env["JIUWENSWARM_RUNTIME_WORKSPACE_READY"] = "1"
    # Desktop now starts Gateway directly, so preserve the original launcher
    # command here instead of relying on jiuwenswarm.app to add it. The
    # updater inherits this value from Gateway when it constructs a restart.
    if "JIUWENSWARM_START_CMD" not in env:
        try:
            env["JIUWENSWARM_START_CMD"] = json.dumps(sys.argv[:])
        except (TypeError, ValueError, OverflowError):
            env["JIUWENSWARM_START_CMD"] = json.dumps([str(arg) for arg in sys.argv[:]])
    if startup_diagnostics_dir is not None:
        env[STARTUP_DIAGNOSTICS_DIR_ENV] = str(startup_diagnostics_dir)
    # Inject the full session port group so app → agent/gateway and web agree.
    # load_dotenv_runtime preserves these under JIUWENSWARM_DESKTOP=1.
    env["WEB_HOST"] = BACKEND_HOST
    env["WEB_PORT"] = str(ports["web"])
    env["GATEWAY_PORT"] = str(ports["gateway"])
    env["AGENT_SERVER_PORT"] = str(ports["agent_server"])
    env["AGENT_PORT"] = str(ports["agent_server"])
    env["FRONTEND_PORT"] = str(ports["frontend"])
    # Gateway prefers AGENT_SERVER_URL over AGENT_SERVER_PORT; drop any stale
    # URL from the parent shell so the remapped port is used.
    env.pop("AGENT_SERVER_URL", None)
    if name == "web":
        logger.info(
            "[desktop] web child ports: frontend=%s proxy=http://%s:%s",
            ports["frontend"],
            BACKEND_HOST,
            ports["web"],
        )
    elif name in {"agent", "gateway", "app"}:
        logger.info(
            "[desktop] %s child ports: %s",
            name,
            _format_ports_for_log(ports),
        )
    return env


def _start_process(
    name: str,
    command: list[str],
    ports: dict[str, int],
    startup_diagnostics_dir: Path | None = None,
) -> subprocess.Popen[bytes]:
    logger.info("[desktop] starting %s: %s", name, command)
    kwargs: dict[str, object] = {
        "env": _build_child_env(name, ports, startup_diagnostics_dir),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    # macOS/Linux: 用 start_new_session=True 创建新进程组，
    # 以便后续用 os.killpg 杀掉整个进程树（含孙子进程）。
    if os.name != "nt":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = _creationflags()
    return subprocess.Popen(command, **kwargs)


# frozen exe 冷启动时, C 扩展 (.pyd) 与大量 .py 首次从 _MEIPASS 读盘很慢.
# 桌面主进程在拉起 agent/gateway/web 子进程前, 起后台线程预读关键包入 OS page
# cache, 子进程 import 时命中内存而非闪存/磁盘, 显著降低冷启动 import 耗时.
# 只读文件头部预读以触发 OS 顺序预取, 零执行零副作用; 非冻结模式 (dev) 无
# _MEIPASS 直接跳过.
# web 子进程 import 的大头是 jiuwenswarm 自身, 放在最前优先预热; openjiuwen
# 与 C 扩展库主要服务 app 子进程。预读块加大到 64KB 以覆盖归档多页。
_WARMUP_PACKAGES = (
    "jiuwenswarm", "openjiuwen", "faiss", "pymilvus", "google", "a2ui",
    "sqlite_vec", "tree_sitter", "tiktoken", "tiktoken_ext",
)
_WARMUP_READ_BYTES = 64 * 1024


def _warmup_page_cache_background() -> None:
    """frozen exe 冷启动后台预读关键包入 OS page cache, 不阻塞 start_services."""
    if not getattr(sys, "frozen", False):
        return  # dev 模式无 _MEIPASS, 跳过 (uv run 已有 pyc + OS cache 暖)
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass or not os.path.isdir(meipass):
        return

    def _read_all(pkg_dir):
        try:
            for root, _dirs, files in os.walk(pkg_dir):
                for f in files:
                    p = os.path.join(root, f)
                    try:
                        with open(p, "rb") as fh:
                            _ = fh.read(_WARMUP_READ_BYTES)
                    except OSError:
                        pass
        except Exception:  # noqa: BLE001
            pass

    def _worker():
        for pkg in _WARMUP_PACKAGES:
            d = os.path.join(meipass, pkg)
            if os.path.isdir(d):
                _read_all(d)

    threading.Thread(target=_worker, name="exe-page-cache-warmup", daemon=True).start()


def _wait_for_tcp(
    host: str,
    port: int,
    timeout: float,
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None

    while time.monotonic() < deadline:
        if process is not None:
            _ensure_process_running(f"service on tcp://{host}:{port}", process)
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)

    raise RuntimeError(f"Timed out waiting for tcp://{host}:{port}: {last_error}")


def _ensure_process_running(name: str, process: subprocess.Popen[bytes]) -> None:
    code = process.poll()
    if code is None:
        return
    raise RuntimeError(f"{name} exited early with code {code}")


def _wait_for_http(
    host: str,
    port: int,
    path: str,
    timeout: float,
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        if process is not None:
            _ensure_process_running(f"service on http://{host}:{port}{path}", process)
        conn = http.client.HTTPConnection(host, port, timeout=2)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            response.read()
            if response.status < 500:
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        finally:
            conn.close()
        time.sleep(0.1)

    raise RuntimeError(
        f"Timed out waiting for http://{host}:{port}{path}: {last_error}"
    )


def _wait_for_port_release(host: str, port: int, timeout: float = 15.0) -> bool:
    return wait_for_tcp_port(host, port, timeout=timeout, target_state="disconnected")


def _launch_windows_installer_helper(
    installer_path: str,
    app_executable: str,
    parent_pid: int = 0,
    backend_port: int = BACKEND_PORT,
    frontend_port: int = FRONTEND_PORT,
) -> None:
    target = Path(installer_path).expanduser().resolve()

    logger.info("[update-helper] starting, target=%s, parent_pid=%d", target, parent_pid)

    wait_pid = parent_pid if parent_pid else os.getppid()
    logger.info("[update-helper] waiting for process %d to exit", wait_pid)
    wait_for_pid_exit(wait_pid)
    logger.info(
        "[update-helper] parent process %d has exited, waiting for ports "
        "backend=%s frontend=%s to release",
        wait_pid,
        backend_port,
        frontend_port,
    )

    _wait_for_port_release(BACKEND_HOST, backend_port, timeout=15.0)
    _wait_for_port_release(FRONTEND_HOST, frontend_port, timeout=15.0)
    logger.info("[update-helper] ports released, proceeding with install")

    try:
        subprocess.Popen(
            [str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("[update-helper] installer launched successfully (interactive)")
    except Exception as exc:
        logger.error("[update-helper] installer launch failed: %s", exc)


class _WindowApi:
    def __init__(self, runtime: "DesktopRuntime") -> None:
        self._runtime = runtime

    def minimize_window(self) -> bool:
        return self._runtime.minimize_window()

    def toggle_fullscreen_window(self) -> bool:
        return self._runtime.toggle_fullscreen_window()

    def close_window(self) -> bool:
        return self._runtime.close_window()

    def get_startup_status(self) -> dict[str, str]:
        """Return a snapshot consumed by the local Loading page."""
        return self._runtime.get_startup_status()

    def install_update(self, installer_path: str) -> bool:
        return self._runtime.install_update(installer_path)

    def download_file(self, url: str, filename: str) -> DesktopSaveResult:
        """通过 webview 下载文件，解决桌面端无法使用 <a> 标签下载的问题。"""
        # 如果是相对路径，拼接完整的 URL（使用前端 web server 端口）
        if url.startswith("/"):
            full_url = f"http://{self._runtime.frontend_host}:{self._runtime.frontend_port}{url}"
        else:
            full_url = url
        logger.info("[desktop] download_file called: url=%s, filename=%s", full_url, filename)
        return self._runtime.download_file(full_url, filename)

    def save_data_url(self, data_url: str, filename: str) -> DesktopSaveResult:
        """保存前端生成的 data URL 文件，供分享图片和图表导出使用。"""
        return self._runtime.save_data_url(data_url, filename)

    def begin_blob_save(
        self, filename: str, mime_type: str, total_size: int
    ) -> dict[str, bool | str]:
        """选择保存位置并创建分块写入事务。"""
        return self._runtime.begin_blob_save(filename, mime_type, total_size)

    def append_blob_save(self, transfer_id: str, encoded_chunk: str) -> bool:
        """向桌面保存事务追加一个 base64 编码的数据块。"""
        return self._runtime.append_blob_save(transfer_id, encoded_chunk)

    def finish_blob_save(self, transfer_id: str) -> DesktopSaveResult:
        """校验并原子提交桌面保存事务。"""
        return self._runtime.finish_blob_save(transfer_id)

    def abort_blob_save(self, transfer_id: str) -> bool:
        """中止桌面保存事务并删除部分文件。"""
        return self._runtime.abort_blob_save(transfer_id)

    def select_project_directory(self) -> str | None:
        """打开系统目录选择器，返回用户选择的项目目录绝对路径。"""
        return self._runtime.select_project_directory()

    def select_local_files(
        self,
        allow_multiple: bool = True,
        initial_dir: str | None = None,
    ) -> list[dict[str, Any]]:
        """打开系统文件选择器，返回本地绝对路径及附件元数据。

        WebView2 / pywebview 的 ``<input type="file">`` 不会暴露 Electron 式
        ``File.path``，文档上传必须走原生对话框拿绝对路径。
        默认打开上次成功选择文件所在目录（无则用户主目录）。
        """
        return self._runtime.select_local_files(
            allow_multiple=bool(allow_multiple),
            initial_dir=initial_dir,
        )

    def select_local_file_path(
        self,
        initial_path: str | None = None,
        title: str | None = None,
    ) -> str | None:
        """Open a native file picker and return the selected absolute path."""
        return self._runtime.select_local_file_path(
            initial_path=initial_path,
            title=title,
        )

    def describe_local_files(self, paths: list[str] | None = None) -> list[dict[str, Any]]:
        """根据本机绝对路径返回与 select_local_files 同形的附件元数据。"""
        return self._runtime.describe_local_files(paths or [])

    def get_clipboard_files(self) -> list[dict[str, Any]]:
        """读取系统剪贴板中的文件路径并描述为附件元数据。"""
        return self._runtime.get_clipboard_files()


def _clipboard_file_paths_windows() -> list[str]:
    """Read CF_HDROP file paths from the Windows clipboard."""
    # Win32 clipboard format CF_HDROP == 15
    cf_hdrop = 15
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    shell32.DragQueryFileW.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.LPWSTR,
        wintypes.UINT,
    ]
    shell32.DragQueryFileW.restype = wintypes.UINT

    if not user32.OpenClipboard(None):
        return []
    try:
        if not user32.IsClipboardFormatAvailable(cf_hdrop):
            return []
        h_drop = user32.GetClipboardData(cf_hdrop)
        if not h_drop:
            return []
        count = shell32.DragQueryFileW(h_drop, 0xFFFFFFFF, None, 0)
        paths: list[str] = []
        for index in range(count):
            length = shell32.DragQueryFileW(h_drop, index, None, 0)
            if length <= 0:
                continue
            buffer = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(h_drop, index, buffer, length + 1)
            value = buffer.value.strip()
            if value:
                paths.append(value)
        return paths
    finally:
        user32.CloseClipboard()


def _clipboard_file_paths_macos() -> list[str]:
    """Read file paths from the macOS general pasteboard."""
    try:
        from AppKit import NSPasteboard  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return []

    try:
        pasteboard = NSPasteboard.generalPasteboard()
        items = pasteboard.propertyListForType_("NSFilenamesPboardType")
        if not items:
            return []
        return [str(item).strip() for item in items if str(item).strip()]
    except Exception:  # noqa: BLE001
        return []


def _clipboard_file_paths() -> list[str]:
    try:
        if os.name == "nt":
            return _clipboard_file_paths_windows()
        if sys.platform == "darwin":
            return _clipboard_file_paths_macos()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[desktop] clipboard file path read failed: %s", exc)
    return []


class DesktopRuntime:
    def __init__(
        self, frontend_host: str, ports: dict[str, int]
    ) -> None:
        self.frontend_host = frontend_host
        self.ports = dict(ports)
        self.frontend_port = int(ports["frontend"])
        self.backend_port = int(ports["web"])
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.window = None
        self._lock = threading.Lock()
        self._blob_save_lock = threading.Lock()
        self._blob_save_transfers: dict[str, _BlobSaveTransfer] = {}
        self._is_shutting_down = False
        self._desktop_dnd_bound = False
        self._startup_cancelled = threading.Event()
        # 先行导航(web 静态页就绪即跳转前端)后, 若后端随后启动失败, 需把
        # 失败诊断页重新载入窗口; 此 Event 标记是否已先行导航。
        self._startup_navigated = threading.Event()
        self._startup_failure_surface_presented = False
        self._startup_status_lock = threading.Lock()
        self._startup_status: dict[str, str] = {
            "state": "starting",
            "title": "",
            "message": "服务启动加载中",
            "component": "",
            "diagnostic_path": "",
            "frontend_url": self.frontend_url,
        }
        startup_id = uuid.uuid4().hex
        preferred_dir = get_logs_dir() / "startup" / startup_id
        try:
            preferred_dir.mkdir(parents=True, exist_ok=True)
            self._startup_diagnostics_dir = preferred_dir
        except OSError:
            fallback_dir = Path(tempfile.gettempdir()) / "jiuwenswarm-startup" / startup_id
            fallback_dir.mkdir(parents=True, exist_ok=True)
            self._startup_diagnostics_dir = fallback_dir
        self._doctor_output_path = self._startup_diagnostics_dir / "doctor.json"

    @property
    def frontend_url(self) -> str:
        return f"http://{self.frontend_host}:{self.frontend_port}"

    @staticmethod
    def _preflight_gateway_singleton(wait: float = 15.0) -> None:
        """Refuse to launch a second Gateway over the same workspace.

        Uses ``GatewayLock.find_holder`` (non-authoritative preflight; the
        Gateway process itself is the authoritative enforcer). Waits up to
        ``wait`` seconds for a still-shutting-down Gateway from an upgrade
        restart to release the lock, then raises if a live holder remains.
        """
        from jiuwenswarm.instance_manager.lock import GatewayLock

        workspace = get_user_workspace_dir()
        holder = GatewayLock.find_holder(workspace)
        if holder is None:
            return

        deadline = time.monotonic() + max(0.0, wait)
        while holder is not None and time.monotonic() < deadline:
            time.sleep(0.5)
            holder = GatewayLock.find_holder(workspace)

        if holder is not None:
            logger.error(
                "[desktop] another Gateway is already serving this workspace "
                "(pid=%s, workspace=%s); aborting to avoid duplicate cron scheduling",
                holder.get("pid"),
                holder.get("workspace"),
            )
            raise RuntimeError(
                f"Another Gateway instance is running (pid={holder.get('pid')}, "
                f"workspace={holder.get('workspace')}). Stop the existing one first."
            )

    def get_startup_status(self) -> dict[str, str]:
        with self._startup_status_lock:
            return dict(self._startup_status)

    def _set_startup_status(self, state: str, **updates: str) -> None:
        # 启动状态机: starting -> web_ready(静态页就绪、先行导航、非终态)
        #   -> ready(全部就绪、终态); starting/web_ready/diagnosing -> failed(终态)。
        # web_ready 非终态, 故 app 在先行导航后失败时, failed 仍可覆盖 web_ready,
        # 诊断/doctor 页可达(不会被误置的终态幂等吞掉)。
        with self._startup_status_lock:
            current = self._startup_status["state"]
            if current in {"ready", "failed"}:
                return
            self._startup_status.update(updates)
            self._startup_status["state"] = state

    def _raise_if_startup_cancelled(self) -> None:
        if self._startup_cancelled.is_set():
            raise RuntimeError("desktop startup cancelled")

    def _present_startup_failure_surface_if_navigated(self) -> None:
        # 先行导航(web_ready)后, loading 页已被前端 SPA 替换, 失去 failed/
        # diagnosing 的展示载体。已先行导航时重新载入 loading 页 HTML: 其轮询
        # get_startup_status 会读到当前 diagnosing/failed 并渲染诊断页。幂等,
        # 仅首次生效, 避免 doctor->failed 期间重复载入。
        if self._startup_failure_surface_presented:
            return
        if not self._startup_navigated.is_set():
            return
        if self.window is None or not hasattr(self.window, "load_html"):
            return
        try:
            self.window.load_html(self._build_loading_html())
        except Exception:  # noqa: BLE001
            logger.warning("[desktop] failed to present startup failure surface")
            return
        self._startup_failure_surface_presented = True

    def _start_managed_process(
        self, name: str, command: list[str]
    ) -> subprocess.Popen[bytes]:
        self._raise_if_startup_cancelled()
        process = _start_process(
            name,
            command,
            self.ports,
            startup_diagnostics_dir=self._startup_diagnostics_dir,
        )
        with self._lock:
            shutting_down = self._is_shutting_down
            if not shutting_down:
                self.processes[name] = process
        if shutting_down:
            _terminate_process_tree(process)
            raise RuntimeError("desktop startup cancelled")
        return process

    def start_services(
        self, on_web_ready: Callable[[], None] | None = None
    ) -> None:
        # Per-workspace Gateway preflight: refuse to start a second full stack
        # over the same workspace (two CronSchedulerService instances over one
        # cron_jobs.json => duplicate cron executions). Briefly wait so an
        # in-flight upgrade restart (old gateway shutting down) can release
        # the lock before we fail.
        self._preflight_gateway_singleton()
        # 先起后台预读, 与后续子进程拉起/端口等待并行, 不阻塞 start_services.
        _warmup_page_cache_background()
        # web 只需打包内的静态资源；先拉起它，使其冻结 EXE 导入与 Desktop
        # 工作区准备重叠。Gateway 未就绪前 web 自身会保持代理重试，前端则按
        # 既有逻辑重连，故无需等待后端再启动 web。
        web_command = _build_child_command(
            "web",
            [
                "--host",
                self.frontend_host,
                "--port",
                str(self.frontend_port),
                "--proxy-target",
                f"http://{BACKEND_HOST}:{self.backend_port}",
            ],
        )
        web_process = self._start_managed_process("web", web_command)
        _ensure_process_running("web", web_process)

        agent_process: subprocess.Popen[bytes] | None = None
        gateway_process: subprocess.Popen[bytes] | None = None
        try:
            # 在 Desktop 进程中只做一次工作区迁移/补齐，随后直接拉起
            # AgentServer 与 Gateway。跳过 app supervisor 可省去一个冻结 EXE。
            prepare_runtime_workspace()
            agent_process = self._start_managed_process(
                "agent", _build_child_command("agent")
            )
            _ensure_process_running("agent", agent_process)
            gateway_process = self._start_managed_process(
                "gateway", _build_child_command("gateway")
            )
            _ensure_process_running("gateway", gateway_process)
        except Exception:
            _terminate_process_tree(web_process)
            if agent_process is not None:
                _terminate_process_tree(agent_process)
            if gateway_process is not None:
                _terminate_process_tree(gateway_process)
            raise

        # The guarded startup above either raises or assigns both processes.
        # Keep this check explicit: ``assert`` statements are removed under
        # optimized Python execution, while the readiness checks below require
        # concrete process handles.
        if agent_process is None or gateway_process is None:
            raise RuntimeError("Managed agent and gateway processes failed to start")

        # 两个就绪等待并行执行；任一侧失败则立即终止两个子进程，使另一
        # 等待线程通过 process.poll() 尽快退出，不能再额外等待完整超时。
        errors: list[Exception] = []
        web_ready_ok = False
        errors_lock = threading.Lock()

        def _wait_agent_ready() -> None:
            try:
                _wait_for_tcp(
                    BACKEND_HOST,
                    self.ports["agent_server"],
                    STARTUP_TIMEOUT_SECONDS,
                    process=agent_process,
                )
            except Exception as exc:  # noqa: BLE001 收集到主线程统一处理
                with errors_lock:
                    errors.append(exc)

        def _wait_gateway_ready() -> None:
            try:
                _wait_for_tcp(
                    BACKEND_HOST,
                    self.backend_port,
                    STARTUP_TIMEOUT_SECONDS,
                    process=gateway_process,
                )
            except Exception as exc:  # noqa: BLE001 收集到主线程统一处理
                with errors_lock:
                    errors.append(exc)

        def _wait_web_ready() -> None:
            nonlocal web_ready_ok
            try:
                _wait_for_http(
                    self.frontend_host,
                    self.frontend_port,
                    "/",
                    STARTUP_TIMEOUT_SECONDS,
                    process=web_process,
                )
                # 静态服务已立即可用: 记下成功, 供主流程在 app 就绪前先行导航。
                with errors_lock:
                    web_ready_ok = True
            except Exception as exc:  # noqa: BLE001 收集到主线程统一处理
                with errors_lock:
                    errors.append(exc)

        waiters = [
            threading.Thread(target=_wait_agent_ready, name="wait-agent-ready"),
            threading.Thread(target=_wait_gateway_ready, name="wait-gateway-ready"),
            threading.Thread(target=_wait_web_ready, name="wait-web-ready"),
        ]
        for waiter in waiters:
            waiter.start()

        web_ready_notified = False
        terminated_after_error = False
        while any(waiter.is_alive() for waiter in waiters):
            for waiter in waiters:
                waiter.join(timeout=0.1)
            with errors_lock:
                has_errors = bool(errors)
                web_ok = web_ready_ok
            # web HTTP ready 即触发导航(仅一次), 不必等 app(AgentServer+Gateway)
            # 就绪; 界面骨架先行展示, API/WS 由前端重连逻辑在 gateway 就绪后补齐。
            if web_ok and on_web_ready is not None and not web_ready_notified:
                on_web_ready()
                web_ready_notified = True
            if has_errors and not terminated_after_error:
                _terminate_process_tree(agent_process)
                _terminate_process_tree(gateway_process)
                _terminate_process_tree(web_process)
                terminated_after_error = True

        with errors_lock:
            startup_errors = list(errors)

        # 等待期间窗口可能已关闭 (shutdown 置位 _startup_cancelled 并终结子进程),
        # 取消优先级高于等待错误, 与串行版的取消语义保持一致。
        self._raise_if_startup_cancelled()

        if startup_errors:
            if not terminated_after_error:
                _terminate_process_tree(agent_process)
                _terminate_process_tree(gateway_process)
                _terminate_process_tree(web_process)
            for exc in startup_errors[1:]:
                logger.error("[desktop] startup waiter also failed: %r", exc)
            raise startup_errors[0]

        # Desktop now owns AgentServer and Gateway directly (rather than via
        # jiuwenswarm.app). Preserve the supervisor's paired-lifecycle rule:
        # if either backend exits after startup, promptly stop its peer so it
        # cannot keep ports, cron jobs, or the gateway singleton lock alive.
        def _watch_backend_pair() -> None:
            while True:
                with self._lock:
                    if self._is_shutting_down:
                        return
                exited = (
                    ("agent", agent_process)
                    if agent_process.poll() is not None
                    else (("gateway", gateway_process) if gateway_process.poll() is not None else None)
                )
                if exited is not None:
                    exited_name, _ = exited
                    peer_name, peer_process = (
                        ("gateway", gateway_process)
                        if exited_name == "agent"
                        else ("agent", agent_process)
                    )
                    logger.error(
                        "[desktop] %s exited after startup; terminating %s peer",
                        exited_name,
                        peer_name,
                    )
                    if peer_process.poll() is None:
                        _terminate_process_tree(peer_process)
                    return
                time.sleep(0.25)

        threading.Thread(
            target=_watch_backend_pair,
            name="desktop-backend-pair-watch",
            daemon=True,
        ).start()
        logger.info("[desktop] services ready: %s", self.frontend_url)

    def _run_doctor_after_failure(self) -> dict[str, object] | None:
        if not getattr(sys, "frozen", False):
            return None
        command = [
            sys.executable,
            DOCTOR_FLAG,
            DOCTOR_OUTPUT_FLAG,
            str(self._doctor_output_path),
        ]
        logger.info("[desktop] running startup doctor after service failure")
        doctor_process: subprocess.Popen[bytes] | None = None
        try:
            doctor_process = self._start_managed_process(
                "doctor",
                command,
            )
            doctor_process.wait(timeout=STARTUP_DOCTOR_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            logger.warning("[desktop] startup doctor timed out")
            if doctor_process is not None and doctor_process.poll() is None:
                _terminate_process_tree(doctor_process)
                try:
                    doctor_process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    _kill_process_tree(doctor_process)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            logger.warning("[desktop] startup doctor failed to run: %s", exc)
            return None
        finally:
            with self._lock:
                if self.processes.get("doctor") is doctor_process:
                    self.processes.pop("doctor", None)
        return load_doctor_result(self._doctor_output_path)

    @staticmethod
    def _should_run_startup_doctor(
        exc: BaseException,
        child_failure: dict[str, object] | None,
    ) -> bool:
        if not getattr(sys, "frozen", False):
            return False
        if is_native_startup_failure(child_failure):
            return True
        child_error_type = str((child_failure or {}).get("error_type") or "")
        unexplained_child_exit = (
            child_failure is None or child_error_type == "SystemExit"
        )
        return unexplained_child_exit and "exited early with code" in str(exc).lower()

    @staticmethod
    def _short_error(value: object, max_chars: int = 700) -> str:
        message = str(value or "").strip()
        if len(message) <= max_chars:
            return message
        return message[: max_chars - 3] + "..."

    def _build_failed_status(
        self,
        exc: BaseException,
        doctor_result: dict[str, object] | None,
        child_failure: dict[str, object] | None = None,
    ) -> dict[str, str]:
        if child_failure is None:
            records = load_startup_failures(self._startup_diagnostics_dir)
            child_failure = select_startup_failure(records)
        blocking_check = select_blocking_doctor_check(
            doctor_result,
            child_failure,
        )

        if blocking_check is not None:
            component = str(blocking_check.get("name") or "unknown")
            display_name = str(blocking_check.get("display_name") or component)
            detail = self._short_error(blocking_check.get("message") or "加载失败")
            return {
                "title": "运行环境缺少必要组件",
                "message": f"{display_name} 无法加载：{detail}",
                "component": component,
                "diagnostic_path": str(self._doctor_output_path),
            }

        if child_failure is not None:
            role = str(child_failure.get("process_role") or "service")
            error_type = str(child_failure.get("error_type") or "Error")
            detail = self._short_error(child_failure.get("message") or exc)
            return {
                "title": f"{DISPLAY_NAME} 服务启动失败",
                "message": f"{role}: {error_type}: {detail}",
                "component": role,
                "diagnostic_path": str(
                    child_failure.get("diagnostic_path")
                    or self._startup_diagnostics_dir
                ),
            }

        return {
            "title": f"{DISPLAY_NAME} 服务启动失败",
            "message": self._short_error(exc) or "未知启动错误",
            "component": "desktop-startup",
            "diagnostic_path": str(self._startup_diagnostics_dir),
        }

    def minimize_window(self) -> bool:
        if self.window is None or not hasattr(self.window, "minimize"):
            return False
        self.window.minimize()
        return True

    def toggle_fullscreen_window(self) -> bool:
        if self.window is None:
            return False
        if hasattr(self.window, "toggle_fullscreen"):
            self.window.toggle_fullscreen()
            return True
        if hasattr(self.window, "maximize"):
            self.window.maximize()
            return True
        return False

    def close_window(self) -> bool:
        if self.window is None or not hasattr(self.window, "destroy"):
            return False

        def _delayed_destroy() -> None:
            time.sleep(0.15)
            try:
                self.window.destroy()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[desktop] failed to close desktop window: %s", exc)

        threading.Thread(target=_delayed_destroy, daemon=True).start()
        return True

    def download_file(self, url: str, filename: str) -> DesktopSaveResult:
        """选择保存位置并在实际写入完成后返回结果。"""
        try:
            target_path = self._select_save_path(filename, ())
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("[desktop] failed to select download path: %s", exc)
            return _desktop_save_result(False)

        if target_path is None:
            logger.info("[desktop] file download cancelled by user")
            return _desktop_save_result(False, cancelled=True)

        temp_path: Path | None = None
        try:
            import urllib.request

            temp_fd, temp_name = tempfile.mkstemp(
                dir=target_path.parent,
                prefix=f".{target_path.name}.",
                suffix=".part",
            )
            os.close(temp_fd)
            temp_path = Path(temp_name)
            urllib.request.urlretrieve(url, temp_path)
            os.replace(temp_path, target_path)
            temp_path = None
            logger.info("[desktop] file downloaded to: %s", target_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("[desktop] download failed: %s", exc)
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    logger.warning(
                        "[desktop] failed to remove partial download %s: %s",
                        temp_path,
                        cleanup_exc,
                    )
            return _desktop_save_result(False)

        self._show_download_complete(str(target_path))
        return _desktop_save_result(True)

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        safe_name = Path(filename).name
        if not safe_name:
            raise ValueError("empty_filename")
        return safe_name

    def _select_save_path(self, filename: str, file_types: tuple[str, ...]) -> Path | None:
        if self.window is None or not hasattr(self.window, "create_file_dialog"):
            raise RuntimeError("desktop_window_unavailable")

        download_dir = Path.home() / "Downloads"
        download_dir.mkdir(parents=True, exist_ok=True)
        selected_paths = self.window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(download_dir),
            save_filename=self._sanitize_filename(filename),
            file_types=file_types,
        )
        if not selected_paths:
            return None
        if isinstance(selected_paths, str):
            return Path(selected_paths)
        return Path(selected_paths[0])

    def select_project_directory(self) -> str | None:
        if self.window is None or not hasattr(self.window, "create_file_dialog"):
            logger.error("[desktop] project directory picker unavailable")
            return None

        try:
            selected_paths = self.window.create_file_dialog(
                webview.FileDialog.FOLDER,
                directory=str(Path.home()),
                allow_multiple=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[desktop] project directory picker failed: %s", exc)
            return None

        if not selected_paths:
            return None
        selected_path = selected_paths if isinstance(selected_paths, str) else selected_paths[0]
        try:
            return str(Path(selected_path).expanduser().resolve())
        except Exception:  # noqa: BLE001
            return str(Path(selected_path).expanduser())

    @staticmethod
    def _resolve_blob_export(
        filename: str, mime_type: str, total_size: int
    ) -> tuple[str, str, _DataUrlExportSpec]:
        safe_name = DesktopRuntime._sanitize_filename(filename)
        if isinstance(total_size, bool) or not isinstance(total_size, int):
            raise ValueError("invalid_blob_size")
        if total_size < 0 or total_size > MAX_JAVASCRIPT_SAFE_INTEGER:
            raise ValueError("invalid_blob_size")
        if not isinstance(mime_type, str):
            raise ValueError("invalid_blob_mime_type")

        metadata = [part.strip().lower() for part in mime_type.split(";")]
        normalized_mime_type = metadata[0]
        export_spec = DATA_URL_EXPORT_SPECS.get(normalized_mime_type)
        if export_spec is None:
            raise ValueError("unsupported_blob_mime_type")

        parameters = metadata[1:]
        if len(parameters) != len(set(parameters)) or any(
            parameter not in export_spec.allowed_parameters for parameter in parameters
        ):
            raise ValueError("unsupported_blob_mime_parameters")
        if Path(safe_name).suffix.lower() not in export_spec.allowed_suffixes:
            raise ValueError("blob_filename_extension_mismatch")
        return safe_name, normalized_mime_type, export_spec

    @staticmethod
    def _discard_blob_save_transfer(transfer: _BlobSaveTransfer) -> None:
        try:
            transfer.export_file.close()
        except OSError as exc:
            logger.warning("[desktop] failed to close partial blob export: %s", exc)
        try:
            transfer.temp_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "[desktop] failed to remove partial blob export %s: %s",
                transfer.temp_path,
                exc,
            )

    def begin_blob_save(
        self, filename: str, mime_type: str, total_size: int
    ) -> dict[str, bool | str]:
        """选择目标路径并创建有界内存的分块保存事务。"""
        try:
            safe_name, normalized_mime_type, export_spec = self._resolve_blob_export(
                filename, mime_type, total_size
            )
            target_path = self._select_save_path(safe_name, export_spec.file_types)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("[desktop] failed to begin blob export: %s", exc)
            return {"ok": False, "cancelled": False}

        if target_path is None:
            logger.info("[desktop] blob export cancelled by user")
            return {"ok": False, "cancelled": True}

        temp_fd: int | None = None
        temp_path: Path | None = None
        export_file: BinaryIO | None = None
        try:
            temp_fd, temp_name = tempfile.mkstemp(
                dir=target_path.parent,
                prefix=f".{target_path.name}.",
                suffix=".part",
            )
            temp_path = Path(temp_name)
            export_file = os.fdopen(temp_fd, "wb")
            temp_fd = None
            transfer = _BlobSaveTransfer(
                expected_size=total_size,
                export_file=export_file,
                mime_type=normalized_mime_type,
                target_path=target_path,
                temp_path=temp_path,
            )
            with self._blob_save_lock:
                transfer_id = uuid.uuid4().hex
                while transfer_id in self._blob_save_transfers:
                    transfer_id = uuid.uuid4().hex
                self._blob_save_transfers[transfer_id] = transfer
            return {"ok": True, "cancelled": False, "transfer_id": transfer_id}
        except OSError as exc:
            logger.error("[desktop] failed to create blob export transaction: %s", exc)
            if export_file is not None:
                try:
                    export_file.close()
                except OSError:
                    pass
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return {"ok": False, "cancelled": False}

    def append_blob_save(self, transfer_id: str, encoded_chunk: str) -> bool:
        """解码并追加一个不超过协议上限的数据块。"""
        if not isinstance(transfer_id, str):
            return False
        max_encoded_size = ((DESKTOP_BLOB_CHUNK_SIZE + 2) // 3) * 4
        transfer_to_discard: _BlobSaveTransfer | None = None

        with self._blob_save_lock:
            transfer = self._blob_save_transfers.get(transfer_id)
            if transfer is None:
                return False
            try:
                if (
                    not isinstance(encoded_chunk, str)
                    or not encoded_chunk
                    or len(encoded_chunk) > max_encoded_size
                ):
                    raise ValueError("invalid_blob_chunk")
                chunk = base64.b64decode(encoded_chunk, validate=True)
                if not chunk or len(chunk) > DESKTOP_BLOB_CHUNK_SIZE:
                    raise ValueError("invalid_blob_chunk_size")
                if transfer.bytes_written + len(chunk) > transfer.expected_size:
                    raise ValueError("blob_size_exceeded")

                written = transfer.export_file.write(chunk)
                if written != len(chunk):
                    raise OSError("incomplete_blob_chunk_write")
                transfer.bytes_written += written
                if len(transfer.signature) < len(PNG_SIGNATURE):
                    remaining = len(PNG_SIGNATURE) - len(transfer.signature)
                    transfer.signature += chunk[:remaining]
            except (OSError, ValueError) as exc:
                logger.error("[desktop] failed to append blob export: %s", exc)
                transfer_to_discard = self._blob_save_transfers.pop(transfer_id)

        if transfer_to_discard is not None:
            self._discard_blob_save_transfer(transfer_to_discard)
            return False
        return True

    def finish_blob_save(self, transfer_id: str) -> DesktopSaveResult:
        """校验字节数和文件签名，然后原子提交分块保存事务。"""
        if not isinstance(transfer_id, str):
            return _desktop_save_result(False)
        with self._blob_save_lock:
            transfer = self._blob_save_transfers.pop(transfer_id, None)
        if transfer is None:
            return _desktop_save_result(False)

        committed = False
        try:
            if transfer.bytes_written != transfer.expected_size:
                raise ValueError("blob_size_mismatch")
            if (
                transfer.mime_type == "image/png"
                and transfer.signature != PNG_SIGNATURE
            ):
                raise ValueError("invalid_png_signature")

            transfer.export_file.flush()
            os.fsync(transfer.export_file.fileno())
            transfer.export_file.close()
            os.replace(transfer.temp_path, transfer.target_path)
            committed = True
            logger.info("[desktop] blob export saved to: %s", transfer.target_path)
            return _desktop_save_result(True)
        except (OSError, ValueError) as exc:
            logger.error("[desktop] failed to finish blob export: %s", exc)
            return _desktop_save_result(False)
        finally:
            if not committed:
                self._discard_blob_save_transfer(transfer)

    def abort_blob_save(self, transfer_id: str) -> bool:
        """中止事务并删除尚未提交的部分文件。"""
        if not isinstance(transfer_id, str):
            return False
        with self._blob_save_lock:
            transfer = self._blob_save_transfers.pop(transfer_id, None)
        if transfer is None:
            return False
        self._discard_blob_save_transfer(transfer)
        return True

    def _abort_all_blob_saves(self) -> None:
        with self._blob_save_lock:
            transfers = list(self._blob_save_transfers.values())
            self._blob_save_transfers.clear()
        for transfer in transfers:
            self._discard_blob_save_transfer(transfer)

    def select_local_files(
        self,
        allow_multiple: bool = True,
        initial_dir: str | None = None,
    ) -> list[dict[str, Any]]:
        if self.window is None or not hasattr(self.window, "create_file_dialog"):
            logger.error("[desktop] local file picker unavailable")
            return []

        from jiuwenswarm.channels.web.file_picker import (
            remember_file_picker_dir,
            resolve_file_picker_initial_dir,
        )

        start_dir = resolve_file_picker_initial_dir(initial_dir)
        try:
            selected_paths = self.window.create_file_dialog(
                webview.FileDialog.OPEN,
                directory=start_dir,
                allow_multiple=bool(allow_multiple),
                file_types=attachment_open_file_types(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[desktop] local file picker failed: %s", exc)
            return []

        if not selected_paths:
            return []

        if isinstance(selected_paths, (str, Path)):
            path_list = [selected_paths]
        else:
            path_list = list(selected_paths)

        results: list[dict[str, Any]] = []
        for raw in path_list:
            item = self._describe_local_file(raw)
            if item is not None:
                results.append(item)
        if results:
            remember_file_picker_dir(results[0].get("path") or path_list[0])
        return results

    def select_local_file_path(
        self,
        initial_path: str | None = None,
        title: str | None = None,
    ) -> str | None:
        """Open a native file picker and return the selected absolute path."""
        if self.window is None or not hasattr(self.window, "create_file_dialog"):
            logger.error("[desktop] local file path picker unavailable")
            return None

        initial_dir = str(Path.home())
        if isinstance(initial_path, str) and initial_path.strip():
            candidate = Path(initial_path.strip()).expanduser()
            parent = candidate.parent if candidate.name else candidate
            if parent.is_dir():
                initial_dir = str(parent)

        file_types = ("Executable files (*.exe)", "All files (*.*)") if sys.platform == "win32" else ()
        try:
            selected_paths = self.window.create_file_dialog(
                webview.FileDialog.OPEN,
                directory=initial_dir,
                allow_multiple=False,
                file_types=file_types,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[desktop] local file path picker failed: %s", exc)
            return None

        if not selected_paths:
            return None
        selected_path = selected_paths if isinstance(selected_paths, (str, Path)) else selected_paths[0]
        try:
            return str(Path(selected_path).expanduser().resolve())
        except Exception:  # noqa: BLE001
            return str(Path(selected_path).expanduser())

    @staticmethod
    def _describe_local_file(raw_path: str | Path) -> dict[str, Any] | None:
        try:
            path = Path(raw_path).expanduser().resolve()
        except Exception:  # noqa: BLE001
            path = Path(raw_path).expanduser()

        if not path.is_file():
            logger.warning("[desktop] selected path is not a file: %s", path)
            return None

        filename = path.name
        ext = path.suffix.lower()
        try:
            size = path.stat().st_size
        except OSError as exc:
            logger.warning("[desktop] failed to stat selected file %s: %s", path, exc)
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
                logger.warning("[desktop] failed to read image %s: %s", path, exc)
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

    def describe_local_files(self, paths: list[str] | Any) -> list[dict[str, Any]]:
        if isinstance(paths, (str, Path)):
            path_list = [paths]
        elif paths:
            path_list = list(paths)
        else:
            return []

        results: list[dict[str, Any]] = []
        for raw in path_list:
            if raw is None:
                continue
            item = self._describe_local_file(str(raw))
            if item is not None:
                results.append(item)
        return results

    def get_clipboard_files(self) -> list[dict[str, Any]]:
        return self.describe_local_files(_clipboard_file_paths())

    def _evaluate_js(self, script: str) -> None:
        if self.window is None:
            return
        try:
            self.window.evaluate_js(script)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[desktop] evaluate_js failed: %s", exc)

    def _run_js(self, script: str) -> Any:
        """Run JS without pywebview's eval()/escape_string wrapping."""
        if self.window is None:
            return None
        try:
            run_js = getattr(self.window, "run_js", None)
            if callable(run_js):
                return run_js(script)
            return self.window.evaluate_js(script)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[desktop] run_js failed: %s", exc)
            return None

    def _mark_desktop_shell(self) -> None:
        """Mark desktop shell and force the page to accept OS file drags.

        React may set ``dropEffect='none'`` during bubble; a window-level bubble
        listener runs afterwards and restores ``copy`` so the forbidden cursor
        does not appear inside the desktop webview. OS file drags into WebView2
        require ``copy`` — ``move``/``none`` are rejected and show the forbidden
        cursor.
        """
        self._run_js(
            """
(function () {
  window.__JIUWEN_DESKTOP__ = true;
  window.__JIUWEN_DROP_QUEUE__ = window.__JIUWEN_DROP_QUEUE__ || [];
  // Durable stub: never leave Python without a callable ingest hook.
  if (typeof window.__JIUWEN_INGEST_LOCAL_FILES__ !== 'function') {
    window.__JIUWEN_INGEST_LOCAL_FILES__ = function (detail) {
      try { window.__JIUWEN_DROP_QUEUE__.push(detail); } catch (err) {}
      window.dispatchEvent(new CustomEvent('jiuwen-desktop-local-files', { detail: detail }));
    };
  }
  window.dispatchEvent(new CustomEvent('jiuwen-desktop-ready'));
  if (window.__JIUWEN_DESKTOP_DND__) return;
  window.__JIUWEN_DESKTOP_DND__ = true;
  function hasFiles(dt) {
    if (!dt || !dt.types) return false;
    try {
      return Array.from(dt.types).indexOf('Files') !== -1;
    } catch (err) {
      return false;
    }
  }
  function accept(e) {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    try { e.dataTransfer.dropEffect = 'copy'; } catch (err) {}
    window.dispatchEvent(new CustomEvent('jiuwen-desktop-file-drag', {detail:{active:true}}));
  }
  function endDrag() {
    window.dispatchEvent(new CustomEvent('jiuwen-desktop-file-drag', {detail:{active:false}}));
  }
  // Capture: ensure preventDefault early. Bubble on window: win over React dropEffect=none.
  window.addEventListener('dragenter', accept, true);
  window.addEventListener('dragover', accept, true);
  window.addEventListener('dragenter', accept, false);
  window.addEventListener('dragover', accept, false);
  window.addEventListener('drop', function (e) {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    endDrag();
  }, true);
})();
"""
        )

    def _dispatch_local_files_event(
        self,
        source: str,
        files: list[dict[str, Any]],
        *,
        client_x: float | int | None = None,
        client_y: float | int | None = None,
    ) -> None:
        if self.window is None or not files:
            return
        payload: dict[str, Any] = {
            "source": source,
            "files": files,
            "trusted": True,
            "dropId": f"{time.time_ns()}",
        }
        if isinstance(client_x, (int, float)):
            payload["clientX"] = client_x
        if isinstance(client_y, (int, float)):
            payload["clientY"] = client_y
        try:
            detail = json.dumps(payload, ensure_ascii=False)
            # Single JS round-trip per drop: end the drag overlay, then hand the
            # files to the durable ingest bridge (frontend or the injected stub).
            # Do not issue multiple concurrent evaluate_js calls here — racing JS
            # calls from pywebview's DOMEventHandler thread can deadlock the
            # WebView2 UI thread.
            script = f"""
(function () {{
  var detail = {detail};
  window.dispatchEvent(new CustomEvent('jiuwen-desktop-file-drag', {{ detail: {{ active: false }} }}));
  if (typeof window.__JIUWEN_INGEST_LOCAL_FILES__ === 'function') {{
    window.__JIUWEN_INGEST_LOCAL_FILES__(detail);
  }} else {{
    window.dispatchEvent(new CustomEvent('jiuwen-desktop-local-files', {{ detail: detail }}));
  }}
}})();
"""
            self._run_js(script)
            logger.info(
                "[desktop] dispatched %d local file(s) from %s", len(files), source
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[desktop] failed to dispatch local files event: %s", exc)

    @staticmethod
    def _on_desktop_drag(_event: Any) -> None:
        # Overlay / cursor feedback is driven by the injected JS accept handlers.
        return None

    def _on_desktop_drop(self, event: Any) -> None:
        # No standalone run_js here: the drag-end overlay event is folded into the
        # dispatch script, and the page-side drop listener already ends the
        # overlay. Extra concurrent JS calls at drop time can deadlock the UI thread.
        try:
            payload = event or {}
            data_transfer = payload.get("dataTransfer") or {}
            raw_files = data_transfer.get("files") or []
        except Exception:  # noqa: BLE001
            return
        if not raw_files:
            logger.info("[desktop] drop event without files")
            return

        paths: list[str] = []
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            path = item.get("pywebviewFullPath")
            if isinstance(path, str) and path.strip():
                paths.append(path.strip())
        if not paths:
            logger.warning("[desktop] drop files missing pywebviewFullPath: %s", raw_files)
            return
        described = self.describe_local_files(paths)
        if described:
            logger.info("[desktop] dispatching %d dropped file(s)", len(described))
            self._dispatch_local_files_event(
                "drop",
                described,
                client_x=payload.get("clientX"),
                client_y=payload.get("clientY"),
            )

    def _bind_desktop_file_dnd(self) -> None:
        if self.window is None or self._desktop_dnd_bound:
            return
        try:
            from webview.dom import DOMEventHandler

            document = self.window.dom.document
            # preventDefault so WebView2 accepts the drop and exposes full paths.
            # Do not stopPropagation on dragenter/dragover — React needs those for
            # the chat drop overlay; the injected window listeners fix the cursor.
            document.events.dragenter += DOMEventHandler(self._on_desktop_drag, True, False)
            document.events.dragover += DOMEventHandler(
                self._on_desktop_drag, True, False, debounce=500
            )
            document.events.drop += DOMEventHandler(self._on_desktop_drop, True, True)
            self._desktop_dnd_bound = True
            logger.info("[desktop] file drag-and-drop handlers bound")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[desktop] failed to bind file drag-and-drop handlers: %s", exc)

    def _schedule_desktop_file_dnd_bind(self) -> None:
        self._mark_desktop_shell()
        self._desktop_dnd_bound = False
        self._bind_desktop_file_dnd()
        if self._desktop_dnd_bound:
            return

        def _retry() -> None:
            try:
                self._mark_desktop_shell()
                self._bind_desktop_file_dnd()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[desktop] retry bind file drag-and-drop failed: %s", exc)

        threading.Timer(1.0, _retry).start()
        threading.Timer(3.0, _retry).start()

    def save_data_url(self, data_url: str, filename: str) -> DesktopSaveResult:
        """选择保存位置并保存受支持的 base64 data URL。"""
        try:
            safe_name = self._sanitize_filename(filename)
        except ValueError as exc:
            logger.error("[desktop] invalid export filename: %s", exc)
            return _desktop_save_result(False)

        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            logger.error("[desktop] invalid data url for export")
            return _desktop_save_result(False)

        header, separator, encoded_data = data_url.partition(",")
        metadata = header[5:].split(";")
        if not separator or len(metadata) < 2 or metadata[-1].lower() != "base64":
            logger.error("[desktop] export data url must use base64 encoding")
            return _desktop_save_result(False)

        mime_type = metadata[0].lower()
        export_spec = DATA_URL_EXPORT_SPECS.get(mime_type)
        if export_spec is None:
            logger.error("[desktop] unsupported export data url type: %s", mime_type)
            return _desktop_save_result(False)

        parameters = [parameter.lower() for parameter in metadata[1:-1]]
        if len(parameters) != len(set(parameters)) or any(
            parameter not in export_spec.allowed_parameters for parameter in parameters
        ):
            logger.error(
                "[desktop] unsupported export data url parameters for %s: %s",
                mime_type,
                parameters,
            )
            return _desktop_save_result(False)

        if Path(safe_name).suffix.lower() not in export_spec.allowed_suffixes:
            logger.error(
                "[desktop] export filename extension does not match %s: %s",
                mime_type,
                safe_name,
            )
            return _desktop_save_result(False)

        try:
            file_bytes = base64.b64decode(encoded_data, validate=True)
        except ValueError as exc:
            logger.error("[desktop] failed to decode export data url: %s", exc)
            return _desktop_save_result(False)

        if mime_type == "image/png" and not file_bytes.startswith(PNG_SIGNATURE):
            logger.error("[desktop] export data is not a PNG")
            return _desktop_save_result(False)

        temp_fd: int | None = None
        temp_path: Path | None = None
        try:
            selected_path = self._select_save_path(safe_name, export_spec.file_types)
            if selected_path is None:
                logger.info("[desktop] data url export cancelled by user")
                return _desktop_save_result(False, cancelled=True)

            temp_fd, temp_name = tempfile.mkstemp(
                dir=selected_path.parent,
                prefix=f".{selected_path.name}.",
                suffix=".part",
            )
            temp_path = Path(temp_name)
            export_file = os.fdopen(temp_fd, "wb")
            temp_fd = None
            with export_file:
                export_file.write(file_bytes)
            os.replace(temp_path, selected_path)
            temp_path = None
            logger.info("[desktop] data url export saved to: %s", selected_path)
            return _desktop_save_result(True)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("[desktop] failed to save data url export: %s", exc)
            return _desktop_save_result(False)
        finally:
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError as cleanup_exc:
                    logger.warning(
                        "[desktop] failed to close partial export: %s",
                        cleanup_exc,
                    )
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    logger.warning(
                        "[desktop] failed to remove partial export %s: %s",
                        temp_path,
                        cleanup_exc,
                    )

    @staticmethod
    def _show_download_complete(file_path: str) -> None:
        """下载完成后提醒用户并打开文件所在文件夹。"""
        try:
            if os.name == "nt":
                # Windows: 弹窗询问是否打开文件夹
                result = ctypes.windll.user32.MessageBoxW(
                    0,
                    f"文件已下载到:\n{file_path}\n\n是否打开所在文件夹？",
                    "下载完成",
                    0x44  # MB_YESNO + MB_ICONINFORMATION
                )
                if result == 6:  # IDYES
                    # 打开文件夹并选中文件
                    explorer_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe")
                    subprocess.Popen(
                        [explorer_path, "/select,", file_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=_creationflags(),
                    )
            elif sys.platform == "darwin":
                # macOS: 弹窗询问
                result = subprocess.run(
                    ["/usr/bin/osascript", "-e", f'''
                    display alert "下载完成" message "文件已下载到:\\n{file_path}\\n\\n是否打开所在文件夹？" buttons {"取消", "打开文件夹"} default button "打开文件夹" as informational
                    '''],
                    capture_output=True,
                    text=True,
                )
                if "打开文件夹" in result.stdout:
                    # 打开文件夹并选中文件
                    subprocess.Popen(
                        ["/usr/bin/open", "-R", file_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("[desktop] failed to show download complete: %s", exc)

    def install_update(self, installer_path: str) -> bool:
        target = Path(installer_path).expanduser().resolve()
        if not target.is_file():
            logger.error("[desktop] installer not found: %s", target)
            return False

        app_executable = Path(sys.executable).resolve()

        if os.name == "nt":
            ok = self._launch_windows_install_helper(target, app_executable)
        elif sys.platform == "darwin":
            ok = self._launch_macos_install_helper(target, app_executable)
        else:
            ok = self._launch_linux_install_helper(target, app_executable)

        if not ok:
            logger.error("[desktop] failed to launch update helper for %s", sys.platform)
            return False

        logger.info("[desktop] launched update helper for %s, parent pid=%d", sys.platform, os.getpid())
        self.close_window()
        return True

    def _launch_macos_install_helper(self, target: Path, app_executable: Path) -> bool:
        parent_pid = os.getpid()
        updates_dir = get_user_workspace_dir() / ".updates"
        updates_dir.mkdir(parents=True, exist_ok=True)

        if not os.access(updates_dir, os.W_OK):
            logger.error("[desktop] no write permission for updates directory: %s", updates_dir)
            return False

        # Derive the .app bundle path from the frozen executable.
        # sys.executable is typically:
        #   /Applications/<configured app bundle>/Contents/MacOS/<configured executable>
        # so the bundle is three levels up. Prefer replacing the exact bundle
        # the user launched, but fall back to /Applications when running from a
        # read-only DMG mount or from a non-bundled development executable.
        app_bundle = app_executable.parent.parent.parent
        if app_bundle.suffix == ".app" and not str(app_bundle).startswith("/Volumes/"):
            install_target = str(app_bundle)
        elif app_bundle.suffix == ".app":
            install_target = f"/Applications/{app_bundle.name}"
        else:
            install_target = f"/Applications/{APP_BUNDLE_NAME}"

        log_file = get_logs_dir() / "update_helper.log"
        backend_port = self.backend_port
        frontend_port = self.frontend_port

        # shlex.quote all external paths to prevent shell injection if the
        # release API serves a malicious asset name.
        q_target = shlex.quote(str(target))
        q_install_target = shlex.quote(install_target)
        q_log_file = shlex.quote(str(log_file))
        q_temp_target = shlex.quote(f"{install_target}.new")
        q_old_target = shlex.quote(f"{install_target}.old")

        helper_content = f"""#!/bin/bash
set -e

LOG_FILE={q_log_file}
exec >>"$LOG_FILE" 2>&1

echo "=== {DISPLAY_NAME} macOS install helper: $(date) ==="
echo "[helper] dmg={q_target}"
echo "[helper] install_target={q_install_target}"
echo "[helper] parent_pid={parent_pid}"

# Wait for parent process to exit
echo "[helper] waiting for parent pid {parent_pid} to exit"
while kill -0 "{parent_pid}" 2>/dev/null; do
    sleep 1
done
echo "[helper] parent process exited"

# Wait for backend/frontend ports to release
wait_port_release() {{
    local port=$1
    local name=$2
    local deadline=$(( SECONDS + 15 ))
    while [ $SECONDS -lt $deadline ]; do
        if ! lsof -iTCP:"$port" -sTCP:LISTEN -P -n >/dev/null 2>&1; then
            echo "[helper] port $port ($name) released"
            return 0
        fi
        sleep 0.5
    done
    echo "[helper] warning: port $port ($name) still in use after 15s, proceeding anyway"
}}
wait_port_release {backend_port} backend
wait_port_release {frontend_port} frontend

# Mount the DMG at a controlled mount point
MOUNT_POINT="/tmp/jiuwenswarm_dmg_{parent_pid}"
rm -rf "$MOUNT_POINT" 2>/dev/null || true
mkdir -p "$MOUNT_POINT"
echo "[helper] attaching DMG at $MOUNT_POINT"
if ! hdiutil attach {q_target} -mountpoint "$MOUNT_POINT" -nobrowse -noautoopen -quiet; then
    echo "[helper] ERROR: hdiutil attach failed"
    rm -rf "$MOUNT_POINT" 2>/dev/null || true
    exit 1
fi

# Find the .app bundle inside the mounted DMG
APP_BUNDLE=$(find "$MOUNT_POINT" -maxdepth 1 -name "*.app" -print -quit)
if [ -z "$APP_BUNDLE" ]; then
    echo "[helper] ERROR: no .app bundle found in DMG"
    hdiutil detach "$MOUNT_POINT" -quiet || true
    rm -rf "$MOUNT_POINT" 2>/dev/null || true
    exit 1
fi
echo "[helper] found app bundle: $APP_BUNDLE"

# Copy to a temp target first. During the final swap, keep the previous bundle
# as OLD_TARGET so a failed install can be rolled back.
TEMP_TARGET={q_temp_target}
OLD_TARGET={q_old_target}
rm -rf "$TEMP_TARGET" 2>/dev/null || true
rm -rf "$OLD_TARGET" 2>/dev/null || true

echo "[helper] copying app to $TEMP_TARGET"
if ! ditto "$APP_BUNDLE" "$TEMP_TARGET"; then
    echo "[helper] ERROR: ditto copy failed"
    rm -rf "$TEMP_TARGET" 2>/dev/null || true
    hdiutil detach "$MOUNT_POINT" -quiet || true
    rm -rf "$MOUNT_POINT" 2>/dev/null || true
    exit 1
fi

restore_old_target() {{
    status=$?
    if [ $status -ne 0 ] && [ -d "$OLD_TARGET" ]; then
        echo "[helper] install failed, restoring previous app bundle"
        rm -rf {q_install_target} 2>/dev/null || true
        mv "$OLD_TARGET" {q_install_target} || true
    fi
    rm -rf "$TEMP_TARGET" 2>/dev/null || true
    hdiutil detach "$MOUNT_POINT" -quiet || true
    rm -rf "$MOUNT_POINT" 2>/dev/null || true
    exit $status
}}
trap restore_old_target EXIT

# Install: move the old app aside, move the verified new bundle into place,
# and let the EXIT trap restore OLD_TARGET if a step fails.
if [ -d {q_install_target} ]; then
    echo "[helper] backing up existing app bundle to $OLD_TARGET"
    mv {q_install_target} "$OLD_TARGET"
fi
mv "$TEMP_TARGET" {q_install_target}

trap - EXIT
rm -rf "$OLD_TARGET" 2>/dev/null || true
echo "[helper] install complete: {q_install_target}"

# Detach DMG and clean up mount point
hdiutil detach "$MOUNT_POINT" -quiet || true
rm -rf "$MOUNT_POINT" 2>/dev/null || true

# Remove quarantine attribute from downloaded DMG contents
xattr -dr com.apple.quarantine {q_install_target} 2>/dev/null || true

# Launch the new app
echo "[helper] launching {q_install_target}"
open {q_install_target} || echo "[helper] WARNING: failed to launch app"
echo "=== install helper finished: $(date) ==="
"""
        helper_path = updates_dir / "_install_helper.sh"
        helper_path.write_text(helper_content, encoding="utf-8")
        helper_path.chmod(0o755)

        subprocess.Popen(
            ["/bin/bash", str(helper_path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(
            "[desktop] macOS install helper launched, target=%s, install_target=%s",
            target, install_target,
        )
        return True

    @staticmethod
    def _launch_linux_install_helper(target: Path, app_executable: Path) -> bool:
        parent_pid = os.getpid()
        updates_dir = get_user_workspace_dir() / ".updates"
        updates_dir.mkdir(parents=True, exist_ok=True)

        if not os.access(updates_dir, os.W_OK):
            logger.error("[desktop] no write permission for updates directory: %s", updates_dir)
            return False

        install_dir = str(app_executable.parent.resolve())
        backup_dir = f"{install_dir}.bak.$RANDOM"

        # shlex.quote all external paths to prevent shell injection if the
        # release API serves a malicious asset name.
        q_target = shlex.quote(str(target))
        q_install_dir = shlex.quote(install_dir)
        q_backup_dir = shlex.quote(backup_dir)
        q_executable = shlex.quote(f"{install_dir}/{EXECUTABLE_NAME}")

        helper_content = f"""#!/bin/bash
set -e
PARENT_PID={parent_pid}
while kill -0 "$PARENT_PID" 2>/dev/null; do
    sleep 1
done

BACKUP={q_backup_dir}
if [ -d {q_install_dir} ]; then
    mv {q_install_dir} "$BACKUP"
fi
mkdir -p {q_install_dir}
tar xzf {q_target} -C {q_install_dir}
rm -rf "$BACKUP" 2>/dev/null || true
nohup {q_executable} >/dev/null 2>&1 &
"""
        helper_path = updates_dir / "_install_helper.sh"
        helper_path.write_text(helper_content, encoding="utf-8")
        helper_path.chmod(0o755)

        subprocess.Popen(
            ["/bin/bash", str(helper_path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("[desktop] Linux install helper launched, target=%s", target)
        return True

    def _launch_windows_install_helper(self, target: Path, app_executable: Path) -> bool:
        detached_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | _creationflags()
        )
        helper_cmd = _build_child_command(
            "update-helper",
            [
                "--installer-path",
                str(target),
                "--app-executable",
                str(app_executable),
                "--parent-pid",
                str(os.getpid()),
                "--backend-port",
                str(self.backend_port),
                "--frontend-port",
                str(self.frontend_port),
            ],
        )
        logger.info("[desktop] launching update helper: %s", helper_cmd)
        subprocess.Popen(
            helper_cmd,
            creationflags=detached_flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True

    def shutdown(self) -> None:
        with self._lock:
            if self._is_shutting_down:
                return
            self._is_shutting_down = True
            self._startup_cancelled.set()
            processes = list(self.processes.values())

        self._abort_all_blob_saves()
        deadline = time.monotonic() + 8.0
        logger.info("[desktop] shutting down child processes")

        for process in processes:
            if process.poll() is None:
                _terminate_process_tree(process)

        while time.monotonic() < deadline:
            if all(process.poll() is not None for process in processes):
                break
            time.sleep(0.2)

        for process in processes:
            if process.poll() is None:
                _kill_process_tree(process)

        with self._lock:
            self.processes.clear()

    @staticmethod
    def _clear_wkwebview_system_cache() -> None:
        """Clear WKWebView HTTP cache directory.

        On macOS, WKWebView caches HTTP responses (JS/CSS etc.) in
        ~/Library/Caches/<bundle_id>/, independent of pywebview's storage_path.
        These cached frontend assets can persist across different DMG versions,
        causing stale UI. Only Caches is cleared to preserve localStorage/IndexedDB
        stored in ~/Library/WebKit/<bundle_id>/.
        """
        if sys.platform != "darwin":
            return
        cache_dir = Path.home() / "Library" / "Caches" / BUNDLE_IDENTIFIER
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
            logger.info("[desktop] cleared WKWebView HTTP cache: %s", cache_dir)

    def run(self, window_title: str, width: int, height: int, debug: bool) -> None:
        self._clear_wkwebview_system_cache()

        storage_path = get_user_workspace_dir() / "tmp" / "webview"
        if storage_path.exists():
            shutil.rmtree(storage_path)
        storage_path.mkdir(parents=True, exist_ok=True)

        self.window = webview.create_window(
            window_title,
            html=self._build_loading_html(),
            js_api=_WindowApi(self),
            width=width,
            height=height,
            min_size=(1100, 720),
            frameless=False,
            easy_drag=False,
            draggable=True,
            text_select=True,
            background_color="#0f172a",
        )

        self.window.events.loaded += self._on_loaded_first
        self.window.events.closed += self._on_closed

        def _start_services_and_report() -> None:
            def _navigate_on_web_ready() -> None:
                # web 静态页就绪: 置 web_ready(非终态、可先行导航) 而非 ready。
                # app(AgentServer+Gateway) 仍可能在随后失败, 届时 failed 可覆盖
                # web_ready 使诊断/doctor 页可达; 全部就绪后由外层置终态 ready。
                self._startup_navigated.set()
                self._set_startup_status(
                    "web_ready",
                    message="服务已就绪",
                    frontend_url=self.frontend_url,
                )

            try:
                self.start_services(on_web_ready=_navigate_on_web_ready)
                self._set_startup_status(
                    "ready",
                    message="服务已就绪",
                    frontend_url=self.frontend_url,
                )
                try:
                    # Healthy starts produce no records; avoid accumulating one
                    # empty directory per launch. Never delete a non-empty session.
                    self._startup_diagnostics_dir.rmdir()
                except OSError:
                    pass
            except Exception as exc:
                logger.exception("[desktop] service startup failed: %s", exc)
                try:
                    records = load_startup_failures(self._startup_diagnostics_dir)
                    child_failure = select_startup_failure(records)
                    doctor_result = None
                    if self._should_run_startup_doctor(exc, child_failure):
                        self._set_startup_status(
                            "diagnosing",
                            title="正在诊断启动失败原因",
                            message="服务启动失败，正在检查本机运行环境…",
                            component="desktop-startup",
                            diagnostic_path=str(self._startup_diagnostics_dir),
                        )
                        # 先行导航后 loading 页已离开, 重新载入以展示"正在诊断"。
                        self._present_startup_failure_surface_if_navigated()
                        doctor_result = self._run_doctor_after_failure()
                    else:
                        logger.info(
                            "[desktop] startup doctor skipped; "
                            "failure already has a non-native cause"
                        )
                    failed_status = self._build_failed_status(
                        exc,
                        doctor_result,
                        child_failure,
                    )
                except Exception as diagnostic_exc:  # noqa: BLE001
                    logger.exception(
                        "[desktop] failed to build startup diagnostics: %s",
                        diagnostic_exc,
                    )
                    failed_status = {
                        "title": f"{DISPLAY_NAME} 服务启动失败",
                        "message": self._short_error(exc) or "未知启动错误",
                        "component": "desktop-startup",
                        "diagnostic_path": str(self._startup_diagnostics_dir),
                    }
                self._set_startup_status("failed", **failed_status)
                # 先行导航后 loading 页已被前端 SPA 替换, 重新载入以展示失败诊断页。
                self._present_startup_failure_surface_if_navigated()
                self.shutdown()

        threading.Thread(
            target=_start_services_and_report,
            name="desktop-service-startup",
            daemon=True,
        ).start()

        gui = "edgechromium" if os.name == "nt" else None
        logger.info("[desktop] opening window with loading screen")
        webview.start(
            debug=debug,
            gui=gui,
            private_mode=False,
            storage_path=str(storage_path),
        )

    @staticmethod
    def _build_loading_html() -> str:
        logo_svg = ""
        pkg_dir = Path(__file__).resolve().parent
        logo_path = pkg_dir.parent / "web" / "frontend" / "dist" / "logo.svg"
        if not logo_path.is_file():
            logo_path = pkg_dir.parent / "web" / "frontend" / "public" / "logo.svg"
        if logo_path.is_file():
            try:
                logo_svg = logo_path.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

        return r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden;background:#0f172a;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
color:#e2e8f0;display:flex;align-items:center;justify-content:center}
.root{width:min(680px,calc(100% - 48px));padding:40px}
.panel{display:flex;flex-direction:column;align-items:center;gap:32px;text-align:center}
.hidden{display:none}

/* Logo */
.logo{width:64px;height:64px;border-radius:16px;
background:linear-gradient(135deg,#3b82f6,#8b5cf6);
display:flex;align-items:center;justify-content:center;
box-shadow:0 8px 24px rgba(59,130,246,.25)}
.logo svg{width:64px;height:64px;border-radius:16px}

/* App name */
.app-name{font-size:22px;font-weight:700;letter-spacing:-.3px;color:#f1f5f9}

/* Spinner */
.spinner{width:32px;height:32px;border:3px solid rgba(148,163,184,.2);
border-top-color:#60a5fa;border-radius:50%;animation:spin 1.5s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* Tip area */
.tip-area{margin-top:8px;text-align:center;min-height:60px;
display:flex;flex-direction:column;align-items:center;gap:8px}
.tip-label{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:#475569}
.tip-text{font-size:13px;color:#94a3b8;max-width:320px;line-height:1.5;
transition:opacity .4s ease,transform .4s ease}
.tip-text.fade-out{opacity:0;transform:translateY(-8px)}
.tip-text.fade-in{opacity:1;transform:translateY(0)}

/* Dots */
.dots{display:flex;gap:4px;justify-content:center}
.dot{width:4px;height:4px;border-radius:50%;background:#475569}
.dot.active{background:#60a5fa;animation:pulse 1.2s ease infinite}
@keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}

/* Startup failure */
.error-icon{width:52px;height:52px;border-radius:50%;display:flex;align-items:center;
justify-content:center;background:rgba(239,68,68,.14);color:#f87171;font-size:30px;font-weight:700}
.error-title{font-size:20px;font-weight:700;color:#f8fafc}
.error-message{max-width:620px;color:#cbd5e1;font-size:14px;line-height:1.7;
white-space:pre-wrap;overflow-wrap:anywhere}
.error-meta{width:100%;padding:14px 16px;border:1px solid #334155;border-radius:10px;
background:#111827;color:#94a3b8;font-size:12px;line-height:1.6;text-align:left;
white-space:pre-wrap;overflow-wrap:anywhere;user-select:text}
.close-button{border:0;border-radius:8px;padding:10px 24px;background:#2563eb;color:white;
font-size:14px;cursor:pointer}
.close-button:hover{background:#1d4ed8}
</style>
</head>
<body>
<div class="root">
<div class="panel" id="loading-panel">
<div class="logo">__LOGO_SVG__</div>
<div class="app-name">__APP_DISPLAY_NAME__</div>
<div class="spinner"></div>
<div class="tip-area">
    <div class="tip-label">专属智能AI Agent助理</div>
    <div class="tip-text" id="tip"></div>
</div>
<div class="dots" id="dots"></div>
<div class="tip-label" id="startup-label" style="margin-top:16px">服务启动加载中</div>
</div>
<div class="panel hidden" id="error-panel">
    <div class="error-icon">!</div>
    <div class="error-title" id="error-title">__APP_DISPLAY_NAME__ 服务启动失败</div>
    <div class="error-message" id="error-message"></div>
    <div class="error-meta" id="error-meta"></div>
    <button class="close-button" id="close-button" type="button">退出 __APP_DISPLAY_NAME__</button>
</div>
</div>
<script>
const tips=[
"多智能体协作 —— 编排多个专业 Agent 协同工作，群体智能涌现",
"多端接入 —— 支持 Web、飞书、钉钉、Telegram 等多种交互方式",
"贴身任务管家 —— 精准理解复杂指令，智能排期，有条不紊完成任务",
"自主演进 —— 根据你的反馈自动调整技能，持续进化，越用越懂你"
];
let idx=0;
let terminal=false;
let pollingStarted=false;
let lastStatusAt=0;
const pageStartedAt=Date.now();
const el=document.getElementById('tip');
const dotsEl=document.getElementById('dots');
const loadingPanel=document.getElementById('loading-panel');
const errorPanel=document.getElementById('error-panel');
const startupLabel=document.getElementById('startup-label');

tips.forEach((_,i)=>{
const d=document.createElement('div');
d.className='dot'+(i===0?' active':'');
dotsEl.appendChild(d);
});

function showTip(){
const dots=dotsEl.children;
for(let i=0;i<dots.length;i++) dots[i].className='dot'+(i===idx?' active':'');
el.className='tip-text fade-out';
setTimeout(()=>{
    el.textContent=tips[idx];
    el.className='tip-text fade-in';
},400);
idx=(idx+1)%tips.length;
}
showTip();
setInterval(showTip,3500);

function showFailure(status){
terminal=true;
loadingPanel.classList.add('hidden');
errorPanel.classList.remove('hidden');
document.getElementById('error-title').textContent=
    status.title||'__APP_DISPLAY_NAME__ 服务启动失败';
document.getElementById('error-message').textContent=
    status.message||'启动过程中发生未知错误。';
const meta=[];
if(status.component) meta.push('故障组件：'+status.component);
if(status.diagnostic_path) meta.push('诊断信息：'+status.diagnostic_path);
document.getElementById('error-meta').textContent=
    meta.length?meta.join('\n'):'请查看 __APP_DISPLAY_NAME__ 日志获取详细信息。';
}

async function pollStartupStatus(){
if(terminal) return;
try{
    const status=await window.pywebview.api.get_startup_status();
    lastStatusAt=Date.now();
    if(status.state==='web_ready'||status.state==='ready'){
        terminal=true;
        setTimeout(()=>showFailure({
            title:'__APP_DISPLAY_NAME__ 页面加载失败',
            message:'服务已经启动，但桌面页面无法打开。请退出后重新启动应用。',
            component:'desktop-navigation'
        }),15000);
        window.location.replace(status.frontend_url);
        return;
    }
    if(status.state==='failed'){
        showFailure(status);
        return;
    }
    if(status.state==='diagnosing'){
        startupLabel.textContent=status.message||'正在诊断启动失败原因';
    }
}catch(_error){
    // The watchdog below turns a broken bridge into a visible error state.
}
if(!terminal) setTimeout(pollStartupStatus,500);
}

function waitForBridge(){
if(terminal||pollingStarted) return;
if(window.pywebview&&window.pywebview.api&&window.pywebview.api.get_startup_status){
    pollingStarted=true;
    pollStartupStatus();
    return;
}
setTimeout(waitForBridge,200);
}

window.addEventListener('pywebviewready',waitForBridge);
waitForBridge();
document.getElementById('close-button').addEventListener('click',async()=>{
try{
    await window.pywebview.api.close_window();
}catch(_error){
    window.close();
}
});

setInterval(()=>{
if(terminal) return;
const now=Date.now();
const bridgeNeverResponded=lastStatusAt===0&&now-pageStartedAt>120000;
const bridgeStoppedResponding=lastStatusAt>0&&now-lastStatusAt>30000;
if(bridgeNeverResponded||bridgeStoppedResponding){
    showFailure({
        title:'无法获取启动状态',
        message:'桌面界面与启动服务之间的通信中断。请退出后重新启动应用。',
        component:'desktop-webview-bridge'
    });
}
},5000);
</script>
</body>
</html>""".replace("__LOGO_SVG__", logo_svg).replace("__APP_DISPLAY_NAME__", DISPLAY_NAME)

    def _on_loaded_first(self) -> None:
        if self.window is not None:
            # 窗口首次加载后最大化（全屏会影响用户体验）
            if hasattr(self.window, "maximize"):
                self.window.maximize()
            self.window.events.loaded -= self._on_loaded_first
            self.window.events.loaded += self._on_loaded

    def _on_loaded(self) -> None:
        # Frontend navigation completed; bind OS file drop path bridge for the new document.
        # Note: never touch the WebView2 controller (window.native.*) from this
        # thread — pywebview fires `loaded` on a background thread and WebView2
        # controller members are UI-thread-only. A cross-apartment COM call from
        # here intermittently deadlocks the UI thread (window "not responding").
        # AllowExternalDrop defaults to true, so no controller access is needed.
        self._schedule_desktop_file_dnd_bind()

    def _on_closed(self) -> None:
        self.shutdown()


def _psutil_terminate(pid: int, force: bool = False) -> None:
    """Terminate a process and all its descendants using psutil.

    Unlike ``taskkill.exe``, this is a pure-Python operation that does not
    spawn an external console process, avoiding console window flashes on
    Windows (console=False builds).
    """
    try:
        import psutil

        parent = psutil.Process(pid)
        # 获取所有子孙进程（在杀父进程之前先拿到完整列表）
        children = parent.children(recursive=True)
        kill_fn = (lambda p: p.kill()) if force else (lambda p: p.terminate())
        # 先杀子孙，再杀父进程，避免子孙变成孤儿
        for child in reversed(children):
            try:
                kill_fn(child)
            except psutil.NoSuchProcess:
                pass
        try:
            kill_fn(parent)
        except psutil.NoSuchProcess:
            pass
    except Exception:  # noqa: BLE001
        pass


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Gracefully terminate a process and all its descendants."""
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            process.terminate()
    else:
        _psutil_terminate(process.pid, force=False)


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Force kill a process and all its descendants."""
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
    else:
        _psutil_terminate(process.pid, force=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"Launch {DISPLAY_NAME} desktop window.")
    parser.add_argument("--title", default=DISPLAY_NAME, help="Desktop window title.")
    parser.add_argument("--width", type=int, default=1440, help="Initial window width.")
    parser.add_argument(
        "--height", type=int, default=960, help="Initial window height."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable pywebview debug mode.",
    )
    parser.add_argument(UPDATE_HELPER_FLAG, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--installer-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--app-executable", default="", help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument(
        "--backend-port", type=int, default=BACKEND_PORT, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--frontend-port", type=int, default=FRONTEND_PORT, help=argparse.SUPPRESS
    )
    return parser.parse_args()


def _setup_tui_path() -> None:
    """Auto-add jiuwenswarm-tui to PATH via ~/.zshrc on macOS."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return
    tui_binary = Path(sys.executable).parent / "jiuwenswarm-tui"
    if not tui_binary.is_file():
        return
    # Prefer /Applications path over /Volumes (DMG mount) path
    tui_dir = str(tui_binary.parent)
    apps_dir = f"/Applications/{APP_BUNDLE_NAME}/Contents/MacOS"
    if Path(apps_dir).is_dir():
        tui_dir = apps_dir
    marker = f"{APP_BUNDLE_NAME}/Contents/MacOS"
    zshrc = Path.home() / ".zshrc"
    try:
        existing = zshrc.read_text(encoding="utf-8") if zshrc.exists() else ""
        if marker in existing:
            return
        with open(zshrc, "a", encoding="utf-8") as f:
            f.write(f"\n# Added by {DISPLAY_NAME} - jiuwenswarm-tui CLI\n")
            f.write(f'export PATH="{tui_dir}:$PATH"\n')
        logger.info("[desktop] added TUI to PATH in ~/.zshrc")
    except OSError as exc:
        logger.warning("[desktop] failed to update ~/.zshrc: %s", exc)


def main() -> None:
    args = _parse_args()
    if getattr(args, "desktop_install_update", False):
        _launch_windows_installer_helper(
            args.installer_path,
            args.app_executable,
            args.parent_pid,
            backend_port=args.backend_port,
            frontend_port=args.frontend_port,
        )
        return

    _cleanup_stale_update_artifacts()
    _setup_tui_path()

    try:
        ports = resolve_desktop_ports()
    except RuntimeError as exc:
        logger.error("[desktop] port resolution failed: %s", exc)
        raise SystemExit(1) from exc

    runtime = DesktopRuntime(
        frontend_host=FRONTEND_HOST,
        ports=ports,
    )
    try:
        runtime.run(
            window_title=args.title,
            width=args.width,
            height=args.height,
            debug=args.debug,
        )
    finally:
        runtime.shutdown()


if __name__ == "__main__":
    main()
