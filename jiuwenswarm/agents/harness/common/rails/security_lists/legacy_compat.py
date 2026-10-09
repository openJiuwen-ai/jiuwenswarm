# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Flat legacy rule APIs backed by the canonical user records.

Control switches/defaults and remembered approvals retain their own contracts.
Legacy whole-list saves only replace records representable by that API; advanced,
disabled and cloud records must never disappear on a legacy get/save round trip.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import (
    DuplicateRecordError, SecurityListRecord, new_record_id, record_to_dict,
    resolve_cell, utc_now_iso, validate_record,
)

MARKER = "legacy_rule_adapters_v1"


def _path_records(paths: list[dict], *, enabled: bool = True) -> list[SecurityListRecord]:
    from jiuwenswarm.common.file_guard_config import validate_paths

    records = []
    for row in validate_paths(paths):
        rec = SecurityListRecord(
            type="file_path", pattern=row["path"], match=row.get("match", "prefix"),
            enabled=enabled, cells={"*": {k: row[k] for k in ("read", "write", "exec") if k in row}},
            migrated_from="file_guard",
        )
        validate_record(rec)
        records.append(rec)
    return records


def _domain_records(urls: dict, *, enabled: bool = True) -> list[SecurityListRecord]:
    if not isinstance(urls, dict):
        raise ValueError("net_guard.urls must be an object")
    records = []
    for pattern, value in urls.items():
        if not isinstance(pattern, str):
            raise ValueError("net_guard patterns must be strings")
        action = value.get("action", value.get("fetch")) if isinstance(value, dict) else value
        if action not in ("allow", "ask", "deny"):
            raise ValueError(f"Invalid domain action: {pattern}")
        rec = SecurityListRecord(
            type="domain", pattern=pattern.strip(),
            match="wildcard" if pattern.strip().startswith("*.") else "exact",
            enabled=enabled, cells={"*": {"*": action}}, migrated_from="net_guard",
        )
        validate_record(rec)  # Unsupported URL/glob syntax is never silently discarded.
        records.append(rec)
    return records


def bootstrap(data: dict, section: dict) -> None:
    """Move old editable lists inside the caller's config transaction, once.

    Keep an immutable rollback snapshot. Mode-stamped/remembered path entries
    stay in the approval channel, rather than losing their approval priority.
    """
    from . import store

    migrations = section.setdefault("migrations", {})
    if not isinstance(migrations, dict):
        raise ValueError("security_lists.migrations must be an object")
    if migrations.get(MARKER):
        return
    perms = data.get("permissions") or {}
    existing = store._parse_existing_user(section)
    occupied = {(r.type, r.pattern, r.match): r for r in existing}
    backup = {}
    now = utc_now_iso()
    for name, key, builder in (("file_guard", "paths", _path_records),
                               ("net_guard", "urls", _domain_records)):
        guard = perms.get(name)
        if not isinstance(guard, dict) or key not in guard:
            continue
        raw = guard[key]
        if name == "file_guard":
            if not isinstance(raw, list):
                raise ValueError("file_guard.paths must be a list")
            # Approval persistence writes mode/created_at; those are not panel rules.
            editable = [r for r in raw if isinstance(r, dict) and not (r.get("mode") or r.get("created_at"))]
            approvals = [r for r in raw if r not in editable]
            if any(not isinstance(r, dict) for r in approvals):
                raise ValueError("file_guard.paths entries must be objects")
            candidates = builder(editable, enabled=guard.get("enabled") is not False)
        else:
            candidates = builder(raw, enabled=guard.get("enabled") is not False)
        backup[name] = deepcopy(raw)
        for rec in candidates:
            old = occupied.get((rec.type, rec.pattern, rec.match))
            if old is not None:
                if old.migrated_from != name and old.cells != rec.cells:
                    raise DuplicateRecordError(f"Legacy rule conflicts with canonical record {old.id}; use security_lists")
                continue
            rec.id = new_record_id()
            rec.created_at = rec.updated_at = now
            validate_record(rec, existing=existing)
            existing.append(rec)
            occupied[(rec.type, rec.pattern, rec.match)] = rec
            section["user"].append(record_to_dict(rec))
        guard[key] = approvals if name == "file_guard" else {}
    migrations[MARKER] = {"at": now, "backup": backup}


def _editable(rec: SecurityListRecord) -> bool:
    if rec.source != "user" or (not rec.enabled and rec.migrated_from not in ("file_guard", "net_guard")):
        return False
    if set(rec.cells) - {"*"} or not rec.cells.get("*"):
        return False
    if rec.type == "file_path":
        # The old file API implies deny on all axes when read is denied.
        if resolve_cell(rec.cells, "*", "read") == "deny":
            return all(resolve_cell(rec.cells, "*", op) == "deny" for op in ("write", "exec"))
    return rec.type in ("file_path", "domain")


def _flat(rec: SecurityListRecord) -> Any:
    if rec.type == "domain":
        return resolve_cell(rec.cells, "*", "*")
    row = {"path": rec.pattern}
    if rec.match != "prefix":
        row["match"] = rec.match
    for op in ("read", "write", "exec"):
        action = resolve_cell(rec.cells, "*", op)
        if action is not None:
            row[op] = action
    return row


def guard_view(name: str, *, data: dict | None = None, owned_only: bool = False) -> dict:
    """Legacy editable view. Read-only records are reported outside its flat list."""
    from jiuwenswarm.common.config import get_config
    from . import store

    cfg = get_config() if data is None else data
    guard = deepcopy((cfg.get("permissions") or {}).get(name) or {})
    section = store._parse_section(cfg.get("security_lists", {}))
    list_type, field = ("file_path", "paths") if name == "file_guard" else ("domain", "urls")
    records = [r for r in section["user"] if r.type == list_type]
    has_marker = bool((cfg.get("security_lists") or {}).get("migrations", {}).get(MARKER))
    # Before the first write, read without mutating configuration.
    if not has_marker:
        old = guard.get(field, [] if field == "paths" else {})
        if field == "paths":
            old = [r for r in old if not (isinstance(r, dict) and (r.get("mode") or r.get("created_at")))]
            projected = _path_records(old, enabled=guard.get("enabled") is not False)
        else:
            projected = _domain_records(old, enabled=guard.get("enabled") is not False)
        occupied = {(r.type, r.pattern, r.match) for r in records}
        records += [r for r in projected if (r.type, r.pattern, r.match) not in occupied]
    editable = [r for r in records if _editable(r)]
    if owned_only:
        editable = [r for r in editable if r.migrated_from == name and r.enabled]
    guard[field] = ({r.pattern: _flat(r) for r in editable} if field == "urls"
                    else [_flat(r) for r in editable])
    readonly = [r for r in records if not _editable(r)]
    readonly += [r for r in section["cloud"]["records"] if r.type == list_type]
    if readonly:
        guard["readonly_rules"] = [record_to_dict(r) for r in readonly]
    return guard


def replace_flat(data: dict, section: dict, name: str, value: Any) -> None:
    """Replace the representable user subset, preserving ids and hidden records."""
    from . import store

    existing = store._parse_existing_user(section)
    list_type = "file_path" if name == "file_guard" else "domain"
    builder = _path_records if name == "file_guard" else _domain_records
    incoming = builder(value)
    current = {(r.type, r.pattern, r.match): r for r in existing}
    kept = [r for r in existing if r.type != list_type or not _editable(r)]
    now = utc_now_iso()
    for rec in incoming:
        old = current.get((rec.type, rec.pattern, rec.match))
        if old is not None and not _editable(old):
            raise DuplicateRecordError(f"Record {old.id} cannot be edited through the legacy API; use security_lists")
        if old is not None:
            if _flat(old) == _flat(rec):
                kept.append(old)  # get/save must not rewrite metadata or cells.
                continue
            rec.id, rec.created_at, rec.enabled = old.id, old.created_at, old.enabled
            rec.note, rec.migrated_from = old.note, old.migrated_from
        else:
            rec.id, rec.created_at = new_record_id(), now
            guard = (data.get("permissions") or {}).get(name) or {}
            rec.enabled = guard.get("enabled") is not False
        rec.updated_at = now
        validate_record(rec, existing=kept)
        kept.append(rec)
    section["user"] = [record_to_dict(r) for r in kept]


def update_guard(name: str, patch: dict) -> dict:
    """Atomically update controls and canonical rules. Caller validates controls."""
    from jiuwenswarm.common.config import update_config
    from . import store

    field = "paths" if name == "file_guard" else "urls"
    result = []

    def mutate(data):
        section = store._ensure_section(data)
        guard = data.setdefault("permissions", {}).setdefault(name, {})
        if name == "net_guard":
            guard.setdefault("enabled", True)
            guard.setdefault("defaults", "allow")
        for key, value in patch.items():
            if key == field:
                continue
            if key in ("defaults", "workspace") and isinstance(value, dict):
                guard.setdefault(key, {}).update(deepcopy(value))
                if guard[key].get("read") == "deny" and guard[key].get("write") == "allow":
                    raise ValueError(f"{key}: write allow requires read allow")
            else:
                guard[key] = deepcopy(value)
        if "enabled" in patch:
            for raw in section["user"]:
                if raw.get("migrated_from") == name:
                    raw["enabled"] = patch["enabled"]
        if field in patch:
            replace_flat(data, section, name, patch[field])
        result.append(guard_view(name, data=data))
        return data

    update_config(mutate)
    from . import audit
    audit.log_event(audit.AUDIT_CHANGE, op=f"legacy.{name}.update", fields=list(patch), source="user")
    return result[0]


def file_rules_for_enforcement(permissions: dict, *, mode: str) -> dict:
    """Generate the old engine's path input, not another persisted rule list."""
    from . import store

    guard = permissions.get("file_guard")
    if not isinstance(guard, dict) or guard.get("enabled") is False:
        return permissions
    lists = store.get_security_lists()
    rows = deepcopy(guard.get("paths") or [])  # approval channel and pre-migration rules
    for rec in lists["user"] + lists["cloud"]["records"]:
        if rec.type != "file_path" or not rec.enabled:
            continue
        row = {"path": rec.pattern, "match": rec.match}
        for op in ("read", "write", "exec"):
            action = resolve_cell(rec.cells, mode, op)
            if action is not None:
                row[op] = action
        if len(row) > 2:
            rows.append(row)
    merged = dict(guard)
    merged["paths"] = rows
    return {**permissions, "file_guard": merged}
