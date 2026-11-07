"""Option-key validation for agent()/agent_session() in the swarmskill validator."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

_VALIDATOR = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm/resources/agent/workspace/skills/swarmskill-creator/scripts/validate_swarmskill.py"
)


@pytest.fixture(scope="module")
def validator():
    spec = importlib.util.spec_from_file_location("validate_swarmskill", _VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _issues(validator, src: str) -> list[str]:
    tree = ast.parse(src)
    # _runtime_call_issues reads _parent links that its own caller installs.
    validator._set_parent_links(tree)
    errors, _warnings = validator._runtime_call_issues(tree)
    return errors


_OK = '''
from swarmflow import agent
async def run(args):
    return await agent("x", label="clip", options={"agent_template": "clip", "model": "m"})
'''
_PREFIX = _OK.replace('"clip", "model"', '"designer/clip", "model"')
_UNKNOWN = _OK.replace('"model": "m"', '"modle": "m"')
_SESSION_BAD = '''
from swarmflow import agent_session
async def run(args):
    s = agent_session(label="chat", options={"agent_template": "designer/clip"})
    return await s.send("x")
'''


def test_known_option_keys_pass(validator):
    assert _issues(validator, _OK) == []


def test_unknown_option_key_is_an_error(validator):
    errors = _issues(validator, _UNKNOWN)
    assert any("unknown agent option" in e and "modle" in e for e in errors)


def test_agent_template_with_group_prefix_is_an_error(validator):
    errors = _issues(validator, _PREFIX)
    assert any("agent_template" in e and "bare member name" in e for e in errors)


def test_agent_template_leader_is_an_error(validator):
    # the Team Leader runs the conversation; only worker members can be swarmflow nodes
    errors = _issues(validator, _OK.replace('"clip", "model"', '"leader", "model"'))
    assert any("agent_template" in e and "Team Leader" in e for e in errors)


def test_agent_session_options_are_validated_too(validator):
    errors = _issues(validator, _SESSION_BAD)
    assert any("agent_template" in e for e in errors)


def test_supported_option_set_matches_engine(validator):
    assert validator.SUPPORTED_AGENT_OPTIONS == frozenset(
        {"label", "phase", "schema", "model", "timeout", "isolation", "agent_type", "builtin_model", "effort", "agent_template"}
    )
