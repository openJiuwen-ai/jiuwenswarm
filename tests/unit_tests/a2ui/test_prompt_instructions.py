# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.server.runtime.a2ui.prompt_instructions import (
    build_a2ui_autonomy_instruction,
    build_a2ui_browser_workflow_instruction,
    is_a2ui_browser_workflow_request,
)
from jiuwenswarm.server.runtime.a2ui.runtime.prompt import (
    build_a2ui_client_event_prompt,
    build_a2ui_prompt_section,
)


def test_a2ui_prompt_discourages_icon_ligature_dependency():
    instruction = build_a2ui_autonomy_instruction("en")

    assert "Avoid A2UI Icon for semantic content" in instruction
    assert "Material Symbols" in instruction
    assert "emoji or text labels" in instruction


def test_a2ui_prompt_is_autonomous_not_forced():
    instruction = build_a2ui_autonomy_instruction("en")

    assert "A2UI is optional" in instruction
    assert "If A2UI is not appropriate, answer in plain text" in instruction
    assert "Do not promise to show the result with A2UI and then output only Markdown" in instruction
    assert "Mandatory A2UI account-action gate" not in instruction
    assert "browser_preflight_submit" not in instruction


def test_a2ui_prompt_discourages_nested_templates():
    instruction = build_a2ui_autonomy_instruction("en")

    assert "Do not nest templates" in instruction
    assert "flatten repeated item details" in instruction


def test_a2ui_prompt_defines_hotel_booking_actions():
    instruction = build_a2ui_browser_workflow_instruction()

    assert "hotel_option_select" in instruction
    assert "continue_hotel_booking" in instruction
    assert "hotel_payment_confirm" in instruction
    assert "hotel_payment_cancel" in instruction


def test_a2ui_prompt_discourages_unsupported_popup_components():
    instruction = build_a2ui_autonomy_instruction("en")

    assert "modal" in instruction
    assert "floating overlay" in instruction
    assert "inline status" in instruction
    assert "plain text" in instruction


def test_a2ui_zh_prompt_discourages_unsupported_popup_components():
    instruction = build_a2ui_autonomy_instruction("zh")

    assert "弹窗" in instruction
    assert "浮层" in instruction
    assert "行内状态" in instruction
    assert "纯文本" in instruction


def test_a2ui_prompt_defines_gmail_and_social_actions():
    instruction = build_a2ui_browser_workflow_instruction()

    assert "Mandatory A2UI account-action gate" in instruction
    assert "task_tool as a substitute" in instruction
    assert "Never search multiple emails and send replies in the same uninterrupted run" in instruction
    assert "returned emails/threads MUST still be shown as A2UI candidates" in instruction
    assert "gmail_email_select" in instruction
    assert "gmail_reply_draft_select" in instruction
    assert "gmail_send_confirm" in instruction
    assert "gmail_cleanup_select" in instruction
    assert "gmail_cleanup_confirm" in instruction
    assert "social_post_draft_select" in instruction
    assert "social_post_confirm" in instruction
    assert "social_post_cancel" in instruction


def test_a2ui_browser_workflow_detection_is_request_scoped():
    """Only real browser workflows and their A2UI events should match."""
    assert is_a2ui_browser_workflow_request("帮我预订北京的酒店")
    assert is_a2ui_browser_workflow_request("Search Gmail and draft replies")
    assert is_a2ui_browser_workflow_request(
        {
            "type": "a2ui.client_event",
            "event": {"userAction": {"name": "gmail_send_confirm"}},
        }
    )
    assert not is_a2ui_browser_workflow_request(
        "生成一个带有故宫图片的介绍故宫的A2UI卡片"
    )
    assert not is_a2ui_browser_workflow_request("生成一个酒店介绍 A2UI 卡片")


def test_a2ui_prompt_adds_browser_rules_only_when_requested():
    """Generic cards stay concise while browser tasks retain workflow rules."""
    generic_prompt = build_a2ui_prompt_section("zh")
    browser_prompt = build_a2ui_prompt_section(
        "zh",
        include_browser_workflows=True,
    )

    assert "browser_preflight_submit" not in generic_prompt
    assert "Mandatory A2UI account-action gate" not in generic_prompt
    assert "browser_preflight_submit" in browser_prompt
    assert "Mandatory A2UI account-action gate" in browser_prompt


def test_a2ui_zh_prompt_section_is_readable():
    prompt = build_a2ui_prompt_section("zh")

    assert "你是 jiuwenswarm 的 A2UI 生成器" not in prompt
    assert "JiuwenSwarm 支持可选的 A2UI 输出格式" in prompt
    assert "当用户需要列表、卡片、表单" in prompt
    assert "浣犳槸" not in prompt
    assert "鐢熸垚" not in prompt


def test_a2ui_en_prompt_does_not_use_generator_identity():
    prompt = build_a2ui_prompt_section("en")

    assert "You are jiuwenswarm's A2UI generator" not in prompt
    assert "JiuwenSwarm supports an optional A2UI output format" in prompt


def test_a2ui_zh_client_event_prompt_is_readable():
    prompt = build_a2ui_client_event_prompt(
        {
            "type": "a2ui.client_event",
            "event": {
                "userAction": {
                    "name": "submit_form",
                    "surfaceId": "surface-1",
                    "sourceComponentId": "submit",
                    "context": {"name": "张三"},
                },
            },
        },
        channel="web",
        language="zh",
    )

    assert "你收到了一次 A2UI 组件交互" in prompt
    assert "张三" in prompt
    assert "submit_form" in prompt
    assert "浣犳敹" not in prompt
