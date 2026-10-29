# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit application-owner shutdown after its Front has detached."""
from __future__ import annotations

import asyncio
import argparse
import json
import logging
import sys
import time

from jiuwenswarm.server.ipc.client import connect, endpoint_is_listening, try_connect
from jiuwenswarm.server.supervisor.worker_process import default_socket_path, legacy_socket_paths
from jiuwenswarm.server.supervisor.resident_lifecycle import retire_worker, startup_lock


def detach_front_process(pid: int, *, force: bool = False, timeout: float = 5.0) -> None:
    """Stop an owned Front subtree; unknown survivors cannot mean success."""
    import psutil

    deadline = time.monotonic() + max(0.0, timeout)
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    by_pid = {child.pid: child for child in children}
    workers = set()
    unknown = set()
    fronts = []
    for child in [parent, *children]:
        try:
            argv = child.cmdline()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            unknown.add(child.pid)
            continue
        module = argv[argv.index("-m") + 1] if "-m" in argv[:-1] else ""
        if module == "jiuwenswarm.server.worker.main":
            workers.add(child.pid)
        if module == "jiuwenswarm.server.app_agentserver" or "--desktop-run-agent" in argv:
            fronts.append(child.pid)
    if not fronts:
        raise RuntimeError("Front detach refused a process without an AgentServer child")
    if parent.pid in workers:
        raise RuntimeError("Front detach refused a Worker root")
    retained = set(workers)
    for child in children:
        try:
            ancestor = child.ppid()
            visited = set()
            while ancestor in by_pid and ancestor not in workers:
                if ancestor in visited:
                    raise RuntimeError("Front ancestry could not be established")
                visited.add(ancestor)
                ancestor = by_pid[ancestor].ppid()
            if ancestor in workers:
                retained.add(child.pid)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            unknown.add(child.pid)
    if unknown - retained:
        raise RuntimeError("Front detach incomplete: descendant ownership could not be established")
    targets = [child for child in reversed(children) if child.pid not in retained]
    for child in targets:
        try:
            child.kill() if force else child.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as exc:
            raise RuntimeError("Front detach incomplete: process termination failed") from exc
    _, alive = psutil.wait_procs(targets, timeout=max(0.0, deadline - time.monotonic()))
    if alive:
        raise RuntimeError("Front detach incomplete: process exit could not be observed")
    # Keep the launcher available for a forced retry until descendants are
    # observed gone. Losing it first would orphan an unconfirmed Front.
    try:
        parent.kill() if force else parent.terminate()
    except psutil.NoSuchProcess:
        return
    _, alive = psutil.wait_procs([parent], timeout=max(0.0, deadline - time.monotonic()))
    if alive:
        raise RuntimeError("Front detach incomplete: launcher exit could not be observed")


async def shutdown_resident_worker(*, timeout: float = 30.0, socket_path: str | None = None) -> dict:
    """Desktop final exit/upgrade authority; ordinary Front restart never calls it.

    Validate the trusted directory/profile and the actual process identity
    before shutdown. Code identity may differ during an explicitly requested
    upgrade. Unvalidated processes are never terminated.
    """
    deadline = time.monotonic() + timeout
    paths = [socket_path] if socket_path else list(dict.fromkeys([default_socket_path(), *legacy_socket_paths()]))
    result = {"ok": True, "absent": True}
    for path in paths:
        async with startup_lock(path, deadline):
            connection = await try_connect(path, timeout=.05)
            if connection is None and not endpoint_is_listening(path):
                continue
            while connection is None and time.monotonic() < deadline:
                try:
                    connection = await asyncio.wait_for(connect(path), min(.2, deadline - time.monotonic()))
                except (OSError, RuntimeError):
                    await asyncio.sleep(.025)
            if connection is None:
                raise TimeoutError("Worker shutdown could not acquire its detached IPC connection")
            try:
                hello = await asyncio.wait_for(connection.recv(), max(.001, deadline - time.monotonic()))
                result = await retire_worker(connection, hello, path, deadline=deadline, allow_legacy=True)
                result = {**result, "pid": hello["pid"]}
            finally:
                await connection.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Application-owner Runtime lifecycle control")
    parser.add_argument("--detach-front", type=int)
    parser.add_argument("--force", action="store_true", help="Force-stop the Front while preserving Worker subtrees")
    parser.add_argument("--shutdown-worker", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--shutdown-receipt", help="Require this desktop update attempt's successful shutdown")
    args = parser.parse_args()
    if args.shutdown_receipt is not None:
        from jiuwenswarm.common.update_shutdown import require_clean_shutdown

        require_clean_shutdown(args.shutdown_receipt)
    if args.detach_front:
        detach_front_process(args.detach_front, force=args.force, timeout=args.timeout)
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
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
