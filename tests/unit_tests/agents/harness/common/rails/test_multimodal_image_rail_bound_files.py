# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MultimodalImageRail 的「装配期绑定清单」分支（team leader 使用）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.multimodal.image_reference import (
    store_current_multimodal_image_files_on_agent,
)
from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)

_AGENT_IMAGES = [
    {
        "type": "image",
        "path": "/tmp/agent-attr.jpg",
        "filename": "agent-attr.jpg",
        "mime_type": "image/jpeg",
    }
]
_BOUND_IMAGES = [
    {"type": "image", "path": "/tmp/round-upload.jpg", "filename": "round-upload.jpg"}
]


def test_bound_manifest_wins_over_agent_attribute() -> None:
    """装配期绑定的清单优先于 agent 属性镜像（team leader 的清单只存在于 build context）。"""

    agent = SimpleNamespace()
    store_current_multimodal_image_files_on_agent(agent, _AGENT_IMAGES)
    rail = MultimodalImageRail(image_files=_BOUND_IMAGES)
    rail.init(agent)

    assert rail._resolve_request_image_files() == _BOUND_IMAGES


def test_empty_bound_manifest_does_not_fall_back_to_agent_attribute() -> None:
    """空清单是权威事实：本轮没有新上传，不得回退读 agent 上一轮的残留。"""

    agent = SimpleNamespace()
    store_current_multimodal_image_files_on_agent(agent, _AGENT_IMAGES)
    rail = MultimodalImageRail(image_files=[])
    rail.init(agent)

    assert rail._resolve_request_image_files() == []


def test_unbound_rail_falls_back_to_agent_attribute() -> None:
    """没传 image_files 时保持单 agent 旧行为：读 agent 实例镜像。"""

    agent = SimpleNamespace()
    store_current_multimodal_image_files_on_agent(agent, _AGENT_IMAGES)
    rail = MultimodalImageRail()
    rail.init(agent)

    assert rail._resolve_request_image_files() == _AGENT_IMAGES


def test_unbound_rail_without_agent_mirror_returns_none() -> None:
    """单 agent 旧行为：agent 上没有镜像 → None，交给 inject 回退 ContextVar。"""

    rail = MultimodalImageRail()
    rail.init(SimpleNamespace())

    assert rail._resolve_request_image_files() is None


# ---------------------------------------------------------------------------
# steer 补图第三通道：轮中途把补充图片追加进绑定快照（team leader 本轮生效）
# ---------------------------------------------------------------------------

_STEER_IMAGES = [
    {"type": "image", "path": "/tmp/steer-1.jpg", "filename": "steer-1.jpg"},
    {"type": "image", "path": "/tmp/steer-2.jpg", "filename": "steer-2.jpg"},
]


def test_append_round_image_files_extends_bound_manifest() -> None:
    """追加进绑定快照后 ``_resolve_request_image_files`` 立即可见（本轮可物化）。"""

    rail = MultimodalImageRail(image_files=list(_BOUND_IMAGES))

    added = rail.append_round_image_files(_STEER_IMAGES)

    assert added == 2
    resolved = rail._resolve_request_image_files()
    assert [item["path"] for item in resolved] == [
        "/tmp/round-upload.jpg",
        "/tmp/steer-1.jpg",
        "/tmp/steer-2.jpg",
    ]


def test_append_round_image_files_is_idempotent_on_normalized_path() -> None:
    """重复追加 / 归一化同路径（相对段形态）/ 空 path：不重复进快照。"""

    rail = MultimodalImageRail(image_files=list(_BOUND_IMAGES))
    rail.append_round_image_files(_STEER_IMAGES)

    added = rail.append_round_image_files(
        [
            {"type": "image", "path": "/tmp/./steer-1.jpg"},
            {"type": "image", "path": ""},
            "not-a-dict",
        ]
    )

    assert added == 0
    assert len(rail._bound_image_files) == 3


def test_append_round_image_files_never_binds_unbound_rail() -> None:
    """未绑定清单的 rail（deep 单代理）不创建绑定：空清单是权威事实。"""

    rail = MultimodalImageRail()

    assert rail.append_round_image_files(_STEER_IMAGES) == 0
    assert rail._bound_image_files is None


class _RecordingPromptBuilder:
    """最小 PromptSection 容器：只记录 add/remove。"""

    def __init__(self) -> None:
        self.sections: dict[str, object] = {}

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)

    def add_section(self, section) -> None:
        self.sections[section.name] = section


def _policy_text(rail: MultimodalImageRail, images_sent: int = 2) -> str:
    builder = _RecordingPromptBuilder()
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    rail._sync_image_policy_section(True, images_sent)
    return builder.sections["multimodal_image_policy"].content["cn"]


def test_team_member_path_guidance_points_to_native_read_file() -> None:
    """成员还没拿到像素时，引导用 read_file 原生读图，而不是 VQA/OCR。"""

    rail = MultimodalImageRail(enable_image_multimodal=True, image_files=_MANIFEST)

    text = _policy_text(rail, images_sent=0)

    assert "read_file" in text
    assert "不要调用 visual_question_answering" in text
    assert "已作为图像内容直接提供" not in text


def test_member_policy_switches_to_native_once_pixels_sent() -> None:
    """像素已提供后改用原生策略段（不再引导 read_file）。"""

    rail = MultimodalImageRail(enable_image_multimodal=True, image_files=_MANIFEST)

    text = _policy_text(rail, images_sent=2)

    assert "已作为图像内容直接提供" in text
    assert "不要调用 visual_question_answering" in text
    assert "请直接使用 read_file" not in text


def test_leader_and_single_agent_get_no_path_guidance() -> None:
    """leader（dispatch_hint）/ 单 agent（未绑定清单）没有像素时不写成员路径引导段。

    合并 feat/native-multimodal 后：主代理/leader 0 张时写「能力 + read_file
    取回路径」能力段；成员专用路径引导（含 VQA/OCR 禁令）仍只对 team 成员生效。
    """

    for rail in (
        MultimodalImageRail(enable_image_multimodal=True, dispatch_hint=True, image_files=_MANIFEST),
        MultimodalImageRail(enable_image_multimodal=True),
    ):
        text = _policy_text(rail, images_sent=0)
        assert "read_file" in text
        assert "不要调用 visual_question_answering" not in text


def test_leader_dispatch_hint_tells_how_to_forward_reference_line() -> None:
    """leader 原生策略段要说明：交给成员时用 send_message 保留引用行。"""

    rail = MultimodalImageRail(enable_image_multimodal=True, dispatch_hint=True)

    text = _policy_text(rail)

    assert "send_message" in text
    assert "[image #n] path=" in text
    assert "任务看板" in text


def test_dispatch_hint_absent_by_default_and_without_images() -> None:
    """默认（teammate / 单 agent）不写转发提示；0 张图时写能力段（含取回路径）。"""

    rail = MultimodalImageRail(enable_image_multimodal=True)
    text = _policy_text(rail)
    assert "任务看板" not in text

    text_no_images = _policy_text(rail, images_sent=0)
    assert "任务看板" not in text_no_images
    # 合并 feat/native-multimodal：0 张时主代理拿能力段（read_file 取回路径），
    # 而非成员段、也非转发提示。
    assert "read_file" in text_no_images
    assert "不要调用 visual_question_answering" not in text_no_images


_MANIFEST = [
    {
        "type": "image",
        "path": "/tmp/2.jpg",
        "filename": "2.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": 111,
    },
    {
        "type": "image",
        "path": "/tmp/3.jpg",
        "filename": "3.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": 222,
    },
]


def _send_ctx(target, content: str) -> SimpleNamespace:
    arguments = {"to": target, "content": content}
    tool_call = SimpleNamespace(name="send_message", arguments=arguments)
    inputs = SimpleNamespace(
        tool_name="send_message", tool_call=tool_call, tool_args=arguments
    )
    return SimpleNamespace(inputs=inputs)


def test_leader_send_message_gets_missing_reference_line() -> None:
    """leader 消息只写 "3.jpg" 时，工具执行前补成完整引用行（用户消息通道）。"""

    rail = MultimodalImageRail(
        enable_image_multimodal=True, dispatch_hint=True, image_files=_MANIFEST
    )
    rail.init(SimpleNamespace())
    ctx = _send_ctx("vision-analyst", "请识别 3.jpg 并交付描述")

    asyncio.run(rail.before_tool_call(ctx))

    content = ctx.inputs.tool_call.arguments["content"]
    assert "[image #1] path=/tmp/3.jpg filename=3.jpg mime=image/jpeg size=222" in content
    assert ctx.inputs.tool_args["content"] == content


def test_dispatch_augment_skips_broadcast_and_user_targets() -> None:
    """广播 / 回用户不补引用行：避免把上传清单发给无关成员或用户。"""

    for target in ("*", "user", ["vision-analyst", "*"]):
        rail = MultimodalImageRail(
            enable_image_multimodal=True, dispatch_hint=True, image_files=_MANIFEST
        )
        rail.init(SimpleNamespace())
        ctx = _send_ctx(target, "请识别 3.jpg")
        asyncio.run(rail.before_tool_call(ctx))
        assert "[image #" not in ctx.inputs.tool_call.arguments["content"]


def test_dispatch_augment_only_for_leader_and_send_message() -> None:
    """没有 dispatch_hint（teammate）或不是 send_message 时不改写参数。"""

    rail = MultimodalImageRail(enable_image_multimodal=True, image_files=_MANIFEST)
    rail.init(SimpleNamespace())
    ctx = _send_ctx("vision-analyst", "请识别 3.jpg")
    asyncio.run(rail.before_tool_call(ctx))
    assert "[image #" not in ctx.inputs.tool_call.arguments["content"]

    leader = MultimodalImageRail(
        enable_image_multimodal=True, dispatch_hint=True, image_files=_MANIFEST
    )
    leader.init(SimpleNamespace())
    ctx2 = _send_ctx("vision-analyst", "请识别 3.jpg")
    ctx2.inputs.tool_name = "create_task"
    asyncio.run(leader.before_tool_call(ctx2))
    assert "[image #" not in ctx2.inputs.tool_call.arguments["content"]
