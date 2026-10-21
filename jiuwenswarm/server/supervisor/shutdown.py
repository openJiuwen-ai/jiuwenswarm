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
    except psutil.NoSuchProcess:
        return
    # A memory/tool subprocess can exit between enumeration and inspection.
    # Its disappearance must not skip detaching the still-live Front.
    records = {}
    for child in [parent, *children]:
        try:
            argv = child.cmdline()
            ancestor = child.ppid()
        except psutil.NoSuchProcess:
            continue
        module = argv[argv.index("-m") + 1] if "-m" in argv else ""
        records[child.pid] = (child, ancestor, module, argv)
    retained = {value for value, record in records.items()
                if record[2] == "jiuwenswarm.server.worker.main"}
    if not any(record[2] == "jiuwenswarm.server.app_agentserver" or "--desktop-run-agent" in record[3]
               for record in records.values()):
        raise RuntimeError("Front detach refused a process without an AgentServer child")
    for child_pid, (_child, ancestor, _module, _argv) in records.items():
        seen = set()
        while ancestor in records and ancestor not in retained and ancestor not in seen:
            seen.add(ancestor)
            ancestor = records[ancestor][1]
        if ancestor in retained:
            retained.add(child_pid)
    for child in reversed(children):
        if child.pid not in retained:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
    try:
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
        from jiuwenswarm.server.ipc.startup import startup_acceptance

        await connection.send(startup_acceptance(hello))
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
