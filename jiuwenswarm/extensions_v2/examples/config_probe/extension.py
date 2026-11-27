# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Query the public config catalog and demonstrate presentation/save policies."""

import json

from jiuwenswarm.extensions_v2.event_types.server_run_command import ServerRunCommandResult


class ConfigProbe:
    def register(self, registrar) -> None:
        registrar.register_event_handler("config.describe", self.describe, {"key": "rsi.enabled"})
        registrar.register_event_handler("config.set", self.before_set, {"key": "rsi.enabled"})
        registrar.register_event_handler("server.run_command", self.command, {"command": "config_probe"})

    async def describe(self, event, ops, next_handler):
        result = await next_handler(event)
        return {**result, "label": "RSI (config probe)",
                "description": "This sample refuses enabling RSI; uninstall it to remove that policy."}

    async def before_set(self, event, ops, next_handler):
        if event.event_input["value"] is True:
            return {"deny": "config_probe demonstration: enabling RSI is blocked"}
        return await next_handler(event)

    async def command(self, event, ops, next_handler):
        result = {"value": await ops.config.value("rsi.enabled"),
                  "items": await ops.config.items(), "snapshot": await ops.config.snapshot()}
        return ServerRunCommandResult(text=json.dumps(result, ensure_ascii=False, indent=2))


def create_extension():
    return ConfigProbe()
