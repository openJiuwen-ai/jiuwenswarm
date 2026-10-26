from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from jiuwenswarm.common.utils import wait_for_pid_exit, wait_for_tcp_port


logger = logging.getLogger(__name__)


def _background_flags() -> int:
    return (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    return wait_for_tcp_port(host, port, timeout=timeout, target_state="connected")


def _wait_for_port_release(host: str, port: int, timeout: float = 15.0) -> bool:
    return wait_for_tcp_port(host, port, timeout=timeout, target_state="disconnected")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = sys.argv[1:]
    restart_json_path: str | None = None
    parent_pid: int | None = None

    i = 0
    while i < len(args):
        if args[i] == "--restart-json" and i + 1 < len(args):
            restart_json_path = args[i + 1]
            i += 2
        elif args[i] == "--parent-pid" and i + 1 < len(args):
            parent_pid = int(args[i + 1])
            i += 2
        else:
            i += 1

    if not restart_json_path:
        logger.error("Missing --restart-json argument")
        sys.exit(1)
    if not parent_pid:
        logger.error("Missing --parent-pid argument")
        sys.exit(1)

    wait_for_pid_exit(parent_pid, timeout=60.0)

    json_path = Path(restart_json_path)
    if not json_path.is_file():
        logger.error("Restart JSON not found: %s", json_path)
        sys.exit(1)

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed to read restart JSON: %s", exc)
        sys.exit(1)

    argv: list[str] = data.get("argv", [])
    env_data: dict[str, str] | None = data.get("env")
    cwd: str | None = data.get("cwd")
    web_argv: list[str] | None = data.get("web_argv")
    web_pid: int | None = data.get("web_pid")
    gateway_port: int = data.get("gateway_port", 19000)
    frontend_port: int = data.get("frontend_port", 5173)

    if not argv:
        logger.error("No argv in restart JSON")
        sys.exit(1)

    if web_pid:
        try:
            os.kill(web_pid, signal.SIGTERM)
        except OSError as exc:
            logger.warning("Failed to kill web process %d: %s", web_pid, exc)

    _wait_for_port_release("127.0.0.1", gateway_port, timeout=15.0)
    if frontend_port != gateway_port:
        _wait_for_port_release("127.0.0.1", frontend_port, timeout=15.0)

    popen_kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }

    if env_data:
        popen_kwargs["env"] = env_data
    if cwd:
        popen_kwargs["cwd"] = cwd

    if sys.platform == "win32":
        popen_kwargs["creationflags"] = _background_flags()
    else:
        popen_kwargs["start_new_session"] = True

    subprocess.Popen(argv, **popen_kwargs)

    if web_argv:
        _wait_for_port("127.0.0.1", gateway_port, timeout=30.0)

        if sys.platform == "win32":
            web_kwargs = {**popen_kwargs, "creationflags": _background_flags()}
        else:
            web_kwargs = popen_kwargs
        subprocess.Popen(web_argv, **web_kwargs)

    json_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()