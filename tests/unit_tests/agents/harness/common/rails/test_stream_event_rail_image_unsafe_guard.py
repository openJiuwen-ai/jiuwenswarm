# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""StreamEventRail 对「已被上游判 unsafe」图片的 read_file 拦截。

回归焦点（B_09 复审意见）：摘图降级重试后 ``provided_paths`` 变为空，read_file 会
重新放行同一张图的读取；同一 invoke 内再次 ``image_url unsafe`` 时单次护栏已置位、
异常直接上抛，本轮仍 0 chunk 失败。因此摘图前写下的请求级黑名单必须在 read_file
工具护栏里生效——命中即拒绝，并给出继续任务的文案。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.multimodal_image_state import (
    UNSAFE_IMAGE_PATHS_KEY,
    mark_unsafe_image_paths,
    unsafe_image_paths,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)


class _FakeSession:
    def __init__(self) -> None:
        self.outputs: list[object] = []

    async def write_stream(self, output: object) -> None:
        self.outputs.append(output)

    @staticmethod
    def get_session_id() -> str:
        return "sess-1"


def _native_rail() -> JiuSwarmStreamEventRail:
    rail = JiuSwarmStreamEventRail()
    rail._deep_agent = SimpleNamespace(
        deep_config=SimpleNamespace(enable_read_image_multimodal=True),
    )
    return rail


def _read_file_ctx(extra: dict, path: str) -> SimpleNamespace:
    arguments = {"file_path": path}
    return SimpleNamespace(
        session=_FakeSession(),
        agent=None,
        context=None,
        extra=extra,
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call_a", name="read_file", arguments=arguments),
            tool_name="read_file",
            tool_args=arguments,
        ),
    )


@pytest.mark.asyncio
async def test_before_tool_call_rejects_blacklisted_unsafe_image() -> None:
    """命中 unsafe 黑名单的 read_file 被拒绝，不再物化像素。"""

    rail = _native_rail()
    extra: dict = {}
    mark_unsafe_image_paths(
        SimpleNamespace(extra=extra, agent=None),
        ["E:\\imgs\\w2006_p28_0.png"],
        agent=rail._deep_agent,
    )
    ctx = _read_file_ctx(extra, "E:\\imgs\\w2006_p28_0.png")

    await rail.before_tool_call(ctx)

    assert ctx.extra["_skip_tool"] is True
    assert ctx.inputs.tool_msg.content.startswith("[READ_FILE_ERROR]")
    assert "image_url unsafe" in ctx.inputs.tool_msg.content
    assert "不能再次读取同一张图" in ctx.inputs.tool_msg.content


@pytest.mark.asyncio
async def test_before_tool_call_allows_unrelated_unprovided_image() -> None:
    """黑名单只拦命中路径：其它未提供图片仍放行给 ReadFileTool 出图。"""

    rail = _native_rail()
    extra: dict = {}
    mark_unsafe_image_paths(
        SimpleNamespace(extra=extra, agent=None),
        ["E:\\imgs\\w2006_p28_0.png"],
        agent=rail._deep_agent,
    )
    ctx = _read_file_ctx(extra, "E:\\imgs\\other.png")

    await rail.before_tool_call(ctx)

    assert not ctx.extra.get("_skip_tool")
    assert getattr(ctx.inputs, "tool_msg", None) is None


def test_blacklist_mirrors_to_agent_for_ctx_less_execution() -> None:
    """工具执行没有 rail ctx 时，黑名单仍可从 agent 属性读到。"""

    agent = SimpleNamespace()
    mark_unsafe_image_paths(
        SimpleNamespace(extra={}, agent=None), ["/tmp/a.png"], agent=agent
    )

    assert unsafe_image_paths(agent=agent) == ["/tmp/a.png"]
    assert getattr(agent, UNSAFE_IMAGE_PATHS_KEY) == ["/tmp/a.png"]
