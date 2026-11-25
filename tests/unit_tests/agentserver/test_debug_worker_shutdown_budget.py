# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Use the real debug stop CLI and Worker lifecycle with controlled persistence."""

import asyncio
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

from jiuwenswarm import debug_launcher
from jiuwenswarm.server.ipc.client import connect_with_retry

SOURCE = Path(__file__).resolve().parents[3]
test_logger = logging.getLogger("test_logger.debug_shutdown_budget")

SHUTDOWN_SHIM = """
import asyncio
import os
from pathlib import Path
import sys
from typing import Any
from jiuwenswarm import debug_launcher

root = Path(os.environ['JW_SHUTDOWN_TEST_ROOT'])
debug_launcher.REPO_ROOT = root

if 'jiuwenswarm.server.worker.main' in sys.orig_argv:
    from jiuwenswarm.server.worker import service

    class ControlledRuntime:
        async def shutdown(self) -> None:
            (root / 'saving').write_text('started', encoding='utf-8')
            await asyncio.sleep(float(os.environ['JW_SHUTDOWN_TEST_DELAY']))
            (root / 'saved').write_text('completed', encoding='utf-8')

    async def load_runtime(
        host: str,
        port: int,
        signals: Any,
        dispatcher: Any,
        loaded: dict[str, Any],
    ) -> ControlledRuntime:
        runtime = ControlledRuntime()
        loaded['runtime'] = runtime
        signals.agent_ready()
        return runtime

    service._load_runtime = load_runtime
"""


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delay", "options", "successful"),
    [(18.0, [], True), (5.0, ["--worker-timeout", "2"], False)],
)
async def test_debug_stop_observes_persistence_and_actual_worker_exit(
    monkeypatch: pytest.MonkeyPatch,
    delay: float,
    options: list[str],
    successful: bool,
) -> None:
    """Default cleanup can exceed 15 seconds; an explicit short budget still fails."""
    with tempfile.TemporaryDirectory(prefix="jwdbg-") as directory:
        root = Path(directory)
        shim = root / "shim"
        shim.mkdir()
        (shim / "sitecustomize.py").write_text(textwrap.dedent(SHUTDOWN_SHIM), encoding="utf-8")
        socket_path = str(root / "worker.sock")
        env = os.environ.copy()
        env.update({
            "JW_SHUTDOWN_TEST_ROOT": str(root),
            "JW_SHUTDOWN_TEST_DELAY": str(delay),
            "JIUWENSWARM_DATA_DIR": str(root / "data"),
            "JIUWENSWARM_CONFIG_DIR": str(root / "data/config"),
            "JIUWENSWARM_IDENTITY_CACHE_DIR": str(root / "identities"),
            "JIUWENSWARM_RUNTIME_SOCKET": socket_path,
            "PYTHONPATH": os.pathsep.join([str(shim), str(SOURCE)]),
        })
        monkeypatch.setattr(debug_launcher, "REPO_ROOT", root)
        debug_launcher.write_debug_state(debug_launcher.DebugState(
            pid=1073741824,
            started_at=0,
            log_file="",
            command=[],
            cwd=str(SOURCE),
        ))
        with (root / "worker.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "jiuwenswarm.server.worker.main", "--socket", socket_path, "--port", "0"],
                cwd=SOURCE,
                env=env,
                stdout=log,
                stderr=log,
            )
            connection = None
            try:
                connection = await connect_with_retry(socket_path, timeout=15)
                hello = await asyncio.wait_for(connection.recv(), 10)
                assert hello["pid"] == process.pid and hello["deployment_scope"]
                while True:
                    frame = await asyncio.wait_for(connection.recv(), 10)
                    if frame.get("state") == "AGENT_READY":
                        break
                await connection.close()
                connection = None
                started = time.monotonic()
                stop_command = Path(sys.executable).parent / (
                    "jiuwenswarm-stop.exe" if os.name == "nt" else "jiuwenswarm-stop"
                )
                stopped = await asyncio.to_thread(
                    subprocess.run,
                    [str(stop_command), *options],
                    cwd=SOURCE,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=45,
                )
                exit_code = await asyncio.to_thread(process.wait, 5)
                test_logger.info("delay=%ss options=%s elapsed=%.2fs CLI=%s Worker=%s",
                                 delay, options, time.monotonic() - started, stopped.returncode, exit_code)
                assert (root / "saving").exists()
                assert (root / "saved").exists() is successful
                assert (stopped.returncode == 0) is successful, stopped.stdout + stopped.stderr
                assert (exit_code == 0) is successful
                assert (debug_launcher.read_debug_state() is None) is successful
                if not successful:
                    assert "forced to exit" in stopped.stdout + stopped.stderr
            finally:
                if connection is not None:
                    await connection.close()
                if process.poll() is None:
                    process.kill()
                await asyncio.to_thread(process.wait, 5)
