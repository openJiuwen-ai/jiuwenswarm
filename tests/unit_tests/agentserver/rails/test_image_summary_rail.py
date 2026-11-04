# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ImageSummaryRail 单元测试.

覆盖：after_tool_call 快路径（read_file 返回 multimodal image item——base64
在场——立即调度，key 与 metadata 一致）；after_invoke 兜底门控（仅当轮主模型
多模态——model input 带 image——才 schedule）；主模型凭证要求；摘要模型端点与
主对话同通道（按 api_base 分流：np:// 注入命名管道 transport + proxyKey 优先，
HTTP 原样直出且 proxyKey 绝不外发）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from openjiuwen.core.context_engine.image_refs import set_image_summary_provider

from jiuwenswarm.agents.harness.common.image_summary import ImageSummaryScheduler
from jiuwenswarm.agents.harness.common.rails.image_summary_rail import ImageSummaryRail


def _credential(*, api_key: str = "k", api_base: str = "http://x", model: str = "m"):
    return SimpleNamespace(api_key=api_key, api_base=api_base, model=model)


def _model(credential):
    return SimpleNamespace(
        model_client_config=credential,
        model_config=SimpleNamespace(model_name=credential.model),
    )


def _agent(*, declared: Any, credential=None):
    model = _model(credential) if credential else None
    deep_config = SimpleNamespace(model=model)
    return SimpleNamespace(
        deep_config=deep_config,
        _deep_config=deep_config,
        _jiuwenswarm_declared_image_support=declared,
    )


def _ctx(*, messages=None, session_id="s1", agent=None):
    context = SimpleNamespace(
        get_messages=lambda: messages or [],
        session_id=lambda: session_id,
    )
    return SimpleNamespace(context=context, agent=agent)


@pytest.fixture(autouse=True)
def _reset_image_summary_provider():
    """ImageSummaryRail 构造时注册进程级 provider，前后复位避免污染其他测试。"""
    set_image_summary_provider(None)
    yield
    set_image_summary_provider(None)


@pytest.mark.asyncio
async def test_skip_when_declared_image_support_false():
    """非多模态模型（False）不 schedule。"""
    rail = ImageSummaryRail()
    agent = _agent(declared=False, credential=_credential())
    ctx = _ctx(agent=agent, messages=[SimpleNamespace()])
    with mock.patch.object(rail._scheduler, "schedule", return_value=0) as schedule:
        await rail.after_invoke(ctx)
        schedule.assert_not_called()


@pytest.mark.asyncio
async def test_skip_when_declared_image_support_none():
    """未知（None）保守不 schedule。"""
    rail = ImageSummaryRail()
    agent = _agent(declared=None, credential=_credential())
    ctx = _ctx(agent=agent, messages=[SimpleNamespace()])
    with mock.patch.object(rail._scheduler, "schedule", return_value=0) as schedule:
        await rail.after_invoke(ctx)
        schedule.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_when_multimodal_with_main_model_credential():
    """多模态 + 无独立 vision → 用当轮主模型凭证 schedule。"""
    rail = ImageSummaryRail()
    credential = _credential()
    agent = _agent(declared=True, credential=credential)
    ctx = _ctx(agent=agent, messages=[SimpleNamespace()])
    ref = SimpleNamespace(key="k1", path="/tmp/a.png", width=None, height=None)
    with mock.patch(
        "jiuwenswarm.agents.harness.common.rails.image_summary_rail.collect_image_refs",
        return_value=[ref],
    ), mock.patch.object(rail._scheduler, "schedule", return_value=2) as schedule:
        await rail.after_invoke(ctx)
        schedule.assert_called_once()
        assert schedule.call_args.kwargs["model_credential"] is not None


@pytest.mark.asyncio
async def test_skip_when_multimodal_but_no_credential():
    """多模态但主模型凭证不全 → 不 schedule。"""
    rail = ImageSummaryRail()
    agent = _agent(declared=True, credential=None)
    ctx = _ctx(agent=agent, messages=[SimpleNamespace()])
    with mock.patch.object(rail._scheduler, "schedule", return_value=0) as schedule:
        await rail.after_invoke(ctx)
        schedule.assert_not_called()


@pytest.mark.asyncio
async def test_skip_when_no_images():
    """无图片不 schedule（即使多模态）。"""
    rail = ImageSummaryRail()
    agent = _agent(declared=True, credential=_credential())
    ctx = _ctx(agent=agent, messages=[])
    with mock.patch.object(rail._scheduler, "schedule", return_value=0) as schedule:
        await rail.after_invoke(ctx)
        schedule.assert_not_called()


def test_scheduler_skips_without_model_credential():
    """无主模型凭证（无 base_url/model 可调）→ 不调度。"""
    from jiuwenswarm.agents.harness.common import image_summary as mod

    provider = SimpleNamespace(index_for=lambda sid: object())
    scheduler = mod.ImageSummaryScheduler(provider)
    refs = [SimpleNamespace(key="k1", path="/tmp/a.png", width=None, height=None)]
    assert scheduler.schedule("s1", refs, model_credential=None) == 0


class _FakeAsyncOpenAI:
    """记录构造参数并返回固定摘要的 AsyncOpenAI 桩。"""

    instances: list["_FakeAsyncOpenAI"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=mock.AsyncMock(
                    return_value=SimpleNamespace(
                        choices=[
                            SimpleNamespace(message=SimpleNamespace(content=" 截图，显示数字 42 "))
                        ]
                    )
                )
            )
        )
        _FakeAsyncOpenAI.instances.append(self)

    async def close(self) -> None:
        return None


class _FakeHttpClient:
    """记录构造参数的 httpx.AsyncClient 桩（避免真实构造/连接管道）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _patch_describe_io(monkeypatch, *, proxy_key: str | None):
    """打桩 _describe 的边界：图片编码、管道 transport、httpx、密钥包。"""
    from jiuwenswarm.agents.harness.common import image_summary as mod

    _FakeAsyncOpenAI.instances.clear()
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeAsyncOpenAI)
    monkeypatch.setattr("httpx.AsyncClient", _FakeHttpClient)
    monkeypatch.setattr(
        mod, "_encode_for_vision", lambda path: ("data:image/jpeg;base64,xx", 800, 600)
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.np_transport.named_pipe_transport_for",
        lambda base_url: ("pipe-transport", base_url),
    )
    monkeypatch.setattr(
        "jiuwenswarm.common.secrets_bootstrap.get_secret", lambda *a, **k: proxy_key
    )
    return mod


@pytest.mark.asyncio
async def test_describe_calls_np_pipe_model(monkeypatch):
    """api_base 为 np://：注入命名管道 transport，api_key 优先 proxyKey。"""
    mod = _patch_describe_io(monkeypatch, proxy_key="proxy-key")
    scheduler = mod.ImageSummaryScheduler.__new__(mod.ImageSummaryScheduler)
    credential = SimpleNamespace(
        api_key="fallback-key", base_url="np://claw-model/v1", model="m1"
    )

    summary, width, height = await scheduler._describe("/tmp/a.png", credential)

    assert summary == "截图，显示数字 42"
    assert (width, height) == (800, 600)
    (instance,) = _FakeAsyncOpenAI.instances
    assert instance.kwargs["base_url"] == "np://claw-model/v1"
    assert instance.kwargs["api_key"] == "proxy-key"
    http_client = instance.kwargs["http_client"]
    assert http_client.kwargs["trust_env"] is False
    assert http_client.kwargs["transport"] == ("pipe-transport", "np://claw-model/v1")
    create_call = instance.chat.completions.create.await_args
    assert create_call.kwargs["model"] == "m1"
    assert create_call.kwargs["stream"] is True
    content = create_call.kwargs["messages"][0]["content"]
    assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,xx"}}


@pytest.mark.asyncio
async def test_describe_api_key_falls_back_without_proxy_key(monkeypatch):
    """np 管道但密钥包无 proxyKey（非桌面形态）时 api_key 回退主模型凭证原值。"""
    mod = _patch_describe_io(monkeypatch, proxy_key=None)
    scheduler = mod.ImageSummaryScheduler.__new__(mod.ImageSummaryScheduler)
    credential = SimpleNamespace(
        api_key="fallback-key", base_url="np://claw-model/v1", model="m1"
    )

    await scheduler._describe("/tmp/a.png", credential)

    (instance,) = _FakeAsyncOpenAI.instances
    assert instance.kwargs["api_key"] == "fallback-key"


@pytest.mark.asyncio
async def test_describe_http_branch_keeps_plain_client(monkeypatch):
    """api_base 为 HTTP(S)：不注入管道 transport，proxyKey 绝不外发。"""
    mod = _patch_describe_io(monkeypatch, proxy_key="proxy-key")
    scheduler = mod.ImageSummaryScheduler.__new__(mod.ImageSummaryScheduler)
    credential = SimpleNamespace(api_key="fallback-key", base_url="https://x/v1", model="m1")

    await scheduler._describe("/tmp/a.png", credential)

    (instance,) = _FakeAsyncOpenAI.instances
    assert instance.kwargs["base_url"] == "https://x/v1"
    assert instance.kwargs["api_key"] == "fallback-key"
    assert instance.kwargs.get("http_client") is None


class _FakeStreamResponse:
    """模拟 SSE 流式响应：异步迭代 chunk（无 choices 属性）。"""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_describe_aggregates_stream_when_proxy_returns_sse(monkeypatch):
    """ModelProxy 透传 SSE：stream=True 聚合 delta.content 得到完整摘要。"""

    class _FakeStreamAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            chunks = [
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="截图，"))]),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="显示数字 42 "))]),
                SimpleNamespace(choices=[]),  # usage-only 尾包
            ]
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=mock.AsyncMock(return_value=_FakeStreamResponse(chunks))
                )
            )

        async def close(self) -> None:
            return None

    mod = _patch_describe_io(monkeypatch, proxy_key="proxy-key")
    monkeypatch.setattr("openai.AsyncOpenAI", _FakeStreamAsyncOpenAI)
    scheduler = mod.ImageSummaryScheduler.__new__(mod.ImageSummaryScheduler)
    credential = SimpleNamespace(
        api_key="fallback-key", base_url="np://claw-model/v1", model="m1"
    )

    summary, width, height = await scheduler._describe("/tmp/a.png", credential)

    assert summary == "截图，显示数字 42"
    assert (width, height) == (800, 600)


# ---------------------------------------------------------------------------
# after_tool_call 快路径（read_file 返回 multimodal image item 即调度）
# ---------------------------------------------------------------------------


def _tool_ctx(*, agent, tool_result, session_id="s1"):
    context = SimpleNamespace(
        get_messages=lambda: [],
        session_id=lambda: session_id,
    )
    return SimpleNamespace(
        context=context,
        agent=agent,
        inputs=SimpleNamespace(tool_result=tool_result),
    )


def _read_file_result(*, source_path="/tmp/a.png", data_url="data:image/png;base64,AAAA"):
    item = {
        "type": "image",
        "source": "read_file",
        "source_path": source_path,
        "data_url": data_url,
        "width": 800,
        "height": 600,
    }
    return SimpleNamespace(data={"content": "...", "multimodal": [item]})


@pytest.mark.asyncio
async def test_after_tool_call_schedules_read_file_image_immediately():
    """read_file 返回 multimodal image item（base64 在场）→ 立即调度，不看声明门控。"""
    from openjiuwen.core.context_engine.image_refs import image_key

    rail = ImageSummaryRail()
    agent = _agent(declared=None, credential=_credential())  # declared 未知也不阻塞快路径
    ctx = _tool_ctx(agent=agent, tool_result=_read_file_result())
    with mock.patch.object(rail._scheduler, "schedule", return_value=1) as schedule:
        await rail.after_tool_call(ctx)
        schedule.assert_called_once()
        (refs,) = schedule.call_args.args[1:] or [schedule.call_args[0][1]]
        (ref,) = refs
        assert ref.path == "/tmp/a.png"
        assert ref.key == image_key("data:image/png;base64,AAAA")
        assert (ref.width, ref.height) == (800, 600)
        assert schedule.call_args.kwargs["model_credential"] is not None


@pytest.mark.asyncio
async def test_after_tool_call_skips_when_no_multimodal_items():
    """工具结果无 multimodal 列表（enable_image_multimodal=False 或非图）→ 不调度。"""
    rail = ImageSummaryRail()
    agent = _agent(declared=True, credential=_credential())
    for tool_result in (
        None,
        SimpleNamespace(data={"content": "text"}),
        SimpleNamespace(data={"multimodal": [{"type": "audio"}]}),
        SimpleNamespace(data={"multimodal": [{"type": "image", "data_url": "https://x/y.png"}]}),
    ):
        ctx = _tool_ctx(agent=agent, tool_result=tool_result)
        with mock.patch.object(rail._scheduler, "schedule", return_value=0) as schedule:
            await rail.after_tool_call(ctx)
            schedule.assert_not_called()


@pytest.mark.asyncio
async def test_after_tool_call_uses_deep_agent_from_init():
    """桥接事件的 ctx.agent 是内层 ReActAgent（无 deep_config），凭证取 init 时的 DeepAgent。"""
    rail = ImageSummaryRail()
    deep_agent = _agent(declared=None, credential=_credential())
    rail.init(deep_agent)
    inner_react = SimpleNamespace()  # 内层 ReActAgent：没有 deep_config
    ctx = _tool_ctx(agent=inner_react, tool_result=_read_file_result())
    with mock.patch.object(rail._scheduler, "schedule", return_value=1) as schedule:
        await rail.after_tool_call(ctx)
        schedule.assert_called_once()
        assert schedule.call_args.kwargs["model_credential"] is not None


@pytest.mark.asyncio
async def test_after_tool_call_without_init_falls_back_to_ctx_agent():
    """未 init 时回退 ctx.agent（非桥接路径）。"""
    rail = ImageSummaryRail()
    agent = _agent(declared=None, credential=_credential())
    ctx = _tool_ctx(agent=agent, tool_result=_read_file_result())
    with mock.patch.object(rail._scheduler, "schedule", return_value=1) as schedule:
        await rail.after_tool_call(ctx)
        schedule.assert_called_once()
        assert schedule.call_args.kwargs["model_credential"] is not None


@pytest.mark.asyncio
async def test_after_tool_call_skips_without_context():
    """context 缺失时不调度。"""
    rail = ImageSummaryRail()
    ctx = SimpleNamespace(
        context=None,
        agent=_agent(declared=True, credential=_credential()),
        inputs=SimpleNamespace(tool_result=_read_file_result()),
    )
    with mock.patch.object(rail._scheduler, "schedule", return_value=0) as schedule:
        await rail.after_tool_call(ctx)
        schedule.assert_not_called()


@pytest.mark.asyncio
async def test_describe_truncates_summary_at_new_cap(monkeypatch):
    """详细摘要按 SUMMARY_MAX_CHARS（1000 字）截断，不再沿用旧版 150 字。"""
    mod = _patch_describe_io(monkeypatch, proxy_key="proxy-key")
    scheduler = mod.ImageSummaryScheduler.__new__(mod.ImageSummaryScheduler)
    credential = SimpleNamespace(
        api_key="fallback-key", base_url="np://claw-model/v1", model="m1"
    )
    long_text = " 截图：包含若干关键数值。 " * 100  # 压缩空白后仍超 1000 字

    class _FakeLongAsyncOpenAI(_FakeAsyncOpenAI):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=mock.AsyncMock(
                        return_value=SimpleNamespace(
                            choices=[SimpleNamespace(message=SimpleNamespace(content=long_text))]
                        )
                    )
                )
            )

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeLongAsyncOpenAI)
    summary, _, _ = await scheduler._describe("/tmp/a.png", credential)

    expected = " ".join(long_text.split())[: mod.SUMMARY_MAX_CHARS]
    assert mod.SUMMARY_MAX_CHARS == 1000
    assert len(summary) == 1000
    assert summary == expected
