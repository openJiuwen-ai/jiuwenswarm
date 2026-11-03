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


def test_a2ui_prompt_is_autonomous_not_forced():
    instruction = build_a2ui_autonomy_instruction("en")

    assert "plain text" in instruction
    assert "narrative explanation" in instruction
    assert "markdown mock" in instruction
    assert "Mandatory A2UI account-action gate" not in instruction
    assert "browser_preflight_submit" not in instruction


def test_a2ui_prompt_discourages_nested_templates():
    instruction = build_a2ui_autonomy_instruction("en")

    assert "Do not nest templates" in instruction
    assert "indexed valueMap" in instruction


def test_a2ui_prompt_titles_input_components_with_text_caption():
    en = build_a2ui_autonomy_instruction("en")

    assert 'usageHint "caption"' in en
    assert '{"literalString": ""}' in en
    assert "Wrap each Text title and its input component together in a Column" in en

    zh = build_a2ui_autonomy_instruction("zh")

    assert 'usageHint="caption"' in zh
    assert '{"literalString": ""}' in zh
    assert "共同包在一个 Column 里" in zh


def test_a2ui_prompt_heading_text_is_plain_text():
    en = build_a2ui_autonomy_instruction("en")

    assert "MUST contain plain text only" in en
    assert "do not use any Markdown syntax" in en
    assert "parsed as a bullet list" in en

    zh = build_a2ui_autonomy_instruction("zh")

    assert "必须是纯文本" in zh
    assert "禁止任何 Markdown 语法" in zh
    assert "会被解析成无序列表" in zh


def test_a2ui_prompt_wraps_outermost_ui_in_card():
    en = build_a2ui_autonomy_instruction("en")

    assert "outermost UI MUST be wrapped in a Card" in en
    assert "Column for vertical" in en

    zh = build_a2ui_autonomy_instruction("zh")

    assert "最外层 UI 统一用 Card 承载" in zh
    assert "纵向 Column、横向 Row、重复列表 List" in zh


def test_a2ui_prompt_enforces_single_parent_tree():
    en = build_a2ui_autonomy_instruction("en")

    assert "single-parent" in en
    assert "children.explicitList" in en

    zh = build_a2ui_autonomy_instruction("zh")

    assert "单亲树" in zh
    assert "不得再出现在任何 Column/Row/List 的 children.explicitList" in zh


def test_a2ui_prompt_multiplechoice_single_select_max_one():
    en = build_a2ui_autonomy_instruction("en")

    assert "single-select, set maxAllowedSelections to 1" in en

    zh = build_a2ui_autonomy_instruction("zh")

    assert "maxAllowedSelections 必须设为 1" in zh


def test_a2ui_prompt_ok_cancel_button_row_right_aligned():
    en = build_a2ui_autonomy_instruction("en")

    assert 'distribution to "end"' in en
    assert "Cancel (secondary) before OK (primary)" in en
    assert "Place a Divider directly above the button row" in en

    zh = build_a2ui_autonomy_instruction("zh")

    assert 'distribution="end"' in zh
    assert "取消（次要）在前、确定（主要）在后" in zh
    assert "按钮行上方必须放一条 Divider 分割线" in zh


def test_a2ui_prompt_forbids_emoji_in_ui_text():
    en = build_a2ui_autonomy_instruction("en")

    assert "MUST NOT contain emoji" in en

    zh = build_a2ui_autonomy_instruction("zh")

    assert "禁止使用 emoji" in zh


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
    assert "JiuwenSwarm 支持 A2UI 输出格式" in prompt
    assert "交互场景用 A2UI" in prompt
    assert "浣犳槸" not in prompt
    assert "鐢熸垚" not in prompt


def test_a2ui_en_prompt_does_not_use_generator_identity():
    prompt = build_a2ui_prompt_section("en")

    assert "You are jiuwenswarm's A2UI generator" not in prompt
    assert "JiuwenSwarm supports A2UI output format" in prompt


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
