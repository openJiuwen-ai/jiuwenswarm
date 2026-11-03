# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Opt-in real SDK -> child CLI -> Runtime -> model acceptance."""

from __future__ import annotations

import asyncio
import json
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


@pytest.mark.asyncio
async def test_run_scoped_capabilities_and_custom_work_agent_live(tmp_path: Path) -> None:
    """Exercise the Python SDK, child CLI, Runtime, and configured model."""
    if os.getenv("JIUWENSWARM_REAL_MODEL_TEST") != "1":
        pytest.skip("set JIUWENSWARM_REAL_MODEL_TEST=1 for local model acceptance")
    source = Path.home() / ".jiuwenswarm" / "config"
    if not (source / "config.yaml").is_file() or not (source / ".env").is_file():
        pytest.skip("real local model configuration is unavailable")
    isolated_config = tmp_path / "config"
    isolated_config.mkdir()
    for name in ("config.yaml", ".env"):
        shutil.copyfile(source / name, isolated_config / name)

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdks" / "python" / "src"))
    from jiuwenswarm_sdk import Client

    client = Client(
        [sys.executable, "-m", "jiuwenswarm.channels.process_cli.main"],
        cwd=str(root), env={"JIUWENSWARM_DATA_DIR": str(tmp_path)},
    )
    catalog = await client.query("model.list", deadline_seconds=60)
    assert catalog["status"] == "completed", catalog
    model = catalog["data"]["models"][0]["selection_key"]
    run = await client.run({
        "input": "Reply with exactly: live capability check",
        "mode": "agent.work.normal",
        "model": model,
        "skills": [],
        "mcp": [],
        "permissions": {"tools": {"read_file": "allow", "write_file": "ask", "run_shell": "deny"}},
        "agent": {
            "name": "live_work_agent",
            "instructions": "Answer briefly without using tools.",
            "tools": ["read_file"],
        },
        "timeout_seconds": 120,
    }, deadline_seconds=180)
    assert run["status"] == "completed", run
    assert run["output"], run

    direct_request = {
        "schema_version": "0.1", "type": "run", "request_id": "direct-noninteractive",
        "input": "Reply with exactly: direct noninteractive check",
        "mode": "agent.work.normal", "model": model, "skills": [], "mcp": [],
        "agent": {
            "name": "live_direct_agent",
            "instructions": "Answer briefly without using tools.",
            "tools": ["read_file"],
        },
        "timeout_seconds": 120,
    }
    direct = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "jiuwenswarm.channels.process_cli.main",
        "--run-json", "-", cwd=str(root),
        env={**os.environ, "JIUWENSWARM_DATA_DIR": str(tmp_path)},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        direct.communicate(json.dumps(direct_request).encode()), 180
    )
    assert direct.returncode == 0, stderr.decode(errors="replace")[-2000:]
    direct_result = json.loads(stdout.splitlines()[-1])
    assert direct_result["status"] == "completed", direct_result
    assert direct_result["output"], direct_result


@pytest.mark.asyncio
async def test_run_scoped_deny_keeps_file_absent_in_real_code_run(tmp_path: Path) -> None:
    if os.getenv("JIUWENSWARM_REAL_MODEL_TEST") != "1":
        pytest.skip("set JIUWENSWARM_REAL_MODEL_TEST=1 for local model acceptance")
    source = Path.home() / ".jiuwenswarm" / "config"
    if not (source / "config.yaml").is_file() or not (source / ".env").is_file():
        pytest.skip("real local model configuration is unavailable")
    isolated_config = tmp_path / "config"
    isolated_config.mkdir()
    for name in ("config.yaml", ".env"):
        shutil.copyfile(source / name, isolated_config / name)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "must_not_exist.txt"
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdks" / "python" / "src"))
    from jiuwenswarm_sdk import Client

    client = Client(
        [sys.executable, "-m", "jiuwenswarm.channels.process_cli.main"],
        cwd=str(root), env={"JIUWENSWARM_DATA_DIR": str(tmp_path)},
    )
    events = []
    async def record_event(event):
        events.append(event)

    async def reject_interaction(event):
        raise AssertionError(f"unexpected interaction: {event.get('payload')}")

    run = await client.run({
        "input": f"Use write_file to create {target} with content blocked. Do not use another tool.",
        "mode": "agent.code.normal",
        "workspace": {"cwd": str(workspace), "project_dir": str(workspace)},
        "permissions": {"tools": {"write_file": "deny"}},
        "agent": {
            "name": "live_denied_agent",
            "instructions": "Try the file tool once. If denied, report the denial and stop.",
            "tools": ["write_file"],
            "max_iterations": 3,
        },
        "timeout_seconds": 90,
    }, on_event=record_event, on_interaction=reject_interaction, deadline_seconds=120)
    assert not target.exists(), run
    assert run["status"] == "completed", run
    assert events, run
