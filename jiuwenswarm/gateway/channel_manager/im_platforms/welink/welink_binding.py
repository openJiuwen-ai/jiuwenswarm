# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared WeLink reader authorization and isolated colleague assistant bindings."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

_BINDING_KEYS = (
    "persistent_agent_id", "assistant_credential_id", "assistant_secret",
    "credential_dir", "assistant_account", "enabled", "config_id",
)


def normalize_welink_section(current: Mapping[str, Any] | None) -> dict[str, Any]:
    """Migrate the legacy slot and validate the shared reader plus assistant bindings."""
    original = dict(current or {})
    section = {key: value for key, value in original.items() if key not in _BINDING_KEYS and key != "agent_id"}
    section.setdefault("reader_credential_dir", str(original.get("credential_dir") or ""))
    section.setdefault("cli_authorized", False)
    section.setdefault("cli_path", "welink-cli")
    section.setdefault("env", "pro")
    section.setdefault("poll_interval_seconds", 4)
    try:
        interval = float(section["poll_interval_seconds"])
    except (TypeError, ValueError) as exc:
        raise ValueError("WeLink poll_interval_seconds must be a positive finite number") from exc
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("WeLink poll_interval_seconds must be a positive finite number")
    raw = original.get("bindings")
    if raw is None:
        agent_id = str(original.get("persistent_agent_id") or original.get("agent_id") or "").strip()
        raw = [{**{key: original[key] for key in _BINDING_KEYS if key in original},
                "persistent_agent_id": agent_id, "legacy_identity_fallback": True}] if agent_id else []
    if not isinstance(raw, list):
        raise ValueError("WeLink bindings must be a list")
    bindings = []
    agents, credentials = set(), set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("WeLink binding must be an object")
        entry = dict(item)
        agent = str(entry.get("persistent_agent_id") or "").strip()
        credential = str(entry.get("assistant_credential_id") or "").strip()
        if not agent or agent in agents:
            raise ValueError("WeLink requires a unique persistent_agent_id for every binding")
        if credential and credential in credentials:
            raise ValueError("WeLink assistant credential is already bound to another colleague")
        agents.add(agent)
        if credential:
            credentials.add(credential)
        entry["persistent_agent_id"] = agent
        entry["assistant_credential_id"] = credential
        entry.setdefault("credential_dir", "")
        entry["enabled"] = bool(original.get("enabled", True) and entry.get("enabled", True) and _cli_ready(section)
                                and str(entry.get("credential_dir") or "").strip()
                                and credential and str(entry.get("assistant_secret") or "").strip())
        bindings.append(entry)
    section["bindings"] = bindings
    section["enabled"] = any(item["enabled"] for item in bindings)
    return section


def _cli_ready(section: Mapping[str, Any]) -> bool:
    return bool(section.get("cli_authorized") and str(section.get("account") or "").strip()
                and str(section.get("reader_credential_dir") or "").strip())


def apply_welink_authorization(current: Mapping[str, Any] | None, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Merge one colleague's authorization without replacing any other binding."""
    merged = normalize_welink_section(current)
    account = str(patch.get("account") or "").strip()
    if account and merged.get("account") and account.casefold() != str(merged["account"]).casefold():
        # A new employee scan replaces the shared reader. Assistant setup cannot.
        if not patch.get("cli_authorized"):
            raise ValueError("WeLink account must match the shared reader account")
        merged["account"] = account
    for key, value in patch.items():
        if key not in _BINDING_KEYS and key not in ("bindings", "account"):
            merged[key] = value
    if account and not merged.get("account"):
        merged["account"] = account
    merged["enabled"] = True
    agent = str(patch.get("persistent_agent_id") or "").strip()
    # Legacy CLI callers still provide credential_dir for the shared login.
    if patch.get("cli_authorized") and "reader_credential_dir" not in patch and patch.get("credential_dir"):
        merged["reader_credential_dir"] = patch["credential_dir"]
    if agent:
        config_id = str(patch.get("config_id") or "").strip()
        entry = next((item for item in merged["bindings"] if item["persistent_agent_id"] == agent), None)
        if entry is None and config_id:
            # The office agent still holds this colleague's legacy binding. Saving moves it
            # onto the connection agent instead of adding a second copy of the same AK.
            entry = next((item for item in merged["bindings"]
                          if item["persistent_agent_id"] == config_id or str(item.get("config_id") or "") == config_id), None)
        if entry is None:
            entry = {"persistent_agent_id": agent}
            merged["bindings"].append(entry)
        previous_credential = str(entry.get("assistant_credential_id") or "")
        incoming_credential = str(patch.get("assistant_credential_id") or "")
        entry.update({key: value for key, value in patch.items() if key in _BINDING_KEYS})
        # A new AK is a different WeLink assistant. Keep the learned member id only for the same credential.
        if (incoming_credential and previous_credential and incoming_credential != previous_credential
                and "assistant_account" not in patch):
            entry["assistant_account"] = ""
        entry["enabled"] = patch.get("enabled", True)
    # Completing the shared login activates every complete assistant.
    if patch.get("cli_authorized"):
        for entry in merged["bindings"]:
            entry["enabled"] = True
    return normalize_welink_section(merged)


def revoke_welink_authorization(current: Mapping[str, Any] | None, agent_id: str) -> dict[str, Any]:
    """Remove one assistant while preserving the reader and other assistants."""
    section = normalize_welink_section(current)
    section["bindings"] = [
        item for item in section["bindings"]
        if item["persistent_agent_id"] != agent_id and item.get("config_id") != agent_id
    ]
    section["enabled"] = True
    return normalize_welink_section(section)


def reveal_new_assistant_credential(credential_id: str, secret: str) -> dict[str, str]:
    """AK/SK for the person who just created the assistant. Shown once, then not returned again."""
    credential = credential_id.strip()
    stored_secret = secret.strip()
    if not credential or not stored_secret:
        return {}
    return {
        "assistant_credential_id": credential,
        "assistant_secret": stored_secret,
    }


def public_welink_authorization(conf: Mapping[str, Any] | None, agent_id: str | None = None) -> dict[str, Any]:
    """Return only allowlisted public fields, including each colleague's readiness."""
    section = normalize_welink_section(conf)
    cli_ready = _cli_ready(section)
    bindings = []
    for entry in section["bindings"]:
        bindings.append({
            "persistent_agent_id": entry["persistent_agent_id"],
            "config_id": str(entry.get("config_id") or ""),
            "assistant_credential_id": entry["assistant_credential_id"],
            "assistant_account": str(entry.get("assistant_account") or ""),
            "cli_ready": cli_ready,
            "assistant_ready": bool(entry["assistant_credential_id"] and entry.get("assistant_secret")),
            "channel_enabled": entry["enabled"],
        })
    selected = next((item for item in bindings if item["persistent_agent_id"] == agent_id or item["config_id"] == agent_id), None)
    if agent_id is None and len(bindings) == 1:
        selected = bindings[0]
    return {
        "cli_ready": cli_ready,
        "assistant_ready": False,
        "channel_enabled": False,
        "persistent_agent_id": agent_id or "",
        "assistant_credential_id": "",
        "assistant_account": "",
        **(selected or {}),
        "account": str(section.get("account") or ""),
        "bindings": bindings,
        "agent_ids": [item["persistent_agent_id"] for item in bindings if item["channel_enabled"]],
    }


def manual_assistant_patch(*, agent_id: str, account: str, credential_id: str, secret: str) -> dict[str, str]:
    """Build the stored patch for an assistant AK/SK typed by the user."""
    agent = agent_id.strip()
    bound_account = account.strip()
    credential = credential_id.strip()
    stored_secret = secret.strip()
    if not agent or not bound_account or not credential or not stored_secret:
        raise ValueError("account, assistant_credential_id and assistant_secret are required")
    return {
        "persistent_agent_id": agent,
        "account": bound_account,
        "assistant_credential_id": credential,
        "assistant_secret": stored_secret,
    }


def assign_learned_assistant_account(section: dict[str, Any], *, agent_id: str, credential_id: str, account: str) -> bool:
    """Record one learned group member id. True when this binding owns it afterwards.

    A different existing value is left untouched. An id already owned by another
    colleague or another credential is refused.
    """
    learned = account.strip()
    agent = agent_id.strip()
    credential = credential_id.strip()
    if not learned or not agent or not credential or not isinstance(section, dict):
        return False
    bindings = section.get("bindings")
    entries = [item for item in bindings if isinstance(item, dict)] if isinstance(bindings, list) else [section]
    for item in entries:
        if str(item.get("assistant_account") or "") != learned:
            continue
        owner = str(item.get("persistent_agent_id") or item.get("agent_id") or "")
        owner_credential = str(item.get("assistant_credential_id") or "")
        if owner != agent or owner_credential != credential:
            return False
    for item in entries:
        owner = str(item.get("persistent_agent_id") or item.get("agent_id") or "")
        owner_credential = str(item.get("assistant_credential_id") or "")
        if owner != agent or owner_credential != credential:
            continue
        existing = str(item.get("assistant_account") or "")
        if existing == learned:
            return True
        if existing:
            return False
        item["assistant_account"] = learned
        return True
    return False


def learn_assistant_account(agent_id: str, credential_id: str, account: str) -> bool:
    """Persist a learned member id for the credential that sent the message."""
    owned = False

    def save(data):
        nonlocal owned
        channels = data.setdefault("channels", {})
        section = channels.get("welink")
        if isinstance(section, dict):
            owned = assign_learned_assistant_account(
                section, agent_id=agent_id, credential_id=credential_id, account=account,
            )
        return data

    from jiuwenswarm.common.config import update_config

    update_config(save)
    return owned


def load_welink_section() -> dict[str, Any]:
    from jiuwenswarm.common.config import CONFIG_YAML_PATH, load_yaml_round_trip

    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    channels = data.get("channels") if isinstance(data, dict) else None
    welink = channels.get("welink") if isinstance(channels, dict) else None
    return dict(welink) if isinstance(welink, dict) else {}


def save_welink_authorization(patch: Mapping[str, Any], current: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one step and write it. Returns the public view and the stored section."""
    from jiuwenswarm.common.config import update_config

    def save(data):
        channels = data.setdefault("channels", {})
        previous = current if current is not None else channels.get("welink")
        channels["welink"] = apply_welink_authorization(previous, patch)
        return data

    data = update_config(save)
    merged = dict(data["channels"]["welink"])
    return public_welink_authorization(merged, str(patch.get("config_id") or patch.get("persistent_agent_id") or "") or None), merged
