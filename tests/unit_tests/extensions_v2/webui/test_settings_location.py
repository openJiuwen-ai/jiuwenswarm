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
async def test_catalog_request_uses_the_bound_web_connection_without_a_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.extensions_v2 import assembly

    package_id = "test.settings-catalog"
    manifest = {
        "schemaVersion": 1,
        "package_type": "extension",
        "package": {"domain": "agentserver", "id": package_id, "version": "1.0.0"},
        "ui": {"apiVersion": 1, "items": [{
            "type": "web.slot.contribution",
            "id": "test-settings",
            "slot": SETTINGS_SLOT,
            "scope": "application",
            "location": {"menu": "general", "section": "display"},
            "title": {"zh": "测试设置", "en": "Test settings"},
            "renderer": {"kind": "webui-tree"},
        }]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    record = SimpleNamespace(root=tmp_path, package_id=package_id, version="1.0.0", generation=1)
    catalog = WebUICatalogService(lambda: (record,))
    monkeypatch.setattr(assembly, "get_webui_runtime", lambda: SimpleNamespace(catalog=lambda: catalog))
    adapter = WebUIDispatchAdapter()
    request = AgentRequest("catalog-1", channel_id="web", req_method=ReqMethod.WEBUI_SETTINGS_CATALOG,
                           metadata={"ws_id": "tab-1"})
    output = await adapter.handle(request)
    assert output.ok
    assert output.payload["errors"] == {}
    assert [item["package_id"] for item in output.payload["items"]] == [package_id]
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
