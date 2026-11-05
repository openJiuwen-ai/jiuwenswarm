"""Settings declarations and their connection-bound catalog entry."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.webui.adapter import WebUIDispatchAdapter
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.settings import SETTINGS_SLOT, parse_settings

SOURCE = Path(__file__).resolve().parents[4] / "jiuwenswarm/extensions_v2/examples/webui_workbench_v2"
PACKAGE_ID = "webui.workbench-v2"


def test_removed_settings_entry_is_not_a_registered_slot() -> None:
    from jiuwenswarm.extensions_v2.webui.constants import slot_spec
    assert slot_spec("application.settings.content") is None



@pytest.mark.parametrize("kind,location", [("menu", {}), ("section", {"menu": "general"}),
    ("item", {"menu": "general", "section": "display", "after": "old"})])
def test_settings_declarations_accept_three_location_levels(kind, location) -> None:
    raw = {"slot": SETTINGS_SLOT, "id": "demo", "kind": kind, "location": location, "title": {"zh": "示例", "en": "Example"}}
    if kind == "menu":
        raw["icon"] = "settings"
    if kind == "item":
        raw["renderer"] = {"kind": "webui-tree"}
    assert parse_settings(raw)["location"] == location



@pytest.mark.parametrize("change", [
    {"id": "bad::id"}, {"kind": "unknown"}, {"title": {"zh": "示例"}},
    {"slot": "application.settings.content"}, {"location": {"menu": "general"}},
    {"location": {"menu": "general", "section": "display", "before": "one", "after": "two"}},
    {"renderer": {"kind": "html"}}, {"scope": "unknown"}, {"instanceKey": "bad|key"},
    {"kind": "menu", "location": {}, "icon": "settings", "renderer": {}},
    {"kind": "section", "location": {"menu": "general"}, "renderer": None},
])
def test_settings_declarations_reject_ambiguous_or_invalid_metadata(change) -> None:
    raw = {"slot": SETTINGS_SLOT, "id": "demo", "kind": "item", "location": {"menu": "general", "section": "display"},
           "title": {"zh": "示例", "en": "Example"}, "renderer": {"kind": "webui-tree"}}
    with pytest.raises(ValueError):
        parse_settings({**raw, **change})



@pytest.mark.asyncio
async def test_catalog_request_uses_the_bound_web_connection_without_a_conversation(monkeypatch) -> None:
    from jiuwenswarm.extensions_v2 import assembly

    catalog = WebUICatalogService(lambda: (SimpleNamespace(root=SOURCE, package_id=PACKAGE_ID, version="1", generation=1),))
    monkeypatch.setattr(assembly, "get_webui_runtime", lambda: SimpleNamespace(catalog=lambda: catalog))
    adapter = WebUIDispatchAdapter()
    request = AgentRequest("catalog-1", channel_id="web", req_method=ReqMethod.WEBUI_SETTINGS_CATALOG,
                           metadata={"ws_id": "tab-1"})
    output = await adapter.handle(request)
    assert output.ok
    assert output.payload["items"][0]["package_id"] == PACKAGE_ID
    assert "install_path" not in json.dumps(output.payload)
    assert not (await adapter.handle(replace(request, params={"path": "/arbitrary"}))).ok
    assert not (await adapter.handle(replace(request, metadata={}))).ok
    assert not (await adapter.handle(replace(request, channel_id="feishu"))).ok



@pytest.mark.asyncio
async def test_gateway_registers_both_catalog_and_existing_dispatch(monkeypatch) -> None:
    from jiuwenswarm.gateway.channel_manager.web import webui_handlers

    routes = {}
    channel = SimpleNamespace(channel_id="web", register_method=lambda name, handler: routes.update({name: handler}))
    proxy = AsyncMock(return_value=True)
    monkeypatch.setattr(webui_handlers, "proxy_unary_request", proxy)
    webui_handlers.register_webui_handlers(channel, lambda: object())
    assert {ReqMethod.WEBUI_DISPATCH.value, ReqMethod.WEBUI_SETTINGS_CATALOG.value} <= routes.keys()
    await routes[ReqMethod.WEBUI_SETTINGS_CATALOG.value](SimpleNamespace(_jiuwen_ws_id="tab-1"), "q1", {}, None)
    assert proxy.call_args.kwargs["req_method"] == ReqMethod.WEBUI_SETTINGS_CATALOG
    assert proxy.call_args.kwargs["metadata"] == {"ws_id": "tab-1"}
