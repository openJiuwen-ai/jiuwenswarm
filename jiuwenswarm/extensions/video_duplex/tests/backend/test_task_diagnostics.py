"""Diagnostics preserve correlation without leaking complete task results."""

import json
import logging
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.utils import append_jsonl_log
from jiuwenswarm.extensions.video_duplex.backend.tasks import diagnostics


def test_task_trace_filters_success_details_but_keeps_failures(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(diagnostics, "get_user_workspace_dir", lambda: tmp_path)
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        diagnostics.trace_task("checkpoint_acknowledged", request_id="checkpoint-1")
        diagnostics.trace_task("task_push_sent", task_id="task-1")
        diagnostics.trace_task("cancel_rpc_response", probe=True, ok=True,
                               payload={"managed_task_status": "running"})
        diagnostics.trace_task("cancel_rpc_response", probe=True, ok=False, error="unreachable")
        diagnostics.trace_task("cancel_rpc_response", probe=True, ok=True,
                               payload={"managed_task_status": "running", "success": False})
        diagnostics.trace_task("checkpoint_failed", error="rejected")
        diagnostics.trace_task("cancel_rpc_sent", probe=False)
    with caplog.at_level(logging.DEBUG, logger=diagnostics.__name__):
        diagnostics.trace_task("checkpoint_acknowledged", request_id="checkpoint-2")
    records = [json.loads(line) for line in (tmp_path / "logs/duplex-task-trace.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [record["event"] for record in records] == [
        "cancel_rpc_response", "cancel_rpc_response", "checkpoint_failed",
        "cancel_rpc_sent", "checkpoint_acknowledged",
    ]
    assert records[0]["error"] == "unreachable"
    assert records[1]["payload"]["success"] is False
    assert records[-1]["request_id"] == "checkpoint-2"


def test_jsonl_rotation_bounds_backups_and_preserves_unicode(tmp_path):
    path = tmp_path / "diagnostics.jsonl"
    for index in range(6):
        append_jsonl_log(path, {"index": index, "text": "测试" * 10}, max_bytes=100, backup_count=2)

    files = [path.with_name(path.name + suffix) for suffix in (".2", ".1", "")]
    assert sorted(item.name for item in tmp_path.glob("*.jsonl.*") if item.suffix != ".lock") == [
        "diagnostics.jsonl.1", "diagnostics.jsonl.2",
    ]
    records = [json.loads(line) for file in files for line in file.read_text(encoding="utf-8").splitlines()]
    assert [record["index"] for record in records] == [3, 4, 5]
    assert all(record["text"] == "测试" * 10 for record in records)
    assert all(file.stat().st_size <= 100 for file in files)


def test_jsonl_rotation_retains_existing_oversized_log(tmp_path):
    path = tmp_path / "existing.jsonl"
    original = json.dumps({"text": "old" * 100}) + "\n"
    path.write_text(original, encoding="utf-8")
    append_jsonl_log(path, {"text": "new"}, max_bytes=100, backup_count=2)
    assert path.with_name(path.name + ".1").read_text(encoding="utf-8") == original
    assert json.loads(path.read_text(encoding="utf-8")) == {"text": "new"}


def test_jsonl_rotation_is_serialized_across_processes(tmp_path):
    path = tmp_path / "shared.jsonl"
    script = (
        "from pathlib import Path; import sys; "
        "from jiuwenswarm.common.utils import append_jsonl_log; "
        "[append_jsonl_log(Path(sys.argv[1]), {'writer': sys.argv[2], 'index': i}, "
        "max_bytes=200, backup_count=20) for i in range(20)]"
    )
    processes = [subprocess.Popen(
        [sys.executable, "-c", script, str(path), str(writer)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    ) for writer in range(2)]
    try:
        for process in processes:
            _, stderr = process.communicate(timeout=45)
            assert process.returncode == 0, stderr.decode(errors="replace")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
    records = [json.loads(line) for file in tmp_path.glob("shared.jsonl*")
               if file.suffix != ".lock" for line in file.read_text(encoding="utf-8").splitlines()]
    assert sorted((record["writer"], record["index"]) for record in records) == [
        (str(writer), index) for writer in range(2) for index in range(20)
    ]


def test_video_log_disk_failure_does_not_interrupt_request(tmp_path, monkeypatch):
    from jiuwenswarm.extensions.video_duplex.backend import video_live

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(video_live, "append_jsonl_log", fail)
    video_live._append_jsonl(tmp_path / "diagnostics.jsonl", {"event": "test"})  # pylint: disable=protected-access


def test_task_trace_writes_correlated_record_without_full_results(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "get_user_workspace_dir", lambda: tmp_path / ".jiuwenswarm")
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
    monkeypatch.setattr(diagnostics, "get_user_workspace_dir", lambda: tmp_path / ".jiuwenswarm")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(diagnostics, "append_jsonl_log", fail)
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

    class DiagnosticManager(task_adapter.VideoSearchManager):
        async def respond(self, ws, req_id, callback, trace):
            await self._respond(ws, req_id, callback, trace=trace)

    manager = DiagnosticManager(
        SimpleNamespace(send_response=send_response), None,
        log_event=lambda *args: None, qwen_active=lambda: True,
    )
    await manager.respond(
        object(), "rpc-1", action,
        trace={"call_id": "call-1", "source": "qwen"},
    )
    assert [record[0] for record in records] == [
        "task_rpc_received", "task_rpc_rejected" if rejected else "task_rpc_completed",
    ]
    assert all(record[1]["rpc_request_id"] == "rpc-1" and record[1]["call_id"] == "call-1"
               for record in records)
    assert replies[0]["ok"] is not rejected
    assert "large output" not in json.dumps(records)
