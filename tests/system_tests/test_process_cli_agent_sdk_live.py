# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Opt-in real SDK -> child CLI -> Runtime -> model acceptance."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_custom_agent_resume_binding_and_busy(tmp_path: Path, request) -> None:
    if os.getenv("JIUWENSWARM_REAL_MODEL_TEST") != "1":
        pytest.skip("set JIUWENSWARM_REAL_MODEL_TEST=1 for local model acceptance")
    source = Path.home() / ".jiuwenswarm" / "config"
    if not (source / "config.yaml").is_file() or not (source / ".env").is_file():
        pytest.skip("real local model configuration is unavailable")

    config = tmp_path / "config"
    config.mkdir()
    for name in ("config.yaml", ".env"):
        shutil.copyfile(source / name, config / name)
    request.addfinalizer(lambda: shutil.rmtree(config, ignore_errors=True))

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[2] / "sdks" / "python" / "src")
    )
    from jiuwenswarm_sdk import Client

    env = {"JIUWENSWARM_DATA_DIR": str(tmp_path)}
    client = Client(
        [sys.executable, "-m", "jiuwenswarm.channels.process_cli.main"],
        cwd=str(root),
        env=env,
    )
    agent = {
        "name": "live_resume_agent",
        "instructions": "Answer the user's question concisely.",
        "tools": ["*"],
    }
    first = await client.run(
        {"input": "Say hello in one sentence.", "agent": agent, "timeout_seconds": 120},
        deadline_seconds=180,
    )
    assert first["status"] == "completed", first
    assert first["output"], first
    session_id = first["session_id"]

    # An independent process holds the same Session's OS lock while the SDK
    # starts another real CLI child for that Session.
    holder_code = (
        "import sys; "
        "from jiuwenswarm.channels.process_cli.session_guard import SessionLease; "
        "lease = SessionLease(sys.argv[1]); lease.acquire(); "
        "print('LOCKED', flush=True); sys.stdin.readline(); lease.release()"
    )
    holder = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        holder_code,
        session_id,
        cwd=str(root),
        env={**os.environ, **env},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        assert (await asyncio.wait_for(holder.stdout.readline(), 30)).strip() == b"LOCKED"
        busy = await client.run(
            {
                "input": "This concurrent call must be rejected.",
                "agent": agent,
                "session_id": session_id,
                "timeout_seconds": 30,
            },
            deadline_seconds=60,
        )
        assert busy["error"]["code"] == "SESSION_BUSY", busy
        assert busy["error"]["retryable"] is True
    finally:
        assert holder.stdin is not None
        holder.stdin.write(b"\n")
        await holder.stdin.drain()
        holder.stdin.close()
        await asyncio.wait_for(holder.wait(), 30)

    resumed = await client.run(
        {
            "input": "Say hello again in one sentence.",
            "agent": agent,
            "session_id": session_id,
            "timeout_seconds": 120,
        },
        deadline_seconds=180,
    )
    assert resumed["status"] == "completed", resumed
    assert resumed["session_id"] == session_id
    assert resumed["output"], resumed

    changed = {**agent, "instructions": "A different root Agent definition."}
    conflict = await client.run(
        {
            "input": "This definition must be rejected.",
            "agent": changed,
            "session_id": session_id,
            "timeout_seconds": 30,
        },
        deadline_seconds=60,
    )
    assert conflict["error"]["code"] == "AGENT_DEFINITION_SESSION_CONFLICT", conflict
