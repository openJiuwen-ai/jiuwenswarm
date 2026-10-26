# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.agents.harness.common.tools.web_search import (
    JiuwenHarnessWebSearchTool,
    ProviderRun,
    WebSearchRecord,
    build_web_search_tool_card,
    ensure_web_search_harness_metadata,
    evaluate_search_quality,
    format_web_search_response,
    normalize_search_mode,
    run_web_search,
)


def _rec(title: str, url: str, snippet: str = "", source: str = "test") -> WebSearchRecord:
    return WebSearchRecord(title=title, url=url, snippet=snippet, source=source)


def test_evaluate_search_quality_passes_with_two_snippets():
    records = [
        _rec("A", "https://a.com", "x" * 40),
        _rec("B", "https://b.com", "y" * 40),
    ]
    passed, reason = evaluate_search_quality(records, max_results=8)
    assert passed is True
    assert reason == "ok"


def test_evaluate_search_quality_paid_answer_with_one_citation():
    records = [_rec("Ref", "https://ref.com", "")]
    answer = "x" * 120
    passed, reason = evaluate_search_quality(
        records,
        answer=answer,
        max_results=8,
        skip_snippet_check=True,
    )
    assert passed is True
    assert reason == "answer_with_citations"


def test_evaluate_search_quality_paid_long_answer_without_skip_snippet():
    records = [_rec("Ref", "https://ref.com", "")]
    answer = "x" * 120
    passed, reason = evaluate_search_quality(records, answer=answer, max_results=8)
    assert passed is True
    assert reason == "answer_with_citations"


def test_normalize_search_mode_aliases():
    assert normalize_search_mode("") == ("default", None)
    assert normalize_search_mode("paid") == ("paid", None)
    assert normalize_search_mode("mcp_petal_search") == ("paid", None)
    assert normalize_search_mode("free_search") == ("free", None)
    assert normalize_search_mode("paid:bocha") == ("paid", "bocha")
    assert normalize_search_mode("paid:petal") == ("paid", "petal")


def test_format_web_search_response_header():
    text = format_web_search_response(
        "test query",
        search_mode="default",
        selected_provider="paid:petal",
        quality_passed=True,
        primary_records=[_rec("Main", "https://main.com", "snippet", "paid:petal")],
        supplementary_records=[],
        primary_answer="",
        providers_tried=["paid:petal(pass)"],
    )
    assert "search_mode=default" in text
    assert "selected=paid:petal" in text
    assert text.index("search_mode=") < text.index("Query:")


def test_build_web_search_tool_card():
    ensure_web_search_harness_metadata()
    card = build_web_search_tool_card(agent_id="main_agent", language="cn")
    assert card.name == "web_search"
    assert isinstance(card.input_params, dict)


def test_jiuwen_harness_web_search_tool_init():
    tool = JiuwenHarnessWebSearchTool(language="cn", agent_id="research_agent")
    assert tool.card.name == "web_search"


def test_run_web_search_paid_unavailable(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.providers.any_paid_provider_available",
        lambda _order: False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.orchestrator._free_search_engines",
        lambda: [("duckduckgo", object)],
    )

    import asyncio

    result = asyncio.run(run_web_search("test", search_mode="paid"))
    assert result.startswith("[ERROR]: paid search unavailable")
    assert "Use search_mode=free instead" in result
    assert "petal" not in result
    assert "tavily" not in result
    assert "bocha" not in result
    assert "availability" not in result
    assert "search_source" not in result


def test_run_web_search_paid_unavailable_no_free(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.providers.any_paid_provider_available",
        lambda _order: False,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.orchestrator._free_search_engines",
        lambda: [],
    )

    import asyncio

    result = asyncio.run(run_web_search("test", search_mode="paid"))
    assert result.startswith("[ERROR]: paid search unavailable")
    assert "No search source is available" in result
    assert "Use search_mode=free" not in result
    assert "petal" not in result
    assert "search_source" not in result


def test_run_web_search_free_does_not_call_paid(monkeypatch):
    paid_called = False

    async def fake_paid(*args, **kwargs):
        nonlocal paid_called
        paid_called = True
        return None, []

    async def fake_free(query, settings):
        return ProviderRun(
            provider="free:bing",
            records=[_rec("Hit", "https://hit.com", "x" * 40, "free:bing")],
            quality_passed=True,
        )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.orchestrator.run_paid_chain",
        fake_paid,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.orchestrator.run_free_chain",
        fake_free,
    )

    import asyncio

    result = asyncio.run(run_web_search("test", search_mode="free"))
    assert paid_called is False
    assert "search_mode=free" in result
    assert "https://hit.com" in result


def test_run_web_search_default_paid_then_free(monkeypatch):
    async def fake_paid(query, settings, preferred_provider=None):
        return None, []

    async def fake_free(query, settings):
        return ProviderRun(
            provider="free:bing",
            records=[_rec("Hit", "https://hit.com", "x" * 40, "free:bing")],
            quality_passed=True,
        )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.orchestrator.run_paid_chain",
        fake_paid,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.orchestrator.run_free_chain",
        fake_free,
    )

    import asyncio

    result = asyncio.run(run_web_search("test", search_mode="default"))
    assert "search_mode=default" in result
    assert "selected=free:bing" in result


def test_web_search_entry_delegates_to_orchestrator(monkeypatch):
    import asyncio

    calls = 0

    async def fake_run(query, *, search_mode="default", search_source=None, max_results=None, cache=None):
        nonlocal calls
        calls += 1
        assert query == "hello"
        assert search_mode == "default"
        assert max_results is None
        return "ok"

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.tool.run_web_search",
        fake_run,
    )

    from jiuwenswarm.agents.harness.common.tools.web_search.tool import web_search

    result = asyncio.run(web_search.invoke({"query": "hello"}))
    assert result == "ok"
    assert calls == 1


def test_web_search_invalid_search_mode_falls_back_to_default(monkeypatch):
    import asyncio

    calls: list[str] = []

    async def fake_run(query, *, search_mode="default", search_source=None, max_results=None, cache=None):
        calls.append(search_mode)
        return "ok"

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.tool.run_web_search",
        fake_run,
    )

    from jiuwenswarm.agents.harness.common.tools.web_search.tool import web_search

    result = asyncio.run(
        web_search.invoke({"query": "hello", "search_mode": "fast"})
    )
    assert result == "ok"
    assert calls == ["default"]


def test_web_search_env_reads_tip_not_bare_keys(monkeypatch):
    """Track B: free/paid availability must follow tip/ns, not bare os.environ."""
    import os

    from jiuwenswarm.agents.harness.common.tools.web_search.free import _env_flag, _free_search_engines
    from jiuwenswarm.agents.harness.common.tools.web_search.log_util import paid_provider_skip_reason
    from jiuwenswarm.agents.harness.common.tools.web_search.providers import paid_provider_available
    from jiuwenswarm.common.local_env_config import (
        apply_env_overrides_to_active,
        bind_agent_env_ns,
        reset_agent_env_ns,
        reset_local_env_state_for_tests,
    )

    saved = dict(os.environ)
    reset_local_env_state_for_tests()
    try:
        os.environ["FREE_SEARCH_DDG_ENABLED"] = "1"
        os.environ["SERPER_API_KEY"] = "bare-serper"
        apply_env_overrides_to_active(
            {
                "FREE_SEARCH_DDG_ENABLED": "0",
                "FREE_SEARCH_BING_ENABLED": "0",
            },
            service_id="default",
            agent_id="office",
        )
        token = bind_agent_env_ns("default", "office")
        try:
            assert _env_flag("FREE_SEARCH_DDG_ENABLED") is False
            assert _free_search_engines() == []
            assert paid_provider_available("serper") is False
            assert paid_provider_skip_reason("serper") == "missing_SERPER_API_KEY"
        finally:
            reset_agent_env_ns(token)

        apply_env_overrides_to_active(
            {"SERPER_API_KEY": "office-serper"},
            service_id="default",
            agent_id="office",
        )
        token = bind_agent_env_ns("default", "office")
        try:
            assert paid_provider_available("serper") is True
            assert paid_provider_skip_reason("serper") == "available"
        finally:
            reset_agent_env_ns(token)
    finally:
        reset_local_env_state_for_tests()
        os.environ.clear()
        os.environ.update(saved)


def test_resolve_petal_search_url_reads_dedicated_env_var(monkeypatch):
    import os
    from jiuwenswarm.common.local_env_config import (
        apply_env_overrides_to_active,
        bind_agent_env_ns,
        reset_agent_env_ns,
        reset_local_env_state_for_tests,
    )

    saved = dict(os.environ)
    reset_local_env_state_for_tests()
    try:
        apply_env_overrides_to_active(
            {"PETAL_SEARCH_URL": "https://petal.example.com/web-search"},
            service_id="default",
            agent_id="test",
        )
        token = bind_agent_env_ns("default", "test")
        try:
            from jiuwenswarm.agents.harness.common.tools.web_search.paid import _resolve_petal_search_url

            assert _resolve_petal_search_url() == "https://petal.example.com/web-search"
        finally:
            reset_agent_env_ns(token)
    finally:
        reset_local_env_state_for_tests()
        os.environ.clear()
        os.environ.update(saved)


def test_resolve_petal_search_url_raises_when_not_set(monkeypatch):
    import os
    from jiuwenswarm.common.local_env_config import reset_local_env_state_for_tests

    saved = dict(os.environ)
    reset_local_env_state_for_tests()
    try:
        os.environ.pop("PETAL_SEARCH_URL", None)
        os.environ.pop("API_BASE", None)
        os.environ.pop("OPENAI_BASE_URL", None)
        os.environ.pop("OPENAI_API_BASE", None)

        from jiuwenswarm.agents.harness.common.tools.web_search.paid import _resolve_petal_search_url

        import pytest

        with pytest.raises(ValueError, match="PETAL_SEARCH_URL is not set"):
            _resolve_petal_search_url()
    finally:
        reset_local_env_state_for_tests()
        os.environ.clear()
        os.environ.update(saved)


def test_resolve_petal_search_url_does_not_fallback_to_api_base(monkeypatch):
    import os
    from jiuwenswarm.common.local_env_config import (
        apply_env_overrides_to_active,
        bind_agent_env_ns,
        reset_agent_env_ns,
        reset_local_env_state_for_tests,
    )

    saved = dict(os.environ)
    reset_local_env_state_for_tests()
    try:
        os.environ.pop("PETAL_SEARCH_URL", None)
        apply_env_overrides_to_active(
            {"API_BASE": "https://llm-api.com/v2"},
            service_id="default",
            agent_id="test",
        )
        token = bind_agent_env_ns("default", "test")
        try:
            from jiuwenswarm.agents.harness.common.tools.web_search.paid import _resolve_petal_search_url

            import pytest

            with pytest.raises(ValueError, match="PETAL_SEARCH_URL is not set"):
                _resolve_petal_search_url()
        finally:
            reset_agent_env_ns(token)
    finally:
        reset_local_env_state_for_tests()
        os.environ.clear()
        os.environ.update(saved)


def test_load_llm_default_headers_reads_dedicated_env_var(monkeypatch):
    import os
    from jiuwenswarm.common.local_env_config import (
        apply_env_overrides_to_active,
        bind_agent_env_ns,
        reset_agent_env_ns,
        reset_local_env_state_for_tests,
    )

    saved = dict(os.environ)
    reset_local_env_state_for_tests()
    try:
        headers_json = '{"Authorization": "Bearer test-token"}'
        apply_env_overrides_to_active(
            {"PETAL_SEARCH_HEADERS": headers_json},
            service_id="default",
            agent_id="test",
        )
        token = bind_agent_env_ns("default", "test")
        try:
            from jiuwenswarm.agents.harness.common.tools.web_search.paid import _load_llm_default_headers

            result = _load_llm_default_headers()
            assert result == {"Authorization": "Bearer test-token"}
        finally:
            reset_agent_env_ns(token)
    finally:
        reset_local_env_state_for_tests()
        os.environ.clear()
        os.environ.update(saved)


def test_load_llm_default_headers_raises_when_not_set(monkeypatch):
    import os
    from jiuwenswarm.common.local_env_config import reset_local_env_state_for_tests

    saved = dict(os.environ)
    reset_local_env_state_for_tests()
    try:
        os.environ.pop("PETAL_SEARCH_HEADERS", None)
        os.environ.pop("default_headers", None)

        from jiuwenswarm.agents.harness.common.tools.web_search.paid import _load_llm_default_headers

        import pytest

        with pytest.raises(ValueError, match="PETAL_SEARCH_HEADERS is not set"):
            _load_llm_default_headers()
    finally:
        reset_local_env_state_for_tests()
        os.environ.clear()
        os.environ.update(saved)


def test_configured_providers_summary_returns_configured_order(monkeypatch):
    def fake_load_settings():
        from jiuwenswarm.agents.harness.common.tools.web_search.types import WebSearchSettings

        return WebSearchSettings(
            timeout_seconds=30,
            max_results=8,
            paid_provider_order=("petal", "bocha"),
        )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.settings.load_web_search_settings",
        fake_load_settings,
    )

    from jiuwenswarm.agents.harness.common.tools.web_search.tool import _configured_providers_summary

    assert _configured_providers_summary() == "petal, bocha"


def test_env_flag_defaults_to_false(monkeypatch):
    monkeypatch.delenv("FREE_SEARCH_DDG_ENABLED", raising=False)

    from jiuwenswarm.agents.harness.common.tools.web_search.free import _env_flag

    assert _env_flag("FREE_SEARCH_DDG_ENABLED") is False


def test_env_flag_can_be_enabled(monkeypatch):
    import os
    from jiuwenswarm.common.local_env_config import (
        apply_env_overrides_to_active,
        bind_agent_env_ns,
        reset_agent_env_ns,
        reset_local_env_state_for_tests,
    )

    saved = dict(os.environ)
    reset_local_env_state_for_tests()
    try:
        apply_env_overrides_to_active(
            {"FREE_SEARCH_DDG_ENABLED": "true"},
            service_id="default",
            agent_id="test",
        )
        token = bind_agent_env_ns("default", "test")
        try:
            from jiuwenswarm.agents.harness.common.tools.web_search.free import _env_flag

            assert _env_flag("FREE_SEARCH_DDG_ENABLED") is True
        finally:
            reset_agent_env_ns(token)
    finally:
        reset_local_env_state_for_tests()
        os.environ.clear()
        os.environ.update(saved)


def test_free_search_engines_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FREE_SEARCH_DDG_ENABLED", raising=False)
    monkeypatch.delenv("FREE_SEARCH_BING_ENABLED", raising=False)

    from jiuwenswarm.agents.harness.common.tools.web_search.free import _free_search_engines

    assert _free_search_engines() == []


def test_build_web_search_description_contains_configured_providers(monkeypatch):
    def fake_load_settings():
        from jiuwenswarm.agents.harness.common.tools.web_search.types import WebSearchSettings

        return WebSearchSettings(
            timeout_seconds=30,
            max_results=8,
            paid_provider_order=("petal", "bocha"),
        )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.web_search.settings.load_web_search_settings",
        fake_load_settings,
    )

    from jiuwenswarm.agents.harness.common.tools.web_search.tool import _build_web_search_description

    desc = _build_web_search_description()
    for provider in ("petal", "bocha", "tavily", "serper", "jina", "perplexity"):
        assert provider not in desc
    assert "search_source" not in desc


def test_build_web_search_description_hides_free_mode_when_no_free_engines(monkeypatch):
    monkeypatch.delenv("FREE_SEARCH_DDG_ENABLED", raising=False)
    monkeypatch.delenv("FREE_SEARCH_BING_ENABLED", raising=False)

    from jiuwenswarm.agents.harness.common.tools.web_search.tool import _build_web_search_description

    desc = _build_web_search_description()
    assert "search_mode=free" not in desc


def test_build_web_search_description_shows_free_mode_when_free_engines_enabled(monkeypatch):
    import os
    from jiuwenswarm.common.local_env_config import (
        apply_env_overrides_to_active,
        bind_agent_env_ns,
        reset_agent_env_ns,
        reset_local_env_state_for_tests,
    )

    saved = dict(os.environ)
    reset_local_env_state_for_tests()
    try:
        apply_env_overrides_to_active(
            {"FREE_SEARCH_DDG_ENABLED": "true"},
            service_id="default",
            agent_id="test",
        )
        token = bind_agent_env_ns("default", "test")
        try:
            from jiuwenswarm.agents.harness.common.tools.web_search.tool import _build_web_search_description

            desc = _build_web_search_description()
            assert "search_mode=free" in desc
        finally:
            reset_agent_env_ns(token)
    finally:
        reset_local_env_state_for_tests()
        os.environ.clear()
        os.environ.update(saved)
