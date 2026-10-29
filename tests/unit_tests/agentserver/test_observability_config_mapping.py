# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from jiuwenswarm.agents.harness.observability_runtime import build_observability_config


@pytest.mark.parametrize('runtime', ['agent', 'team'])
@pytest.mark.parametrize('probe_enabled', [True, False])
def test_agentarts_provider_exports_agentcore_parents(tmp_path, runtime, probe_enabled) -> None:
    """The wheel and AgentCore must export the same complete span tree."""
    import os
    import subprocess
    import sys

    script = '''
import sys
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from openjiuwen.extensions.observability.setup import get_tracer, shutdown_observability
from jiuwenswarm.agents.harness.observability_runtime import (
    build_observability_config, bind_agentarts_tracer_provider,
)
provider = TracerProvider()
exporter = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
config = build_observability_config(
    {'exporter': 'file'}, service_name='local-agent', traces_dir=sys.argv[1],
)
assert bind_agentarts_tracer_provider(config) is (sys.argv[3] == 'true')
assert bind_agentarts_tracer_provider(config) is (sys.argv[3] == 'true')
if sys.argv[2] == 'agent':
    from openjiuwen.harness.observability import acquire_observability
else:
    from openjiuwen.agent_teams.observability import acquire_observability
acquire_observability(config)
acquire_observability(config)
with get_tracer('openjiuwen.harness.observability').start_as_current_span('agent-root') as root:
    with trace.get_tracer('opentelemetry.instrumentation.jiuwen').start_as_current_span('probe-child'):
        pass
spans = exporter.get_finished_spans()
if sys.argv[3] == 'true':
    assert len(spans) == 2, [(span.name, span.parent) for span in spans]
    child, parent = spans
    assert child.context.trace_id == parent.context.trace_id
    assert child.parent.span_id == parent.context.span_id
else:
    assert [span.name for span in spans] == ['probe-child'], 'Opt-out must retain native provider behavior'
shutdown_observability()
with trace.get_tracer('host').start_as_current_span('still-running'):
    pass
assert exporter.get_finished_spans()[-1].name == 'still-running', 'AgentCore must not shut down the wheel provider'
'''
    enabled = str(probe_enabled).lower()
    env = dict(os.environ, agentarts_agentops_otel_agent_apm_enable=enabled)
    result = subprocess.run(
        [sys.executable, '-c', script, str(tmp_path), runtime, enabled],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-5000:]


@pytest.mark.parametrize('initialization_order', ['before-bind', 'during-bind'])
def test_agentarts_binding_preserves_an_existing_private_provider(initialization_order) -> None:
    import os
    import subprocess
    import sys

    script = '''
import sys
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from openjiuwen.extensions.observability import setup
from jiuwenswarm.agents.harness.observability_runtime import (
    build_observability_config, bind_agentarts_tracer_provider,
)
host = TracerProvider()
host_exporter = InMemorySpanExporter()
host.add_span_processor(SimpleSpanProcessor(host_exporter))
trace.set_tracer_provider(host)
private_exporter = InMemorySpanExporter()
config = build_observability_config({}, service_name='private', traces_dir='unused')
initialize = setup.init_observability
if sys.argv[1] == 'before-bind':
    initialize(config, span_exporter_override=private_exporter)
else:
    def initialize_after_competitor(*args, **kwargs):
        # Another initializer wins after the binding helper checks SDK state.
        # Both initializations below use the real SDK identity/ownership checks.
        initialize(config, span_exporter_override=private_exporter)
        initialize(*args, **kwargs)
    setup.init_observability = initialize_after_competitor
try:
    assert bind_agentarts_tracer_provider(config) is False
    with setup.get_tracer('private').start_as_current_span('private-span'):
        pass
    assert len(private_exporter.get_finished_spans()) == 1
    assert host_exporter.get_finished_spans() == ()
finally:
    setup.shutdown_observability()
    host.shutdown()
'''
    result = subprocess.run(
        [sys.executable, "-c", script, initialization_order],
        env=dict(os.environ, agentarts_agentops_otel_agent_apm_enable="true"),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-5000:]


def test_agentarts_binding_propagates_actual_initialization_failure(monkeypatch):
    from openjiuwen.extensions.observability import setup
    from opentelemetry.sdk.trace import TracerProvider

    from jiuwenswarm.agents.harness.observability_runtime import (
        bind_agentarts_tracer_provider,
    )

    provider = TracerProvider()
    monkeypatch.setenv('agentarts_agentops_otel_agent_apm_enable', 'true')
    monkeypatch.setattr('opentelemetry.trace.get_tracer_provider', lambda: provider)
    monkeypatch.setattr(setup, 'is_initialized', lambda: False)

    def fail_initialization(*args, **kwargs):
        raise RuntimeError('callback registration failed')

    monkeypatch.setattr(setup, 'init_observability', fail_initialization)
    try:
        with pytest.raises(RuntimeError, match='callback registration failed'):
            bind_agentarts_tracer_provider(object())
    finally:
        provider.shutdown()


@pytest.mark.parametrize('mode', ['unified', 'host-shared', 'host-conflict'])
def test_team_provider_binding_with_unified_runtime(tmp_path, mode):
    """ACTIVE already binds AgentCore; a conflicting host takes the fallback."""
    import os
    import subprocess
    import sys

    script = '''
import asyncio
import sys
from types import SimpleNamespace
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from openjiuwen.core.runner import Runner
from openjiuwen.extensions.observability import setup
from jiuwenswarm.agents.harness.team import team_manager as team
from jiuwenswarm.telemetry import runtime as module
from jiuwenswarm.telemetry.config import TelemetryConfig
from jiuwenswarm.telemetry.provider import build_default_providers

async def main():
    mode = sys.argv[1]
    config = TelemetryConfig(enabled=True)
    bundle = build_default_providers(config)
    host = TracerProvider() if mode == 'host-conflict' else bundle.tracer_provider
    cloud = InMemorySpanExporter()
    host.add_span_processor(SimpleSpanProcessor(cloud))
    if mode != 'unified':
        trace.set_tracer_provider(host)
    if mode == 'host-shared':
        bundle.owns_tracer = False
    module.load_telemetry_config = lambda: config
    module.build_provider_bundle = lambda *args, **kwargs: bundle
    module.get_config = lambda: {}
    runtime = module.TelemetryRuntime()
    team._get_unified_runtime = lambda: runtime
    team.get_config = lambda: {
        'team_observability': {'enabled': True, 'exporter': 'file', 'traces_dir': sys.argv[2]},
        'trajectory_ui': {'enabled': False},
    }
    team.get_skill_evolution_enabled = lambda cfg: False
    team.sync_trajectory_runtime = lambda *args, **kwargs: None
    registry = SimpleNamespace(callback_framework=Runner.callback_framework)
    try:
        state = await runtime.start(process_role='agentserver', registry=registry, extension_manager=None)
        if mode == 'host-conflict':
            assert state is module.RuntimeState.DEGRADED
            assert not runtime.is_unified_active()
            assert 'different global' in runtime.component_status()['traces'].error
            assert not setup.is_initialized()
        else:
            assert state is module.RuntimeState.ACTIVE, runtime.component_status()
            assert runtime.is_unified_active()
            assert runtime.component_status()['agentcore'].active
            assert setup.is_initialized()
            assert runtime.tracer_provider is trace.get_tracer_provider() is host
        team.sync_team_observability()
        with setup.get_tracer('openjiuwen.agent_teams.observability').start_as_current_span('team-root'):
            with trace.get_tracer('probe').start_as_current_span('probe-child'):
                pass
        host.force_flush()
        spans = cloud.get_finished_spans()
        assert [span.name for span in spans] == ['probe-child', 'team-root']
        child, parent = spans
        assert child.context.trace_id == parent.context.trace_id
        assert child.parent.span_id == parent.context.span_id
        team.shutdown_team_observability()
        with trace.get_tracer('host').start_as_current_span('after-team-stop'):
            pass
        assert cloud.get_finished_spans()[-1].name == 'after-team-stop'
    finally:
        team.shutdown_team_observability()
        setup.shutdown_observability()
        await runtime.stop()
        if not bundle.owns_tracer or mode == 'host-conflict':
            host.shutdown()

asyncio.run(main())
'''
    result = subprocess.run(
        [sys.executable, '-c', script, mode, str(tmp_path)],
        env=dict(os.environ, agentarts_agentops_otel_agent_apm_enable='true'),
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr[-5000:]


def test_single_agent_native_backend_defaults_are_forwarded() -> None:
    config = build_observability_config(
        {},
        service_name="jiuwenswarm-agent",
        default_backend="otlp",
        traces_dir="/tmp/traces",
    )

    assert config.backend == "otlp"
    assert config.max_attributes == 200


def test_observability_backend_and_attribute_limit_can_be_configured() -> None:
    config = build_observability_config(
        {"backend": "langfuse", "max_attributes": 512},
        service_name="jiuwenswarm-agent",
        default_backend="otlp",
        traces_dir="/tmp/traces",
    )

    assert config.backend == "langfuse"
    assert config.max_attributes == 512


def test_trace_message_redaction_settings_are_forwarded_to_sdk() -> None:
    config = build_observability_config(
        {"redact_prompts": True, "redact_completions": True},
        service_name="jiuwenswarm-agent",
        default_backend="otlp",
        traces_dir="/tmp/traces",
    )

    assert config.redact_prompts is True
    assert config.redact_completions is True
