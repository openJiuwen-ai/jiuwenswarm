"""把近期会话登记进托管名单（自动发现）。不做排除表。"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.server.im.im_connector.types import ChannelTarget, Identity


def identity_ids(identity: Identity | None) -> set[str]:
    if identity is None:
        return set()
    ids: set[str] = set()
    account = (identity.account or "").strip()
    if account:
        ids.add(account)
    extra = identity.extra or {}
    for item in extra.get("open_ids") or []:
        text = str(item or "").strip()
        if text:
            ids.add(text)
    return ids


def select_auto_host_candidates(
    conversations: list[ChannelTarget],
    *,
    hosted: set[tuple[str, str]],
    self_ids: set[str],
    auto_host_groups: bool,
    auto_host_users: bool,
    max_new: int = 20,
) -> list[ChannelTarget]:
    """已在名单的跳过；丢掉自己和自己的私聊；群/私聊按开关；私聊优先，封顶 max_new。"""
    picked: list[ChannelTarget] = []
    seen: set[tuple[str, str]] = set()
    for conv in conversations:
        kind = conv.kind
        external_id = (conv.external_id or "").strip()
        if kind not in {"group", "user"} or not external_id:
            continue
        key = (kind, external_id)
        if key in hosted or key in seen:
            continue
        if kind == "user" and external_id in self_ids:
            continue
        if kind == "group" and not auto_host_groups:
            continue
        if kind == "user" and not auto_host_users:
            continue
        seen.add(key)
        picked.append(conv)
    picked.sort(key=lambda item: (0 if item.kind == "user" else 1, item.title or "", item.external_id))
    cap = max(0, int(max_new))
    return picked[:cap]


def channel_allows_auto_host(policy: dict[str, Any] | None) -> bool:
    ch = policy or {}
    return bool(ch.get("auto_host_groups") or ch.get("auto_host_users"))
