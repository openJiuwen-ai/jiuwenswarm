from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter import interface_deep, interface_flash


def test_deep_web_factory_keeps_existing_tool_builder(monkeypatch) -> None:
    sentinel = [object(), object()]
    captured = {}

    def _build(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(interface_deep, "build_jiuwen_harness_named_web_tools", _build)
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    adapter._resolve_runtime_language = lambda: "cn"

    assert adapter._build_web_tools("deep-agent", cache="cache") is sentinel
    assert captured == {"agent_id": "deep-agent", "language": "cn", "cache": "cache"}


def test_flash_web_factory_exposes_only_web_flash(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.flash.tools import web_flash

    sentinel = object()
    monkeypatch.setattr(web_flash, "build_web_flash_tool", lambda **_kwargs: sentinel)
    adapter = object.__new__(interface_flash.JiuwenSwarmFlashAdapter)
    adapter._resolve_runtime_language = lambda: "cn"

    assert adapter._build_web_tools("flash-agent", cache="cache") == [sentinel]


def test_flash_cron_factory_exposes_only_cron_flash(monkeypatch) -> None:
    from jiuwenswarm.agents.harness.flash.tools import cron_flash

    sentinel = SimpleNamespace(card=SimpleNamespace(name="cron_flash"))
    backend = object()

    class _Bridge:
        def __init__(self) -> None:
            self.ensure_calls = []

        def get_backend(self, **_kwargs):
            return backend

        def ensure_scheduler_started(self, **kwargs):
            self.ensure_calls.append(kwargs)

    bridge = _Bridge()
    monkeypatch.setattr(
        cron_flash,
        "build_cron_flash_tool",
        lambda actual_backend, **_kwargs: sentinel if actual_backend is backend else None,
    )
    adapter = object.__new__(interface_flash.JiuwenSwarmFlashAdapter)
    adapter._cron_runtime = bridge
    adapter._runtime_cron_tool_context = object()
    adapter._env_service_id = "service"
    adapter._env_agent_id = "tenant-agent"
    adapter._tool_owner_id = lambda: "tool-owner"
    adapter._resolve_runtime_language = lambda: "cn"

    assert adapter._build_cron_tools() == [sentinel]
    assert bridge.ensure_calls == [{"service_id": "service", "agent_id": "tenant-agent"}]
    assert adapter._cron_tool_names() == frozenset({"cron_flash"})


def test_deep_cron_lifecycle_names_do_not_include_flash() -> None:
    adapter = object.__new__(interface_deep.JiuWenSwarmDeepAdapter)
    names = adapter._cron_tool_names()
    assert "cron_create_job" in names
    assert "cron_flash" not in names


def test_flash_progressive_tools_replace_deep_web_and_cron(monkeypatch) -> None:
    captured = {}

    def _build(_self, config):
        captured.update(config)
        return "rail"

    monkeypatch.setattr(interface_deep.JiuWenSwarmDeepAdapter, "_build_progressive_tool_rail", _build)
    adapter = object.__new__(interface_flash.JiuwenSwarmFlashAdapter)
    result = adapter._build_progressive_tool_rail({
        "tool_lazy_load": {
            "enabled": True,
            "eager_tools": [
                "bash",
                "web_search",
                "fetch_webpage",
                "cron_create_job",
                "cron_metrics",
            ],
        },
    })

    assert result == "rail"
    eager = captured["tool_lazy_load"]["eager_tools"]
    assert eager == ["bash", "cron_metrics", "web_flash", "cron_flash"]
