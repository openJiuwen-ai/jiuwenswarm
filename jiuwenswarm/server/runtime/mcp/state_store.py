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
          "managed": "env",        # provenance mark: admin env-preset channel
          "skills": ["..."]        # cli/skill-only
        }
      },
      "mounts": {},
      "preset": {"ts": 0, "hash": "..."}  # admin env-preset fence (applied ts + content hash)
    }

Token values are NEVER stored here — only ``${VAR}`` placeholders. Real
tokens live in ``mcp/credentials/<name>.json`` and are resolved at
``McpServerConfig`` build time via the CredentialStore resolver.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_workspace_dir

logger = logging.getLogger(__name__)


def get_workspace_dir() -> Path:
    """MCP 数据根锚点：租户感知的 agent workspace（保留旧名供测试 patch）。

    个人版即全局 ``~/.jiuwenswarm/agent/workspace``；企业版随 ``TenantContext``
    解析到 ``workspace_{tenant_key}``，state.json 按租户隔离。
    """
    return get_agent_workspace_dir()

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


def _upsert_locked(
    st: dict[str, Any],
    name: str,
    entry: dict[str, Any],
    *,
    state: str,
    integration_type: str | None = None,
    skills: list[str] | None = None,
    enabled: bool | None = None,
    managed: str | None = None,
) -> dict[str, Any]:
    """Lock-free record upsert on an already-loaded state dict.

    Caller must hold ``_lock`` and is responsible for the final ``_save``.
    Mirrors the historical ``upsert_mcp_record`` merge exactly (entry fields
    stored verbatim, placeholders preserved for CredentialStore).
    """
    n = str(name or "").strip()
    if not n:
        raise ValueError("mcp name is required")
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
    if managed is not None:
        record["managed"] = managed
    cons[n] = record
    return dict(record)


def upsert_mcp_record(
    name: str,
    entry: dict[str, Any],
    *,
    state: str = "connected",
    integration_type: str | None = None,
    skills: list[str] | None = None,
    enabled: bool | None = None,
    managed: str | None = None,
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

    ``managed`` is the provenance mark (``"env"`` = admin env-preset
    channel, see :func:`apply_managed_preset`); absent/``None`` keeps the
    record a plain user-created one.
    """
    with _lock:
        st = _load()
        record = _upsert_locked(
            st,
            name,
            entry,
            state=state,
            integration_type=integration_type,
            skills=skills,
            enabled=enabled,
            managed=managed,
        )
        _save(st)
        return record


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


def _preset_hash(entries: dict[str, dict[str, Any]]) -> str:
    """Stable short hash of a preset payload (for stale-content warnings)."""
    canonical = json.dumps(entries, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def apply_managed_preset(
    ts: int,
    entries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Reconcile the admin env preset into state.json behind a ts fence.

    ``ts`` is the management-plane timestamp (epoch ms) carried by the env
    channel; the top-level ``preset.ts`` records what this workspace already
    applied. Fence semantics are whole-set and all-or-nothing:

    * ``ts <= preset.ts`` → skip entirely: no write, no removal, no restore
      of user-deleted records. A stale pod generation during a rolling
      rebuild therefore can never write an older preset back, and user
      deletions between pushes are respected. Content that changed without
      a ts bump only logs a warning — refreshing ``ts`` is the admin
      contract.
    * ``ts > preset.ts`` → full overwrite of the managed set: records
      marked ``managed`` that are no longer in ``entries`` are removed
      (their names come back in ``removed`` so the caller can best-effort
      drop their credentials); each surviving entry is remove-then-inserted
      so dropped fields don't survive the merge. A same-name USER record
      (no ``managed`` mark) always wins and is left untouched.

    Runs atomically under ``_lock`` and must execute inside the tenant
    context (the workspace anchor); callers without one must not invoke it.
    Returns ``{"applied": bool, "removed": [names], "skipped_user": [names]}``.
    """
    fence_ts = int(ts)
    with _lock:
        st = _load()
        prior_preset = st.get("preset")
        prior_preset = prior_preset if isinstance(prior_preset, dict) else {}
        applied_ts = int(prior_preset.get("ts") or 0)
        payload_hash = _preset_hash(entries)
        if fence_ts <= applied_ts:
            if prior_preset.get("hash") not in (None, payload_hash):
                logger.warning(
                    "[mcp.state] preset content changed without a ts bump "
                    "(applied_ts=%s, incoming_ts=%s); skipping — refresh ts "
                    "to apply",
                    applied_ts,
                    fence_ts,
                )
            return {"applied": False, "removed": [], "skipped_user": []}
        cons = st.setdefault("mcp", {})
        removed: list[str] = []
        skipped_user: list[str] = []
        for name in list(cons.keys()):
            rec = cons[name]
            if not (isinstance(rec, dict) and rec.get("managed")):
                continue
            if name in entries:
                continue
            del cons[name]
            removed.append(name)
        for name in sorted(entries):
            entry = entries[name]
            prior = cons.get(name)
            if isinstance(prior, dict) and not prior.get("managed"):
                # A user-created record with the same name wins; the admin
                # preset never clobbers user data.
                skipped_user.append(name)
                logger.warning(
                    "[mcp.state] preset entry '%s' shadowed by a user record; "
                    "skipping",
                    name,
                )
                continue
            # Remove-then-insert so fields dropped from the new payload do
            # not survive the upsert merge (fresh record, enabled=False).
            cons.pop(name, None)
            transport = str(entry.get("transport", "") or "")
            _upsert_locked(
                st,
                name,
                entry,
                state="connected",
                integration_type=(
                    "stdio-mcp" if transport == "stdio" else "remote-mcp"
                ),
                managed="env",
            )
        st["preset"] = {"ts": fence_ts, "hash": payload_hash}
        _save(st)
        return {
            "applied": True,
            "removed": sorted(removed),
            "skipped_user": sorted(skipped_user),
        }


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
    "apply_managed_preset",
    "set_mcp_state",
    "set_mcp_enabled",
    "record_to_mcp_entry",
]


# --------------------------------------------------------------------------- #
# common 注入点自注册：import 即注册，供 common 经
# ``common.external_providers.provider("mcp_state")`` 反向取用（分层规则 R1）。
# --------------------------------------------------------------------------- #
def _register_common_provider() -> None:
    import sys

    from e2a.provider_domains import PROVIDER_DOMAIN_MCP_STATE

    from jiuwenswarm.common.external_providers import register

    register(PROVIDER_DOMAIN_MCP_STATE, sys.modules[__name__])


_register_common_provider()
