# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP connection state store.

MCP connection state lives in ``<workspace>/mcp/state.json``.
connect/disconnect mutate this file; ``get_mcp_servers`` merges it with
config.yaml's hand-written ``mcp.servers`` so upper layers (adapter /
handlers / registry) read one combined list. The ``enabled`` flag here is
the TUI/global-default switch only — the web channel ignores it and loads
by chat.send's ``mcp`` field instead.

Layout::

    {
      "version": 1,
      "mcp": {
        "<name>": {
          "transport": "sse|stdio|streamable-http",
          "url": "...",            # remote
          "headers": {...},        # ${VAR} placeholders preserved verbatim
          "command": "...",        # stdio
          "args": [...],
          "env": {...},
          "timeout_s": 600,
          "integration_type": "remote-mcp|stdio-mcp|cli",
          "state": "connected|disconnected",
          "enabled": false,        # TUI-only default switch; web ignores
          "server_id_scope": "mcp:<name>",
          "skills": ["..."]        # cli/skill-only
        }
      },
      "mounts": {}
    }

Token values are NEVER stored here — only ``${VAR}`` placeholders. Real
tokens live in ``mcp/credentials/<name>.json`` and are resolved at
``McpServerConfig`` build time via the CredentialStore resolver.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_workspace_dir  # re-export for test patches

logger = logging.getLogger(__name__)

_STATE_VERSION = 1
_STATE_FILE = "state.json"

_MCP_ROOT = "mcp"


def _mcp_root() -> Path:
    """<workspace>/mcp/ — unified MCP data root."""
    return get_workspace_dir() / _MCP_ROOT

# Serialize all state.json mutations; reload reads are concurrent-safe (a
# stale snapshot at worst yields a "not yet connected" result, which a retry
# on the next reload corrects).
_lock = threading.Lock()


def _state_path() -> Path:
    return _mcp_root() / _STATE_FILE


def _ensure_dir() -> Path:
    root = _mcp_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _empty_state() -> dict[str, Any]:
    return {"version": _STATE_VERSION, "mcp": {}, "mounts": {}}


def _load() -> dict[str, Any]:
    """Read state.json; tolerant of BOM and a missing/corrupt file."""
    p = _state_path()
    if not p.is_file():
        return _empty_state()
    try:
        # utf-8-sig tolerates a BOM written by legacy tooling (PowerShell
        # Set-Content -Encoding utf8), matching CredentialStore's behavior.
        with p.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[mcp.state] failed to read %s: %s", p, exc)
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    data.setdefault("version", _STATE_VERSION)
    data.setdefault("mcp", {})
    data.setdefault("mounts", {})
    return data


def _save(state: dict[str, Any]) -> None:
    """Atomically write state.json (no BOM, sorted for diff stability)."""
    _ensure_dir()  # ensure workspace mcp/ dir exists before writing
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:  # utf-8, no BOM
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(p)
    except OSError as exc:
        logger.warning("[mcp.state] failed to write %s: %s", p, exc)


def read_mcp_state() -> dict[str, Any]:
    """Public read for ``get_mcp_servers`` to merge. Returns a fresh dict."""
    with _lock:
        return _load()


def get_mcp_record(name: str) -> dict[str, Any] | None:
    """Return one MCP's state record, or None if absent."""
    n = str(name or "").strip()
    if not n:
        return None
    with _lock:
        return _load().get("mcp", {}).get(n)


def list_connected_mcps() -> list[dict[str, Any]]:
    """All MCPs that should be treated as live (state in {connected, connecting}).

    Both states mean "the MCP has an entry the agent should register / a token
    it should inject / skills it should see":

    * ``connected`` — fully registered, tools available.
    * ``connecting`` — connect in progress (CLI OAuth in flight, or the
      handler hasn't flipped it to connected yet). The entry was persisted so
      a restart re-reads it (connecting is treated like connected for the
      reload/init path), but the frontend must show "connecting" until the
      handler flips it.

    ``registered`` (custom MCP freshly created, never connected) and
    ``disconnected`` are excluded — they have no live MCP server and no
    reason to be registered on restart.

    Used by get_mcp_servers (MCP registration), init, and credential sync.
    For the frontend's "is this connected" check, use
    :func:`list_truly_connected_mcps` (connected only) — ``connecting`` must
    render as "connecting", not "connected".
    """
    with _lock:
        cons = _load().get("mcp", {})
    out: list[dict[str, Any]] = []
    for name, c in cons.items():
        if isinstance(c, dict) and c.get("state") in ("connected", "connecting"):
            rec = dict(c)
            rec["name"] = name
            out.append(rec)
    return out


def list_truly_connected_mcps() -> list[dict[str, Any]]:
    """MCPs with state==connected ONLY (``connecting`` excluded).

    The frontend's "connected" badge reflects only fully-registered MCPs —
    an MCP mid-connect (state==connecting) must show "connecting", so the
    user doesn't see "connected" while apply is still running or may fail.
    Use this for connection_state derivation; use :func:`list_connected_mcps`
    for registration/credential paths that must include connecting MCPs too.
    """
    with _lock:
        cons = _load().get("mcp", {})
    out: list[dict[str, Any]] = []
    for name, c in cons.items():
        if isinstance(c, dict) and c.get("state") == "connected":
            rec = dict(c)
            rec["name"] = name
            out.append(rec)
    return out


def list_connecting_mcps() -> list[dict[str, Any]]:
    """MCPs with state==connecting (connect in progress, not yet connected).

    Drives the frontend's "connecting" badge. Distinct from connected (the
    apply/register is not done) and from registered (the user did initiate
    connect, so a restart should re-register it — list_connected_mcps
    includes both connected and connecting for that reason).
    """
    with _lock:
        cons = _load().get("mcp", {})
    out: list[dict[str, Any]] = []
    for name, c in cons.items():
        if isinstance(c, dict) and c.get("state") == "connecting":
            rec = dict(c)
            rec["name"] = name
            out.append(rec)
    return out


def list_tui_enabled_mcps() -> list[dict[str, Any]]:
    """Live state.json MCPs whose TUI/global-default ``enabled`` flag is True.

    The TUI channel (root adapter) loads the union of config.yaml
    ``enabled`` and these state.json ``enabled=True`` records — its global
    default set. Web ignores ``enabled`` (it loads by chat.send's ``mcp``
    field), so a web-connected MCP with ``enabled=False`` stays out of the
    TUI set until the user enables it there.

    ``state`` in {connected, connecting} means "live entry to register";
    ``registered``/``disconnected`` are excluded (no live server). C/D
    (skill-only / pure-cli) have no MCP host and are filtered out by
    ``record_to_mcp_entry`` at the call site (they surface via skills, and
    the root adapter never scans MCP skill dirs).
    """
    out: list[dict[str, Any]] = []
    for rec in list_connected_mcps():
        if rec.get("enabled") is True:
            out.append(rec)
    return out


def connected_mcp_skill_dirs() -> list[dict[str, str]]:
    """Derive the MCP skill scan dirs from state.json's connected records.

    An MCP's bundled skills surface to the agent only while it is connected
    (state==connected). Deriving the dir list from state.json makes the skill
    scan always reflect the live connection state — disconnect removes the
    state.json record, and the skill disappears on the next scan.

    Returns ``[{"name": <mcp>, "dir": <abs skills dir>}]`` for each connected
    MCP whose ``mcp/skills/<name>/`` dir exists on disk. remote/stdio MCPs
    (no bundled skills) never get a dir written, so they're naturally absent.
    """
    skills_root = _mcp_root() / "skills"
    out: list[dict[str, str]] = []
    for rec in list_connected_mcps():
        name = str(rec.get("name", "") or "").strip()
        if not name:
            continue
        d = skills_root / name
        if d.is_dir():
            out.append({"name": name, "dir": str(d)})
    return out


def list_registered_mcps() -> list[dict[str, Any]]:
    """All custom MCPs with state in {registered, connected}.

    Used by mcp.list so both registered (not yet connected) and connected
    custom MCPs surface — the user needs to see a registered one to click
    connect. Marketplace MCPs aren't returned here (they're discovered by
    scanning the package dir); this only returns custom MCPs whose definition
    lives in state.json because they have no package on disk.
    """
    with _lock:
        cons = _load().get("mcp", {})
    out: list[dict[str, Any]] = []
    for name, c in cons.items():
        if not isinstance(c, dict):
            continue
        st = str(c.get("state", "") or "").strip()
        if st in ("registered", "connected"):
            rec = dict(c)
            rec["name"] = name
            out.append(rec)
    return out


def upsert_mcp_record(
    name: str,
    entry: dict[str, Any],
    *,
    state: str = "connected",
    integration_type: str | None = None,
    skills: list[str] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Insert or update an MCP record. ``entry`` is the build_config_entry
    result (or a custom-mcp dict); its transport/url/headers/command/args/env
    fields are stored verbatim (placeholders preserved for CredentialStore).

    ``enabled`` is the TUI/global-default switch (default False on first
    insert): only the TUI channel reads it to decide its loaded set; web
    ignores it entirely (web loads by chat.send's ``mcp`` field). Pass
    ``enabled=True`` when the TUI creates an MCP (add = immediately on);
    leave False (default) for web-connected MCPs so they don't pollute the
    TUI default set. Pass a non-None value on update to flip it; ``None``
    leaves the existing value untouched.
    """
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
    with _lock:
        st = _load()
        cons = st.setdefault("mcp", {})
        prior = cons.get(n)
        record = dict(prior) if isinstance(prior, dict) else {}
        # First insert defaults enabled=False; later upserts preserve it
        # unless the caller passes an explicit value.
        if "enabled" not in record and prior is None:
            record["enabled"] = False
        # Merge the entry's transport/connection fields (placeholders kept).
        for k in ("transport", "url", "headers", "command", "args", "env",
                  "timeout_s", "server_id_scope"):
            if k in entry:
                record[k] = entry[k]
        record["state"] = state
        if integration_type is not None:
            record["integration_type"] = integration_type
        if skills is not None:
            record["skills"] = list(skills)
        if enabled is not None:
            record["enabled"] = bool(enabled)
        cons[n] = record
        _save(st)
        return dict(record)


def remove_mcp_record(name: str) -> dict[str, Any] | None:
    """Delete an MCP record. Returns the removed record or None."""
    n = str(name or "").strip()
    if not n:
        return None
    with _lock:
        st = _load()
        cons = st.get("mcp", {})
        removed = cons.pop(n, None)
        if removed is not None:
            _save(st)
        return removed


def set_mcp_state(name: str, *, state: str) -> None:
    """Flip an MCP's connection state (e.g. registered -> connected).

    Used by connect_mcp for custom MCPs whose definition already lives in
    state.json — connecting just flips the state, the definition fields stay
    as-is.
    """
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
    with _lock:
        st = _load()
        cons = st.get("mcp", {})
        rec = cons.get(n)
        if not isinstance(rec, dict):
            raise KeyError(f"mcp '{n}' not found in state")
        rec["state"] = str(state or "").strip()
        _save(st)


def record_to_mcp_entry(name: str, record: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a state.json MCP record into an mcp.servers-shaped dict.

    Mirrors the shape build_config_entry produces, so get_mcp_servers can
    merge state.json MCPs into the same list config.yaml uses, and the
    adapter's _build_mcp_server_config consumes them identically.

    Returns None for skill-only MCPs (no transport/url/command — only a
    ``server_id_scope`` + skills). These have no MCP server to register, so
    they must NOT appear in the merged mcp.servers list. Skill-only MCPs
    surface via their bundled skills (SkillManager), not the MCP server
    registry.
    """
    n = str(name or "").strip()
    has_mcp_host = bool(
        record.get("transport")
        or record.get("url")
        or record.get("command")
    )
    if not has_mcp_host:
        return None
    entry: dict[str, Any] = {"name": n}
    transport = record.get("transport") or "streamable-http"
    entry["transport"] = transport
    for k in ("url", "headers", "command", "args", "env",
              "timeout_s", "server_id_scope"):
        if k in record and record[k] is not None:
            entry[k] = record[k]
    # enabled is TUI-only; carry it through so TUI's loader can filter on it.
    # Absent (legacy record) reads as False via .get default at the call site.
    if "enabled" in record:
        entry["enabled"] = bool(record["enabled"])
    return entry


def set_mcp_enabled(name: str, *, enabled: bool) -> None:
    """Flip an MCP's TUI/global-default ``enabled`` flag (connect state stays).

    TUI-only: the web channel never reads ``enabled`` (it loads by chat.send's
    ``mcp`` field). Flipping ``enabled`` off keeps the connection alive (config
    + credentials stay), it only hides the MCP from the TUI default set.
    """
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
    with _lock:
        st = _load()
        cons = st.get("mcp", {})
        rec = cons.get(n)
        if not isinstance(rec, dict):
            raise KeyError(f"mcp '{n}' not found in state")
        rec["enabled"] = bool(enabled)
        _save(st)


__all__ = [
    "read_mcp_state",
    "get_mcp_record",
    "list_connected_mcps",
    "list_truly_connected_mcps",
    "list_connecting_mcps",
    "list_tui_enabled_mcps",
    "connected_mcp_skill_dirs",
    "list_registered_mcps",
    "upsert_mcp_record",
    "remove_mcp_record",
    "set_mcp_state",
    "set_mcp_enabled",
    "record_to_mcp_entry",
]
