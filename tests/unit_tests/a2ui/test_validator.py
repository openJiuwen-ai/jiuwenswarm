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


# --- BUG20261124415272: single-parent component tree -------------------------


def _a2ui_card(components: list[dict], data_model: list[dict] | None = None) -> str:
    messages: list[dict] = [
        {"beginRendering": {"surfaceId": "single-parent", "root": "root"}},
        {"surfaceUpdate": {"surfaceId": "single-parent", "components": components}},
    ]
    if data_model is not None:
        messages.append({"dataModelUpdate": {"surfaceId": "single-parent", "contents": data_model}})
    return "<a2ui-json>\n" + json.dumps(messages, ensure_ascii=False, indent=2) + "\n</a2ui-json>"


def _literal_text(component_id: str, text: str) -> dict:
    """Plain Text node for single-parent fixtures (distinct from usageHint helper ``_text``)."""
    return {"id": component_id, "component": {"Text": {"text": {"literalString": text}}}}


def _choice_card(main_children: list[str]) -> str:
    """Shape of the BUG20261124415272 card: ``opt`` sits in section ``g1``."""
    return _a2ui_card(
        [
            {"id": "root", "component": {"Card": {"child": "main_col"}}},
            {"id": "main_col", "component": {"Column": {"children": {"explicitList": main_children}}}},
            _literal_text("title", "连坐问题怎么根治"),
            {"id": "g1", "component": {"Column": {"children": {"explicitList": ["g1_title", "opt"]}}}},
            _literal_text("g1_title", "选一个方案"),
            {
                "id": "opt",
                "component": {
                    "MultipleChoice": {
                        "selections": {"path": "/form/fix"},
                        "options": [
                            {"label": {"literalString": "A"}, "value": "a"},
                            {"label": {"literalString": "C"}, "value": "c"},
                        ],
                        "maxAllowedSelections": 1,
                        "variant": "checkbox",
                    }
                },
            },
            _literal_text("opt_label", ""),
            {"id": "btn_row", "component": {"Row": {"children": {"explicitList": ["go_btn"]}}}},
            {
                "id": "go_btn",
                "component": {
                    "Button": {
                        "child": "go_text",
                        "primary": True,
                        "action": {
                            "name": "apply_fix",
                            "context": [{"key": "fix", "value": {"path": "/form/fix"}}],
                        },
                    }
                },
            },
            _literal_text("go_text", "按这个执行"),
        ],
        data_model=[
            {
                "key": "form",
                "valueMap": [{"key": "fix", "valueMap": [{"key": "0", "valueString": "a"}]}],
            }
        ],
    )


def test_a2ui_validator_rejects_component_mounted_under_two_parents():
    """BUG20261124415272: ``opt`` listed by both ``g1`` and ``main_col`` rendered two linked dropdowns."""
    from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec

    result = get_protocol_spec().validate_response(
        _choice_card(["title", "g1", "opt_label", "opt", "btn_row"])
    )

    assert result.valid is False
    assert result.error is not None
    assert "single-parent" in result.error
    assert "'opt'" in result.error
    assert "main_col" in result.error
    assert "g1" in result.error


def test_a2ui_validator_accepts_component_mounted_under_one_parent():
    from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec

    result = get_protocol_spec().validate_response(
        _choice_card(["title", "g1", "opt_label", "btn_row"])
    )

    assert result.valid is True


def test_a2ui_validator_rejects_component_listed_twice_by_one_parent():
    from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec

    result = get_protocol_spec().validate_response(
        _a2ui_card(
            [
                {"id": "root", "component": {"Column": {"children": {"explicitList": ["a", "a"]}}}},
                _literal_text("a", "x"),
            ]
        )
    )

    assert result.valid is False
    assert "single-parent" in result.error
    assert "'a'" in result.error


@pytest.mark.parametrize(
    ("mount_component", "shared_id"),
    [
        (
            {"id": "m", "component": {"Button": {"child": "shared", "action": {"name": "go"}}}},
            "shared",
        ),
        (
            {
                "id": "m",
                "component": {
                    "Tabs": {"tabItems": [{"title": {"literalString": "A"}, "child": "shared"}]}
                },
            },
            "shared",
        ),
        (
            {
                "id": "m",
                "component": {"Modal": {"entryPointChild": "open", "contentChild": "shared"}},
            },
            "shared",
        ),
    ],
    ids=["button-child", "tabs-child", "modal-content-child"],
)
def test_a2ui_validator_rejects_single_child_field_also_listed_in_column(
    mount_component, shared_id
):
    from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec

    components = [
        {"id": "root", "component": {"Column": {"children": {"explicitList": ["m", shared_id]}}}},
        mount_component,
        _literal_text(shared_id, "shared"),
    ]
    if mount_component["component"].get("Modal"):
        components.append(_literal_text("open", "open"))

    result = get_protocol_spec().validate_response(_a2ui_card(components))

    assert result.valid is False
    assert "single-parent" in result.error
    assert f"'{shared_id}'" in result.error


def test_a2ui_validator_accepts_template_shared_by_two_lists():
    """Templates are instantiated per item with item-relative bindings; sharing one is not a double mount."""
    from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec

    result = get_protocol_spec().validate_response(
        _a2ui_card(
            [
                {
                    "id": "root",
                    "component": {"Column": {"children": {"explicitList": ["list_a", "list_b"]}}},
                },
                {
                    "id": "list_a",
                    "component": {
                        "List": {
                            "children": {
                                "template": {"componentId": "row", "dataBinding": "/a"}
                            }
                        }
                    },
                },
                {
                    "id": "list_b",
                    "component": {
                        "List": {
                            "children": {
                                "template": {"componentId": "row", "dataBinding": "/b"}
                            }
                        }
                    },
                },
                {"id": "row", "component": {"Text": {"text": {"path": "name"}}}},
            ],
            data_model=[
                {
                    "key": "a",
                    "valueMap": [
                        {"key": "0", "valueMap": [{"key": "name", "valueString": "x"}]}
                    ],
                },
                {
                    "key": "b",
                    "valueMap": [
                        {"key": "0", "valueMap": [{"key": "name", "valueString": "y"}]}
                    ],
                },
            ],
        )
    )

    assert result.valid is True
