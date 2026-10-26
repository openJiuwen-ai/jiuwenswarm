"""Per-channel hosting policy, stored in hosting.db."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jiuwenswarm.server.im.im_hosting.gate import (
    BUILTIN_GROUP_RULE,
    BUILTIN_USER_RULE,
    normalize_rule,
)
from jiuwenswarm.server.im.im_hosting.store import HostingStore

CHANNEL_IDS = ("feishu", "dingtalk", "welink")


def default_channel_policy() -> dict[str, Any]:
    return {
        "discover_interval_seconds": 300,
        "discover_count": 50,
        "auto_host_groups": False,
        "auto_host_users": False,
        "auto_host_max_new": 20,
        "default_group_rule": normalize_rule(BUILTIN_GROUP_RULE),
        "default_user_rule": normalize_rule(BUILTIN_USER_RULE),
        "expert_persona": "",
        "poll_interval_seconds": 10,  # 托管的会话10s拉一次新消息
        "fetch_count": 50,
        "reply_enabled": True,
        "max_replies_per_poll": 3,
    }


def default_policy() -> dict[str, dict[str, Any]]:
    return {cid: default_channel_policy() for cid in CHANNEL_IDS}


def _merge_channel(raw: Any) -> dict[str, Any]:
    base = default_channel_policy()
    if not isinstance(raw, dict):
        return base
    for key in base:
        if key not in raw:
            continue
        if key in {"default_group_rule", "default_user_rule"}:
            fallback = BUILTIN_USER_RULE if key == "default_user_rule" else BUILTIN_GROUP_RULE
            base[key] = normalize_rule(raw[key], fallback=fallback)
        else:
            base[key] = raw[key]
    return base


class HostingPolicyStore:
    def __init__(
        self,
        db_path: Path | None = None,
        *,
        store: HostingStore | None = None,
    ) -> None:
        self._store = store or HostingStore(db_path)

    def load(self) -> dict[str, dict[str, Any]]:
        payloads = self._store.load_policy_payloads()
        out = default_policy()
        for cid in CHANNEL_IDS:
            if cid in payloads:
                out[cid] = _merge_channel(payloads[cid])
        return out

    def save(self, policy: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        merged = default_policy()
        for cid in CHANNEL_IDS:
            merged[cid] = _merge_channel(policy.get(cid))
        self._store.save_policy_payloads(merged)
        return merged

    def patch_channel(self, channel_id: str, patch: dict[str, Any]) -> dict[str, dict[str, Any]]:
        cid = (channel_id or "").strip()
        if cid not in CHANNEL_IDS:
            raise ValueError(f"unknown channel_id: {channel_id}")
        current = self.load()
        current[cid] = _merge_channel({**current[cid], **patch})
        return self.save(current)
