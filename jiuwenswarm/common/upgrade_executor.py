from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from jiuwenswarm.common.version import __version__
from jiuwenswarm.common.utils import get_user_workspace_dir


DOWNLOAD_CHUNK_SIZE = 1024 * 512


def _updates_dir() -> Path:
    path = get_user_workspace_dir() / ".updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


class UpgradeExecutor(ABC):
    upgrade_mode: str = ""
    is_platform_supported: bool = True

    def __init__(
        self,
        config: dict[str, Any],
        status_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        self._config = config
        self._status_callback = status_callback

    @abstractmethod
    def install(self) -> None:
        ...

    def upgrade(self) -> None:
        """Default upgrade raises; overridden by executors that install."""
        raise NotImplementedError(
            f"{type(self).__name__} does not implement upgrade()"
        )

    @staticmethod
    def _fetch_text(url: str, headers: dict[str, str], timeout: int) -> str:
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} when requesting {url}") from exc
        except socket.timeout as exc:
            raise RuntimeError(
                f"Timeout ({timeout}s) when requesting {url}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"Network error when requesting {url}: {exc.reason}"
            ) from exc

    @staticmethod
    def _download_headers() -> dict[str, str]:
        from jiuwenswarm.common.updater import get_access_token

        headers: dict[str, str] = {
            "Accept": "application/octet-stream, */*",
            "User-Agent": f"JiuwenSwarm-Updater/{__version__}",
        }
        token = get_access_token()
        if token:
            headers["PRIVATE-TOKEN"] = token
        return headers


class DesktopExecutor(UpgradeExecutor):
    upgrade_mode = "desktop"

    def __init__(
        self,
        config: dict[str, Any],
        status_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        super().__init__(config, status_callback)
        self.is_platform_supported = True

    def install(self) -> None:
        timeout = self._config["timeout_seconds"]
        download_url = str(self._config.get("download_url", ""))
        asset_name = str(self._config.get("asset_name", ""))
        headers = self._download_headers()

        final_path = _updates_dir() / asset_name
        partial_path = final_path.with_suffix(final_path.suffix + ".part")

        try:
            self._download_file(download_url, partial_path, headers, timeout)

            partial_path.replace(final_path)
            size = final_path.stat().st_size
            self._status_callback({
                "state": "downloaded",
                "downloaded_path": str(final_path),
                "downloaded_bytes": size,
                "total_bytes": size,
                "error": "",
            })
        except Exception as exc:
            if partial_path.exists():
                partial_path.unlink(missing_ok=True)
            self._status_callback({
                "state": "error",
                "error": f"Update download failed: {exc}",
                "downloaded_bytes": 0,
            })

    def _download_file(
        self,
        url: str,
        destination: Path,
        headers: dict[str, str],
        timeout: int,
    ) -> None:
        request = Request(url, headers=headers)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (
            urlopen(request, timeout=timeout) as response,
            open(destination, "wb") as handle,
        ):
            total_header = response.headers.get("Content-Length")
            total_bytes = (
                int(total_header) if total_header and total_header.isdigit() else 0
            )
            self._status_callback({"total_bytes": total_bytes})

            downloaded = 0
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                self._status_callback(
                    {"downloaded_bytes": downloaded, "total_bytes": total_bytes}
                )


class PipExecutor(UpgradeExecutor):
    upgrade_mode = "pip"

    def __init__(
        self,
        config: dict[str, Any],
        status_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        super().__init__(config, status_callback)
        self.is_platform_supported = True

    def install(self) -> None:
        package = self._config.get("repo_name", "jiuwenswarm")
        timeout = self._config["timeout_seconds"]

        self._status_callback({
            "downloaded_bytes": 0,
            "total_bytes": 100,
            "current_activity": "",
            "error": "",
        })

        try:
            editable_info = self._check_editable_install(package)
            if editable_info:
                raise RuntimeError(editable_info)

            pip_args = self._build_install_args(package, timeout)

            process = subprocess.Popen(
                pip_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            lines: list[str] = []
            last_report = 0.0
            if process.stdout:
                for raw_line in iter(process.stdout.readline, ""):
                    line = raw_line.rstrip("\r\n")
                    lines.append(line)
                    now = time.time()
                    if now - last_report >= 0.5:
                        last_report = now
                        self._status_callback({
                            "downloaded_bytes": min(len(lines), 99),
                            "total_bytes": 100,
                            "current_activity": line,
                            "error": "",
                        })

            process.wait()

            if process.returncode != 0:
                raise RuntimeError(
                    f"pip install failed (exit {process.returncode}): "
                    + "\n".join(lines[-20:])
                )

            pip_output = "\n".join(lines)
            self._status_callback({
                "state": "restart_pending",
                "downloaded_bytes": 100,
                "total_bytes": 100,
                "current_activity": "",
                "error": "",
                "pip_output": pip_output,
            })
        except Exception as exc:
            self._status_callback({
                "state": "update_available",
                "error": f"pip install failed: {exc}",
                "downloaded_bytes": 0,
            })

    def _check_editable_install(self, package: str) -> str | None:
        uv_cmd = self._resolve_uv_command()
        try:
            if uv_cmd:
                result = subprocess.run(
                    [uv_cmd, "pip", "show", "--format", "json", package],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    import json as _json
                    data = _json.loads(result.stdout)
                    if isinstance(data, dict) and data.get("editable"):
                        return (
                            f"'{package}' is installed as an editable package. "
                            "Use 'git pull && uv sync' to update instead."
                        )
            else:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "show", package],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if line.startswith("Editable:"):
                            return (
                                f"'{package}' is installed as an editable package. "
                                "Use 'git pull && pip install -e .' to update instead."
                            )
        except Exception:
            pass
        return None

    @staticmethod
    def _find_uv_binary() -> str | None:
        uv_name = "uv.exe" if sys.platform == "win32" else "uv"
        python_dir = Path(sys.executable).parent

        uv_in_python_dir = python_dir / uv_name
        if uv_in_python_dir.is_file():
            return str(uv_in_python_dir)

        uv_on_path = shutil.which("uv")
        if uv_on_path:
            return uv_on_path

        return None

    @staticmethod
    def _is_uv_managed_venv() -> bool:
        """Return True only when running inside a uv-managed virtual environment."""
        # System Python (no venv active) — never use uv
        if sys.prefix == sys.base_prefix:
            return False

        # Conda environment — use conda's own pip, not uv
        if os.environ.get("CONDA_PREFIX") or (Path(sys.prefix) / "conda-meta").is_dir():
            return False

        # uv writes "uv = X.Y.Z" into pyvenv.cfg when creating a venv
        venv_path = os.environ.get("VIRTUAL_ENV") or sys.prefix
        cfg_file = Path(venv_path) / "pyvenv.cfg"
        if cfg_file.is_file():
            try:
                text = cfg_file.read_text(encoding="utf-8")
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("uv ") or stripped == "uv":
                        return True
            except Exception:
                pass

        return False

    @staticmethod
    def _resolve_uv_command() -> str | None:
        if not PipExecutor._is_uv_managed_venv():
            return None
        return PipExecutor._find_uv_binary()

    def _build_install_args(self, package: str, timeout: int) -> list[str]:
        pypi_mirror = self._config.get("pypi_mirror", "").strip()

        uv_cmd = self._resolve_uv_command()
        if uv_cmd:
            args = [uv_cmd, "pip", "install", "--upgrade", package]
            if pypi_mirror:
                args.extend(["--index-url", pypi_mirror])
            return args

        args = [
            sys.executable, "-m", "pip", "install", "--upgrade",
            "--timeout", str(timeout),
            package,
        ]
        if pypi_mirror:
            args.extend(["-i", pypi_mirror])
        return args

    def upgrade(self) -> None:
        start_cmd_raw = os.getenv("JIUWENSWARM_START_CMD")
        if start_cmd_raw:
            try:
                stored_argv = json.loads(start_cmd_raw)
            except Exception:
                stored_argv = sys.argv[:]
        else:
            stored_argv = sys.argv[:]

        start_is_start_cmd = bool(start_cmd_raw) and (
            "jiuwenswarm-start" in str(stored_argv)
            or "start_services" in str(stored_argv)
        )

        if start_is_start_cmd:
            restart_argv = [sys.executable, "-m", "jiuwenswarm.start_services"] + stored_argv[1:]
        else:
            restart_argv = [sys.executable, "-m", "jiuwenswarm.app"] + stored_argv[1:]

        restart_cmd = subprocess.list2cmdline(restart_argv)
        self._status_callback({"state": "restarting", "installing": True, "error": "", "restart_command": restart_cmd})

        web_argv = None
        web_pid = None

        if start_is_start_cmd:
            web_argv = None
            web_pid = None
        else:
            web_info_file = _updates_dir() / "web_process.json"
            if web_info_file.is_file():
                try:
                    web_data = json.loads(web_info_file.read_text(encoding="utf-8"))
                    if isinstance(web_data.get("argv"), list):
                        raw_web_argv = web_data["argv"]
                        if raw_web_argv and raw_web_argv[0]:
                            web_argv = [sys.executable, "-m", "jiuwenswarm.channels.web.app_web"] + raw_web_argv[1:]
                        else:
                            web_argv = None
                        web_pid = int(web_data["pid"]) if web_data.get("pid") else None
                except Exception:
                    pass

        frontend_port = int(os.getenv("FRONTEND_PORT", "5173"))
        restart_data = {
            "argv": restart_argv,
            "env": dict(os.environ),
            "cwd": os.getcwd(),
            "pid": os.getpid(),
            "web_argv": web_argv,
            "web_pid": web_pid,
            "gateway_port": int(os.getenv("WEB_PORT", "19000")),
            "frontend_port": frontend_port,
            "timestamp": time.time(),
        }
        restart_file = _updates_dir() / ".restart_pending.json"
        with open(restart_file, "w", encoding="utf-8") as f:
            f.write(json.dumps(restart_data, indent=2))
            f.flush()
            os.fsync(f.fileno())

        helper_args = [
            sys.executable, "-m", "jiuwenswarm.common.updater_restart_helper",
            "--restart-json", str(restart_file),
            "--parent-pid", str(os.getpid()),
        ]

        if sys.platform == "win32":
            subprocess.Popen(
                helper_args,
                creationflags=(
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.Popen(
                helper_args,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def create_executor(
    install_mode: str,
    config: dict[str, Any],
    status_callback: Callable[[dict[str, Any]], None],
) -> UpgradeExecutor:
    if install_mode == "pip":
        return PipExecutor(config, status_callback)

    return DesktopExecutor(config, status_callback)