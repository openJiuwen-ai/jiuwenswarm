from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.server.im.im_connector.types import ImMessage
import pytest

from jiuwenswarm.server.im.im_hosting.reply_bridge import (
    build_inbound_prompt,
    extract_reply_text,
    generate_reply_via_expert,
    hosting_session_id,
    inbound_display_text,
    load_hosting_history,
    resolve_target_expert,
)


def test_build_inbound_prompt_contains_sender_and_body():
    msg = ImMessage(
        channel_id="feishu",
        msg_id="m1",
        conversation_external_id="oc_x",
        sender_name="许康",
        content_text="入职流程？",
        sent_at=1,
    )
    text = build_inbound_prompt(
        msg,
        target_title="测试群",
        target_kind="group",
        persona="## 人设\n- 语气：简短\n\n## 职责\n- 代回入职",
    )
    assert "许康" in text
    assert "入职流程" in text
    assert "测试群" in text
    assert "## 人设" in text
    assert "代回入职" in text


def test_extract_reply_text_from_content():
    resp = AgentResponse(
        request_id="r1",
        channel_id="im_hosting",
        ok=True,
        payload={"content": "  好的，我来帮你  "},
    )
    assert extract_reply_text(resp) == "好的，我来帮你"


def test_hosting_session_id_stable():
    assert hosting_session_id("tid-1") == "im_hosting.tid-1"


def test_resolve_target_expert_reads_hosting_record():
    service_id, agent_id = resolve_target_expert(
        {"expert_service_id": "service_default", "expert_agent_id": "agent_default"}
    )
    assert service_id == "service_default"
    assert agent_id == "agent_default"


def test_resolve_target_expert_defaults_personal_ids():
    assert resolve_target_expert({}) == ("default", "default")


def test_inbound_display_text_prefers_original_message():
    msg = ImMessage(
        channel_id="feishu",
        msg_id="m1",
        conversation_external_id="oc_x",
        sender_name="许康",
        content_text="入职流程？",
        sent_at=1,
    )
    assert inbound_display_text(msg) == "许康: 入职流程？"


@pytest.mark.asyncio
async def test_generate_reply_uses_target_expert_on_runtime():
    class _FakeRuntime:
        def __init__(self) -> None:
            self.request = None

        async def process_message(self, request):
            self.request = request
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"content": "已收到，我来跟进入职。"},
            )

    runtime = _FakeRuntime()
    msg = ImMessage(
        channel_id="feishu",
        msg_id="m1",
        conversation_external_id="oc_x",
        sender_name="许康",
        content_text="入职流程？",
        sent_at=1,
    )
    text = await generate_reply_via_expert(
        runtime,
        target={
            "id": "tid-1",
            "title": "测试群",
            "target_kind": "group",
            "expert_service_id": "svc-a",
            "expert_agent_id": "agt-b",
            "expert_persona": "## 人设\n- 语气：简短",
        },
        message=msg,
    )
    assert text == "已收到，我来跟进入职。"
    assert runtime.request is not None
    assert runtime.request.service_id == "svc-a"
    assert runtime.request.agent_id == "agt-b"
    assert runtime.request.session_id == "im_hosting.tid-1"
    params = runtime.request.params
    assert "system_prompt" not in params
    assert "## 人设" in params["query"]
    assert "入职流程" in params["query"]
    assert params["content"] == params["query"]


class _Chunk:
    def __init__(self, payload: dict, *, is_complete: bool = False) -> None:
        self.payload = payload
        self.is_complete = is_complete


@pytest.mark.asyncio
async def test_generate_reply_uses_history_final_after_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.im.im_hosting.reply_bridge.resolve_tenant_sessions_dir",
        lambda *args, **kwargs: tmp_path,
    )

    class _StreamRuntime:
        async def process_message_stream(self, request):
            session_dir = tmp_path / request.session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "history.json").write_text(
                (
                    '{"role":"user","request_id":"%s","content":"许康: 中秋干什么"}\n'
                    '{"role":"assistant","request_id":"%s","event_type":"chat.final",'
                    '"content":"还没定呢，赏月吧"}\n'
                )
                % (request.request_id, request.request_id),
                encoding="utf-8",
            )
            yield _Chunk({"event_type": "chat.delta", "content": "还没定呢"})
            yield _Chunk({"event_type": "chat.final", "content": ""}, is_complete=True)

        async def process_message(self, request):
            raise AssertionError("stream already ran, must not fall back to unary")

    text = await generate_reply_via_expert(
        _StreamRuntime(),
        target={
            "id": "tid-1",
            "title": "测试群",
            "target_kind": "group",
            "expert_service_id": "default",
            "expert_agent_id": "default",
        },
        message=ImMessage(
            channel_id="dingtalk",
            msg_id="m1",
            conversation_external_id="cid",
            sender_name="许康",
            content_text="中秋干什么",
            sent_at=1,
        ),
    )
    assert text == "还没定呢，赏月吧"


def test_load_hosting_history_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.im.im_hosting.reply_bridge.resolve_tenant_sessions_dir",
        lambda *args, **kwargs: tmp_path / "sessions",
    )
    payload = load_hosting_history(
        {
            "id": "tid-1",
            "expert_service_id": "svc-a",
            "expert_agent_id": "agt-b",
        }
    )
    assert payload["session_id"] == "im_hosting.tid-1"
    assert payload["messages"] == []
    assert payload["expert_service_id"] == "svc-a"
