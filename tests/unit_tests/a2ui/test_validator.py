# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression coverage for A2UI runtime semantic validation."""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec
from jiuwenswarm.server.runtime.a2ui.types import A2UIResponsePart
from jiuwenswarm.server.runtime.a2ui.validator import validate_a2ui_response


def _surface_update(surface_id, *components):
    return {"surfaceUpdate": {"surfaceId": surface_id, "components": list(components)}}


def _text(component_id, usage_hint):
    return {
        "id": component_id,
        "component": {"Text": {"text": {"literalString": "Content"}, "usageHint": usage_hint}},
    }


def _column(component_id, child):
    return {"id": component_id, "component": {"Column": {"children": {"explicitList": [child]}}}}


def _tagged_block(*messages):
    return f"<a2ui-json>{json.dumps(messages)}</a2ui-json>"


@pytest.mark.parametrize("hint", ("h1", "h2", "h3", "h4", "h5", "caption", "body"))
def test_a2ui_validator_rejects_text_usage_hint_matching_its_own_id(hint):
    result = get_protocol_spec().validate_response(_tagged_block(_surface_update("form", _text(hint, hint))))

    assert not result.valid
    assert f"usageHint {hint!r}" in result.error


@pytest.mark.parametrize("split_blocks", (False, True))
def test_a2ui_validator_rejects_text_usage_hint_matching_a_parent_id(split_blocks):
    begin = {"beginRendering": {"surfaceId": "form", "root": "body"}}
    parent = _column("body", "copy")
    child = _text("copy", "body")
    if split_blocks:
        response = _tagged_block(begin, _surface_update("form", parent, _text("copy", "h1")))
        response += _tagged_block(_surface_update("form", child))
    else:
        response = _tagged_block(begin, _surface_update("form", parent, child))

    result = get_protocol_spec().validate_response(response)

    assert not result.valid
    assert "Text 'copy'" in result.error
    assert "usageHint 'body'" in result.error
    assert "surface 'form'" in result.error


def test_a2ui_validator_scopes_usage_hint_collisions_to_one_live_surface():
    response = _tagged_block(
        _surface_update("first", _column("body", "title"), _text("title", "h1")),
        _surface_update("second", _text("copy", "body")),
        {"deleteSurface": {"surfaceId": "first"}},
    ) + _tagged_block(_surface_update("first", _text("copy", "body")))

    assert get_protocol_spec().validate_response(response).valid


def test_a2ui_validator_accepts_body_id_when_usage_hint_is_omitted():
    text = _text("copy", "body")
    del text["component"]["Text"]["usageHint"]
    messages = (
        {"beginRendering": {"surfaceId": "form", "root": "body"}},
        _surface_update("form", _column("body", "copy"), text),
    )

    assert get_protocol_spec().validate_response(_tagged_block(*messages)).valid


def test_a2ui_validator_rejects_usage_hint_matching_an_id_in_an_earlier_parsed_part():
    spec = get_protocol_spec()
    first = _surface_update("form", _column("body", "copy"), _text("copy", "h1"))
    second = _surface_update("form", _text("copy", "body"))
    parts = [
        A2UIResponsePart(kind="a2ui", messages=[first]),
        A2UIResponsePart(kind="text", text="Updated form"),
        A2UIResponsePart(kind="a2ui", messages=[second]),
    ]

    result = validate_a2ui_response(
        "parsed output", parse_response=lambda _: parts, validate_messages=spec.validate_messages,
    )

    assert not result.valid
    assert "Text 'copy' usageHint 'body'" in result.error


def test_a2ui_validator_checks_latest_component_definition_after_an_update():
    response = _tagged_block(
        _surface_update("form", _text("copy", "body")),
        _surface_update("form", _text("copy", "h1"), _column("body", "copy")),
    )

    assert get_protocol_spec().validate_response(response).valid


def test_a2ui_validator_accepts_single_level_template():
    response = """<a2ui-json>
[
  {
    "beginRendering": {
      "surfaceId": "single-template",
      "root": "root"
    }
  },
  {
    "surfaceUpdate": {
      "surfaceId": "single-template",
      "components": [
        {
          "id": "root",
          "component": {
            "List": {
              "children": {
                "template": {
                  "componentId": "student-row-tpl",
                  "dataBinding": "/students"
                }
              },
              "direction": "vertical"
            }
          }
        },
        {
          "id": "student-row-tpl",
          "component": {
            "Text": {
              "text": {
                "path": "summary"
              },
              "usageHint": "body"
            }
          }
        }
      ]
    }
  },
  {
    "dataModelUpdate": {
      "surfaceId": "single-template",
      "contents": [
        {
          "key": "/students",
          "valueMap": [
            {
              "key": "0",
              "valueMap": [
                {
                  "key": "summary",
                  "valueString": "姓名：张雨萱 | 学号：2024001"
                }
              ]
            }
          ]
        }
      ]
    }
  }
]
</a2ui-json>"""

    assert get_protocol_spec().validate_response(response).valid is True


def test_a2ui_validator_rejects_nested_templates():
    response = """<a2ui-json>
[
  {
    "beginRendering": {
      "surfaceId": "nested-template",
      "root": "root"
    }
  },
  {
    "surfaceUpdate": {
      "surfaceId": "nested-template",
      "components": [
        {
          "id": "root",
          "component": {
            "List": {
              "children": {
                "template": {
                  "componentId": "student-card-tpl",
                  "dataBinding": "/students"
                }
              },
              "direction": "vertical"
            }
          }
        },
        {
          "id": "student-card-tpl",
          "component": {
            "Column": {
              "children": {
                "template": {
                  "componentId": "line-tpl",
                  "dataBinding": "lines"
                }
              }
            }
          }
        },
        {
          "id": "line-tpl",
          "component": {
            "Text": {
              "text": {
                "path": "text"
              },
              "usageHint": "body"
            }
          }
        }
      ]
    }
  },
  {
    "dataModelUpdate": {
      "surfaceId": "nested-template",
      "contents": [
        {
          "key": "/students",
          "valueMap": [
            {
              "key": "0",
              "valueMap": [
                {
                  "key": "lines",
                  "valueMap": [
                    {
                      "key": "0",
                      "valueMap": [
                        {
                          "key": "text",
                          "valueString": "姓名：张雨萱"
                        }
                      ]
                    }
                  ]
                }
              ]
            }
          ]
        }
      ]
    }
  }
]
</a2ui-json>"""

    result = get_protocol_spec().validate_response(response)

    assert result.valid is False
    assert result.error is not None
    assert "nested templates" in result.error
