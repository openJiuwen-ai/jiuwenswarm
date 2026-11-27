import json
from types import SimpleNamespace

from jiuwenclaw.app_cli import run_acp
from jiuwenclaw.e2a.constants import E2A_RESPONSE_KIND_ACP_PROMPT_RESULT, E2A_RESPONSE_KIND_E2A_CHUNK
from jiuwenclaw.e2a.models import E2AProvenance, E2AResponse, utc_now_iso


class FakeStdin:
    def __init__(self):
        self.buffer = []

    def write(self, data):
        self.buffer.append(data)

    @staticmethod
    def flush():
        return None

    @staticmethod
    def close():
        return None


class FakeGatewayProcess:
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


def build_response(response_id, kind, final, body):
    return E2AResponse(
        response_id=response_id,
        request_id="req-int",
        jsonrpc_id="rpc-int",
        is_final=final,
        status="succeeded" if final else "in_progress",
        response_kind=kind,
        timestamp=utc_now_iso(),
        provenance=E2AProvenance(
            source_protocol="e2a",
            converter="test",
            converted_at=utc_now_iso(),
            details={},
        ),
        channel="acp",
        session_id="sess-int",
        body=body,
    )


def test_integration_acp_cli_returns_final_result(monkeypatch):
    chunk = build_response("chunk-int", E2A_RESPONSE_KIND_E2A_CHUNK, False, {"delta": "thinking"})
    final = build_response(
        "final-int",
        E2A_RESPONSE_KIND_ACP_PROMPT_RESULT,
        True,
        {"content": "integration answer", "session_id": "sess-int"},
    )
    proc = FakeGatewayProcess(
        [
            json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n",
            json.dumps(final.to_dict(), ensure_ascii=False) + "\n",
        ]
    )
    captured = {}

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(
        "jiuwenclaw.app_cli.write_json_stdout",
        lambda payload: captured.setdefault("output", payload),
    )

    exit_code = run_acp(
        SimpleNamespace(
            agent_server_url=None,
            session_id="sess-int",
            args=["hello"],
        )
    )

    assert exit_code == 0
    outbound = json.loads("".join(proc.stdin.buffer).strip())
    assert outbound.get("method") == "session/prompt"
    output = captured.get("output")
    assert isinstance(output, dict)
    result = output.get("result")
    assert isinstance(result, dict)
    assert result.get("content") == "integration answer"
