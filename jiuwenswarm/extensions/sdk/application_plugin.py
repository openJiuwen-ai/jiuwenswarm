from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable

from jiuwenswarm.extensions.sdk.base import BaseExtension


WebSocketEndpoint = Callable[[Any], Awaitable[None]]

# Raster and vector types a browser renders from a data URI without help.
ICON_MIME_TYPES = {
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class ApplicationPluginServices:
    """Core services made available while an application plugin binds to Gateway."""

    agent_client: Any = None
    media_attachment_normalizer: Callable[[dict[str, Any], str | None], None] | None = None

    def require_agent_client(self) -> Any:
        """Return the registered Core Agent client or fail during plugin binding."""

        if self.agent_client is None:
            raise RuntimeError("application plugin requires the Core Agent service")
        return self.agent_client

    def normalize_media_attachments(
        self,
        params: dict[str, Any],
        session_id: str | None,
    ) -> None:
        """Prepare browser media through the host-registered attachment service."""

        if self.media_attachment_normalizer is None:
            raise RuntimeError(
                "application plugin requires the media attachment service"
            )
        self.media_attachment_normalizer(params, session_id)


@dataclass(frozen=True)
class WebSocketRouteContribution:
    """A plugin WebSocket route, optionally retained for host-level entry points."""

    path: str
    endpoint: WebSocketEndpoint
    check_origin: bool = True
    available_when_disabled: bool = False


@dataclass(frozen=True)
class FrontendContribution:
    """A page contribution exposed to the web frontend.

    ``bundled`` entries are compiled with Jiuwen and resolved by ``component``.
    ``iframe`` entries are prebuilt assets shipped by an installable plugin.

    Two marks, because they do different jobs. ``icon`` is the navigation mark:
    monochrome, painted by the rail as a mask so it takes the theme's colour the
    way every built-in entry does, which a bitmap cannot. ``logo`` is the
    plugin's identity as it appears on its card, where full-colour artwork is
    right. A plugin may supply either, both, or neither; the card falls back to
    ``icon`` and then to a generic glyph.

    Both are paths relative to the plugin's own directory. The host inlines them
    as data URIs on the manifest, so a plugin carries its identity wherever its
    directory lives -- inside the package, or in a user's
    ``application_plugins`` folder that no static route serves.
    ``description_i18n_key`` localizes the card's description the same way
    ``title_i18n_key`` localizes its name; the untranslated fallback stays the
    plugin's ``extension.yaml`` description.

    ``name_i18n_key`` is the plugin's own product name, which is not the same
    fact as ``title``: the rail's label says what the page is ("Cloud Docs"),
    the name says which plugin provides it ("Co-scribe for Cloud Docs"). A
    plugin that supplies none falls back to its id on the card.
    """

    id: str
    nav_key: str
    title: str
    title_i18n_key: str = ""
    render_mode: str = "iframe"
    component: str = ""
    entrypoint: str = ""
    position: int = 100
    icon: str = ""
    logo: str = ""
    description_i18n_key: str = ""
    name_i18n_key: str = ""


class ApplicationPluginExtension(BaseExtension):
    """Extension contract for a full-stack Jiuwen application plugin."""

    plugin_id: str = ""

    def is_enabled(self) -> bool:
        """Return whether the plugin should be exposed and accept runtime work."""

        return True

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        """Register local RPC methods and connection hooks on ``channel``.

        Frontend-only plugins do not need to override this method.
        Settings handlers may pass ``available_when_disabled=True`` so users
        can re-enable the plugin from its own management component.
        """

        del channel, services

    def websocket_routes(self) -> tuple[WebSocketRouteContribution, ...]:
        return ()

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return ()

    def frontend_asset_root(self) -> Path | None:
        root = self._get_extension_dir()
        if root is None:
            return None
        candidate = root / "frontend" / "dist"
        return candidate if candidate.is_dir() else None

    def resolve_icon(self, icon: str) -> str:
        """Inline a contribution icon as a data URI, or "" when unusable.

        The path is read relative to the plugin's own directory and must stay
        inside it. Anything unreadable, outside, or of an unsupported type
        answers "" so the card falls back to its generic glyph rather than
        rendering a broken image.
        """

        if not isinstance(icon, str) or not icon.strip():
            return ""
        relative = icon.strip().replace("\\", "/")
        if relative.startswith("/") or ".." in PurePosixPath(relative).parts:
            return ""
        mime = ICON_MIME_TYPES.get(PurePosixPath(relative).suffix.lower())
        if mime is None:
            return ""
        root_dir = self._get_extension_dir()
        if root_dir is None:
            return ""
        root = root_dir.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return ""
        if not target.is_file():
            return ""
        try:
            payload = target.read_bytes()
        except OSError:
            return ""
        return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


class ManifestApplicationPlugin(ApplicationPluginExtension):
    """Application plugin described entirely by ``extension.yaml``.

    This is intended for prebuilt iframe applications that do not need a
    Python backend. The loader creates and registers it automatically.
    """

    def __init__(self, root: Path) -> None:
        self.set_extension_dir(root)
        self.plugin_id = self.metadata.id

    async def initialize(self, config: Any) -> None:
        del config

    async def shutdown(self) -> None:
        return None

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        contributions: list[FrontendContribution] = []
        for index, raw in enumerate(self.metadata.frontend):
            render_mode = str(raw.get("render_mode", "iframe")).strip()
            if render_mode != "iframe":
                raise ValueError(
                    "manifest-only application plugins support iframe frontends only"
                )
            entrypoint = str(raw.get("entrypoint", "index.html")).strip()
            if not entrypoint:
                raise ValueError("application plugin frontend entrypoint must not be empty")
            contribution_id = str(raw.get("id") or f"{self.plugin_id}-page").strip()
            nav_key = str(raw.get("nav_key") or f"app:{self.plugin_id}").strip()
            title = str(raw.get("title") or self.metadata.name or self.plugin_id).strip()
            try:
                position = int(raw.get("position", 100 + index))
            except (TypeError, ValueError) as exc:
                raise ValueError("application plugin frontend position must be an integer") from exc
            contributions.append(
                FrontendContribution(
                    id=contribution_id,
                    nav_key=nav_key,
                    title=title,
                    title_i18n_key=str(raw.get("title_i18n_key", "")).strip(),
                    render_mode=render_mode,
                    entrypoint=entrypoint,
                    position=position,
                    icon=str(raw.get("icon", "")).strip(),
                    description_i18n_key=str(
                        raw.get("description_i18n_key", "")
                    ).strip(),
                )
            )
        return tuple(contributions)
