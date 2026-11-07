# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit application-owner shutdown after its Front has detached."""
from __future__ import annotations

import asyncio
import argparse
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil


def _pending_workers(paths: list[str]) -> dict[str, list[tuple[psutil.Process, float]]]:
    """Observe booting processes, without granting them lifecycle authority."""
    matches = {path: [] for path in paths}
    executables = {os.path.normcase(str(Path(sys.executable).resolve()))}
    if not getattr(sys, "frozen", False):
        executables.add(os.path.normcase(str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())))
    username = psutil.Process().username()
    for process in psutil.process_iter(["cmdline", "exe", "username", "create_time"]):
        try:
            info = process.info
            argv = info.get("cmdline") or []
            if argv[1:3] != ["-m", "jiuwenswarm.server.worker.main"] or "--socket" not in argv:
                continue
            if info.get("username") != username or not info.get("exe"):
                continue
            if os.path.normcase(str(Path(info["exe"]).resolve())) not in executables:
                continue
            socket = argv[argv.index("--socket") + 1]
            if socket in matches:
                matches[socket].append((process, process.create_time()))
        except (psutil.Error, OSError, IndexError):
            continue
    return matches


def _pending_worker_alive(workers: list[tuple[psutil.Process, float]]) -> bool:
    for process, created in workers:
        try:
            if process.is_running() and process.create_time() == created and process.status() != psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            return True  # Uninspectable is not proof of exit.
    return False


def detach_front_process(pid: int, *, force: bool = False, timeout: float = 5.0) -> None:
    """Stop an owned Front subtree; unknown survivors cannot mean success."""
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


async def shutdown_resident_worker(*, timeout: float = 30.0, socket_path: str | None = None,
                                   include_legacy: bool = False, legacy_worker_pid: int | None = None) -> dict:
    """Drain this deployment and observe exit; legacy Python ownership is explicit."""
    from jiuwenswarm.server.supervisor.resident_lifecycle import LegacyWorkerUnowned
    from jiuwenswarm.server.ipc.client import connect, endpoint_is_listening, try_connect
    from jiuwenswarm.server.supervisor.worker_process import default_socket_path, legacy_socket_paths
    from jiuwenswarm.server.supervisor.resident_lifecycle import retire_worker, startup_lock

    deadline = time.monotonic() + max(.001, timeout)
    paths = [socket_path] if socket_path else [default_socket_path()]
    if socket_path is None and (include_legacy or legacy_worker_pid is not None):
        paths.extend(legacy_socket_paths())
    pending = await asyncio.to_thread(_pending_workers, paths)
    result = {"ok": True, "absent": True}
    unknown = []
    for path in dict.fromkeys(paths):
        async with startup_lock(path, deadline):
            connection = await try_connect(path, timeout=.05)
            if connection is None and not endpoint_is_listening(path) and not _pending_worker_alive(pending[path]):
                continue
            while connection is None and time.monotonic() < deadline:
                try:
                    connection = await asyncio.wait_for(connect(path), min(.2, max(.001, deadline - time.monotonic())))
                except (OSError, RuntimeError):
                    if not endpoint_is_listening(path) and not _pending_worker_alive(pending[path]):
                        break
                    await asyncio.sleep(.025)
            if connection is None:
                if not endpoint_is_listening(path) and not _pending_worker_alive(pending[path]):
                    continue
                raise TimeoutError(f"Worker shutdown could not acquire detached IPC at {path}")
            try:
                hello = await asyncio.wait_for(connection.recv(), max(.001, deadline - time.monotonic()))
                if legacy_worker_pid is not None and hello.get("pid") != legacy_worker_pid:
                    continue
                historical_endpoint = path != paths[0] and legacy_worker_pid is None
                if historical_endpoint and isinstance(hello, dict) and hello.get("deployment_scope") is not None:
                    from jiuwenswarm.server.supervisor.identity import deployment_scope

                    if hello["deployment_scope"] != deployment_scope():
                        logging.getLogger(__name__).warning(
                            "Leaving historical Worker PID=%s at %s: it belongs to another deployment",
                            hello.get("pid"), path)
                        continue
                try:
                    outcome = await retire_worker(connection, hello, path, deadline=deadline,
                        allow_legacy=True, expected_pid=legacy_worker_pid)
                except LegacyWorkerUnowned as exc:
                    unknown.append(exc)
                    logging.getLogger(__name__).warning("%s", exc)
                    continue
                result = {**outcome, "pid": hello["pid"]}
            finally:
                await connection.close()
    if result.get("absent"):
        if legacy_worker_pid is not None:
            raise RuntimeError(f"Selected legacy Worker PID={legacy_worker_pid} was not found in this data directory")
        if unknown:
            raise unknown[0]
    return result


def shutdown_application_worker(*, timeout: float = 30.0, workspace: Path | None = None,
                                legacy_worker_pid: int | None = None) -> bool:
    """Complete owner shutdown without changing another instance's process environment."""
    try:
        if workspace is None:
            result = asyncio.run(shutdown_resident_worker(timeout=timeout, include_legacy=True,
                legacy_worker_pid=legacy_worker_pid))
        else:
            command = [sys.executable]
            if getattr(sys, "frozen", False):
                command.append("--desktop-run-worker-lifecycle")
            else:
                command.extend(["-P", "-m", "jiuwenswarm.server.supervisor.shutdown"])
            command.extend(["--shutdown-worker", "--workspace", str(workspace), "--timeout", str(timeout)])
            if legacy_worker_pid is not None:
                command.extend(["--legacy-worker-pid", str(legacy_worker_pid)])
            env = os.environ.copy()
            env["JIUWENSWARM_DATA_DIR"] = str(workspace)
            package_root = str(Path(__file__).resolve().parents[3])
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH", "")]))
            completed = subprocess.run(command, env=env, capture_output=True, text=True,
                timeout=max(.001, timeout) + 5, check=False, start_new_session=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Worker lifecycle helper failed (exit={completed.returncode}): {completed.stderr[-2000:]}"
                )
            result = json.loads(completed.stdout)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError(f"Worker shutdown incomplete: {result}")
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        logging.getLogger(__name__).error("Application Worker shutdown incomplete: %s", exc)
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Application-owner Runtime lifecycle control")
    parser.add_argument("--detach-front", type=int)
    parser.add_argument("--force", action="store_true", help="Force-stop the Front while preserving Worker subtrees")
    parser.add_argument("--shutdown-worker", action="store_true")
    parser.add_argument("--workspace", type=Path, help="Resolve the target instance in an isolated lifecycle process")
    parser.add_argument("--legacy-worker-pid", type=int, help="Explicitly select an unversioned Python Worker")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--shutdown-receipt", help="Require this desktop update attempt's successful shutdown")
    args = parser.parse_args()
    if args.workspace is not None:
        workspace = args.workspace.expanduser().resolve()
        os.environ["JIUWENSWARM_DATA_DIR"] = str(workspace)
        from jiuwenswarm.dotenv_early import load_dotenv_runtime

        bootstrap = workspace / ".env"
        if bootstrap.is_file():
            load_dotenv_runtime(bootstrap, override=True)
        # The selected instance owns the data root; do not let a stale .env
        # redirect this stop command into another instance's workspace.
        os.environ["JIUWENSWARM_DATA_DIR"] = str(workspace)
        from jiuwenswarm.common.utils import get_env_file

        load_dotenv_runtime(get_env_file(), override=True)
        os.environ["JIUWENSWARM_DATA_DIR"] = str(workspace)
    if args.shutdown_receipt is not None:
        from jiuwenswarm.common.update_shutdown import require_clean_shutdown

        require_clean_shutdown(args.shutdown_receipt)
    if args.detach_front:
        detach_front_process(args.detach_front, force=args.force, timeout=args.timeout)
    if args.shutdown_worker:
        if args.legacy_worker_pid is None:
            result = asyncio.run(shutdown_resident_worker(timeout=max(.001, args.timeout), include_legacy=True))
        else:
            result = asyncio.run(shutdown_resident_worker(timeout=max(.001, args.timeout),
                legacy_worker_pid=args.legacy_worker_pid))
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
