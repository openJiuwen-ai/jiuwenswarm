# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Root-only per-card permission queue built on Core sparse resume."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Literal

from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest

from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import (
    ToolInvocationKeyV1,
    normalize_tool_invocation_text,
)

MAX_ROOT_PERMISSION_CARDS = 64
RootPermissionCardState = Literal["active", "pending", "resuming"]


class RootPermissionQueueError(ValueError):
    """Fail-closed root permission queue error."""


@dataclass(frozen=True, slots=True)
class RootPermissionCard:
    """Immutable identity and interrupt request for one root tool call."""

    key: ToolInvocationKeyV1
    tool_name: str
    state: RootPermissionCardState
    request: InterruptRequest | None = None
    auto_manual: bool = False
    root_context: Any | None = None


@dataclass(frozen=True, slots=True)
class RootPermissionSnapshot:
    """One Core interruption snapshot in its visible FIFO order."""

    cards: tuple[RootPermissionCard, ...]
    interactions: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class RootPermissionAnswer:
    """One exact head-card answer reserved for immediate sparse dispatch."""

    card: RootPermissionCard
    interactive_input: InteractiveInput
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RootPermissionClaim:
    """The single CAS winner for one admitted sparse-resume callback."""

    card: RootPermissionCard
    quarantined: bool


class RootPermissionQueue:
    """Own pending-card order and one head answer in flight per root session."""

    def __init__(self, *, id_factory: Callable[[], str] | None = None) -> None:
        self._id_factory = id_factory or (lambda: f"tiv_{secrets.token_urlsafe(18)}")
        self._cards: dict[str, RootPermissionCard] = {}
        self._active: dict[tuple[str, str, str, str], str] = {}
        self._pending: dict[tuple[str, str, str], str] = {}
        self._order: dict[str, tuple[str, ...]] = {}
        self._quarantined: set[str] = set()
        self._lock = threading.RLock()

    def begin(
        self,
        *,
        root_session_id: str,
        request_id: str,
        execution_session_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> RootPermissionCard:
        """Bind a fresh call or return its exact pending card on Core resume."""

        root_session_id = _text("root_session_id", root_session_id)
        execution_session_id = _text("execution_session_id", execution_session_id)
        tool_call_id = _text("tool_call_id", tool_call_id)
        tool_name = _text("tool_name", tool_name)
        pending_key = (root_session_id, execution_session_id, tool_call_id)
        with self._lock:
            self._raise_if_quarantined_locked(root_session_id)
            pending_id = self._pending.get(pending_key)
            if pending_id is not None:
                card = self._cards.get(pending_id)
                if card is None or card.tool_name != tool_name:
                    raise RootPermissionQueueError("permission_queue_pending_mismatch")
                return card

            request_id = _text("request_id", request_id)
            active_key = (
                root_session_id,
                request_id,
                execution_session_id,
                tool_call_id,
            )
            if active_key in self._active:
                raise RootPermissionQueueError("permission_queue_duplicate_call")
            invocation_id = _text("invocation_id", self._id_factory(), maximum=128)
            if invocation_id in self._cards:
                raise RootPermissionQueueError("permission_queue_id_collision")
            key = ToolInvocationKeyV1(
                invocation_id=invocation_id,
                root_session_id=root_session_id,
                request_id=request_id,
                executor_kind="agent",
                execution_session_id=execution_session_id,
                tool_call_id=tool_call_id,
            )
            card = RootPermissionCard(
                key=key,
                tool_name=tool_name,
                state="active",
            )
            self._cards[invocation_id] = card
            self._active[active_key] = invocation_id
            return card

    def mark_pending(
        self,
        key: ToolInvocationKeyV1,
        *,
        request: InterruptRequest,
        auto_manual: bool,
        root_context: Any | None,
    ) -> RootPermissionCard:
        """Freeze the exact interrupt request emitted for one active call."""

        if not isinstance(request, InterruptRequest):
            raise RootPermissionQueueError("permission_queue_request_invalid")
        with self._lock:
            card = self._exact_card_locked(key)
            if card.state == "pending":
                if card.request != request:
                    raise RootPermissionQueueError(
                        "permission_queue_request_changed"
                    )
                return card
            if card.state != "active":
                raise RootPermissionQueueError("permission_queue_pending_transition")
            pending_key = (
                key.root_session_id,
                key.execution_session_id,
                key.tool_call_id,
            )
            existing_id = self._pending.get(pending_key)
            if existing_id not in {None, key.invocation_id}:
                raise RootPermissionQueueError("permission_queue_pending_collision")
            frozen = replace(
                card,
                state="pending",
                request=deepcopy(request),
                auto_manual=bool(auto_manual),
                root_context=root_context,
            )
            self._cards[key.invocation_id] = frozen
            self._active.pop(_active_key(card.key), None)
            self._pending[pending_key] = key.invocation_id
            return frozen

    def pending_for_call(
        self,
        *,
        root_session_id: str,
        execution_session_id: str,
        tool_call_id: str,
    ) -> RootPermissionCard | None:
        """Return the exact pending sibling without mutating queue order."""

        lookup = (
            str(root_session_id or ""),
            str(execution_session_id or ""),
            str(tool_call_id or ""),
        )
        with self._lock:
            invocation_id = self._pending.get(lookup)
            card = self._cards.get(invocation_id or "")
            return card if card is not None and card.state == "pending" else None

    def reserve_answer(
        self,
        root_session_id: str,
        incoming: InteractiveInput,
    ) -> RootPermissionAnswer:
        """Validate and reserve exactly one current head-card answer."""

        root_session_id = _text("root_session_id", root_session_id)
        if getattr(incoming, "raw_inputs", None) is not None:
            raise RootPermissionQueueError("permission_queue_raw_input_invalid")
        submitted = getattr(incoming, "user_inputs", None)
        if not isinstance(submitted, Mapping) or len(submitted) != 1:
            raise RootPermissionQueueError("permission_queue_one_answer_required")
        raw_card_id, raw_payload = next(iter(submitted.items()))
        card_id = _text("card_id", raw_card_id, maximum=128)
        if not isinstance(raw_payload, Mapping):
            raise RootPermissionQueueError("permission_queue_payload_invalid")

        with self._lock:
            self._raise_if_quarantined_locked(root_session_id)
            order = self._order.get(root_session_id, ())
            if not order:
                raise RootPermissionQueueError("permission_queue_empty")
            card = self._cards.get(order[0])
            if card is None or card.state != "pending":
                raise RootPermissionQueueError("permission_queue_head_invalid")
            if card_id != card.key.invocation_id:
                raise RootPermissionQueueError("permission_queue_non_head_answer")
            payload = _normalize_answer(raw_payload)
            reserved = replace(card, state="resuming")
            self._cards[card.key.invocation_id] = reserved
            sparse = InteractiveInput()
            sparse.update(card.key.tool_call_id, deepcopy(dict(payload)))
            return RootPermissionAnswer(reserved, sparse, payload)

    def release_answer(self, answer: RootPermissionAnswer) -> None:
        """Return an unsent head reservation to pending state."""

        with self._lock:
            current = self._cards.get(answer.card.key.invocation_id)
            if current is None or current != answer.card or current.state != "resuming":
                return
            self._cards[current.key.invocation_id] = replace(current, state="pending")

    def claim_answer_for_call(
        self,
        *,
        root_session_id: str,
        execution_session_id: str,
        tool_call_id: str,
        tool_name: str,
    ) -> RootPermissionClaim:
        """CAS one admitted answer from ``resuming`` to callback-owned ``active``."""

        lookup = (
            _text("root_session_id", root_session_id),
            _text("execution_session_id", execution_session_id),
            _text("tool_call_id", tool_call_id),
        )
        tool_name = _text("tool_name", tool_name)
        with self._lock:
            invocation_id = self._pending.get(lookup)
            card = self._cards.get(invocation_id or "")
            if card is None or card.state != "resuming":
                raise RootPermissionQueueError("permission_queue_answer_not_reserved")
            if card.tool_name != tool_name:
                raise RootPermissionQueueError("permission_queue_pending_mismatch")
            active_key = _active_key(card.key)
            if active_key in self._active:
                raise RootPermissionQueueError("permission_queue_duplicate_call")
            claimed = replace(card, state="active")
            self._cards[card.key.invocation_id] = claimed
            self._pending.pop(lookup, None)
            self._active[active_key] = card.key.invocation_id
            return RootPermissionClaim(
                card=claimed,
                quarantined=card.key.root_session_id in self._quarantined,
            )

    def consume_accepted_answer_if_quarantined(
        self,
        answer: RootPermissionAnswer,
    ) -> bool:
        """Let request cleanup consume an admitted answer not claimed by callback."""

        with self._lock:
            current = self._cards.get(answer.card.key.invocation_id)
            if current is None or current != answer.card:
                return False
            if (
                current.state != "resuming"
                or current.key.root_session_id not in self._quarantined
            ):
                return False
            self._remove_locked(current)
            return True

    def reconcile(
        self,
        result: Any,
        *,
        root_session_id: str,
    ) -> RootPermissionSnapshot | None:
        """Reconcile Core's returned interruption set and expose only its head."""

        root_session_id = _text("root_session_id", root_session_id)
        if not isinstance(result, Mapping) or result.get("result_type") != "interrupt":
            return None
        raw_state = result.get("state")
        raw_ids = result.get("interrupt_ids")
        invalid_snapshot = not isinstance(raw_state, list) or not isinstance(
            raw_ids, list
        )
        if invalid_snapshot:
            self.quarantine(root_session_id)
            raise RootPermissionQueueError("permission_queue_snapshot_invalid")
        if not raw_state or len(raw_state) != len(raw_ids):
            self.quarantine(root_session_id)
            raise RootPermissionQueueError("permission_queue_snapshot_invalid")
        if len(raw_state) > MAX_ROOT_PERMISSION_CARDS:
            self.quarantine(root_session_id)
            raise RootPermissionQueueError("permission_queue_snapshot_invalid")

        interactions: list[dict[str, Any]] = []
        keys: list[ToolInvocationKeyV1] = []
        saw_other = False
        for raw_id, item in zip(raw_ids, raw_state, strict=True):
            inner_id = _text("tool_call_id", raw_id)
            interaction = _interaction(item)
            if interaction is None or interaction["id"] != inner_id:
                self.quarantine(root_session_id)
                raise RootPermissionQueueError("permission_queue_snapshot_mismatch")
            request = interaction["value"]
            wire_key = _request_key(request)
            if wire_key is None:
                saw_other = True
                interactions.append(interaction)
                continue
            try:
                key = ToolInvocationKeyV1.from_wire(wire_key)
            except (TypeError, ValueError) as exc:
                self.quarantine(root_session_id)
                raise RootPermissionQueueError(
                    "permission_queue_locator_invalid"
                ) from exc
            if key.root_session_id != root_session_id or key.tool_call_id != inner_id:
                self.quarantine(root_session_id)
                raise RootPermissionQueueError("permission_queue_locator_mismatch")
            keys.append(key)
            interactions.append(interaction)

        if not keys:
            return None
        if saw_other or len(keys) != len(interactions):
            self.quarantine(root_session_id)
            raise RootPermissionQueueError("permission_queue_mixed_interrupt")
        if len({key.invocation_id for key in keys}) != len(keys):
            self.quarantine(root_session_id)
            raise RootPermissionQueueError("permission_queue_duplicate")

        with self._lock:
            cards = tuple(self._exact_card_locked(key) for key in keys)
            if any(card.state != "pending" for card in cards):
                self._quarantined.add(root_session_id)
                raise RootPermissionQueueError("permission_queue_card_not_pending")
            live_ids = {key.invocation_id for key in keys}
            for card in tuple(self._cards.values()):
                if card.key.root_session_id != root_session_id:
                    continue
                if card.state == "resuming" and card.key.invocation_id not in live_ids:
                    self._quarantined.add(root_session_id)
                    raise RootPermissionQueueError(
                        "permission_queue_unclaimed_resume_missing"
                    )
                if card.state == "pending" and card.key.invocation_id not in live_ids:
                    self._quarantined.add(root_session_id)
                    raise RootPermissionQueueError("permission_queue_sibling_missing")
            self._order[root_session_id] = tuple(key.invocation_id for key in keys)
            return RootPermissionSnapshot(cards=cards, interactions=tuple(interactions))

    def get(self, key: ToolInvocationKeyV1) -> RootPermissionCard | None:
        with self._lock:
            card = self._cards.get(key.invocation_id)
            return card if card is not None and card.key == key else None

    def finish(self, key: ToolInvocationKeyV1) -> bool:
        with self._lock:
            card = self._cards.get(key.invocation_id)
            if card is None or card.key != key:
                return False
            self._remove_locked(card)
            return True

    def finish_active(self, key: ToolInvocationKeyV1) -> bool:
        """Finish only the exact permission evaluation still owned by a callback."""

        with self._lock:
            card = self._cards.get(key.invocation_id)
            if card is None or card.key != key or card.state != "active":
                return False
            self._remove_locked(card)
            return True

    def has_live(self, *, root_session_id: str | None = None) -> bool:
        """Return whether this queue still owns any permission admission state."""

        with self._lock:
            return any(
                root_session_id is None
                or card.key.root_session_id == root_session_id
                for card in self._cards.values()
            )

    def snapshot_scope(
        self, *, root_session_id: str, request_id: str | None = None
    ) -> tuple[ToolInvocationKeyV1, ...]:
        with self._lock:
            keys: list[ToolInvocationKeyV1] = []
            for card in self._cards.values():
                if card.key.root_session_id != root_session_id:
                    continue
                if request_id is None or card.key.request_id == request_id:
                    keys.append(card.key)
            return tuple(keys)

    def begin_cutover(self, *, root_session_id: str) -> bool:
        """Quarantine a non-empty root scope before Core cancellation."""

        root_session_id = str(root_session_id or "")
        with self._lock:
            if root_session_id in self._quarantined:
                return True
            if not any(
                card.key.root_session_id == root_session_id
                for card in self._cards.values()
            ):
                return False
            self._quarantined.add(root_session_id)
            return True

    def cutover_continuation_scope(
        self,
        *,
        root_session_id: str,
    ) -> tuple[ToolInvocationKeyV1, ...]:
        """Freeze only Core continuation cards after active callback cleanup."""

        root_session_id = str(root_session_id or "")
        with self._lock:
            if root_session_id not in self._quarantined:
                raise RootPermissionQueueError("permission_queue_cutover_not_started")
            cards = tuple(
                card
                for card in self._cards.values()
                if card.key.root_session_id == root_session_id
            )
            if any(card.state == "active" for card in cards):
                raise RootPermissionQueueError(
                    "permission_queue_active_cleanup_pending"
                )
            if any(card.state == "resuming" for card in cards):
                raise RootPermissionQueueError(
                    "permission_queue_unowned_resume"
                )
            return tuple(card.key for card in cards)

    def cancel_snapshot(self, keys: tuple[ToolInvocationKeyV1, ...]) -> int:
        with self._lock:
            removed = 0
            for key in keys:
                card = self._cards.get(key.invocation_id)
                if card is not None and card.key == key:
                    self._remove_locked(card)
                    removed += 1
            return removed

    def discard_continuation(
        self,
        keys: tuple[ToolInvocationKeyV1, ...],
        *,
        root_session_id: str,
    ) -> int:
        """Remove one confirmed-discarded Core continuation and recover its queue."""

        root_session_id = str(root_session_id or "")
        with self._lock:
            if root_session_id not in self._quarantined:
                raise RootPermissionQueueError("permission_queue_cutover_not_started")
            expected_ids = {
                key.invocation_id
                for key in keys
                if key.root_session_id == root_session_id
            }
            live_ids = {
                card.key.invocation_id
                for card in self._cards.values()
                if card.key.root_session_id == root_session_id
            }
            if live_ids != expected_ids:
                raise RootPermissionQueueError(
                    "permission_queue_discard_scope_changed"
                )
            removed = 0
            for key in keys:
                if key.root_session_id != root_session_id:
                    continue
                card = self._cards.get(key.invocation_id)
                if card is not None and card.key == key:
                    self._remove_locked(card)
                    removed += 1
            self._quarantined.discard(root_session_id)
            return removed

    def quarantine(self, root_session_id: str) -> None:
        with self._lock:
            self._quarantined.add(str(root_session_id or ""))

    def raise_if_quarantined(self, root_session_id: str) -> None:
        with self._lock:
            self._raise_if_quarantined_locked(root_session_id)

    def _raise_if_quarantined_locked(self, root_session_id: str) -> None:
        if str(root_session_id or "") in self._quarantined:
            raise RootPermissionQueueError("permission_queue_quarantined")

    def _exact_card_locked(self, key: ToolInvocationKeyV1) -> RootPermissionCard:
        card = self._cards.get(key.invocation_id)
        if card is None or card.key != key:
            raise RootPermissionQueueError("permission_queue_card_mismatch")
        return card

    def _remove_locked(self, card: RootPermissionCard) -> None:
        self._cards.pop(card.key.invocation_id, None)
        self._active.pop(_active_key(card.key), None)
        self._pending.pop(
            (
                card.key.root_session_id,
                card.key.execution_session_id,
                card.key.tool_call_id,
            ),
            None,
        )
        order = tuple(
            item
            for item in self._order.get(card.key.root_session_id, ())
            if item != card.key.invocation_id
        )
        if order:
            self._order[card.key.root_session_id] = order
        else:
            self._order.pop(card.key.root_session_id, None)


def _active_key(key: ToolInvocationKeyV1) -> tuple[str, str, str, str]:
    return (
        key.root_session_id,
        key.request_id,
        key.execution_session_id,
        key.tool_call_id,
    )


def _text(name: str, value: Any, *, maximum: int = 512) -> str:
    try:
        return normalize_tool_invocation_text(name, value, maximum=maximum)
    except ValueError as exc:
        raise RootPermissionQueueError(f"permission_queue_{name}_invalid") from exc


def _normalize_answer(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "approved",
        "auto_confirm",
        "persist_allow",
        "feedback",
    }
    if not set(value).issubset(allowed):
        raise RootPermissionQueueError("permission_queue_payload_invalid")
    approved = value.get("approved")
    auto_confirm = value.get("auto_confirm")
    persist_allow = value.get("persist_allow", False)
    feedback = value.get("feedback", "")
    if not isinstance(approved, bool) or not isinstance(auto_confirm, bool):
        raise RootPermissionQueueError("permission_queue_decision_invalid")
    if not isinstance(persist_allow, bool) or not isinstance(feedback, str):
        raise RootPermissionQueueError("permission_queue_decision_invalid")
    if len(feedback) > 4000:
        raise RootPermissionQueueError("permission_queue_feedback_too_large")
    if persist_allow and (not approved or not auto_confirm):
        raise RootPermissionQueueError("permission_queue_scope_invalid")
    result = {
        "approved": approved,
        "auto_confirm": auto_confirm,
        "feedback": feedback,
    }
    if "persist_allow" in value:
        result["persist_allow"] = persist_allow
    return result


def _interaction(item: Any) -> dict[str, Any] | None:
    payload = getattr(item, "payload", item)
    if hasattr(payload, "id") and hasattr(payload, "value"):
        return {"id": str(payload.id or "").strip(), "value": payload.value}
    if isinstance(payload, Mapping):
        if "payload" in payload:
            return _interaction(payload["payload"])
        if "id" in payload and "value" in payload:
            return {
                "id": str(payload.get("id") or "").strip(),
                "value": payload.get("value"),
            }
    return None


def _request_key(request: Any) -> Mapping[str, Any] | None:
    metadata = (
        request.get("metadata")
        if isinstance(request, Mapping)
        else getattr(request, "metadata", None)
    )
    if isinstance(metadata, Mapping) and isinstance(
        metadata.get("tool_invocation_key"), Mapping
    ):
        return metadata["tool_invocation_key"]
    value = request.get("value") if isinstance(request, Mapping) else None
    if isinstance(value, Mapping) and isinstance(
        value.get("tool_invocation_key"), Mapping
    ):
        return value["tool_invocation_key"]
    return None


__all__ = [
    "RootPermissionAnswer",
    "RootPermissionCard",
    "RootPermissionQueue",
    "RootPermissionQueueError",
    "RootPermissionSnapshot",
]
