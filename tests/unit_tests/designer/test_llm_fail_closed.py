# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer LLM fail-closed helpers: no silent heuristic when chat model is missing."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.designer import model_tools
from jiuwenswarm.server.runtime.designer.model_tools import (
    DesignerLlmError,
    LLM_BILLING,
    LLM_NOT_CONFIGURED,
    classify_llm_failure,
    model_text_or_raise,
    require_llm,
)

_require_media_models = model_tools.require_media_models


def test_classify_llm_failure_codes() -> None:
    code, message = classify_llm_failure("Error code: 402 Insufficient Balance")
    assert code == LLM_BILLING
    assert "billing" in message.lower() or "credit" in message.lower()

    code, message = classify_llm_failure("No models configured in Settings")
    assert code == LLM_NOT_CONFIGURED
    assert "not configured" in message.lower() or "No models" in message

    code, message = classify_llm_failure("connection reset")
    assert code == model_tools.LLM_API_ERROR
    assert "failed" in message.lower()


def test_require_llm_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_tools, "_chat_billing_block", "")
    monkeypatch.setattr(model_tools, "llm_available", lambda: False)
    with pytest.raises(DesignerLlmError) as excinfo:
        require_llm()
    assert excinfo.value.code == LLM_NOT_CONFIGURED


def test_require_llm_billing_block() -> None:
    model_tools._chat_billing_block = ""
    try:
        model_tools.note_chat_model_unavailable("Error code: 402 Insufficient Balance")
        with pytest.raises(DesignerLlmError) as excinfo:
            require_llm()
        assert excinfo.value.code == LLM_BILLING
    finally:
        model_tools._chat_billing_block = ""


def test_model_text_or_raise_rejects_unavailable() -> None:
    with pytest.raises(DesignerLlmError) as excinfo:
        model_text_or_raise(
            {
                "ok": False,
                "unavailable": True,
                "code": LLM_BILLING,
                "error": "402 Insufficient Balance",
                "text": "",
            }
        )
    assert excinfo.value.code == LLM_BILLING


@pytest.mark.asyncio
async def test_call_model_tool_no_credentials_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_tools, "_chat_billing_block", "")
    monkeypatch.setattr(
        model_tools,
        "list_configured_models",
        lambda: [
            {
                "id": "demo",
                "model_name": "demo",
                "api_base": "https://example.com/v1",
                "index": 0,
            }
        ],
    )
    monkeypatch.setattr(model_tools, "pick_model_for_optimize", lambda _opt: {
        "id": "demo",
        "model_name": "demo",
        "api_base": "https://example.com/v1",
        "index": 0,
    })
    monkeypatch.setattr(model_tools, "get_config", lambda: {"models": {"defaults": []}})
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)

    result = await model_tools.call_model_tool(
        prompt="hello",
        system="reply",
        optimize_for="cost",
    )
    assert result["ok"] is False
    assert result.get("fallback") is False
    assert result["code"] == LLM_NOT_CONFIGURED
    assert not str(result.get("text") or "").startswith("[local-tool-fallback]")


def test_stamp_run_failure_uses_designer_llm_user_message() -> None:
    from jiuwenswarm.common.schema.designer_graph import RUN_STATUS_FAILED
    from jiuwenswarm.server.runtime.designer.executor import _stamp_run_failure

    run: dict = {"status": "running", "node_states": {}}
    exc = DesignerLlmError(
        "Chat model request failed: Authentication Fails, Your api key is invalid",
        code=model_tools.LLM_API_ERROR,
    )
    _stamp_run_failure(run, exc)
    assert run["status"] == RUN_STATUS_FAILED
    assert "Authentication Fails" in str(run["error"])
    assert "Chat model request failed" in str(run["error"])


def test_normalize_execution_run_preserves_error() -> None:
    from jiuwenswarm.common.schema.designer_graph import normalize_execution_run

    run = normalize_execution_run(
        {
            "schema_version": "designer-execution-run.v1",
            "run_id": "run_deadbeef",
            "graph_id": "graph_deadbeef",
            "project_id": "proj_1",
            "status": "failed",
            "node_states": {},
            "current_node_ids": [],
            "error": "Chat model request failed: invalid api key",
            "warning": "soft notice",
            "warnings": ["soft notice"],
        }
    )
    assert run["error"] == "Chat model request failed: invalid api key"
    assert run["warning"] == "soft notice"
    assert run["warnings"] == ["soft notice"]


class _ToolkitSpawner:
    async def spawn_node_agent(self, run_id: str, node_id: str) -> str:
        return f"{run_id}:{node_id}"

    def apply_agent_graph_patch(self, graph_id: str, patch: dict) -> dict:
        return {"graph_id": graph_id, **patch}

    def load_graph_snapshot(self, graph_id: str, run_id: str) -> dict:
        return {"graph_id": graph_id, "run_id": run_id}


@pytest.mark.asyncio
async def test_node_agent_call_model_raises_on_tool_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import DesignerGraphToolkit

    async def fail_model_call(**_kwargs: object) -> dict:
        return {"ok": False, "error": "upstream unavailable", "text": ""}

    monkeypatch.setattr(model_tools, "call_model_tool", fail_model_call)
    graph = {
        "graph_id": "graph_fail_closed",
        "description": "Make a short film.",
        "nodes": [{"id": "n_brief", "type": "text", "config": {"role": "brief"}}],
        "metadata": {},
    }
    toolkit = DesignerGraphToolkit(
        _ToolkitSpawner(),
        NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_brief"),
    )

    with pytest.raises(DesignerLlmError):
        await toolkit.call_model(prompt="Write the brief.")


def test_smart_graph_rejects_missing_llm_analysis_sections() -> None:
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

    complete = {
        "source": "llm",
        "characters": [{"id": "char_1", "name": "Lead"}],
        "scenes": [{"id": "set_1", "name": "Room"}],
        "shots": [
            {
                "shot_index": 1,
                "setting_id": "set_1",
                "action": "Lead opens the door.",
                "character_ids": ["char_1"],
            }
        ],
    }
    for missing in ("characters", "scenes", "shots"):
        analysis = {key: value for key, value in complete.items() if key != missing}
        with pytest.raises(DesignerLlmError, match=missing):
            build_smart_video_graph(
                project_id=f"project_missing_{missing}",
                prompt="Lead opens the door.",
                analysis=analysis,
            )


@pytest.mark.asyncio
async def test_director_graph_design_rejects_empty_model_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer import orchestration

    async def empty_model_call(**_kwargs: object) -> dict:
        return {"ok": True, "text": ""}

    monkeypatch.setattr(orchestration, "call_model_tool", empty_model_call)
    graph = {
        "description": "Lead opens the door.",
        "nodes": [],
        "metadata": {
            "script_analysis": {
                "source": "llm",
                "characters": [{"id": "char_1", "name": "Lead"}],
                "scenes": [{"id": "set_1", "name": "Room"}],
                "shots": [{"shot_index": 1, "setting_id": "set_1", "action": "Open door"}],
            }
        },
    }

    with pytest.raises(DesignerLlmError):
        await orchestration.Director().design_execution_graph(graph)


@pytest.mark.asyncio
async def test_director_reviews_reject_nonthrowing_model_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer import orchestration

    async def fail_model_call(**_kwargs: object) -> dict:
        return {"ok": False, "error": "authentication failed", "text": ""}

    monkeypatch.setattr(orchestration, "call_model_tool", fail_model_call)
    director = orchestration.Director()
    brief_graph = {
        "description": "Lead opens the door.",
        "nodes": [],
        "metadata": {
            "approved_brief": "# Brief\n\nLead opens the door.",
            "script_analysis": {"characters": [{"id": "char_1", "name": "Lead"}]},
        },
    }
    with pytest.raises(DesignerLlmError):
        await director.review_brief(brief_graph)

    storyboard_graph = {
        "description": "Lead opens the door.",
        "nodes": [],
        "metadata": {
            "script_analysis": {
                "shots": [{"shot_index": 1, "action": "Lead opens the door."}]
            }
        },
    }
    with pytest.raises(DesignerLlmError):
        await director.review_storyboard_once(storyboard_graph, node_states=None)


_SLOT_FIELDS = ("ENABLED", "API_KEY", "API_BASE", "MODEL_NAME", "PROTOCOL")


def _set_slot(monkeypatch: pytest.MonkeyPatch, prefix: str, **values: str) -> None:
    for field in _SLOT_FIELDS:
        monkeypatch.delenv(f"{prefix}_{field}", raising=False)
    for field, value in values.items():
        monkeypatch.setenv(f"{prefix}_{field.upper()}", value)


def _minimax_slot(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    _set_slot(
        monkeypatch,
        prefix,
        enabled="true",
        api_key="sk-test",
        api_base="https://api.minimax.io/v1",
        model_name="MiniMax-H3",
        protocol="minimax",
    )


def test_require_media_models_blocks_incomplete_image(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_slot(monkeypatch, "VISUAL_GEN", enabled="true")
    with pytest.raises(DesignerLlmError) as excinfo:
        _require_media_models(image=True)
    assert excinfo.value.code == model_tools.MEDIA_NOT_CONFIGURED
    assert "Image generation is not configured" in str(excinfo.value)


def test_require_media_models_blocks_switched_off_image(monkeypatch: pytest.MonkeyPatch) -> None:
    _minimax_slot(monkeypatch, "VISUAL_GEN")
    monkeypatch.setenv("VISUAL_GEN_ENABLED", "false")
    with pytest.raises(DesignerLlmError) as excinfo:
        _require_media_models(image=True)
    assert "Image generation is switched off" in str(excinfo.value)


def test_require_media_models_noop_when_nothing_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_slot(monkeypatch, "VISUAL_GEN")
    _set_slot(monkeypatch, "VIDEO_GEN")
    _require_media_models()


def test_require_media_models_blocks_incomplete_video_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _minimax_slot(monkeypatch, "VISUAL_GEN")
    _set_slot(monkeypatch, "VIDEO_GEN", enabled="true")
    with pytest.raises(DesignerLlmError) as excinfo:
        _require_media_models(image=True, video=True)
    message = str(excinfo.value)
    assert "Video generation" in message
    assert "Image generation" not in message


def test_require_media_models_allows_openrouter_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_slot(
        monkeypatch,
        "VIDEO_GEN",
        enabled="true",
        api_key="sk-or",
        api_base="https://openrouter.ai/api/v1",
        model_name="google/veo-3",
        protocol="openrouter",
    )
    # OpenRouter-style endpoints are a supported Design backend, so this must
    # not raise the way the native-only gate used to.
    _require_media_models(video=True)


def test_require_media_models_allows_vllm_omni_without_key_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_slot(
        monkeypatch,
        "VISUAL_GEN",
        enabled="true",
        api_base="http://127.0.0.1:8000/v1",
        protocol="vllm-omni",
    )
    _require_media_models(image=True)
