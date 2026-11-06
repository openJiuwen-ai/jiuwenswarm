"""Instance visibility belongs to delivered frames in both WebUI runtimes."""

import json
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.events import Event

ROOT = Path(__file__).resolve().parents[4]
CASES = json.loads((ROOT / "jiuwenswarm/channels/web/frontend/tests/fixtures/webuiInstanceVisibilityCases.json").read_text())
DOMAINS = ("extensions_v2", "gateway.extensions_v2")


def render_target(models, domain, package_id):
    if domain == "gateway.extensions_v2":
        return models.RenderTarget("", "application.entry", package_id, "details", scope="application", connection_id="browser")
    return models.RenderTarget("s1", "conversation.input.above", package_id, "details")


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_instance_visibility_contract(domain, case) -> None:
    models = import_module(f"jiuwenswarm.{domain}.webui.models")
    schema = import_module(f"jiuwenswarm.{domain}.webui.schema")
    request = models.UiRenderInput(
        render_target(models, domain, case.get("package_id", "visibility-test")),
        models.RenderNode("Text", {"value": "Usage"}),
        models.RenderMode(case.get("mode", "replace")),
        visible_when=case.get("visible_when"),
    )
    assert schema.validate_request(request).ok == case["valid"]


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.parametrize("nested", [False, True])
def test_box_visibility_prop_is_rejected(domain, nested) -> None:
    models = import_module(f"jiuwenswarm.{domain}.webui.models")
    schema = import_module(f"jiuwenswarm.{domain}.webui.schema")
    node = models.RenderNode("Box", {"visibleWhen": {"key": "visible", "default": True}})
    root = models.RenderNode("Box", children=(node,)) if nested else node
    request = models.UiRenderInput(render_target(models, domain, "visibility-test"), root)

    report = schema.validate_request(request)

    assert not report.ok
    assert report.code == "SCHEMA_ERROR"


@pytest.mark.parametrize("domain", DOMAINS)
@pytest.mark.asyncio
async def test_instance_visibility_is_snapshotted_replaced_and_rolled_back(domain) -> None:
    models = import_module(f"jiuwenswarm.{domain}.webui.models")
    registry_module = import_module(f"jiuwenswarm.{domain}.webui.registry")
    core_module = import_module(f"jiuwenswarm.{domain}.webui.core_handlers.render_core")
    registry = registry_module.ActiveRenderRegistry()
    transport = SimpleNamespace(send_frame=AsyncMock(return_value=True), send_clear=AsyncMock(return_value=True),
                                send_error=AsyncMock(return_value=True))
    handler = core_module.build_render_core_handler(transport, registry)
    target = render_target(models, domain, "visibility-test")
    node = models.RenderNode("Text", {"value": "Usage"})
    binding = {"key": "visible", "default": True}
    request = models.UiRenderInput(target, node, visible_when=binding)
    assert (await handler(Event("ui.render", request), None, AsyncMock())).status == models.RenderStatus.DELIVERED
    first = transport.send_frame.call_args.args[0]
    assert first.to_payload()["visible_when"] == {"key": "visible", "default": True}
    binding["key"] = "mutated"
    assert first.visible_when["key"] == "visible"
    with pytest.raises(TypeError):
        first.visible_when["key"] = "forbidden"

    transport.send_frame.return_value = False
    failed = await handler(Event("ui.render", replace(request, visible_when={"key": "other", "default": False})), None, AsyncMock())
    assert failed.status == models.RenderStatus.DELIVERY_FAILED
    assert registry.get(target).revision == first.revision
    assert registry.get(target).visible_when == first.visible_when

    transport.send_frame.return_value = True
    assert (await handler(Event("ui.render", models.UiRenderInput(target, node)), None, AsyncMock())).status == models.RenderStatus.DELIVERED
    assert "visible_when" not in transport.send_frame.call_args.args[0].to_payload()
    assert registry.get(target).visible_when is None

    invalid = replace(request, visible_when={"key": "visible", "default": True, "package_id": "other"})
    assert (await handler(Event("ui.render", invalid), None, AsyncMock())).status == models.RenderStatus.REJECTED
    assert registry.get(target).visible_when is None
    transport.send_error.assert_awaited_once()

    clear = models.UiRenderInput(target, mode=models.RenderMode.CLEAR)
    assert (await handler(Event("ui.render", clear), None, AsyncMock())).status == models.RenderStatus.DELIVERED
    assert registry.get(target) is None
