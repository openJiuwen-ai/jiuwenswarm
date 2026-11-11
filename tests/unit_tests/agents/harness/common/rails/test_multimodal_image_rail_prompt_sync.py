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

from jiuwenswarm.agents.harness.common.multimodal.image_history import (
    rewrite_orphan_attached_markers,
)
from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)


def _fake_agent(language: str = "cn") -> SimpleNamespace:
    return SimpleNamespace(
        system_prompt_builder=SystemPromptBuilder(language=language),
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


def test_attachment_no_pixels_routes_by_capability() -> None:
    """0 张时按能力分流:enabled 指路 read_file(与能力段同向),disabled 保留 VQA 兜底。

    回归焦点:压缩器搬走像素块后 attached 状态行残留,附件必须不再写"请调 VQA"
    (原生模型的矛盾信号),而是引导 read_file 重读被压缩省略的图。
    """

    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(_fake_agent())

    native = rail._build_image_state_attachment(enabled=True, images_sent=0)
    assert "本次请求附带图片：0 张" in native
    assert "read_file" in native
    assert "视觉理解能力允许时" not in native
    assert "visual_question_answering" not in native

    legacy = rail._build_image_state_attachment(enabled=False, images_sent=0)
    assert "visual_question_answering 等图片理解工具" in legacy
    assert "read_file" not in legacy


def test_attachment_no_pixels_routes_by_capability_en() -> None:
    """0 张时按能力分流(en):enabled 指路 read_file,disabled 保留 VQA 兜底。

    回归焦点:能力分流曾只改 cn 分支,en 分支保持旧两分——enabled + 0 张时
    附件仍写 "call a vision tool",与能力段 capability_en("call read_file on
    it")互相矛盾,英文会话被推回视觉工具老路。
    """

    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(_fake_agent(language="en"))

    native = rail._build_image_state_attachment(enabled=True, images_sent=0)
    assert "0 images" in native
    assert "read_file" in native
    assert "visual_question_answering" not in native

    legacy = rail._build_image_state_attachment(enabled=False, images_sent=0)
    assert "visual_question_answering" in legacy
    assert "read_file" not in legacy


def test_sync_mutator_rewrites_orphan_attached_markers() -> None:
    """孤儿标记改写:压缩搬走像素块后,残留的 attached 状态行改为 omitted: compacted。

    消费端契约(附件计数 / read_file 护栏的 provided 路径)按 attached 判定
    "已提供、禁止重读";孤儿标记不改口会把无像素的模型锁死(换模型回程事故)。
    """

    agent = _fake_agent()
    rail = MultimodalImageRail(enable_image_multimodal=True)
    rail.init(agent)
    ctx = _make_ctx(agent)

    assert rail._install_image_state_sync_window_mutator(ctx, True) is True
    mutator = ctx.context._window_mutators[0]

    # 压缩后的消息:文本 part 里引用行 + attached 状态行都在,像素块没了。
    window = _FakeWindow(
        system_messages=[SimpleNamespace(content="stale-system")],
        context_messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _reference_line(1, "/tmp/a.png")},
                    {"type": "text", "text": "[image #1 attached]"},
                ],
            }
        ],
    )
    result = asyncio.run(mutator(None, window))

    # 1) 孤儿标记被改口为 omitted: compacted(按行定位,不动引用行)。
    texts = [p["text"] for p in result.context_messages[0]["content"] if p.get("type") == "text"]
    assert any("[image #1 omitted: compacted]" in t for t in texts), texts
    assert not any("[image #1 attached]" in t for t in texts), texts
    assert any("path=/tmp/a.png" in t for t in texts), texts

    # 2) provided 路径不再包含孤儿(护栏据此放行 read_file 重读)。
    rebuilt_policy = str(rail._system_prompt_builder.build())
    assert "你可以直接看图" in rebuilt_policy

    # 3) 像素块仍在的消息不被误伤:标记与块配对就不是孤儿。
    window2 = _FakeWindow(
        system_messages=[SimpleNamespace(content="s")],
        context_messages=[_provided_image_message()],
    )
    result2 = asyncio.run(mutator(None, window2))
    block_texts = [
        p["text"]
        for p in result2.context_messages[0]["content"]
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    assert any("[image #1 attached]" in t for t in block_texts), block_texts


def test_rewrite_orphan_attached_markers_string_summary() -> None:
    """字符串 content(压缩器摘要形态)同样改写孤儿 attached 标记。

    DialogueCompressor 的替换消息是 UserMessage(content=<str>);旧实现的
    isinstance(content, list) 检查会跳过字符串消息,docstring 声称的主场景
    (纯文本摘要残留标记)实际不被处理。字符串消息不可能携带像素块,其中
    残留的 attached 状态行必为孤儿。
    """

    # 压缩摘要形态:引用行 + attached 状态行同在一个纯字符串 content 里。
    summary = {
        "role": "user",
        "content": (
            "[image #1] path=/tmp/a.png filename=a.png mime=image/png\n"
            "用户上传了图片并提问。\n"
            "[image #1 attached]"
        ),
    }
    assert rewrite_orphan_attached_markers([summary]) == 1
    rewritten = summary["content"]
    assert isinstance(rewritten, list), "命中后写回列表形态"
    texts = [str(part.get("text", "")) for part in rewritten if isinstance(part, dict)]
    joined = "\n".join(texts)
    assert "[image #1 attached]" not in joined
    assert "[image #1 omitted: compacted]" in joined
    # 引用行原样保留(它是 read_file 重读的路径来源,不是孤儿)。
    assert "[image #1] path=/tmp/a.png" in joined

    # 无命中的字符串消息保持原形态,不强转列表。
    plain = {"role": "user", "content": "普通文本,没有标记"}
    assert rewrite_orphan_attached_markers([plain]) == 0
    assert plain["content"] == "普通文本,没有标记"


def test_rewrite_orphan_attached_markers_skips_non_user_messages() -> None:
    """tool result / assistant 正文里的同格式行不改写。

    状态行仅由上传入口写入 user 消息;其余角色里的同格式行是文件内容
    (如 read_file 读到的会话导出),就地改写会让持久化历史与磁盘实际
    内容不一致,破坏工具结果的权威记录。
    """

    # read_file 文本文件场景:tool 结果的字符串 content 里带一整行暗号。
    tool_result = {
        "role": "tool",
        "content": "Image file read: log.txt\n[image #5 attached]\nend",
    }
    assert rewrite_orphan_attached_markers([tool_result]) == 0
    assert tool_result["content"] == "Image file read: log.txt\n[image #5 attached]\nend"

    # assistant 消息同样不改写(列表形态)。
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "[image #7 attached]"}],
    }
    assert rewrite_orphan_attached_markers([assistant]) == 0
    assert assistant["content"][0]["text"] == "[image #7 attached]"

    # user 消息照常改写(对照,主场景不受过滤影响)。
    user_msg = {"role": "user", "content": "看这张\n[image #5 attached]"}
    assert rewrite_orphan_attached_markers([user_msg]) == 1
    assert "[image #5 attached]" not in str(user_msg["content"])
    assert "[image #5 omitted: compacted]" in str(user_msg["content"])


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

    # 1) 策略段按最终 0 张降级为能力声明：不再宣称"已提供 / 别调 VQA"，
    #    但保留"你可以直接看图"的能力事实 + read_file 取回路径（enabled 模型
    #    始终知道自己的原生视觉与像素读取入口）。
    assert rail._system_prompt_builder.has_section("multimodal_image_policy")
    rebuilt_policy = str(rail._system_prompt_builder.build())
    assert "你可以直接看图" in rebuilt_policy
    assert "请用 read_file 读取" in rebuilt_policy
    assert "已作为图像内容直接提供" not in rebuilt_policy
    assert "不要调用" not in rebuilt_policy
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