# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""System-level coverage for the A2UI response flow."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.system]


VALID_A2UI_RESPONSE = """<a2ui-json>
[
  {
    "beginRendering": {
      "surfaceId": "system-test",
      "root": "root"
    }
  },
  {
    "surfaceUpdate": {
      "surfaceId": "system-test",
      "components": [
        {
          "id": "root",
          "component": {
            "Text": {
              "text": {
                "literalString": "System test A2UI content"
              }
            }
          }
        }
      ]
    }
  }
]
</a2ui-json>"""


@pytest.mark.asyncio
async def test_a2ui_system_flow_accepts_event_and_valid_response(monkeypatch):
    """Verify the A2UI host flow across config, prompt, event, and response."""
    from jiuwenswarm.server.runtime.a2ui.config import get_a2ui_config
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event
    from jiuwenswarm.server.runtime.a2ui.runtime.finalizer import A2UIResponseFinalizer
    from jiuwenswarm.server.runtime.a2ui.runtime.prompt import build_a2ui_prompt_section
    from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    config = get_a2ui_config({"a2ui": {"enabled": True}})
    prompt_section = build_a2ui_prompt_section("en")
    client_event = {
        "type": "a2ui.client_event",
        "event": {
            "userAction": {
                "name": "submit_selection",
                "surfaceId": "system-test",
                "sourceComponentId": "submit",
                "context": {"selected": "alpha"},
            }
        },
    }

    client_prompt = build_user_prompt_if_a2ui_event(
        client_event,
        channel="web",
        language="en",
    )
    finalized = await A2UIResponseFinalizer().finalize(
        VALID_A2UI_RESPONSE,
        user_query="show system test result",
        request_id="system-a2ui-valid-response",
        repair_call=lambda _: pytest.fail("valid A2UI response must not be repaired"),
    )
    validation = get_protocol_spec().validate_response(finalized)

    assert config.enabled is True
    assert "<a2ui-json>" in prompt_section
    assert "browser_preflight_submit" not in prompt_section
    assert client_prompt is not None
    assert "submit_selection" in client_prompt
    assert "alpha" in client_prompt
    assert finalized == VALID_A2UI_RESPONSE
    assert validation.valid is True
