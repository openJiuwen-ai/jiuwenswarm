# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise Agent/Team file output against a real host-owned OTel provider."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("start_order", ["agent,team", "team,agent", "team", "agent"])
def test_file_output_follows_active_runtimes(tmp_path, start_order):
    # A fresh interpreter isolates OTel's process-wide provider and SDK demand
    # state. Only configuration/trajectory storage are stubbed; SDK lifecycle,
    # BatchSpanProcessor and TraceFileExporter are real.
    script = r'''
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from openjiuwen.extensions.observability import setup
from jiuwenswarm.agents.harness import agent_observability as agent
from jiuwenswarm.agents.harness.team import team_manager as team

root = Path(sys.argv[1])
order = sys.argv[2].split(',')
provider = TracerProvider()
cloud = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(cloud))
trace.set_tracer_provider(provider)
config = {
    'agent_observability': {'enabled': False, 'exporter': 'file', 'traces_dir': str(root)},
    'team_observability': {'enabled': False, 'exporter': 'file', 'traces_dir': str(root)},
    'trajectory_ui': {'enabled': False},
}
for module in (agent, team):
    module.get_config = lambda: config
    module.get_skill_evolution_enabled = lambda cfg: False
    module.sync_trajectory_runtime = lambda *args, **kwargs: None
agent.shutdown_trajectory_runtime = lambda *args, **kwargs: True
team._get_unified_runtime = lambda: SimpleNamespace(is_unified_active=lambda: False)
sync = {'agent': agent.sync_agent_observability, 'team': team.sync_team_observability}

def file_names():
    names = []
    for path in root.glob('*.jsonl'):
        for line in path.read_text(encoding='utf-8').splitlines():
            for resource in json.loads(line)['resourceSpans']:
                for scope in resource['scopeSpans']:
                    names.extend(span['name'] for span in scope['spans'])
    return names

def emit(name, *, expect_file):
    with setup.get_tracer('openjiuwen.harness.observability').start_as_current_span(name):
        pass
    provider.force_flush()
    assert [s.name for s in cloud.get_finished_spans()].count(name) == 1, name
    assert file_names().count(name) == int(expect_file), (name, file_names())

try:
    for runtime in order:
        config[f'{runtime}_observability']['enabled'] = True
        sync[runtime]()
        sync[runtime]()
    emit('initial', expect_file=True)
    for index, runtime in enumerate(order):
        config[f'{runtime}_observability']['enabled'] = False
        sync[runtime]()
        # Check before the remaining runtime gets another request: its file
        # output must never be interrupted by the other runtime's shutdown.
        emit(f'after-disable-{runtime}', expect_file=index < len(order) - 1)
    # Shutdown/re-enable must not leak a file exporter or stack duplicate ones.
    for cycle in range(2):
        runtime = order[-1]
        config[f'{runtime}_observability']['enabled'] = True
        sync[runtime]()
        sync[runtime]()
        emit(f'restarted-{cycle}', expect_file=True)
        config[f'{runtime}_observability']['enabled'] = False
        sync[runtime]()
        emit(f'host-after-stop-{cycle}', expect_file=False)
finally:
    agent.shutdown_agent_observability()
    team.shutdown_team_observability()
    setup.shutdown_observability()
    provider.shutdown()
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), start_order],
        env=dict(os.environ, agentarts_agentops_otel_agent_apm_enable="true"),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-5000:]
