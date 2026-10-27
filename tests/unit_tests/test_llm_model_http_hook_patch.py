# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""before_model_http 扩展埋点（llm_sse_patch Patch 5）的单元测试。

覆盖核心埋点的验收要点（F153）：

* 合并规则：保留键丢弃（Authorization / thinking 族）、x-span-id 允许写入、
  非法值（含 CR/LF 的头、不可序列化的正文字段）跳过、调用方显式值优先、
  未写的口袋保持原样；
* 触发链路：invoke / stream 出口触发事件并合并口袋；
* 失败语义：handler 抛错、registry 未初始化时请求照常发出（零新增字段）；
* 幂等：独立标志，重复安装不二次包装。
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

import pytest

import jiuwenswarm.llm_sse_patch as patch_mod

from openjiuwen.core.foundation.llm.model_clients import openai_model_client as oai_mod

_MODULE_FLAG = "_MODEL_HTTP_HOOK_PATCH_APPLIED"
_CLIENT_FLAG = "_model_http_hook_patch_applied"


@pytest.fixture()
def fresh_patch_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空埋点补丁幂等标志，模拟「服务启动早期尚未打补丁」的进程状态。

    清单内各客户端（OpenAI / Anthropic / OpenAIAccount，后两者若可用）一并
    复位类级标志（app_agentserver 导入即打过补丁），并把出口方法换成 no-op
    假方法，避免每个用例的 apply() 在真实包装上叠层。
    """
    monkeypatch.setattr(patch_mod, _MODULE_FLAG, False, raising=False)

    async def _noop_invoke(self: Any, *args: Any, **kwargs: Any):
        return "ok"

    async def _noop_stream(self: Any, *args: Any, **kwargs: Any):
        yield 1

    async def _noop_generate_image(self: Any, *args: Any, **kwargs: Any):
        return "image-ok"

    monkeypatch.setattr(oai_mod.OpenAIModelClient, _CLIENT_FLAG, False, raising=False)
    monkeypatch.setattr(oai_mod.OpenAIModelClient, "invoke", _noop_invoke)
    monkeypatch.setattr(oai_mod.OpenAIModelClient, "stream", _noop_stream)
    monkeypatch.setattr(
        oai_mod.OpenAIModelClient, "generate_image", _noop_generate_image
    )

    for _mod_name, _class_name in (
        ("anthropic_model_client", "AnthropicModelClient"),
        ("openai_account_model_client", "OpenAIAccountModelClient"),
    ):
        try:
            _mod = importlib.import_module(
                f"openjiuwen.core.foundation.llm.model_clients.{_mod_name}"
            )
        except Exception:
            continue
        _cls = getattr(_mod, _class_name)
        monkeypatch.setattr(_cls, _CLIENT_FLAG, False, raising=False)
        monkeypatch.setattr(_cls, "invoke", _noop_invoke)
        monkeypatch.setattr(_cls, "stream", _noop_stream)


def _fake_client() -> SimpleNamespace:
    return SimpleNamespace(
        model_client_config=SimpleNamespace(api_base="https://modelarts-maas.cn/v1"),
        model_config=SimpleNamespace(model_name="glm-5.2"),
    )


def _install_fake_trigger(
    monkeypatch: pytest.MonkeyPatch,
    *,
    filler: Any = None,
    error: Exception | None = None,
    initialized: bool = True,
) -> list[dict[str, Any]]:
    """替换 ExtensionRegistry.trigger_if_initialized，记录调用并可控返回。"""
    calls: list[dict[str, Any]] = []

    async def _fake_trigger(event: str, context: Any = None, **kwargs: Any) -> bool:
        calls.append({"event": event, "context": context})
        if error is not None:
            raise error
        if filler is not None:
            filler(context)
        return initialized

    monkeypatch.setattr(
        "jiuwenswarm.extensions.registry.ExtensionRegistry.trigger_if_initialized",
        _fake_trigger,
    )
    return calls


# ── 事件名与合并规则 ─────────────────────────────────────────────────


def test_event_name_is_scoped() -> None:
    from jiuwenswarm.extensions.hook_event import AgentServerHookEvents

    assert AgentServerHookEvents.BEFORE_MODEL_HTTP == "agent_server:before_model_http"


def test_merge_keeps_span_id_and_drops_reserved_and_invalid_values() -> None:
    from jiuwenswarm.extensions.hooks_context import ModelHttpHookContext

    kwargs: dict[str, Any] = {}
    ctx = ModelHttpHookContext(
        headers={
            "x-client": "miniprogram|vivo|trial",
            "Authorization": "Basic evil",
            "X-SPAN-ID": "ext-span-1",  # 扩展可写，宿主不再丢弃
            "x-bad": "a\r\nSet-Cookie: pwn",
            "x-cn": "用户中文原话",  # 非 latin-1 头值会让 httpx 发送时抛错，必须跳过
            "": "empty-name",
        },
        extra_body={
            "report_id": "r-1",
            "thinking": {"type": "enabled"},
            "enable_thinking": True,
            "reasoning_effort": "high",
            "unserializable": object(),
        },
    )
    patch_mod._merge_model_http_hook_ctx(ctx, kwargs)  # pylint: disable=protected-access

    assert kwargs["custom_headers"] == {
        "x-client": "miniprogram|vivo|trial",
        "X-SPAN-ID": "ext-span-1",
    }
    assert kwargs["extra_body"] == {"report_id": "r-1"}


def test_merge_span_id_is_case_insensitive_and_caller_wins() -> None:
    from jiuwenswarm.extensions.hooks_context import ModelHttpHookContext

    kwargs: dict[str, Any] = {"custom_headers": {"X-SPAN-ID": "caller-span"}}
    ctx = ModelHttpHookContext(headers={"x-span-id": "ext-span"})
    patch_mod._merge_model_http_hook_ctx(ctx, kwargs)  # pylint: disable=protected-access

    # 调用方非空值优先，且不新增第二个大小写变体（否则 httpx 会合并成双值）
    assert kwargs["custom_headers"] == {"X-SPAN-ID": "caller-span"}


def test_merge_span_id_ignores_blank_extension_value() -> None:
    from jiuwenswarm.extensions.hooks_context import ModelHttpHookContext

    kwargs: dict[str, Any] = {}
    ctx = ModelHttpHookContext(headers={"x-span-id": " "})
    patch_mod._merge_model_http_hook_ctx(ctx, kwargs)  # pylint: disable=protected-access

    # 扩展空值视为未提供，不写入；留给 MaaS 兜底 uuid
    assert kwargs["custom_headers"] == {}


def test_merge_span_id_replaces_caller_blank_value() -> None:
    from jiuwenswarm.extensions.hooks_context import ModelHttpHookContext

    kwargs: dict[str, Any] = {"custom_headers": {"X-SPAN-ID": ""}}
    ctx = ModelHttpHookContext(headers={"x-span-id": "ext-span"})
    patch_mod._merge_model_http_hook_ctx(ctx, kwargs)  # pylint: disable=protected-access

    # 调用方空值无意义：清掉空键，采用扩展的非空值，最终仍只有一个同名键
    assert kwargs["custom_headers"] == {"x-span-id": "ext-span"}


def test_merge_dedupes_any_header_case_insensitively() -> None:
    from jiuwenswarm.extensions.hooks_context import ModelHttpHookContext

    # 任意头都按大小写不敏感去重，不需要为每个新增头单独特判
    kwargs: dict[str, Any] = {"custom_headers": {"X-Client": "caller"}}
    ctx = ModelHttpHookContext(headers={"x-client": "extension"})
    patch_mod._merge_model_http_hook_ctx(ctx, kwargs)  # pylint: disable=protected-access
    assert kwargs["custom_headers"] == {"X-Client": "caller"}

    # 调用方空值时允许扩展值覆盖，并沿用扩展的键名
    kwargs = {"custom_headers": {"X-Client": " "}}
    patch_mod._merge_model_http_hook_ctx(ctx, kwargs)  # pylint: disable=protected-access
    assert kwargs["custom_headers"] == {"x-client": "extension"}


def test_merge_keeps_caller_values_and_empty_pockets() -> None:
    from jiuwenswarm.extensions.hooks_context import ModelHttpHookContext

    kwargs: dict[str, Any] = {"custom_headers": {"x-client": "caller"}}
    ctx = ModelHttpHookContext(headers={"x-client": "extension"})
    patch_mod._merge_model_http_hook_ctx(ctx, kwargs)  # pylint: disable=protected-access

    # 调用方显式传入的值优先，扩展不得覆盖
    assert kwargs["custom_headers"]["x-client"] == "caller"
    # 只写 headers 口包不产生 extra_body：正文零变化
    assert "extra_body" not in kwargs


# ── 安装幂等 ─────────────────────────────────────────────────────────


def test_patch_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, fresh_patch_state: None
) -> None:
    async def _fake_orig_invoke(self: Any, *args: Any, **kwargs: Any):
        return "ok"

    async def _fake_orig_stream(self: Any, *args: Any, **kwargs: Any):
        yield 1

    monkeypatch.setattr(oai_mod.OpenAIModelClient, "invoke", _fake_orig_invoke)
    monkeypatch.setattr(oai_mod.OpenAIModelClient, "stream", _fake_orig_stream)

    patch_mod.apply_before_model_http_hook_patch()
    wrapped_invoke = oai_mod.OpenAIModelClient.invoke
    wrapped_stream = oai_mod.OpenAIModelClient.stream

    patch_mod.apply_before_model_http_hook_patch()
    assert oai_mod.OpenAIModelClient.invoke is wrapped_invoke
    assert oai_mod.OpenAIModelClient.stream is wrapped_stream
    assert getattr(patch_mod, _MODULE_FLAG) is True  # pylint: disable=protected-access
    assert (  # pylint: disable=protected-access
        getattr(oai_mod.OpenAIModelClient, _CLIENT_FLAG) is True
    )


# ── invoke / stream 出口触发与合并 ───────────────────────────────────


def test_invoke_wrapper_triggers_and_merges(
    monkeypatch: pytest.MonkeyPatch, fresh_patch_state: None
) -> None:
    recorded: list[dict[str, Any]] = []

    async def _fake_orig_invoke(self: Any, *args: Any, **kwargs: Any):
        recorded.append(kwargs)
        return "ok"

    monkeypatch.setattr(oai_mod.OpenAIModelClient, "invoke", _fake_orig_invoke)
    patch_mod.apply_before_model_http_hook_patch()

    def _fill(ctx: Any) -> None:
        ctx.headers["x-client"] = "miniprogram|vivo|trial"
        ctx.headers["x-message-meta"] = "scen|sid|inv|msg"
        ctx.extra_body["report_id"] = "r-1"

    calls = _install_fake_trigger(monkeypatch, filler=_fill)

    result = asyncio.run(
        oai_mod.OpenAIModelClient.invoke(_fake_client(), messages="hi")
    )

    assert result == "ok"
    assert calls[0]["event"] == "agent_server:before_model_http"
    assert calls[0]["context"].api_base == "https://modelarts-maas.cn/v1"
    assert calls[0]["context"].model_name == "glm-5.2"
    assert recorded[0]["custom_headers"] == {
        "x-client": "miniprogram|vivo|trial",
        "x-message-meta": "scen|sid|inv|msg",
    }
    assert recorded[0]["extra_body"] == {"report_id": "r-1"}


def test_stream_wrapper_triggers_and_passes_chunks(
    monkeypatch: pytest.MonkeyPatch, fresh_patch_state: None
) -> None:
    recorded: list[dict[str, Any]] = []

    async def _fake_orig_stream(self: Any, *args: Any, **kwargs: Any):
        recorded.append(kwargs)
        for chunk in (1, 2, 3):
            yield chunk

    monkeypatch.setattr(oai_mod.OpenAIModelClient, "stream", _fake_orig_stream)
    patch_mod.apply_before_model_http_hook_patch()

    _install_fake_trigger(
        monkeypatch,
        filler=lambda ctx: ctx.headers.update({"x-client": "miniprogram"}),
    )

    async def _collect() -> list[int]:
        agen = oai_mod.OpenAIModelClient.stream(_fake_client(), messages="hi")
        return [chunk async for chunk in agen]

    chunks = asyncio.run(_collect())

    assert chunks == [1, 2, 3]
    assert recorded[0]["custom_headers"] == {"x-client": "miniprogram"}


def test_generate_image_wrapper_triggers_and_merges(
    monkeypatch: pytest.MonkeyPatch, fresh_patch_state: None
) -> None:
    """图像生成出口（images.generate）同样计费，必须经过埋点。"""
    recorded: list[dict[str, Any]] = []

    async def _fake_orig_generate_image(self: Any, *args: Any, **kwargs: Any):
        recorded.append(kwargs)
        return "image-ok"

    monkeypatch.setattr(
        oai_mod.OpenAIModelClient, "generate_image", _fake_orig_generate_image
    )
    patch_mod.apply_before_model_http_hook_patch()

    _install_fake_trigger(
        monkeypatch,
        filler=lambda ctx: ctx.headers.update(
            {"x-client": "miniprogram|vivo|trial"}
        ),
    )

    result = asyncio.run(
        oai_mod.OpenAIModelClient.generate_image(_fake_client(), messages=["hi"])
    )

    assert result == "image-ok"
    assert recorded[0]["custom_headers"] == {"x-client": "miniprogram|vivo|trial"}


def test_anthropic_client_is_covered(
    monkeypatch: pytest.MonkeyPatch, fresh_patch_state: None
) -> None:
    """AC-7：Anthropic 等异构客户端的 invoke/stream 也在埋点覆盖内。"""
    anthropic_mod = pytest.importorskip(
        "openjiuwen.core.foundation.llm.model_clients.anthropic_model_client"
    )
    recorded: list[dict[str, Any]] = []

    async def _fake_orig_invoke(self: Any, *args: Any, **kwargs: Any):
        recorded.append(kwargs)
        return "ok"

    monkeypatch.setattr(
        anthropic_mod.AnthropicModelClient, "invoke", _fake_orig_invoke
    )
    patch_mod.apply_before_model_http_hook_patch()

    calls = _install_fake_trigger(
        monkeypatch,
        filler=lambda ctx: ctx.headers.update({"x-client": "miniprogram"}),
    )

    result = asyncio.run(
        anthropic_mod.AnthropicModelClient.invoke(_fake_client(), messages="hi")
    )

    assert result == "ok"
    assert calls[0]["event"] == "agent_server:before_model_http"
    assert recorded[0]["custom_headers"] == {"x-client": "miniprogram"}


def test_openai_account_client_is_covered(
    monkeypatch: pytest.MonkeyPatch, fresh_patch_state: None
) -> None:
    """OpenAIAccount（独立实现，custom_headers 通道完整）也在埋点覆盖内。"""
    account_mod = pytest.importorskip(
        "openjiuwen.core.foundation.llm.model_clients.openai_account_model_client"
    )
    recorded: list[dict[str, Any]] = []

    async def _fake_orig_invoke(self: Any, *args: Any, **kwargs: Any):
        recorded.append(kwargs)
        return "ok"

    monkeypatch.setattr(
        account_mod.OpenAIAccountModelClient, "invoke", _fake_orig_invoke
    )
    patch_mod.apply_before_model_http_hook_patch()

    calls = _install_fake_trigger(
        monkeypatch,
        filler=lambda ctx: ctx.headers.update({"x-client": "miniprogram"}),
    )

    result = asyncio.run(
        account_mod.OpenAIAccountModelClient.invoke(_fake_client(), messages="hi")
    )

    assert result == "ok"
    assert calls[0]["event"] == "agent_server:before_model_http"
    assert recorded[0]["custom_headers"] == {"x-client": "miniprogram"}


# ── 覆盖面与安装语义 ─────────────────────────────────────────────────


def test_inherited_methods_are_not_double_wrapped(
    monkeypatch: pytest.MonkeyPatch, fresh_patch_state: None
) -> None:
    """vars 护栏：子类继承的方法不再包装，避免每次调用双触发埋点。"""
    async def _fake_orig_invoke(self: Any, *args: Any, **kwargs: Any):
        return "ok"

    monkeypatch.setattr(oai_mod.OpenAIModelClient, "invoke", _fake_orig_invoke)
    patch_mod.apply_before_model_http_hook_patch()

    class _SubClient(oai_mod.OpenAIModelClient):
        """未重写任何出口方法的子类（HuaweiMaas / OpenRouter 场景）。"""

    class _OverrideClient(oai_mod.OpenAIModelClient):
        """仅重写 generate_image 的子类（DashScope 场景）。"""

        async def generate_image(self, *args: Any, **kwargs: Any):
            return "img"

    for _cls in (_SubClient, _OverrideClient):
        # 复位继承来的类级标志，模拟「该子类尚未打过补丁」
        _cls._model_http_hook_patch_applied = False  # pylint: disable=protected-access

    # 未重写方法：零包装，继承方法仍指向父类（已包装）实现
    assert (
        patch_mod._wrap_model_http_hook_on_class(  # pylint: disable=protected-access
            _SubClient, ("invoke", "stream", "generate_image")
        )
        == []
    )
    assert _SubClient.invoke is oai_mod.OpenAIModelClient.invoke

    # 重写的方法：只包自己定义的 generate_image，invoke 仍走父类包装
    assert (
        patch_mod._wrap_model_http_hook_on_class(  # pylint: disable=protected-access
            _OverrideClient, ("invoke", "stream", "generate_image")
        )
        == ["generate_image"]
    )
    assert _OverrideClient.invoke is oai_mod.OpenAIModelClient.invoke

    # 继承方法只触发一次埋点（若被子类二次包装会触发两次）
    calls = _install_fake_trigger(monkeypatch)
    result = asyncio.run(_SubClient.invoke(_fake_client(), messages="hi"))
    assert result == "ok"
    assert len(calls) == 1


def test_flag_not_set_when_no_client_wrapped(
    monkeypatch: pytest.MonkeyPatch, fresh_patch_state: None
) -> None:
    """全部客户端不可导入时不置位幂等标志，下次调用可重试（与 P1/P2 一致）。"""
    real_import = patch_mod._import_model_client_class  # pylint: disable=protected-access
    monkeypatch.setattr(patch_mod, "_import_model_client_class", lambda dotted: None)

    patch_mod.apply_before_model_http_hook_patch()
    assert getattr(patch_mod, _MODULE_FLAG) is False

    monkeypatch.setattr(patch_mod, "_import_model_client_class", real_import)

    async def _fake_orig_invoke(self: Any, *args: Any, **kwargs: Any):
        return "ok"

    async def _fake_orig_stream(self: Any, *args: Any, **kwargs: Any):
        yield 1

    monkeypatch.setattr(oai_mod.OpenAIModelClient, "invoke", _fake_orig_invoke)
    monkeypatch.setattr(oai_mod.OpenAIModelClient, "stream", _fake_orig_stream)
    patch_mod.apply_before_model_http_hook_patch()
    assert getattr(patch_mod, _MODULE_FLAG) is True


# ── 失败语义：绝不阻塞模型调用 ───────────────────────────────────────


def test_handler_failure_does_not_block_invoke(
    monkeypatch: pytest.MonkeyPatch, fresh_patch_state: None
) -> None:
    recorded: list[dict[str, Any]] = []

    async def _fake_orig_invoke(self: Any, *args: Any, **kwargs: Any):
        recorded.append(kwargs)
        return "ok"

    monkeypatch.setattr(oai_mod.OpenAIModelClient, "invoke", _fake_orig_invoke)
    patch_mod.apply_before_model_http_hook_patch()
    _install_fake_trigger(monkeypatch, error=RuntimeError("handler boom"))

    result = asyncio.run(
        oai_mod.OpenAIModelClient.invoke(_fake_client(), messages="hi")
    )

    # handler 抛错：请求照常发出，只是不带扩展字段
    assert result == "ok"
    assert "custom_headers" not in recorded[0]
    assert "extra_body" not in recorded[0]


def test_registry_not_initialized_is_noop(
    monkeypatch: pytest.MonkeyPatch, fresh_patch_state: None
) -> None:
    recorded: list[dict[str, Any]] = []

    async def _fake_orig_invoke(self: Any, *args: Any, **kwargs: Any):
        recorded.append(kwargs)
        return "ok"

    monkeypatch.setattr(oai_mod.OpenAIModelClient, "invoke", _fake_orig_invoke)
    patch_mod.apply_before_model_http_hook_patch()
    _install_fake_trigger(monkeypatch, initialized=False)

    result = asyncio.run(
        oai_mod.OpenAIModelClient.invoke(_fake_client(), messages="hi")
    )

    # registry 未初始化：零副作用，请求与现状完全一致
    assert result == "ok"
    assert "custom_headers" not in recorded[0]
    assert "extra_body" not in recorded[0]
