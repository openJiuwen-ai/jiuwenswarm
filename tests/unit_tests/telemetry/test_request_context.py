from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from jiuwenswarm.extensions.identity_provider import IdentityInfo, IdentityStore
from jiuwenswarm.telemetry.attributes import (
    APP_ID,
    DOMAIN_ID,
    GEN_AI_CONVERSATION_ID,
    JIUWENCLAW_APP_ID,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_DOMAIN_ID,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
    JIUWENCLAW_USER_ID,
    USER_ID,
)
from jiuwenswarm.telemetry.request_context import (
    TraceBindingRegistry,
    bind_incoming_request,
    refresh_incoming_mode_binding,
    reset_incoming_request,
)
from jiuwenswarm.telemetry.metrics import metrics_channel_id, metrics_session_id


def _span_context(*, trace_id: int, span_id: int, is_remote: bool = False) -> SpanContext:
    return SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=is_remote,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )


def _traceparent(trace_id: int, span_id: int) -> str:
    return f"00-{trace_id:032x}-{span_id:016x}-01"


@pytest.fixture(autouse=True)
def reset_identity_store():
    token = IdentityStore.set_identity(None)
    try:
        yield
    finally:
        IdentityStore.clear(token)


def test_old_binding_cannot_remove_new_request() -> None:
    registry = TraceBindingRegistry(max_bindings=32, ttl_seconds=60)
    old = registry.bind("s1", "r1", object())
    new = registry.bind("s1", "r2", object())

    assert registry.remove(old) is True
    assert registry.resolve("s1", "r1") is None
    assert registry.resolve_session("s1") is new


def test_remove_requires_generation_and_root_span_object_identity() -> None:
    registry = TraceBindingRegistry(max_bindings=32, ttl_seconds=60)
    root_span = object()
    handle = registry.bind("s1", "r1", root_span)

    assert registry.remove(replace(handle, generation=handle.generation + 1)) is False
    assert registry.remove(replace(handle, root_span=object())) is False
    assert registry.resolve("s1", "r1") is handle
    assert registry.remove(handle) is True
    assert registry.remove(handle) is False


def test_rebinding_exact_key_makes_old_handle_stale() -> None:
    registry = TraceBindingRegistry(max_bindings=32, ttl_seconds=60)
    old = registry.bind("s1", "r1", object())
    new = registry.bind("s1", "r1", object())

    assert registry.remove(old) is False
    assert registry.resolve("s1", "r1") is new
    assert registry.resolve_session("s1") is new


def test_removing_latest_binding_falls_back_to_latest_remaining_request() -> None:
    registry = TraceBindingRegistry(max_bindings=32, ttl_seconds=60)
    old = registry.bind("s1", "r1", object())
    new = registry.bind("s1", "r2", object())

    assert registry.remove(new) is True
    assert registry.resolve_session("s1") is old


def test_capacity_and_ttl_cleanup_preserve_exact_and_latest_index_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1.0
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.request_context.time.monotonic", lambda: now
    )
    registry = TraceBindingRegistry(max_bindings=2, ttl_seconds=5)
    first = registry.bind("s1", "r1", object())
    now = 2.0
    second = registry.bind("s2", "r2", object())
    now = 3.0
    third = registry.bind("s3", "r3", object())

    assert registry.resolve("s1", "r1") is None
    assert registry.resolve_session("s1") is None
    assert registry.resolve("s2", "r2") is second
    assert registry.resolve_session("s3") is third
    now = 8.0
    assert registry.prune() == 2
    assert registry.resolve_session("s2") is None
    assert registry.resolve_session("s3") is None
    assert registry.remove(first) is False


def test_resolve_prunes_expired_binding_without_later_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1.0
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.request_context.time.monotonic", lambda: now
    )
    registry = TraceBindingRegistry(max_bindings=8, ttl_seconds=5)
    registry.bind("s1", "r1", object())
    now = 7.0

    assert registry.resolve("s1", "r1") is None


def test_resolve_session_prunes_expired_binding_without_later_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1.0
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.request_context.time.monotonic", lambda: now
    )
    registry = TraceBindingRegistry(max_bindings=8, ttl_seconds=5)
    registry.bind("s1", "r1", object())
    now = 7.0

    assert registry.resolve_session("s1") is None


@pytest.mark.asyncio
async def test_concurrent_session_bindings_remain_isolated() -> None:
    registry = TraceBindingRegistry(max_bindings=64, ttl_seconds=60)

    async def bind_and_read(index: int):
        session_id = f"s{index}"
        handle = registry.bind(session_id, "request", object())
        await asyncio.sleep(0)
        assert registry.resolve(session_id, "request") is handle
        assert registry.resolve_session(session_id) is handle
        return handle

    handles = await asyncio.gather(*(bind_and_read(index) for index in range(20)))
    assert len({handle.generation for handle in handles}) == 20


def test_incoming_request_binds_w3c_parent_and_identity_then_restores_previous() -> None:
    outer_context = _span_context(trace_id=0x111, span_id=0x222)
    scope = trace.use_span(
        NonRecordingSpan(outer_context),
        end_on_exit=False,
    )
    scope.__enter__()
    identity_token = IdentityStore.set_identity(IdentityInfo(user_id="outer"))
    session_token = metrics_session_id.set("outer-session")
    channel_token = metrics_channel_id.set("outer-channel")
    request = SimpleNamespace(
        session_id="incoming-session",
        channel_id="web",
        metadata={
            "traceparent": _traceparent(0xABC, 0xDEF),
            "user_id": "user",
            "domain_id": "domain",
            "app_id": "app",
        }
    )
    try:
        binding = bind_incoming_request(request)
        current_context = trace.get_current_span().get_span_context()
        assert current_context.trace_id == 0xABC
        assert current_context.span_id == 0xDEF
        assert current_context.is_remote is True
        assert IdentityStore.get_identity() == IdentityInfo(
            user_id="user",
            domain_id="domain",
            app_id="app",
        )
        assert metrics_session_id.get() == "incoming-session"
        assert metrics_channel_id.get() == "web"

        reset_incoming_request(binding)
        assert trace.get_current_span().get_span_context() == outer_context
        assert IdentityStore.get_identity() == IdentityInfo(user_id="outer")
        assert metrics_session_id.get() == "outer-session"
        assert metrics_channel_id.get() == "outer-channel"
    finally:
        metrics_channel_id.reset(channel_token)
        metrics_session_id.reset(session_token)
        IdentityStore.clear(identity_token)
        scope.__exit__(None, None, None)


def test_incoming_request_registers_remote_trace_attributes_for_team_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span_registry = SimpleNamespace(bind_trace_attributes=Mock())
    runtime = SimpleNamespace(
        is_unified_active=lambda: True,
        span_registry=span_registry,
    )
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.get_telemetry_runtime",
        lambda: runtime,
    )
    request = SimpleNamespace(
        metadata={
            "traceparent": _traceparent(0xABC, 0xDEF),
            "user_id": "user-team",
            "domain_id": "domain-team",
            "app_id": "app-team",
        },
        request_id="request-team",
        session_id="session-team",
        channel_id="web",
        params={"mode": "team"},
        req_method=SimpleNamespace(value="chat.send"),
        is_stream=True,
    )

    binding = bind_incoming_request(request)
    try:
        span_registry.bind_trace_attributes.assert_called_once()
        trace_id, attributes = span_registry.bind_trace_attributes.call_args.args
        assert trace_id == 0xABC
        assert attributes == {
            GEN_AI_CONVERSATION_ID: "session-team",
            USER_ID: "user-team",
            DOMAIN_ID: "domain-team",
            APP_ID: "app-team",
            JIUWENCLAW_USER_ID: "user-team",
            JIUWENCLAW_DOMAIN_ID: "domain-team",
            JIUWENCLAW_APP_ID: "app-team",
            JIUWENCLAW_CHANNEL_ID: "web",
            JIUWENCLAW_SESSION_ID: "session-team",
            JIUWENCLAW_REQUEST_ID: "request-team",
            "jiuwenclaw.mode": "team",
            "jiuwenclaw.req.method": "chat.send",
            "jiuwenclaw.stream": True,
        }
    finally:
        reset_incoming_request(binding)


def test_refresh_incoming_mode_binding_updates_current_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span_registry = SimpleNamespace(bind_trace_attributes=Mock())
    runtime = SimpleNamespace(
        is_unified_active=lambda: True,
        span_registry=span_registry,
    )
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.get_telemetry_runtime",
        lambda: runtime,
    )
    context = _span_context(trace_id=0xABC, span_id=0x123)
    scope = trace.use_span(NonRecordingSpan(context), end_on_exit=False)
    scope.__enter__()
    try:
        refresh_incoming_mode_binding("agent", "flash")
        span_registry.bind_trace_attributes.assert_called_once_with(
            0xABC,
            {
                "jiuwenclaw.mode": "agent",
                "jiuwenclaw.adapter.mode": "flash",
            },
        )
    finally:
        scope.__exit__(None, None, None)


def test_refresh_incoming_mode_binding_without_trace_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_runtime() -> None:
        raise AssertionError("runtime must not be read without a valid trace")

    monkeypatch.setattr(
        "jiuwenswarm.telemetry.get_telemetry_runtime",
        fail_runtime,
    )
    refresh_incoming_mode_binding("flash")


def test_apply_resolved_mode_to_request_refreshes_adapter_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.handlers import _shared

    refreshed = Mock()
    monkeypatch.setattr(_shared, "refresh_incoming_mode_binding", refreshed)
    monkeypatch.setattr(
        _shared,
        "get_config",
        lambda: {"flash": {"enabled": True}},
    )
    request = AgentRequest(
        request_id="request-flash",
        channel_id="web",
        params={"mode": "agent"},
    )

    assert _shared._apply_resolved_mode_to_request(request) == ("flash", None)
    assert request.params["mode"] == "agent"
    assert request.resolved_adapter_mode == "flash"
    refreshed.assert_called_once_with("agent", "flash")


def test_apply_resolved_mode_to_request_refreshes_explicit_flash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.handlers import _shared

    refreshed = Mock()
    monkeypatch.setattr(_shared, "refresh_incoming_mode_binding", refreshed)
    monkeypatch.setattr(
        _shared,
        "get_config",
        lambda: {"flash": {"enabled": True}},
    )
    request = AgentRequest(
        request_id="request-flash",
        channel_id="web",
        params={"mode": "flash"},
    )

    assert _shared._apply_resolved_mode_to_request(request) == ("flash", None)
    assert request.params["mode"] == "flash"
    assert request.resolved_adapter_mode == "flash"
    refreshed.assert_called_once_with("flash", "flash")


def test_apply_resolved_mode_to_request_refreshes_unchanged_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.handlers import _shared

    refreshed = Mock()
    monkeypatch.setattr(_shared, "refresh_incoming_mode_binding", refreshed)
    monkeypatch.setattr(
        _shared,
        "get_config",
        lambda: {"flash": {"enabled": False}},
    )
    request = AgentRequest(
        request_id="request-agent",
        channel_id="web",
        params={"mode": "agent"},
    )

    assert _shared._apply_resolved_mode_to_request(request) == ("agent", None)
    refreshed.assert_called_once_with("agent", "agent")


def test_apply_resolved_mode_to_request_normalizes_explicit_flash_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.server.handlers import _shared

    refreshed = Mock()
    monkeypatch.setattr(_shared, "refresh_incoming_mode_binding", refreshed)
    monkeypatch.setattr(
        _shared,
        "get_config",
        lambda: {"flash": {"enabled": True}},
    )
    request = AgentRequest(
        request_id="request-flash-variant",
        channel_id="web",
        params={"mode": " FLASH "},
    )

    assert _shared._apply_resolved_mode_to_request(request) == ("flash", None)
    assert request.params["mode"] == "flash"
    assert request.resolved_adapter_mode == "flash"
    refreshed.assert_called_once_with("flash", "flash")


def test_bind_failure_restores_attached_otel_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer = _span_context(trace_id=0x111, span_id=0x222)
    scope = trace.use_span(NonRecordingSpan(outer), end_on_exit=False)
    scope.__enter__()
    request = SimpleNamespace(
        metadata={"traceparent": _traceparent(0xABC, 0xDEF)}
    )

    def fail_bind(identity: IdentityInfo):
        raise RuntimeError("identity unavailable")

    monkeypatch.setattr(IdentityStore, "set_identity", fail_bind)
    try:
        with pytest.raises(RuntimeError, match="identity unavailable"):
            bind_incoming_request(request)
        assert trace.get_current_span().get_span_context() == outer
    finally:
        scope.__exit__(None, None, None)


def test_metric_session_bind_failure_restores_identity_and_otel_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.telemetry import request_context as request_context_module

    outer = _span_context(trace_id=0x111, span_id=0x222)
    scope = trace.use_span(NonRecordingSpan(outer), end_on_exit=False)
    scope.__enter__()
    outer_identity = IdentityInfo(user_id="outer")
    identity_token = IdentityStore.set_identity(outer_identity)

    class FailingSessionContext:
        @staticmethod
        def set(_value: str):
            raise RuntimeError("session context unavailable")

    monkeypatch.setattr(
        request_context_module,
        "metrics_session_id",
        FailingSessionContext,
        raising=False,
    )
    request = SimpleNamespace(
        session_id="incoming-session",
        metadata={
            "traceparent": _traceparent(0xABC, 0xDEF),
            "user_id": "request-user",
        },
    )
    try:
        with pytest.raises(RuntimeError, match="session context unavailable"):
            bind_incoming_request(request)
        assert IdentityStore.get_identity() == outer_identity
        assert trace.get_current_span().get_span_context() == outer
    finally:
        IdentityStore.clear(identity_token)
        scope.__exit__(None, None, None)


@pytest.mark.parametrize("error", [RuntimeError("broken id"), KeyboardInterrupt()])
def test_identity_normalization_error_never_attaches_incoming_context_or_identity(
    error: BaseException,
) -> None:
    class ExplodingIdentity:
        def __str__(self) -> str:
            raise error

    outer = _span_context(trace_id=0x111, span_id=0x222)
    scope = trace.use_span(NonRecordingSpan(outer), end_on_exit=False)
    scope.__enter__()
    identity_token = IdentityStore.set_identity(IdentityInfo(user_id="outer"))
    request = SimpleNamespace(
        metadata={
            "traceparent": _traceparent(0xABC, 0xDEF),
            "user_id": ExplodingIdentity(),
        }
    )
    try:
        with pytest.raises(type(error)):
            bind_incoming_request(request)
        assert trace.get_current_span().get_span_context() == outer
        assert IdentityStore.get_identity() == IdentityInfo(user_id="outer")
    finally:
        IdentityStore.clear(identity_token)
        scope.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_incoming_request_context_is_isolated_between_async_tasks() -> None:
    async def read_context(index: int) -> tuple[int, str | None]:
        request = SimpleNamespace(
            metadata={
                "traceparent": _traceparent(0xA00 + index, 0xB00 + index),
                "user_id": f"u{index}",
            }
        )
        binding = bind_incoming_request(request)
        try:
            await asyncio.sleep(0)
            identity = IdentityStore.get_identity()
            return (
                trace.get_current_span().get_span_context().trace_id,
                identity.user_id if identity is not None else None,
            )
        finally:
            reset_incoming_request(binding)

    assert await asyncio.gather(read_context(1), read_context(2)) == [
        (0xA01, "u1"),
        (0xA02, "u2"),
    ]
    assert IdentityStore.get_identity() is None
    assert trace.get_current_span().get_span_context().is_valid is False


def test_missing_metadata_binds_empty_context_and_identity_then_restores_outer() -> None:
    outer = _span_context(trace_id=0x111, span_id=0x222)
    scope = trace.use_span(NonRecordingSpan(outer), end_on_exit=False)
    scope.__enter__()
    identity_token = IdentityStore.set_identity(IdentityInfo(user_id="outer"))
    try:
        binding = bind_incoming_request(SimpleNamespace(metadata=None))
        assert trace.get_current_span().get_span_context().is_valid is False
        assert IdentityStore.get_identity() == IdentityInfo()
        reset_incoming_request(binding)
        assert trace.get_current_span().get_span_context() == outer
        assert IdentityStore.get_identity() == IdentityInfo(user_id="outer")
    finally:
        IdentityStore.clear(identity_token)
        scope.__exit__(None, None, None)


def test_reset_incoming_request_is_idempotent() -> None:
    binding = bind_incoming_request(
        SimpleNamespace(
            metadata={
                "traceparent": _traceparent(0xABC, 0xDEF),
                "user_id": "user",
            }
        )
    )

    reset_incoming_request(binding)
    reset_incoming_request(binding)

    assert IdentityStore.get_identity() is None
    assert trace.get_current_span().get_span_context().is_valid is False


def test_nested_request_bindings_reject_out_of_order_reset_and_restore_lifo() -> None:
    initial = _span_context(trace_id=0x111, span_id=0x222)
    scope = trace.use_span(NonRecordingSpan(initial), end_on_exit=False)
    scope.__enter__()
    initial_identity = IdentityStore.set_identity(IdentityInfo(user_id="initial"))
    outer = bind_incoming_request(
        SimpleNamespace(
            metadata={
                "traceparent": _traceparent(0xAAA, 0x101),
                "user_id": "outer",
            }
        )
    )
    inner = bind_incoming_request(
        SimpleNamespace(
            metadata={
                "traceparent": _traceparent(0xBBB, 0x202),
                "user_id": "inner",
            }
        )
    )
    try:
        with pytest.raises(RuntimeError, match="LIFO"):
            reset_incoming_request(outer)
        assert trace.get_current_span().get_span_context().trace_id == 0xBBB
        assert IdentityStore.get_identity() == IdentityInfo(user_id="inner")

        reset_incoming_request(inner)
        assert trace.get_current_span().get_span_context().trace_id == 0xAAA
        assert IdentityStore.get_identity() == IdentityInfo(user_id="outer")

        reset_incoming_request(outer)
        assert trace.get_current_span().get_span_context() == initial
        assert IdentityStore.get_identity() == IdentityInfo(
            user_id="initial"
        )
        reset_incoming_request(outer)
    finally:
        reset_incoming_request(inner)
        reset_incoming_request(outer)
        IdentityStore.clear(initial_identity)
        scope.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_reset_from_child_task_cannot_poison_parent_cleanup() -> None:
    binding = bind_incoming_request(
        SimpleNamespace(
            metadata={
                "traceparent": _traceparent(0xABC, 0xDEF),
                "user_id": "user",
            }
        )
    )

    await asyncio.create_task(asyncio.to_thread(reset_incoming_request, binding))
    assert trace.get_current_span().get_span_context().trace_id == 0xABC
    assert IdentityStore.get_identity() == IdentityInfo(user_id="user")

    reset_incoming_request(binding)
    assert IdentityStore.get_identity() is None
    assert trace.get_current_span().get_span_context().is_valid is False


def test_identity_reset_control_error_still_detaches_otel_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ResetControlSignal(BaseException):
        pass

    real_reset = IdentityStore.clear

    def restore_then_raise(token) -> None:
        real_reset(token)
        raise ResetControlSignal

    binding = bind_incoming_request(
        SimpleNamespace(
            metadata={
                "traceparent": _traceparent(0xABC, 0xDEF),
                "user_id": "user",
            }
        )
    )
    monkeypatch.setattr(IdentityStore, "clear", restore_then_raise)
    try:
        with pytest.raises(ResetControlSignal):
            reset_incoming_request(binding)

        assert IdentityStore.get_identity() is None
        assert trace.get_current_span().get_span_context().is_valid is False
        reset_incoming_request(binding)
    finally:
        current_context = trace.get_current_span().get_span_context()
        if current_context.trace_id == 0xABC:
            otel_context.detach(binding.otel_token)


def test_metric_session_reset_control_error_still_restores_identity_and_otel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.telemetry import request_context as request_context_module

    class ResetControlSignal(BaseException):
        pass

    real_session_context = request_context_module.metrics_session_id

    class RestoreThenRaiseSessionContext:
        @staticmethod
        def reset(token) -> None:
            real_session_context.reset(token)
            raise ResetControlSignal

    outer = _span_context(trace_id=0x111, span_id=0x222)
    scope = trace.use_span(NonRecordingSpan(outer), end_on_exit=False)
    scope.__enter__()
    outer_identity = IdentityInfo(user_id="outer")
    identity_token = IdentityStore.set_identity(outer_identity)
    outer_session_token = metrics_session_id.set("outer-session")
    binding = bind_incoming_request(
        SimpleNamespace(
            session_id="incoming-session",
            metadata={
                "traceparent": _traceparent(0xABC, 0xDEF),
                "user_id": "request-user",
            },
        )
    )
    monkeypatch.setattr(
        request_context_module,
        "metrics_session_id",
        RestoreThenRaiseSessionContext,
    )
    try:
        with pytest.raises(ResetControlSignal):
            reset_incoming_request(binding)
        assert metrics_session_id.get() == "outer-session"
        assert IdentityStore.get_identity() == outer_identity
        assert trace.get_current_span().get_span_context() == outer
    finally:
        current_context = trace.get_current_span().get_span_context()
        if current_context.trace_id == 0xABC:
            otel_context.detach(binding.otel_token)
        metrics_session_id.reset(outer_session_token)
        IdentityStore.clear(identity_token)
        scope.__exit__(None, None, None)
