"""One poll round: fetch, watermark, preview, gate, optional auto-reply."""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

from jiuwenswarm.server.im.im_connector.plugin import ChannelPlugin
from jiuwenswarm.server.im.im_connector.types import ChannelTarget, FetchOptions, ImMessage, SendOptions
from jiuwenswarm.server.im.im_hosting.gate import RelevanceJudge, message_passes_gate, resolve_rule
from jiuwenswarm.server.im.im_hosting.reply_bridge import AgentManagerLike, generate_reply_via_expert
from jiuwenswarm.server.im.im_hosting.store import HostingStore

LOGGER = logging.getLogger(__name__)

PREVIEW_LIMIT = 20
MAX_RELEVANT_JUDGEMENTS = 8
# 自动进名单时，触发发现的那几条往往略早于 hosting_since，首轮仍要代回。
AUTO_HOST_LOOKBACK_MS = 5 * 60 * 1000


def preview_from_messages(
    messages: list[ImMessage],
    *,
    gated_by_id: dict[str, dict[str, Any]] | None = None,
    limit: int = PREVIEW_LIMIT,
) -> list[dict[str, Any]]:
    ordered = sorted(messages, key=lambda m: (m.sent_at or 0, m.msg_id or ""))
    tail = ordered[-limit:]
    extra = gated_by_id or {}
    rows = []
    for item in tail:
        row: dict[str, Any] = {
            "msg_id": item.msg_id,
            "sender_name": item.sender_name,
            "content_text": (item.content_text or "")[:500],
            "sent_at": item.sent_at,
            "is_self": item.is_self,
        }
        gate = extra.get(item.msg_id or "")
        if gate:
            row["gated"] = gate.get("passed")
            row["gate_reason"] = gate.get("reason")
            if gate.get("replied"):
                row["replied"] = True
        rows.append(row)
    return rows


def bootstrap_watermark_ms(
    messages: list[ImMessage],
    hosting_since_ms: int,
) -> int:
    """手选托管：首轮只锚定水位，不代回当前页里已有的历史。"""
    latest = max((item.sent_at or 0 for item in messages), default=0)
    return max(int(hosting_since_ms or 0), int(latest or 0))


def first_poll_base_ms(target: dict[str, Any], messages: list[ImMessage]) -> int:
    hosting_since = int(target.get("hosting_since_ms") or 0)
    if str(target.get("source") or "") == "auto":
        return max(0, hosting_since - AUTO_HOST_LOOKBACK_MS)
    return bootstrap_watermark_ms(messages, hosting_since)


def advance_watermark_ms(
    current_ms: int,
    messages: list[ImMessage],
) -> int:
    latest = max((item.sent_at or 0 for item in messages), default=current_ms)
    return max(int(current_ms or 0), int(latest or 0))


def _new_inbound(messages: list[ImMessage], watermark_ms: int) -> list[ImMessage]:
    out = []
    for item in messages:
        if item.is_self:
            continue
        if int(item.sent_at or 0) <= int(watermark_ms or 0):
            continue
        out.append(item)
    return out


async def _try_auto_reply(
    *,
    store: HostingStore,
    plugin: ChannelPlugin,
    target: dict[str, Any],
    conv: ChannelTarget,
    item: ImMessage,
    agent_manager: AgentManagerLike | None = None,
    channel_policy: dict[str, Any] | None = None,
) -> bool:
    channel_id = str(target["channel_id"])
    kind = str(target["target_kind"])
    external_id = str(target["external_id"])
    msg_id = str(item.msg_id or "")
    if not store.claim_reply_turn(channel_id, kind, external_id, msg_id):
        return False
    try:
        try:
            reply_text = await generate_reply_via_expert(
                agent_manager,
                target=target,
                message=item,
                channel_policy=channel_policy,
            )
        except ValueError as exc:
            LOGGER.warning("[im_hosting] skip reply target=%s: %s", target.get("id"), exc)
            return False
        if not reply_text.strip():
            return False
        send = await plugin.send_message(
            conv,
            reply_text,
            SendOptions(
                mention_sender=kind == "group",
                extra={"source_message": item},
            ),
        )
        if not send.ok:
            LOGGER.warning(
                "[im_hosting] send failed target=%s msg=%s err=%s",
                target.get("id"),
                msg_id,
                send.error_message or send.error_code,
            )
            return False
        return True
    except Exception:
        LOGGER.exception(
            "[im_hosting] auto-reply failed target=%s msg=%s",
            target.get("id"),
            msg_id,
        )
        return False


async def run_poll_once(
    store: HostingStore,
    plugin: ChannelPlugin,
    target: dict[str, Any],
    *,
    fetch_count: int = 50,
    channel_policy: dict[str, Any] | None = None,
    relevance_judge: RelevanceJudge | None = None,
    agent_manager: AgentManagerLike | None = None,
) -> dict[str, Any]:
    kind = str(target["target_kind"])
    if kind not in {"group", "user"}:
        raise ValueError(f"invalid target_kind: {kind}")
    external_id = str(target["external_id"])
    channel_id = str(target["channel_id"])
    policy = channel_policy or {}
    reply_enabled = bool(policy.get("reply_enabled", True))
    max_replies = max(0, int(policy.get("max_replies_per_poll") or 3))
    conv = ChannelTarget(
        kind=cast(Literal["group", "user"], kind),
        external_id=external_id,
        title=target.get("title"),
    )
    page = await plugin.fetch_messages(conv, FetchOptions(count=max(1, int(fetch_count))))
    messages = list(page.messages or [])
    wm = store.get_watermark(channel_id, kind, external_id)
    rule = resolve_rule(target, channel_policy)
    gated_by_id: dict[str, dict[str, Any]] = {}
    gated_in = 0
    replied = 0
    if wm is None:
        base_wm = first_poll_base_ms(target, messages)
    else:
        base_wm = int(wm["last_processed_at_ms"] or 0)
    current_wm = base_wm
    judged = 0
    inbound = sorted(
        _new_inbound(messages, base_wm),
        key=lambda m: (m.sent_at or 0, m.msg_id or ""),
    )
    for item in inbound:
        if judged >= MAX_RELEVANT_JUDGEMENTS and rule.get("match_mode") == "relevant":
            passed, reason = False, "relevant_capped"
        else:
            passed, reason = await message_passes_gate(
                item.content_text or "",
                rule,
                relevance_judge=relevance_judge,
            )
            if rule.get("match_mode") == "relevant":
                judged += 1
        gate_row: dict[str, Any] = {"passed": passed, "reason": reason}
        if item.msg_id:
            gated_by_id[item.msg_id] = gate_row
        sent_at = int(item.sent_at or 0)
        if passed:
            gated_in += 1
            if reply_enabled and replied < max_replies:
                did_reply = await _try_auto_reply(
                    store=store,
                    plugin=plugin,
                    target=target,
                    conv=conv,
                    item=item,
                    agent_manager=agent_manager,
                    channel_policy=policy,
                )
                if did_reply:
                    replied += 1
                    gate_row["replied"] = True
                    if item.msg_id:
                        gated_by_id[item.msg_id] = gate_row
        current_wm = max(current_wm, sent_at)
    next_ms = advance_watermark_ms(current_wm, messages)
    last_id = None
    if messages:
        newest = max(messages, key=lambda m: (m.sent_at or 0, m.msg_id or ""))
        last_id = newest.msg_id
    store.set_watermark(
        channel_id,
        kind,
        external_id,
        last_processed_at_ms=next_ms,
        last_processed_msg_id=last_id,
    )
    preview = preview_from_messages(messages, gated_by_id=gated_by_id)
    store.record_poll(str(target["id"]), preview=preview, error=None)
    return {
        "target_id": target["id"],
        "fetched": len(messages),
        "watermark_ms": next_ms,
        "preview": preview,
        "rule": rule,
        "gated_in": gated_in,
        "replied": replied,
        "reply_enabled": reply_enabled,
    }
