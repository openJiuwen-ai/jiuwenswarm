from types import SimpleNamespace

import pytest

from jiuwenswarm.benchmarks.duplex_completion import VerifyAndFinish
from jiuwenswarm.benchmarks.duplex_metrics import Events
from jiuwenswarm.benchmarks.duplex_runtime import request_fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "failure", "changed", "success"])
async def test_submission_requires_saved_unchanged_deliverable_and_passed_check(tmp_path, case):
    calls = []
    states = iter([{"ready": case != "missing", "reason": "Missing output", "revision": "a"},
                   {"ready": True, "reason": "", "revision": "b" if case == "changed" else "a"}])

    async def state():
        return next(states)

    async def execute(inputs, *, tool_name):
        calls.append(tool_name)
        return "exit_code=1\nAssertionError" if case == "failure" else "exit_code=0\nChecks passed"

    tool = VerifyAndFinish(execute=execute, submission_state=state, events=Events(tmp_path / "events.jsonl"))
    result = await tool.invoke({"command": "python verify.py", "summary": "Saved and checked"})
    assert result["submitted"] == (case == "success")
    assert (tool.accepted is not None) == (case == "success")
    assert len(calls) == (0 if case == "missing" else 1)


def test_request_fingerprint_ignores_call_identity_but_not_input_or_parameters():
    def request(identity, text="same", seconds=None):
        return {"messages": [
            {"role": "assistant", "tool_calls": [{"id": identity,
                "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}]},
            {"role": "tool", "tool_call_id": identity, "content": text}],
            "tools": [], "session": SimpleNamespace(seconds=seconds)}

    a = request_fingerprint((), request("a"), {"temperature": 0})
    b = request_fingerprint((), request("b", seconds=99), {"temperature": 0})
    assert a["request_sha256"] != b["request_sha256"]
    assert a["semantic_request_sha256"] == b["semantic_request_sha256"]
    for kwargs, config in [(request("a", "changed"), {"temperature": 0}),
                           (request("a"), {"temperature": 1})]:
        assert request_fingerprint((), kwargs, config)["semantic_request_sha256"] != a["semantic_request_sha256"]


def test_request_fingerprint_matches_sdk_message_conversion():
    from openjiuwen.core.foundation.llm.schema.message import UserMessage

    def request(identity, content="same"):
        return {"messages": [UserMessage(content=content, metadata={"context_message_id": identity})]}

    a = request_fingerprint((), request("a"), {})
    b = request_fingerprint((), request("b"), {})
    assert a["request_sha256"] != b["request_sha256"]
    assert a["semantic_request_sha256"] == b["semantic_request_sha256"]
    assert request_fingerprint((), request("a", "changed"), {})["semantic_request_sha256"] != a["semantic_request_sha256"]
    assert request_fingerprint((request("a")["messages"],), {}, {})["semantic_request_sha256"] == a["semantic_request_sha256"]

    # Raw dictionaries are forwarded by the SDK; their metadata must not be hidden.
    raw = {"messages": [{"role": "user", "content": "same", "metadata": {"custom": "a"}}]}
    c = request_fingerprint((), raw, {})
    raw["messages"][0]["metadata"]["custom"] = "b"
    assert request_fingerprint((), raw, {})["semantic_request_sha256"] != c["semantic_request_sha256"]
