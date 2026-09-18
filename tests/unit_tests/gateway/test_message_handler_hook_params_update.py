"""before_chat_request 钩子改写 params 后实时回推前端的单测（issue #2792）。"""

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler


class _FakeAgentClient:
    @staticmethod
    async def send_request(env):  # noqa: ANN001, ARG004
        raise AssertionError("unexpected non-stream request in this test")

    @staticmethod
    async def send_request_stream(env):  # noqa: ANN001, ARG004
        return
        yield  # pragma: no cover


def _chat_send_message(params: dict[str, object]) -> Message:
    return Message(
        id="chat-send-1",
        type="req",
        channel_id="web",
        session_id="sess-1",
        params=params,
        timestamp=0,
        ok=True,
        req_method=ReqMethod.CHAT_SEND,
        is_stream=True,
    )


def _new_handler() -> MessageHandler:
    # 与兄弟测试一致：重置单例，避免拿到其它测试留下的实例（含队列残留）。
    setattr(MessageHandler, "_instance", None)
    return MessageHandler(_FakeAgentClient())


async def _trigger_hook_and_collect(msg: Message, monkeypatch, mutate):  # noqa: ANN001
    """把扩展注册表替换为直接改 params 的假钩子，跑完「快照 → 钩子 → 回推检测」，
    返回 robot_messages 队列内容。"""

    async def _fake_trigger(event, ctx, **kwargs):  # noqa: ANN001, ARG001
        mutate(ctx.params)

    monkeypatch.setattr(
        ExtensionRegistry,
        "get_instance",
        lambda: SimpleNamespace(trigger=_fake_trigger),
    )

    handler = _new_handler()
    pre = handler._snapshot_hook_watch_params(msg)  # noqa: SLF001
    await handler._trigger_before_chat_request_hook(msg)  # noqa: SLF001
    await handler._publish_hook_params_update_if_changed(msg, pre)  # noqa: SLF001
    out = []
    while not handler._robot_messages.empty():  # noqa: SLF001
        out.append(handler._robot_messages.get_nowait())  # noqa: SLF001
    return out


@pytest.mark.asyncio
async def test_query_replaced_by_hook_pushes_update(monkeypatch):  # noqa: ANN001
    msg = _chat_send_message({"query": "rm -rf /"})

    out = await _trigger_hook_and_collect(
        msg, monkeypatch, lambda p: p.__setitem__("query", "您的消息因安全策略被拦截")
    )

    assert len(out) == 1
    event = out[0]
    assert event.event_type == EventType.CHAT_MESSAGE_UPDATED
    assert event.id == "chat-send-1"
    assert event.session_id == "sess-1"
    assert event.payload["event_type"] == "chat.message_updated"
    assert event.payload["request_id"] == "chat-send-1"
    assert event.payload["updates"] == {"query": "您的消息因安全策略被拦截"}


@pytest.mark.asyncio
async def test_model_name_changed_by_hook_pushes_update(monkeypatch):  # noqa: ANN001
    msg = _chat_send_message({"query": "写个脚本", "model_name": "default-model"})

    out = await _trigger_hook_and_collect(
        msg, monkeypatch, lambda p: p.__setitem__("model_name", "coder-model")
    )

    assert len(out) == 1
    assert out[0].payload["updates"] == {"model_name": "coder-model"}


@pytest.mark.asyncio
async def test_both_changed_push_single_update(monkeypatch):  # noqa: ANN001
    msg = _chat_send_message({"query": "rm -rf /", "model_name": "default-model"})

    def mutate(p):  # noqa: ANN001
        p["query"] = "您的消息因安全策略被拦截"
        p["model_name"] = "coder-model"

    out = await _trigger_hook_and_collect(msg, monkeypatch, mutate)

    assert len(out) == 1
    assert out[0].payload["updates"] == {
        "query": "您的消息因安全策略被拦截",
        "model_name": "coder-model",
    }


@pytest.mark.asyncio
async def test_unchanged_params_push_nothing(monkeypatch):  # noqa: ANN001
    msg = _chat_send_message({"query": "hello"})

    out = await _trigger_hook_and_collect(msg, monkeypatch, lambda p: None)

    assert out == []


@pytest.mark.asyncio
async def test_non_dict_params_push_nothing(monkeypatch):  # noqa: ANN001
    msg = _chat_send_message({"query": "hello"})
    msg.params = None  # 模拟异常入参

    out = await _trigger_hook_and_collect(msg, monkeypatch, lambda p: None)

    assert out == []


@pytest.mark.asyncio
async def test_non_chat_method_skips_hook_and_update(monkeypatch):  # noqa: ANN001
    called = {"trigger": False}

    def mutate(p):  # noqa: ANN001
        called["trigger"] = True
        p["query"] = "改写"

    msg = _chat_send_message({"query": "hello"})
    msg.req_method = ReqMethod.HISTORY_GET

    out = await _trigger_hook_and_collect(msg, monkeypatch, mutate)

    assert out == []
    assert called["trigger"] is False
