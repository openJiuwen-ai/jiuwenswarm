import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.extensions.symphony.extension import (
    SYMPHONY_BUILD_SCORE,
    SYMPHONY_GRAPH,
    SYMPHONY_PAUSE_BUILD,
    SYMPHONY_PLAN,
    SYMPHONY_SCORE_STATUS,
    SymphonyExtension,
    _BuildProcessLogger,
    _build_log_payload,
    _build_presentation,
    _latest_effective_build_log_entry,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.symphony.config import symphony_config_from_dict
from jiuwenswarm.symphony.orchestration.artifacts import ScoreArtifacts

_REAL_SCORE_STATUS = SymphonyExtension.score_status


@pytest.fixture(autouse=True)
def _use_chinese_preferred_language(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.get_config",
        lambda: {"preferred_language": "zh"},
    )

    async def fresh_score(self, params=None, request=None):
        del self, params, request
        return {"success": True, "exists": True, "stale": False}

    monkeypatch.setattr(SymphonyExtension, "score_status", fresh_score)


class _Registry:
    def __init__(self):
        self.handlers = {}

    def register_rpc_handler(self, method, handler):
        self.handlers[method] = handler


def test_extension_registers_rpc_handlers():
    registry = _Registry()

    SymphonyExtension().register(registry)

    assert set(registry.handlers) == {
        SYMPHONY_SCORE_STATUS,
        SYMPHONY_BUILD_SCORE,
        SYMPHONY_PAUSE_BUILD,
        SYMPHONY_GRAPH,
        SYMPHONY_PLAN,
    }


def test_symphony_skill_metadata():
    skill_md = Path(
        "jiuwenswarm/extensions/symphony/skills/symphony-assistant/SKILL.md"
    )
    content = skill_md.read_text(encoding="utf-8")

    assert "name: symphony-assistant" in content
    assert "source of truth" in content
    assert "allowed_tools:" in content
    assert "- symphony_compose_score" in content
    assert "- symphony_read_score" in content
    assert "- symphony_refresh_score" in content
    assert "skill capabilities, skill chaining, skill ordering" in content
    assert "use `search_skill` to discover external skills" in content
    assert "call `symphony_refresh_score`" in content
    assert "currently installed skills" not in content
    assert "instead of inventing them" in content


def test_extension_requires_query():
    result = asyncio.run(SymphonyExtension().plan({"query": ""}))

    assert result["success"] is False
    assert "query is required" in result["detail"]


def test_plan_refreshes_stale_score_in_extension(monkeypatch, tmp_path):
    extension = SymphonyExtension()
    calls = []

    async def stale_status(params=None, request=None):
        del params, request
        calls.append("status")
        return {"success": True, "exists": True, "stale": True}

    async def build_score(params=None, request=None):
        del params, request
        calls.append("build")
        return {"success": True}

    async def plan_from_score(*args, **kwargs):
        del args, kwargs
        calls.append("plan")
        return {"status": "ready", "recommended_plans": []}

    monkeypatch.setattr(extension, "score_status", stale_status)
    monkeypatch.setattr(extension, "build_score", build_score)
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.plan_from_score",
        plan_from_score,
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {"paths": {"score_dir": str(tmp_path / "score")}}
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )

    result = asyncio.run(extension.plan({"query": "compose"}))

    assert calls == ["status", "build", "plan"]
    assert result["score_build"] == {"success": True, "rebuilt": True}


def test_plan_uses_llm_plan_from_score(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    llm_config = object()
    seen = {}
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.get_config",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {"paths": {"score_dir": str(configured_score_dir)}}
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: {"score_dir": str(score_dir)},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: llm_config,
    )

    async def fake_plan_from_score(score_dir, query, received_llm_config, **kwargs):
        seen["score_dir"] = score_dir
        seen["query"] = query
        seen["llm_config"] = received_llm_config
        seen["kwargs"] = kwargs
        return {
            "status": "ready",
            "recommended_plans": [
                {
                    "title": "Plan",
                    "status": "ready",
                    "steps": [{"skill_id": "skill-1", "skill_name": "Skill 1"}],
                }
            ],
            "execution_graph": {"edges": []},
        }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.plan_from_score",
        fake_plan_from_score,
    )

    result = asyncio.run(SymphonyExtension().plan({"query": "do work"}))

    assert result["success"] is True
    assert result["mode"] == "fast"
    assert result["direct_display"] is True
    assert "display_format" not in result
    assert seen["score_dir"] == configured_score_dir.resolve()
    assert seen["query"] == "do work"
    assert seen["llm_config"] is llm_config
    assert "orchestration_config" in seen["kwargs"]
    assert seen["kwargs"]["language"] == "cn"
    assert seen["kwargs"]["dynamic_graph_enabled"] is False


def test_plan_ignores_internal_language_parameter(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    seen = {}
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {"paths": {"score_dir": str(configured_score_dir)}}
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: {"score_dir": str(score_dir)},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )

    async def fake_plan_from_score(*args, **kwargs):
        del args
        seen.update(kwargs)
        return {
            "status": "ready",
            "recommended_plans": [],
            "execution_graph": {"edges": []},
        }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.plan_from_score",
        fake_plan_from_score,
    )

    result = asyncio.run(
        SymphonyExtension().plan({"query": "do work", "language": "en"})
    )

    assert result["language"] == "cn"
    assert result["content"].startswith("## Symphony 编排计划")
    assert seen["language"] == "cn"


def test_plan_resolves_language_from_runtime_config(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    seen = {}
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.get_config",
        lambda: {"preferred_language": "en"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {"paths": {"score_dir": str(configured_score_dir)}}
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: {"score_dir": str(score_dir)},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )

    async def fake_plan_from_score(*args, **kwargs):
        del args
        seen.update(kwargs)
        return {
            "status": "ready",
            "recommended_plans": [],
            "execution_graph": {"edges": []},
        }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.plan_from_score",
        fake_plan_from_score,
    )

    result = asyncio.run(SymphonyExtension().plan({"query": "do work"}))

    assert result["language"] == "en"
    assert seen["language"] == "en"


def test_plan_passes_enabled_dynamic_graph_switch(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    seen = {}
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {"score_dir": str(configured_score_dir)},
                "evolution": {"enabled": True},
            }
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: {"score_dir": str(score_dir)},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )

    async def fake_plan_from_score(score_dir, query, received_llm_config, **kwargs):
        del score_dir, query, received_llm_config
        seen.update(kwargs)
        return {
            "status": "ready",
            "recommended_plans": [],
            "execution_graph": {"edges": []},
        }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.plan_from_score",
        fake_plan_from_score,
    )

    result = asyncio.run(SymphonyExtension().plan({"query": "do work"}))

    assert result["success"] is True
    assert seen["dynamic_graph_enabled"] is True


def test_plan_uses_requested_fast_mode(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    seen = {}
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {"paths": {"score_dir": str(configured_score_dir)}}
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: {"score_dir": str(score_dir)},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )

    async def fake_plan_from_score(score_dir, query, received_llm_config, **kwargs):
        del score_dir, query, received_llm_config
        seen["orchestration_config"] = kwargs["orchestration_config"]
        return {
            "status": "ready",
            "recommended_plans": [],
            "execution_graph": {"edges": []},
        }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.plan_from_score",
        fake_plan_from_score,
    )

    result = asyncio.run(SymphonyExtension().plan({"query": "do work", "mode": "fast"}))

    assert result["success"] is True
    assert result["mode"] == "fast"
    assert seen["orchestration_config"].mode == "fast"


def test_plan_passes_candidate_skill_ids(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    seen = {}
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {"paths": {"score_dir": str(configured_score_dir)}}
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: {"score_dir": str(score_dir)},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_execution_disabled_skills",
        lambda: ["skill-b"],
    )

    async def fake_plan_from_score(score_dir, query, received_llm_config, **kwargs):
        del score_dir, query, received_llm_config
        seen["candidate_skill_ids"] = kwargs["candidate_skill_ids"]
        seen["disabled_skill_names"] = kwargs["disabled_skill_names"]
        return {
            "status": "ready",
            "recommended_plans": [],
            "execution_graph": {"edges": []},
        }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.plan_from_score",
        fake_plan_from_score,
    )

    result = asyncio.run(
        SymphonyExtension().plan(
            {
                "query": "do work",
                "candidate_skill_ids": [" skill-a ", "", "skill-a", "skill-b"],
            }
        )
    )

    assert result["success"] is True
    assert seen["candidate_skill_ids"] == ["skill-a", "skill-b"]
    assert seen["disabled_skill_names"] == ["skill-b"]


def test_graph_filters_disabled_skills_from_visual_payload(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    artifacts = ScoreArtifacts(
        score_dir=configured_score_dir,
        manifest={},
        skills=[
            {"id": "skill-a", "name": "Alpha Skill"},
            {"id": "skill-b", "name": "Beta Skill"},
            {"id": "skill-c", "name": "Gamma Skill"},
        ],
        graph={
            "nodes": [
                {"id": "skill:skill-a", "type": "skill"},
                {"id": "skill:skill-b", "type": "skill"},
                {"id": "skill:skill-c", "type": "skill"},
            ],
            "edges": [
                {"source": "skill:skill-a", "target": "skill:skill-b"},
                {"source": "skill:skill-a", "target": "skill:skill-c"},
                {"source": "skill:skill-b", "target": "skill:skill-c"},
            ],
        },
        lookup={
            "by_output": {
                "draft": ["skill-a"],
                "review": ["skill-b"],
            },
            "neighbors": {
                "skill-a": ["skill-b", "skill-c"],
                "skill-b": ["skill-c"],
            },
            "by_text_term": {
                "alpha": ["skill-a", "skill-b"],
                "beta": ["skill-b"],
            },
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {"score_dir": str(configured_score_dir)},
                "evolution": {"enabled": True},
            }
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: artifacts,
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_execution_disabled_skills",
        lambda: ["Beta Skill"],
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_dynamic_overlay",
        lambda score_dir: {
            "edges": {
                "skill-a->skill-c:can_feed": {"runtime_weight": 1.15},
            }
        },
    )

    result = asyncio.run(SymphonyExtension().graph({}))

    assert result["success"] is True
    assert [skill["id"] for skill in result["skills"]] == ["skill-a", "skill-c"]
    assert [node["id"] for node in result["graph"]["nodes"]] == [
        "skill:skill-a",
        "skill:skill-c",
    ]
    assert result["graph"]["edges"] == [
        {
            "source": "skill:skill-a",
            "target": "skill:skill-c",
            "runtime_weight": 1.15,
        }
    ]
    assert result["score_lookup"] == {
        "by_output": {"draft": ["skill-a"]},
        "neighbors": {"skill-a": ["skill-c"]},
        "by_text_term": {"alpha": ["skill-a"]},
    }


def test_plan_uses_requested_beam_mode(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    seen = {}
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {"paths": {"score_dir": str(configured_score_dir)}}
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: {"score_dir": str(score_dir)},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )

    async def fake_plan_from_score(score_dir, query, received_llm_config, **kwargs):
        del score_dir, query, received_llm_config
        seen["orchestration_config"] = kwargs["orchestration_config"]
        return {
            "status": "ready",
            "recommended_plans": [],
            "execution_graph": {"edges": []},
        }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.plan_from_score",
        fake_plan_from_score,
    )

    result = asyncio.run(SymphonyExtension().plan({"query": "do work", "mode": "beam"}))

    assert result["success"] is True
    assert result["mode"] == "beam"
    assert seen["orchestration_config"].mode == "beam"


def test_plan_streams_beam_progress_events(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    events = []
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {"score_dir": str(configured_score_dir)},
                "orchestration": {"mode": "beam"},
            }
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: {"score_dir": str(score_dir)},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )

    async def fake_plan_from_score(score_dir, query, received_llm_config, **kwargs):
        del score_dir, query, received_llm_config
        await kwargs["progress_callback"](
            {
                "event": "started",
                "language": "en",
                "round_index": 0,
                "graph": {"nodes": [], "edges": []},
            }
        )
        return {
            "status": "ready",
            "recommended_plans": [],
            "execution_graph": {"edges": []},
            "beam_search": {"graph": {"nodes": [], "edges": []}},
        }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.plan_from_score",
        fake_plan_from_score,
    )
    request = SimpleNamespace(
        metadata={
            "symphony_progress_callback": lambda event: events.append(event),
        }
    )

    result = asyncio.run(
        SymphonyExtension().plan({"query": "do work", "mode": "beam"}, request)
    )

    assert result["success"] is True
    assert events == [
        {
            "event": "started",
            "language": "en",
            "round_index": 0,
            "graph": {"nodes": [], "edges": []},
        }
    ]


def test_plan_presentation_uses_recommended_plan(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {"paths": {"score_dir": str(configured_score_dir)}}
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: {"score_dir": str(score_dir)},
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )

    async def fake_plan_from_score(score_dir, query, received_llm_config, **kwargs):
        del score_dir, query, received_llm_config, kwargs
        return {
            "recommended_plans": [
                {
                    "title": "Recommended Plan",
                    "status": "ready",
                    "reason": "Best match.",
                    "steps": [
                        {"skill_id": "skill-1", "name": "Skill 1"},
                        {"skill_id": "skill-2", "name": "Skill 2"},
                    ],
                    "can_feed_edges": [
                        {
                            "source_id": "skill-1",
                            "target_id": "skill-2",
                            "confidence": 0.91,
                        }
                    ],
                    "missing_inputs": [
                        {
                            "skill_id": "imap-smtp-email",
                            "name": "收件邮箱地址",
                            "type": "unknown",
                            "reason": "需要用户提供收件邮箱地址才能发送邮件",
                        }
                    ],
                }
            ],
            "execution_graph": {
                "edges": [
                    {
                        "source": "skill-1",
                        "target": "skill-2",
                        "confidence": 0.91,
                    }
                ]
            },
            "beam_search": {
                "language": "cn",
                "round_index": 1,
                "graph": {
                    "nodes": [
                        {
                            "id": "skill-1",
                            "label": "Skill 1",
                            "status": "final",
                            "seed": True,
                        },
                        {
                            "id": "skill-2",
                            "label": "Skill 2",
                            "status": "final",
                            "seed": False,
                        },
                        {
                            "id": "skill-3",
                            "label": "Skill 3",
                            "status": "rejected",
                            "seed": False,
                        },
                    ],
                    "edges": [
                        {
                            "source": "skill-1",
                            "target": "skill-2",
                            "status": "final",
                        },
                        {
                            "source": "skill-1",
                            "target": "skill-3",
                            "status": "rejected",
                        },
                    ],
                },
            },
        }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.plan_from_score",
        fake_plan_from_score,
    )

    result = asyncio.run(SymphonyExtension().plan({"query": "do work"}))

    assert result["success"] is True
    assert "## Recommended Plan" in result["content"]
    assert "markdown" not in result
    assert result["direct_display"] is True
    assert "display_format" not in result
    assert "Status:" not in result["content"]
    assert result["result"]["recommended_plans"][0]["status"] == "ready"
    assert "Best match." in result["content"]
    assert result["content"].endswith("是否按照上述编排结果执行？")
    assert "## Beam search" not in result["content"]
    assert "classDef rejected" not in result["content"]
    assert "score `0.91`" not in result["content"]
    assert "strong can_feed match" not in result["content"]
    assert "Missing inputs:" not in result["content"]
    assert "收件邮箱地址" not in result["content"]
    assert "imap-smtp-email" not in result["content"]
    assert result["result"]["recommended_plans"][0]["missing_inputs"] == [
        {
            "skill_id": "imap-smtp-email",
            "name": "收件邮箱地址",
            "type": "unknown",
            "reason": "需要用户提供收件邮箱地址才能发送邮件",
        }
    ]
    assert (
        result["result"]["recommended_plans"][0]["can_feed_edges"][0]["confidence"]
        == 0.91
    )
    assert 'N1["Skill 1"]' in result["content"]
    assert "N1 --> N2" in result["content"]


def test_plan_presentation_does_not_ask_to_execute_empty_plan():
    presentation = _build_presentation(
        {"recommended_plans": [{"title": "Empty Plan", "status": "ready", "steps": []}]}
    )

    assert "是否按照上述编排结果执行？" not in presentation["markdown"]


def test_plan_presentation_asks_to_execute_in_english():
    presentation = _build_presentation(
        {
            "recommended_plans": [
                {
                    "title": "Recommended Plan",
                    "status": "ready",
                    "steps": [{"skill_id": "skill-1"}],
                }
            ]
        },
        language="en",
    )

    assert presentation["markdown"].endswith(
        "Would you like to proceed with the orchestration plan above?"
    )


def test_plan_presentation_does_not_ask_to_execute_no_plan():
    presentation = _build_presentation(
        {"recommended_plans": [{"title": "No Plan", "status": "no_plan", "steps": []}]}
    )

    assert "是否按照上述编排结果执行？" not in presentation["markdown"]


def test_build_score_awaits_service_and_records_build_log(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    skills_root = tmp_path / "skills"
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(skills_root),
                    "score_dir": str(configured_score_dir),
                }
            }
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )
    seen = {}

    class _Result:
        @staticmethod
        def to_dict():
            return {
                "success": True,
                "score_dir": str(configured_score_dir.resolve()),
                "skill_count": 1,
                "reused_count": 0,
                "extracted_count": 1,
                "removed_count": 0,
                "edge_count": 0,
                "diagnostics_count": 0,
            }

    async def fake_build_score(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        kwargs["build_log"](
            "fingerprint.extract.start", current=1, total=1, path="skill-1"
        )
        return _Result()

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.service_build_score",
        fake_build_score,
    )

    result = asyncio.run(SymphonyExtension().build_score({}))

    assert result["success"] is True
    assert result["score_dir"] == str(configured_score_dir.resolve())
    assert seen["args"][0] == skills_root.resolve()
    assert seen["args"][1] == configured_score_dir.resolve()
    assert seen["kwargs"]["force"] is False
    assert result["build_log"][-1]["stage"] == "update.done"


def test_build_score_accepts_force_rebuild_param(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    skills_root = tmp_path / "skills"
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(skills_root),
                    "score_dir": str(configured_score_dir),
                }
            }
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )
    seen = {}

    class _Result:
        @staticmethod
        def to_dict():
            return {
                "success": True,
                "score_dir": str(configured_score_dir.resolve()),
                "skill_count": 1,
                "reused_count": 0,
                "extracted_count": 1,
                "removed_count": 0,
                "edge_count": 0,
                "diagnostics_count": 0,
            }

    async def fake_build_score(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.service_build_score",
        fake_build_score,
    )

    result = asyncio.run(SymphonyExtension().build_score({"force": True}))

    assert result["success"] is True
    assert seen["args"][0] == skills_root.resolve()
    assert seen["kwargs"]["force"] is True


def test_pause_build_cancels_active_build(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(tmp_path / "skills"),
                    "score_dir": str(configured_score_dir),
                }
            }
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )

    async def run_case():
        started = asyncio.Event()

        async def fake_build_score(*args, **kwargs):
            del args
            kwargs["build_log"]("graph.resolve.start")
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(
            "jiuwenswarm.extensions.symphony.extension.service_build_score",
            fake_build_score,
        )
        extension = SymphonyExtension()
        build_task = asyncio.create_task(extension.build_score({}))
        await started.wait()
        pause_result = await extension.pause_build({})
        build_result = await build_task
        return pause_result, build_result

    pause_result, build_result = asyncio.run(run_case())

    assert pause_result["success"] is True
    assert pause_result["paused"] is True
    assert pause_result["build_progress"]["status"] == "paused"
    assert pause_result["build_log"][-1]["stage"] == "update.paused"
    assert build_result["success"] is False
    assert build_result["paused"] is True
    assert build_result["build_progress"]["status"] == "paused"
    assert build_result["build_log"][-1]["stage"] == "update.paused"


def test_score_status_repairs_interrupted_build_after_restart(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    skills_root = tmp_path / "skills"
    logger = _BuildProcessLogger(configured_score_dir / "build_log.jsonl")
    logger.reset()
    logger.record("update.start")
    logger.record("graph.resolve.start")
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(skills_root),
                    "score_dir": str(configured_score_dir),
                }
            }
        ),
    )

    class _Status:
        @staticmethod
        def to_dict():
            return {
                "success": True,
                "score_dir": str(configured_score_dir),
                "exists": False,
                "stale": True,
            }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.score_status",
        lambda *args, **kwargs: _Status(),
    )

    async def run_case():
        extension = SymphonyExtension()
        first = await _REAL_SCORE_STATUS(extension, {})
        second = await _REAL_SCORE_STATUS(extension, {})
        return first, second

    first, second = asyncio.run(run_case())

    assert first["build_progress"]["status"] == "paused"
    assert first["build_log"][-1]["stage"] == "update.paused"
    assert first["build_log"][-1]["reason"] == "process_interrupted"
    assert second["build_progress"]["status"] == "paused"
    assert [
        entry["stage"] for entry in second["build_log"]
    ].count("update.paused") == 1


def test_score_status_keeps_active_build_running(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    skills_root = tmp_path / "skills"
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(skills_root),
                    "score_dir": str(configured_score_dir),
                }
            }
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.LLMConfig.from_default_model",
        lambda: object(),
    )

    class _Status:
        @staticmethod
        def to_dict():
            return {
                "success": True,
                "score_dir": str(configured_score_dir),
                "exists": False,
                "stale": True,
            }

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.score_status",
        lambda *args, **kwargs: _Status(),
    )

    async def run_case():
        started = asyncio.Event()

        async def fake_build_score(*args, **kwargs):
            del args
            kwargs["build_log"]("graph.resolve.start")
            started.set()
            await asyncio.sleep(30)

        monkeypatch.setattr(
            "jiuwenswarm.extensions.symphony.extension.service_build_score",
            fake_build_score,
        )
        extension = SymphonyExtension()
        build_task = asyncio.create_task(extension.build_score({}))
        await started.wait()
        status = await _REAL_SCORE_STATUS(extension, {})
        await extension.pause_build({})
        await build_task
        return status

    result = asyncio.run(run_case())

    assert result["build_progress"]["status"] == "running"
    assert all(
        entry.get("reason") != "process_interrupted"
        for entry in result["build_log"]
    )


def test_pause_build_repairs_interrupted_build_without_active_task(
    monkeypatch, tmp_path
):
    configured_score_dir = tmp_path / "configured"
    logger = _BuildProcessLogger(configured_score_dir / "build_log.jsonl")
    logger.reset()
    logger.record("update.start")
    logger.record("graph.resolve.start")
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(tmp_path / "skills"),
                    "score_dir": str(configured_score_dir),
                }
            }
        ),
    )

    async def run_case():
        extension = SymphonyExtension()
        first = await extension.pause_build({})
        second = await extension.pause_build({})
        return first, second

    result, repeated = asyncio.run(run_case())

    assert result["success"] is True
    assert result["paused"] is True
    assert result["build_progress"]["status"] == "paused"
    assert result["build_log"][-1]["stage"] == "update.paused"
    assert result["build_log"][-1]["reason"] == "process_interrupted"
    assert repeated["paused"] is True
    assert repeated["build_progress"]["status"] == "paused"
    assert [
        entry["stage"] for entry in repeated["build_log"]
    ].count("update.paused") == 1


def test_graph_repairs_interrupted_build_without_status_request(
    monkeypatch, tmp_path
):
    configured_score_dir = tmp_path / "configured"
    logger = _BuildProcessLogger(configured_score_dir / "build_log.jsonl")
    logger.reset()
    logger.record("update.start")
    logger.record("graph.resolve.start")
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(tmp_path / "skills"),
                    "score_dir": str(configured_score_dir),
                }
            }
        ),
    )

    result = asyncio.run(SymphonyExtension().graph({}))

    assert result["success"] is False
    assert result["build_progress"]["status"] == "paused"
    assert result["build_log"][-1]["stage"] == "update.paused"
    assert result["build_log"][-1]["reason"] == "process_interrupted"


def test_graph_returns_business_error_when_artifacts_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(tmp_path / "skills"),
                    "score_dir": str(tmp_path),
                }
            }
        ),
    )

    result = asyncio.run(SymphonyExtension().graph({"score_dir": str(tmp_path)}))

    assert result["success"] is False
    assert result["score_dir"] == str(tmp_path.resolve())
    assert "技能总谱不存在" in result["detail"]
    assert "Missing score artifact" in result["error"]
    assert result["build_progress"]["status"] == "idle"


def test_graph_ignores_request_score_dir_param(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    legacy_score_dir = tmp_path / "legacy"
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(tmp_path / "skills"),
                    "score_dir": str(configured_score_dir),
                }
            }
        ),
    )

    result = asyncio.run(
        SymphonyExtension().graph({"score_dir": str(legacy_score_dir)})
    )

    assert result["success"] is False
    assert result["score_dir"] == str(configured_score_dir.resolve())


def test_graph_includes_orchestration_min_edge_confidence(monkeypatch, tmp_path):
    configured_score_dir = tmp_path / "configured"
    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_symphony_config",
        lambda: symphony_config_from_dict(
            {
                "paths": {
                    "skills_root": str(tmp_path / "skills"),
                    "score_dir": str(configured_score_dir),
                },
                "orchestration": {"min_edge_confidence": 0.33},
            }
        ),
    )

    class _Artifacts:
        score_dir = configured_score_dir.resolve()
        manifest = {"created_at": "2026-06-14T00:00:00+00:00"}
        skills = {"skills": []}
        graph = {"nodes": [], "edges": []}
        lookup = {}

    monkeypatch.setattr(
        "jiuwenswarm.extensions.symphony.extension.load_score_artifacts",
        lambda score_dir: _Artifacts(),
    )

    result = asyncio.run(SymphonyExtension().graph({}))

    assert result["success"] is True
    assert result["orchestration_min_edge_confidence"] == 0.33


def test_build_log_payload_reports_running_progress(tmp_path):
    logger = _BuildProcessLogger(tmp_path / "build_log.jsonl")
    logger.reset()
    logger.record("scan.start", skills_root=str(tmp_path / "skills"))
    logger.record("fingerprint.extract.start", current=2, total=4, path="demo")

    result = _build_log_payload(tmp_path)

    assert len(result["build_log"]) == 2
    assert result["build_log"][-1]["label"] == "提取技能指纹"
    assert result["build_progress"]["status"] == "running"
    assert result["build_progress"]["percent"] == 36


def test_build_log_payload_clamps_progress_count_to_total(tmp_path):
    logger = _BuildProcessLogger(tmp_path / "build_log.jsonl")
    logger.reset()
    logger.record("fingerprint.extract.start", current=19, total=14, path="demo")

    result = _build_log_payload(tmp_path)

    assert result["build_log"][-1]["current"] == 14
    assert result["build_log"][-1]["total"] == 14
    assert result["build_progress"]["current"] == 14
    assert result["build_progress"]["total"] == 14
    assert result["build_progress"]["percent"] == 48


def test_build_log_payload_terminal_stage_wins_over_late_progress(tmp_path):
    logger = _BuildProcessLogger(tmp_path / "build_log.jsonl")
    logger.reset()
    logger.record("fingerprint.extract.start", current=14, total=15, path="demo")
    logger.record("update.failed", error="bad model")
    logger.record("fingerprint.extract.start", current=15, total=15, path="late-demo")

    result = _build_log_payload(tmp_path)

    assert result["build_progress"]["status"] == "error"
    assert result["build_progress"]["stage"] == "update.failed"
    assert result["build_progress"]["label"] == "总谱构建失败"
    assert result["build_progress"]["percent"] == 100


def test_latest_effective_build_log_entry_handles_empty_entries():
    assert _latest_effective_build_log_entry([]) == {}


def test_build_log_payload_includes_manifest_token_usage(tmp_path):
    usage = {
        "total": {
            "request_count": 2,
            "prompt_tokens": 106436,
            "completion_tokens": 496,
            "total_tokens": 106932,
        },
        "by_stage": {},
        "by_operation": {},
        "records": [],
    }
    (tmp_path / "score_manifest.json").write_text(
        json.dumps({"llm": {"token_usage": usage}}),
        encoding="utf-8",
    )

    result = _build_log_payload(tmp_path)

    assert result["llm_token_usage"] == usage
    assert result["build_progress"]["llm_token_usage"] == usage


def test_running_build_log_payload_does_not_reuse_persisted_token_usage(tmp_path):
    logger = _BuildProcessLogger(tmp_path / "build_log.jsonl")
    logger.reset()
    logger.record("fingerprint.extract.start", current=1, total=3, path="demo")
    stale_usage = {
        "total": {
            "request_count": 2,
            "prompt_tokens": 106436,
            "completion_tokens": 496,
            "total_tokens": 106932,
        },
    }
    (tmp_path / "score_manifest.json").write_text(
        json.dumps({"llm": {"token_usage": stale_usage}}),
        encoding="utf-8",
    )

    result = _build_log_payload(tmp_path)

    assert result["build_progress"]["status"] == "running"
    assert result["llm_token_usage"] == {}
    assert "llm_token_usage" not in result["build_progress"]


def test_agent_adapter_keeps_symphony_business_error_as_successful_rpc():
    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    ExtensionRegistry.reset_instance()
    registry = ExtensionRegistry.create_instance(None, {}, None)
    registry.register_rpc_handler(
        SYMPHONY_GRAPH,
        lambda _params, request=None: {"success": False, "detail": "总谱不存在"},
    )
    request = AgentRequest(
        request_id="request-1",
        channel_id="web",
        req_method=ReqMethod.SYMPHONY_GRAPH,
        params={},
    )

    try:
        adapter = JiuWenSwarm.__new__(JiuWenSwarm)
        response = asyncio.run(getattr(adapter, "_handle_symphony_request")(request))
    finally:
        ExtensionRegistry.reset_instance()

    assert response is not None
    assert response.ok is True
    assert response.payload == {"success": False, "detail": "总谱不存在"}
