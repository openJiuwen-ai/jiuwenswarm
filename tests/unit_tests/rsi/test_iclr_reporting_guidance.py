"""Completion guidance reaches actual writer prompts; no model or network calls."""
from unittest.mock import AsyncMock, patch
from importlib import import_module

import pytest

from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.reporting.agent import ReportingAgent

def _agent_type():
    return import_module('jiuwenswarm.agents.harness.common.rsi.iclr_reporting').IclrReportingAgent


def _source(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    for name in ('iclr2026_conference.sty', 'iclr2026_conference.bst', 'natbib.sty', 'fancyhdr.sty'):
        (source / name).write_text('offline template fixture', encoding='utf8')
    return source


@pytest.mark.asyncio
async def test_retry_query_separates_host_soft_notes_from_real_blockers(tmp_path, monkeypatch):
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common import workspace
    monkeypatch.setattr(workspace, '_PROJECT_ROOT', tmp_path / 'run')
    with patch.object(ReportingAgent, '_run_paper_agent', new_callable=AsyncMock, return_value='real session error') as run:
        reporter = _agent_type()({}, template_dir=_source(tmp_path), model=object())
        actual = await reporter._run_paper_agent(run_id='retry-guidance', query='Retained prior notes: introduction too short; four sections missing.')
    assert actual.startswith('real session error')
    query = run.call_args.kwargs['query']
    assert 'Word-count notes are non-blocking' in query
    assert 'missing sections, unknown citation keys, session errors and compiler errors remain blocking' in query
    assert 'write missing sections before rechecking existing ones' in query
    assert 'do not repeatedly reconstruct supplied verified_facts from results.json' in query


def test_real_writer_system_and_react_prompt_share_retry_and_safe_tool_policy(tmp_path, monkeypatch):
    from openjiuwen.core.foundation.llm import Model
    from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common import workspace

    monkeypatch.setattr(workspace, '_PROJECT_ROOT', tmp_path / 'run')
    model = Model(model_client_config=ModelClientConfig(client_provider='OpenAI', api_key='unused-test', api_base='http://127.0.0.1:1/v1', max_retries=0), model_config=ModelRequestConfig(model='offline-test'))
    reporter = _agent_type()({'reporting': {'method_figure': {'enabled': False}}}, template_dir=_source(tmp_path), model=model)
    monkeypatch.setattr(reporter, '_write_latex_runtime_config', lambda paper: None)
    reporter._latex_runtime = object()
    agent = reporter._build_paper_agent(run_id='system-guidance')
    for prompt in (agent.deep_config.system_prompt, agent.system_prompt_builder.build(), agent._react_agent.config.prompt_template[0]['content']):
        assert 'Word-count notes are non-blocking' in prompt
        assert 'write missing sections before rechecking existing ones' in prompt
        assert 'switch to glob/read_file without widening sandbox roots' in prompt


@pytest.mark.asyncio
async def test_actual_writer_prompts_bind_execution_and_completed_control_facts(tmp_path, monkeypatch):
    from openjiuwen.core.foundation.llm import Model
    from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig, ModelRequestConfig
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common import workspace

    monkeypatch.setattr(workspace, '_PROJECT_ROOT', tmp_path / 'run')
    model = Model(model_client_config=ModelClientConfig(client_provider='OpenAI', api_key='unused-test', api_base='http://127.0.0.1:1/v1', max_retries=0), model_config=ModelRequestConfig(model='offline-test'))
    reporter = _agent_type()({'reporting': {'method_figure': {'enabled': False}}}, template_dir=_source(tmp_path), model=model)
    monkeypatch.setattr(reporter, '_write_latex_runtime_config', lambda paper: None)
    reporter._latex_runtime = object()
    agent = reporter._build_paper_agent(run_id='trace-facts-guidance')
    with patch.object(ReportingAgent, '_run_paper_agent', new_callable=AsyncMock, return_value='') as run:
        await reporter._run_paper_agent(run_id='trace-facts-guidance', query='Current reports: Execution and Reporting only; verified_facts include completed input-token-matched controls.')
    prompts = (agent.deep_config.system_prompt, agent.system_prompt_builder.build(),
        agent._react_agent.config.prompt_template[0]['content'], run.call_args.kwargs['query'])
    for prompt in prompts:
        assert 'Configured enabled modules do not prove actual execution' in prompt
        assert 'Without a supplied actual Reflection report, do not claim this run executed Reflection' in prompt
        assert 'Use verified_facts to identify completed input-token matching' in prompt
        assert 'output-length or compute matching' in prompt
        assert 'Shared initial-correct responses contribute zero to a paired arm contrast' in prompt
        assert 'Identical McNemar p-values may result from identical discordant counts' in prompt
        assert 'Zero-call replay logs describe reproduction, not original model acquisition' in prompt
        assert 'do not claim fresh evaluations from retained evidence' in prompt
