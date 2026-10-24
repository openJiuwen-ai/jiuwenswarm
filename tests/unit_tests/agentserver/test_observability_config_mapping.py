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


def test_agentarts_binding_preserves_an_existing_private_provider() -> None:
    import os
    import subprocess
    import sys

    script = '''
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
setup.init_observability(config, span_exporter_override=private_exporter)
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
        [sys.executable, "-c", script],
        env=dict(os.environ, agentarts_agentops_otel_agent_apm_enable="true"),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
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
