# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""External-channel output policy (BUG-1 F-1/F-2).

External IM channels (Feishu, DingTalk, ...) receive every runtime frame
today — dispatch receipts, round polling, sub-agent intermediate reports and
status snapshots — which floods owners with process noise and buries the
actual deliverable. This module owns the single gate contract applied at the
Front→Gateway forwarding choke point:

* ``output.external.verbosity: final | progress | verbose`` (default ``final``)
  decides how much of a gated channel's traffic reaches the outside world.
* ``output.external.channels`` lists the channel ids the gate governs; any
  channel outside the list (web/desktop/CLI tooling surfaces) is never gated,
  so interactive UIs keep their full event stream.
* Frames whose owning request has no recorded channel (server pushes, cron
  delivery, control traffic) are never gated — fail-open, the gate may only
  reduce traffic it can positively attribute to a gated conversation.

Dropped frames are acked as delivered (the Worker's critical-frame ACK must
never stall on policy) and logged with request/event identity so every drop
stays traceable in the Front log.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

VERBOSITY_FINAL = "final"
VERBOSITY_PROGRESS = "progress"
VERBOSITY_VERBOSE = "verbose"
_VERBOSITIES = frozenset({VERBOSITY_FINAL, VERBOSITY_PROGRESS, VERBOSITY_VERBOSE})

#: Channel ids whose outbound traffic the gate governs by default. Interactive
#: surfaces (web/desktop/CLI) render progress events as UI state and must not
#: be gated; external IM adapters are the noise surface the policy targets.
DEFAULT_GATED_CHANNELS = frozenset({"feishu", "dingtalk", "welink"})

#: Event substrings that always pass every verbosity level: terminal answers,
#: failures, and every interaction that waits on the owner (ask/permission/
#: approval/confirm/interrupt). These are the "must land" classes of F-2.
_ALWAYS_MARKERS = (
    "final",
    "error",
    "ask_user",
    "permission",
    "approval",
    "confirm",
    "interrupt",
)

#: Exact event types that always pass (deliverables that carry no text body).
_ALWAYS_EXACT = frozenset({"chat.file", "video.core.result"})

#: Progress-category events: shown under ``progress``/``verbose``, dropped
#: under ``final``. Anything not always-pass, listed here, or known-streaming
#: is treated the same as this category (unknown frames fail closed under
#: ``final``, per the gate contract).
_PROGRESS_EVENTS = frozenset({
    "chat.delta",
    "chat.processing_status",
    "chat.reasoning",
    "chat.tool_call",
    "chat.tool_result",
    "chat.subtask_update",
    "chat.subagent_activity",
    "chat.moa_status",
    "todo.updated",
    "goal.snapshot",
    "runtime.accepted",
    "context.compression_state",
    "history.message",
    "models.updated",
})

#: Event prefixes treated as progress noise (team.* member chatter,
#: starter_* board frames — review comment #202779917: S_01 lists starter_*
#: as a progress PREFIX, so single enumerate members by prefix; future
#: starter_* events classify progress instead of unknown).
_PROGRESS_PREFIXES = ("team.", "starter_")


def event_type_of(payload: dict[str, Any]) -> str:
    """Extract the native event type from an E2A wire payload.

    Mirrors the Worker event-store view: the event lives on the wire body,
    with nested ``delta``/``result`` wrappers unwrapped, falling back to
    ``response_kind`` and then the payload-level ``event_type``.
    """
    body = payload.get("body")
    if not isinstance(body, dict):
        body = payload.get("payload")
    if not isinstance(body, dict):
        body = {}
    nested = body.get("delta") if isinstance(body.get("delta"), dict) else body.get("result")
    if isinstance(nested, dict):
        body = {**body, **nested}
    event = str(body.get("event_type") or payload.get("response_kind") or payload.get("event_type") or "")
    return event.strip().lower()


def classify_event(event: str) -> str:
    """Return the frame's policy class: ``always``, ``progress``, or ``unknown``."""
    if not event:
        return "unknown"
    if event in _ALWAYS_EXACT:
        return "always"
    if any(marker in event for marker in _ALWAYS_MARKERS):
        return "always"
    if event in _PROGRESS_EVENTS or event.startswith(_PROGRESS_PREFIXES):
        return "progress"
    return "unknown"


def passes_gate(verbosity: str, event: str) -> bool:
    """Decide whether one event type reaches the external channel."""
    if verbosity not in _VERBOSITIES:
        verbosity = VERBOSITY_FINAL
    if verbosity == VERBOSITY_VERBOSE:
        return True
    kind = classify_event(event)
    if kind == "always":
        return True
    if verbosity == VERBOSITY_PROGRESS:
        return True
    # verbosity == final: only always-pass frames are delivered.
    return False


@dataclass(frozen=True)
class OutputPolicy:
    """Parsed ``output.external`` view shared by Front forwarding decisions."""

    verbosity: str = VERBOSITY_FINAL
    channels: frozenset[str] = field(default_factory=lambda: DEFAULT_GATED_CHANNELS)
    source: str = "default"

    def gate_for(self, channel_id: str) -> str | None:
        """Return the verbosity for a gated channel, or ``None`` when ungated."""
        if not channel_id:
            return None
        if channel_id.strip().lower() not in self.channels:
            return None
        return self.verbosity


def _normalize_channels(raw: Any) -> frozenset[str]:
    if not isinstance(raw, (list, tuple, set)):
        return DEFAULT_GATED_CHANNELS
    return frozenset(str(item).strip().lower() for item in raw if str(item).strip())


def load_output_policy() -> OutputPolicy:
    """Read ``output.external`` from config.yaml; any problem falls back to ``final``.

    The env override ``JIUWENSWARM_OUTPUT_VERBOSITY`` wins over config.yaml so
    operators can stop the bleeding on a live deployment without a restart
    window; an invalid env value is ignored rather than trusted.
    """
    section: dict[str, Any] = {}
    try:
        from jiuwenswarm.common.utils import get_config_file

        cf = get_config_file()
        if cf.exists():
            from ruamel.yaml import YAML

            data = YAML(typ="safe").load(cf.read_text(encoding="utf-8")) or {}
            output = data.get("output") if isinstance(data, dict) else None
            external = output.get("external") if isinstance(output, dict) else None
            if isinstance(external, dict):
                section = external
    except Exception:  # noqa: BLE001 - config problems must not break Front
        logger.warning("[Front] failed to read output.external config; defaulting", exc_info=True)

    verbosity = str(section.get("verbosity") or VERBOSITY_FINAL).strip().lower()
    if verbosity not in _VERBOSITIES:
        logger.warning("[Front] invalid output.external.verbosity %r; using %s", verbosity, VERBOSITY_FINAL)
        verbosity = VERBOSITY_FINAL
    env_verbosity = os.environ.get("JIUWENSWARM_OUTPUT_VERBOSITY", "").strip().lower()
    source = "config" if section else "default"
    if env_verbosity in _VERBOSITIES:
        verbosity = env_verbosity
        source = "env"
    return OutputPolicy(verbosity=verbosity, channels=_normalize_channels(section.get("channels")), source=source)
