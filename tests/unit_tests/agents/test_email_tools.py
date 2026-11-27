# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import base64
import os
from email.message import EmailMessage

import pytest

from jiuwenswarm.agents.harness.common.tools import email_tools


def test_email_prefers_plain_text_and_lists_forwarded_message():
    message = EmailMessage()
    message["Subject"] = "Forwarded invoice"
    message.set_content("Plain body\n> Quoted reply", cte="quoted-printable")
    message.add_alternative("<p>Alternative HTML body</p>", subtype="html")
    forwarded = EmailMessage()
    forwarded["Subject"] = "Original message"
    forwarded.set_content("Forwarded content")
    message.add_attachment(forwarded, filename="original.eml")

    result = email_tools._parse_email(message.as_bytes())

    assert result["body"] == "Plain body\n> Quoted reply\n"
    assert result["attachment_names"] == ["original.eml"]
    assert result["body_truncated"] is False


def test_html_email_normalizes_breaks_and_omits_an_unclosed_hidden_element():
    message = EmailMessage()
    message["Subject"] = "HTML invoice"
    message.set_content("Invoice<br/>Total<script>hidden text", subtype="html")

    assert email_tools._parse_email(message.as_bytes())["body"] == "Invoice\nTotal"


def test_email_output_limits(monkeypatch):
    monkeypatch.setattr(email_tools, "_MAX_BODY_CHARS", 4)
    monkeypatch.setattr(email_tools, "_MAX_ATTACHMENTS", 1)
    message = EmailMessage()
    message["Subject"] = "Example"
    message.set_content("Body beyond the limit")
    for name in ("first.txt", "second.txt"):
        message.add_attachment(name, filename=name)

    result = email_tools._parse_email(message.as_bytes())

    assert result["body"] == "Body"
    assert result["body_truncated"] is True
    assert result["attachment_names"] == ["first.txt"]
    assert result["attachments_truncated"] is True


def test_email_rejects_excessive_mime_structure(monkeypatch):
    monkeypatch.setattr(email_tools, "_MAX_MIME_DEPTH", 0)
    message = EmailMessage()
    message["Subject"] = "Example"
    message.set_content("Plain body")
    message.add_alternative("<p>HTML body</p>", subtype="html")
    with pytest.raises(ValueError, match="MIME structure limits"):
        email_tools._parse_email(message.as_bytes())


def test_email_rejects_non_message():
    with pytest.raises(ValueError, match="recognizable email headers"):
        email_tools._parse_email(b"Ordinary text.")


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="Secure upload reads require O_NOFOLLOW")
def test_email_reads_twenty_mib_and_rejects_one_byte_more(tmp_path, monkeypatch):
    from jiuwenswarm.common import paths

    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    uploads = paths.get_agent_sessions_dir() / "size-limit" / "uploads"
    uploads.mkdir(parents=True)
    path = uploads / "large.eml"
    with path.open("wb") as upload:
        upload.write(
            b"Subject: At the limit\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Transfer-Encoding: binary\r\n\r\n"
        )
        upload.truncate(20 * 1024 * 1024)
    assert email_tools._read_email_sync("size-limit", str(path))["headers"]["Subject"] == "At the limit"

    with path.open("ab") as upload:
        upload.write(b"x")
    assert email_tools._read_email_sync("size-limit", str(path)) == {
        "error": "The email exceeds the 20 MiB reading limit.",
    }


@pytest.mark.asyncio
async def test_email_rejects_unsupported_extension():
    result = await email_tools.read_email.invoke({"file_path": "/uploads/mailbox.pst"})
    assert result == {"error": "Select an uploaded .eml or .msg file."}


@pytest.mark.asyncio
async def test_read_email_from_personal_document_upload(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit import (
        bind_session_messaging_route,
        reset_session_messaging_route,
    )
    from jiuwenswarm.common import paths
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.attachments.upload_storage import read_session_upload_chunk
    from jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter import WorkspaceFileAdapter

    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    data = base64.b64encode(b"Subject: Example\r\n\r\nEmail body").decode("ascii")
    response = await WorkspaceFileAdapter().handle(AgentRequest(
        request_id="upload", channel_id="web", session_id="own",
        req_method=ReqMethod.DOCUMENT_PERSIST,
        params={"documents": [{"filename": "test.eml", "base64_data": data}]},
    ))
    assert response.ok
    file_path = response.payload["files"]["uploaded_documents"][0]["path"]
    with pytest.raises(PermissionError):
        read_session_upload_chunk("own", file_path, 0, 100)

    for session_id in ("own", "other"):
        token = bind_session_messaging_route(session_id=session_id, request_id="read", user_id=None)
        try:
            result = await email_tools.read_email.invoke({"file_path": file_path})
        finally:
            reset_session_messaging_route(token)
        if session_id == "own":
            assert result["headers"]["Subject"] == "Example"
            assert result["body"] == "Email body"
        else:
            assert "error" in result

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    with pytest.raises(PermissionError):
        read_session_upload_chunk("own", file_path, 0, 100, allow_personal=True)
