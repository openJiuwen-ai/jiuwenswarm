# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application form roots, scope and the package-owned document boundary."""

import pytest
from jiuwenswarm.gateway.extensions_v2.webui.models import RenderNode, RenderTarget, UiRenderInput
from jiuwenswarm.gateway.extensions_v2.webui.schema import apply_slot_scope, validate_request, validate_root


def surface(**props) -> RenderNode:
    return RenderNode(component="ApplicationSurface", props={
        "title": {"zh": "蜜蜂", "en": "Bee"}, "src": "/extension-assets/bee/g1/presentation.html", **props,
    })


def test_presentation_is_connection_scoped_and_accepts_its_own_package():
    target, report = apply_slot_scope(RenderTarget(session_id="ignored", slot_id="application.presentation", package_id="bee"),
                                      trusted_connection_id="ws-1")
    assert report.ok
    assert target.session_id == ""
    assert target.connection_id == "ws-1"
    assert validate_request(UiRenderInput(target=target, root=surface(config={"sprites": {"idle": "/idle.png"}}))).ok


@pytest.mark.parametrize("props", [
    {"src": "https://example.com/page"},
    {"src": "/extension-assets/bee/g1/%2e%2e/file"},
    {"width": True}, {"width": 159}, {"width": 300.5}, {"height": 961},
    {"title": {"zh": "蜜蜂"}}, {"title": {"zh": "", "en": "Bee"}},
    {"config": {"expression": float("nan")}},
    {"config": {"large": "a" * 16384}}, {"style": {}},
])
def test_invalid_presentation_properties_are_rejected(props):
    assert not validate_root(surface(**props)).ok


def test_presentation_rejects_foreign_assets_and_ordinary_slot_roots():
    target = RenderTarget(session_id="", slot_id="application.presentation", package_id="other", connection_id="ws")
    assert not validate_request(UiRenderInput(target=target, root=surface())).ok
    assert not validate_request(UiRenderInput(target=target, root=RenderNode(component="Box"))).ok
    assert not validate_root(RenderNode(component="Box", children=(surface(),))).ok
