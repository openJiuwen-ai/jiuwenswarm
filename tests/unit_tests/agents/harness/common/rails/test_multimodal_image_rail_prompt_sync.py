# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MultimodalImageRail 压缩后同步图片状态提示（P2 修复）。

回归焦点：

- ``before_model_call`` 在 ``get_context_window`` 压缩**之前**按完整历史写策略段
  （``multimodal_image_policy``）与 ``runtime.image_state`` 附件；FullCompact 压缩搬走
  含图历史后，模型实际收到 0 张，但系统提示仍宣称"已提供 N 张、别调 VQA"。新增的
  context 引擎 window mutator 在压缩之后、附件注入之前按最终窗口重建系统提示 / 重写
  附件。
- 附件里的"不要 read_file 重读"只限定到实际随请求提供像素的图片（``provided_paths``
  非空才写），与 read_file 按目标放行护栏一致。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from openjiuwen.harness.prompts.builder import SystemPromptBuilder
from openjiuwen.harness.prompts.prompt_attachment_manager import PromptAttachmentManager

from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)


def _fake_agent() -> SimpleNamespace:
    return SimpleNamespace(
        system_prompt_builder=SystemPromptBuilder(language="cn"),
        prompt_attachment_manager=PromptAttachmentManager(),
        deep_config=SimpleNamespace(enable_read_image_multimodal=True),
    )


def _reference_line(index: int = 1, path: str = "/tmp/a.png") -> str:
    return f"[image #{index}] path={path} filename=a.png mime=image/png"


def _reference_only_message(index: int = 1, path: str = "/tmp/a.png") -> dict:
    """只有引用行、没有 image_url 块：模拟压缩把像素块搬走、引用行残存。"""

    return {
        "role": "user",
        "content": [{"type": "text", "text": _reference_line(index, path)}],
    }


def _provided_image_message(index: int = 1, path: str = "/tmp/a.png") -> dict:
    """引用行 + attached 状态行 + image_url 块：像素仍在最终窗口。"""

    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": f"{_reference_line(index, path)}\n[image #{index} attached]",
            },
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }


class _FakeWindow:
    def __init__(self, system_messages: list, context_messages: list) -> None:
        self.system_messages = system_messages
        self.context_messages = context_messages

    def model_copy(self, update: dict) -> "_FakeWindow":
        return _FakeWindow(
            system_messages=update.get("system_messages", self.system_messages),
            context_messages=self.context_messages,
        )


def _make_ctx(agent: SimpleNamespace, session_id: str = "sess-1") -> SimpleNamespace:
    context = SimpleNamespace(_window_mutators=[])
    session = SimpleNamespace(get_session_id=lambda: session_id)
    return SimpleNamespace(
        context=context,
        session=session,
        agent=agent,
        inputs=SimpleNamespace(messages=[]),
        extra={},
    )


def test_attachment_limits_reread_to_provided_images() -> None:
    """有 provided_paths 时只说"重读这 N 张已提供的图"，不再笼统'读取同一张图'。"""

    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(_fake_agent())

    text = rail._build_image_state_attachment(
        enabled=True, images_sent=2, provided_paths=["/tmp/a.png", "/tmp/b.png"]
    )
    assert "本次请求附带图片：2 张" in text
    assert "重新读取这 2 张已随请求提供像素的图片" in text
    assert "读取同一张图" not in text


def test_attachment_omits_reread_when_no_provided_paths() -> None:
    """provided_paths 为空时不写 read_file 限制（没进消息的图应允许重读）。"""

    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(_fake_agent())

    text = rail._build_image_state_attachment(
        enabled=True, images_sent=1, provided_paths=[]
    )
    assert "read_file" not in text


def test_sync_mutator_resyncs_prompt_after_compaction() -> None:
    """压缩搬走 image block 后，同步 mutator 应删策略段、并把附件重写为 0 张。"""

    agent = _fake_agent()
    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(agent)
    ctx = _make_ctx(agent)

    # 模拟压缩前：before_model_call 已写"有图"策略段 + "1 张"附件。
    rail._sync_image_policy_section(True, 1)
    asyncio.run(
        rail._sync_image_state_attachment(
            ctx,
            enabled=True,
            images_sent=1,
            has_image_references=True,
            provided_paths=["/tmp/a.png"],
        )
    )

    assert rail._install_image_state_sync_window_mutator(ctx, True) is True
    mutator = ctx.context._window_mutators[0]

    # 压缩后窗口：只剩引用行、无 image block（图片被压缩搬走）。
    window = _FakeWindow(
        system_messages=[SimpleNamespace(content="stale-system")],
        context_messages=[_reference_only_message()],
    )
    result = asyncio.run(mutator(None, window))

    # 1) 策略段按最终 0 张删除，重建的系统提示不再宣称"已提供 / 别调 VQA"。
    assert not rail._system_prompt_builder.has_section("multimodal_image_policy")
    assert "不要调用" not in result.system_messages[0].content

    # 2) 附件重写为"0 张"，不再残留压缩前的"1 张"。
    attachments = asyncio.run(
        agent.prompt_attachment_manager.collect_for_session("sess-1")
    )
    assert len(attachments) == 1
    assert "0 张" in attachments[0].content
    assert "1 张" not in attachments[0].content


def test_sync_mutator_keeps_prompt_when_final_window_still_has_images() -> None:
    """最终窗口仍带像素时，同步 mutator 保留策略段与张数，不误删。"""

    agent = _fake_agent()
    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(agent)
    ctx = _make_ctx(agent)

    assert rail._install_image_state_sync_window_mutator(ctx, True) is True
    mutator = ctx.context._window_mutators[0]

    window = _FakeWindow(
        system_messages=[SimpleNamespace(content="stale-system")],
        context_messages=[_provided_image_message()],
    )
    result = asyncio.run(mutator(None, window))

    assert rail._system_prompt_builder.has_section("multimodal_image_policy")
    assert "不要调用" in result.system_messages[0].content

    attachments = asyncio.run(
        agent.prompt_attachment_manager.collect_for_session("sess-1")
    )
    assert len(attachments) == 1
    assert "1 张" in attachments[0].content
    assert "重新读取这 1 张已随请求提供像素的图片" in attachments[0].content