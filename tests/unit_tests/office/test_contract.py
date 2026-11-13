# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The PowerPoint contract JiuwenSwarm ships: how the model is offered its tools, and what a task pane attaches with."""

from __future__ import annotations

import json

import jsonschema
import pytest
from openjiuwen.core.foundation.tool import ToolExecution, ToolExposure

from jiuwenswarm.office.contract import ToolAnnotations, load_contract
from jiuwenswarm.server.client_attachments import load_client_capabilities, registry
from tests.unit_tests.agentserver.client_attachments_support import CLIENT_APP, answer, request
from tests.unit_tests.office.conftest import POWERPOINT_PACKAGE

DIRECT = {"read_presentation_context", "list_slide_shapes", "read_slide_image",
          "read_slide_text", "edit_slide_text", "find_and_replace"}
# What JiuwenSwarm ships, read before the ``isolated`` fixture replaces it.
_SHIPPED_APPS = registry.APPS
SUPPORTED = {"supported_tools": ["read_presentation_context", "read_slide_image", "presentation_apply_patch"]}


def compact(tools: list[dict]) -> str:
    """``tools.json``'s format: one line per tool field, so a diff names the tool and field that changed."""

    def value(item) -> str:
        return json.dumps(item, ensure_ascii=False, separators=(", ", ": "))

    objects = (",\n".join(f"    {value(key)}: {value(item)}" for key, item in tool.items()) for tool in tools)
    return "[\n" + ",\n".join("  {\n" + fields + "\n  }" for fields in objects) + "\n]\n"


def test_tools_json_keeps_its_compact_format() -> None:
    text = (POWERPOINT_PACKAGE / "tools.json").read_text("utf-8")
    assert text == compact(json.loads(text))


def test_profile_json_is_the_identity_the_task_pane_build_reads() -> None:
    assert json.loads((POWERPOINT_PACKAGE / "profile.json").read_text("utf-8")) == {
        "app_id": "office_powerpoint", "domain": "presentation", "contract_version": 1}


def test_each_contract_tool_is_a_client_tool_offered_as_its_annotations_say() -> None:
    tools = load_contract(POWERPOINT_PACKAGE)
    cards = [tool.card() for tool in tools]
    assert len(cards) == 18
    assert {card.name for card in cards if card.exposure is ToolExposure.DIRECT} == DIRECT
    assert all(card.execution is ToolExecution.CLIENT and not card.idempotent for card in cards)
    for tool, card in zip(tools, cards):
        assert card.parallel_safe is tool.annotations.parallel_safe
        assert card.parallel_safe is not tool.annotations.mutating
        assert card.input_params == tool.input_schema
    # Each agent registers its own cards: registering one rewrites its id.
    assert tools[0].card() is not tools[0].card()


def test_the_registry_knows_the_same_contract() -> None:
    app = registry.client_apps()[CLIENT_APP]
    assert app.tool_names == {tool.name for tool in load_contract(POWERPOINT_PACKAGE)}


@pytest.fixture
def shipped(isolated, monkeypatch):
    monkeypatch.setattr(registry, "APPS", _SHIPPED_APPS)
    registry.client_apps.cache_clear()


def test_a_task_pane_attaches_its_conversation_and_keeps_what_its_powerpoint_runs(shipped) -> None:
    attached = answer(request(capabilities=SUPPORTED))

    assert attached["ok"] is True and attached["payload"]["client_app"] == CLIENT_APP
    assert load_client_capabilities("session-1") == SUPPORTED


@pytest.mark.parametrize("unknown", ["delegate_slides", "render_slide"])
def test_an_attach_declaring_tools_outside_the_contract_is_refused(shipped, unknown) -> None:
    refused = answer(request(capabilities={"supported_tools": ["read_presentation_context", unknown]}))

    assert refused["ok"] is False and refused["payload"]["code"] == "INVALID_ARGUMENT"
    assert load_client_capabilities("session-1") is None


COLUMN = {"type": "column", "categories": ["Q1", "Q2"], "values": [1, 2]}


@pytest.mark.parametrize("tool_name", ["create_slide_chart", "edit_built_chart"])
@pytest.mark.parametrize(("spec", "valid"), [
    ({**COLUMN, "style": {"accent": "1F4E79"}, "position": {"x": 0, "y": 0, "w": 914400, "h": 914400},
      "construction": {"legend": "top"}, "point_labels": [{"index": 0, "text": "peak"}]}, True),
    ({"type": "big_number", "title": "7%", "subtitle": "growth", "style": {"font": "Calibri"}}, True),
    ({**COLUMN, "style": {"accent": "blue"}}, False),
    ({**COLUMN, "position": {"x": 0, "y": 0}}, False),
    ({**COLUMN, "colour": "1F4E79"}, False),
    ({"type": "big_number", "title": "7%", "subtitle": "growth", "construction": {"legend": "top"}}, False),
    ({"type": "line", "categories": ["a", "b"], "series": [{"name": "s", "values": [1, 2]}],
      "point_labels": [{"index": 0, "text": "x"}]}, False),
])
def test_a_chart_spec_takes_the_shared_fields_its_form_lists(tool_name, spec, valid) -> None:
    """Fields every chart form shares are defined once on ``spec``; each form still lists the ones it takes."""
    tool = next(tool for tool in load_contract(POWERPOINT_PACKAGE) if tool.name == tool_name)
    arguments = {"slide_id": "256", "spec": spec}
    if valid:
        jsonschema.validate(arguments, tool.input_schema)
    else:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(arguments, tool.input_schema)


@pytest.mark.parametrize("annotations", [
    {"mutating": True, "exposure": "hidden", "parallel_safe": False},
    {"mutating": "yes", "exposure": "deferred", "parallel_safe": False},
    {"mutating": True, "exposure": "deferred", "parallel_safe": "no"},
    {"mutating": True, "exposure": "deferred"},
    {"mutating": True, "exposure": "deferred", "parallel_safe": False, "risk": "high"},
])
def test_incoherent_annotations_are_refused(annotations) -> None:
    with pytest.raises(ValueError):
        ToolAnnotations.parse("a_tool", annotations)
