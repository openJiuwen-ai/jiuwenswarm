"""Diagnostics preserve correlation without leaking complete task results."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.video_duplex.backend.tasks import diagnostics


def test_task_trace_writes_correlated_record_without_full_results(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    receipt = diagnostics.receipt_state({
        "call_id": "call-1",
        "tool_result": {"state": "accepted", "job_id": "job-1", "result": "long code"},
        "jobs": [{"job_id": "job-1", "status": "cancelling", "result": "secret result"}],
        "frame_data_url": "data:image/jpeg;base64,private",
    })
    diagnostics.trace_task("task_rpc_completed", rpc_request_id="rpc-1", receipt=receipt)
    record = json.loads((tmp_path / ".jiuwenswarm/logs/duplex-task-trace.jsonl").read_text(encoding="utf-8"))
    assert record["rpc_request_id"] == "rpc-1"
    assert record["receipt"]["call_id"] == "call-1"
    assert record["receipt"]["tool_result"] == {"state": "accepted", "job_id": "job-1"}
    assert record["receipt"]["jobs"][0]["status"] == "cancelling"
    assert "long code" not in json.dumps(record)
    assert "secret result" not in json.dumps(record)
    assert "base64" not in json.dumps(record)
    assert record["server_time"] and record["pid"]


def test_task_trace_disk_failure_does_not_interrupt_control(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", fail)
    diagnostics.trace_task("cancel_rpc_sent", request_id="execution-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True])
async def test_task_rpc_trace_distinguishes_completion_and_rejection(monkeypatch, rejected):
    from jiuwenswarm.extensions.video_duplex.backend import task_adapter

    records, replies = [], []
    monkeypatch.setattr(task_adapter, "trace_task", lambda event, **fields: records.append((event, fields)))

    async def send_response(ws, req_id, **fields):
        replies.append(fields)

    async def action():
        if rejected:
            raise ValueError("stale revision")
        return {"tool_result": {"state": "accepted", "job_id": "job-1", "result": "large output"}}

    manager = SimpleNamespace(channel=SimpleNamespace(send_response=send_response))
    await task_adapter.VideoSearchManager._respond(
        manager, object(), "rpc-1", action,
        trace={"call_id": "call-1", "source": "qwen"},
    )
    assert [record[0] for record in records] == [
        "task_rpc_received", "task_rpc_rejected" if rejected else "task_rpc_completed",
    ]
    assert all(record[1]["rpc_request_id"] == "rpc-1" and record[1]["call_id"] == "call-1"
               for record in records)
    assert replies[0]["ok"] is not rejected
    assert "large output" not in json.dumps(records)
