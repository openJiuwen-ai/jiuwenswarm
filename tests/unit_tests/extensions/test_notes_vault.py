"""Unit tests for the notes vault application plugin.

Layered the same way as the host diagnostics suite: pure store logic, the
extension registration contract, RPC handlers driven through a fake channel,
and gating verified against the real ``_ApplicationPluginChannel``.

No ``openjiuwen`` and no ``fastapi`` required.
"""

from __future__ import annotations

import itertools
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from jiuwenswarm.extensions.notes_vault import backend as vault_backend
from jiuwenswarm.extensions.notes_vault.backend import handlers
from jiuwenswarm.extensions.notes_vault.backend.store import (
    DEFAULT_SCOPE,
    MAX_BODY_LENGTH,
    MAX_NOTES_PER_SCOPE,
    MAX_TAG_LENGTH,
    MAX_TITLE_LENGTH,
    NotesStore,
    NoteStoreError,
)
from jiuwenswarm.extensions.notes_vault.extension import (
    PLUGIN_ID,
    NotesVaultPlugin,
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

EXTENSION_DIR = Path(vault_backend.__file__).resolve().parent.parent


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
        self.responses.clear()
        await self.methods[method]["handler"](None, "req-1", params, session_id)
        assert len(self.responses) == 1, "handler must answer exactly once"
        return self.responses[0]


class FakeRegistry:
    """Duck-typed stand-in for ``ExtensionRegistry``."""

    def __init__(self) -> None:
        self.plugins: dict[str, Any] = {}

    def register_application_plugin(self, plugin: Any) -> None:
        self.plugins[plugin.plugin_id] = plugin

    def get_application_plugins(self) -> tuple[Any, ...]:
        return tuple(self.plugins.values())


def _clock() -> Any:
    """Return a deterministic, strictly increasing ISO-8601 UTC clock."""
    counter = itertools.count()
    base = datetime(2025, 1, 1, tzinfo=UTC)

    def now() -> str:
        moment = base + timedelta(seconds=next(counter))
        return moment.isoformat().replace("+00:00", "Z")

    return now


@pytest.fixture
def store(tmp_path: Path) -> NotesStore:
    return NotesStore(tmp_path, now=_clock())


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def state(tmp_path: Path) -> handlers.VaultState:
    return handlers.VaultState(storage_dir=tmp_path, max_notes=10)


@pytest.fixture
def bound(state: handlers.VaultState, channel: FakeChannel) -> FakeChannel:
    handlers.register_handlers(channel, state)
    return channel


# ---------------------------------------------------------------------- store


def test_create_and_get_round_trip(store: NotesStore) -> None:
    created = store.create_note("s1", title="First", body="hello", tags=["a", "B"])

    assert created["title"] == "First"
    assert created["body"] == "hello"
    assert created["tags"] == ["a", "B"]
    assert created["pinned"] is False
    assert created["created_at"] == created["updated_at"]
    assert store.get_note("s1", created["id"]) == created


def test_notes_persist_across_store_instances(tmp_path: Path) -> None:
    first = NotesStore(tmp_path, now=_clock())
    created = first.create_note("s1", title="Durable")

    second = NotesStore(tmp_path, now=_clock())

    assert second.get_note("s1", created["id"])["title"] == "Durable"


def test_writes_are_atomic_and_leave_no_temp_file(
    store: NotesStore, tmp_path: Path
) -> None:
    store.create_note("s1", title="Atomic")

    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "s1.json").is_file()


def test_scopes_are_isolated(store: NotesStore) -> None:
    store.create_note("s1", title="alpha")
    store.create_note("s2", title="beta")

    assert [note["title"] for note in store.list_notes("s1")] == ["alpha"]
    assert [note["title"] for note in store.list_notes("s2")] == ["beta"]


def test_missing_scope_falls_back_to_default(store: NotesStore) -> None:
    created = store.create_note(None, title="fallback")

    assert store.get_note(DEFAULT_SCOPE, created["id"])["title"] == "fallback"


@pytest.mark.parametrize(
    "scope",
    ["../escape", "..", "a/b", "a\\b", "a b", "-leading", "a" * 65, "s1\x00"],
)
def test_invalid_scopes_are_rejected(store: NotesStore, scope: str) -> None:
    with pytest.raises(NoteStoreError):
        store.scope_path(scope)


@pytest.mark.parametrize("scope", ["a..b", "s1.."])
def test_dot_dot_scopes_are_rejected(store: NotesStore, scope: str) -> None:
    with pytest.raises(NoteStoreError):
        store.scope_path(scope)


def test_scope_path_stays_inside_base_dir(store: NotesStore, tmp_path: Path) -> None:
    assert store.scope_path("ok-scope").parent == tmp_path.resolve()


def test_blank_scope_normalises_to_default(store: NotesStore) -> None:
    assert store.scope_path("  ").name == f"{DEFAULT_SCOPE}.json"


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("title", [None, "", "   ", 5, "x" * (MAX_TITLE_LENGTH + 1)])
def test_create_rejects_bad_title(store: NotesStore, title: Any) -> None:
    with pytest.raises(NoteStoreError):
        store.create_note("s1", title=title)


@pytest.mark.parametrize("body", [5, "x" * (MAX_BODY_LENGTH + 1)])
def test_create_rejects_bad_body(store: NotesStore, body: Any) -> None:
    with pytest.raises(NoteStoreError):
        store.create_note("s1", title="ok", body=body)


def test_create_allows_empty_body(store: NotesStore) -> None:
    assert store.create_note("s1", title="ok", body="")["body"] == ""


@pytest.mark.parametrize(
    "tags",
    [
        "not-a-list",
        [1, 2],
        ["x" * (MAX_TAG_LENGTH + 1)],
        [f"t{index}" for index in range(20)],
    ],
)
def test_create_rejects_bad_tags(store: NotesStore, tags: Any) -> None:
    with pytest.raises(NoteStoreError):
        store.create_note("s1", title="ok", tags=tags)


def test_create_deduplicates_tags_case_insensitively(store: NotesStore) -> None:
    created = store.create_note("s1", title="ok", tags=["Work", "work", " ", "Home"])

    assert created["tags"] == ["Work", "Home"]


@pytest.mark.parametrize("pinned", ["yes", 1, None])
def test_create_rejects_non_boolean_pinned(store: NotesStore, pinned: Any) -> None:
    with pytest.raises(NoteStoreError):
        store.create_note("s1", title="ok", pinned=pinned)


def test_create_enforces_max_notes(tmp_path: Path) -> None:
    store = NotesStore(tmp_path, now=_clock(), max_notes=2)
    store.create_note("s1", title="one")
    store.create_note("s1", title="two")

    with pytest.raises(NoteStoreError, match="maximum"):
        store.create_note("s1", title="three")


@pytest.mark.parametrize("max_notes", [0, -1, MAX_NOTES_PER_SCOPE + 1, "5", True, 1.5])
def test_store_rejects_bad_max_notes(tmp_path: Path, max_notes: Any) -> None:
    with pytest.raises(NoteStoreError):
        NotesStore(tmp_path, max_notes=max_notes)


# ------------------------------------------------------------------- querying


def test_list_sorts_pinned_first_then_newest(store: NotesStore) -> None:
    first = store.create_note("s1", title="first")
    second = store.create_note("s1", title="second")
    store.update_note("s1", first["id"], pinned=True)

    titles = [note["title"] for note in store.list_notes("s1")]

    assert titles == ["first", "second"]
    assert store.get_note("s1", second["id"])["pinned"] is False


def test_query_matches_title_body_and_tags(store: NotesStore) -> None:
    store.create_note("s1", title="Meeting notes", body="agenda")
    store.create_note("s1", title="Shopping", body="buy MILK")
    store.create_note("s1", title="Random", tags=["meeting"])

    assert len(store.list_notes("s1", query="meeting")) == 2
    assert len(store.list_notes("s1", query="milk")) == 1
    assert store.list_notes("s1", query="nothing") == []


def test_tag_filter_is_exact_and_case_insensitive(store: NotesStore) -> None:
    store.create_note("s1", title="a", tags=["Work"])
    store.create_note("s1", title="b", tags=["workshop"])

    assert [n["title"] for n in store.list_notes("s1", tag="work")] == ["a"]


def test_limit_truncates_results(store: NotesStore) -> None:
    for index in range(4):
        store.create_note("s1", title=f"note-{index}")

    assert len(store.list_notes("s1", limit=2)) == 2
    assert store.list_notes("s1", limit=0) == []


@pytest.mark.parametrize("limit", [-1, "2", 1.5, True])
def test_limit_rejects_bad_values(store: NotesStore, limit: Any) -> None:
    with pytest.raises(NoteStoreError):
        store.list_notes("s1", limit=limit)


# ------------------------------------------------------------------- mutation


def test_update_applies_partial_changes(store: NotesStore) -> None:
    created = store.create_note("s1", title="before", body="old", tags=["x"])

    updated = store.update_note("s1", created["id"], title="after")

    assert updated["title"] == "after"
    assert updated["body"] == "old"
    assert updated["tags"] == ["x"]
    assert updated["updated_at"] > created["updated_at"]


def test_update_can_clear_tags_and_pin(store: NotesStore) -> None:
    created = store.create_note("s1", title="t", tags=["x"])

    updated = store.update_note("s1", created["id"], tags=[], pinned=True)

    assert updated["tags"] == []
    assert updated["pinned"] is True


def test_update_rejects_unknown_note(store: NotesStore) -> None:
    with pytest.raises(NoteStoreError, match="not found"):
        store.update_note("s1", "missing", title="x")


def test_update_requires_a_note_id(store: NotesStore) -> None:
    with pytest.raises(NoteStoreError):
        store.update_note("s1", None, title="x")


def test_delete_removes_the_note(store: NotesStore) -> None:
    created = store.create_note("s1", title="gone")
    store.create_note("s1", title="kept")

    removed = store.delete_note("s1", created["id"])

    assert removed["id"] == created["id"]
    assert [note["title"] for note in store.list_notes("s1")] == ["kept"]


def test_delete_rejects_unknown_note(store: NotesStore) -> None:
    with pytest.raises(NoteStoreError, match="not found"):
        store.delete_note("s1", "missing")


# --------------------------------------------------------------------- misc


def test_stats_reports_counts(store: NotesStore) -> None:
    store.create_note("s1", title="a", tags=["x"])
    second = store.create_note("s1", title="b", tags=["x", "y"])
    store.update_note("s1", second["id"], pinned=True)

    stats = store.stats("s1")

    assert stats == {
        "scope": "s1",
        "notes": 2,
        "pinned": 1,
        "tags": 2,
        "max_notes": MAX_NOTES_PER_SCOPE,
    }


def test_export_markdown_renders_notes(store: NotesStore) -> None:
    created = store.create_note("s1", title="Alpha", body="body text", tags=["t"])
    store.update_note("s1", created["id"], pinned=True)

    markdown = store.export_markdown("s1")

    assert markdown.startswith("# Notes: s1")
    assert "## Alpha (pinned)" in markdown
    assert "body text" in markdown
    assert "- tags: t" in markdown
    assert created["id"] in markdown


def test_export_markdown_handles_empty_scope(store: NotesStore) -> None:
    markdown = store.export_markdown("empty")

    assert markdown.strip() == "# Notes: empty\n\n_No notes yet._".strip()


def test_export_markdown_marks_empty_body(store: NotesStore) -> None:
    store.create_note("s1", title="No body")

    assert "_(empty)_" in store.export_markdown("s1")


def test_corrupt_store_raises_instead_of_losing_data(
    store: NotesStore, tmp_path: Path
) -> None:
    (tmp_path / "s1.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(NoteStoreError, match="unreadable"):
        store.list_notes("s1")


def test_store_with_wrong_shape_raises(store: NotesStore, tmp_path: Path) -> None:
    (tmp_path / "s1.json").write_text(json.dumps(["nope"]), encoding="utf-8")

    with pytest.raises(NoteStoreError, match="invalid shape"):
        store.list_notes("s1")


def test_store_without_notes_key_is_empty(store: NotesStore, tmp_path: Path) -> None:
    (tmp_path / "s1.json").write_text(json.dumps({"version": 1}), encoding="utf-8")

    assert store.list_notes("s1") == []


# ------------------------------------------------------------------- manifest


def test_manifest_matches_plugin_id() -> None:
    manifest = yaml.safe_load((EXTENSION_DIR / "extension.yaml").read_text("utf-8"))
    assert manifest["id"] == PLUGIN_ID


def test_manifest_declares_application_package() -> None:
    manifest = yaml.safe_load((EXTENSION_DIR / "extension.yaml").read_text("utf-8"))
    assert manifest["package_type"] == "application"
    assert manifest["author"] == "zhuyingming0708"
    assert manifest["dependencies"] == {}
    assert manifest["permissions"] == []


def test_manifest_is_next_to_extension_entry() -> None:
    assert (EXTENSION_DIR / "extension.py").is_file()
    assert (EXTENSION_DIR / "extension.yaml").is_file()


def test_metadata_loads_from_manifest() -> None:
    plugin = NotesVaultPlugin()
    assert plugin.metadata.id == PLUGIN_ID
    assert plugin.metadata.name == "Notes Vault"


# ------------------------------------------------------------------ contract


async def test_register_extensions_registers_one_plugin(
    registry: FakeRegistry,
) -> None:
    registered = await register_extensions(registry)

    assert len(registered) == 1
    assert registry.plugins == {PLUGIN_ID: registered[0]}
    assert isinstance(registered[0], ApplicationPluginExtension)
    assert registered[0].plugin_id == PLUGIN_ID


async def test_real_registry_accepts_the_plugin() -> None:
    registry = ExtensionRegistry(
        callback_framework=object(),
        config={},
        logger=logging.getLogger("test"),
    )
    plugin = NotesVaultPlugin(registry)
    registry.register_application_plugin(plugin)

    assert registry.get_application_plugin(PLUGIN_ID) is plugin
    with pytest.raises(ValueError, match="already registered"):
        registry.register_application_plugin(NotesVaultPlugin(registry))


def test_frontend_contribution_is_bundled_page() -> None:
    contributions = NotesVaultPlugin().frontend_contributions()

    assert len(contributions) == 1
    assert contributions[0].render_mode == "bundled"
    assert contributions[0].component == PLUGIN_ID
    assert contributions[0].nav_key == f"app:{PLUGIN_ID}"


def test_websocket_routes_are_not_contributed() -> None:
    assert NotesVaultPlugin().websocket_routes() == ()


def test_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(handlers.ENABLED_ENV, raising=False)
    assert NotesVaultPlugin().is_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "OFF"])
def test_disable_switch(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(handlers.ENABLED_ENV, raw)
    assert NotesVaultPlugin().is_enabled() is False


async def test_shutdown_is_a_noop() -> None:
    assert await NotesVaultPlugin().shutdown() is None


# ------------------------------------------------------------- configuration


def test_build_state_uses_default_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(handlers.STORAGE_DIR_ENV, raising=False)
    plugin = NotesVaultPlugin()

    state = plugin._build_state({})

    assert state.storage_dir == handlers.default_storage_dir()
    assert state.max_notes == MAX_NOTES_PER_SCOPE


def test_build_state_prefers_config_dir(tmp_path: Path) -> None:
    state = NotesVaultPlugin()._build_state({"storage_dir": str(tmp_path)})

    assert state.storage_dir == tmp_path


def test_build_state_ignores_blank_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(handlers.STORAGE_DIR_ENV, str(tmp_path))
    state = NotesVaultPlugin()._build_state({"storage_dir": "   "})

    assert state.storage_dir == tmp_path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(5, 5), (10_000, MAX_NOTES_PER_SCOPE), (-1, 1), ("nope", MAX_NOTES_PER_SCOPE)],
)
def test_build_state_clamps_max_notes(raw: Any, expected: int) -> None:
    state = NotesVaultPlugin()._build_state({"max_notes": raw})

    assert state.max_notes == expected


async def test_initialize_honours_env_storage_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(handlers.STORAGE_DIR_ENV, str(tmp_path))
    plugin = NotesVaultPlugin()

    await plugin.initialize({})

    state = plugin._ensure_state()
    assert state.storage_dir == tmp_path


async def test_initialize_survives_unreadable_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = NotesVaultPlugin()

    def boom() -> dict:
        raise ImportError("yaml missing")

    monkeypatch.setattr(plugin, "_load_config_from_yaml", boom)

    await plugin.initialize({})

    assert plugin._ensure_state().max_notes == MAX_NOTES_PER_SCOPE


def test_default_storage_dir_honours_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(handlers.STORAGE_DIR_ENV, str(tmp_path))

    assert handlers.default_storage_dir() == tmp_path


# ------------------------------------------------------------------- handlers


def test_registers_expected_method_surface(bound: FakeChannel) -> None:
    assert set(bound.methods) == {
        "notes.list",
        "notes.get",
        "notes.create",
        "notes.update",
        "notes.delete",
        "notes.stats",
        "notes.export",
        "notes.settings.get",
        "notes.settings.update",
        "notes.settings.set_enabled",
    }


def test_every_method_is_local_only(bound: FakeChannel) -> None:
    assert all(entry["local_only"] for entry in bound.methods.values())


def test_only_management_methods_survive_disable(bound: FakeChannel) -> None:
    surviving = {
        method
        for method, entry in bound.methods.items()
        if entry["available_when_disabled"]
    }
    assert surviving == set(handlers.MANAGEMENT_METHODS)


async def test_create_then_list_via_rpc(bound: FakeChannel) -> None:
    created = await bound.call(
        "notes.create",
        {"title": "RPC note", "body": "hi", "tags": ["a"]},
        session_id="session-1",
    )
    assert created["ok"] is True
    note_id = created["payload"]["note"]["id"]

    listed = await bound.call("notes.list", {}, session_id="session-1")

    assert listed["payload"]["total"] == 1
    assert listed["payload"]["scope"] == "session-1"
    assert listed["payload"]["notes"][0]["id"] == note_id


async def test_notes_are_scoped_by_session(bound: FakeChannel) -> None:
    await bound.call("notes.create", {"title": "mine"}, session_id="session-1")

    other = await bound.call("notes.list", {}, session_id="session-2")

    assert other["payload"]["total"] == 0
    assert other["payload"]["scope"] == "session-2"


async def test_get_update_delete_via_rpc(bound: FakeChannel) -> None:
    created = await bound.call("notes.create", {"title": "original"}, session_id="s1")
    note_id = created["payload"]["note"]["id"]

    fetched = await bound.call("notes.get", {"note_id": note_id}, session_id="s1")
    assert fetched["payload"]["note"]["title"] == "original"

    updated = await bound.call(
        "notes.update",
        {"note_id": note_id, "title": "renamed", "pinned": True},
        session_id="s1",
    )
    assert updated["payload"]["note"]["title"] == "renamed"
    assert updated["payload"]["note"]["pinned"] is True

    deleted = await bound.call("notes.delete", {"note_id": note_id}, session_id="s1")
    assert deleted["payload"]["note"]["id"] == note_id

    missing = await bound.call("notes.get", {"note_id": note_id}, session_id="s1")
    assert missing["ok"] is False
    assert missing["code"] == "BAD_REQUEST"


async def test_list_supports_query_and_limit(bound: FakeChannel) -> None:
    for title in ("alpha", "beta", "gamma"):
        await bound.call("notes.create", {"title": title}, session_id="s1")

    filtered = await bound.call(
        "notes.list", {"query": "a", "limit": 1}, session_id="s1"
    )

    assert filtered["payload"]["total"] == 1


async def test_stats_and_export_via_rpc(bound: FakeChannel) -> None:
    await bound.call("notes.create", {"title": "doc", "body": "text"}, session_id="s1")

    stats = await bound.call("notes.stats", {}, session_id="s1")
    assert stats["payload"]["notes"] == 1

    exported = await bound.call("notes.export", {}, session_id="s1")
    assert "## doc" in exported["payload"]["markdown"]


async def test_export_uses_default_scope_without_session(bound: FakeChannel) -> None:
    await bound.call("notes.create", {"title": "orphan"})

    exported = await bound.call("notes.export")

    assert exported["payload"]["markdown"].startswith(f"# Notes: {DEFAULT_SCOPE}")


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"title": ""},
        {"title": 5},
        {"title": "ok", "tags": "nope"},
        {"title": "ok", "pinned": "yes"},
        {"title": "x" * (MAX_TITLE_LENGTH + 1)},
    ],
)
async def test_create_rejects_bad_params(bound: FakeChannel, params: Any) -> None:
    response = await bound.call("notes.create", params, session_id="s1")

    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"


async def test_non_object_params_are_rejected(bound: FakeChannel) -> None:
    response = await bound.call("notes.list", "not-an-object", session_id="s1")

    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"


async def test_settings_get_returns_state(
    bound: FakeChannel, state: handlers.VaultState
) -> None:
    response = await bound.call("notes.settings.get")

    assert response["payload"]["storage_dir"] == str(state.storage_dir)
    assert response["payload"]["max_notes"] == 10


async def test_settings_update_rebuilds_store(
    bound: FakeChannel, state: handlers.VaultState
) -> None:
    response = await bound.call("notes.settings.update", {"values": {"max_notes": 3}})

    assert response["ok"] is True
    assert response["payload"]["max_notes"] == 3
    assert state.store._max_notes == 3


@pytest.mark.parametrize(
    "values",
    [{"unknown": 1}, {"max_notes": 0}, {"max_notes": "3"}, {"max_notes": True}, "nope"],
)
async def test_settings_update_rejects_bad_values(
    bound: FakeChannel, values: Any
) -> None:
    response = await bound.call("notes.settings.update", {"values": values})

    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"


async def test_settings_set_enabled_round_trip(
    bound: FakeChannel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(handlers.ENABLED_ENV, raising=False)

    off = await bound.call("notes.settings.set_enabled", {"enabled": False})
    assert off["payload"]["enabled"] is False

    on = await bound.call("notes.settings.set_enabled", {"enabled": True})
    assert on["payload"]["enabled"] is True


async def test_settings_set_enabled_requires_boolean(bound: FakeChannel) -> None:
    response = await bound.call("notes.settings.set_enabled", {"enabled": "yes"})

    assert response["ok"] is False
    assert response["code"] == "BAD_REQUEST"


async def test_handlers_never_raise_into_the_channel(
    bound: FakeChannel, state: handlers.VaultState, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(state.store, "list_notes", boom)

    response = await bound.call("notes.list", {}, session_id="s1")

    assert response["ok"] is False
    assert response["code"] == "INTERNAL_ERROR"
    assert "store exploded" in response["error"]


# --------------------------------------------------------------------- gating


async def test_data_methods_are_gated_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(handlers.ENABLED_ENV, "0")
    plugin = NotesVaultPlugin()
    channel = FakeChannel()
    plugin.bind_web_channel(
        _ApplicationPluginChannel(channel, plugin), ApplicationPluginServices()
    )

    response = await channel.call("notes.list")

    assert response["ok"] is False
    assert response["code"] == "APPLICATION_PLUGIN_DISABLED"
    assert PLUGIN_ID in response["error"]


async def test_management_methods_answer_while_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(handlers.ENABLED_ENV, "0")
    monkeypatch.setenv(handlers.STORAGE_DIR_ENV, str(tmp_path))
    plugin = NotesVaultPlugin()
    channel = FakeChannel()
    plugin.bind_web_channel(
        _ApplicationPluginChannel(channel, plugin), ApplicationPluginServices()
    )

    response = await channel.call("notes.settings.get")

    assert response["ok"] is True
    assert response["payload"]["enabled"] is False
