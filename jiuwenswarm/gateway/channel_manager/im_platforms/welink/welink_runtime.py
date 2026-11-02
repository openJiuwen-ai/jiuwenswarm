# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Apply only changed WeLink assistant bindings; retain the shared reader."""
from __future__ import annotations

import asyncio

from .welink_binding import learn_assistant_account, normalize_welink_section
from .welink_cli import WelinkCli
from .welink_connect import WelinkChannel, WelinkConfig
from .welink_reader import WelinkReader
from pathlib import Path


class WelinkRuntime:
    def __init__(self, manager, router, handler):
        self.manager = manager
        self.router = router
        self.handler = handler
        self.channels: dict[str, WelinkChannel] = {}
        self.reader = None
        self.reader_task = None
        self._reader_settings = None

    async def _stop_channel(self, key: str) -> None:
        from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelKey
        channel = self.channels.pop(key)
        self.manager.unregister_channel(ChannelKey('welink', key))
        task = getattr(channel, 'start_task', None)
        if task is not None:
            task.cancel()
        await channel.stop()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _stop_reader(self) -> None:
        if self.reader_task is not None:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        self.reader_task = None
        self.reader = None

    async def apply(self, section) -> None:
        conf = normalize_welink_section(section)
        settings = (conf.get('account'), conf.get('reader_credential_dir'), conf.get('cli_path', 'welink-cli'),
                    conf.get('env', 'pro'), conf.get('poll_interval_seconds', 4))
        configs = {}
        if conf.get('enabled') and conf.get('cli_authorized'):
            for binding in conf['bindings']:
                if not binding.get('enabled'):
                    continue
                config = WelinkConfig(enabled=True, account=settings[0], agent_id=binding['persistent_agent_id'],
                    credential_dir=binding['credential_dir'], reader_credential_dir=settings[1],
                    cli_path=settings[2], env_name=settings[3], poll_interval_seconds=float(settings[4]),
                    assistant_credential_id=binding['assistant_credential_id'], assistant_secret=binding['assistant_secret'],
                    assistant_account=binding.get('assistant_account', ''), shared_reader=True,
                    legacy_identity_fallback=bool(binding.get('legacy_identity_fallback')))
                configs[config.assistant_credential_id] = config
        if settings != self._reader_settings or not configs:
            await self._stop_reader()
        for key in tuple(self.channels):
            if key not in configs or self.channels[key].config != configs[key]:
                await self._stop_channel(key)
        for key, config in configs.items():
            if key in self.channels:
                continue
            channel = WelinkChannel(config, self.router)
            channel.remember_account = (
                lambda account, agent_id=config.agent_id, credential_id=config.assistant_credential_id:
                learn_assistant_account(agent_id, credential_id, account)
            )
            channel.set_inbound_handler(self.handler)
            self.channels[key] = channel
            self.manager.register_channel(channel)
            channel.start_task = asyncio.create_task(channel.start(), name=f'welink-assistant-{config.agent_id}')
        for channel in self.channels.values():
            channel._shared_binding_count = len(self.channels)
        if self.channels and self.reader_task is None:
            cli = WelinkCli(settings[2], Path(settings[1]), env_name=settings[3])
            self.reader = WelinkReader(cli, settings[0], settings[1], float(settings[4]), self.channels)
            self.reader_task = asyncio.create_task(self.reader.run(), name='welink-shared-reader')
        self._reader_settings = settings

    async def stop(self) -> None:
        await self._stop_reader()
        for key in tuple(self.channels):
            await self._stop_channel(key)
