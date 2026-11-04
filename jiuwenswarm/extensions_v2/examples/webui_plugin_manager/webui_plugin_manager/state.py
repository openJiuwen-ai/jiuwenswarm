# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-conversation manager state for the plugin-manager example page."""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import CATALOG, CATALOG_BY_ID, PluginEntry

FILTER_ALL = "all"
FILTER_ENABLED = "enabled"
FILTER_DISABLED = "disabled"
FILTER_UPDATABLE = "updatable"

TAB_PLUGINS = "plugins"
TAB_ABOUT = "about"

NOTICE_INFO = ""
NOTICE_SUCCESS = "success"
NOTICE_WARNING = "warning"


@dataclass(slots=True)
class ManagerState:
    """search, filters, row overrides and the selected detail row.

    The state is activation-local and keyed by the real conversation id. It does
    not read or write the extension manager; the catalog is a demo fixture.
    """

    query: str = ""
    status_filter: str = FILTER_ALL
    tab: str = TAB_PLUGINS
    selected_id: str | None = None
    overrides: dict[str, bool] = field(default_factory=dict)
    removed: set[str] = field(default_factory=set)
    notice: str = ""
    notice_type: str = NOTICE_INFO
    operations: int = 0
    mounted: set[tuple[str, str]] = field(default_factory=set)

    def is_enabled(self, entry: PluginEntry) -> bool:
        """Return the current switch value, including local overrides."""

        return self.overrides.get(entry.plugin_id, entry.enabled)

    def set_enabled(self, plugin_id: str, checked: bool) -> PluginEntry | None:
        """Record a switch change for one catalog row."""

        entry = self.entry(plugin_id)
        if entry is None or entry.incompatible_reason:
            return None
        self.overrides[plugin_id] = checked
        return entry

    def entry(self, plugin_id: str) -> PluginEntry | None:
        """Look up one visible catalog row."""

        if plugin_id in self.removed:
            return None
        return CATALOG_BY_ID.get(plugin_id)

    @property
    def selected(self) -> PluginEntry | None:
        """The selected detail row, or None when it was removed or never set."""

        if not self.selected_id:
            return None
        return self.entry(self.selected_id)

    def installed(self) -> tuple[PluginEntry, ...]:
        """All remaining catalog rows in declared order."""

        return tuple(entry for entry in CATALOG if entry.plugin_id not in self.removed)

    def visible(self) -> tuple[PluginEntry, ...]:
        """Apply the search box and the status filter."""

        query = self.query.strip().casefold()
        rows: list[PluginEntry] = []
        for entry in self.installed():
            enabled = self.is_enabled(entry)
            if self.status_filter == FILTER_ENABLED and not enabled:
                continue
            if self.status_filter == FILTER_DISABLED and enabled:
                continue
            if self.status_filter == FILTER_UPDATABLE and not entry.update_available:
                continue
            if query and not self._matches(entry, query):
                continue
            rows.append(entry)
        return tuple(rows)

    @staticmethod
    def _matches(entry: PluginEntry, query: str) -> bool:
        haystack = " ".join(
            (entry.plugin_id, entry.name, entry.description, entry.author, *entry.tags)
        ).casefold()
        return query in haystack

    def counts(self) -> dict[str, int]:
        """Count installed, enabled, disabled and updatable rows."""

        rows = self.installed()
        enabled = sum(1 for entry in rows if self.is_enabled(entry))
        updates = sum(1 for entry in rows if entry.update_available)
        return {
            "installed": len(rows),
            "enabled": enabled,
            "disabled": len(rows) - enabled,
            "updates": updates,
        }

    def notify(self, message: str, kind: str = NOTICE_INFO) -> None:
        """Record the last operation result shown in the page footer."""

        self.notice = message
        self.notice_type = kind

    def uninstall(self, plugin_id: str) -> PluginEntry | None:
        """Remove one non-builtin row from the demo list."""

        entry = self.entry(plugin_id)
        if entry is None or not entry.can_uninstall:
            return None
        self.removed.add(plugin_id)
        if self.selected_id == plugin_id:
            self.selected_id = None
        return entry

    def set_all(self, checked: bool) -> int:
        """Switch every compatible row and return how many changed."""

        changed = 0
        for entry in self.installed():
            if entry.incompatible_reason or self.is_enabled(entry) == checked:
                continue
            self.overrides[entry.plugin_id] = checked
            changed += 1
        return changed

    def reset_demo(self) -> None:
        """Restore the catalog, filters and overrides to their initial values."""

        self.query = ""
        self.status_filter = FILTER_ALL
        self.tab = TAB_PLUGINS
        self.selected_id = None
        self.overrides.clear()
        self.removed.clear()
        self.notice = "已恢复演示数据 / Demo data restored"
        self.notice_type = NOTICE_SUCCESS
