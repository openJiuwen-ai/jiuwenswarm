# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Poll group history for one logged-in WeLink account."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Protocol

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import (
    WelinkHistoryMessage,
    parse_cli_json,
    parse_group_members,
    parse_history_messages,
    parse_recent_group_ids,
    reader_assistant_accounts,
    redact_secrets,
    select_new_messages,
    shares_reader_assistant,
)


def redact_cli_failure(text: str) -> str:
    compact = " ".join(redact_secrets(text or "").split())
    return compact[:240]

logger = logging.getLogger(__name__)

# A short burst is worth handing to the colleague. A whole day of history is not.
_DELIVER_LIMIT = 5
# One welink-cli history page. Used once when a group flips from "not shared" to shared,
# so the catch-up lands on the latest messages instead of the page after a stale cursor.
_CATCHUP_LIMIT = 20


class WelinkRunner(Protocol):
    def query_recent_conversations(self) -> object:
        ...

    def query_history(self, group_id: str, message_id: str | None) -> object:
        ...

    def query_group_members(self, group_id: str) -> object:
        ...


class CursorStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, str] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                self._data = {str(key): str(value) for key, value in loaded.items() if value}

    @property
    def path(self) -> Path:
        return self._path

    def get(self, group_id: str) -> str | None:
        value = self._data.get(group_id)
        return value or None

    def set(self, group_id: str, message_id: str) -> None:
        self._data[group_id] = message_id
        self._write()

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self._path)


class MembershipStore:
    """Persisted answer to "is the reader in this group with their own assistant?"."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, object]] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                self._data = {str(key): value for key, value in loaded.items() if isinstance(value, dict)}

    def get(self, group_id: str) -> tuple[bool, frozenset[str]] | None:
        raw = self._data.get(group_id)
        if not isinstance(raw, dict) or "shared" not in raw:
            return None
        assistants = raw.get("assistants") or []
        if not isinstance(assistants, list):
            assistants = []
        return bool(raw.get("shared")), frozenset(str(item) for item in assistants if str(item).strip())

    def set(self, group_id: str, *, shared: bool, assistants: set[str]) -> None:
        self._data[group_id] = {"shared": bool(shared), "assistants": sorted(assistants)}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self._path)


class WelinkPoller:
    """Follow the recent-conversation list and read only groups shared with this assistant.

    Each tick spends one recent-conversation call plus one heavy call. History of a
    known shared group comes before classifying an unknown group. A group marked as
    not shared stays in the recent list as a recheck candidate, and those rechecks
    walk the whole list before repeating the front. An assistant added later clears
    that mark. The first history read after the flip asks welink-cli for the latest
    page once; later ticks continue from the new cursor.
    """

    def __init__(
        self,
        runner: WelinkRunner,
        account: str,
        cursors: CursorStore,
        *,
        rate_limit_backoff_seconds: float = 30,
        member_interval_seconds: float = 15,
        membership_recheck_seconds: float = 60,
        membership: MembershipStore | None = None,
    ) -> None:
        self._runner = runner
        self._account = account
        self._cursors = cursors
        self._membership = membership or MembershipStore(cursors.path.with_name("membership.json"))
        self._rate_limit_backoff_seconds = rate_limit_backoff_seconds
        self._member_interval_seconds = member_interval_seconds
        self._membership_recheck_seconds = membership_recheck_seconds
        self._member_error_seconds = 30.0
        self._snapshot: tuple[str, ...] = ()
        self._history_checked: set[str] = set()
        self._history_deferred: set[str] = set()
        self._history_backoff_until = 0.0
        self._member_backoff_until = 0.0
        self._recent_backoff_until = 0.0
        self._last_member_query = 0.0
        self._member_hold: dict[str, float] = {}
        self._member_recheck_after: dict[str, float] = {}
        self._recent_index: dict[str, int] = {}
        self._member_scan = 0
        self._latest_catchup: set[str] = set()

    def poll(self) -> list[WelinkHistoryMessage]:
        now = time.monotonic()
        group_ids = self._load_recent(now)
        if not group_ids:
            return []
        if tuple(group_ids) != self._snapshot:
            self._snapshot = tuple(group_ids)
            self._history_checked.clear()
            self._history_deferred.clear()
            self._note_recent_movement(group_ids)
        self._release_deferred(group_ids, now)
        history_group = self._history_target(group_ids, now)
        if history_group:
            return self._read_history(history_group, now)
        member_group = self._member_target(group_ids, now)
        if member_group:
            self._classify(member_group, now)
            return []
        recheck = self._recheck_target(group_ids, now)
        if recheck:
            return self._read_history(recheck, now)
        return []

    def commit(self, group_id: str, message_id: str) -> None:
        self._cursors.set(group_id, message_id)

    def _load_recent(self, now: float) -> list[str]:
        if now < self._recent_backoff_until:
            return list(self._snapshot)
        recent = self._runner.query_recent_conversations()
        detail = redact_cli_failure(getattr(recent, "stderr", "") or getattr(recent, "stdout", ""))
        if getattr(recent, "returncode", 0) not in (0, None):
            logger.warning(
                "[Welink] query-recent-conversation failed: code=%s %s",
                getattr(recent, "returncode", None),
                detail,
            )
            if "429" in detail:
                self._recent_backoff_until = now + self._rate_limit_backoff_seconds
            return list(self._snapshot)
        return parse_recent_group_ids(parse_cli_json(getattr(recent, "stdout", "") or ""))

    def _note_recent_movement(self, group_ids: list[str]) -> None:
        """A group that newly appears or moves forward is due for a membership recheck."""
        for index, group_id in enumerate(group_ids):
            previous = self._recent_index.get(group_id)
            if previous is None or index < previous:
                self._member_recheck_after.pop(group_id, None)
            self._recent_index[group_id] = index

    def _release_deferred(self, group_ids: list[str], now: float) -> None:
        if now < self._history_backoff_until or not self._history_deferred:
            return
        shared = [group_id for group_id in group_ids if self._is_shared(group_id)]
        if not shared or all(group_id in self._history_deferred for group_id in shared):
            self._history_deferred.clear()

    def _history_target(self, group_ids: list[str], now: float) -> str | None:
        if now < self._history_backoff_until:
            return None
        pending = [
            group_id
            for group_id in group_ids
            if self._is_shared(group_id)
            and group_id not in self._history_checked
            and group_id not in self._history_deferred
        ]
        return pending[0] if pending else None

    def _recheck_target(self, group_ids: list[str], now: float) -> str | None:
        if now < self._history_backoff_until:
            return None
        shared = [
            group_id
            for group_id in group_ids
            if self._is_shared(group_id) and group_id not in self._history_deferred
        ]
        if shared and all(group_id in self._history_checked for group_id in shared):
            return shared[0]
        return None

    def _member_target(self, group_ids: list[str], now: float) -> str | None:
        if now < self._member_backoff_until:
            return None
        if self._last_member_query and now - self._last_member_query < self._member_interval_seconds:
            return None
        unknowns = [
            group_id
            for group_id in group_ids
            if self._membership.get(group_id) is None and self._member_hold.get(group_id, 0.0) <= now
        ]
        if unknowns:
            return unknowns[0]
        count = len(group_ids)
        if count == 0:
            return None
        # Continue after the previous recheck. Starting over at the front lets the
        # first few groups consume the member budget again as soon as their cooldown
        # ends, so a group further down the recent list never gets a turn.
        start = self._member_scan % count
        for step in range(count):
            index = (start + step) % count
            group_id = group_ids[index]
            if not self._recheck_due(group_id, now):
                continue
            self._member_scan = index + 1
            return group_id
        return None

    def _recheck_due(self, group_id: str, now: float) -> bool:
        record = self._membership.get(group_id)
        if record is None or record[0]:
            return False
        if self._member_hold.get(group_id, 0.0) > now:
            return False
        return self._member_recheck_after.get(group_id, 0.0) <= now

    def _is_shared(self, group_id: str) -> bool:
        record = self._membership.get(group_id)
        return bool(record and record[0])

    def _classify(self, group_id: str, now: float) -> None:
        self._last_member_query = now
        previous = self._membership.get(group_id)
        try:
            result = self._runner.query_group_members(group_id)
        except Exception:
            logger.warning("[Welink] query-group-member failed: group_id=%s", group_id)
            self._member_hold[group_id] = now + self._member_error_seconds
            return
        detail = redact_cli_failure(getattr(result, "stderr", "") or getattr(result, "stdout", ""))
        if getattr(result, "returncode", 0) not in (0, None):
            logger.warning("[Welink] query-group-member failed: group_id=%s %s", group_id, detail)
            if "429" in detail:
                self._member_backoff_until = now + self._rate_limit_backoff_seconds
            self._member_hold[group_id] = now + self._member_error_seconds
            return
        payload = parse_cli_json(getattr(result, "stdout", "") or "")
        if isinstance(payload, dict) and payload.get("resultCode") not in (0, "0", None):
            logger.warning(
                "[Welink] query-group-member rejected group_id=%s code=%s",
                group_id,
                payload.get("resultCode"),
            )
            self._member_hold[group_id] = now + self._member_error_seconds
            return
        members = parse_group_members(payload)
        if not members:
            self._member_hold[group_id] = now + self._member_error_seconds
            return
        reader = self._account.strip()
        # A recent conversation means the reader is in the group. A member page that
        # does not list them is incomplete, so it must not be stored as "not shared".
        if reader and not any(member.account == reader for member in members):
            logger.warning("[Welink] query-group-member incomplete group_id=%s", group_id)
            self._member_hold[group_id] = now + self._member_error_seconds
            return
        shared = shares_reader_assistant(members, self._account)
        assistants = reader_assistant_accounts(members, self._account)
        logger.info(
            "[Welink] members group_id=%s count=%s assistants=%s shared=%s",
            group_id,
            len(members),
            len(assistants),
            shared,
        )
        self._membership.set(group_id, shared=shared, assistants=assistants)
        if shared and previous is not None and not previous[0]:
            self._latest_catchup.add(group_id)
            self._member_recheck_after.pop(group_id, None)
            logger.info("[Welink] group now shared with this assistant group_id=%s", group_id)
            return
        if not shared:
            self._member_recheck_after[group_id] = now + self._membership_recheck_seconds
            logger.info("[Welink] skip group without this assistant group_id=%s", group_id)

    def _read_history(self, group_id: str, now: float) -> list[WelinkHistoryMessage]:
        record = self._membership.get(group_id)
        assistants = set(record[1]) if record else set()
        cursor = self._cursors.get(group_id)
        catch_up = group_id in self._latest_catchup
        if catch_up:
            logger.info("[Welink] catch up latest history group_id=%s", group_id)
        history = self._runner.query_history(group_id, None if catch_up else cursor)
        detail = redact_cli_failure(getattr(history, "stderr", "") or getattr(history, "stdout", ""))
        if getattr(history, "returncode", 0) not in (0, None):
            logger.warning("[Welink] query-history-message failed: group_id=%s %s", group_id, detail)
            self._history_deferred.add(group_id)
            if "429" in detail:
                self._history_backoff_until = now + self._rate_limit_backoff_seconds
            return []
        self._latest_catchup.discard(group_id)
        messages = parse_history_messages(parse_cli_json(getattr(history, "stdout", "") or ""), group_id=group_id)
        newer, advanced = select_new_messages(
            messages,
            cursor,
            account=self._account,
            ignore_senders=assistants,
        )
        self._history_checked.add(group_id)
        if advanced and advanced != cursor and not newer:
            self._cursors.set(group_id, advanced)
            logger.info("[Welink] baseline group_id=%s cursor=%s", group_id, advanced)
            return []
        limit = _CATCHUP_LIMIT if catch_up else _DELIVER_LIMIT
        if len(newer) > limit:
            logger.info(
                "[Welink] keep recent messages group_id=%s kept=%s skipped=%s",
                group_id,
                limit,
                len(newer) - limit,
            )
            newer = newer[-limit:]
        if newer:
            logger.info("[Welink] new messages group_id=%s count=%s", group_id, len(newer))
        return newer
