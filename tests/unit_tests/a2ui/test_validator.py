# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression coverage for A2UI runtime semantic validation."""

from __future__ import annotations


def test_a2ui_validator_accepts_single_level_template():
    from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec

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
    from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec

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
