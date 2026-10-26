"""Gateway request telemetry and cross-WebSocket context contracts."""

from __future__ import annotations

import ast
import asyncio
import inspect
from contextvars import Context
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry import trace
from opentelemetry.propagate import get_global_textmap, set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode

from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse
from jiuwenswarm.common.schema.message import Message, ReqMethod
from jiuwenswarm.extensions.identity_provider import IdentityInfo, IdentityStore
from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler
from jiuwenswarm.telemetry.attributes import (
    ERROR_TYPE,
    JIUWENCLAW_CANCELED,
    JIUWENCLAW_CHANNEL_ID,
    JIUWENCLAW_REQUEST_ID,
    JIUWENCLAW_SESSION_ID,
)
from jiuwenswarm.telemetry.metrics import metrics_channel_id, metrics_session_id


def test_gateway_stack_state_is_not_accessed_as_protected_outside_handle() -> None:
    from jiuwenswarm.telemetry import gateway as gateway_module

    source = Path(gateway_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    protected_stack_members = {
        child.attr
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute)
        and child.attr in {"_stack_marker", "_stack_token"}
    }

    assert protected_stack_members == set()


class _TestMessageHandler(MessageHandler):
    @classmethod
    def create(cls, client: object) -> _TestMessageHandler:
        MessageHandler._instance = None
        cls._instance = None
        return cls(client)


def _runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SimpleNamespace, InMemorySpanExporter, Mock]:
    from jiuwenswarm.telemetry import gateway as gateway_module

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry_metrics = Mock()
    runtime = SimpleNamespace(
        is_unified_active=lambda: True,
        tracer_provider=provider,
        telemetry_metrics=telemetry_metrics,
    )
    monkeypatch.setattr(gateway_module, "_get_runtime", lambda: runtime)
    return runtime, exporter, telemetry_metrics


def _envelope(
    request_id: str,
    *,
    stream: bool,
    channel_context: dict[str, object] | None = None,
) -> E2AEnvelope:
    return E2AEnvelope(
        request_id=request_id,
        channel="web",
        session_id=f"session-{request_id}",
        user_id="envelope-user",
        method=ReqMethod.CHAT_SEND.value,
        params={"query": "hello"},
        is_stream=stream,
        channel_context=dict(channel_context or {}),
    )


@pytest.mark.asyncio
async def test_real_non_stream_request_has_one_gateway_root_and_remote_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.telemetry.context_propagation import (
        bind_incoming_request,
        reset_incoming_request,
    )

    runtime, exporter, telemetry_metrics = _runtime(monkeypatch)
    child_trace_ids: list[int] = []

    class TracingClient:
        @staticmethod
        async def send_request(env: E2AEnvelope) -> AgentResponse:
            assert env.channel_context["source"] == "business"
            assert env.channel_context["traceparent"] != (
                "00-11111111111111111111111111111111-2222222222222222-01"
            )
            request = e2a_to_agent_request(env)
            binding = bind_incoming_request(request)
            try:
                child = runtime.tracer_provider.get_tracer(
                    "agentserver.test"
                ).start_span(
                    "agent.invoke",
                    kind=SpanKind.SERVER,
                )
                child_trace_ids.append(child.get_span_context().trace_id)
                child.end()
            finally:
                reset_incoming_request(binding)
            return AgentResponse(
                request_id=env.request_id or "",
                channel_id=env.channel or "",
                payload={"content": "ok"},
            )

    handler = _TestMessageHandler.create(TracingClient())
    handler._response_to_message = Mock(return_value=object())
    handler.publish_robot_messages = AsyncMock()
    env = _envelope(
        "request-unary",
        stream=False,
        channel_context={
            "source": "business",
            "traceparent": ("00-11111111111111111111111111111111-2222222222222222-01"),
        },
    )
    identity_token = IdentityStore.set_identity(
        IdentityInfo(user_id="user-1", domain_id="domain-1", app_id="app-1")
    )
    try:
        response = await handler._process_non_stream_request(
            Message(
                id=env.request_id or "",
                type="req",
                channel_id=env.channel or "",
                session_id=env.session_id,
                params=dict(env.params),
                timestamp=0.0,
                ok=True,
                req_method=ReqMethod.CHAT_SEND,
                is_stream=False,
            ),
            env,
        )
    finally:
        IdentityStore.clear(identity_token)
        runtime.tracer_provider.shutdown()

    spans = exporter.get_finished_spans()
    gateway_spans = [span for span in spans if span.name == "channel.request"]
    child_span = next(span for span in spans if span.name == "agent.invoke")
    gateway_span = gateway_spans[0]

    assert response is not None
    assert len(gateway_spans) == 1
    assert gateway_span.kind is SpanKind.SERVER
    assert child_trace_ids == [gateway_span.context.trace_id]
    assert child_span.parent.span_id == gateway_span.context.span_id
    assert gateway_span.attributes[JIUWENCLAW_CHANNEL_ID] == "web"
    assert gateway_span.attributes[JIUWENCLAW_SESSION_ID] == "session-request-unary"
    assert gateway_span.attributes[JIUWENCLAW_REQUEST_ID] == "request-unary"
    assert gateway_span.attributes["jiuwenclaw.req.method"] == "chat.send"
    assert gateway_span.attributes["jiuwenclaw.stream"] is False
    assert (
        env.channel_context.items()
        >= {
            "source": "business",
            "domain_id": "domain-1",
            "app_id": "app-1",
        }.items()
    )
    assert env.channel_context["user_id"] == "user-1"
    assert "user_id" not in env.channel_context.get("routing", {})
    telemetry_metrics.add.assert_called_once_with(
        "jiuwenclaw.request.count",
        1,
        {JIUWENCLAW_CHANNEL_ID: "web"},
    )
    assert telemetry_metrics.record.call_args.args[0] == "jiuwenclaw.request.duration"
    assert telemetry_metrics.record.call_args.args[1] >= 0
    telemetry_metrics.add.assert_called_once()


class _SuccessStreamClient:
    @staticmethod
    async def send_request_stream(env: E2AEnvelope):
        if False:
            yield env


class _ErrorStreamClient:
    @staticmethod
    async def send_request_stream(env: E2AEnvelope):
        if False:
            yield env
        raise ValueError("stream failed")


class _CancelledStreamClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def send_request_stream(self, env: E2AEnvelope):
        self.started.set()
        await asyncio.Event().wait()
        if False:
            yield env


@pytest.mark.asyncio
async def test_real_stream_records_success_error_and_cancel_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, exporter, telemetry_metrics = _runtime(monkeypatch)

    success = _TestMessageHandler.create(_SuccessStreamClient())
    await success.process_stream(
        _envelope("stream-success", stream=True),
        "session-stream-success",
        None,
        emit_processing_status=False,
    )

    failed = _TestMessageHandler.create(_ErrorStreamClient())
    failed._publish_stream_cancelled_final = AsyncMock()
    with pytest.raises(ValueError, match="stream failed"):
        await failed.process_stream(
            _envelope("stream-error", stream=True),
            "session-stream-error",
            None,
            emit_processing_status=False,
        )

    cancelling_client = _CancelledStreamClient()
    cancelled = _TestMessageHandler.create(cancelling_client)
    task = asyncio.create_task(
        cancelled.process_stream(
            _envelope("stream-cancel", stream=True),
            "session-stream-cancel",
            None,
            emit_processing_status=False,
        )
    )
    await cancelling_client.started.wait()
    task.cancel()
    await task
    runtime.tracer_provider.shutdown()

    gateway_spans = [
        span for span in exporter.get_finished_spans() if span.name == "channel.request"
    ]
    span_by_request = {
        span.attributes[JIUWENCLAW_REQUEST_ID]: span for span in gateway_spans
    }
    assert len(gateway_spans) == 3
    assert span_by_request["stream-success"].status.status_code is StatusCode.OK
    assert span_by_request["stream-error"].status.status_code is StatusCode.ERROR
    assert span_by_request["stream-error"].attributes[ERROR_TYPE] == "ValueError"
    assert span_by_request["stream-cancel"].status.status_code is StatusCode.ERROR
    assert span_by_request["stream-cancel"].attributes[JIUWENCLAW_CANCELED] is True

    request_count_calls = [
        metric_call
        for metric_call in telemetry_metrics.add.call_args_list
        if metric_call.args[0] == "jiuwenclaw.request.count"
    ]
    error_count_calls = [
        metric_call
        for metric_call in telemetry_metrics.add.call_args_list
        if metric_call.args[0] == "jiuwenclaw.request.error.count"
    ]
    duration_calls = [
        metric_call
        for metric_call in telemetry_metrics.record.call_args_list
        if metric_call.args[0] == "jiuwenclaw.request.duration"
    ]
    assert len(request_count_calls) == 3
    assert len(error_count_calls) == 2
    assert len(duration_calls) == 3
    for metric_call in (*request_count_calls, *error_count_calls, *duration_calls):
        assert metric_call.args[2] == {JIUWENCLAW_CHANNEL_ID: "web"}


@pytest.mark.parametrize(
    ("channel_context", "expected_identity"),
    [
        (
            {
                "user_id": "top-user",
                "domain_id": "top-domain",
                "app_id": "top-app",
                "query": {
                    "user_id": ["query-user"],
                    "domain_id": ("query-domain",),
                    "app_id": "query-app",
                },
            },
            IdentityInfo(
                user_id="top-user",
                domain_id="top-domain",
                app_id="top-app",
            ),
        ),
        (
            {
                "user_id": "list-user",
                "domain_id": None,
                "app_id": "",
                "query": {
                    "user_id": ["ignored-user"],
                    "domain_id": ("tuple-domain", "ignored-domain"),
                    "app_id": "scalar-app",
                },
            },
            IdentityInfo(
                user_id="list-user",
                domain_id="tuple-domain",
                app_id="scalar-app",
            ),
        ),
    ],
)
def test_gateway_promotes_normalized_carrier_identity(
    monkeypatch: pytest.MonkeyPatch,
    channel_context: dict[str, object],
    expected_identity: IdentityInfo,
) -> None:
    from jiuwenswarm.telemetry.gateway import (
        close_gateway_request,
        open_gateway_request,
    )

    runtime, _, _ = _runtime(monkeypatch)
    outer_identity_token = IdentityStore.set_identity(IdentityInfo())
    outer_session_token = metrics_session_id.set("outer-session")
    outer_channel_token = metrics_channel_id.set("outer-channel")
    env = _envelope(
        "query-identity",
        stream=False,
        channel_context=channel_context,
    )
    handle = None
    try:
        handle = open_gateway_request(env)
        assert env.channel_context["user_id"] == expected_identity.user_id
        assert "user_id" not in env.channel_context.get("routing", {})
        assert env.channel_context["domain_id"] == expected_identity.domain_id
        assert env.channel_context["app_id"] == expected_identity.app_id
        assert IdentityStore.get_identity() == expected_identity
        assert metrics_session_id.get() == "session-query-identity"
        assert metrics_channel_id.get() == "web"
    finally:
        if handle is not None:
            close_gateway_request(handle)
        assert IdentityStore.get_identity() == IdentityInfo()
        assert metrics_session_id.get() == "outer-session"
        assert metrics_channel_id.get() == "outer-channel"
        metrics_channel_id.reset(outer_channel_token)
        metrics_session_id.reset(outer_session_token)
        IdentityStore.clear(outer_identity_token)
        runtime.tracer_provider.shutdown()


@pytest.mark.parametrize("terminal_error", [None, ValueError("request failed")])
def test_gateway_metrics_observe_request_context_until_terminal_recording(
    monkeypatch: pytest.MonkeyPatch,
    terminal_error: BaseException | None,
) -> None:
    from jiuwenswarm.telemetry.gateway import (
        close_gateway_request,
        open_gateway_request,
    )

    runtime, _, telemetry_metrics = _runtime(monkeypatch)
    observed_contexts: list[tuple[IdentityInfo | None, str | None]] = []

    def observe_context(*_args: object) -> None:
        observed_contexts.append(
            (IdentityStore.get_identity(), metrics_session_id.get())
        )

    telemetry_metrics.add.side_effect = observe_context
    telemetry_metrics.record.side_effect = observe_context
    outer_identity = IdentityInfo(user_id="outer-user")
    outer_identity_token = IdentityStore.set_identity(outer_identity)
    outer_session_token = metrics_session_id.set("outer-session")
    env = _envelope(
        "context-lifecycle",
        stream=False,
        channel_context={
            "query": {
                "user_id": ["request-user"],
                "domain_id": ("request-domain",),
                "app_id": "request-app",
            }
        },
    )
    handle = None
    try:
        handle = open_gateway_request(env)
        request_identity = IdentityStore.get_identity()
        assert request_identity == IdentityInfo(
            user_id="request-user",
            domain_id="request-domain",
            app_id="request-app",
        )
        close_gateway_request(handle, error=terminal_error)
        assert observed_contexts
        assert observed_contexts == [
            (request_identity, "session-context-lifecycle")
        ] * len(observed_contexts)
        assert IdentityStore.get_identity() == outer_identity
        assert metrics_session_id.get() == "outer-session"
        for metric_call in telemetry_metrics.add.call_args_list:
            assert metric_call.args[2] == {JIUWENCLAW_CHANNEL_ID: "web"}
        for metric_call in telemetry_metrics.record.call_args_list:
            assert metric_call.args[2] == {JIUWENCLAW_CHANNEL_ID: "web"}
    finally:
        if handle is not None:
            close_gateway_request(handle)
        metrics_session_id.reset(outer_session_token)
        IdentityStore.clear(outer_identity_token)
        runtime.tracer_provider.shutdown()


def test_gateway_partial_open_failure_restores_parent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.telemetry import gateway as gateway_module

    runtime, _, _ = _runtime(monkeypatch)
    outer_identity = IdentityInfo(user_id="outer-user")
    outer_identity_token = IdentityStore.set_identity(outer_identity)

    class FailingSessionContext:
        set_values: list[str] = []

        @classmethod
        def set(cls, value: str):
            cls.set_values.append(value)
            raise RuntimeError("session context unavailable")

    monkeypatch.setattr(
        gateway_module,
        "metrics_session_id",
        FailingSessionContext,
        raising=False,
    )
    env = _envelope(
        "partial-open",
        stream=False,
        channel_context={"user_id": "request-user"},
    )
    handle = None
    try:
        handle = gateway_module.open_gateway_request(env)
        assert FailingSessionContext.set_values == ["session-partial-open"]
        assert handle.span is None
        assert IdentityStore.get_identity() == outer_identity
        gateway_module.close_gateway_request(handle)
        assert IdentityStore.get_identity() == outer_identity
        runtime.telemetry_metrics.add.assert_not_called()
        runtime.telemetry_metrics.record.assert_not_called()
    finally:
        if handle is not None:
            gateway_module.close_gateway_request(handle)
        IdentityStore.clear(outer_identity_token)
        runtime.tracer_provider.shutdown()


def test_gateway_context_reset_control_error_still_detaches_span_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.telemetry import gateway as gateway_module

    class ResetControlSignal(BaseException):
        pass

    runtime, exporter, _ = _runtime(monkeypatch)
    real_session_context = gateway_module.metrics_session_id

    class RestoreThenRaiseSessionContext:
        @staticmethod
        def reset(token) -> None:
            real_session_context.reset(token)
            raise ResetControlSignal

    outer_identity = IdentityInfo(user_id="outer-user")
    outer_identity_token = IdentityStore.set_identity(outer_identity)
    outer_session_token = metrics_session_id.set("outer-session")
    handle = gateway_module.open_gateway_request(
        _envelope("reset-control", stream=False)
    )
    monkeypatch.setattr(
        gateway_module,
        "metrics_session_id",
        RestoreThenRaiseSessionContext,
    )
    try:
        with pytest.raises(ResetControlSignal):
            gateway_module.close_gateway_request(handle)
        assert metrics_session_id.get() == "outer-session"
        assert IdentityStore.get_identity() == outer_identity
        assert trace.get_current_span().get_span_context().is_valid is False
        assert len(exporter.get_finished_spans()) == 1
    finally:
        current_context = trace.get_current_span().get_span_context()
        if current_context.is_valid:
            from opentelemetry import context as otel_context

            otel_context.detach(handle.context_token)
        metrics_session_id.reset(outer_session_token)
        IdentityStore.clear(outer_identity_token)
        runtime.tracer_provider.shutdown()


def test_gateway_rejects_out_of_order_close_without_consuming_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.telemetry.gateway import (
        close_gateway_request,
        open_gateway_request,
    )

    runtime, exporter, telemetry_metrics = _runtime(monkeypatch)

    def exercise_nested_requests() -> None:
        base_identity = IdentityInfo(user_id="base-user")
        base_identity_token = IdentityStore.set_identity(base_identity)
        base_session_token = metrics_session_id.set("base-session")
        base_channel_token = metrics_channel_id.set("base-channel")
        outer = None
        inner = None
        try:
            outer_env = _envelope(
                "outer",
                stream=False,
                channel_context={"user_id": "u1"},
            )
            outer_env.session_id = "s1"
            outer = open_gateway_request(outer_env)
            outer_span_context = outer.span.get_span_context()

            inner_env = _envelope(
                "inner",
                stream=False,
                channel_context={"user_id": "u2"},
            )
            inner_env.session_id = "s2"
            inner = open_gateway_request(inner_env)
            inner_span_context = inner.span.get_span_context()
            metric_add_count = telemetry_metrics.add.call_count

            with pytest.raises(RuntimeError, match="LIFO"):
                close_gateway_request(outer)

            assert IdentityStore.get_identity() == IdentityInfo(user_id="u2")
            assert metrics_session_id.get() == "s2"
            assert metrics_channel_id.get() == "web"
            assert trace.get_current_span().get_span_context() == inner_span_context
            assert telemetry_metrics.add.call_count == metric_add_count
            telemetry_metrics.record.assert_not_called()
            assert exporter.get_finished_spans() == ()

            close_gateway_request(inner)
            assert IdentityStore.get_identity() == IdentityInfo(user_id="u1")
            assert metrics_session_id.get() == "s1"
            assert metrics_channel_id.get() == "web"
            assert trace.get_current_span().get_span_context() == outer_span_context

            close_gateway_request(outer)
            assert IdentityStore.get_identity() == base_identity
            assert metrics_session_id.get() == "base-session"
            assert metrics_channel_id.get() == "base-channel"
            assert trace.get_current_span().get_span_context().is_valid is False
            assert telemetry_metrics.record.call_count == 2
            assert len(exporter.get_finished_spans()) == 2
        finally:
            for handle in (inner, outer):
                if handle is not None:
                    try:
                        close_gateway_request(handle)
                    except RuntimeError:
                        pass
            metrics_channel_id.reset(base_channel_token)
            metrics_session_id.reset(base_session_token)
            IdentityStore.clear(base_identity_token)

    try:
        Context().run(exercise_nested_requests)
    finally:
        runtime.tracer_provider.shutdown()


@pytest.mark.asyncio
async def test_gateway_child_task_close_cannot_consume_parent_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.telemetry.gateway import (
        close_gateway_request,
        open_gateway_request,
    )

    runtime, exporter, telemetry_metrics = _runtime(monkeypatch)

    async def exercise_parent_request() -> None:
        base_identity = IdentityInfo(user_id="base-user")
        base_identity_token = IdentityStore.set_identity(base_identity)
        base_session_token = metrics_session_id.set("base-session")
        base_channel_token = metrics_channel_id.set("base-channel")
        handle = None
        try:
            env = _envelope(
                "parent-owned",
                stream=False,
                channel_context={"user_id": "u1"},
            )
            env.session_id = "s1"
            handle = open_gateway_request(env)
            request_span_context = handle.span.get_span_context()
            metric_add_count = telemetry_metrics.add.call_count

            async def attempt_child_close() -> RuntimeError | None:
                try:
                    close_gateway_request(handle)
                except RuntimeError as error:
                    return error
                return None

            ownership_error = await asyncio.create_task(attempt_child_close())

            assert ownership_error is not None
            assert "different context" in str(ownership_error)
            assert IdentityStore.get_identity() == IdentityInfo(user_id="u1")
            assert metrics_session_id.get() == "s1"
            assert metrics_channel_id.get() == "web"
            assert trace.get_current_span().get_span_context() == request_span_context
            assert telemetry_metrics.add.call_count == metric_add_count
            telemetry_metrics.record.assert_not_called()
            assert exporter.get_finished_spans() == ()

            close_gateway_request(handle)
            assert IdentityStore.get_identity() == base_identity
            assert metrics_session_id.get() == "base-session"
            assert metrics_channel_id.get() == "base-channel"
            assert trace.get_current_span().get_span_context().is_valid is False
            telemetry_metrics.record.assert_called_once()
            assert len(exporter.get_finished_spans()) == 1
        finally:
            if handle is not None:
                try:
                    close_gateway_request(handle)
                except RuntimeError:
                    pass
            metrics_channel_id.reset(base_channel_token)
            metrics_session_id.reset(base_session_token)
            IdentityStore.clear(base_identity_token)

    try:
        await asyncio.create_task(exercise_parent_request(), context=Context())
    finally:
        runtime.tracer_provider.shutdown()


@pytest.mark.asyncio
async def test_stream_cleanup_error_still_closes_gateway_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, exporter, telemetry_metrics = _runtime(monkeypatch)
    handler = _TestMessageHandler.create(_SuccessStreamClient())
    handler._pop_stream_tracking_and_broadcast = AsyncMock(
        side_effect=RuntimeError("cleanup failed")
    )

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await handler.process_stream(
            _envelope("stream-cleanup-error", stream=True),
            "session-stream-cleanup-error",
            None,
            emit_processing_status=False,
        )
    runtime.tracer_provider.shutdown()

    gateway_span = next(
        span for span in exporter.get_finished_spans() if span.name == "channel.request"
    )
    assert gateway_span.status.status_code is StatusCode.ERROR
    assert gateway_span.attributes[ERROR_TYPE] == "RuntimeError"
    assert telemetry_metrics.add.call_args_list[-1].args[0] == (
        "jiuwenclaw.request.error.count"
    )
    assert telemetry_metrics.record.call_args.args[0] == (
        "jiuwenclaw.request.duration"
    )


@pytest.mark.asyncio
async def test_cancelled_stream_keeps_cancelled_label_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, exporter, _ = _runtime(monkeypatch)
    client = _CancelledStreamClient()
    handler = _TestMessageHandler.create(client)
    handler._pop_stream_tracking_and_broadcast = AsyncMock(
        side_effect=RuntimeError("cleanup failed after cancel")
    )
    task = asyncio.create_task(
        handler.process_stream(
            _envelope("stream-cancel-cleanup-error", stream=True),
            "session-stream-cancel-cleanup-error",
            None,
            emit_processing_status=False,
        )
    )
    await client.started.wait()
    task.cancel()

    with pytest.raises(RuntimeError, match="cleanup failed after cancel"):
        await task
    runtime.tracer_provider.shutdown()

    gateway_span = next(
        span for span in exporter.get_finished_spans() if span.name == "channel.request"
    )
    assert gateway_span.status.status_code is StatusCode.ERROR
    assert gateway_span.attributes[ERROR_TYPE] == "RuntimeError"
    assert gateway_span.attributes[JIUWENCLAW_CANCELED] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wrapper_name", "impl_name"),
    [
        ("_handle_unary", "_handle_unary_impl"),
        ("_handle_stream", "_handle_stream_impl"),
    ],
)
async def test_real_agentserver_boundaries_restore_remote_parent_and_identity(
    monkeypatch: pytest.MonkeyPatch,
    wrapper_name: str,
    impl_name: str,
) -> None:
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.server.handlers import _default
    from jiuwenswarm.telemetry.gateway import (
        close_gateway_request,
        open_gateway_request,
    )

    runtime, exporter, _ = _runtime(monkeypatch)
    env = _envelope(f"request-{wrapper_name}", stream=wrapper_name == "_handle_stream")
    identity_token = IdentityStore.set_identity(
        IdentityInfo(
            user_id="remote-user", domain_id="remote-domain", app_id="remote-app"
        )
    )
    gateway_handle = open_gateway_request(env)
    request = e2a_to_agent_request(env)
    seen_identities: list[IdentityInfo | None] = []

    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server._agent_manager = None

    async def impl(_ctx: object, _request: object) -> None:
        seen_identities.append(IdentityStore.get_identity())
        span = runtime.tracer_provider.get_tracer("agentserver.boundary").start_span(
            f"bound.{wrapper_name}",
            kind=SpanKind.SERVER,
        )
        span.end()

    monkeypatch.setattr(_default, impl_name, impl)

    class _NullWs:
        async def send(self, text: str) -> None:
            return None

    from jiuwenswarm.server.context import AgentServerServices, RequestContext
    from jiuwenswarm.server.transports.sink import WSSink

    _ws = _NullWs()
    ctx = RequestContext(
        request=request,
        sink=WSSink(_ws, asyncio.Lock()),
        connection_id=str(id(_ws)),
        services=AgentServerServices(server),
    )
    try:
        await getattr(_default, wrapper_name)(ctx, request)
    finally:
        close_gateway_request(gateway_handle)
        IdentityStore.clear(identity_token)
        runtime.tracer_provider.shutdown()

    spans = exporter.get_finished_spans()
    gateway_span = next(span for span in spans if span.name == "channel.request")
    bound_span = next(span for span in spans if span.name == f"bound.{wrapper_name}")
    assert bound_span.context.trace_id == gateway_span.context.trace_id
    assert bound_span.parent.span_id == gateway_span.context.span_id
    assert seen_identities == [
        IdentityInfo(
            user_id="remote-user",
            domain_id="remote-domain",
            app_id="remote-app",
        )
    ]
    assert IdentityStore.get_identity() is None
    assert trace.get_current_span().get_span_context().is_valid is False


def test_w3c_trace_context_does_not_depend_on_global_propagator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.telemetry.context_propagation import (
        bind_incoming_request,
        reset_incoming_request,
    )
    from jiuwenswarm.telemetry.gateway import (
        close_gateway_request,
        open_gateway_request,
    )

    runtime, exporter, _ = _runtime(monkeypatch)
    original_propagator = get_global_textmap()
    set_global_textmap(CompositePropagator([]))
    env = _envelope("explicit-w3c", stream=False)
    binding = None
    handle = None
    try:
        handle = open_gateway_request(env)
        assert "traceparent" in env.channel_context
        request = e2a_to_agent_request(env)
        binding = bind_incoming_request(request)
        child = runtime.tracer_provider.get_tracer("explicit.w3c").start_span(
            "explicit.child",
            kind=SpanKind.SERVER,
        )
        child.end()
    finally:
        if binding is not None:
            reset_incoming_request(binding)
        if handle is not None:
            close_gateway_request(handle)
        set_global_textmap(original_propagator)
        runtime.tracer_provider.shutdown()

    spans = exporter.get_finished_spans()
    gateway_span = next(span for span in spans if span.name == "channel.request")
    child_span = next(span for span in spans if span.name == "explicit.child")
    assert child_span.context.trace_id == gateway_span.context.trace_id
    assert child_span.parent.span_id == gateway_span.context.span_id


def test_process_stream_keeps_keyword_only_emit_processing_status() -> None:
    parameter = inspect.signature(MessageHandler.process_stream).parameters[
        "emit_processing_status"
    ]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is True
