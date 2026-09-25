"""Assembling the co-scribe toolkit onto a team member.

Until now clouddoc reached an agent one way: imperative registration on the
single-agent path, which the watcher's own sessions use. A team member went through
the declarative path instead and got nothing, so co-scribe simply did not exist in team
mode.

The gating is what these tests are about, and one gate is not like the others.

Disabled, or no connection configured, is ordinary: build nothing, say so at info, move
on. **An unattended turn is different.** The single-agent path pairs these tools with a
second act -- it strips the session's ability set down to a closed allowlist, so a turn
that a comment triggered with nobody watching cannot reach bash. That stripping is
imperative and works on a live ability_manager; a provider returns elements and never
touches abilities, so there is no way to do it here.

Today no unattended turn reaches this path, because the watcher dispatches to a
single-agent session. But "no caller reaches it" is exactly how invariant ③ was being
upheld before the last review, and it was not a rule. So the factory refuses, and the
refusal has a test.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.swarm import registry
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers.runtime_tools import build_clouddoc_tools


CLOUDDOC_CHANNEL = "__clouddoc__"


@pytest.fixture
def key(tmp_path):
    """A credentials file shaped like a service-account key, never used to call out."""
    import json

    p = tmp_path / "sa.json"
    p.write_text(
        json.dumps({
            "type": "service_account",
            "client_email": "agent@example.iam.gserviceaccount.com",
            "token_uri": "https://oauth2.googleapis.com/token",
            "private_key": "",
            "project_id": "p",
        }),
        encoding="utf-8",
    )
    return str(p)


def _cfg(key: str, **over):
    cfg = {
        "enabled": True,
        "connections": [{"credentials_file": key, "documents": ["doc-1"]}],
    }
    cfg.update(over)
    return cfg


def test_a_team_member_gets_the_co_scribe_tools(key):
    """The point of the whole change: in team mode the tools now exist."""
    tools = build_clouddoc_tools(
        {"clouddoc_config": _cfg(key)},
        SwarmBuildContext(session_id="s1", channel_id="web"),
    )
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        ALL_TOOL_NAMES,
    )

    assert {t.card.name for t in tools} == set(ALL_TOOL_NAMES)


def test_an_unattended_turn_is_refused_rather_than_served(key):
    """The one gate that is a safety boundary rather than a convenience.

    This path cannot narrow the ability set, so a turn that requires a narrowed one gets
    no tools at all. Refusing costs nothing today -- no unattended turn arrives here --
    and is the difference between a rule and a coincidence on the day one does."""
    tools = build_clouddoc_tools(
        {"clouddoc_config": _cfg(key)},
        SwarmBuildContext(session_id="s1", channel_id=CLOUDDOC_CHANNEL),
    )
    assert tools == [], "无人值守回合必须拿不到工具，而不是拿到未收窄的全集"


def test_disabled_builds_nothing(key):
    tools = build_clouddoc_tools(
        {"clouddoc_config": _cfg(key, enabled=False)},
        SwarmBuildContext(session_id="s1", channel_id="web"),
    )
    assert tools == []


def test_no_configured_connection_builds_nothing():
    """A deployment with the feature on but no key is a half-configured one, not an
    error: the panel is where a person adds the key, and a member that failed to build
    would take the whole team down over it."""
    tools = build_clouddoc_tools(
        {"clouddoc_config": {"enabled": True, "connections": []}},
        SwarmBuildContext(session_id="s1", channel_id="web"),
    )
    assert tools == []


def test_an_unreadable_key_does_not_stop_the_member_being_built(tmp_path):
    """A corrupt key is a configuration problem for one capability. Raising here would
    fail the member, and a person would see a team that cannot start rather than a team
    without one toolkit."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    tools = build_clouddoc_tools(
        {
            "clouddoc_config": {
                "enabled": True,
                "connections": [{"credentials_file": str(bad), "documents": ["d"]}],
            }
        },
        SwarmBuildContext(session_id="s1", channel_id="web"),
    )
    assert tools == []


def test_the_element_is_declared_in_the_catalog():
    """Declared, or ``build_member_capability_specs`` cannot resolve it by name and the
    member is built without ever saying why."""
    from openjiuwen.agent_teams.harness.manifest import get_catalog

    from jiuwenswarm.agents.swarm.registry import register_swarm_providers

    register_swarm_providers()
    entry = get_catalog()[registry.CLOUDDOC_TOOLS]
    props = entry.input_schema["properties"]
    assert props["clouddoc_config"]["source"] == "params"
    assert props["channel_id"]["source"] == "context"


def test_every_tool_reaches_a_team_member(key):
    """The team runtime filters inherited abilities against TOOL_WHITELIST and logs a
    miss at debug, so a tool left out of it disappears with no error anywhere.

    Taken from the toolkit's own list rather than copied, which is what this checks:
    the design's as-built section said seven tools while the toolkit built nine, and a
    hand-copied whitelist would have carried exactly that gap into team mode."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools import (
        ALL_TOOL_NAMES,
    )
    from jiuwenswarm.agents.harness.team.team_runtime_inheritance import TOOL_WHITELIST

    built = {
        t.card.name
        for t in build_clouddoc_tools(
            {"clouddoc_config": _cfg(key)},
            SwarmBuildContext(session_id="s1", channel_id="web"),
        )
    }
    assert built == set(ALL_TOOL_NAMES), "工具集与权威清单不一致"
    assert built <= TOOL_WHITELIST, f"以下工具会被 team 白名单静默丢弃：{built - TOOL_WHITELIST}"


def test_a_team_member_reaches_every_connection(key, tmp_path):
    """A team turn is a person talking, so it is routed across all connections like
    the chat path; a second connection's documents must not be invisible to it."""
    import json

    second = tmp_path / "sa2.json"
    second.write_text(json.dumps({
        "type": "service_account",
        "client_email": "agent2@example.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token", "private_key": "", "project_id": "p",
    }), encoding="utf-8")
    cfg = _cfg(key)
    cfg["connections"].append({"credentials_file": str(second), "documents": ["doc-2"]})
    tools = build_clouddoc_tools(
        {"clouddoc_config": cfg}, SwarmBuildContext(session_id="s1", channel_id="web"),
    )
    assert tools, "two connections must still yield the tool set"


def test_the_configured_roster_reaches_the_providers(key, monkeypatch):
    """The chat host hands its providers the configured agent roster; the team host
    must hand over the same one, or a provider built here cannot tell another agent's
    mention from a person's."""
    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers import routing

    seen: dict = {}
    real = routing.build_routed_provider

    def capture(specs, **kw):
        seen.update(kw)
        return real(specs, **kw)

    monkeypatch.setattr(routing, "build_routed_provider", capture)
    tools = build_clouddoc_tools(
        {"clouddoc_config": _cfg(key, agent_roster=["ou_x"])},
        SwarmBuildContext(session_id="s1", channel_id="web"),
    )
    assert tools
    assert seen["agent_roster"] == ("ou_x",)
