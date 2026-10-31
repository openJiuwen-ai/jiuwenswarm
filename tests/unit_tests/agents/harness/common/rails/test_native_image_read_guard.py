# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""原生图片输入（[[S_22]]）的 stream rail 护栏：read_file 重复读 / 批量 / 视觉工具。

回归焦点：
1. 已随消息提供像素的图片路径（含 ``file_paths`` 批量中任一项）必须整批拒绝；
2. 未提供的图片在原生模式放行（交给 ReadFileTool 注入像素），非图片二进制仍拒绝；
3. 原生模式禁止再调视觉理解工具；原生关闭时视觉工具是唯一读图通道，不拦截。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    _PROVIDED_IMAGE_PATHS_EXTRA_KEY,
    JiuSwarmStreamEventRail,
)


class _FakeSession:
    def __init__(self) -> None:
        self.outputs: list = []

    async def write_stream(self, output) -> None:
        self.outputs.append(output)

    @staticmethod
    def get_session_id() -> str:
        return "sess-native-image"


def _rail(native: bool) -> JiuSwarmStreamEventRail:
    rail = JiuSwarmStreamEventRail()
    rail._deep_agent = SimpleNamespace(
        deep_config=SimpleNamespace(enable_read_image_multimodal=native)
    )
    return rail


def _ctx(
    extra: dict,
    call_id: str,
    name: str,
    arguments: dict,
    *,
    provided_paths: list[str] | None = None,
) -> SimpleNamespace:
    if provided_paths is not None:
        extra[_PROVIDED_IMAGE_PATHS_EXTRA_KEY] = list(provided_paths)
    return SimpleNamespace(
        session=_FakeSession(),
        agent=None,
        context=None,
        extra=extra,
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id=call_id, name=name, arguments=arguments),
            tool_name=name,
            tool_args=arguments,
        ),
    )


def _result_text(ctx: SimpleNamespace) -> str:
    return str(getattr(ctx.inputs, "tool_result", "") or "")


@pytest.mark.asyncio
async def test_native_mode_rejects_batch_read_when_any_path_is_provided():
    rail = _rail(True)
    ctx = _ctx(
        {},
        "call_batch",
        "read_file",
        {"file_paths": ["/uploads/a.png", "/uploads/notes.txt"]},
        provided_paths=["/uploads/a.png"],
    )

    await rail.before_tool_call(ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert "已作为图像内容随本次消息直接提供" in _result_text(ctx)


@pytest.mark.asyncio
async def test_native_mode_allows_unprovided_image_and_rejects_other_binary():
    rail = _rail(True)

    image_ctx = _ctx({}, "call_img", "read_file", {"file_path": "/uploads/new.png"})
    await rail.before_tool_call(image_ctx)
    assert not image_ctx.extra.get("_skip_tool")
    # 图片路径交给 ReadFileTool，本层不改写参数
    assert image_ctx.inputs.tool_args == {"file_path": "/uploads/new.png"}

    binary_ctx = _ctx({}, "call_bin", "read_file", {"file_path": "/uploads/archive.zip"})
    await rail.before_tool_call(binary_ctx)
    assert binary_ctx.extra.get("_skip_tool") is True
    assert "[READ_FILE_ERROR]" in _result_text(binary_ctx)


@pytest.mark.asyncio
async def test_native_mode_rejects_vision_tool():
    rail = _rail(True)
    ctx = _ctx({}, "call_vqa", "visual_question_answering", {"image_path_or_url": "/a.png"})

    await rail.before_tool_call(ctx)

    assert ctx.extra.get("_skip_tool") is True
    assert "[VISION_TOOL_SKIPPED]" in _result_text(ctx)


@pytest.mark.asyncio
async def test_non_native_mode_keeps_vision_tool_and_blocks_batch_non_text():
    rail = _rail(False)

    vqa_ctx = _ctx({}, "call_vqa", "visual_question_answering", {"image_path_or_url": "/a.png"})
    await rail.before_tool_call(vqa_ctx)
    assert not vqa_ctx.extra.get("_skip_tool")

    binary_ctx = _ctx({}, "call_bin", "read_file", {"file_paths": ["/uploads/archive.zip"]})
    await rail.before_tool_call(binary_ctx)
    assert binary_ctx.extra.get("_skip_tool") is True
    assert "[READ_FILE_ERROR]" in _result_text(binary_ctx)
