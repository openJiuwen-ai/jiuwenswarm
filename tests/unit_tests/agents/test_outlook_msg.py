# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read synthetic Outlook items independently generated for these tests.

The Unicode samples use @tutao/oxmsg 0.2.3. The ANSI samples use SheetJS CFB
1.2.2 with explicit MAPI code pages and encoded text streams.
"""

import base64
import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from jiuwenswarm.agents.harness.common.tools import email_tools
from jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit import (
    bind_session_messaging_route,
    reset_session_messaging_route,
)
from jiuwenswarm.common import paths
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
from jiuwenswarm.enterprise.tenant.tenant_paths import build_tenant_key
from jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter import WorkspaceFileAdapter


def _sample(name: str) -> bytes:
    with ZipFile(Path(__file__).resolve().parents[2] / "fixtures" / "outlook_msg_samples.zip") as samples:
        return samples.read(name)


async def _read_in_session(session_id: str, file_path: str) -> dict:
    token = bind_session_messaging_route(session_id=session_id, request_id="read", user_id=None)
    try:
        return await email_tools.read_email.invoke({"file_path": file_path})
    finally:
        reset_session_messaging_route(token)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="Secure upload reads require O_NOFOLLOW")
@pytest.mark.asyncio
@pytest.mark.parametrize("edition", ["personal", "enterprise"])
async def test_msg_upload_is_readable_only_by_its_conversation(tmp_path, monkeypatch, edition):
    monkeypatch.setenv("JIUWENSWARM_EDITION", edition)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    with TenantContext.scoped(build_tenant_key("group", "bot", "owner")):
        response = await WorkspaceFileAdapter().handle(AgentRequest(
            request_id="upload", channel_id="web", session_id="own", req_method=ReqMethod.DOCUMENT_PERSIST,
            params={"documents": [{
                "filename": "invoice.MSG", "base64_data": base64.b64encode(_sample("invoice.msg")).decode("ascii"),
            }]},
        ))
        assert response.ok
        path = response.payload["files"]["uploaded_documents"][0]["path"]
        result = await _read_in_session("own", path)
        assert result["headers"]["Subject"] == "Résumé approval"
        assert "sender@example.test" in result["headers"]["From"]
        assert result["headers"]["To"] == "analyst@example.test"
        assert result["headers"]["Cc"] == "reviewer@example.test"
        assert "2020" in result["headers"]["Date"]
        assert result["body"] == "Invoice total: 42 EUR."
        assert result["attachment_names"] == ["café.txt", "report.csv"]
        assert "Private attachment content." not in str(result)
        assert "error" in await _read_in_session("other-session", path)

    if edition == "enterprise":
        with TenantContext.scoped(build_tenant_key("group", "bot", "another-user")):
            assert "error" in await _read_in_session("own", path)


def test_msg_reads_html_only_body_and_transport_headers():
    result = email_tools._parse_msg(_sample("html.msg"))
    assert result["body"] == "HTML invoice\nTotal 42"
    assert result["headers"]["From"] == "sender@example.test"
    assert result["headers"]["To"] == "recipient@example.test"
    assert result["attachment_names"] == []


def test_msg_decodes_ansi_fields_with_the_declared_code_page():
    result = email_tools._parse_msg(_sample("cp950.msg"))
    assert result["headers"]["Subject"] == "Invoice Ω"
    assert result["body"] == "Total Ω: 42"


def test_msg_reads_a_body_when_ansi_and_internet_encodings_differ():
    result = email_tools._parse_msg(_sample("mixed-codepage.msg"))
    assert result["headers"]["Subject"] == "Facturé"
    assert result["body"] == "Invoice coût: 42"


def test_msg_uses_locale_for_ansi_subject_and_internet_code_page_for_html():
    result = email_tools._parse_msg(_sample("western-html.msg"))
    assert result["headers"]["Subject"] == "Résumé"
    assert result["body"] == "Итого: 42"


def test_msg_identifies_rtf_only_body():
    result = email_tools._parse_msg(_sample("rtf.msg"))
    assert result["headers"]["Subject"] == "RTF only"
    assert result["body"] == ""
    assert "RTF" in result["body_note"]


def test_msg_reads_attachment_names_without_opening_payloads(monkeypatch):
    from olefile import OleFileIO

    opened = set()
    open_stream = OleFileIO.openstream

    def record_open(ole, stream):
        path = stream.split("/") if isinstance(stream, str) else stream
        opened.add(path[-1])
        return open_stream(ole, stream)

    monkeypatch.setattr(OleFileIO, "openstream", record_open)
    assert email_tools._parse_msg(_sample("invoice.msg"))["attachment_names"] == ["café.txt", "report.csv"]
    assert "RTF" in email_tools._parse_msg(_sample("rtf.msg"))["body_note"]
    assert "__substg1.0_3707001F" in opened
    assert "__substg1.0_37010102" not in opened
    assert "__substg1.0_10090102" not in opened


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="Secure upload reads require O_NOFOLLOW")
def test_msg_accepts_twenty_mib_and_rejects_one_more_byte(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    uploads = paths.get_agent_sessions_dir() / "size-check" / "uploads"
    uploads.mkdir(parents=True)
    file_path = uploads / "large.msg"
    with file_path.open("wb") as upload:
        upload.write(_sample("invoice.msg"))
        upload.truncate(20 * 1024 * 1024)
    assert email_tools._read_email_sync("size-check", str(file_path))["body"] == "Invoice total: 42 EUR."
    with file_path.open("ab") as upload:
        upload.write(b"x")
    assert "20 MiB" in email_tools._read_email_sync("size-check", str(file_path))["error"]


def test_msg_truncates_fields_body_and_attachment_list(monkeypatch):
    monkeypatch.setattr(email_tools, "_MAX_FIELD_CHARS", 5)
    monkeypatch.setattr(email_tools, "_MAX_BODY_CHARS", 7)
    monkeypatch.setattr(email_tools, "_MAX_ATTACHMENTS", 1)
    result = email_tools._parse_msg(_sample("invoice.msg"))
    assert result["headers"]["Subject"] == "Résum"
    assert result["body"] == "Invoice"
    assert result["body_truncated"] is True
    assert result["attachment_names"] == ["café."]
    assert result["attachments_truncated"] is True


def test_msg_rejects_non_mail_outlook_item():
    with pytest.raises(ValueError, match="not an email"):
        email_tools._parse_msg(_sample("task.msg"))


def test_msg_rejects_mime_email_renamed_to_msg():
    with pytest.raises(ValueError, match="MSG"):
        email_tools._parse_msg(b"Subject: Not an Outlook file\r\n\r\nBody")
