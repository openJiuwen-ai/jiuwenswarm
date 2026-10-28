# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One durable reader fans group history out to independently bound assistants."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .welink_connect import WelinkChannel, auth_recheck_due
from .welink_messages import WelinkHistoryMessage, WelinkMedia, select_new_messages
from .welink_poller import CursorStore, WelinkPoller

logger = logging.getLogger(__name__)


class WelinkReader:
    def __init__(self, cli, account: str, directory: str, interval: float, channels: dict[str, WelinkChannel]):
        self._cli = cli
        self.account = account
        self.directory = Path(directory)
        self.interval = interval
        self.channels = channels
        self._poller = WelinkPoller(cli, account, CursorStore(self.directory / 'cursors.json'))
        self._pending_path = self.directory / 'fanout-pending.json'
        self._seen_path = self.directory / 'assistant-observations.json'
        self._aliases_path = self.directory / 'message-aliases.json'
        self._aliases = json.loads(self._aliases_path.read_text()) if self._aliases_path.exists() else {}
        self._seen = set(json.loads(self._seen_path.read_text()) if self._seen_path.exists() else [])
        self._pending = json.loads(self._pending_path.read_text()) if self._pending_path.exists() else []
        # Upgrade the old single-reader pending batch without dropping unaccepted messages.
        old = self.directory / ('pending-' + hashlib.sha256(account.encode()).hexdigest()[:24] + '.json')
        if not self._pending_path.exists() and old.exists():
            if len(channels) != 1:
                raise ValueError('legacy WeLink pending batch has ambiguous assistant ownership')
            self._pending = [{'message': raw, 'targets': list(channels),
                              'target_agents': self._target_agents(channels), 'commit': True}
                             for raw in json.loads(old.read_text())]
            self._save(self._pending_path, self._pending)
        for job in self._pending:
            if 'target_agents' not in job:
                credentials = [job['credential']] if job.get('prepare_catchup') else job.get('targets', [])
                job['target_agents'] = self._target_agents(credentials)
        if self._pending:
            self._save(self._pending_path, self._pending)
        self._auth_ok_at = 0.0

    @staticmethod
    def _save(path: Path, value) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            temporary.chmod(0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    @staticmethod
    def _message(raw) -> WelinkHistoryMessage:
        values = dict(raw)
        values['media'] = tuple(WelinkMedia(**item) for item in values.get('media', ()))
        return WelinkHistoryMessage(**values)

    def _key(self, credential: str, group: str) -> str:
        return json.dumps([credential, self.channels[credential].config.agent_id, group])

    def _target_agents(self, credentials) -> dict[str, str]:
        return {key: self.channels[key].config.agent_id for key in credentials if key in self.channels}

    def _eligible(self, group: str) -> dict[str, WelinkChannel]:
        if not self._poller._is_shared(group):
            return {}
        record = self._poller._membership.get(group)
        accounts = set(record[1]) if record else set()
        return {key: channel for key, channel in tuple(self.channels.items()) if channel._own_assistants(accounts)}

    async def _refresh_group(self, group: str) -> bool:
        record = self._poller._membership.get(group)
        now = time.monotonic()
        due = getattr(self._poller, '_recheck_due', lambda *_: False)(group, now)
        stale = record is None or (record[0] and not self._poller._is_shared(group))
        if (due or stale) and getattr(self._poller, '_member_hold', {}).get(group, 0) <= now:
            await asyncio.to_thread(self._poller._classify, group, now)
            record = self._poller._membership.get(group)
        return record is not None and not (record[0] and not self._poller._is_shared(group))

    async def scan(self) -> None:
        if not self._pending:
            messages = await asyncio.to_thread(self._poller.poll)
            jobs = []
            for group in self._poller._snapshot:
                for credential, channel in self._eligible(group).items():
                    key = self._key(credential, group)
                    if key in self._seen:
                        continue
                    jobs.append({'prepare_catchup': True, 'group': group, 'credential': credential, 'seen_key': key,
                                 'target_agents': self._target_agents([credential])})
            for item in messages:
                targets = list(self._eligible(item.group_id))
                jobs.append({'message': asdict(item), 'targets': targets,
                             'target_agents': self._target_agents(targets), 'commit': True})
            self._pending = jobs
            self._save(self._pending_path, jobs)
        while self._pending:
            job = self._pending[0]
            if job.get('prepare_catchup'):
                group, credential = job['group'], job['credential']
                channel = self.channels.get(credential)
                if channel is None or job['target_agents'].get(credential) != channel.config.agent_id:
                    self._pending.pop(0)
                    self._save(self._pending_path, self._pending)
                    continue
                if not await self._refresh_group(group):
                    return
                if not self._eligible(group).get(credential):
                    self._pending.pop(0)
                    self._save(self._pending_path, self._pending)
                    continue
                history = await asyncio.to_thread(channel._history, group)
                record = self._poller._membership.get(group)
                catchup, _ = select_new_messages(history, None, account=self.account, ignore_senders=set(record[1]))
                now = datetime.now(timezone.utc).isoformat()
                catchup = [replace(item, received_at=item.received_at or now) for item in catchup]
                boundary = min((item.received_at for item in catchup), default=now)
                prepared = [{'message': asdict(item), 'targets': [credential], 'commit': False,
                             'first_observation': True, 'visible_from': boundary,
                             'target_agents': dict(job['target_agents']),
                             'seen_key': job['seen_key'] if index == len(catchup) - 1 else ''}
                            for index, item in enumerate(catchup)]
                if not prepared:
                    self._seen.add(job['seen_key'])
                    self._save(self._seen_path, sorted(self._seen))
                self._pending = prepared + self._pending[1:]
                self._save(self._pending_path, self._pending)
                continue
            original = self._message(job['message'])
            alias_key = json.dumps([original.group_id, original.message_id])
            canonical = job.get('canonical_message_id') or self._aliases.get(alias_key)
            if not canonical:
                canonical = original.message_id
                # Persist the archive/request key before any recipient can accept it.
                for channel in tuple(self.channels.values()):
                    if channel._link is not None:
                        channel._link.claim_polled(original.group_id, original.message_id, original.text, sender_id=original.sender_id)
                        candidate = channel._link.canonical_message_id(original.group_id, original.message_id)
                        if candidate != original.message_id:
                            canonical = candidate
                            break
            if self._aliases.get(alias_key) != canonical:
                self._aliases[alias_key] = canonical
                self._save(self._aliases_path, self._aliases)
            if job.get('canonical_message_id') != canonical:
                job['canonical_message_id'] = canonical
                self._save(self._pending_path, self._pending)
            message = replace(original, message_id=canonical)
            if not await self._refresh_group(original.group_id):
                return
            eligible = self._eligible(original.group_id)
            for credential in list(job['targets']):
                channel = eligible.get(credential)
                if channel is None or job['target_agents'].get(credential) != channel.config.agent_id:
                    job['targets'].remove(credential)
                    self._save(self._pending_path, self._pending)
                    continue
                accepted = await channel._deliver(message, first_observation=job.get('first_observation', False),
                                                  visible_from=job.get('visible_from', ''))
                if not accepted:
                    return
                job['targets'].remove(credential)
                self._save(self._pending_path, self._pending)
            if job.get('commit'):
                self._poller.commit(original.group_id, original.message_id)
            if job.get('seen_key'):
                self._seen.add(job['seen_key'])
                self._save(self._seen_path, sorted(self._seen))
            self._pending.pop(0)
            self._save(self._pending_path, self._pending)

    async def run(self) -> None:
        while True:
            try:
                channel = next(iter(self.channels.values()), None)
                now = time.monotonic()
                if channel is not None and auth_recheck_due(self._auth_ok_at, now):
                    self._auth_ok_at = now if await asyncio.to_thread(channel._logged_in) else 0
                if self._auth_ok_at:
                    await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('[Welink] shared reader scan unavailable')
            await asyncio.sleep(max(self.interval, 1))
