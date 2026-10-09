# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Opt-in real model and third-party SDK acceptance of public revision 1."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

import pytest
from jsonschema import Draft202012Validator


@pytest.fixture
def live_host(tmp_path):
    if os.getenv("JIUWENSWARM_REAL_MODEL_TEST") != "1":
        pytest.skip("set JIUWENSWARM_REAL_MODEL_TEST=1")
    source = Path.home() / ".jiuwenswarm/config"
    if not all((source / name).is_file() for name in ("config.yaml", ".env")):
        pytest.skip("local model configuration unavailable")
    config = tmp_path / "config"
    config.mkdir()
    for name in ("config.yaml", ".env"):
        shutil.copyfile(source / name, config / name)
    repo = Path(__file__).resolve().parents[2]
    sdk = repo / "sdks/python/src"
    sys.path.insert(0, str(sdk))
    from jiuwenswarm_sdk import Client

    env = {
        **os.environ,
        "JIUWENSWARM_DATA_DIR": str(tmp_path),
        "PYTHONPATH": str(sdk) + os.pathsep + str(repo),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    env.pop("JIUWENSWARM_CONFIG_DIR", None)
    command = [
        sys.executable,
        str(Path(__file__).with_name("process_cli_wire_proxy.py")),
        str(tmp_path / "wire"),
        sys.executable,
        "-m",
        "jiuwenswarm.channels.process_cli.main",
    ]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host = {
        "root": tmp_path,
        "workspace": workspace,
        "command": command,
        "env": env,
        "repo": repo,
        "client": Client(command, cwd=str(repo), env=env),
    }
    try:
        yield host
    finally:
        assert config.resolve().parent == tmp_path.resolve()
        for name in ("config.yaml", ".env"):
            (config / name).unlink(missing_ok=True)
        sys.path.remove(str(sdk))


async def discover(host):
    client = host["client"]
    capabilities = await client.query("protocol.capabilities", deadline_seconds=30)
    published = await client.query("protocol.schema", deadline_seconds=30)
    models = await client.query("model.list", deadline_seconds=90)
    assert (
        capabilities["status"] == published["status"] == models["status"] == "completed"
    )
    assert capabilities["data"]["protocol_revision"] == 1
    assert all(
        capabilities["data"]["features"][name]
        for name in ("empty_tools", "field_errors", "stable_event_fields")
    )
    validator = Draft202012Validator(published["data"])
    for record in (capabilities, published, models):
        validator.validate(record)
    return validator, models["data"]["models"][0]["selection_key"]


def check_wire(host, validator):
    from jiuwenswarm_sdk.protocol import Records

    evidence = []
    for folder in (host["root"] / "wire").iterdir():
        inputs = [
            json.loads(line)
            for line in (folder / "stdin.jsonl").read_bytes().splitlines()
        ]
        first = inputs[0]
        for record in inputs:
            validator.validate(record)
        reader = Records(
            first["request_id"],
            query=first["type"] == "query",
            operation=first.get("operation"),
        )
        outputs = []
        for line in (folder / "stdout.jsonl").read_bytes().splitlines(keepends=True):
            record = reader.accept(line)
            validator.validate(record)
            outputs.append(record)
        code = json.loads((folder / "exit.json").read_text())["exit_code"]
        reader.finish(code)
        evidence.append(
            {
                "request_id": first["request_id"],
                "records": len(outputs),
                "exit_code": code,
                "controls": [record["type"] for record in inputs[1:]],
            }
        )
    (host["root"] / "wire-validation.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )
    return evidence


@pytest.mark.asyncio
async def test_all_public_query_data_matches_discovered_schema(live_host):
    host = live_host
    validator, model = await discover(host)
    for operation, params in (
        ("model.resolve", {"requested": model}),
        ("mode.list", {}),
        ("mode.resolve", {"requested": "agent.work.normal"}),
        ("permission.get", {}),
        ("mcp.validate", {"references": []}),
        ("session.list", {"limit": 10, "offset": 0}),
        ("session.get", {"session_id": "missing-protocol-revision-session"}),
    ):
        result = await host["client"].query(operation, params, deadline_seconds=90)
        assert result["status"] == "completed", result
        validator.validate(result)
    assert len(check_wire(host, validator)) == 10


@pytest.mark.asyncio
async def test_discovery_field_errors_and_raw_empty_tools_real_model(live_host):
    host = live_host
    validator, model = await discover(host)
    bad = await host["client"].run(
        {"input": "Must not reach model", "max_turns": "bad"}, deadline_seconds=30
    )
    assert bad["exit_code"] == 2 and bad["error"]["details"]["field"] == "/max_turns", (
        bad
    )
    validator.validate(bad)
    target = host["workspace"] / "must_not_write.txt"
    request = {
        "schema_version": "0.1",
        "type": "run",
        "request_id": "raw-empty-tools",
        "input": f"Use write_file to create {target}. If no tools are available, return tool_available=false using the requested schema.",
        "mode": "agent.code.normal",
        "model": model,
        "skills": None,
        "mcp": [],
        "agent": {
            "name": "empty_tools_live",
            "instructions": "Report whether a tool is available. Do not claim a file was written without a tool.",
            "tools": [],
        },
        "workspace": {
            "cwd": str(host["workspace"]),
            "project_dir": str(host["workspace"]),
        },
        "output_schema": {
            "type": "object",
            "properties": {"tool_available": {"type": "boolean"}},
            "required": ["tool_available"],
            "additionalProperties": False,
        },
        "max_turns": 2,
        "timeout_seconds": 150,
    }
    validator.validate(request)
    child = await asyncio.create_subprocess_exec(
        *host["command"],
        "--run-json",
        "-",
        cwd=host["repo"],
        env=host["env"],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        child.communicate(json.dumps(request).encode()), 210
    )
    assert child.returncode == 0, (
        stderr.decode(errors="replace")[-2000:]
        + stdout.decode(errors="replace")[-2000:]
    )
    records = [json.loads(line) for line in stdout.splitlines()]
    result = records[-1]
    assert result["output_json"] == {"tool_available": False}, result
    assert not target.exists()
    assert not any(
        record.get("event_type") in ("chat.tool_call", "host_tool.requested")
        for record in records
    )
    for record in records:
        validator.validate(record)
    # Invalid requests are intentionally outside the input schema.
    for folder in (host["root"] / "wire").iterdir():
        first = json.loads((folder / "stdin.jsonl").read_bytes().splitlines()[0])
        if first.get("max_turns") == "bad":
            continue
        for line in (folder / "stdout.jsonl").read_bytes().splitlines():
            validator.validate(json.loads(line))
    (host["root"] / "empty-tools.result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_public_approval_host_tool_business_and_resume_real_model(live_host):
    host = live_host
    validator, model = await discover(host)
    client = host["client"]
    marker = "REVISION1_" + uuid.uuid4().hex
    target = host["workspace"] / "report.txt"
    calls, approvals, events = [], [], []

    async def observe(event):
        validator.validate(event)
        events.append(event)

    async def lookup(event):
        payload = event["payload"]
        calls.append(payload)
        assert (
            payload["name"] == "host_lookup" and payload["arguments"]["key"] == "sample"
        )
        return {"verification_code": marker}

    async def approve(event):
        payload = event["payload"]
        assert payload["kind"] == "permission"
        approvals.append(payload)
        answers = []
        for question in payload["questions"]:
            assert any(
                option["value"] == "allow_once" for option in question["options"]
            )
            answer = {
                "question": question["question"],
                "selected_options": ["allow_once"],
                "custom_input": "",
            }
            if question["card_id"] is not None:
                answer["card_id"] = question["card_id"]
            answers.append(answer)
        return answers

    agent = {
        "name": "revision_host_agent",
        "instructions": "Use the supplied tools for operations that require them. Respect permissions. Preserve host data exactly. When an output JSON schema is requested, your entire final response must be one JSON object. Do not include any explanation, introduction, Markdown, or other text in the final response.",
        "tools": ["host_lookup", "write_file"],
    }
    request = {
        "input": f"First call host_lookup with key=sample. Then use write_file to write its verification_code, and only that code, into {target}. After writing, return verification_code and saved=true using the requested JSON schema.",
        "mode": "agent.code.normal",
        "model": model,
        "skills": [],
        "mcp": [],
        "agent": agent,
        "workspace": {
            "cwd": str(host["workspace"]),
            "project_dir": str(host["workspace"]),
        },
        "permissions": {"tools": {"write_file": "ask"}},
        "host_tools": [
            {
                "name": "host_lookup",
                "description": "Return authoritative data from the third-party host.",
                "input_schema": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            }
        ],
        "output_schema": {
            "type": "object",
            "properties": {
                "verification_code": {"type": "string"},
                "saved": {"type": "boolean"},
            },
            "required": ["verification_code", "saved"],
            "additionalProperties": False,
        },
        "max_turns": 6,
        "timeout_seconds": 150,
    }
    result = await client.run(
        request,
        on_event=observe,
        on_interaction=approve,
        on_tool_call=lookup,
        deadline_seconds=210,
    )
    assert result["status"] == "completed", result
    assert result["output_json"] == {"verification_code": marker, "saved": True}, result
    assert target.read_text(encoding="utf-8").strip() == marker
    assert calls and approvals
    assert any(
        event["event_type"] == "chat.tool_call"
        and event["payload"]["tool"]["name"] == "write_file"
        for event in events
    )
    validator.validate(result)
    (host["root"] / "business.result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    target.unlink()
    resumed_events = []

    async def resumed_observe(event):
        validator.validate(event)
        resumed_events.append(event)

    resumed = await client.run(
        {
            "input": "Recall the verification_code from our previous turn. Do not call tools or read files. Return it in a single sentence.",
            "session_id": result["session_id"],
            "agent": agent,
            "model": model,
            "timeout_seconds": 150,
        },
        on_event=resumed_observe,
        deadline_seconds=210,
    )
    assert resumed["status"] == "completed" and marker in resumed["output"], resumed
    assert resumed["session_id"] == result["session_id"]
    assert not any(event["event_type"] == "chat.tool_call" for event in resumed_events)
    validator.validate(resumed)
    session = await client.query(
        "session.get", {"session_id": result["session_id"]}, deadline_seconds=90
    )
    validator.validate(session)
    assert session["data"]["session"]["session_id"] == result["session_id"]
    evidence = check_wire(host, validator)
    assert any(
        "answer" in entry["controls"] and "tool_result" in entry["controls"]
        for entry in evidence
    )
