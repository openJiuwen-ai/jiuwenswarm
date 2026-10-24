"""Bootstrap the persistent engine worker inside the configured executor."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import sys


def main() -> None:
    configuration = Path(os.environ.pop("JIUWENSWARM_CODE_WORKER_CONFIG"))
    settings = json.loads(configuration.read_text(encoding="utf-8"))
    port, token = settings["port"], settings["token"]
    lifetime = socket.create_connection(("127.0.0.1", port), timeout=30)
    lifetime.settimeout(None)
    lifetime.sendall(f"{token}\tlifetime\t{os.getpid()}\n".encode("ascii"))
    if os.name != "nt":
        # The driver's exec adapter maps this inherited descriptor to fd 4;
        # do not overwrite a descriptor owned by the frozen host's logging.
        os.set_inheritable(lifetime.fileno(), True)
        os.environ["COMPUTER_USE_LIFETIME_FD"] = str(lifetime.fileno())
    else:
        os.environ.pop("COMPUTER_USE_LIFETIME_FD", None)
    if not getattr(sys, "frozen", False):
        from .hosting import engine_root, import_engine

        import_engine(engine_root())
    from computer_use.mcp.runtime import main as run_worker

    os.environ["COMPUTER_USE_WORKER_PORT"] = str(port)
    os.environ["COMPUTER_USE_WORKER_TOKEN"] = token
    os.environ["COMPUTER_USE_WORKER_ADMISSION"] = "1"
    try:
        asyncio.run(run_worker())
    finally:
        lifetime.close()
