# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace

import pytest


VALID_A2UI_RESPONSE = """<a2ui-json>
[
  {
    "beginRendering": {
      "surfaceId": "default",
      "root": "root"
    }
  },
  {
    "surfaceUpdate": {
      "surfaceId": "default",
      "components": [
        {
          "id": "root",
          "component": {
            "Text": {
              "text": {
                "literalString": "Valid repaired UI"
              }
            }
          }
        }
      ]
    }
  }
]
</a2ui-json>"""

UNTAGGED_A2UI_LIKE_RESPONSE = """beginRendering

Email summary:

- Found 5 messages from openjiuwen
- 1 message needs a reply
"""


INVALID_A2UI_RESPONSE = """Here is the UI.
<a2ui-json>
[
  {
    "type": "beginRendering",
    "version": "0.8"
  }
]
</a2ui-json>"""


@pytest.mark.asyncio
async def test_a2ui_finalizer_repairs_invalid_tagged_response():
    from jiuwenswarm.server.runtime.a2ui.runtime.finalizer import A2UIResponseFinalizer

    prompts = []

    async def repair_call(prompt: str):
        prompts.append(prompt)
        return SimpleNamespace(content=VALID_A2UI_RESPONSE)

    result = await A2UIResponseFinalizer().finalize(
        INVALID_A2UI_RESPONSE,
        user_query="生成库存展示界面",
        request_id="req-finalizer-repair",
        repair_call=repair_call,
    )

    assert result == VALID_A2UI_RESPONSE
    assert len(prompts) == 1
    assert "生成库存展示界面" in prompts[0]
    assert "A2UI 0.8" in prompts[0]


@pytest.mark.asyncio
async def test_a2ui_finalizer_repairs_untagged_a2ui_like_response():
    from jiuwenswarm.server.runtime.a2ui.runtime.finalizer import A2UIResponseFinalizer

    prompts = []

    async def repair_call(prompt: str):
        prompts.append(prompt)
        return SimpleNamespace(content=VALID_A2UI_RESPONSE)

    result = await A2UIResponseFinalizer().finalize(
        UNTAGGED_A2UI_LIKE_RESPONSE,
        user_query="summarize openjiuwen email and confirm reply",
        request_id="req-finalizer-untagged-repair",
        repair_call=repair_call,
    )

    assert result == VALID_A2UI_RESPONSE
    assert len(prompts) == 1
    assert "A2UI tags" in prompts[0]
    assert "beginRendering" in prompts[0]


@pytest.mark.asyncio
async def test_a2ui_finalizer_falls_back_after_failed_repairs():
    from jiuwenswarm.server.runtime.a2ui.runtime.finalizer import A2UIResponseFinalizer

    async def repair_call(prompt: str):
        return SimpleNamespace(content=INVALID_A2UI_RESPONSE)

    result = await A2UIResponseFinalizer().finalize(
        INVALID_A2UI_RESPONSE,
        user_query="生成库存展示界面",
        request_id="req-finalizer-fallback",
        repair_call=repair_call,
    )

    assert "<a2ui-json>" not in result
    assert "A2UI 界面生成失败" not in result
    assert result == "Here is the UI."


@pytest.mark.asyncio
async def test_a2ui_finalizer_treats_empty_repair_output_as_failure():
    """Empty repair output must never count as a valid repair.

    Regression for case #26 (2026-10-29): the adapter's repair_model_response
    returns None when no model instance is available; coercing that to "" used
    to pass ``validate_response("")`` vacuously (no blocks -> valid), so the
    finalizer reported status="repaired" with empty content. Downstream,
    interface.py skips the suppressed-final resend for falsy finalized content
    and the whole A2UI block is silently lost (persisted probe residue only).
    """
    from jiuwenswarm.server.runtime.a2ui.runtime.finalizer import A2UIResponseFinalizer

    calls: list[str] = []

    async def repair_call(prompt: str):
        calls.append(prompt)
        return SimpleNamespace(content="")

    result = await A2UIResponseFinalizer().finalize_result(
        INVALID_A2UI_RESPONSE,
        user_query="生成库存展示界面",
        request_id="req-finalizer-empty-repair",
        repair_call=repair_call,
    )

    # 空输出不得标成 repaired；应跑满 max_repair_attempts 后落 repair_failed
    assert result.status == "repair_failed"
    assert len(calls) == 2
    # 返回内容必须非空（基于原始内容可读降级），否则 suppress 下重发被跳过
    assert result.content.strip() != ""


@pytest.mark.asyncio
async def test_a2ui_finalizer_treats_none_repair_return_as_failure():
    """repair_call returning bare None (no model available) is a failed repair."""
    from jiuwenswarm.server.runtime.a2ui.runtime.finalizer import A2UIResponseFinalizer

    async def repair_call(prompt: str):
        return None

    result = await A2UIResponseFinalizer().finalize_result(
        INVALID_A2UI_RESPONSE,
        user_query="生成库存展示界面",
        request_id="req-finalizer-none-repair",
        repair_call=repair_call,
    )

    assert result.status == "repair_failed"
    assert result.content.strip() != ""


@pytest.mark.asyncio
async def test_a2ui_finalizer_leaves_plain_text_untouched():
    from jiuwenswarm.server.runtime.a2ui.runtime.finalizer import A2UIResponseFinalizer

    async def repair_call(prompt: str):
        raise AssertionError("plain text must not trigger repair")

    result = await A2UIResponseFinalizer().finalize(
        "plain answer",
        user_query="你好",
        request_id="req-finalizer-plain",
        repair_call=repair_call,
    )

    assert result == "plain answer"
