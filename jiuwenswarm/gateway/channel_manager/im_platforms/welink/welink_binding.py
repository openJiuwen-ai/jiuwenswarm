# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Merge WeLink CLI login and assistant AK/SK onto one colleague."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SECRET_KEYS = ("assistant_secret",)


def apply_welink_authorization(current: Mapping[str, Any] | None, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Return the channel section after one authorization step.

    The channel stays disabled until the same colleague has both a CLI login
    and an assistant credential. Switching colleagues drops the previous half.
    """
    merged: dict[str, Any] = dict(current or {})
    incoming_agent = str(patch.get("persistent_agent_id") or "").strip()
    previous_agent = str(merged.get("persistent_agent_id") or "").strip()
    if incoming_agent and previous_agent and incoming_agent != previous_agent:
        merged["assistant_credential_id"] = ""
        merged["assistant_secret"] = ""
        merged["cli_authorized"] = False
        merged["credential_dir"] = ""
    for key, value in patch.items():
        merged[key] = value
    agent_id = str(merged.get("persistent_agent_id") or "").strip()
    cli_ready = bool(
        merged.get("cli_authorized")
        and str(merged.get("account") or "").strip()
        and str(merged.get("credential_dir") or "").strip()
        and agent_id
    )
    assistant_ready = bool(
        str(merged.get("assistant_credential_id") or "").strip()
        and str(merged.get("assistant_secret") or "").strip()
        and agent_id
    )
    merged["enabled"] = bool(cli_ready and assistant_ready)
    merged.setdefault("cli_path", "welink-cli")
    merged.setdefault("env", "pro")
    merged.setdefault("poll_interval_seconds", 4)
    return merged


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


def public_welink_authorization(conf: Mapping[str, Any] | None) -> dict[str, Any]:
    """Status safe to send to the browser. The assistant secret stays out."""
    data = dict(conf or {})
    for key in _SECRET_KEYS:
        data.pop(key, None)
    agent_id = str((conf or {}).get("persistent_agent_id") or "").strip()
    secret = str((conf or {}).get("assistant_secret") or "").strip()
    credential_id = str((conf or {}).get("assistant_credential_id") or "").strip()
    cli_ready = bool(
        (conf or {}).get("cli_authorized")
        and str((conf or {}).get("account") or "").strip()
        and str((conf or {}).get("credential_dir") or "").strip()
        and agent_id
    )
    assistant_ready = bool(credential_id and secret and agent_id)
    return {
        "cli_ready": cli_ready,
        "assistant_ready": assistant_ready,
        "channel_enabled": bool(cli_ready and assistant_ready),
        "account": str((conf or {}).get("account") or ""),
        "persistent_agent_id": agent_id,
        "assistant_credential_id": credential_id,
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


def load_welink_section() -> dict[str, Any]:
    from jiuwenswarm.common.config import CONFIG_YAML_PATH, load_yaml_round_trip

    data = load_yaml_round_trip(CONFIG_YAML_PATH)
    channels = data.get("channels") if isinstance(data, dict) else None
    welink = channels.get("welink") if isinstance(channels, dict) else None
    return dict(welink) if isinstance(welink, dict) else {}


def save_welink_authorization(patch: Mapping[str, Any], current: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one step and write it. Returns the public view and the stored section."""
    from jiuwenswarm.common.config import update_channel_in_config

    merged = apply_welink_authorization(load_welink_section() if current is None else current, patch)
    update_channel_in_config("welink", merged)
    return public_welink_authorization(merged), merged
