"""Conservatively poll known DingTalk groups using employee-scoped OAuth."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliScope
from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import DingTalkBinding, configured_dingtalk_bindings
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError


logger = logging.getLogger(__name__)
_OVERLAP = timedelta(seconds=30)
_READ_BUDGET = 8
_GroupKey = tuple[str, str, str]
_RecipientKey = tuple[str, str, str, str, str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('A timezone-aware polling clock is required')
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class _Recipient:
    binding: DingTalkBinding
    dispatch_after: datetime


@dataclass
class _Group:
    key: _GroupKey
    conversation_id: str
    recipients: list[_Recipient]


@dataclass
class _ReadBudget:
    used: int = 0


class DingTalkGroupPoller:
    """Read only active adaptive bindings, then use the normal history ingress."""

    def __init__(
        self, manager: Any, inbound: Any, *,
        config_loader: Callable[[], Mapping[str, Any]] | None = None,
        interval: float = 10.0,
        clock: Callable[[], datetime] = _utc_now,
        state_root: Path | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError('Polling interval must be positive')
        self._manager = manager
        self._inbound = inbound
        self._config_loader = config_loader
        self._interval = interval
        self._clock = clock
        self._state_root = Path(state_root) if state_root is not None else (
            Path(manager._sessions.db_path).parent / 'dingtalk_group_polling'
        )
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._floors: dict[_RecipientKey, datetime] = {}
        self._progress: dict[_GroupKey, datetime] = {}
        self._recipient_progress: dict[_RecipientKey, datetime] = {}
        self._backoff: dict[tuple[str, ...], tuple[int, datetime]] = {}
        self._last_key: _GroupKey | None = None
        self._preferred_readers: dict[_GroupKey, tuple[str, str]] = {}
        self._last_readers: dict[_GroupKey, tuple[str, str]] = {}

    async def start(self) -> None:
        """Start at most one background loop; a closed poller cannot restart."""
        if not self._closed and self._task is None:
            self._task = asyncio.create_task(self._run(), name='dingtalk-group-history')

    async def aclose(self) -> None:
        """Cancel reads and wait until any manually triggered poll stops dispatching."""
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            pass

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.poll_once()
            except Exception as exc:
                # Exception strings may contain upstream response bodies or credentials.
                logger.warning('DingTalk group polling cycle failed (%s)', type(exc).__name__)
            await asyncio.sleep(self._interval)

    async def _bindings(self) -> tuple[DingTalkBinding, ...]:
        config = await asyncio.to_thread(self._config_loader) if self._config_loader is not None else None
        return await asyncio.to_thread(configured_dingtalk_bindings, config)

    def _eligible_accesses(self, binding: DingTalkBinding) -> tuple[Any, ...]:
        service = self._manager._service
        ns, agent = binding.namespace_id, binding.agent_id
        if not service.get_colleague(ns, agent).enabled:
            return ()
        if service.read_settings(ns, agent).group_reply_mode != 'adaptive':
            return ()
        connection = service.connection_of(ns, agent)
        if connection is None or (connection.channel, connection.connection_key) != ('dingtalk', binding.app_id):
            return ()
        return tuple(access for access in self._manager.group_chats.active_accesses(ns, agent)
                     if access.app_id == binding.app_id)

    def _ready(self, key: tuple[str, ...], now: datetime) -> bool:
        retry = self._backoff.get(key)
        return retry is None or retry[1] <= now

    def _defer(self, key: tuple[str, ...], now: datetime) -> None:
        failures = min(self._backoff.get(key, (0, now))[0] + 1, 6)
        self._backoff[key] = (failures, now + timedelta(seconds=min(300, self._interval * 2 ** failures)))

    async def _discover(self, now: datetime) -> dict[_GroupKey, _Group]:
        groups: dict[_GroupKey, _Group] = {}
        observed: set[_RecipientKey] = set()
        for binding in await self._bindings():
            if self._closed:
                break
            scope_key = ('scope', binding.namespace_id, binding.agent_id, binding.app_id)
            existing = {key for key in self._floors
                        if key[0] == binding.namespace_id
                        and key[3:] == (binding.agent_id, binding.app_id)}
            try:
                accesses = await asyncio.to_thread(self._eligible_accesses, binding)
                if not accesses:
                    continue
                active_groups = {access.group_id for access in accesses}
                existing = {key for key in existing if key[2] in active_groups}
                if not self._ready(scope_key, now):
                    observed.update(existing)
                    continue
                scope = DingTalkCliScope(binding.namespace_id, binding.agent_id)
                reader = await self._manager.dingtalk_cli_runtime.group_history_identity(scope)
                if not isinstance(reader, str) or not reader:
                    # A completed user login should be noticed on the next cycle.
                    # Only uncertain/transient failures need exponential backoff.
                    self._backoff.pop(scope_key, None)
                    continue
                existing = {key for key in existing if key[1] == reader}
                self._backoff.pop(scope_key, None)
                for access in accesses:
                    group = await asyncio.to_thread(
                        self._manager.group_chats.get, binding.namespace_id, access.group_id,
                    )
                    if group.platform != 'dingtalk':
                        continue
                    key = (binding.namespace_id, reader, access.group_id)
                    recipient_key = (*key, binding.agent_id, binding.app_id)
                    observed.add(recipient_key)
                    if key not in self._progress:
                        self._progress[key] = await asyncio.to_thread(self._load_progress, key, now)
                    self._recipient_progress.setdefault(recipient_key, self._progress[key])
                    if recipient_key not in self._floors:
                        logger.info('DingTalk employee group observation started: agent=%s group=%s after=%s',
                                    binding.agent_id, access.group_id, now.isoformat())
                    floor = self._floors.setdefault(recipient_key, now)
                    if key not in groups:
                        groups[key] = _Group(key, group.platform_chat_id, [])
                    groups[key].recipients.append(_Recipient(binding, floor))
            except Exception as exc:
                # Transient lookup failures must not turn outage-period messages into history.
                observed.update(existing)
                self._defer(scope_key, _utc(self._clock()))
                logger.warning('DingTalk group polling authorization failed (%s)', type(exc).__name__)
        # Reset only confirmed lost access; transient failures retain their original floor.
        self._floors = {key: value for key, value in self._floors.items() if key in observed}
        self._recipient_progress = {key: value for key, value in self._recipient_progress.items() if key in observed}
        return groups

    async def _still_authorized(self, group: _Group, recipient: _Recipient) -> bool:
        binding = recipient.binding
        if binding not in await self._bindings():
            return False
        accesses = await asyncio.to_thread(self._eligible_accesses, binding)
        if not any(access.group_id == group.key[2] for access in accesses):
            return False
        current_group = await asyncio.to_thread(
            self._manager.group_chats.get, binding.namespace_id, group.key[2],
        )
        if (current_group.platform, current_group.platform_chat_id) != ('dingtalk', group.conversation_id):
            return False
        reader = await self._manager.dingtalk_cli_runtime.group_history_identity(
            DingTalkCliScope(binding.namespace_id, binding.agent_id),
        )
        return reader == group.key[1] and not self._closed

    def _state_path(self, key: _GroupKey) -> Path:
        digest = hashlib.sha256(json.dumps(key, ensure_ascii=True).encode('utf-8')).hexdigest()
        return self._state_root / f'{digest}.json'

    def _load_progress(self, key: _GroupKey, now: datetime) -> datetime:
        try:
            data = json.loads(self._state_path(key).read_text(encoding='utf-8'))
            if data.get('version') != 1:
                raise ValueError('Unknown polling checkpoint version')
            value = _utc(datetime.fromisoformat(data['since']))
            if value.year < 2000:
                raise ValueError('Invalid polling checkpoint boundary')
            return min(value, now - _OVERLAP)
        except FileNotFoundError:
            return now - _OVERLAP
        except (ValueError, KeyError, TypeError, OSError) as exc:
            logger.warning('DingTalk group polling checkpoint unavailable (%s)', type(exc).__name__)
            return now - _OVERLAP

    def _save_progress(self, key: _GroupKey, since: datetime) -> None:
        self._state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != 'nt':
            self._state_root.chmod(0o700)
        descriptor, name = tempfile.mkstemp(prefix='.checkpoint-', dir=self._state_root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump({'version': 1, 'since': since.isoformat()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_path(key))
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _partial_boundary(payload: dict[str, Any]) -> datetime | None:
        cursor = payload.get('nextCursor')
        if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0:
            return datetime.fromtimestamp(cursor / 1000, timezone.utc) - timedelta(milliseconds=1)
        times = []
        for message in payload['messages']:
            value = message.get('createTime')
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.utcoffset() is None:
                    parsed = parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
                times.append(parsed.astimezone(timezone.utc))
            except (AttributeError, ValueError, TypeError):
                return None
        return max(times) - timedelta(milliseconds=1) if times else None

    async def _mark_absent(self, group: _Group, recipient: _Recipient, observed_at: datetime) -> None:
        binding = recipient.binding
        self._floors.pop((*group.key, binding.agent_id, binding.app_id), None)
        # A delayed read must never retire a different app that replaced this binding.
        if binding not in await self._bindings():
            return
        connection = await asyncio.to_thread(
            self._manager._service.connection_of, binding.namespace_id, binding.agent_id,
        )
        if connection is None or (connection.channel, connection.connection_key) != ('dingtalk', binding.app_id):
            return
        source = hashlib.sha256(json.dumps((group.key, binding.app_id, binding.robot_code)).encode()).hexdigest()
        await asyncio.to_thread(
            self._manager.group_chats.update_membership,
            binding.namespace_id, binding.agent_id, group.key[2], binding.app_id,
            status='left', observed_at=observed_at, source_ref=f'dingtalk:bot-list:{source}',
        )

    async def _read(self, group: _Group, query_started: datetime, budget: _ReadBudget) -> None:
        key = group.key
        if key not in self._progress:
            self._progress[key] = await asyncio.to_thread(self._load_progress, key, query_started)
        # Revalidate before using a candidate employee's OAuth to make the request.
        readers = []
        for recipient in group.recipients:
            if await self._still_authorized(group, recipient):
                readers.append(recipient)
        if not readers or self._closed:
            return
        # Shared reads cannot let one employee consume another employee's backlog.
        # Paused employees keep their cursor and catch up when their access is revalidated.
        since = min(self._recipient_progress.get(
            (*key, item.binding.agent_id, item.binding.app_id), self._progress[key],
        ) for item in readers)
        preferred = self._preferred_readers.get(key)
        last = self._last_readers.get(key)
        identities = [(item.binding.agent_id, item.binding.app_id) for item in readers]
        if preferred in identities:
            index = identities.index(preferred)
            readers = [readers[index], *readers[:index], *readers[index + 1:]]
        elif last in identities:
            index = identities.index(last) + 1
            readers = readers[index:] + readers[:index]
        payload = None
        successful_reader = None
        missing_codes: set[str] = set()
        attempted: set[tuple[str, str]] = set()
        for recipient in readers:
            if budget.used >= _READ_BUDGET or self._closed:
                break
            binding = recipient.binding
            scope_key = (binding.namespace_id, binding.agent_id)
            if scope_key in attempted:
                continue
            attempted.add(scope_key)
            identity = (binding.agent_id, binding.app_id)
            self._last_readers[key] = identity
            budget.used += 1
            try:
                payload = await self._manager.dingtalk_cli_runtime.read_group_history(
                    DingTalkCliScope(binding.namespace_id, binding.agent_id),
                    conversation_id=group.conversation_id, since=since.isoformat(),
                )
            except PersistentAgentError as exc:
                if exc.code != 'DINGTALK_CLI_GROUP_BOT_NOT_PRESENT':
                    raise
                missing_codes.add(binding.robot_code)
                if self._preferred_readers.get(key) == identity:
                    self._preferred_readers.pop(key, None)
                await self._mark_absent(group, recipient, query_started)
                continue
            successful_reader = identity
            break
        if self._closed:
            return
        if payload is None:
            self._defer(('group', *key), _utc(self._clock()))
            return
        if (
            not isinstance(payload, dict) or payload.get('readerKey') != key[1]
            or not isinstance(payload.get('messages'), list)
            or any(not isinstance(message, dict) for message in payload['messages'])
            or not isinstance(payload.get('complete'), bool) or not isinstance(payload.get('hasMore'), bool)
            or not isinstance(payload.get('botCodes'), list)
            or any(not isinstance(code, str) or not code.strip() for code in payload['botCodes'])
        ):
            raise ValueError('Unverified DingTalk polling response')
        if successful_reader is not None:
            self._preferred_readers[key] = successful_reader
        bot_codes = set(payload['botCodes']) - missing_codes
        recipients = []
        for recipient in readers:
            if recipient.binding.robot_code not in bot_codes:
                if recipient.binding.robot_code not in missing_codes:
                    await self._mark_absent(group, recipient, query_started)
                continue
            if await self._still_authorized(group, recipient):
                recipients.append(recipient)
        if not recipients or self._closed:
            self._defer(('group', *key), _utc(self._clock()))
            return
        if payload['messages']:
            logger.info('DingTalk group history batch: group=%s since=%s recipients=%s count=%s',
                        key[2], since.isoformat(),
                        [(item.binding.agent_id, item.dispatch_after.isoformat()) for item in recipients],
                        len(payload['messages']))
        for message in payload['messages']:
            for recipient in recipients:
                if self._closed:
                    return
                disposition = await self._inbound.handle_history_message(
                    recipient.binding, message, group_id=key[2], dispatch_after=recipient.dispatch_after,
                )
                if disposition != InboundDisposition.HANDLED:
                    self._defer(('group', *key), _utc(self._clock()))
                    return
        bot_senders = payload.get('botSenders', {})
        verified_senders = {
            recipient.binding.app_id: 'dingtalk-open:' + bot_senders[recipient.binding.robot_code]
            for recipient in recipients if recipient.binding.robot_code in bot_senders
        }
        if verified_senders and payload['complete'] and not payload['hasMore'] and not self._closed:
            await asyncio.to_thread(
                self._manager.group_archive.reconcile_dingtalk_deliveries, key[0], key[2], verified_senders,
                observed_since=since, observed_through=query_started,
            )
        if payload['complete'] and not payload['hasMore']:
            boundary = query_started - _OVERLAP
        else:
            partial = self._partial_boundary(payload)
            if partial is None or min(partial, query_started) <= since:
                self._defer(('group', *key), _utc(self._clock()))
                return
            boundary = min(partial, query_started)
        if self._closed:
            return
        boundary = max(since, boundary)
        shared_boundary = max(self._progress[key], boundary)
        await asyncio.to_thread(self._save_progress, key, shared_boundary)
        self._progress[key] = shared_boundary
        for recipient in recipients:
            recipient_key = (*key, recipient.binding.agent_id, recipient.binding.app_id)
            self._recipient_progress[recipient_key] = max(self._recipient_progress.get(recipient_key, since), boundary)
        self._backoff.pop(('group', *key), None)

    async def poll_once(self) -> int:
        """Attempt at most eight group reads, fairly rotating among eligible groups."""
        async with self._lock:
            if self._closed:
                return 0
            now = _utc(self._clock())
            groups = await self._discover(now)
            keys = sorted(groups)
            if self._last_key is not None:
                keys = [key for key in keys if key > self._last_key] + [key for key in keys if key <= self._last_key]
            budget = _ReadBudget()
            for key in keys:
                if self._closed or budget.used >= _READ_BUDGET:
                    break
                retry_key = ('group', *key)
                if not self._ready(retry_key, now):
                    continue
                self._last_key = key
                try:
                    await self._read(groups[key], _utc(self._clock()), budget)
                except Exception as exc:
                    self._defer(retry_key, _utc(self._clock()))
                    logger.warning('DingTalk group history read failed (%s)', type(exc).__name__)
            return budget.used
