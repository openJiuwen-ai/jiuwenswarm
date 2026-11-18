from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework
from e2a.agent_models import AgentResponse
from e2a.message import Message, ReqMethod

from agent_gateway.extensions import base_crypto
from agent_gateway.extensions.registry import ExtensionRegistry
from agent_gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.agents.harness.common.tools.pdf_tools import (
    ReadPdfRequest,
    _read_pdf_sync,
    read_pdf,
)
from jiuwenswarm.agents.harness.common.tools.tool_discovery_catalog import BUILTIN_TOOL_DISCOVERY
from jiuwenswarm.server.runtime.attachments.document_attachments import persist_and_parse_documents


def _build_pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    chunks = [b"%PDF-1.4\n"]
    offsets = [0]
    cursor = len(chunks[0])
    for number, body in enumerate(objects, start=1):
        offsets.append(cursor)
        chunk = f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        chunks.append(chunk)
        cursor += len(chunk)
    rows = ["0000000000 65535 f "]
    rows.extend(f"{offset:010d} 00000 n " for offset in offsets[1:])
    chunks.append(f"xref\n0 {len(rows)}\n".encode("ascii"))
    chunks.append(("\n".join(rows) + "\n").encode("ascii"))
    chunks.append(
        f"trailer\n<< /Size {len(rows)} /Root 1 0 R >>\nstartxref\n{cursor}\n%%EOF\n".encode("ascii")
    )
    return b"".join(chunks)


def _upload_documents(paths: list[Path]) -> list[dict[str, Any]]:
    result = persist_and_parse_documents(
        {
            "documents": [
                {"filename": path.name, "mime_type": "application/pdf", "path": str(path)}
                for path in paths
            ]
        }
    )
    assert not result.get("document_errors")
    return result["media_items"]


def _frontend_query(items: list[dict[str, Any]]) -> str:
    return "\n".join(["请读取 PDF。", "【上传文档】", *(f'@"{item["path"]}"' for item in items)])


@pytest.fixture
def isolated_gateway_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    original_provider = base_crypto.get_crypto_provider()
    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    try:
        yield
    finally:
        base_crypto.set_crypto_provider(original_provider)


@pytest.mark.parametrize("with_spaces", [False, True])
def test_eight_uploaded_pdfs_keep_references_without_binary_content(
    tmp_path: Path,
    with_spaces: bool,
) -> None:
    directory = tmp_path / ("documents with spaces" if with_spaces else "documents")
    directory.mkdir()
    paths = []
    for index in range(8):
        path = directory / f"paper-{index}.pdf"
        path.write_bytes(_build_pdf(f"Gateway PDF body {index}"))
        paths.append(path.resolve())
    items = _upload_documents(paths)
    assert len(items) == 8

    enriched = MessageHandler._resolve_structured_attachments(
        _frontend_query(items), items, cwd=str(tmp_path)
    )

    assert enriched.count("<file-reference") == 8
    for path in paths:
        assert f'path="{path}"' in enriched
        assert f'filename="{path.name}"' in enriched
    assert 'type="PDF"' in enriched
    assert "use a document reading tool" in enriched.lower()
    assert "<file-content" not in enriched
    assert "%PDF-" not in enriched
    assert "endstream" not in enriched
    assert "\ufffd" not in enriched
    for index in range(8):
        assert f"Gateway PDF body {index}" not in enriched


def test_read_pdf_is_discoverable() -> None:
    assert read_pdf.card.name == "read_pdf"
    assert "pdf_path" in read_pdf.card.input_params["properties"]
    assert "read_pdf" in BUILTIN_TOOL_DISCOVERY


def test_read_pdf_reads_uploaded_document(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    path = tmp_path / "readable.pdf"
    path.write_bytes(_build_pdf("E2E6122 readable body"))
    items = _upload_documents([path])

    result = _read_pdf_sync(ReadPdfRequest(pdf_path=items[0]["path"], pages=(1,)))
    assert "E2E6122 readable body" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("structured_attachments", [False, True])
async def test_chat_send_preserves_pdf_references_in_query_and_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    structured_attachments: bool,
    isolated_gateway_registry: None,
) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def send_request(self, envelope: Any) -> AgentResponse:
            self.requests.append(envelope)
            return AgentResponse(
                request_id=envelope.request_id,
                channel_id=envelope.channel,
                ok=True,
                payload={"event_type": "chat.final", "content": "ok"},
            )

    path = tmp_path / "dispatch.pdf"
    path.write_bytes(_build_pdf("E2E6122 dispatch body"))
    items = _upload_documents([path])
    query = _frontend_query(items)
    message = Message(
        id="pdf-chat-send",
        type="req",
        channel_id="web",
        session_id="sess-pdf-chat-send",
        params={
            "query": query,
            "content": query,
            "attachments": items if structured_attachments else [],
            "mode": "agent",
        },
        metadata={"cwd": str(tmp_path), "user_id": "test-user"},
        timestamp=0,
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=False,
    )
    monkeypatch.setattr(MessageHandler, "_instance", None)
    client = RecordingClient()
    handler = MessageHandler(client)
    ExtensionRegistry.create_instance(
        AsyncCallbackFramework(enable_metrics=False, enable_logging=False),
        {},
        logging.getLogger(__name__),
    )
    await handler.start_forwarding()
    try:
        async with asyncio.timeout(5):
            await handler.publish_user_messages(message)
            while not client.requests:
                await asyncio.sleep(0.01)
            await handler.consume_robot_messages(timeout=None)
    finally:
        await handler.stop_forwarding()
        if handler._fire_and_forget_tasks:
            await asyncio.gather(*handler._fire_and_forget_tasks, return_exceptions=True)

    assert len(client.requests) == 1
    enriched = client.requests[0].params["query"]
    assert client.requests[0].params["content"] == enriched
    assert message.params["query"] == enriched
    assert message.params["content"] == enriched
    assert f'path="{path.resolve()}"' in enriched
    assert 'type="PDF"' in enriched
    assert "use a document reading tool" in enriched.lower()
    params = json.dumps(client.requests[0].params, ensure_ascii=False, default=str)
    assert "%PDF-" not in params
    assert "endstream" not in params
    assert "\ufffd" not in params
    assert "E2E6122 dispatch body" not in params
