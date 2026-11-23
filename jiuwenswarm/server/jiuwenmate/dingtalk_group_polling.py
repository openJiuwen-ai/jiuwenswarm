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

from jiuwenswarm.server.jiuwenmate.dingtalk_cli_runtime import DingTalkCliScope, valid_history_page_fields
from jiuwenswarm.server.jiuwenmate.dingtalk_inbound import DingTalkBinding, configured_dingtalk_bindings
from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import InboundDisposition
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError


logger = logging.getLogger(__name__)
_OVERLAP = timedelta(seconds=30)
_INITIAL_CATCHUP = timedelta(minutes=5)
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
    """Reconcile known Bot memberships separately from adaptive history reads."""

    def __init__(
        self, manager: Any, inbound: Any, *,
        config_loader: Callable[[], Mapping[str, Any]] | None = None,
        interval: float = 10.0,
        membership_interval: float = 60.0,
        clock: Callable[[], datetime] = _utc_now,
        state_root: Path | None = None,
    ) -> None:
        if interval <= 0 or membership_interval <= 0:
            raise ValueError('Polling interval must be positive')
        self._manager = manager
        self._inbound = inbound
        self._config_loader = config_loader
        self._interval = interval
        self._membership_interval = membership_interval
        self._clock = clock
        self._state_root = Path(state_root) if state_root is not None else (
            Path(manager._sessions.db_path).parent / 'dingtalk_group_polling'
        )
        self._task: asyncio.Task[None] | None = None
        self._membership_task: asyncio.Task[None] | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._membership_lock = asyncio.Lock()
        self._last_membership_key: tuple[str, ...] | None = None
        self._floors: dict[_RecipientKey, datetime] = {}
        self._progress: dict[_GroupKey, datetime] = {}
        self._recipient_progress: dict[_RecipientKey, datetime] = {}
        self._backoff: dict[tuple[str, ...], tuple[int, datetime]] = {}
        self._last_key: _GroupKey | None = None
        self._preferred_readers: dict[_GroupKey, tuple[str, str]] = {}
        self._last_readers: dict[_GroupKey, tuple[str, str]] = {}
        self._discovery: dict[tuple[str, ...], tuple[datetime, int | None]] = {}
        self._discovery_failed: set[tuple[str, ...]] = set()
        self._authorization_started: dict[tuple[str, ...], datetime] = {}
        self._listening: set[tuple[str, str, str, str]] = set()
        self._read_failed: set[tuple[str, str, str, str]] = set()

    async def start(self) -> None:
        """Start at most one background loop; a closed poller cannot restart."""
        if not self._closed and self._task is None:
            self._task = asyncio.create_task(self._run(), name='dingtalk-group-history')
            self._membership_task = asyncio.create_task(self._run_memberships(), name='dingtalk-group-membership')

    async def aclose(self) -> None:
        """Cancel reads and wait until any manually triggered poll stops dispatching."""
        self._closed = True
        tasks = tuple(task for task in (self._task, self._membership_task) if task is not None)
        self._task = None
        self._membership_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            pass
        async with self._membership_lock:
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

    async def _run_memberships(self) -> None:
        while not self._closed:
            try:
                await self.poll_memberships_once()
            except Exception as exc:
                logger.warning('DingTalk group membership cycle failed (%s)', type(exc).__name__)
            await asyncio.sleep(self._membership_interval)

    def _membership_accesses(self, binding: DingTalkBinding) -> tuple[Any, ...]:
        """Include uncertain known bindings without depending on group reply mode."""
        service = self._manager.service
        connection = service.connection_of(binding.namespace_id, binding.agent_id)
        if connection is None:
            service.get_config(binding.namespace_id, binding.agent_id)
        elif (connection.channel, connection.connection_key) != ('dingtalk', binding.app_id):
            return ()
        groups = self._manager.group_chats
        accesses = list(groups.active_accesses(binding.namespace_id, binding.agent_id))
        accesses.extend(groups.unresolved_accesses(binding.namespace_id, binding.agent_id))
        return tuple(access for access in accesses if access.app_id == binding.app_id)

    async def poll_memberships_once(self) -> int:
        """Check at most eight known Bot bindings, without reading message bodies."""
        from jiuwenswarm.common.im_binding_lock import im_binding_lock

        inspect = getattr(self._manager.dingtalk_cli_runtime, 'inspect_group_membership', None)
        async with self._membership_lock:
            if self._closed or not callable(inspect):
                return 0
            choices = {}
            for binding in await self._bindings():
                try:
                    accesses = await asyncio.to_thread(self._membership_accesses, binding)
                    for access in accesses:
                        group = await asyncio.to_thread(
                            self._manager.group_chats.get, binding.namespace_id, access.group_id,
                        )
                        if group.platform == 'dingtalk':
                            key = (binding.namespace_id, binding.agent_id, binding.app_id, access.group_id)
                            choices[key] = (binding, access, group)
                except Exception as exc:
                    logger.warning('DingTalk membership binding unavailable (%s)', type(exc).__name__)
            keys = sorted(choices)
            if self._last_membership_key is not None:
                keys = ([key for key in keys if key > self._last_membership_key]
                        + [key for key in keys if key <= self._last_membership_key])
            used = 0
            for key in keys:
                if self._closed or used >= _READ_BUDGET:
                    break
                binding, access, group = choices[key]
                async with im_binding_lock('dingtalk', binding.app_id):
                    if binding not in await self._bindings():
                        continue
                    if access not in await asyncio.to_thread(self._membership_accesses, binding):
                        continue
                    observed_at = _utc(self._clock())
                    self._last_membership_key = key
                    used += 1
                    try:
                        proof = await inspect(
                            DingTalkCliScope(binding.namespace_id, binding.agent_id, app_id=binding.app_id),
                            conversation_id=group.platform_chat_id,
                        )
                    except Exception as exc:
                        logger.warning('DingTalk membership probe unavailable (%s)', type(exc).__name__)
                        proof = {'status': 'unknown'}
                    if self._closed or binding not in await self._bindings():
                        continue
                    if access not in await asyncio.to_thread(self._membership_accesses, binding):
                        continue
                    status = proof.get('status') if isinstance(proof, dict) else None
                    if status not in {'active', 'left'}:
                        # A failed observation does not revoke the last trusted membership fact.
                        logger.warning('DingTalk membership could not be refreshed: agent=%s group=%s',
                                       binding.agent_id, access.group_id)
                        continue
                    if status == 'left' and status != access.status:
                        await asyncio.to_thread(
                            self._manager.group_chats.update_membership,
                            binding.namespace_id, binding.agent_id, access.group_id, binding.app_id,
                            status=status, observed_at=observed_at, source_ref='dingtalk:membership-probe',
                            expected_revision=access.revision,
                        )
            return used

    def _eligible_binding(self, binding: DingTalkBinding) -> bool:
        return self._inbound.accepts_group_polling(binding)

    def _eligible_accesses(self, binding: DingTalkBinding) -> tuple[Any, ...]:
        if not self._eligible_binding(binding):
            return ()
        ns, agent = binding.namespace_id, binding.agent_id
        return tuple(access for access in self._manager.group_chats.active_accesses(ns, agent)
                     if access.app_id == binding.app_id)

    def status(self, namespace_id: str, agent_id: str, app_id: str) -> dict[str, Any]:
        """Report group observation independently from the account's OAuth status."""
        binding = DingTalkBinding(namespace_id=namespace_id, agent_id=agent_id, app_id=app_id, robot_code=app_id)
        identity = (namespace_id, agent_id, app_id)
        if self._closed:
            return {'status': 'unavailable', 'group_count': 0}
        if not self._manager.service.get_colleague(namespace_id, agent_id).enabled:
            return {'status': 'disabled', 'group_count': 0}
        if self._manager.service.read_settings(namespace_id, agent_id).group_reply_mode != 'adaptive':
            return {'status': 'mentions_only', 'group_count': 0}
        group_ids = {access.group_id for access in self._eligible_accesses(binding)}
        count = len(group_ids)
        scans = [key for key in self._discovery if key[:1] == (namespace_id,) and key[2:] == (agent_id, app_id)]
        if (
            any((*identity, group) in self._read_failed for group in group_ids)
            or any(key in self._discovery_failed for key in scans)
        ):
            state = 'error'
        elif count and all((*identity, group) in self._listening for group in group_ids):
            state = 'listening'
        elif scans and all(self._discovery[key][1] is None for key in scans) and not count:
            state = 'no_groups'
        else:
            state = 'discovering'
        return {'status': state, 'group_count': count}

    def _forget_authorization(self, binding: DingTalkBinding) -> None:
        identity = (binding.namespace_id, binding.agent_id, binding.app_id)
        self._authorization_started = {key: value for key, value in self._authorization_started.items()
                                       if (key[0], *key[2:]) != identity}
        self._discovery = {key: value for key, value in self._discovery.items() if (key[0], *key[2:]) != identity}
        self._discovery_failed.intersection_update(self._discovery)
        self._floors = {key: value for key, value in self._floors.items() if (key[0], *key[3:]) != identity}
        self._recipient_progress = {key: value for key, value in self._recipient_progress.items()
                                    if (key[0], *key[3:]) != identity}
        self._listening = {key for key in self._listening if key[:3] != identity}
        self._read_failed = {key for key in self._read_failed if key[:3] != identity}

    async def _discover_accesses(self, binding: DingTalkBinding, reader: str, now: datetime) -> bool | None:
        discover = getattr(self._manager.dingtalk_cli_runtime, 'discover_groups', None)
        if not callable(discover):
            return
        key = (binding.namespace_id, reader, binding.agent_id, binding.app_id)
        due, cursor = self._discovery.get(key, (now, None))
        if due > now:
            return
        try:
            page = await discover(
                DingTalkCliScope(binding.namespace_id, binding.agent_id, app_id=binding.app_id), cursor=cursor,
            )
            if page.get('readerKey') != reader:
                raise ValueError('Group discovery reader changed')
            from jiuwenswarm.common.im_binding_lock import im_binding_lock
            async with im_binding_lock('dingtalk', binding.app_id):
                if self._closed or binding not in await self._bindings():
                    return False
                if not await asyncio.to_thread(self._eligible_binding, binding):
                    return False
                current = await self._manager.dingtalk_cli_runtime.group_history_identity(
                    DingTalkCliScope(binding.namespace_id, binding.agent_id, app_id=binding.app_id),
                )
                if current != reader or self._closed:
                    return False
                confirmed_at = _utc(self._clock())
                for item in page['groups']:
                    group = await asyncio.to_thread(
                        self._manager.group_chats.resolve,
                        binding.namespace_id, 'dingtalk', item['conversation_id'], now=confirmed_at,
                    )
                    active_apps = await asyncio.to_thread(
                        self._manager.group_chats.active_apps,
                        binding.namespace_id, binding.agent_id, group.group_chat_id,
                    )
                    if binding.app_id not in active_apps:
                        visible_from = max(self._authorization_started[key], confirmed_at - _INITIAL_CATCHUP)
                        await asyncio.to_thread(
                            self._manager.group_chats.observe_access, binding.namespace_id, binding.agent_id,
                            group.group_chat_id, binding.app_id, observed_at=confirmed_at,
                            visible_from=visible_from, source_ref='dingtalk:discovery:' + reader,
                        )
                        active_apps = await asyncio.to_thread(
                            self._manager.group_chats.active_apps,
                            binding.namespace_id, binding.agent_id, group.group_chat_id,
                        )
                    if binding.app_id not in active_apps:
                        # A confirmed rejoin must not replay messages from its departed interval.
                        await asyncio.to_thread(
                            self._manager.group_chats.update_membership, binding.namespace_id, binding.agent_id,
                            group.group_chat_id, binding.app_id, status='active', observed_at=confirmed_at,
                            source_ref='dingtalk:discovery:' + reader,
                        )
                    if item['name']:
                        await asyncio.to_thread(
                            self._manager.group_chats.set_name, binding.namespace_id, group.group_chat_id, item['name'],
                        )
            delay = self._interval if page['hasMore'] else 60
            self._discovery[key] = (_utc(self._clock()) + timedelta(seconds=delay), page['nextCursor'])
            if page['failed']:
                self._discovery_failed.add(key)
            elif cursor is None:
                self._discovery_failed.discard(key)
            return True
        except Exception as exc:
            self._discovery[key] = (_utc(self._clock()) + timedelta(seconds=20), cursor)
            self._discovery_failed.add(key)
            logger.warning('DingTalk group discovery failed (%s)', type(exc).__name__)

    def _ready(self, key: tuple[str, ...], now: datetime) -> bool:
        retry = self._backoff.get(key)
        return retry is None or retry[1] <= now

    def _defer(self, key: tuple[str, ...], now: datetime) -> None:
        failures = min(self._backoff.get(key, (0, now))[0] + 1, 6)
        self._backoff[key] = (failures, now + timedelta(seconds=min(300, self._interval * 2 ** failures)))

    async def _discover(self, now: datetime) -> dict[_GroupKey, _Group]:
        groups: dict[_GroupKey, _Group] = {}
        observed: set[_RecipientKey] = set()
        authorized: set[tuple[str, ...]] = set()
        for binding in await self._bindings():
            if self._closed:
                break
            scope_key = ('scope', binding.namespace_id, binding.agent_id, binding.app_id)
            recipient = (binding.namespace_id, binding.agent_id, binding.app_id)
            existing = {key for key in self._floors if (key[0], *key[3:]) == recipient}
            previous_scopes = {key for key in self._authorization_started if (key[0], *key[2:]) == recipient}
            try:
                if not await asyncio.to_thread(self._eligible_binding, binding):
                    continue
                if not self._ready(scope_key, now):
                    observed.update(existing)
                    authorized.update(previous_scopes)
                    continue
                scope = DingTalkCliScope(binding.namespace_id, binding.agent_id, app_id=binding.app_id)
                reader = await self._manager.dingtalk_cli_runtime.group_history_identity(scope)
                if not isinstance(reader, str) or not reader:
                    # A completed user login should be noticed on the next cycle.
                    # Only uncertain/transient failures need exponential backoff.
                    self._backoff.pop(scope_key, None)
                    self._forget_authorization(binding)
                    continue
                authorization_key = (binding.namespace_id, reader, binding.agent_id, binding.app_id)
                authorized.add(authorization_key)
                self._authorization_started.setdefault(authorization_key, _utc(self._clock()))
                stale = {
                    key for key in self._discovery if (key[0], *key[2:]) == recipient and key[1] != reader
                }
                if stale:
                    self._discovery = {key: value for key, value in self._discovery.items() if key not in stale}
                    self._discovery_failed.difference_update(stale)
                    identity = (binding.namespace_id, binding.agent_id, binding.app_id)
                    self._listening = {key for key in self._listening if key[:3] != identity}
                    self._read_failed = {key for key in self._read_failed if key[:3] != identity}
                if await self._discover_accesses(binding, reader, now) is False:
                    self._forget_authorization(binding)
                    authorized.discard(authorization_key)
                    continue
                accesses = await asyncio.to_thread(self._eligible_accesses, binding)
                active_groups = {access.group_id for access in accesses}
                existing = {key for key in existing if key[2] in active_groups}
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
                    catchup_start = max(
                        self._authorization_started[authorization_key], _utc(self._clock()) - _INITIAL_CATCHUP,
                    )
                    discovered = getattr(access, 'source_ref', '').startswith('dingtalk:discovery:')
                    readable_from = max(catchup_start, access.visible_from) if discovered else max(
                        now, getattr(access, 'visible_from', now),
                    )
                    first_observation = recipient_key not in self._floors
                    floor = self._floors.setdefault(recipient_key, readable_from)
                    self._recipient_progress.setdefault(recipient_key, min(self._progress[key], floor))
                    if first_observation:
                        logger.info('DingTalk employee group observation started: agent=%s group=%s after=%s',
                                    binding.agent_id, access.group_id, floor.isoformat())
                    if key not in groups:
                        groups[key] = _Group(key, group.platform_chat_id, [])
                    groups[key].recipients.append(_Recipient(binding, floor))
            except Exception as exc:
                # Transient lookup failures must not turn outage-period messages into history.
                observed.update(existing)
                authorized.update(previous_scopes)
                self._defer(scope_key, _utc(self._clock()))
                logger.warning('DingTalk group polling authorization failed (%s)', type(exc).__name__)
        # Reset only confirmed lost access; transient failures retain their original floor.
        self._floors = {key: value for key, value in self._floors.items() if key in observed}
        self._recipient_progress = {key: value for key, value in self._recipient_progress.items() if key in observed}
        self._authorization_started = {
            key: value for key, value in self._authorization_started.items() if key in authorized
        }
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
            DingTalkCliScope(binding.namespace_id, binding.agent_id, app_id=binding.app_id),
        )
        if reader != group.key[1]:
            self._forget_authorization(binding)
            return False
        return not self._closed

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
        with tempfile.TemporaryDirectory(prefix='.checkpoint-', dir=self._state_root) as directory:
            temporary = Path(directory) / 'checkpoint.json'
            with temporary.open('x', encoding='utf-8') as stream:
                if os.name != 'nt':
                    temporary.chmod(0o600)
                json.dump({'version': 1, 'since': since.isoformat()}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_path(key))

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
        if not await asyncio.to_thread(self._inbound.matches_connection, binding):
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
                    DingTalkCliScope(binding.namespace_id, binding.agent_id, app_id=binding.app_id),
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
        if not valid_history_page_fields(payload) or payload.get('readerKey') != key[1]:
            raise ValueError('Unverified DingTalk polling response')
        valid_messages = all(isinstance(message, dict) for message in payload['messages'])
        if not valid_messages or any(not isinstance(code, str) or not code.strip() for code in payload['botCodes']):
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
        verified_complete_page = verified_senders and payload['complete'] and not payload['hasMore']
        if verified_complete_page and not self._closed:
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
            identity = (recipient.binding.namespace_id, recipient.binding.agent_id, recipient.binding.app_id, key[2])
            self._listening.add(identity)
            self._read_failed.discard(identity)
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
                    self._read_failed.update(
                        (item.binding.namespace_id, item.binding.agent_id, item.binding.app_id, key[2])
                        for item in groups[key].recipients
                    )
                    self._defer(retry_key, _utc(self._clock()))
                    safe_codes = {'DINGTALK_CLI_AUTH_BUSY', 'DINGTALK_CLI_GROUP_HISTORY_UNAVAILABLE',
                                  'DINGTALK_CLI_GROUP_HISTORY_INVALID', 'DINGTALK_CLI_USER_AUTH_REQUIRED'}
                    code = getattr(exc, 'code', None)
                    logger.warning('DingTalk group history read failed (%s, %s, cause=%s)', type(exc).__name__,
                                   code if code in safe_codes else 'unknown', type(exc.__cause__).__name__)
            return budget.used
