# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.attachments.document_attachments import (
    FORBIDDEN_DOCUMENT_EXTENSIONS,
    forbidden_formats,
    is_forbidden_document,
    is_supported_document,
    persist_and_parse_documents,
)


@pytest.mark.asyncio
async def test_persist_caps_document_count_at_max(tmp_path: Path):
    documents = []
    for idx in range(25):
        doc = tmp_path / f"doc-{idx}.md"
        doc.write_text(f"# doc {idx}", encoding="utf-8")
        documents.append(
            {
                "filename": doc.name,
                "mime_type": "text/markdown",
                "path": str(doc),
            }
        )
    result = persist_and_parse_documents({"documents": documents})

    items = result.get("media_items") or []
    # _MAX_DOCUMENT_COUNT=8 truncates the batch to the first 8 documents.
    assert len(items) == 8
    assert not result.get("document_errors")


def test_forbidden_formats_include_executables():
    formats = set(forbidden_formats())
    assert formats == set(FORBIDDEN_DOCUMENT_EXTENSIONS)
    assert ".exe" in formats
    assert ".dll" in formats
    assert ".ps1" in formats
    assert ".dmg" in formats
    assert ".pdf" not in formats


def test_document_blacklist_helpers():
    assert is_supported_document(filename="note.ipynb")
    assert is_supported_document(filename="report.docx")
    assert not is_supported_document(filename="a.exe")
    assert is_forbidden_document(filename="malware.bin")
    assert is_forbidden_document(suffix=".ps1")
    assert not is_forbidden_document(filename="readme.md")


@pytest.mark.asyncio
async def test_persist_documents_returns_original_path_without_writing(tmp_path: Path):
    content = "# uploaded doc\n\ncontent"
    source = tmp_path / "readme.md"
    source.write_text(content, encoding="utf-8")

    payload = {
        "documents": [
            {
                "filename": "readme.md",
                "mime_type": "text/markdown",
                "path": str(source),
            }
        ]
    }
    result = persist_and_parse_documents(payload)

    items = result.get("media_items") or []
    assert len(items) == 1
    assert items[0]["type"] == "document"
    assert Path(items[0]["path"]) == source.resolve()
    assert items[0]["original_path"] == items[0]["path"]
    assert "text" not in items[0]
    assert "parser" not in items[0]
    assert result["files"]["uploaded_documents"][0]["path"] == items[0]["path"]


@pytest.mark.asyncio
async def test_persist_rejects_forbidden_extension(tmp_path: Path):
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ")
    result = persist_and_parse_documents(
        {
            "documents": [
                {
                    "filename": "setup.exe",
                    "path": str(exe),
                }
            ]
        }
    )
    assert not result.get("media_items")
    errors = result.get("document_errors") or []
    assert len(errors) == 1
    assert "forbidden" in errors[0]["error"].lower()


@pytest.mark.asyncio
async def test_persist_missing_path_without_content_is_rejected():
    result = persist_and_parse_documents(
        {
            "documents": [
                {
                    "filename": "readme.md",
                    "mime_type": "text/markdown",
                }
            ]
        }
    )
    assert not result.get("media_items")
    errors = result.get("document_errors") or []
    assert len(errors) == 1
    assert "path" in errors[0]["error"].lower() or "base64" in errors[0]["error"].lower()


@pytest.mark.asyncio
async def test_persist_base64_document_writes_to_upload_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.attachments.document_attachments.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    result = persist_and_parse_documents(
        {
            "documents": [
                {
                    "filename": "readme.md",
                    "mime_type": "text/markdown",
                    "base64_data": "IyB1cGxvYWRlZCBkb2M=",  # b"# uploaded doc"
                }
            ]
        },
        session_id="sess-1",
    )
    items = result.get("media_items") or []
    assert len(items) == 1
    assert items[0]["type"] == "document"
    stored = Path(items[0]["path"])
    assert stored.is_file()
    assert stored.read_bytes() == b"# uploaded doc"
    assert stored.name == "readme.md"
    assert result["files"]["uploaded_documents"][0]["path"] == items[0]["path"]


@pytest.mark.asyncio
async def test_persist_base64_document_rejects_forbidden_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.attachments.document_attachments.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    result = persist_and_parse_documents(
        {
            "documents": [
                {
                    "filename": "setup.exe",
                    "base64_data": "TQ==",  # b"M"
                }
            ]
        },
        session_id="sess-1",
    )
    assert not result.get("media_items")
    errors = result.get("document_errors") or []
    assert len(errors) == 1
    assert "forbidden" in errors[0]["error"].lower()


@pytest.mark.asyncio
async def test_persist_base64_document_strips_content_from_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The persisted response must not echo base64 content back (WS frame size)."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.attachments.document_attachments.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    result = persist_and_parse_documents(
        {
            "documents": [
                {
                    "filename": "notes.md",
                    "mime_type": "text/markdown",
                    "base64_data": "IyB1cGxvYWRlZCBkb2M=",  # b"# uploaded doc"
                }
            ]
        },
        session_id="sess-1",
    )
    # Original documents list is echoed back as the response payload; the base64
    # payload must be dropped so the response stays small.
    docs = result.get("documents") or []
    assert len(docs) == 1
    assert "base64_data" not in docs[0]
    assert "base64Data" not in docs[0]
    # media_items likewise only carry the server-side path metadata.
    items = result.get("media_items") or []
    assert len(items) == 1
    assert "base64_data" not in items[0]
    assert "base64Data" not in items[0]


@pytest.mark.asyncio
async def test_persist_passthrough_large_document_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A document already persisted by the gateway HTTP bridge is passed through."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.attachments.document_attachments.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    upload_dir = tmp_path / "sess-1" / "uploads"
    upload_dir.mkdir(parents=True)
    big = upload_dir / "big.pdf"
    big.write_bytes(b"%PDF-1.4 " + b"x" * 1000)
    result = persist_and_parse_documents(
        {
            "documents": [
                {
                    "type": "document",
                    "filename": "big.pdf",
                    "mime_type": "application/pdf",
                    "path": str(big),
                    "_persisted": True,
                }
            ]
        },
        session_id="sess-1",
    )
    items = result.get("media_items") or []
    assert len(items) == 1
    assert items[0]["path"] == str(big)
    assert items[0]["size_bytes"] == big.stat().st_size


@pytest.mark.asyncio
async def test_persist_passthrough_missing_persisted_path_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A _persisted item whose file no longer exists is dropped, not errored."""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.attachments.document_attachments.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    result = persist_and_parse_documents(
        {
            "documents": [
                {
                    "type": "document",
                    "filename": "ghost.pdf",
                    "path": "/nonexistent/ghost.pdf",
                    "_persisted": True,
                }
            ]
        },
        session_id="sess-1",
    )
    assert not result.get("media_items")
    assert not result.get("document_errors")
