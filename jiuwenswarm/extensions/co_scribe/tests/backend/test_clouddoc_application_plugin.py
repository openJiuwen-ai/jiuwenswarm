"""Co-scribe is contributed as an application plugin, gated by clouddoc.enabled.

The Docs page used to be a hardcoded entry in the web shell's nav rail. It is a
frontend contribution now, which means two things have to hold: the plugin has
to register itself from its own directory the way any application plugin does,
and ``is_enabled`` has to be the deployment flag rather than a copy of it --
because the rail's entry, the badge on the 应用插件 card and the feature itself
all read that one answer.
"""

from __future__ import annotations

import pathlib
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.extensions.application_host import application_plugin_manifest
from jiuwenswarm.extensions.co_scribe.backend import settings
from jiuwenswarm.extensions.loader import ExtensionLoader
from jiuwenswarm.extensions.sdk.application_plugin import FrontendContribution
from jiuwenswarm.extensions.registry import (
    ExtensionRegistry,
    _ApplicationPluginChannel,
)


# The tests live inside the plugin now, so its root is two levels up.
CO_SCRIBE_ROOT = Path(__file__).resolve().parents[2]


class _Registry:
    """Just enough of ExtensionRegistry for the loader to register into."""

    def __init__(self) -> None:
        self.plugins: dict[str, Any] = {}
        self.config = None

    def register_application_plugin(self, extension: Any) -> None:
        self.plugins[extension.plugin_id] = extension


class _Channel:
    def __init__(self) -> None:
        self.methods: dict[str, Any] = {}
        self.responses: list[dict] = []

    def register_method(self, method, handler, *, local_only=False):  # noqa: ANN001
        self.methods[method] = handler

    async def send_response(self, ws, req_id, **payload):  # noqa: ANN001
        self.responses.append({"id": req_id, **payload})


@pytest.fixture()
def flag(monkeypatch):
    """An in-memory stand-in for the clouddoc section of config.yaml."""

    state: dict[str, Any] = {"clouddoc": {"connections": [{"credentials_file": "k"}]}}
    monkeypatch.setattr(settings.config, "get_config", lambda: state)
    monkeypatch.setattr(settings.config, "update_config", lambda mutate: mutate(state))
    return state["clouddoc"]


@pytest.mark.asyncio
async def test_the_plugin_registers_itself_from_its_own_directory():
    registry = _Registry()
    loaded = await ExtensionLoader(registry).load_extension(CO_SCRIBE_ROOT)

    assert list(registry.plugins) == ["co-scribe"]
    plugin = registry.plugins["co-scribe"]
    assert loaded == [plugin]
    assert plugin.metadata.package_type == "application"

    (contribution,) = plugin.frontend_contributions()
    # A bundled page: it compiles into the web build and is resolved by the
    # plugin id, so no iframe entrypoint and no prebuilt asset root.
    assert contribution.render_mode == "bundled"
    assert contribution.nav_key == "app:co-scribe"
    assert contribution.title_i18n_key == "nav.docs"
    assert contribution.entrypoint == ""


def test_is_enabled_follows_the_deployment_flag_in_both_directions(flag):
    plugin = _load_plugin()

    flag["enabled"] = True
    assert plugin.is_enabled() is True

    flag["enabled"] = False
    assert plugin.is_enabled() is False

    flag["enabled"] = True
    assert plugin.is_enabled() is True


def test_a_deployment_that_never_configured_cloud_docs_reads_as_off(flag):
    flag.pop("enabled", None)
    assert _load_plugin().is_enabled() is False


def test_the_manifest_reports_the_flag_it_is_asked_for(flag):
    registry = ExtensionRegistry(
        callback_framework=None, config={}, logger=None
    )
    plugin = _load_plugin()
    registry.register_application_plugin(plugin)

    flag["enabled"] = True
    (entry,) = application_plugin_manifest(registry)["plugins"]
    assert entry["plugin_id"] == "co-scribe"
    assert entry["enabled"] is True
    assert entry["nav_key"] == "app:co-scribe"

    flag["enabled"] = False
    (entry,) = application_plugin_manifest(registry)["plugins"]
    assert entry["enabled"] is False


def test_the_manifest_carries_the_plugin_identity(flag):
    """Icon and description travel with the plugin, not with a marketplace card.

    The icon is inlined as a data URI because ``jiuwenswarm/extensions/`` is on
    no static route -- and an application plugin can just as well live in a
    user's ``application_plugins`` folder, which is on none either.
    """

    registry = ExtensionRegistry(callback_framework=None, config={}, logger=None)
    registry.register_application_plugin(_load_plugin())
    (entry,) = application_plugin_manifest(registry)["plugins"]

    # A monochrome SVG, not a raster: the rail paints the mark as a mask so it
    # takes the theme's colour the way every built-in icon does, and a bitmap
    # cannot follow a theme.
    # Two marks, two jobs: the rail's is a monochrome SVG it paints as a mask so
    # it follows the theme; the card's is the product's own artwork, which a
    # mask would flatten to a silhouette.
    assert entry["icon"].startswith("data:image/svg+xml;base64,")
    assert entry["logo"].startswith("data:image/png;base64,")
    assert entry["description_i18n_key"] == "docs.plugin.summary"
    assert entry["description"], "the untranslated fallback must not be empty"
    # The retired revert must not be promised anywhere in the identity.
    assert "revert" not in entry["description"].lower()
    assert "回退" not in entry["description"]


def test_the_declared_icon_is_a_real_file_in_the_plugin_directory():
    plugin = _load_plugin()
    (contribution,) = plugin.frontend_contributions()
    assert (CO_SCRIBE_ROOT / contribution.icon).is_file()


def test_a_plugin_without_an_icon_still_produces_an_entry(flag):
    """video_duplex declares none; its card falls back to the generic glyph."""

    plugin = _load_plugin()
    plugin.frontend_contributions = lambda: (  # type: ignore[method-assign]
        FrontendContribution(
            id="co-scribe-docs",
            nav_key="app:co-scribe",
            title="Docs",
            render_mode="bundled",
            component="co-scribe",
        ),
    )
    registry = ExtensionRegistry(callback_framework=None, config={}, logger=None)
    registry.register_application_plugin(plugin)

    (entry,) = application_plugin_manifest(registry)["plugins"]
    assert "icon" not in entry
    assert entry["description_i18n_key"] == ""
    assert entry["title"] == "Docs"


@pytest.mark.parametrize(
    "icon",
    ["", "../../../etc/passwd", "/etc/passwd", "assets/missing.png", "extension.py"],
)
def test_an_unusable_icon_resolves_to_nothing_rather_than_a_broken_image(icon):
    assert _load_plugin().resolve_icon(icon) == ""


def test_the_collaboration_skill_reaches_the_agents_skill_library():
    """Where an application plugin's skill lives.

    The SDK has no skill contribution point, and a plugin package's ``skills``
    array only binds when a session names the package in ``plugin_names`` --
    which an application plugin never appears in, and which unattended watcher
    turns have no picker to set. The one root every session scans is the skills
    library, so that is where the skill ships from: the built-in shelf, on both
    default-install lists so a fresh workspace gets it and an upgraded one is
    back-filled on the next launch.
    """

    from jiuwenswarm.common import utils

    shelf = utils.get_builtin_skills_dir() / "co-scribe-collab"
    assert (shelf / "SKILL.md").is_file()

    source = (
        pathlib.Path(utils.__file__).read_text(encoding="utf-8")
    )
    assert source.count('"co-scribe-collab",') == 2, (
        "the skill must be on both _install_default_builtin_skills (fresh "
        "workspaces) and ensure_default_builtin_skills (upgrades)"
    )


def test_the_skill_is_installed_into_the_skills_library_on_launch(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils

    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path / ".jiuwenswarm")
    utils.ensure_default_builtin_skills()

    installed = utils.get_agent_skills_dir() / "co-scribe-collab" / "SKILL.md"
    assert installed.is_file()
    assert "clouddoc_read" in installed.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_the_card_can_turn_the_feature_on_and_off(flag):
    """The switch on the 应用插件 card writes the same flag it reads.

    Both methods stay reachable while the plugin is disabled -- a switch that
    refuses to answer once it is off can never turn the feature back on.
    """

    flag["enabled"] = False
    plugin = _load_plugin()
    channel = _Channel()
    # Bound through the registry's own wrapper, so the disabled gate the
    # registry installs is the one under test, not a stand-in for it.
    plugin.bind_web_channel(_ApplicationPluginChannel(channel, plugin), None)

    assert set(channel.methods) == {
        "co_scribe.settings.get",
        "co_scribe.settings.set_enabled",
    }

    await channel.methods["co_scribe.settings.get"](None, "1", {}, None)
    assert channel.responses[-1]["payload"] == {"enabled": False}

    await channel.methods["co_scribe.settings.set_enabled"](
        None, "2", {"enabled": True}, None
    )
    assert channel.responses[-1]["payload"] == {"enabled": True}
    assert flag["enabled"] is True
    # Enabling repairs an unset mode so the watcher has one it understands.
    assert flag["mode"] == "mandate"
    assert plugin.is_enabled() is True

    await channel.methods["co_scribe.settings.set_enabled"](
        None, "3", {"enabled": False}, None
    )
    assert flag["enabled"] is False
    assert plugin.is_enabled() is False
    # Turning it off hides the surfaces; it does not discard what was configured.
    assert flag["connections"] == [{"credentials_file": "k"}]


@pytest.mark.asyncio
async def test_the_switch_refuses_a_non_boolean(flag):
    plugin = _load_plugin()
    channel = _Channel()
    # Bound through the registry's own wrapper, so the disabled gate the
    # registry installs is the one under test, not a stand-in for it.
    plugin.bind_web_channel(_ApplicationPluginChannel(channel, plugin), None)

    await channel.methods["co_scribe.settings.set_enabled"](
        None, "1", {"enabled": "yes"}, None
    )
    assert channel.responses[-1]["ok"] is False
    assert channel.responses[-1]["code"] == "BAD_REQUEST"
    assert "enabled" not in flag


def _load_plugin():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "tests.co_scribe_extension", CO_SCRIBE_ROOT / "extension.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plugin = module.CoScribeApplicationPlugin()
    plugin.set_extension_dir(CO_SCRIBE_ROOT)
    return plugin
