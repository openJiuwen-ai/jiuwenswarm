"""Co-scribe as an application plugin.

The Docs page used to be a hardcoded entry in the web shell's nav rail, gated by
a bespoke flag the shell fetched for itself. It is contributed here instead: the
registry publishes the manifest, the rail renders whatever the manifest lists,
and ``is_enabled`` is the one place that answers whether co-scribe is on.
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
    FrontendContribution,
)
from jiuwenswarm.extensions.co_scribe.backend import settings


class CoScribeApplicationPlugin(ApplicationPluginExtension):
    plugin_id = "co-scribe"

    def is_enabled(self) -> bool:
        return settings.clouddoc_enabled()

    async def initialize(self, config: Any) -> None:
        del config

    async def shutdown(self) -> None:
        return None

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        del services

        async def get_settings(ws, req_id, params, session_id):  # noqa: ANN001
            del params, session_id
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload=settings.settings_payload(),
            )

        async def set_enabled(ws, req_id, params, session_id):  # noqa: ANN001
            del session_id
            enabled = params.get("enabled") if isinstance(params, dict) else None
            if not isinstance(enabled, bool):
                await channel.send_response(
                    ws,
                    req_id,
                    ok=False,
                    error="enabled must be a boolean",
                    code="BAD_REQUEST",
                )
                return
            settings.set_clouddoc_enabled(enabled)
            await channel.send_response(
                ws,
                req_id,
                ok=True,
                payload=settings.settings_payload(),
            )

        # Both stay reachable while the plugin is off: the card on the 应用插件
        # page is how a user turns it back on, and a disabled plugin whose own
        # switch is refused can never be re-enabled from the interface.
        channel.register_method(
            "co_scribe.settings.get",
            get_settings,
            local_only=True,
            available_when_disabled=True,
        )
        channel.register_method(
            "co_scribe.settings.set_enabled",
            set_enabled,
            local_only=True,
            available_when_disabled=True,
        )

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return (
            FrontendContribution(
                id="co-scribe-docs",
                nav_key="app:co-scribe",
                title="Docs",
                # The rail and the card both render this key, so the entry keeps
                # the label it had while it was hardcoded in the shell.
                title_i18n_key="nav.docs",
                # The page is "Cloud Docs"; the plugin that provides it is
                # "Co-scribe for Cloud Docs". The card shows both.
                name_i18n_key="docs.plugin.name",
                render_mode="bundled",
                component="co-scribe",
                # After the built-in rail, and after the full-duplex plugin (75),
                # so the contributed band has a stable order.
                position=80,
                # Identity travels with the plugin now that its marketplace
                # package is gone. The path is relative to this directory; the
                # host inlines the bytes onto the manifest.
                # A line mark in the rail's own idiom rather than the product
                # logo: the rail renders it as a mask so it takes currentColor,
                # which is how every built-in entry follows the theme.
                icon="assets/nav-icon.svg",
                # The card shows the product's own mark; the rail cannot, since
                # a bitmap will not follow the theme.
                logo="assets/icon.png",
                description_i18n_key="docs.plugin.summary",
            ),
        )


async def register_extensions(registry: Any) -> list[CoScribeApplicationPlugin]:
    extension = CoScribeApplicationPlugin()
    registry.register_application_plugin(extension)
    return [extension]
