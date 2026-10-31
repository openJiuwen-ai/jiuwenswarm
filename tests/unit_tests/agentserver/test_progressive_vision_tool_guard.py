# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ProgressiveToolRail 必须拦截原生看图模型下的视觉理解工具调用。

判定只依赖 ``deep_config.enable_read_image_multimodal``（当前模型能力），
不依赖请求级 ContextVar 或图片路径匹配：工具执行可能跨任务/线程，
ContextVar 会丢失，路径匹配会漏拦。
"""

# pylint: disable=protected-access

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)
from jiuwenswarm.agents.harness.common.tools.invoke_tool_tool import (
    InvokeToolInput,
)


def _rail(native_image: bool) -> ProgressiveToolRail:
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool"])
    rail._deep_agent = SimpleNamespace(
        deep_config=SimpleNamespace(enable_read_image_multimodal=native_image)
    )
    return rail


@pytest.mark.asyncio
async def test_vision_tool_blocked_when_native_image_enabled(tmp_path):
    image = tmp_path / "2.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    rail = _rail(native_image=True)

    result = await rail._invoke_target_tool_with_binding(
        session=None,
        params=InvokeToolInput(
            tool_name="visual_question_answering",
            arguments={"image_path_or_url": str(image), "question": "描述这张图"},
        ),
    )

    assert result["success"] is False
    assert result["tool_name"] == "visual_question_answering"
    assert "[VISION_TOOL_SKIPPED]" in result["error"]


@pytest.mark.asyncio
async def test_vision_tool_blocked_without_request_image_context(tmp_path):
    """ContextVar 丢失时也必须拦截（跨线程/任务场景）。"""
    image = tmp_path / "2.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    rail = _rail(native_image=True)

    result = await rail._invoke_target_tool_with_binding(
        session=None,
        params=InvokeToolInput(
            tool_name="image_reading",
            arguments={"path": str(image)},
        ),
    )

    assert result["success"] is False
    assert "[VISION_TOOL_SKIPPED]" in result["error"]


@pytest.mark.asyncio
async def test_vision_tool_not_blocked_when_native_image_disabled(tmp_path):
    image = tmp_path / "2.jpg"
    image.write_bytes(b"\xff\xd8\xff\xd9")
    rail = _rail(native_image=False)

    result = await rail._invoke_target_tool_with_binding(
        session=None,
        params=InvokeToolInput(
            tool_name="visual_question_answering",
            arguments={"image_path_or_url": str(image), "question": "描述这张图"},
        ),
    )

    assert "[VISION_TOOL_SKIPPED]" not in str(result.get("error") or "")


def _card(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        id=f"id-{name}",
        description=f"desc-{name}",
        input_params={},
        properties={},
    )


@pytest.mark.asyncio
async def test_vision_tools_removed_from_schema_when_native_image_enabled():
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool", "bash"])
    rail._lazy_load_active_for_ctx = lambda ctx: False
    tools = [
        _card("bash"),
        _card("read_file"),
        _card("visual_question_answering"),
        _card("image_reading"),
    ]
    spb = SimpleNamespace(sections=[], add_section=lambda section: spb.sections.append(section))
    deep_agent = SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: list(tools)),
        system_prompt_builder=spb,
        deep_config=SimpleNamespace(enable_read_image_multimodal=True),
    )
    # 生产环境中 ctx.agent 是包装对象，不持有 deep_config；能力必须从
    # init() 注入的 _deep_agent 读取。
    wrapper = SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: list(tools)),
        system_prompt_builder=spb,
    )
    rail._deep_agent = deep_agent
    rail._config_agent = deep_agent
    ctx = SimpleNamespace(agent=wrapper, inputs=SimpleNamespace(tools=list(tools)))

    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["bash", "read_file"]


@pytest.mark.asyncio
async def test_meta_tools_removed_from_schema_when_lazy_load_bypassed():
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool", "bash"])
    rail._lazy_load_active_for_ctx = lambda ctx: False
    tools = [_card("bash"), _card("tools_search"), _card("invoke_tool")]
    spb = SimpleNamespace(sections=[], add_section=lambda section: spb.sections.append(section))
    agent = SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: list(tools)),
        system_prompt_builder=spb,
        deep_config=SimpleNamespace(enable_read_image_multimodal=False),
    )
    rail._deep_agent = agent
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(tools)))

    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["bash"]


@pytest.mark.asyncio
async def test_vision_tools_kept_in_schema_when_native_image_disabled():
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool", "bash"])
    rail._lazy_load_active_for_ctx = lambda ctx: False
    tools = [_card("bash"), _card("visual_question_answering")]
    spb = SimpleNamespace(sections=[], add_section=lambda section: spb.sections.append(section))
    agent = SimpleNamespace(
        ability_manager=SimpleNamespace(list=lambda: list(tools)),
        system_prompt_builder=spb,
        deep_config=SimpleNamespace(enable_read_image_multimodal=False),
    )
    rail._deep_agent = agent
    rail._runtime_agent = agent
    ctx = SimpleNamespace(agent=agent, inputs=SimpleNamespace(tools=list(tools)))

    await rail.before_model_call(ctx)

    assert [tool.name for tool in ctx.inputs.tools] == ["bash", "visual_question_answering"]
