"""Unit tests for the host diagnostics application plugin.

The tests are deliberately layered:

* collector tests exercise pure standard-library logic;
* contract tests assert the extension registration surface the loader relies
  on (``register_extensions`` + ``ApplicationPluginExtension``);
* handler tests drive every RPC method through a fake channel;
* gating tests use the real ``_ApplicationPluginChannel`` so the
  ``available_when_disabled`` behaviour is verified against framework code
  rather than a stand-in.

They need neither ``openjiuwen`` nor ``fastapi``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from jiuwenswarm.extensions.host_diagnostics import backend as host_backend
from jiuwenswarm.extensions.host_diagnostics.backend import collector, handlers
from jiuwenswarm.extensions.host_diagnostics.extension import (
    PLUGIN_ID,
    HostDiagnosticsPlugin,
    register_extensions,
)
from jiuwenswarm.extensions.registry import (
    ExtensionRegistry,
    _ApplicationPluginChannel,
)
from jiuwenswarm.extensions.sdk.application_plugin import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
)

EXTENSION_DIR = Path(host_backend.__file__).resolve().parent.parent


# --------------------------------------------------------------------- fakes


class FakeChannel:
    """Duck-typed stand-in for the Gateway web channel."""

    def __init__(self) -> None:
        self.methods: dict[str, dict[str, Any]] = {}
        self.responses: list[dict[str, Any]] = []

    def register_method(
        self,
        method: str,
        handler: Any,
        *,
        local_only: bool = False,
        available_when_disabled: bool = False,
    ) -> None:
        self.methods[method] = {
            "handler": handler,
            "local_only": local_only,
            "available_when_disabled": available_when_disabled,
        }

    async def send_response(
        self,
        ws: Any,
        req_id: Any,
        *,
        ok: bool = True,
        payload: Any = None,
        error: str | None = None,
        code: str | None = None,
    ) -> None:
        self.responses.append(
            {
                "ws": ws,
                "req_id": req_id,
                "ok": ok,
                "payload": payload,
                "error": error,
                "code": code,
            }
        )

    async def call(
        self,
        method: str,
        params: Any = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Invoke a registered method and return its single response."""
        self.responses.clear()
        await self.methods[method]["handler"](None, "req-1", params, session_id)
        assert len(self.responses) == 1, "handler must answer exactly once"
        return self.responses[0]


class FakeRegistry:
    """Duck-typed stand-in for ``ExtensionRegistry``."""

    def __init__(self) -> None:
        self.plugins: dict[str, Any] = {}
        self._agent_server_client = None
        self._third_agent = None
        self._crypto_tool = None

    def register_application_plugin(self, plugin: Any) -> None:
        self.plugins[plugin.plugin_id] = plugin

    def get_application_plugins(self) -> tuple[Any, ...]:
        return tuple(self.plugins.values())

    def get_agent_server_client_extension(self) -> Any:
        return self._agent_server_client

    def get_third_agent_extension(self) -> Any:
        return self._third_agent

    def get_crypto_utility_extension(self) -> Any:
        return self._crypto_tool


class FakeMetadata:
    """Stand-in for ``ExtensionMetadata`` used by inventory tests."""

    id = "fake-plugin"
    name = "Fake Plugin"
    version = "9.9.9"
    description = "inventory fixture"
    package_type = "application"
    permissions = ("camera",)


class FakePlugin:
    """Minimal duck-typed application plugin."""

    plugin_id = "fake-plugin"
    metadata = FakeMetadata()

    def is_enabled(self) -> bool:
        return True


class BrokenMetadataPlugin(FakePlugin):
    """A plugin whose metadata property raises, as a bad manifest would."""

    @property
    def metadata(self) -> Any:  # type: ignore[override]
        raise FileNotFoundError("extension.yaml missing")


def _named_plugin(plugin_id: str, version: str = "1.0.0") -> Any:
    """Build a duck-typed plugin reporting a distinct id and version."""
    metadata = type(
        "Meta",
        (),
        {
            "id": plugin_id,
            "name": plugin_id,
            "version": version,
            "description": "",
            "package_type": "application",
            "permissions": (),
        },
    )
    return type(
        "Plugin",
        (FakePlugin,),
        {"plugin_id": plugin_id, "metadata": metadata()},
    )()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def plugin(registry: FakeRegistry) -> HostDiagnosticsPlugin:
    return HostDiagnosticsPlugin(registry)


@pytest.fixture
def bound(
    plugin: HostDiagnosticsPlugin, channel: FakeChannel
) -> tuple[HostDiagnosticsPlugin, FakeChannel]:
    plugin.bind_web_channel(channel, ApplicationPluginServices())
    return plugin, channel


# ------------------------------------------------------------------ manifest


def test_manifest_matches_plugin_id() -> None:
    manifest = yaml.safe_load((EXTENSION_DIR / "extension.yaml").read_text("utf-8"))
    assert manifest["id"] == PLUGIN_ID


def test_manifest_declares_application_package() -> None:
    manifest = yaml.safe_load((EXTENSION_DIR / "extension.yaml").read_text("utf-8"))
    assert manifest["package_type"] == "application"
    assert manifest["author"] == "zhuyingming0708"
    assert manifest["version"] == "1.0.0"
    assert manifest["dependencies"] == {}


def test_manifest_is_next_to_extension_entry() -> None:
    assert (EXTENSION_DIR / "extension.py").is_file()
    assert (EXTENSION_DIR / "extension.yaml").is_file()


def test_metadata_loads_from_manifest(plugin: HostDiagnosticsPlugin) -> None:
    assert plugin.metadata.id == PLUGIN_ID
    assert plugin.metadata.package_type == "application"


# ------------------------------------------------------------------ contract


async def test_register_extensions_registers_one_plugin(
    registry: FakeRegistry,
) -> None:
    registered = await register_extensions(registry)

    assert len(registered) == 1
    assert registry.plugins == {PLUGIN_ID: registered[0]}
    assert isinstance(registered[0], ApplicationPluginExtension)
    assert registered[0].plugin_id == PLUGIN_ID


async def test_register_extensions_is_idempotent_per_registry() -> None:
    first = FakeRegistry()
    second = FakeRegistry()
    await register_extensions(first)
    await register_extensions(second)
    assert set(first.plugins) == set(second.plugins) == {PLUGIN_ID}


async def test_real_registry_accepts_the_plugin() -> None:
    registry = ExtensionRegistry(
        callback_framework=object(),
        config={},
        logger=logging.getLogger("test"),
    )
    plugin = HostDiagnosticsPlugin(registry)
    registry.register_application_plugin(plugin)

    assert registry.get_application_plugin(PLUGIN_ID) is plugin
    with pytest.raises(ValueError, match="already registered"):
        registry.register_application_plugin(HostDiagnosticsPlugin(registry))


async def test_frontend_contribution_is_bundled_page(
    plugin: HostDiagnosticsPlugin,
) -> None:
    contributions = plugin.frontend_contributions()

    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.render_mode == "bundled"
    assert contribution.component == PLUGIN_ID
    assert contribution.nav_key == f"app:{PLUGIN_ID}"
    assert contribution.title == "Host Diagnostics"


def test_websocket_routes_are_not_contributed(plugin: HostDiagnosticsPlugin) -> None:
    assert plugin.websocket_routes() == ()


def test_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(handlers.ENABLED_ENV, raising=False)
    assert HostDiagnosticsPlugin().is_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
def test_disable_switch(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(handlers.ENABLED_ENV, raw)
    assert HostDiagnosticsPlugin().is_enabled() is False


async def test_initialize_records_extension_dir(plugin: HostDiagnosticsPlugin) -> None:
    await plugin.initialize({})
    response = await _bound_call(plugin, "host.diagnostics.paths")
    assert response["payload"]["extension_dir"] == str(EXTENSION_DIR)


async def test_shutdown_is_a_noop(plugin: HostDiagnosticsPlugin) -> None:
    assert await plugin.shutdown() is None


async def _bound_call(
    plugin: HostDiagnosticsPlugin, method: str, params: Any = None
) -> dict[str, Any]:
    """Bind ``plugin`` to a fresh channel, invoke ``method``, return response."""
    channel = FakeChannel()
    plugin.bind_web_channel(channel, ApplicationPluginServices())
    return await channel.call(method, params)


# ------------------------------------------------------------------- handlers


def test_registers_expected_method_surface(bound: tuple[Any, FakeChannel]) -> None:
    _, channel = bound
    assert set(channel.methods) == {
        "host.diagnostics.ping",
        "host.diagnostics.summary",
        "host.diagnostics.paths",
        "host.diagnostics.plugins",
        "host.diagnostics.settings.get",
        "host.diagnostics.settings.update",
        "host.diagnostics.settings.set_enabled",
    }


def test_every_method_is_local_only(bound: tuple[Any, FakeChannel]) -> None:
    _, channel = bound
    assert all(entry["local_only"] for entry in channel.methods.values())


def test_only_management_methods_survive_disable(
    bound: tuple[Any, FakeChannel],
) -> None:
    _, channel = bound
    surviving = {
        method
        for method, entry in channel.methods.items()
        if entry["available_when_disabled"]
    }
    assert surviving == set(handlers.MANAGEMENT_METHODS)


async def test_ping_reports_ok(bound: tuple[Any, FakeChannel]) -> None:
    _, channel = bound
    response = await channel.call("host.diagnostics.ping")

    assert response["ok"] is True
    assert response["payload"]["status"] == "ok"
    assert isinstance(response["payload"]["pid"], int)


async def test_summary_omits_env_by_default(
    bound: tuple[Any, FakeChannel],
) -> None:
    _, channel = bound
    response = await channel.call("host.diagnostics.summary")

    payload = response["payload"]
    assert response["ok"] is True
    assert set(payload) == {"platform", "python", "process"}
    assert payload["python"]["implementation"]


async def test_summary_can_include_redacted_env(
    bound: tuple[Any, FakeChannel], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOST_DIAGNOSTICS_TEST_TOKEN", "super-secret")
    _, channel = bound
    response = await channel.call("host.diagnostics.summary", {"include_env": True})

    env = response["payload"]["env"]
    assert env["entries"]["HOST_DIAGNOSTICS_TEST_TOKEN"] == collector.REDACTED
    assert "super-secret" not in str(response["payload"])


async def test_summary_rejects_non_boolean_include_env(
    bound: tuple[Any, FakeChannel],
) -> None:
    _, channel = bound
    response = await channel.call("host.diagnostics.summary", {"include_env": "yes"})

    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"


async def test_paths_reports_cwd(bound: tuple[Any, FakeChannel]) -> None:
    _, channel = bound
    response = await channel.call("host.diagnostics.paths")

    assert response["ok"] is True
    assert response["payload"]["cwd"]


async def test_plugins_lists_application_plugins(
    registry: FakeRegistry, channel: FakeChannel
) -> None:
    registry.plugins["fake-plugin"] = FakePlugin()
    plugin = HostDiagnosticsPlugin(registry)
    plugin.bind_web_channel(channel, ApplicationPluginServices())

    response = await channel.call("host.diagnostics.plugins")
    described = response["payload"]["application_plugins"]

    assert [item["plugin_id"] for item in described] == ["fake-plugin"]
    assert described[0]["permissions"] == ["camera"]
    assert response["payload"]["typed_extensions"] == {
        "agent_server_client": None,
        "third_agent": None,
        "crypto_utility": None,
    }


async def test_settings_get_returns_defaults(bound: tuple[Any, FakeChannel]) -> None:
    _, channel = bound
    response = await channel.call("host.diagnostics.settings.get")

    assert response["payload"]["include_env"] is False
    assert response["payload"]["env_limit"] == collector.MAX_ENV_ENTRIES


async def test_settings_update_applies_valid_values(
    bound: tuple[Any, FakeChannel],
) -> None:
    _, channel = bound
    response = await channel.call(
        "host.diagnostics.settings.update",
        {"values": {"include_env": True, "env_limit": 3}},
    )

    assert response["ok"] is True
    assert response["payload"]["include_env"] is True
    assert response["payload"]["env_limit"] == 3


@pytest.mark.parametrize(
    "values",
    [
        {"unknown": 1},
        {"include_env": "yes"},
        {"env_limit": 0},
        {"env_limit": 10_000},
        {"env_limit": True},
        {"env_limit": "3"},
    ],
)
async def test_settings_update_rejects_bad_values(
    bound: tuple[Any, FakeChannel], values: dict[str, Any]
) -> None:
    _, channel = bound
    response = await channel.call(
        "host.diagnostics.settings.update", {"values": values}
    )

    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"


async def test_settings_update_rejects_non_object_params(
    bound: tuple[Any, FakeChannel],
) -> None:
    _, channel = bound
    response = await channel.call("host.diagnostics.settings.update", "nope")

    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"


async def test_settings_set_enabled_round_trip(
    bound: tuple[Any, FakeChannel], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(handlers.ENABLED_ENV, raising=False)
    plugin, channel = bound

    off = await channel.call(
        "host.diagnostics.settings.set_enabled", {"enabled": False}
    )
    assert off["payload"]["enabled"] is False
    assert plugin.is_enabled() is False

    on = await channel.call("host.diagnostics.settings.set_enabled", {"enabled": True})
    assert on["payload"]["enabled"] is True


async def test_settings_set_enabled_requires_boolean(
    bound: tuple[Any, FakeChannel],
) -> None:
    _, channel = bound
    response = await channel.call(
        "host.diagnostics.settings.set_enabled", {"enabled": 1}
    )

    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"


async def test_handlers_never_raise_into_the_channel(
    bound: tuple[Any, FakeChannel], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collector failure must surface as an error response, not an exception."""
    _, channel = bound

    def boom(**_: Any) -> Any:
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(handlers, "collect_health", boom)
    response = await channel.call("host.diagnostics.ping")

    assert response["ok"] is False
    assert response["code"] == "INTERNAL_ERROR"
    assert "collector exploded" in response["error"]


# --------------------------------------------------------------------- gating


async def test_data_methods_are_gated_when_disabled(
    plugin: HostDiagnosticsPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind through the real gating channel, exactly as the registry does."""
    monkeypatch.setenv(handlers.ENABLED_ENV, "0")
    channel = FakeChannel()
    plugin.bind_web_channel(
        _ApplicationPluginChannel(channel, plugin), ApplicationPluginServices()
    )

    response = await channel.call("host.diagnostics.ping")

    assert response["ok"] is False
    assert response["code"] == "APPLICATION_PLUGIN_DISABLED"
    assert PLUGIN_ID in response["error"]


async def test_management_methods_answer_while_disabled(
    plugin: HostDiagnosticsPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``available_when_disabled`` keeps the settings UI reachable."""
    monkeypatch.setenv(handlers.ENABLED_ENV, "0")
    channel = FakeChannel()
    plugin.bind_web_channel(
        _ApplicationPluginChannel(channel, plugin), ApplicationPluginServices()
    )

    response = await channel.call("host.diagnostics.settings.get")

    assert response["ok"] is True
    assert response["payload"]["enabled"] is False


# ------------------------------------------------------------------ collector


def test_collect_env_redacts_sensitive_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_API_KEY", "abc123")
    monkeypatch.setenv("MY_PLAIN_VALUE", "visible")

    snapshot = collector.collect_env(limit=collector.MAX_ENV_ENTRIES)

    assert snapshot["entries"]["MY_API_KEY"] == collector.REDACTED
    assert snapshot["entries"]["MY_PLAIN_VALUE"] == "visible"
    assert snapshot["redacted"] >= 1


def test_collect_env_honours_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(5):
        monkeypatch.setenv(f"DIAG_LIMIT_{index}", str(index))

    snapshot = collector.collect_env(limit=2)

    assert snapshot["returned"] == 2
    assert snapshot["total"] >= 5
    assert snapshot["truncated"] is True


@pytest.mark.parametrize("limit", [0, -1, "3", 1.5, True, None])
def test_collect_env_rejects_bad_limit(limit: Any) -> None:
    with pytest.raises(ValueError):
        collector.collect_env(limit=limit)


def test_collect_process_uptime_is_monotonic() -> None:
    early = collector.collect_process(now=collector._PROCESS_STARTED_AT)
    later = collector.collect_process(now=collector._PROCESS_STARTED_AT + 10)

    assert early["uptime_seconds"] == 0.0
    assert later["uptime_seconds"] == 10.0
    assert later["pid"] == early["pid"]


def test_collect_platform_and_python_shapes() -> None:
    assert set(collector.collect_platform()) == {
        "system",
        "release",
        "version",
        "machine",
        "processor",
        "hostname",
    }
    assert set(collector.collect_python()) == {
        "version",
        "implementation",
        "executable",
        "prefix",
        "frozen",
    }


def test_collect_paths_defaults_extension_dir_to_empty() -> None:
    assert collector.collect_paths()["extension_dir"] == ""


def test_describe_plugin_falls_back_to_plugin_id() -> None:
    described = collector.describe_plugin(BrokenMetadataPlugin())

    assert described["plugin_id"] == "fake-plugin"
    assert described["name"] == "fake-plugin"
    assert described["permissions"] == []
    assert described["enabled"] is True


def test_describe_application_plugins_survives_missing_registry() -> None:
    assert collector.describe_application_plugins(None) == []


def test_describe_application_plugins_is_sorted() -> None:
    registry = FakeRegistry()
    registry.plugins["b-plugin"] = _named_plugin("b-plugin")
    registry.plugins["a-plugin"] = _named_plugin("a-plugin")

    described = collector.describe_application_plugins(registry)

    assert [item["plugin_id"] for item in described] == ["a-plugin", "b-plugin"]


def test_describe_typed_extensions_reports_class_names() -> None:
    registry = FakeRegistry()
    registry._crypto_tool = FakePlugin()

    described = collector.describe_typed_extensions(registry)

    assert described["crypto_utility"] == "FakePlugin"
    assert described["third_agent"] is None


def test_describe_typed_extensions_survives_missing_registry() -> None:
    assert collector.describe_typed_extensions(None) == {
        "agent_server_client": None,
        "third_agent": None,
        "crypto_utility": None,
    }
