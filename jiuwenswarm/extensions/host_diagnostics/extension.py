"""Host diagnostics application plugin (extension entry point).

Discovered by ``ExtensionLoader`` because this directory contains both
``extension.yaml`` and ``extension.py``.  The loader imports this module and
awaits :func:`register_extensions`, which registers the plugin on the shared
``ExtensionRegistry``.

Design notes
------------
* Read-only: the plugin never mutates host state except for its own
  ``HOST_DIAGNOSTICS_ENABLED`` flag.
* No third-party imports: only ``jiuwenswarm.extensions.sdk`` (which is
  lazily exported and free of ``openjiuwen`` imports) plus this plugin's own
  standard-library-only backend package.
* No hook subscriptions and no WebSocket routes, so it cannot interfere with
  existing chat traffic.
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.extensions.host_diagnostics.backend.handlers import (
    DiagnosticsSettings,
    diagnostics_enabled,
    register_handlers,
)
from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
    FrontendContribution,
)

#: Plugin identity.  Must match ``id`` in ``extension.yaml`` and the
#: ``applicationPluginId`` exported by ``frontend/index.tsx``.
PLUGIN_ID = "host-diagnostics"


class HostDiagnosticsPlugin(ApplicationPluginExtension):
    """Expose read-only runtime diagnostics through Gateway-local RPC."""

    plugin_id = PLUGIN_ID

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry
        self._settings = DiagnosticsSettings.from_env()
        self._extension_dir: str | None = None

    def is_enabled(self) -> bool:
        """Diagnostics are on by default; ``HOST_DIAGNOSTICS_ENABLED=0`` disables."""
        return diagnostics_enabled()

    async def initialize(self, config: Any) -> None:
        """Capture the resolved extension directory for the ``paths`` method."""
        del config
        root = self._get_extension_dir()
        self._extension_dir = str(root) if root is not None else None

    async def shutdown(self) -> None:
        """Nothing to release: the plugin holds no resources or connections."""
        return

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        """Register the plugin's RPC methods on the Gateway web channel.

        ``services`` is intentionally unused: diagnostics never needs the Core
        Agent client or the media attachment normalizer, so the plugin keeps
        working even when those host services are unavailable.
        """
        del services
        register_handlers(
            channel,
            registry=self._registry,
            settings=self._settings,
            extension_dir=self._extension_dir,
        )

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        """Contribute one bundled workspace page."""
        return (
            FrontendContribution(
                id="host-diagnostics-page",
                nav_key=f"app:{PLUGIN_ID}",
                title="Host Diagnostics",
                title_i18n_key="applicationPlugins.hostDiagnostics.title",
                render_mode="bundled",
                component=PLUGIN_ID,
                position=120,
            ),
        )


async def register_extensions(registry: Any) -> list[HostDiagnosticsPlugin]:
    """Register the plugin. Called by ``ExtensionLoader`` after import."""
    plugin = HostDiagnosticsPlugin(registry)
    registry.register_application_plugin(plugin)
    return [plugin]
