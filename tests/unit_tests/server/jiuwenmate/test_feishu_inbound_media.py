"""Feishu trigger images must remain bound to the current message and execution."""
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from jiuwenswarm.server.jiuwenmate import feishu_inbound_media as media


def message(kind="post", content=None, **updates):
    return {"message_id": "om_test", "chat_id": "oc_test", "deleted": False,
            "msg_type": kind, "update_time": "1", "body": {"content": json.dumps(content or {
                "content": [[{"tag": "text", "text": "img_fake"},
                             {"tag": "img", "image_key": "img_one"},
                             {"tag": "img", "image_key": "img_one"}]]})}, **updates}


def test_only_structured_images_are_selected_in_post_order():
    refs = ("feishu:attachment:om_test:img_one",)
    assert media.image_keys(message(), "om_test", "oc_test", refs) == ("img_one", "img_one")
    assert media.image_keys(message("text", {"text": "img_one"}), "om_test", "oc_test", refs) == ()


@pytest.mark.parametrize("updates", [{"chat_id": "other"}, {"deleted": True}, {"message_id": "other"}])
def test_images_from_a_different_or_recalled_message_are_rejected(updates):
    with pytest.raises(media.MediaUnavailable):
        media.image_keys(message(**updates), "om_test", "oc_test", ("feishu:attachment:om_test:img_one",))


def test_unregistered_image_cannot_be_downloaded():
    with pytest.raises(media.MediaUnavailable):
        media.image_keys(message(), "om_test", "oc_test", ())


def png():
    result = io.BytesIO()
    Image.new("RGB", (20, 10), "red").save(result, format="PNG")
    return result.getvalue()


@pytest.mark.asyncio
async def test_download_deduplicates_files_rechecks_source_and_cleans_up(tmp_path):
    current = message()
    downloaded = []
    def serve(request):
        if request.url.path.endswith("/resources/img_one"):
            downloaded.append(request.url.path)
            return httpx.Response(200, content=png(), headers={"content-type": "image/png"})
        return httpx.Response(200, json={"code": 0, "data": {"items": [current]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        scope = media.FeishuInboundImages(client, "om_test", "oc_test", ("feishu:attachment:om_test:img_one",), tmp_path)
        await scope.prepare()
        assert len(downloaded) == 1
        assert scope.references == ("feishu-image:1", "feishu-image:2")
        path = await scope.resolve("feishu-image:1")
        assert Path(path).read_bytes() == png()
        with pytest.raises(media.MediaUnavailable):
            await scope.resolve("https://example.com/secret.png")
        current["deleted"] = True
        with pytest.raises(media.MediaUnavailable):
            await scope.resolve("feishu-image:2")
        await scope.aclose()
        assert not Path(path).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("data,limit", [(b"broken image", 100), (png(), 8)])
async def test_invalid_or_oversized_images_leave_no_files(tmp_path, data, limit):
    def serve(request):
        if "/resources/" in request.url.path:
            return httpx.Response(200, content=data)
        return httpx.Response(200, json={"code": 0, "data": {"items": [message("image", {"image_key": "img_one"})]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        scope = media.FeishuInboundImages(client, "om_test", "oc_test", ("feishu:attachment:om_test:img_one",), tmp_path, max_bytes=limit)
        await scope.prepare()
        assert not scope.references
        assert scope.notice
        await scope.aclose()
        assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_vision_tool_uses_scoped_reference_and_refuses_office_files(tmp_path):
    from openjiuwen.harness.tools.multimodal.vision import VisualQuestionAnsweringTool
    from jiuwenswarm.server.jiuwenmate.vision_runtime import _OfficeImageTool
    current = message("image", {"image_key": "img_one"})
    def serve(request):
        if "/resources/" in request.url.path:
            return httpx.Response(200, content=png())
        return httpx.Response(200, json={"code": 0, "data": {"items": [current]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        scope = media.FeishuInboundImages(client, "om_test", "oc_test", ("feishu:attachment:om_test:img_one",), tmp_path)
        await scope.prepare()
        base = VisualQuestionAnsweringTool()
        received = []
        async def invoke(inputs, **kwargs):
            received.append(inputs)
            return "recognized"
        base.invoke = invoke
        tool = _OfficeImageTool(base, tmp_path, image_scope=scope)
        assert await tool.invoke({"image_path_or_url": "feishu-image:1", "question": "what?"}) == "recognized"
        assert Path(received[0]["image_path_or_url"]).is_file()
        with pytest.raises(media.MediaUnavailable):
            await tool.invoke({"image_path_or_url": str(tmp_path / "other.png"), "question": "what?"})
        assert len(received) == 1
        await scope.aclose()


def test_localized_post_uses_one_language_and_ignores_links():
    content = {"zh_cn": {"content": [[{"tag": "a", "href": "img_untrusted"},
        {"tag": "img", "image_key": "img_one"}]]}, "en_us": {"content": [[{"tag": "img", "image_key": "img_other"}]]}}
    assert media.image_keys(message(content=content), "om_test", "oc_test", ("feishu:attachment:om_test:img_one",)) == ("img_one",)


@pytest.mark.asyncio
async def test_failed_picture_keeps_other_picture_number(tmp_path):
    current = message(content={"content": [[{"tag": "img", "image_key": "img_bad"}, {"tag": "img", "image_key": "img_one"}]]})
    def serve(request):
        if request.url.path.endswith("/resources/img_bad"):
            return httpx.Response(403, json={"code": 1})
        if request.url.path.endswith("/resources/img_one"):
            return httpx.Response(200, content=png())
        return httpx.Response(200, json={"code": 0, "data": {"items": [current]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        scope = media.FeishuInboundImages(client, "om_test", "oc_test", tuple(f"feishu:attachment:om_test:{key}" for key in ("img_bad", "img_one")), tmp_path)
        await scope.prepare()
        assert scope.references == ("feishu-image:2",)
        assert "图 1 读取失败" in scope.guidance()
        assert Path(await scope.resolve("feishu-image:2")).is_file()
        await scope.aclose()


@pytest.mark.asyncio
async def test_cancel_during_download_cleans_private_directory(tmp_path):
    import asyncio
    entered = asyncio.Event()
    class BlockedImage(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""
    async def serve(request):
        if "/resources/" in request.url.path:
            return httpx.Response(200, stream=BlockedImage())
        return httpx.Response(200, json={"code": 0, "data": {"items": [message("image", {"image_key": "img_one"})]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        scope = media.FeishuInboundImages(client, "om_test", "oc_test", ("feishu:attachment:om_test:img_one",), tmp_path)
        task = asyncio.create_task(scope.prepare())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,namespace,expected", [(False, "local", "视觉模型尚未配置"), (True, "other", "绑定不可用")])
async def test_preparation_reports_missing_model_or_wrong_agent_binding(tmp_path, monkeypatch, configured, namespace, expected):
    from types import SimpleNamespace
    from jiuwenswarm.common import config as config_module
    from jiuwenswarm.server.jiuwenmate import vision_runtime
    monkeypatch.setattr(config_module, "get_config", lambda: {"channels": {"feishu": {"apps": [
        {"app_id": "app", "persistent_agent_id": "agent", "app_secret": "test-secret"}]}}})
    monkeypatch.setattr(vision_runtime, "vision_config_from_env", lambda: object() if configured else None)
    inputs = SimpleNamespace(kind="group_message", trigger_message_id="om_test",
                             attachment_refs=("feishu:attachment:om_test:img_one",))
    record = SimpleNamespace(namespace_id=namespace, agent_id="agent", session_id="session", request_id="request", request_input=inputs,
        route_snapshot=SimpleNamespace(channel="feishu", app_id="app", target_id="oc_test"))
    scope = await media.prepare_feishu_images(record, tmp_path)
    assert expected in scope.guidance()
    assert not scope.references
    await scope.aclose()
    assert scope.client.is_closed


@pytest.mark.asyncio
async def test_driver_hands_image_guidance_to_existing_request_and_closes_scope(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from jiuwenswarm.server.jiuwenmate import execution_driver
    from jiuwenswarm.server.jiuwenmate.execution_models import ConversationRef, RequestRouteSnapshot
    from openjiuwen.harness import persistent_agent as core
    from tests.unit_tests.server.jiuwenmate.test_session_scheduler import Host, NS, NOW, ACCESS, settle, stub_invocation_builder
    stub_invocation_builder(monkeypatch)
    trigger = core.GroupMessage(message_id="om_test", sender_id="human", sender_name="Tester", sender_kind="human",
        received_at=NOW, text="Describe image", attachment_refs=("feishu:attachment:om_test:img_one",))
    class Port:
        message = trigger
        def set_related_agents(self, agents):
            self.related_agents = agents
        async def current_trigger(self, execution, mid):
            assert mid == "om_test"
            return self.message
    port = Port()
    monkeypatch.setattr(execution_driver, "HostGroupChatPort", lambda *args, **kwargs: port)
    class Scope:
        closed = False
        def guidance(self): return "图 1：feishu-image:1"
        async def aclose(self): self.closed = True
    scope = Scope()
    seen = []
    async def prepare(record, workspace):
        seen.append(record.request_input)
        return scope
    monkeypatch.setattr(media, "prepare_feishu_images", prepare)
    host = Host(tmp_path)
    # Group sharing does not change current-trigger reads.
    await host.manager.start()
    try:
        session = host.sessions.resolve_session(NS, "mate_dev", ConversationRef.group("group-1"), now=NOW)
        state, _ = await host.scheduler.accept(session, snapshot=host.service.snapshot(NS, "mate_dev", "v1"),
            source_ref="source-image", access=ACCESS, request_input=core.RequestInput(kind="group_message",
            text=trigger.text, source=trigger, reply_target="group-1", attachment_refs=trigger.attachment_refs),
            route_snapshot=RequestRouteSnapshot(channel="feishu", app_id="app", target_id="oc_test", source_message_id="om_test"), received_at=NOW)
        await settle(lambda: bool(host.core.started) or host.status(state)[0] == "ended")
        assert host.core.started, host.status(state)
        text = host.core.started[0]
        assert "feishu-image:1" in text
        assert seen[0].source.message_id == "om_test"
        assert host.core.contexts[text].extras[media.SCOPE_KEY] is scope
        from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
        port.message = None
        with pytest.raises(PersistentAgentError, match="图片原消息已不可读"):
            await scope.source_guard()
        host.core.release(text)
        await settle(lambda: host.status(state)[0] == "ended")
        assert scope.closed
    finally:
        await host.manager.aclose()


@pytest.mark.asyncio
async def test_recovery_refuses_a_changed_message_revision(tmp_path):
    current = message("image", {"image_key": "img_one"})
    def serve(request):
        if "/resources/" in request.url.path:
            return httpx.Response(200, content=png())
        return httpx.Response(200, json={"code": 0, "data": {"items": [current]}})
    manifest = tmp_path / "sources" / "request.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        def create():
            return media.FeishuInboundImages(client, "om_test", "oc_test", ("feishu:attachment:om_test:img_one",), tmp_path / "files", manifest_path=manifest)
        first = create()
        await first.prepare()
        assert first.references
        await first.aclose()
        unchanged = create()
        await unchanged.prepare()
        assert unchanged.references == first.references
        await unchanged.aclose()
        current["update_time"] = "2"
        changed = create()
        await changed.prepare()
        assert not changed.references
        assert "修改" in changed.notice
        await changed.aclose()


@pytest.mark.asyncio
async def test_slow_vision_service_returns_a_bounded_failure(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from openjiuwen.harness.tools.multimodal.vision import VisualQuestionAnsweringTool
    from jiuwenswarm.server.jiuwenmate.vision_runtime import _OfficeImageTool
    monkeypatch.setenv("VISION_REQUEST_TIMEOUT_SECONDS", "0.01")
    async def resolve(reference): return str(tmp_path / "test.png")
    base = VisualQuestionAnsweringTool()
    async def invoke(inputs, **kwargs): await asyncio.Event().wait()
    base.invoke = invoke
    tool = _OfficeImageTool(base, tmp_path, image_scope=SimpleNamespace(resolve=resolve))
    with pytest.raises(ValueError, match="视觉服务响应超时"):
        await asyncio.wait_for(tool.invoke({"image_path_or_url": "feishu-image:1", "question": "what?"}), .2)
