# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.gateway.document_attachments import (
    FORBIDDEN_DOCUMENT_EXTENSIONS,
    forbidden_formats,
    is_forbidden_document,
    is_supported_document,
    persist_and_parse_documents,
)


@pytest.mark.asyncio
async def test_persist_accepts_many_documents_without_count_limit(tmp_path: Path):
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
    result = await persist_and_parse_documents({"documents": documents})

    items = result.get("media_items") or []
    assert len(items) == 25
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
    result = await persist_and_parse_documents(payload)

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
    result = await persist_and_parse_documents(
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
async def test_persist_rejects_missing_path():
    result = await persist_and_parse_documents(
        {
            "documents": [
                {
                    "filename": "readme.md",
                    "mime_type": "text/markdown",
                    "base64_data": "YWJj",
                }
            ]
        }
    )
    assert not result.get("media_items")
    errors = result.get("document_errors") or []
    assert len(errors) == 1
    assert "path" in errors[0]["error"].lower()
