# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jiuwenswarm_sdk import Client, InteractionRequired, ProtocolError, TransportError
from jiuwenswarm_sdk.protocol import Records, encode

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixture_child.py"


def client(mode="success", **kwargs):
    return Client(
        [sys.executable, str(FIXTURE)],
        env={"SDK_FIXTURE": mode},
        shutdown_grace_seconds=1,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "split_utf8", "stderr"])
async def test_real_pipes(mode):
    result = await client(mode).run({"input": "test"}, deadline_seconds=10)
    assert result["exit_code"] == 0
    assert result["output"] == "hello 中文 😀"


@pytest.mark.asyncio
async def test_distinct_query_processes():
    sdk = client()
    first = await sdk.query("mode.list")
    second = await sdk.query("model.list")
    assert first["data"]["pid"] != second["data"]["pid"]
    assert first["session_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "bad_id",
        "bad_version",
        "bad_sequence",
        "partial",
        "bad_utf8",
        "empty",
        "extra",
        "wrong_exit",
        "duplicate",
        "overflow_number",
    ],
)
async def test_reject_protocol_or_process_disagreement(mode):
    with pytest.raises(ProtocolError):
        await client(mode).run({"input": "test"}, deadline_seconds=10)


@pytest.mark.parametrize(
    ("line", "cause"),
    [
        (b"\xff\n", UnicodeDecodeError),
        (b"{\n", json.JSONDecodeError),
        (b"[" * 2000 + b"\n", RecursionError),
    ],
)
def test_invalid_json_retains_protocol_error_and_cause(line, cause):
    with pytest.raises(ProtocolError, match="invalid UTF-8 JSON record") as error:
        Records("request").accept(line)
    assert isinstance(error.value.__cause__, cause)


@pytest.mark.asyncio
async def test_output_limit():
    with pytest.raises(ProtocolError):
        await client("oversize", max_record_bytes=4096).run(
            {"input": "test"}, deadline_seconds=10
        )


@pytest.mark.asyncio
async def test_error_result_not_retried():
    result = await client("error").run({"input": "test"})
    assert result["status"] == "failed"
    assert result["exit_code"] == 1


@pytest.mark.asyncio
async def test_two_answers_same_process():
    seen = []

    async def answer(event):
        seen.append(event["payload"]["interaction_id"])
        return [{"question": "answer", "selected_options": ["yes"], "custom_input": ""}]

    result = await client("two_questions").run(
        {"input": "test"}, on_interaction=answer, deadline_seconds=10
    )
    assert result["exit_code"] == 0
    assert seen == ["opaque-0", "opaque-1"]


@pytest.mark.asyncio
async def test_no_automatic_approval():
    with pytest.raises(InteractionRequired):
        await client("ask").run({"input": "test"}, deadline_seconds=10)


@pytest.mark.asyncio
async def test_cancel_before_answer_collects_real_child(tmp_path):
    cancel = asyncio.Event()

    async def observe(_):
        cancel.set()
        await asyncio.sleep(0.02)

    sdk = Client(
        [sys.executable, str(FIXTURE)],
        env={"SDK_FIXTURE": "wait", "SDK_MARKER": str(tmp_path / "closed")},
    )
    result = await sdk.run(
        {"input": "test"}, cancel=cancel, on_event=observe, deadline_seconds=10
    )
    assert result["exit_code"] == 130
    assert (tmp_path / "closed").exists()


@pytest.mark.asyncio
async def test_callback_failure_cancels_before_return(tmp_path):
    async def fail(_):
        raise ValueError("host callback")

    marker = tmp_path / "closed"
    sdk = Client(
        [sys.executable, str(FIXTURE)],
        env={"SDK_FIXTURE": "ask", "SDK_MARKER": str(marker)},
    )
    with pytest.raises(ValueError, match="host callback"):
        await sdk.run({"input": "test"}, on_interaction=fail, deadline_seconds=10)
    assert marker.exists()


@pytest.mark.asyncio
async def test_host_deadline_interrupts_callback_and_cleans(tmp_path):
    async def wait(_):
        await asyncio.sleep(10)

    marker = tmp_path / "closed"
    sdk = Client(
        [sys.executable, str(FIXTURE)],
        env={"SDK_FIXTURE": "wait", "SDK_MARKER": str(marker)},
    )
    with pytest.raises(TimeoutError):
        await sdk.run({"input": "test"}, on_interaction=wait, deadline_seconds=0.5)
    assert marker.exists()


@pytest.mark.asyncio
async def test_result_is_not_returned_before_child_exit(tmp_path):
    marker = tmp_path / "closed"
    sdk = Client(
        [sys.executable, str(FIXTURE)],
        env={"SDK_FIXTURE": "delayed_exit", "SDK_MARKER": str(marker)},
    )
    assert (await sdk.run({"input": "test"}))["exit_code"] == 0
    assert marker.exists()


@pytest.mark.asyncio
async def test_spawn_failure():
    with pytest.raises(TransportError):
        await Client(["nonexistent-jiuwen-sdk-program"]).run({"input": "test"})


@pytest.mark.asyncio
async def test_cancel_interrupts_pending_host_answer(tmp_path):
    cancel = asyncio.Event()

    async def answer(_):
        cancel.set()
        await asyncio.sleep(30)
        raise AssertionError("late answer must not be sent")

    result = await client("wait").run(
        {"input": "test"}, on_interaction=answer, cancel=cancel, deadline_seconds=2
    )
    assert result["exit_code"] == 130


@pytest.mark.asyncio
async def test_cancelled_caller_task_reaps_child(tmp_path):
    entered = asyncio.Event()
    marker = tmp_path / "closed"

    async def answer(_):
        entered.set()
        await asyncio.sleep(30)

    sdk = Client(
        [sys.executable, str(FIXTURE)],
        env={"SDK_FIXTURE": "wait", "SDK_MARKER": str(marker)},
    )
    task = asyncio.create_task(sdk.run({"input": "test"}, on_interaction=answer))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert marker.exists()


def _is_running(pid):
    if os.name == "nt":
        tasklist = (
            Path(os.environ.get("SystemRoot", "C:/Windows"))
            / "System32"
            / "tasklist.exe"
        )
        result = subprocess.run(
            [str(tasklist), "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=10,
        )
        return any(
            len(row) > 1 and row[1] == str(pid)
            for row in csv.reader(result.stdout.decode(errors="replace").splitlines())
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = Path(f"/proc/{pid}/stat")
    if state.exists():
        try:
            return state.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
    return True


@pytest.mark.asyncio
async def test_force_fallback_reaps_owned_child_tree(tmp_path):
    marker = tmp_path / "owned-pids"
    sdk = Client(
        [sys.executable, str(FIXTURE)],
        env={"SDK_FIXTURE": "stubborn", "SDK_MARKER": str(marker)},
        shutdown_grace_seconds=0.2,
    )
    with pytest.raises(TimeoutError):
        await sdk.run({"input": "test"}, deadline_seconds=0.5)
    assert marker.exists()
    pids = json.loads(marker.read_text(encoding="utf-8"))
    for _ in range(20):
        if not any(_is_running(pid) for pid in pids):
            break
        await asyncio.sleep(0.1)
    assert not any(_is_running(pid) for pid in pids)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_rejected(value):
    with pytest.raises(ValueError):
        encode({"timeout_seconds": value})


def test_no_runtime_dependency():
    source = str(FIXTURE.parents[1] / "python" / "src")
    script = (
        "import sys\n"
        f"sys.path.append({source!r})\n"
        "import jiuwenswarm_sdk\n"
        "from pathlib import Path\n"
        f"assert Path(jiuwenswarm_sdk.__file__).resolve().parent == Path({source!r}) / 'jiuwenswarm_sdk'\n"
        "assert not any(k.startswith(('jiuwenswarm.', 'openjiuwen')) for k in sys.modules)\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
