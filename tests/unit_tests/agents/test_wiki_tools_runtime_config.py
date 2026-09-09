from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from openjiuwen.core.kv_cache import resolve_session_lineage
from openjiuwen.core.session.agent import Session

from jiuwenswarm.agents.harness.common.tools import wiki_tools


def _ascend_model():
    return SimpleNamespace(
        model_client_config=SimpleNamespace(client_provider="AscendAffinity")
    )


def _openai_affinity_model():
    return SimpleNamespace(
        model_client_config=SimpleNamespace(
            client_provider="OpenAI",
            extensions=SimpleNamespace(
                kv_cache=SimpleNamespace(mode="affinity"),
            ),
        )
    )


def test_create_llm_wiki_propagates_runtime_policy_and_parent_lineage(
    monkeypatch,
):
    captured = {}

    def fake_llm_wiki(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        wiki_tools,
        "get_config",
        lambda: {
            "kv_cache_affinity_config": {
                "enable_kv_cache_affinity": True,
            },
            "react": {
                "enable_read_image_multimodal": False,
            }
        },
    )
    monkeypatch.setattr(
        wiki_tools,
        "get_current_session",
        lambda: Session(session_id="product-session"),
    )
    monkeypatch.setattr(wiki_tools, "LLMWiki", fake_llm_wiki)

    result = wiki_tools._create_llm_wiki(
        workspace="workspace",
        model=_ascend_model(),
        sys_operation=None,
    )

    assert result is not None
    assert captured["enable_read_image_multimodal"] is False
    workspace_scope = hashlib.sha256(
        str(Path("workspace").resolve()).encode()
    ).hexdigest()[:12]
    assert captured["session_id"] == (
        f"product-session:subagent:wiki:{workspace_scope}"
    )
    assert captured["parent_session_id"] == "product-session"
    kv_config = captured["kv_cache_affinity_config"]
    assert kv_config.enable_kv_cache_affinity is True


def test_llm_wiki_session_exposes_provider_cache_lineage(monkeypatch, tmp_path):
    monkeypatch.setattr(
        wiki_tools,
        "create_deep_agent",
        lambda **kwargs: SimpleNamespace(),
    )

    wiki = wiki_tools.LLMWiki(
        workspace=tmp_path,
        model=_ascend_model(),
        session_id="product-session:subagent:wiki",
        parent_session_id="product-session",
    )

    assert resolve_session_lineage(wiki._session) == (
        "product-session:subagent:wiki",
        "product-session",
    )


def test_create_llm_wiki_accepts_openai_affinity_capability(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        wiki_tools,
        "get_config",
        lambda: {
            "kv_cache_affinity_config": {
                "enable_kv_cache_affinity": True,
            },
        },
    )
    monkeypatch.setattr(wiki_tools, "get_current_session", lambda: None)
    monkeypatch.setattr(
        wiki_tools,
        "LLMWiki",
        lambda **kwargs: captured.update(kwargs) or SimpleNamespace(),
    )

    wiki_tools._create_llm_wiki(
        workspace="workspace",
        model=_openai_affinity_model(),
        sys_operation=None,
    )

    assert (
        captured["kv_cache_affinity_config"].enable_kv_cache_affinity
        is True
    )
