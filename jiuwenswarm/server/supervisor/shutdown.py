# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit application-owner shutdown after its Front has detached."""
from __future__ import annotations

import asyncio
import argparse
import json
import logging
import sys
import time

from jiuwenswarm.server.ipc.client import connect, endpoint_is_listening
from jiuwenswarm.server.ipc.protocol import PROTOCOL_VERSION
from jiuwenswarm.server.supervisor.identity import data_scope
from jiuwenswarm.server.supervisor.worker_process import default_socket_path

logger = logging.getLogger(__name__)


def detach_front_process(pid: int) -> None:
    """Stop an owned Front launcher subtree while retaining Worker and tools."""
    import psutil
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        by_pid = {child.pid: child for child in children}
        retained = set()
        fronts = []
        for child in [parent, *children]:
            argv = child.cmdline()
            module = argv[argv.index("-m") + 1] if "-m" in argv else ""
            if module == "jiuwenswarm.server.worker.main":
                retained.add(child.pid)
            if module == "jiuwenswarm.server.app_agentserver" or "--desktop-run-agent" in argv:
                fronts.append(child.pid)
        if not fronts:
            raise RuntimeError("Front detach refused a process without an AgentServer child")
        for child in children:
            ancestor = child.ppid()
            while ancestor in by_pid and ancestor not in retained:
                ancestor = by_pid[ancestor].ppid()
            if ancestor in retained:
                retained.add(child.pid)
        for child in reversed(children):
            if child.pid not in retained:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
        parent.terminate()
    except psutil.NoSuchProcess:
        pass


async def shutdown_resident_worker(*, timeout: float = 30.0) -> dict:
    """Desktop final exit/upgrade authority; ordinary Front restart never calls it.

    Validate the trusted directory/profile and the actual process identity
    before shutdown. Code identity may differ during an explicitly requested
    upgrade. Unvalidated processes are never terminated.
    """
    socket_path = default_socket_path()
    if not endpoint_is_listening(socket_path):
        return {"ok": True, "absent": True}
    deadline = time.monotonic() + timeout
    connection = None
    while connection is None and time.monotonic() < deadline:
        try:
            connection = await asyncio.wait_for(connect(socket_path), min(.2, deadline - time.monotonic()))
        except (OSError, RuntimeError, asyncio.TimeoutError):
            await asyncio.sleep(.025)
    if connection is None:
        raise TimeoutError("Worker shutdown could not acquire its detached IPC connection")
    worker = None
    created = None
    try:
        hello = await asyncio.wait_for(connection.recv(), max(.001, deadline - time.monotonic()))
        if (hello.get("kind") != "hello" or hello.get("protocol") != PROTOCOL_VERSION
                or hello.get("data_scope") != data_scope()):
            raise RuntimeError("Worker shutdown refused an unvalidated deployment")
        import psutil
        worker = psutil.Process(int(hello["pid"]))
        created = worker.create_time()
        await connection.send({"kind": "shutdown"})
        while time.monotonic() < deadline:
            frame = await asyncio.wait_for(connection.recv(), max(.001, deadline - time.monotonic()))
            if frame.get("kind") == "shutdown.done":
                if frame.get("ok") is not True:
                    raise RuntimeError(f"Worker reported incomplete shutdown: {frame.get('error', '')}")
                return frame
        raise TimeoutError("Worker shutdown exceeded its application deadline")
    except (asyncio.TimeoutError, OSError, RuntimeError):
        if worker is not None:
            import psutil
            try:
                if worker.create_time() == created and worker.is_running():
                    logger.error(
                        "Worker final exit forced; shutdown acknowledgement/persistence incomplete pid=%s", worker.pid
                    )
                    for child in reversed(worker.children(recursive=True)):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    worker.kill()
            except psutil.NoSuchProcess:
                pass
        raise
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Application-owner Runtime lifecycle control")
    parser.add_argument("--detach-front", type=int)
    parser.add_argument("--shutdown-worker", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.detach_front:
        detach_front_process(args.detach_front)
    if args.shutdown_worker:
        result = asyncio.run(shutdown_resident_worker(timeout=max(.001, args.timeout)))
        result_logger = logging.Logger(f"{__name__}.result", level=logging.INFO)
        result_handler = logging.StreamHandler(sys.stdout)
        result_handler.setFormatter(logging.Formatter("%(message)s"))
        result_logger.addHandler(result_handler)
        try:
            result_logger.info("%s", json.dumps(result))
        finally:
            result_handler.close()


if __name__ == "__main__":
    main()
