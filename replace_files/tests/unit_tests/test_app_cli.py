import json
import sys
from types import SimpleNamespace

from jiuwenclaw.app_cli import run_acp
from jiuwenclaw.e2a.constants import E2A_RESPONSE_KIND_ACP_PROMPT_RESULT
from jiuwenclaw.e2a.models import E2AProvenance, E2AResponse, utc_now_iso


class FakeStdin:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    @staticmethod
    def flush():
        return None

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, stdout_lines):
        self.stdin = FakeStdin()
        self.stdout = iter(stdout_lines)
        self.terminated = False

    def poll(self):
        if self.terminated:
            return 0
        return None

    def terminate(self):
        self.terminated = True

    @staticmethod
    def wait(timeout=None):
        return 0

    def kill(self):
        self.terminated = True


def build_final_response(jsonrpc_id, content, session_id):
    return E2AResponse(
        response_id="resp-1",
        request_id="req-1",
        jsonrpc_id=jsonrpc_id,
        is_final=True,
        status="succeeded",
        response_kind=E2A_RESPONSE_KIND_ACP_PROMPT_RESULT,
        timestamp=utc_now_iso(),
        provenance=E2AProvenance(
            source_protocol="e2a",
            converter="test",
            converted_at=utc_now_iso(),
            details={},
        ),
        channel="acp",
        session_id=session_id,
        body={"content": content, "session_id": session_id},
    )


def test_run_acp_starts_gateway_and_prints_final_jsonrpc(monkeypatch):
    captured = {}
    response = build_final_response("rpc-1", "hello back", "sess-1")
    fake_proc = FakeProc([json.dumps(response.to_dict(), ensure_ascii=False) + "\n"])

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        captured["proc"] = fake_proc
        return fake_proc

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    monkeypatch.setattr(
        "jiuwenclaw.app_cli.write_json_stdout",
        lambda payload: captured.setdefault("output", payload),
    )

    exit_code = run_acp(
        SimpleNamespace(
            agent_server_url=None,
            session_id="sess-1",
            args=["hello", "world"],
        )
    )

    assert exit_code == 0
    assert captured.get("cmd") == [sys.executable, "-m", "jiuwenclaw.channel.acp_channel"]
    proc = captured.get("proc")
    assert proc is not None
    outbound = json.loads("".join(proc.stdin.writes).strip())
    assert outbound.get("method") == "session/prompt"
    params = outbound.get("params")
    assert isinstance(params, dict)
    assert params.get("content") == "hello world"
    output = captured.get("output")
    assert isinstance(output, dict)
    result = output.get("result")
    assert isinstance(result, dict)
    assert result.get("content") == "hello back"


def test_run_acp_passes_agent_server_url(monkeypatch):
    captured = {}
    response = build_final_response("rpc-2", "ok", "sess-2")

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc([json.dumps(response.to_dict()) + "\n"])

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    monkeypatch.setattr("jiuwenclaw.app_cli.write_json_stdout", lambda payload: None)

    exit_code = run_acp(
        SimpleNamespace(
            agent_server_url="ws://127.0.0.1:18092",
            session_id="sess-2",
            args=["hello"],
        )
    )

    assert exit_code == 0
    assert captured.get("cmd") == [
        sys.executable,
        "-m",
        "jiuwenclaw.channel.acp_channel",
        "--gateway-url",
        "ws://127.0.0.1:18092",
    ]
