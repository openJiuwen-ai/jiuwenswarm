import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tool_progress_context import (
    bind_tool_progress,
    reset_tool_progress,
)
from jiuwenswarm.agents.harness.common.tools.symphony_toolkits import (
    SymphonyToolkit,
)
from jiuwenswarm.extensions.registry import ExtensionRegistry


class _CallbackFramework:
    @staticmethod
    def register_sync(*args, **kwargs):
        return None

    async def trigger(self, *args, **kwargs):
        return None


def setup_function():
    ExtensionRegistry.reset_instance()


def teardown_function():
    ExtensionRegistry.reset_instance()


@pytest.fixture(autouse=True)
def enabled_symphony_config(monkeypatch):
    def fake_load_symphony_config(config=None):
        raw = config.get("symphony") if isinstance(config, dict) else None
        enabled = raw.get("enabled", True) if isinstance(raw, dict) else True
        return SimpleNamespace(enabled=bool(enabled))

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.symphony_toolkits.load_symphony_config",
        fake_load_symphony_config,
    )


def _registry() -> ExtensionRegistry:
    return ExtensionRegistry.create_instance(
        callback_framework=_CallbackFramework(),
        config={},
        logger=object(),
    )


def test_plan_calls_compose_rpc_once_without_language():
    registry = _registry()
    calls = []

    async def handler(params, request=None):
        del request
        calls.append(params)
        return {"success": True, "content": "## Plan", "direct_display": True}

    registry.register_rpc_handler("symphony.plan", handler)

    result = asyncio.run(SymphonyToolkit().plan("use installed skills"))

    assert calls == [{"query": "use installed skills"}]
    assert result == {
        "success": True,
        "content": "## Plan",
        "direct_display": True,
        "continue_after_display": True,
        "followup_action": "external_skill_discovery",
    }


def test_plan_passes_mode_and_deduplicated_candidates():
    registry = _registry()
    seen = {}

    async def handler(params, request=None):
        del request
        seen.update(params)
        return {"success": True}

    registry.register_rpc_handler("symphony.plan", handler)

    asyncio.run(
        SymphonyToolkit().plan(
            "compose",
            mode="beam",
            candidate_skill_ids=["skill-a", "skill-a", "Skill B"],
        )
    )

    assert seen == {
        "query": "compose",
        "mode": "beam",
        "candidate_skill_ids": ["skill-a", "Skill B"],
    }


def test_plan_preserves_progress_callback_on_extension_request():
    registry = _registry()
    events = []

    async def handler(params, request=None):
        del params
        callback = request.metadata["symphony_progress_callback"]
        await callback({"event": "started", "graph": {"nodes": [], "edges": []}})
        return {"success": True}

    registry.register_rpc_handler("symphony.plan", handler)
    token = bind_tool_progress(events.append)
    try:
        asyncio.run(SymphonyToolkit().plan("compose", mode="beam"))
    finally:
        reset_tool_progress(token)

    assert events == [{"event": "started", "graph": {"nodes": [], "edges": []}}]


def test_plan_returns_compact_plan_and_beam_graph():
    registry = _registry()

    async def handler(params, request=None):
        del params, request
        return {
            "success": True,
            "content": "## Beam Plan",
            "result": {
                "beam_search": {
                    "language": "cn",
                    "round_index": 2,
                    "graph": {
                        "nodes": [
                            {
                                "id": "skill-a",
                                "label": "Skill A",
                                "status": "final",
                                "seed": True,
                                "unused": "drop",
                            }
                        ],
                        "edges": [],
                    },
                },
                "recommended_plans": [
                    {
                        "title": "Plan",
                        "status": "ready",
                        "steps": [{"skill_id": "skill-a", "name": "Skill A"}],
                        "can_feed_edges": [],
                        "missing_inputs": [],
                    }
                ],
            },
        }

    registry.register_rpc_handler("symphony.plan", handler)

    result = asyncio.run(SymphonyToolkit().plan("compose", mode="beam"))

    assert result["beam_search"] == {
        "language": "cn",
        "round_index": 2,
        "graph": {
            "nodes": [
                {
                    "id": "skill-a",
                    "label": "Skill A",
                    "status": "final",
                    "seed": True,
                }
            ],
            "edges": [],
        },
    }
    assert result["plan"]["steps"] == [
        {"step": 1, "skill_id": "skill-a", "name": "Skill A"}
    ]
    assert "result" not in result


def test_plan_preserves_dynamic_graph_metadata():
    registry = _registry()

    async def handler(params, request=None):
        del params, request
        return {
            "success": True,
            "plan_id": "plan-session-1",
            "dynamic_graph_enabled": True,
            "result": {"recommended_plans": []},
        }

    registry.register_rpc_handler("symphony.plan", handler)

    result = asyncio.run(SymphonyToolkit().plan("compose"))

    assert result["plan_id"] == "plan-session-1"
    assert result["dynamic_graph_enabled"] is True


def test_toolkit_compacts_inferred_edge_provenance():
    edge = SymphonyToolkit._compact_can_feed_edge(
        {
            "source_id": "skill-a",
            "target_id": "skill-b",
            "confidence": None,
            "method": "fast_llm_inferred",
            "reason": "LLM connected retrieved candidates.",
            "port_mappings": [],
        }
    )

    assert edge == {
        "source_id": "skill-a",
        "target_id": "skill-b",
        "method": "fast_llm_inferred",
        "reason": "LLM connected retrieved candidates.",
    }


def test_plan_reports_missing_rpc_handler():
    _registry()

    result = asyncio.run(SymphonyToolkit().plan("compose"))

    assert result["success"] is False
    assert "handler not registered" in result["detail"]


def test_status_and_refresh_remain_explicit_tools():
    registry = _registry()
    registry.register_rpc_handler(
        "symphony.score_status",
        lambda params, request=None: {"success": True, "exists": True},
    )
    registry.register_rpc_handler(
        "symphony.build_score",
        lambda params, request=None: {"success": True, "rebuilt": True},
    )

    assert asyncio.run(SymphonyToolkit().score_status())["exists"] is True
    assert asyncio.run(SymphonyToolkit().refresh_score())["rebuilt"] is True


def test_get_tools_describes_fast_and_beam_without_language():
    compose_tool = next(
        tool
        for tool in SymphonyToolkit().get_tools()
        if tool.card.name == "symphony_compose_score"
    )
    properties = compose_tool.card.input_params["properties"]

    assert properties["mode"]["enum"] == ["fast", "beam"]
    assert "language" not in properties
    assert "most relevant" in properties["candidate_skill_ids"]["description"]
    assert "fast is the default" in properties["mode"]["description"]


def test_disabled_config_hides_tools():
    assert SymphonyToolkit().get_tools({"symphony": {"enabled": False}}) == []
