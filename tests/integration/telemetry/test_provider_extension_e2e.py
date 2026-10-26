"""End-to-end lifecycle for an externally owned telemetry provider extension."""

from __future__ import annotations

import sys
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from openjiuwen.agent_teams.observability import shutdown_observability
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback import LLMCallEvents, ToolCallEvents

from jiuwenswarm.extensions.identity_provider import IdentityInfo, IdentityStore
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.manager import ExtensionManager
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk.telemetry_provider import TelemetryProviderExtension
from jiuwenswarm.telemetry import provider as provider_module
from jiuwenswarm.telemetry import runtime as runtime_module
from jiuwenswarm.telemetry.config import TelemetryConfig
from jiuwenswarm.telemetry.provider import ProviderBundle
from jiuwenswarm.telemetry.runtime import RuntimeState, TelemetryRuntime


class _CountingTracerProvider(TracerProvider):
    def __init__(self, *, resource: Resource) -> None:
        super().__init__(resource=resource)
        self.flush_calls = 0
        self.shutdown_calls = 0

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flush_calls += 1
        return super().force_flush(timeout_millis=timeout_millis)

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        super().shutdown()


class _CountingMeterProvider(MeterProvider):
    def __init__(self, *, resource: Resource, reader: InMemoryMetricReader) -> None:
        super().__init__(resource=resource, metric_readers=[reader])
        self.flush_calls = 0
        self.shutdown_calls = 0

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        self.flush_calls += 1
        return super().force_flush(timeout_millis=timeout_millis)

    def shutdown(self, timeout_millis: int = 30000) -> None:
        self.shutdown_calls += 1
        super().shutdown(timeout_millis=timeout_millis)


class _ProviderExtension(TelemetryProviderExtension):
    def __init__(self, bundle: ProviderBundle) -> None:
        self.bundle = bundle
        self.build_calls = 0
        self.initialize_calls = 0
        self.shutdown_calls = 0

    async def initialize(self, config) -> None:
        del config
        self.initialize_calls += 1

    def build_providers(self, cfg: TelemetryConfig) -> ProviderBundle:
        assert cfg.enabled is True
        self.build_calls += 1
        return self.bundle

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class ProxyTracerProvider:
    pass


class ProxyLoggerProvider:
    pass


class _ProxyMeterProvider:
    pass


def _metric_names(reader: InMemoryMetricReader) -> set[str]:
    data = reader.get_metrics_data()
    if data is None:
        return set()
    return {
        metric.name
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }


@pytest.mark.asyncio
async def test_runtime_claims_extension_and_only_flushes_external_providers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from jiuwenswarm.agents.harness import agent_observability

    shutdown_observability()
    identity_context_token = IdentityStore.set_identity(None)
    exporter = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    resource = Resource.create({"service.name": "provider-extension-e2e"})
    tracer_provider = _CountingTracerProvider(resource=resource)
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    meter_provider = _CountingMeterProvider(resource=resource, reader=reader)
    logger_provider = LoggerProvider(resource=resource)
    extension = _ProviderExtension(
        ProviderBundle(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            logger_provider=logger_provider,
            resource=resource,
            owns_tracer=False,
            owns_meter=False,
            owns_logger=False,
        )
    )
    support_name = f"_telemetry_extension_support_{id(extension)}"
    support_module = ModuleType(support_name)
    support_module.extension = extension
    monkeypatch.setitem(sys.modules, support_name, support_module)
    extension_root = tmp_path / "telemetry_probe"
    extension_root.mkdir()
    (extension_root / "extension.yaml").write_text(
        "name: telemetry-probe\n",
        encoding="utf-8",
    )
    (extension_root / "extension.py").write_text(
        (
            f"from {support_name} import extension\n\n"
            "async def register_extensions(registry):\n"
            "    await extension.initialize(None)\n"
            "    registry.register_telemetry_provider(extension)\n"
            "    return extension\n"
        ),
        encoding="utf-8",
    )
    registry = ExtensionRegistry(
        callback_framework=Runner.callback_framework,
        config={},
        logger=SimpleNamespace(warning=lambda *_args: None),
    )
    manager = ExtensionManager(registry=registry)
    manager.loader = ExtensionLoader(registry)
    manager.loader.add_search_path(tmp_path)

    def cleanup_external_providers() -> None:
        shutdown_observability()
        tracer_provider.shutdown()
        meter_provider.shutdown()
        logger_provider.shutdown()
        IdentityStore.clear(identity_context_token)
        sys.modules.pop("jiuwenswarm.loaded_extension.telemetry_probe", None)

    try:
        await manager.load_all_extensions()
        assert registry.get_telemetry_provider_extension() is extension
        assert manager._loaded_extensions == [extension]
        assert extension.initialize_calls == 1
    except BaseException:
        await manager.shutdown_all_extensions()
        cleanup_external_providers()
        raise
    runtime = TelemetryRuntime()
    defaults = Mock(side_effect=AssertionError("default providers must not be built"))
    globals_state = SimpleNamespace(
        tracer=ProxyTracerProvider(),
        meter=_ProxyMeterProvider(),
        logger=ProxyLoggerProvider(),
    )
    monkeypatch.setattr(
        runtime_module,
        "load_telemetry_config",
        lambda: TelemetryConfig(
            enabled=True,
            traces_exporter="none",
            metrics_exporter="none",
            service_name="provider-extension-e2e",
        ),
    )
    monkeypatch.setattr(provider_module, "build_default_providers", defaults)
    monkeypatch.setattr(
        runtime_module.trace,
        "get_tracer_provider",
        lambda: globals_state.tracer,
    )
    monkeypatch.setattr(
        runtime_module.trace,
        "set_tracer_provider",
        lambda provider: setattr(globals_state, "tracer", provider),
    )
    monkeypatch.setattr(
        runtime_module.metrics,
        "get_meter_provider",
        lambda: globals_state.meter,
    )
    monkeypatch.setattr(
        runtime_module.metrics,
        "set_meter_provider",
        lambda provider: setattr(globals_state, "meter", provider),
    )
    monkeypatch.setattr(
        runtime_module._logs,
        "get_logger_provider",
        lambda: globals_state.logger,
    )
    monkeypatch.setattr(
        runtime_module._logs,
        "set_logger_provider",
        lambda provider: setattr(globals_state, "logger", provider),
    )
    monkeypatch.setattr(
        agent_observability,
        "_get_unified_runtime",
        lambda: runtime,
    )

    stopped = False
    try:
        state = await runtime.start(
            process_role="agentserver",
            registry=registry,
            extension_manager=manager,
        )
        assert state is RuntimeState.ACTIVE
        assert manager._loaded_extensions == []
        assert extension.build_calls == 1
        defaults.assert_not_called()

        identity_token = IdentityStore.set_identity(
            IdentityInfo(
                user_id="provider-user",
                domain_id="provider-domain",
                app_id="provider-app",
            )
        )
        handle = agent_observability.open_agent_run_span(
            session_id="provider-session",
            request_id="provider-request",
            channel_id="provider-channel",
            mode="agent.fast",
        )
        assert handle is not None
        try:
            await Runner.callback_framework.trigger(
                LLMCallEvents.LLM_INVOKE_INPUT,
                messages=[{"role": "user", "content": "provider path"}],
                model="provider-model",
                call_id="provider-llm",
            )
            await Runner.callback_framework.trigger(
                ToolCallEvents.TOOL_CALL_STARTED,
                tool_name="provider_tool",
                tool_id="provider-tool",
                inputs=(("payload",), {}),
            )
            await Runner.callback_framework.trigger(
                ToolCallEvents.TOOL_CALL_FINISHED,
                tool_name="provider_tool",
                tool_id="provider-tool",
                inputs=(("payload",), {}),
                result={"ok": True},
            )
            result = SimpleNamespace(
                content="provider result",
                finish_reason="stop",
                tool_calls=[],
                usage_metadata=SimpleNamespace(input_tokens=6, output_tokens=2),
            )
            callback_results = await Runner.callback_framework.trigger(
                LLMCallEvents.LLM_INVOKE_OUTPUT,
                result=result,
                call_id="provider-llm",
            )
            assert callback_results[0] is result
        finally:
            agent_observability.close_agent_run_span(
                handle,
                session_id="provider-session",
            )
            IdentityStore.clear(identity_token)

        tracer_provider.force_flush()
        spans = list(exporter.get_finished_spans())
        assert [span.name for span in spans].count("llm.call") == 1
        assert [span.name for span in spans].count("tool.provider_tool") == 1
        assert {span.attributes.get("user.id") for span in spans} == {"provider-user"}
        assert {
            "gen_ai.client.operation.count",
            "gen_ai.tool.call.count",
        } <= _metric_names(reader)

        tracer_flushes_before_stop = tracer_provider.flush_calls
        meter_flushes_before_stop = meter_provider.flush_calls
        await runtime.stop()
        stopped = True
        await manager.shutdown_all_extensions()

        assert extension.shutdown_calls == 1
        assert tracer_provider.flush_calls > tracer_flushes_before_stop
        assert meter_provider.flush_calls > meter_flushes_before_stop
        assert tracer_provider.shutdown_calls == 0
        assert meter_provider.shutdown_calls == 0
    finally:
        if not stopped:
            await runtime.stop()
            await manager.shutdown_all_extensions()
        cleanup_external_providers()
