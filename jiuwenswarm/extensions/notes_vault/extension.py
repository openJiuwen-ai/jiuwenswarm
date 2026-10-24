"""Notes Vault application plugin (extension entry point).

Discovered by ``ExtensionLoader`` because this directory contains both
``extension.yaml`` and ``extension.py``.  The plugin keeps a small per-session
note store on disk and exposes it through Gateway-local RPC.  It registers no
hook callbacks and no WebSocket routes, so it cannot alter chat behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jiuwenswarm.extensions.notes_vault.backend.handlers import (
    VaultState,
    default_storage_dir,
    notes_vault_enabled,
    register_handlers,
)
from jiuwenswarm.extensions.notes_vault.backend.store import MAX_NOTES_PER_SCOPE
from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
    FrontendContribution,
)

#: Plugin identity.  Must match ``id`` in ``extension.yaml`` and the
#: ``applicationPluginId`` exported by ``frontend/index.tsx``.
PLUGIN_ID = "notes-vault"


class NotesVaultPlugin(ApplicationPluginExtension):
    """Per-session scratch notes with on-disk persistence."""

    plugin_id = PLUGIN_ID

    def __init__(self, registry: Any = None) -> None:
        self._registry = registry
        self._state: VaultState | None = None

    def is_enabled(self) -> bool:
        """Notes are on by default; ``NOTES_VAULT_ENABLED=0`` disables them."""
        return notes_vault_enabled()

    async def initialize(self, config: Any) -> None:
        """Resolve the storage directory and build the store.

        Precedence: plugin ``config.yaml`` -> environment variable ->
        ``~/.jiuwenswarm/notes_vault``.  A malformed or missing plugin config
        is not fatal; the plugin falls back to defaults so a typo can never
        take the Gateway down.
        """
        del config
        settings: dict[str, Any] = {}
        try:
            loaded = self._load_config_from_yaml()
            if isinstance(loaded, dict):
                settings = loaded
        except Exception:  # noqa: BLE001
            settings = {}
        self._state = self._build_state(settings)

    def _build_state(self, settings: dict[str, Any]) -> VaultState:
        """Create the vault state from resolved settings."""
        raw_dir = settings.get("storage_dir")
        storage_dir = (
            Path(raw_dir).expanduser()
            if isinstance(raw_dir, str) and raw_dir.strip()
            else default_storage_dir()
        )
        raw_max = settings.get("max_notes")
        max_notes = MAX_NOTES_PER_SCOPE
        if isinstance(raw_max, int) and not isinstance(raw_max, bool):
            max_notes = max(1, min(raw_max, MAX_NOTES_PER_SCOPE))
        return VaultState(storage_dir=storage_dir, max_notes=max_notes)

    def _ensure_state(self) -> VaultState:
        """Return the vault state, creating it lazily if needed."""
        if self._state is None:
            self._state = self._build_state({})
        return self._state

    async def shutdown(self) -> None:
        """Nothing to release: every write is already flushed to disk."""
        return

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        """Register the plugin's RPC methods on the Gateway web channel."""
        del services  # notes are self-contained; no host services required
        register_handlers(channel, self._ensure_state())

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        """Contribute one bundled workspace page."""
        return (
            FrontendContribution(
                id="notes-vault-page",
                nav_key=f"app:{PLUGIN_ID}",
                title="Notes Vault",
                title_i18n_key="applicationPlugins.notesVault.title",
                render_mode="bundled",
                component=PLUGIN_ID,
                position=130,
            ),
        )


async def register_extensions(registry: Any) -> list[NotesVaultPlugin]:
    """Register the plugin. Called by ``ExtensionLoader`` after import."""
    plugin = NotesVaultPlugin(registry)
    registry.register_application_plugin(plugin)
    return [plugin]
