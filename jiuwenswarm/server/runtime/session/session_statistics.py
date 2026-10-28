# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only statistics over committed primary-session facts."""

from __future__ import annotations

from collections.abc import Mapping
import math

from openjiuwen.core.context_engine.usage import ContextUsageSnapshot

from jiuwenswarm.common.protocol_ids import is_valid_session_id
from jiuwenswarm.common.session_message import SESSION_MESSAGE_ORIGIN
from jiuwenswarm.server.runtime.session.session_history import read_committed_main_history

SESSION_USAGE_INFO_KEY = "session_usage_info"


class SessionStatisticsError(RuntimeError):
    """Committed statistics could not be read without inventing missing facts."""


def _read_records(session_id: str) -> list[dict] | None:
    if not is_valid_session_id(session_id):
        raise ValueError("invalid session_id")
    try:
        return read_committed_main_history(session_id)
    except (OSError, ValueError, TimeoutError) as exc:
        raise SessionStatisticsError("cannot read committed session statistics") from exc


def read_session_turns(session_id: str) -> int | None:
    """Count distinct committed primary user inputs in the retained history."""
    records = _read_records(session_id)
    if records is None:
        return None
    keys: set[tuple[str, object]] = set()
    for index, record in enumerate(records):
        if record.get("role") not in ("user", "human"):
            continue
        metadata = record.get("metadata")
        automation = metadata.get("automation") if isinstance(metadata, dict) else None
        if (
            record.get("subagent_id") or record.get("extension_message_id")
            or record.get("message_origin") == SESSION_MESSAGE_ORIGIN
            or record.get("feedback_only") or record.get("is_compact_summary")
            or record.get("mode") == "subagent"
            or record.get("channel_id") in ("cron", "heartbeat")
            or record.get("source")
            or isinstance(automation, dict) and automation.get("kind") == "heartbeat"
        ):
            continue
        request_id = record.get("request_id")
        record_id = record.get("id")
        if isinstance(request_id, str) and request_id.strip():
            keys.add(("request", request_id))
        elif isinstance(record_id, str) and record_id:
            keys.add(("record", record_id))
        else:
            keys.add(("legacy", index))
    return len(keys)


def _number(value: object, field: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid usage {field}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid usage {field}")
    return value


def _context(session_id: str, raw: dict | None) -> dict | None:
    if raw is None or not isinstance(raw.get("context_window"), dict):
        return None
    # Child/member windows can share the product session; they are not the
    # primary context and must not replace its most recent observation.
    if (
        raw.get("session_id") != session_id or raw.get("depth", 0) != 0
        or raw.get("parent_invocation_id") or raw.get("member_name") or raw.get("team_id")
    ):
        return None
    for key in ("limit_tokens", "input_tokens", "local_estimated_input_tokens", "occupancy_rate"):
        _number(raw["context_window"].get(key), key)
    model = ContextUsageSnapshot.model_validate(raw)
    data = model.model_dump(mode="json")
    window = data["context_window"]
    for key in ("limit_tokens", "input_tokens", "local_estimated_input_tokens", "occupancy_rate"):
        _number(window.get(key), key)
    for part in data["parts"].values():
        _number(part.get("tokens"), "parts.tokens")
    return {
        "request_id": model.request_id, "phase": model.phase,
        "timestamp": model.timestamp, "sequence": model.sequence,
        "context_window": window, "measurement": data["measurement"],
        "parts": data["parts"],
    }


def _turn(raw: dict | None) -> dict | None:
    if raw is None or not isinstance(raw.get("usage"), dict):
        return None
    usage = raw["usage"]
    values = {key: _number(usage.get(key), key) for key in (
        "input_tokens", "output_tokens", "total_tokens", "cache_tokens",
    )}
    if all(value is None for value in values.values()):
        return None
    return {
        "request_id": raw.get("request_id"), "timestamp": raw.get("timestamp"),
        "source": "provider_usage", **values,
    }


def build_usage_snapshot(
    session_id: str,
    *,
    context_usage: dict | None = None,
    turn_usage: dict | None = None,
    source: str = "host",
) -> dict | None:
    """Copy only public counters and measurement metadata at a host boundary."""
    if not is_valid_session_id(session_id):
        raise ValueError("invalid session_id")
    context = _context(session_id, context_usage)
    turn = _turn(turn_usage)
    if context is None and turn is None:
        return None
    return {
        "schema_version": "session-usage.v1", "session_id": session_id,
        "source": source, "context": context, "turn": turn,
    }


def _copy_snapshot(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _copy_snapshot(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_copy_snapshot(item) for item in value]
    return value


def select_usage_breakdown(snapshot: Mapping | None, breakdown: bool) -> dict | None:
    """Return an isolated result, omitting category details by default."""
    if not isinstance(breakdown, bool):
        raise TypeError("session.usage breakdown must be a bool")
    result = _copy_snapshot(snapshot)
    if result is not None and not breakdown and result.get("context") is not None:
        result["context"].pop("parts", None)
    return result


def read_session_usage(session_id: str, breakdown: bool = False) -> dict | None:
    """Read the last committed primary window and per-turn consumption facts."""
    if not isinstance(breakdown, bool):
        raise TypeError("session.usage breakdown must be a bool")
    records = _read_records(session_id)
    if records is None:
        return None
    context = None
    turn = None
    try:
        for record in reversed(records):
            if (
                record.get("subagent_id") or record.get("mode") == "subagent"
                or record.get("session_id") not in (None, "", session_id)
            ):
                continue
            if context is None and record.get("event_type") == "context.usage":
                context = _context(session_id, record)
            if turn is None and record.get("event_type") == "chat.usage_summary":
                turn = _turn(record)
            if context is not None and turn is not None:
                break
    except (TypeError, ValueError) as exc:
        raise SessionStatisticsError("invalid committed session usage") from exc
    snapshot = None if context is None and turn is None else {
        "schema_version": "session-usage.v1", "session_id": session_id,
        "source": "history", "context": context, "turn": turn,
    }
    return select_usage_breakdown(snapshot, breakdown)
