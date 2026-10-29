# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structured messages through real Registry, Worker event chain and JSONL."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.session.history_io import run_history_io
from jiuwenswarm.server.runtime.session.history_message import message_from_base_message, message_from_dict, valid_message_rewrite
from jiuwenswarm.server.runtime.session.session_history import append_history_record
from .management.support import write_package
from .test_session_history_ops import history_host, _message, _query, _rows


POLICY = '''import copy
import json
import os
from pathlib import Path
from dataclasses import replace
from openjiuwen.core.events import Event

class Extension:
    def register(self, registrar):
        registrar.register_event_handler("session.append", self.policy, {"door": "tool-result", "origin.kind": "tool"}, "user")

    async def policy(self, event, ops, next_handler):
        request = event.event_input
        observed = {"uuid": request.uuid, "door": request.door, "origin": {
            "kind": request.origin.kind, "name": request.origin.name, "entry": request.origin.entry},
            "message": request.message.to_dict(), "session_id": ops.session_context.session_id}
        with Path(os.environ["STRUCTURED_HISTORY_OUTPUT"]).open("a", encoding="utf-8") as out:
            out.write(json.dumps(observed) + "\\n")
        mode = request.message.name
        if mode == "forge-id":
            request = replace(request, uuid="other-row")
        elif mode == "forge-origin":
            request = replace(request, origin=replace(request.origin, kind="extension"), door="note")
        elif mode == "forge-role":
            request = replace(request, message=replace(request.message, role="user"))
        elif mode == "forge-tool":
            request = replace(request, message=replace(request.message, tool_call_id="other-call"))
        elif mode == "forge-media":
            blocks = copy.deepcopy(request.message.content)
            blocks[-1]["source"] = {"type": "url", "url": "changed"}
            request = replace(request, message=replace(request.message, content=blocks))
        elif mode == "forge-count":
            request = replace(request, message=replace(request.message, content=[]))
        else:
            blocks = [{**block, "text": block["text"].replace("13800138000", "[phone]")} if block.get("type") == "text" else block for block in request.message.content]
            request = replace(request, message=replace(request.message, content=blocks))
        result = await next_handler(Event(event.event_type, request))
        if mode == "mutate-receipt":
            result.message.content[0]["text"] = "forged after commit"
        return result

def create_extension():
    return Extension()
'''


async def _install_policy(history_host, tmp_path, monkeypatch):
    host, _ = history_host
    output = tmp_path / "observed.jsonl"
    monkeypatch.setenv("STRUCTURED_HISTORY_OUTPUT", str(output))
    package = write_package(tmp_path / "structured", package_id="structured.policy", module_source=POLICY)
    assert (await host.manager.install([package]))[0].status == "SUCCESS"
    return output


@pytest.mark.asyncio
async def test_structured_note_has_identical_message_and_disk_identity_on_retry(history_host):
    _, sessions = history_host
    note = {"id": "structured-note", "message": {"type": "assistant", "content": [
        {"type": "text", "text": "first "}, {"type": "text", "text": "second"},
    ]}}
    first = (await _query(history_host, message=note))["receipt"]
    retry = (await _query(history_host, message=note))["receipt"]
    assert first["status"] == "persisted" and retry["status"] == "duplicate"
    assert first["uuid"] == retry["uuid"] == _rows(sessions)[0]["id"]
    assert first["uuid"] != note["id"] and first["message_id"] == note["id"]
    assert first["message"] == retry["message"] == first["history_record"]["transcript_message"]
    assert first["message"]["content"] == note["message"]["content"]
    assert first["message"]["is_meta"] is True and first["history_record"]["content"] == "first second"
    assert (await _query(history_host))["page"]["messages"][0] == first["history_record"]


@pytest.mark.asyncio
async def test_structured_rejection_has_no_message_uuid_or_disk_row(history_host):
    _, sessions = history_host
    result = await _query(history_host, message={"id": "refused", "message": {"type": "system", "content": [{"type": "text", "text": "deny"}]}})
    assert result["receipt"]["status"] == "rejected"
    assert all(result["receipt"][key] is None for key in ("uuid", "message", "history_record"))
    assert not _rows(sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    {"type": "tool", "content": "tool"},
    {"type": "user", "role": "assistant", "content": "bad"},
    {"type": "assistant", "tool_call_id": "forged", "content": "bad"},
    {"type": "assistant", "tool_calls": [], "content": "bad"},
    {"type": "user", "content": [{"type": "image", "source": {"url": "x"}}]},
    {"type": "assistant", "content": [{"type": "text", "text": 1}]},
    {"type": "assistant", "content": "bad", "session_id": "other"},
    {"type": "assistant", "content": "bad", "is_meta": 1},
])
async def test_structured_note_rejects_spoofed_or_invalid_fields(history_host, message):
    _, sessions = history_host
    assert (await _query(history_host, message={"id": "bad", "message": message}))["error"] == "INVALID_MESSAGE"
    assert not _rows(sessions)


@pytest.mark.asyncio
async def test_tool_result_rewrite_updates_rendering_and_preserves_protected_record(history_host, tmp_path, monkeypatch):
    output = await _install_policy(history_host, tmp_path, monkeypatch)
    _, sessions = history_host
    await run_history_io(append_history_record, session_id="hist-a", request_id="tool-row", channel_id="web",
                         role="assistant", content="", timestamp=123.0, event_type="chat.tool_result",
                         extra={"tool_call_id": "call-1", "name": "lookup", "result": {"phone": "13800138000"},
                                "rendered_result": "phone=13800138000", "private_fact": {"fixed": True}})
    row = _rows(sessions)[0]
    observed = json.loads(output.read_text(encoding="utf-8"))
    assert observed["door"] == "tool-result" and observed["origin"] == {"kind": "tool", "name": "lookup", "entry": "history"}
    assert observed["message"]["type"] == observed["message"]["role"] == "tool"
    assert observed["message"]["tool_call_id"] == row["tool_call_id"] == "call-1"
    assert observed["uuid"] == row["uuid"] and row["timestamp"] == 123.0
    assert row["rendered_result"] == row["content"] == "phone=[phone]"
    assert row["transcript_message"]["content"] == [{"type": "text", "text": "phone=[phone]"}]
    assert row["result"] == {"phone": "13800138000"} and row["private_fact"] == {"fixed": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["forge-id", "forge-origin", "forge-role", "forge-tool", "forge-media", "forge-count"])
async def test_protected_fields_and_media_tampering_are_isolated(history_host, tmp_path, monkeypatch, mode):
    await _install_policy(history_host, tmp_path, monkeypatch)
    _, sessions = history_host
    content = [{"type": "text", "text": "13800138000"}, {"type": "image", "source": {"type": "url", "url": "original"}}]
    await run_history_io(append_history_record, session_id="hist-a", request_id="protected", channel_id="web",
                         role="assistant", content="original display", timestamp=123.0, event_type="chat.tool_result",
                         extra={"name": mode, "tool_call_id": "call-pinned", "result": "original raw result", "rendered_result": content})
    row = _rows(sessions)[0]
    assert row["id"] == "protected:assistant" and row["tool_call_id"] == "call-pinned"
    assert row["content"] == "original display" and row["rendered_result"] == content
    assert row["transcript_message"]["role"] == "tool"
    assert row["transcript_message"]["content"] == content


@pytest.mark.asyncio
async def test_receipt_mutation_does_not_replace_committed_structured_message(history_host, tmp_path, monkeypatch):
    await _install_policy(history_host, tmp_path, monkeypatch)
    _, sessions = history_host
    await run_history_io(append_history_record, session_id="hist-a", request_id="receipt", channel_id="web", role="assistant",
                         content="", timestamp=123.0, event_type="chat.tool_result",
                         extra={"name": "mutate-receipt", "tool_call_id": "call-1", "result": "original raw result", "rendered_result": "13800138000"})
    assert _rows(sessions)[0]["transcript_message"]["content"][0]["text"] == "[phone]"


def test_native_base_messages_keep_media_tool_calls_and_private_metadata():
    from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage

    image = {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}}
    native = UserMessage(content=[{"type": "text", "text": "look"}, image], metadata={"host_only": "kept"})
    view = message_from_base_message(native)
    assert view.type == "user" and view.content[1] == image and "metadata" not in view.to_dict()
    view.content[1]["image_url"]["url"] = "changed"
    assert native.content[1] == image and native.metadata == {"host_only": "kept"}
    assistant = AssistantMessage(content="", tool_calls=[{"id": "call-1", "type": "function", "name": "lookup", "arguments": "{}"}])
    projected = message_from_base_message(assistant)
    assert projected.tool_calls[0]["id"] == "call-1"
    tool = message_from_base_message(ToolMessage(content="result", tool_call_id="call-1"))
    assert tool.type == tool.role == "tool" and tool.tool_call_id == "call-1"


def test_rewrite_cannot_modify_unknown_blocks_or_tool_call_arguments():
    from dataclasses import replace

    original = message_from_dict({"type": "assistant", "content": [{"type": "vendor", "payload": {"key": "original"}}],
                                  "tool_calls": [{"id": "call-1", "arguments": "{}"}]})
    changed = copy.deepcopy(original)
    changed.content[0]["payload"]["key"] = "changed"
    assert not valid_message_rewrite(original, changed)
    assert not valid_message_rewrite(original, replace(original, tool_calls=[{"id": "call-1", "arguments": "changed"}]))


@pytest.mark.asyncio
async def test_each_platform_row_has_unique_uuid_without_changing_product_group_id(history_host):
    _, sessions = history_host
    for body in ("first", "second"):
        await run_history_io(append_history_record, session_id="hist-a", request_id="one-request", channel_id="web",
                             role="assistant", content=body, timestamp=123.0, event_type="chat.final")
    rows = _rows(sessions)
    assert len(rows) == 2 and rows[0]["id"] == rows[1]["id"] == "one-request:assistant"
    assert rows[0]["uuid"] != rows[1]["uuid"]


@pytest.mark.asyncio
async def test_native_tool_message_is_intercepted_before_jsonl_and_merged_without_mutating_caller(history_host, tmp_path, monkeypatch):
    from openjiuwen.core.foundation.llm import ToolMessage

    output = await _install_policy(history_host, tmp_path, monkeypatch)
    _, sessions = history_host
    native = ToolMessage(content="phone=13800138000", name="lookup", tool_call_id="native-call", metadata={"host_only": "fixed"})
    await run_history_io(append_history_record, session_id="hist-a", request_id="native-tool", channel_id="web",
                         role="tool", content=native, timestamp=123.0)
    row = _rows(sessions)[0]
    assert json.loads(output.read_text(encoding="utf-8"))["message"]["tool_call_id"] == "native-call"
    assert row["native_message"]["content"] == row["rendered_result"] == "phone=[phone]"
    assert row["native_message"]["metadata"] == {"host_only": "fixed"}
    assert row["native_message"]["tool_call_id"] == row["tool_call_id"] == "native-call"
    assert native.content == "phone=13800138000" and native.metadata == {"host_only": "fixed"}


@pytest.mark.asyncio
async def test_native_multimodal_user_message_preserves_blocks_on_the_platform_write_path(history_host):
    from openjiuwen.core.foundation.llm import UserMessage

    _, sessions = history_host
    blocks = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "https://example.invalid/p.png"}}]
    native = UserMessage(content=blocks, metadata={"fixed": True})
    await run_history_io(append_history_record, session_id="hist-a", request_id="native-user", channel_id="web",
                         role="user", content=native, timestamp=123.0)
    row = _rows(sessions)[0]
    assert row["transcript_message"]["content"] == blocks
    assert row["native_message"]["content"] == blocks and row["native_message"]["metadata"] == {"fixed": True}
    assert row["content"] == "look"


def test_read_only_json_fields_reject_bool_int_type_substitution():
    from dataclasses import replace

    original = message_from_dict({"type": "assistant", "content": [{"type": "vendor", "count": 1}], "is_meta": True})
    assert not valid_message_rewrite(original, replace(original, is_meta=1))
    assert not valid_message_rewrite(original, replace(original, content=[{"type": "vendor", "count": True}]))


@pytest.mark.asyncio
async def test_native_assistant_with_explicit_none_event_type_gets_final_mapping(history_host):
    from openjiuwen.core.foundation.llm import AssistantMessage

    _, sessions = history_host
    await run_history_io(append_history_record, session_id="hist-a", request_id="native-assistant", channel_id="web",
                         role="assistant", content=AssistantMessage(content="answer"), timestamp=123.0, event_type=None)
    row = _rows(sessions)[0]
    assert row["event_type"] == "chat.final"
    assert row["transcript_message"]["type"] == "assistant" and row["native_message"]["content"] == "answer"
