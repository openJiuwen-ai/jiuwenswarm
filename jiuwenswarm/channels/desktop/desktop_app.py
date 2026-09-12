from __future__ import annotations

import argparse
import base64
import binascii
import http.client
import logging
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
from pathlib import Path

from logging.handlers import RotatingFileHandler

import webview

from jiuwenswarm.common.utils import get_user_workspace_dir, get_logs_dir, wait_for_pid_exit, wait_for_tcp_port
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
UPDATE_HELPER_FLAG = "--desktop-install-update"
DESKTOP_ENV_FLAG = "JIUWENSWARM_DESKTOP"
STARTUP_TIMEOUT_SECONDS = 45.0
PNG_DATA_URL_PREFIX = "data:image/png;base64,"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DesktopSaveResult = dict[str, bool]
UPDATE_CLEANUP_PATTERNS = (
    "JiuwenSwarm-setup-*.exe",
    "JiuwenSwarm-*.dmg",
    "JiuwenSwarm-*.tar.gz",
    "JiuwenSwarm-*.exe.part",
    "JiuwenSwarm-*.dmg.part",
    "JiuwenSwarm-*.tar.gz.part",
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
        fmt="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(filename)s:%(lineno)d: %(message)s",
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
        if name == "app":
            flag = APP_CHILD_FLAG
        elif name == "web":
            flag = WEB_CHILD_FLAG
        else:
            flag = UPDATE_HELPER_FLAG
        base = [sys.executable, flag]
    elif name == "app":
        base = [sys.executable, "-m", "jiuwenswarm.app"]
    elif name == "web":
        base = [sys.executable, "-m", "jiuwenswarm.channels.web.app_web"]
    else:
        base = [sys.executable, "-m", "jiuwenswarm.channels.desktop.desktop_app", UPDATE_HELPER_FLAG]
    if extra_args:
        base.extend(extra_args)
    return base


def _build_child_env(name: str, ports: dict[str, int]) -> dict[str, str]:
    env = os.environ.copy()
    env[DESKTOP_ENV_FLAG] = "1"
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
    elif name == "app":
        logger.info(
            "[desktop] app child ports: %s",
            _format_ports_for_log(ports),
        )
    return env


def _start_process(
    name: str,
    command: list[str],
    ports: dict[str, int],
) -> subprocess.Popen[bytes]:
    logger.info("[desktop] starting %s: %s", name, command)
    kwargs: dict[str, object] = {
        "env": _build_child_env(name, ports),
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
# 只读首页 (4096B) 触发预读, 零执行零副作用; 非冻结模式 (dev) 无 _MEIPASS 直接跳过.
_WARMUP_PACKAGES = (
    "openjiuwen", "faiss", "pymilvus", "google", "a2ui",
    "sqlite_vec", "tree_sitter", "tiktoken", "tiktoken_ext",
)


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
                            _ = fh.read(4096)
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
            time.sleep(0.35)

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
        time.sleep(0.35)

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
        """保存前端生成的 data URL 文件，供分享图片导出使用。"""
        return self._runtime.save_data_url(data_url, filename)

    def select_project_directory(self) -> str | None:
        """打开系统目录选择器，返回用户选择的项目目录绝对路径。"""
        return self._runtime.select_project_directory()


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
        self._is_shutting_down = False

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

    def start_services(self) -> None:
        # Per-workspace Gateway preflight: refuse to start a second full stack
        # over the same workspace (two CronSchedulerService instances over one
        # cron_jobs.json => duplicate cron executions). Briefly wait so an
        # in-flight upgrade restart (old gateway shutting down) can release
        # the lock before we fail.
        self._preflight_gateway_singleton()
        # 先起后台预读, 与后续子进程拉起/端口等待并行, 不阻塞 start_services.
        _warmup_page_cache_background()
        self.processes["app"] = _start_process(
            "app", _build_child_command("app"), self.ports
        )
        _ensure_process_running("app", self.processes["app"])
        _wait_for_tcp(
            BACKEND_HOST,
            self.backend_port,
            STARTUP_TIMEOUT_SECONDS,
            process=self.processes["app"],
        )

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
        self.processes["web"] = _start_process("web", web_command, self.ports)
        _ensure_process_running("web", self.processes["web"])
        _wait_for_http(
            self.frontend_host,
            self.frontend_port,
            "/",
            STARTUP_TIMEOUT_SECONDS,
            process=self.processes["web"],
        )
        logger.info("[desktop] services ready: %s", self.frontend_url)

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

    def save_data_url(self, data_url: str, filename: str) -> DesktopSaveResult:
        """选择保存位置并保存 PNG data URL。"""
        if not isinstance(data_url, str) or not data_url.startswith(PNG_DATA_URL_PREFIX):
            logger.error("[desktop] invalid data url for share export")
            return _desktop_save_result(False)

        try:
            image_bytes = base64.b64decode(data_url[len(PNG_DATA_URL_PREFIX):], validate=True)
        except binascii.Error as exc:
            logger.error("[desktop] failed to decode share export data url: %s", exc)
            return _desktop_save_result(False)

        if not image_bytes.startswith(PNG_SIGNATURE):
            logger.error("[desktop] share export data is not a PNG")
            return _desktop_save_result(False)

        try:
            selected_path = self._select_save_path(filename, ("PNG Image (*.png)",))
            if selected_path is None:
                logger.info("[desktop] share image save cancelled by user")
                return _desktop_save_result(False, cancelled=True)

            selected_path.write_bytes(image_bytes)
            logger.info("[desktop] share image saved to: %s", selected_path)
            return _desktop_save_result(True)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("[desktop] failed to save share image: %s", exc)
            return _desktop_save_result(False)

    @staticmethod
    def _show_download_complete(file_path: str) -> None:
        """下载完成后提醒用户并打开文件所在文件夹。"""
        try:
            if os.name == "nt":
                import ctypes
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
        #   /Applications/JiuwenSwarm.app/Contents/MacOS/jiuwenswarm
        # so the bundle is three levels up. Prefer replacing the exact bundle
        # the user launched, but fall back to /Applications when running from a
        # read-only DMG mount or from a non-bundled development executable.
        app_bundle = app_executable.parent.parent.parent
        if app_bundle.suffix == ".app" and not str(app_bundle).startswith("/Volumes/"):
            install_target = str(app_bundle)
        elif app_bundle.suffix == ".app":
            install_target = f"/Applications/{app_bundle.name}"
        else:
            install_target = "/Applications/JiuwenSwarm.app"

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

echo "=== JiuwenSwarm macOS install helper: $(date) ==="
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
        q_executable = shlex.quote(f"{install_dir}/jiuwenswarm")

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

        deadline = time.monotonic() + 8.0
        logger.info("[desktop] shutting down child processes")

        for process in self.processes.values():
            if process.poll() is None:
                _terminate_process_tree(process)

        while time.monotonic() < deadline:
            if all(process.poll() is not None for process in self.processes.values()):
                break
            time.sleep(0.2)

        for process in self.processes.values():
            if process.poll() is None:
                _kill_process_tree(process)

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
        cache_dir = Path.home() / "Library" / "Caches" / "com.jiuwenswarm.desktop"
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

        def _start_services_and_navigate() -> None:
            try:
                self.start_services()
                if self.window is not None:
                    self.window.load_url(self.frontend_url)
            except Exception as exc:
                logger.error("[desktop] service startup failed: %s", exc)

        threading.Thread(target=_start_services_and_navigate, daemon=True).start()

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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:100%;height:100%;overflow:hidden;background:#0f172a;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
color:#e2e8f0;display:flex;align-items:center;justify-content:center}
.root{display:flex;flex-direction:column;align-items:center;gap:32px;padding:40px}

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
</style>
</head>
<body>
<div class="root">
<div class="logo">__LOGO_SVG__</div>
<div class="app-name">JiuwenSwarm</div>
<div class="spinner"></div>
<div class="tip-area">
    <div class="tip-label">专属智能AI Agent助理</div>
    <div class="tip-text" id="tip"></div>
</div>
<div class="dots" id="dots"></div>
<div class="tip-label" style="margin-top:16px">服务启动加载中</div>
</div>
<script>
const tips=[
"多智能体协作 —— 编排多个专业 Agent 协同工作，群体智能涌现",
"多端接入 —— 支持 Web、飞书、钉钉、Telegram 等多种交互方式",
"贴身任务管家 —— 精准理解复杂指令，智能排期，有条不紊完成任务",
"自主演进 —— 根据你的反馈自动调整技能，持续进化，越用越懂你"
];
let idx=0;
const el=document.getElementById('tip');
const dotsEl=document.getElementById('dots');

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
</script>
</body>
</html>""".replace("__LOGO_SVG__", logo_svg)

    def _on_loaded_first(self) -> None:
        if self.window is not None:
            # 窗口首次加载后最大化（全屏会影响用户体验）
            if hasattr(self.window, "maximize"):
                self.window.maximize()
            self.window.events.loaded -= self._on_loaded_first
            self.window.events.loaded += self._on_loaded

    def _on_loaded(self) -> None:
        pass

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
    parser = argparse.ArgumentParser(description="Launch JiuwenSwarm desktop window.")
    parser.add_argument("--title", default="JiuwenSwarm", help="Desktop window title.")
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
    apps_dir = "/Applications/JiuwenSwarm.app/Contents/MacOS"
    if Path(apps_dir).is_dir():
        tui_dir = apps_dir
    marker = "JiuwenSwarm.app/Contents/MacOS"
    zshrc = Path.home() / ".zshrc"
    try:
        existing = zshrc.read_text(encoding="utf-8") if zshrc.exists() else ""
        if marker in existing:
            return
        with open(zshrc, "a", encoding="utf-8") as f:
            f.write(f"\n# Added by JiuwenSwarm - jiuwenswarm-tui CLI\n")
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
