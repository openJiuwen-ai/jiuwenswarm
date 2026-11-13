# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared process/websocket helpers for tests/system_tests/.

Extracted verbatim from the duplicated helper blocks in
test_btw_command_st.py, test_cli_channel_ws.py and
test_team_continuous_dialog.py. The wait helpers default to 30s timeouts
(the value shared by test_btw_command_st.py and test_cli_channel_ws.py);
test_team_continuous_dialog.py passes its longer timeouts explicitly at
its call sites.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pick_free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _start_process(cmd: list[str], *, env: dict[str, str], log_path: Path) -> subprocess.Popen:
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )
    log_file.close()
    return proc


def _stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        return

    proc.terminate()
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


async def _wait_for_log(log_path: Path, needle: str, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            if needle in text:
                return
        await asyncio.sleep(0.2)
    log_text = (
        log_path.read_text(encoding="utf-8", errors="ignore")
        if log_path.exists()
        else ""
    )
    raise AssertionError(
        f"Timed out waiting for log line: {needle}\nlog={log_text}"
    )


async def _wait_for_websocket_ready(url: str, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            async with websockets.connect(url):
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await asyncio.sleep(0.2)
    raise AssertionError(
        f"Timed out waiting for websocket: {url} last_error={last_error}"
    )


async def _recv_until_response(ws, req_id: str, timeout: float = 10.0) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        remaining = max(0.1, deadline - asyncio.get_running_loop().time())
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if frame.get("type") == "res" and frame.get("id") == req_id:
            return frame
    raise AssertionError(f"Timed out waiting for response id={req_id}")
