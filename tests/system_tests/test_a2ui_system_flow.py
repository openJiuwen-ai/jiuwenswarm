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
    prompt_section = build_a2ui_prompt_section(
        "en",
        include_browser_workflows=True,
    )
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
    assert "browser_preflight_submit" in prompt_section
    assert "hotel_option_select" in prompt_section
    assert "hotel_payment_confirm" in prompt_section
    assert "gmail_email_select" in prompt_section
    assert "gmail_cleanup_confirm" in prompt_section
    assert "social_post_draft_select" in prompt_section
    assert "social_post_confirm" in prompt_section
    assert "Do not ask for those missing browser-task details" in prompt_section
    assert "ask_user tool" in prompt_section
    assert "Mandatory A2UI account-action gate" in prompt_section
    assert "task_tool as a substitute" in prompt_section
    assert "returned emails/threads MUST still be shown as A2UI candidates" in prompt_section
    assert client_prompt is not None
    assert "submit_selection" in client_prompt
    assert "alpha" in client_prompt
    assert finalized == VALID_A2UI_RESPONSE
    assert validation.valid is True


def test_a2ui_browser_preflight_event_prompts_browser_subagent(monkeypatch):
    """Browser preflight submissions should continue into the browser subagent path."""
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    client_event = {
        "type": "a2ui.client_event",
        "event": {
            "userAction": {
                "name": "browser_preflight_submit",
                "surfaceId": "browser-preflight",
                "sourceComponentId": "submit",
                "context": {
                    "original_query": "Book a hotel in Shanghai",
                    "task_type": "hotel",
                    "next_action": "run_browser_agent",
                    "city": "Shanghai",
                    "check_in": "2026-07-01",
                    "check_out": "2026-07-03",
                    "must_confirm_before_payment": True,
                },
            }
        },
    }

    client_prompt = build_user_prompt_if_a2ui_event(
        client_event,
        channel="web",
        language="en",
    )

    assert client_prompt is not None
    assert "browser task preflight submission" in client_prompt
    assert "spawn_sub_agent" in client_prompt
    assert "browser_agent" in client_prompt
    assert "Book a hotel in Shanghai" in client_prompt
    assert "must_confirm_before_payment" in client_prompt


def test_a2ui_hotel_option_select_continues_current_browser_state(monkeypatch):
    """Hotel candidate selection should not be interpreted as a fresh hotel search."""
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    client_event = {
        "type": "a2ui.client_event",
        "event": {
            "userAction": {
                "name": "hotel_option_select",
                "surfaceId": "hotel-candidates",
                "sourceComponentId": "hotel-2-select",
                "context": {
                    "original_query": "Book a hotel in Shanghai",
                    "task_type": "hotel",
                    "next_action": "continue_hotel_booking",
                    "city": "Shanghai",
                    "check_in": "2026-07-01",
                    "check_out": "2026-07-03",
                    "guest_count": 2,
                    "hotel_name": "Example Riverside Hotel",
                    "candidate_index": 2,
                    "detail_url": "https://example.test/hotel/2",
                },
            }
        },
    }

    client_prompt = build_user_prompt_if_a2ui_event(
        client_event,
        channel="web",
        language="en",
    )

    assert client_prompt is not None
    assert "hotel candidate selection" in client_prompt
    assert "spawn_sub_agent" in client_prompt
    assert "browser_agent" in client_prompt
    assert "current browser state/session" in client_prompt
    assert "Do not repeat the broad hotel search" in client_prompt
    assert "hotel_payment_confirm" in client_prompt
    assert "Example Riverside Hotel" in client_prompt


def test_a2ui_hotel_payment_confirmation_is_guarded(monkeypatch):
    """Final payment confirmation should verify the visible order before proceeding."""
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    client_event = {
        "type": "a2ui.client_event",
        "event": {
            "userAction": {
                "name": "hotel_payment_confirm",
                "surfaceId": "hotel-payment",
                "sourceComponentId": "confirm",
                "context": {
                    "task_type": "hotel",
                    "next_action": "confirm_hotel_payment",
                    "hotel_name": "Example Riverside Hotel",
                    "total_price": "CNY 1288",
                },
            }
        },
    }

    client_prompt = build_user_prompt_if_a2ui_event(
        client_event,
        channel="web",
        language="en",
    )

    assert client_prompt is not None
    assert "final hotel payment confirmation" in client_prompt
    assert "verify that the selected hotel" in client_prompt
    assert "current browser state match the context" in client_prompt
    assert "Example Riverside Hotel" in client_prompt


def test_a2ui_gmail_email_select_continues_current_search_results(monkeypatch):
    """Gmail email selection should open the selected result, not rerun the broad search."""
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    client_event = {
        "type": "a2ui.client_event",
        "event": {
            "userAction": {
                "name": "gmail_email_select",
                "surfaceId": "gmail-results",
                "sourceComponentId": "thread-1-open",
                "context": {
                    "original_query": "Summarize recent project delay emails",
                    "task_type": "gmail",
                    "next_action": "continue_gmail_email_review",
                    "search_query": "from:manager@example.com project delay newer:7d",
                    "sender": "manager@example.com",
                    "subject": "Project delay update",
                    "thread_index": 1,
                    "thread_url": "https://mail.google.com/mail/u/0/#inbox/thread-a:r1",
                },
            }
        },
    }

    client_prompt = build_user_prompt_if_a2ui_event(
        client_event,
        channel="web",
        language="en",
    )

    assert client_prompt is not None
    assert "Gmail email/thread selection" in client_prompt
    assert "spawn_sub_agent" in client_prompt
    assert "browser_agent" in client_prompt
    assert "current Gmail browser state/session" in client_prompt
    assert "Do not repeat the broad Gmail search" in client_prompt
    assert "gmail_reply_draft_select" in client_prompt
    assert "Project delay update" in client_prompt


def test_a2ui_gmail_cleanup_confirmation_is_guarded(monkeypatch):
    """Gmail cleanup confirmation should verify selected messages before mutation."""
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    client_event = {
        "type": "a2ui.client_event",
        "event": {
            "userAction": {
                "name": "gmail_cleanup_confirm",
                "surfaceId": "gmail-cleanup-confirm",
                "sourceComponentId": "confirm",
                "context": {
                    "task_type": "gmail_cleanup",
                    "next_action": "confirm_gmail_cleanup",
                    "operation": "archive",
                    "selected_count": 12,
                    "search_query": "category:promotions older:30d",
                },
            }
        },
    }

    client_prompt = build_user_prompt_if_a2ui_event(
        client_event,
        channel="web",
        language="en",
    )

    assert client_prompt is not None
    assert "final Gmail cleanup confirmation" in client_prompt
    assert "spawn_sub_agent" in client_prompt
    assert "current Gmail browser state/session" in client_prompt
    assert "verify the selected messages/count" in client_prompt
    assert "Never delete, archive, unsubscribe" in client_prompt
    assert "category:promotions older:30d" in client_prompt


def test_a2ui_gmail_send_confirmation_allows_confirmed_send(monkeypatch):
    """Final Gmail send confirmation should send only matching visible content."""
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    client_event = {
        "type": "a2ui.client_event",
        "event": {
            "userAction": {
                "name": "gmail_send_confirm",
                "surfaceId": "gmail-send-confirm",
                "sourceComponentId": "send",
                "context": {
                    "task_type": "gmail",
                    "next_action": "confirm_gmail_send",
                    "recipient": "manager@example.com",
                    "subject": "Re: Project delay update",
                    "body": "Thanks for the update. I will adjust the plan.",
                },
            }
        },
    }

    client_prompt = build_user_prompt_if_a2ui_event(
        client_event,
        channel="web",
        language="en",
    )

    assert client_prompt is not None
    assert "final Gmail send confirmation" in client_prompt
    assert "visible Gmail compose/reply state matches" in client_prompt
    assert "send the email now" in client_prompt
    assert "Never send to a different recipient" in client_prompt
    assert "manager@example.com" in client_prompt


def test_a2ui_social_post_draft_select_stops_before_publish(monkeypatch):
    """Social draft selection should fill compose UI but not publish."""
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    client_event = {
        "type": "a2ui.client_event",
        "event": {
            "userAction": {
                "name": "social_post_draft_select",
                "surfaceId": "social-drafts",
                "sourceComponentId": "draft-1",
                "context": {
                    "original_query": "Draft a LinkedIn product update",
                    "task_type": "social_post",
                    "next_action": "continue_social_post_draft",
                    "platform": "LinkedIn",
                    "account_hint": "Company Page",
                    "draft_body": "We shipped a faster A2UI booking flow.",
                    "visibility": "public",
                },
            }
        },
    }

    client_prompt = build_user_prompt_if_a2ui_event(
        client_event,
        channel="web",
        language="en",
    )

    assert client_prompt is not None
    assert "social media post draft selection" in client_prompt
    assert "spawn_sub_agent" in client_prompt
    assert "browser_agent" in client_prompt
    assert "fill the selected draft" in client_prompt
    assert "Do not publish" in client_prompt
    assert "social_post_confirm" in client_prompt
    assert "LinkedIn" in client_prompt


def test_a2ui_social_post_confirmation_is_guarded(monkeypatch):
    """Final social publish should verify visible compose state before posting."""
    from jiuwenswarm.server.runtime.a2ui.integration import build_user_prompt_if_a2ui_event

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    client_event = {
        "type": "a2ui.client_event",
        "event": {
            "userAction": {
                "name": "social_post_confirm",
                "surfaceId": "social-post-confirm",
                "sourceComponentId": "publish",
                "context": {
                    "task_type": "social_post",
                    "next_action": "confirm_social_post",
                    "platform": "LinkedIn",
                    "account_hint": "Company Page",
                    "draft_body": "We shipped a faster A2UI booking flow.",
                    "visibility": "public",
                },
            }
        },
    }

    client_prompt = build_user_prompt_if_a2ui_event(
        client_event,
        channel="web",
        language="en",
    )

    assert client_prompt is not None
    assert "final social media post confirmation" in client_prompt
    assert "verify that the visible compose state matches" in client_prompt
    assert "publish the post now" in client_prompt
    assert "Never publish different content" in client_prompt
    assert "LinkedIn" in client_prompt
