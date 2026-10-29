# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run the actual channel update blocks with a connector import tripwire."""

import ast
import asyncio
import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def _channel_update(name, monkeypatch):
    source = Path(__file__).parents[3] / "gateway/app_gateway.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    apply = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "_apply_channel_config")
    block = next(node for node in apply.body if isinstance(node, ast.If)
                 and ast.unparse(node.test) == f"'{name}' in changed_channels")
    imports = []
    original_import = builtins.__import__

    def guarded_import(module, *args, **kwargs):
        if module.endswith(("feishu_connect", "xiaoyi_connect")):
            imports.append(module)
            return SimpleNamespace(FeishuChannel=channel_factory, FeishuConfig=config_factory,
                                   XiaoyiChannel=channel_factory, XiaoyiChannelConfig=config_factory)
        return original_import(module, *args, **kwargs)

    async def start():
        return None

    def config_factory(**kwargs):
        return SimpleNamespace(**kwargs)
    channel_factory = Mock(side_effect=lambda *a, **kw: SimpleNamespace(start=start))
    old = SimpleNamespace(app_id="old", _channel_id="old", start_task=None)
    manager = SimpleNamespace(pop_channels_by_id=Mock(return_value=[old]), register_channel=Mock())
    namespace = dict(changed_channels=[name], conf={}, channel_manager=manager,
                     _stop_channel=AsyncMock(), im_inbound=SimpleNamespace(unregister_adapter=Mock()),
                     im_outbound=SimpleNamespace(unregister_adapter=Mock()), logger=Mock(),
                     feishu_enterprise_tasks={"old": None}, feishu_enterprise_channels={"old": old},
                     _is_channel_enabled=lambda config, fields: (
                         config.get("enabled", all(config.get(k) for k in fields)), "disabled"),
                     asyncio=asyncio, _DummyBus=lambda: None,
                     _FEISHU_DEFAULT_API_BASE="https://example.invalid",
                     _XIAOYI_DEFAULT_PUSH_URL="https://example.invalid")
    function = ast.AsyncFunctionDef(name="update", args=ast.arguments(
        posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[ast.Global(names=["feishu_enterprise_tasks", "feishu_enterprise_channels"]), block],
        decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    return namespace, imports, channel_factory


@pytest.mark.parametrize("channel", ["feishu", "feishu_enterprise", "xiaoyi"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_disabled_connectors_skip_import_but_keep_update_cleanup(channel, enabled, monkeypatch):
    ns, imports, factory = _channel_update(channel, monkeypatch)
    app = dict(enabled=enabled, app_id="app", app_secret="secret", ak="ak", sk="sk", agent_id="agent")
    ns["conf"] = {channel: {"bot": app} if channel == "feishu_enterprise" else {"apps": [app]}}
    await ns["update"]()
    await asyncio.sleep(0)
    assert bool(imports) is enabled
    assert factory.call_count == int(enabled)
    ns["_stop_channel"].assert_awaited_once()
    if channel.startswith("feishu"):
        ns["im_inbound"].unregister_adapter.assert_called_once()
        ns["im_outbound"].unregister_adapter.assert_called_once()


async def test_empty_enterprise_config_does_not_import_connector(monkeypatch):
    ns, imports, _ = _channel_update("feishu_enterprise", monkeypatch)
    ns["conf"] = {"feishu_enterprise": {}}
    await ns["update"]()
    assert imports == []
    ns["_stop_channel"].assert_awaited_once()
