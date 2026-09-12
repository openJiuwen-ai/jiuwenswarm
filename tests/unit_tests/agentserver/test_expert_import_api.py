"""expert.import 的上传、持久化与安全边界测试。"""

import asyncio
import base64
import io
import json
import zipfile
from pathlib import Path

import pytest

import jiuwenswarm.server.agent_ws_server as server_module
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.expert import expert_store as es


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


@pytest.fixture(autouse=True)
def _wire_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        server_module,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {
            "response_id": response_id,
            "ok": resp.ok,
            "payload": resp.payload,
        },
    )


def _request(params: dict) -> AgentRequest:
    return AgentRequest(
        request_id="req-import",
        channel_id="desktop",
        req_method=ReqMethod.EXPERT_IMPORT,
        params=params,
    )


def _agent_zip() -> bytes:
    manifest = {
        "packageType": "agent_template",
        "agentCard": {"id": "uploaded-expert", "name": "上传专家", "description": "描述"},
        "persona": {"dir": "agents"},
        "metadata": {"tags": ["test"]},
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("uploaded-expert/manifest.json", json.dumps(manifest))
        archive.writestr("uploaded-expert/agents/00-identity.md", "# 人设")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_import_zip_persists_and_appears_in_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setattr(es, "get_expert_cache_dir", lambda: cache)
    server = server_module.AgentWebSocketServer()
    ws = FakeWebSocket()
    await server._handle_expert_import(
        ws,
        _request({
            "filename": "uploaded.zip",
            "file_content": base64.b64encode(_agent_zip()).decode(),
        }),
        asyncio.Lock(),
    )
    assert ws.sent[0]["ok"] is True
    assert ws.sent[0]["payload"]["expert_id"] == "uploaded-expert"
    assert (cache / "uploaded-expert" / es.IMPORTED_MARKER).is_file()
    summaries = await es.ImportedExpertPackageSource(cache).list()
    assert [(item.id, item.source, item.available) for item in summaries] == [
        ("uploaded-expert", "imported", True)
    ]


@pytest.mark.asyncio
async def test_import_rejects_bad_base64() -> None:
    server = server_module.AgentWebSocketServer()
    ws = FakeWebSocket()
    await server._handle_expert_import(
        ws, _request({"filename": "bad.zip", "file_content": "%%%"}), asyncio.Lock()
    )
    assert ws.sent[0]["payload"]["code"] == "BAD_REQUEST"


def test_import_rejects_zip_path_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(es, "get_expert_cache_dir", lambda: tmp_path / "cache")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../manifest.json", "{}")
    with pytest.raises(es.InvalidExpertPackage, match="zip 条目非法"):
        es.import_expert_zip(buffer.getvalue())
