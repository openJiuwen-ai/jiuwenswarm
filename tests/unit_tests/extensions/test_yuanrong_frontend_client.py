# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.extensions.yuanrong_frontend_client import (
    YuanrongAgentApiError,
    YuanrongAgentFileError,
    YuanrongFrontendAgentClient,
)


class YuanrongFrontendAgentClientProbe(YuanrongFrontendAgentClient):
    """Subclass exposing protected helpers for unit tests (G.CLS.11)."""

    def invoke_headers(
        self,
        session_id: str,
        *,
        user_id: str | None = None,
        req_method: str | None = None,
        stream: bool = False,
    ) -> dict[str, str]:
        return self._invoke_headers(
            session_id,
            user_id=user_id,
            req_method=req_method,
            stream=stream,
        )

    def parse_agent_file_list_response(
        self,
        body: str,
        status: int,
    ) -> list[dict[str, Any]]:
        return self._parse_agent_file_list_response(body, status)

    def parse_agent_file_mkdir_response(
        self,
        body: str,
        status: int,
        path: str,
    ) -> dict[str, Any]:
        return self._parse_agent_file_mkdir_response(body, status, path)


@pytest.fixture
def client() -> YuanrongFrontendAgentClientProbe:
    return YuanrongFrontendAgentClientProbe(
        frontend_endpoint="http://127.0.0.1:8080",
        function_version_urn="urn:test:function:1",
        concurrency=2,
    )


def test_invoke_headers_without_user_id(client: YuanrongFrontendAgentClientProbe):
    headers = client.invoke_headers("sess-1")

    assert "X-Session-Context" not in headers
    instance = json.loads(headers["X-Instance-Session"])
    assert instance == {"sessionID": "sess-1", "sessionTTL": 900, "concurrency": 2}


def test_invoke_headers_with_user_id(client: YuanrongFrontendAgentClientProbe):
    headers = client.invoke_headers("sess-1", user_id="alice")

    assert json.loads(headers["X-Session-Context"]) == {"sessionCtx": "alice"}
    assert json.loads(headers["X-Instance-Session"]) == {"sessionID": "sess-1", "sessionTTL": 900, "concurrency": 2}


def test_invoke_headers_stream_accepts_sse(client: YuanrongFrontendAgentClientProbe):
    headers = client.invoke_headers("sess-1", user_id="bob", stream=True)

    assert headers["Accept"] == "text/event-stream"
    assert json.loads(headers["X-Session-Context"]) == {"sessionCtx": "bob"}


@pytest.mark.asyncio
async def test_send_request_passes_user_id_in_session_context(client: YuanrongFrontendAgentClientProbe):
    await client.connect("http://127.0.0.1:8080")

    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=0):
        captured.update(dict(req.header_items()))
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b'{"ok": true}'
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    envelope = E2AEnvelope(
        request_id="req-1",
        channel="tui",
        session_id="sess-1",
        method="chat.send",
        user_id="alice",
    )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        response = await client.send_request(envelope)

    assert response.ok is True
    assert json.loads(captured["X-session-context"]) == {"sessionCtx": "alice"}


@pytest.mark.asyncio
async def test_send_request_omits_session_context_without_user_id(client: YuanrongFrontendAgentClientProbe):
    await client.connect("http://127.0.0.1:8080")

    captured: dict[str, str] = {}

    def fake_urlopen(req, timeout=0):
        captured.update(dict(req.header_items()))
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b'{}'
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    envelope = E2AEnvelope(
        request_id="req-2",
        channel="tui",
        session_id="sess-2",
        method="chat.send",
    )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        await client.send_request(envelope)

    assert "X-session-context" not in {k.lower() for k in captured}


@pytest.mark.asyncio
async def test_create_and_delete_sandbox_calls_agent_api(client: YuanrongFrontendAgentClientProbe):
    await client.connect("http://127.0.0.1:8080")

    requests: list[tuple[str, str, bytes | None]] = []

    def fake_urlopen(req, timeout=0):
        body = req.data
        requests.append((req.method, req.full_url, body))
        resp = MagicMock()
        resp.status = 200
        if req.method == "POST" and req.full_url.endswith("/api/agent"):
            resp.read.return_value = (
                b'{"code":200,"instance_id":"0b6c6322-6533-4901-8000-00000000bb0b"}'
            )
        elif req.method == "DELETE" and "/api/agent/" in req.full_url:
            resp.read.return_value = b'{"code":200,"status":"deleted"}'
        else:
            resp.read.return_value = b"{}"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        sandbox = await client.create_sandbox(
            namespace="dev",
            name="agent-001",
            workspace="/home/hhc/workspaceA",
            runtime_spec={
                "runtime": "python3.11",
                "sandbox_type": "docker",
                "rootfs": {
                    "imageurl": "yr-docker-runtime:v0",
                    "user": "agentos",
                    "ports": ["tcp:22"],
                },
                "cpu": 600,
                "memory": 512,
            },
            env_vars={"userid": "u-9f3a", "TRACE_ID": "ver-1"},
            mounts=[
                {
                    "source": "/home/hhc/workspaceB",
                    "target": "/mnt/workspaceB",
                    "readonly": False,
                }
            ],
        )
        await client.delete_sandbox(sandbox.sandbox_id)

    assert sandbox.sandbox_id == "0b6c6322-6533-4901-8000-00000000bb0b"
    assert sandbox.status == "ready"
    assert sandbox.metadata["provisioning"] == "yuanrong_agent_api_inline"

    create_method, create_url, create_body = requests[0]
    assert create_method == "POST"
    assert create_url == "http://127.0.0.1:8080/api/agent"
    create_payload = json.loads(create_body.decode("utf-8"))
    assert create_payload == {
        "namespace": "dev",
        "name": "agent-001",
        "workspace": "/home/hhc/workspaceA",
        "runtime_spec": {
            "runtime": "python3.11",
            "sandbox_type": "docker",
            "rootfs": {
                "imageurl": "yr-docker-runtime:v0",
                "user": "agentos",
                "ports": ["tcp:22"],
            },
            "cpu": 600,
            "memory": 512,
        },
        "env_vars": {"userid": "u-9f3a", "TRACE_ID": "ver-1"},
        "mounts": [
            {
                "source": "/home/hhc/workspaceB",
                "target": "/mnt/workspaceB",
                "readonly": False,
            }
        ],
    }
    assert "urn" not in create_payload

    delete_method, delete_url, delete_body = requests[1]
    assert delete_method == "DELETE"
    assert delete_url == (
        "http://127.0.0.1:8080/api/agent/"
        "0b6c6322-6533-4901-8000-00000000bb0b"
    )
    assert delete_body is None


@pytest.mark.asyncio
async def test_delete_sandbox_treats_404_as_success(client: YuanrongFrontendAgentClientProbe):
    await client.connect("http://127.0.0.1:8080")
    client._do_agent_delete = lambda instance_id: (  # noqa: ARG005
        404,
        '{"code":404,"message":"not found"}',
    )
    await client.delete_sandbox("already-gone")


_INLINE_RUNTIME_SPEC = {
    "runtime": "python3.11",
    "rootfs": {"imageurl": "yr-docker-runtime:v0"},
}


@pytest.mark.asyncio
async def test_create_sandbox_uses_agent_timeout():
    client = YuanrongFrontendAgentClientProbe(
        frontend_endpoint="http://127.0.0.1:8080",
        function_version_urn="urn:test:function:1",
        invoke_timeout_s=60.0,
        agent_timeout_s=300.0,
    )
    await client.connect("http://127.0.0.1:8080")
    captured_timeout: dict[str, float] = {}

    def fake_urlopen(req, timeout=0):
        captured_timeout["timeout"] = timeout
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = (
            b'{"code":200,"instance_id":"0b6c6322-6533-4901-8000-00000000bb0b"}'
        )
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        await client.create_sandbox(
            namespace="dev",
            name="agent-001",
            workspace="/home/hhc/workspaceA",
            runtime_spec=_INLINE_RUNTIME_SPEC,
        )

    assert captured_timeout["timeout"] == 300.0


@pytest.mark.asyncio
async def test_create_sandbox_raises_clear_timeout_error():
    client = YuanrongFrontendAgentClientProbe(
        frontend_endpoint="http://127.0.0.1:8080",
        function_version_urn="urn:test:function:1",
        agent_timeout_s=12.0,
    )
    await client.connect("http://127.0.0.1:8080")

    with patch(
        "urllib.request.urlopen",
        side_effect=TimeoutError("timed out"),
    ):
        with pytest.raises(YuanrongAgentApiError, match="request timeout after 12"):
            await client.create_sandbox(
                namespace="dev",
                name="agent-001",
                workspace="/home/hhc/workspaceA",
                runtime_spec=_INLINE_RUNTIME_SPEC,
            )


@pytest.mark.asyncio
async def test_create_sandbox_raises_on_agent_api_error(client: YuanrongFrontendAgentClientProbe):
    await client.connect("http://127.0.0.1:8080")

    def fake_urlopen(req, timeout=0):
        resp = MagicMock()
        resp.status = 500
        resp.read.return_value = b'{"code":500,"message":"create failed"}'
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(YuanrongAgentApiError, match="agent API failed"):
            await client.create_sandbox(
                namespace="dev",
                name="agent-001",
                workspace="/home/hhc/workspaceA",
                runtime_spec=_INLINE_RUNTIME_SPEC,
            )


@pytest.mark.asyncio
async def test_create_sandbox_requires_connection(client: YuanrongFrontendAgentClientProbe):
    with pytest.raises(RuntimeError, match="client not connected"):
        await client.create_sandbox(
            namespace="dev",
            name="agent-001",
            workspace="/home/hhc/workspaceA",
            runtime_spec=_INLINE_RUNTIME_SPEC,
        )


@pytest.mark.asyncio
async def test_create_sandbox_requires_required_fields(
    client: YuanrongFrontendAgentClientProbe,
):
    await client.connect("http://127.0.0.1:8080")
    with pytest.raises(ValueError, match="namespace is required"):
        await client.create_sandbox(
            namespace="",
            name="agent-001",
            workspace="/home/hhc/workspaceA",
            runtime_spec=_INLINE_RUNTIME_SPEC,
        )
    with pytest.raises(ValueError, match="name is required"):
        await client.create_sandbox(
            namespace="dev",
            name="",
            workspace="/home/hhc/workspaceA",
            runtime_spec=_INLINE_RUNTIME_SPEC,
        )
    with pytest.raises(ValueError, match="workspace is required"):
        await client.create_sandbox(
            namespace="dev",
            name="agent-001",
            workspace="",
            runtime_spec=_INLINE_RUNTIME_SPEC,
        )
    with pytest.raises(ValueError, match="runtime_spec.runtime is required"):
        await client.create_sandbox(
            namespace="dev",
            name="agent-001",
            workspace="/home/hhc/workspaceA",
            runtime_spec={"rootfs": {"imageurl": "yr-docker-runtime:v0"}},
        )
    with pytest.raises(ValueError, match="runtime_spec.rootfs.imageurl is required"):
        await client.create_sandbox(
            namespace="dev",
            name="agent-001",
            workspace="/home/hhc/workspaceA",
            runtime_spec={"runtime": "python3.11", "rootfs": {}},
        )


def test_parse_list_unwraps_faas_envelope(client: YuanrongFrontendAgentClientProbe):
    body = json.dumps({
        "body": {
            "items": [
                {
                    "name": "hello.txt",
                    "path": "/home/agentos/hello.txt",
                    "is_directory": False,
                    "size": 10,
                },
            ]
        },
        "innerCode": "0",
        "billingDuration": "todo",
    })
    items = client.parse_agent_file_list_response(body, 200)
    assert len(items) == 1
    assert items[0]["name"] == "hello.txt"


def test_parse_list_plain_items_still_works(client: YuanrongFrontendAgentClientProbe):
    body = json.dumps({"items": [{"name": "a", "path": "/home/agentos/a"}]})
    items = client.parse_agent_file_list_response(body, 200)
    assert items[0]["name"] == "a"


def test_parse_list_faas_error_raises(client: YuanrongFrontendAgentClientProbe):
    body = json.dumps({"body": {"error": "boom"}, "innerCode": "1"})
    with pytest.raises(YuanrongAgentFileError):
        client.parse_agent_file_list_response(body, 200)


def test_parse_mkdir_unwraps_code_data_envelope(client: YuanrongFrontendAgentClientProbe):
    body = json.dumps(
        {
            "code": 200,
            "data": {
                "success": True,
                "path": "/home/snuser/work/sub",
                "created": True,
            },
        }
    )
    parsed = client.parse_agent_file_mkdir_response(body, 200, "/fallback")
    assert parsed == {
        "success": True,
        "path": "/home/snuser/work/sub",
        "created": True,
    }


def test_parse_mkdir_idempotent_created_false(client: YuanrongFrontendAgentClientProbe):
    body = json.dumps(
        {"code": 200, "data": {"success": True, "path": "/home/agentos/sub", "created": False}}
    )
    parsed = client.parse_agent_file_mkdir_response(body, 200, "/home/agentos/sub")
    assert parsed["created"] is False


def test_parse_mkdir_unwraps_faas_envelope(client: YuanrongFrontendAgentClientProbe):
    body = json.dumps(
        {
            "body": {"success": True, "path": "/home/agentos/sub", "created": True},
            "innerCode": "0",
        }
    )
    parsed = client.parse_agent_file_mkdir_response(body, 200, "/fallback")
    assert parsed["path"] == "/home/agentos/sub"
    assert parsed["created"] is True


def test_parse_mkdir_http_400_is_bad_request(client: YuanrongFrontendAgentClientProbe):
    with pytest.raises(YuanrongAgentFileError) as exc:
        client.parse_agent_file_mkdir_response(
            json.dumps({"code": 400, "message": "parent directory does not exist"}),
            400,
            "/home/agentos/nope/deep",
        )
    assert exc.value.error_code == "BAD_REQUEST"
    assert exc.value.http_status == 400


def test_normalize_mkdir_mode_rejects_illegal(client: YuanrongFrontendAgentClientProbe):
    assert client._normalize_mkdir_mode(None) is None  # noqa: SLF001
    assert client._normalize_mkdir_mode("0755") == "0755"  # noqa: SLF001
    assert client._normalize_mkdir_mode("700") == "700"  # noqa: SLF001
    with pytest.raises(YuanrongAgentFileError) as exc:
        client._normalize_mkdir_mode("rwx")  # noqa: SLF001
    assert exc.value.error_code == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_mkdir_agent_dir_sends_query_params(client: YuanrongFrontendAgentClientProbe):
    await client.connect("http://127.0.0.1:8080")
    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=0):
        del timeout
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["authorization"] = dict(req.header_items()).get("Authorization")
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps(
            {"code": 200, "data": {"success": True, "path": "/home/agentos/sub", "created": True}}
        ).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = await client.mkdir_agent_dir(
            "inst-1",
            "/home/agentos/sub",
            mode="0755",
            recursive=True,
            auth_headers={"Authorization": "Bearer tok-mkdir"},
        )

    assert result["created"] is True
    assert captured["method"] == "POST"
    assert captured["authorization"] == "Bearer tok-mkdir"
    assert "/api/agent/inst-1/files/mkdir?" in captured["url"]
    assert "path=%2Fhome%2Fagentos%2Fsub" in captured["url"]
    assert "mode=0755" in captured["url"]
    assert "recursive=true" in captured["url"]


@pytest.mark.asyncio
async def test_mkdir_agent_dir_omits_mode_when_unset(client: YuanrongFrontendAgentClientProbe):
    await client.connect("http://127.0.0.1:8080")
    captured: dict[str, Any] = {}

    def fake_urlopen(req, timeout=0):
        del timeout
        captured["url"] = req.full_url
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps(
            {"success": True, "path": "/home/agentos/sub", "created": True}
        ).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        await client.mkdir_agent_dir("inst-1", "/home/agentos/sub")

    assert "mode=" not in captured["url"]
    assert "recursive=false" in captured["url"]


@pytest.mark.asyncio
async def test_mkdir_agent_dir_rejects_empty_path(client: YuanrongFrontendAgentClientProbe):
    await client.connect("http://127.0.0.1:8080")
    with pytest.raises(ValueError, match="path is required"):
        await client.mkdir_agent_dir("inst-1", "   ")


@pytest.mark.asyncio
async def test_mkdir_agent_dir_rejects_nul_path(client: YuanrongFrontendAgentClientProbe):
    await client.connect("http://127.0.0.1:8080")
    with pytest.raises(YuanrongAgentFileError) as exc:
        await client.mkdir_agent_dir("inst-1", "/home/agentos/foo\x00bar")
    assert exc.value.error_code == "BAD_REQUEST"
