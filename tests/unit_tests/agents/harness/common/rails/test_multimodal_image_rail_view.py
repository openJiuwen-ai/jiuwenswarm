# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""发送侧唯一决策点的测试：按能力 + 按引用出像素，历史留 base64。

覆盖硬约束：
1. 支持看图时，缩小后的像素写进历史消息；下一轮直接复用，不再读文件。
2. 不支持看图时，只在视图副本里剥掉 image_url，历史里的 base64 保留。
3. 单次请求超预算时，最老的图片改成文字说明并记入历史，之后不再自动加回。
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
    build_image_reference_line,
    collect_image_references_from_messages,
    ensure_multimodal_image_filter_window_mutator,
    ensure_multimodal_image_window_mutator,
    estimate_image_visual_tokens,
    filter_image_content_from_view,
    inject_image_content_from_references,
    is_current_multimodal_image_path,
    is_image_content_block,
    is_provided_image_path,
    reset_current_multimodal_image_files,
    set_current_multimodal_image_files,
)
from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    _PROVIDED_IMAGE_PATHS_EXTRA_KEY,
    JiuSwarmStreamEventRail,
)


def _tiny_png() -> bytes:
    image = Image.new("RGB", (64, 64), (210, 70, 60))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class _FakeContext:
    """只暴露 rail/mutator 依赖的 ModelContext 表面：get_messages + _window_mutators。"""

    def __init__(self, messages: list) -> None:
        self._messages = list(messages)
        self._window_mutators: list = []

    def get_messages(self):
        return self._messages


class _FakeWindow:
    """带 ``model_copy`` 的 ContextWindow 假体，返回视图副本。"""

    def __init__(self, context_messages: list) -> None:
        self.context_messages = list(context_messages)

    def model_copy(self, update=None) -> "_FakeWindow":
        update = update or {}
        return _FakeWindow(list(update.get("context_messages", self.context_messages)))


def _ctx(context) -> SimpleNamespace:
    return SimpleNamespace(context=context)


def _has_image_block(content) -> bool:
    return isinstance(content, list) and any(
        is_image_content_block(part) for part in content
    )


# ---- 过滤侧：视图级，不就地改历史 ---- #


def test_filter_image_content_from_view_strips_image_blocks_keeps_original():
    original_content = [
        {"type": "text", "text": "你好"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    msg = {"role": "user", "content": original_content}

    view_messages, removed = filter_image_content_from_view([msg])

    assert removed == 1
    # 历史原样：原 message 及其 content 未被改动
    assert msg["content"] is original_content
    assert original_content[1]["type"] == "image_url"
    assert _has_image_block(msg["content"])
    # 视图副本：image_url 块被过滤
    view_content = view_messages[0]["content"]
    assert not _has_image_block(view_content)
    assert view_messages[0] is not msg


def test_filter_image_content_from_view_noop_when_no_image_blocks():
    msg = {"role": "user", "content": [{"type": "text", "text": "普通文本"}]}

    view_messages, removed = filter_image_content_from_view([msg])

    assert removed == 0
    assert view_messages[0] is msg


def test_provided_image_path_matches_normalized_path_without_contextvar():
    # rail 从 ctx.extra 传入快照：不依赖 ContextVar，跨任务也能判定。
    assert is_provided_image_path("/tmp/a/../a/home.png", ["/tmp/a/home.png"]) is True
    assert is_provided_image_path("/tmp/a/home.png", []) is False
    assert is_provided_image_path("", ["/tmp/a/home.png"]) is False


def test_provided_image_paths_snapshot_survives_contextvar_reset():
    """回归：工具跨任务执行时 ContextVar 丢失，invoke 快照仍能识别已提供图片。"""
    rail = JiuSwarmStreamEventRail.__new__(JiuSwarmStreamEventRail)
    ctx = SimpleNamespace(extra={})
    path = "/uploads/home.png"
    token = set_current_multimodal_image_files(
        [{"path": path, "filename": "home.png", "mime_type": "image/png"}]
    )
    try:
        assert rail._provided_image_paths(ctx) == [path]
    finally:
        reset_current_multimodal_image_files(token)

    # ContextVar 已还原（模拟工具在另一个任务执行），ctx.extra 快照仍可判定。
    assert ctx.extra[_PROVIDED_IMAGE_PATHS_EXTRA_KEY] == [path]
    assert rail._provided_image_paths(ctx) == [path]


# ---- 注入侧：按引用出像素，就地写入历史（路线二） ---- #


def test_inject_image_content_from_references_writes_pixels_into_history(tmp_path):
    p = tmp_path / "home.png"
    p.write_bytes(_tiny_png())
    line = build_image_reference_line(
        {"path": str(p), "filename": "home.png", "mime_type": "image/png"}, 1
    )
    msg = {"role": "user", "content": "对比一下\n" + line}

    view_messages, injected = inject_image_content_from_references([msg])

    assert injected == 1
    # 路线二：缩小后的像素写进历史消息本身（持久化 base64），不是视图副本
    content = msg["content"]
    assert isinstance(content, list)
    assert _has_image_block(content)
    assert view_messages[0] is msg
    joined = "\n".join(
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    )
    # 引用行原文保留（可按原文件重新出图），并追加 attached 状态行
    assert "[image #1] path=" in joined
    assert "[image #1 attached]" in joined

    # 下一轮直接复用消息里的 base64：不再读文件、不再重复注入
    _, injected_again = inject_image_content_from_references([msg])
    assert injected_again == 0
    blocks = [part for part in msg["content"] if is_image_content_block(part)]
    assert len(blocks) == 1


def test_inject_image_content_from_references_noop_without_references():
    msg = {"role": "user", "content": "没有图片引用"}

    view_messages, injected = inject_image_content_from_references([msg])

    assert injected == 0
    assert view_messages[0] is msg


def _reference_message(tmp_path, index: int, question: str):
    p = tmp_path / f"img{index}.png"
    p.write_bytes(_tiny_png())
    line = build_image_reference_line(
        {"path": str(p), "filename": p.name, "mime_type": "image/png"}, index
    )
    return {"role": "user", "content": f"{question}\n{line}"}


def test_inject_image_content_drops_oldest_new_image_when_over_count_budget(tmp_path):
    """超张数预算：最老的新图不注入像素，改成文字说明记入历史，之后不再加回。"""

    msgs = [_reference_message(tmp_path, i, f"问题{i}") for i in (1, 2, 3)]

    _, injected = inject_image_content_from_references(msgs, max_count=2)

    assert injected == 2
    joined_1 = "\n".join(
        part.get("text", "")
        for part in msgs[0]["content"]
        if isinstance(part, dict) and part.get("type") == "text"
    )
    assert "[image #1 omitted: request budget]" in joined_1
    assert not _has_image_block(msgs[0]["content"])
    assert _has_image_block(msgs[1]["content"])
    assert _has_image_block(msgs[2]["content"])

    # omitted 状态行已记入历史：再次组请求不会把 #1 自动加回
    _, injected_again = inject_image_content_from_references(
        msgs, max_count=10
    )
    assert injected_again == 0
    assert not _has_image_block(msgs[0]["content"])


def test_inject_image_content_offloads_oldest_existing_image_when_over_budget(tmp_path):
    """超预算时优先卸历史里最老的已注入图片，替换成文字说明。"""

    old_msgs = [_reference_message(tmp_path, 1, "问题1"), _reference_message(tmp_path, 2, "问题2")]
    inject_image_content_from_references(old_msgs)  # 无限额注入两张

    new_msg = _reference_message(tmp_path, 3, "问题3")
    messages = [*old_msgs, new_msg]

    _, injected = inject_image_content_from_references(messages, max_count=2)

    assert injected == 1
    # 最老的 #1 像素块被替换成文字说明，#2 保留像素，#3 新注入
    joined_1 = "\n".join(
        part.get("text", "")
        for part in messages[0]["content"]
        if isinstance(part, dict) and part.get("type") == "text"
    )
    assert "[image omitted: request budget]" in joined_1
    assert not _has_image_block(messages[0]["content"])
    assert _has_image_block(messages[1]["content"])
    assert _has_image_block(messages[2]["content"])


def test_estimate_image_visual_tokens_tile_math():
    # 512x512 → 1 tile：85 + 170 = 255
    assert estimate_image_visual_tokens(512, 512) == 255
    # 513x513 → 2x2 tiles：85 + 170*4 = 765
    assert estimate_image_visual_tokens(513, 513) == 765
    # 零/负尺寸视为不可计量
    assert estimate_image_visual_tokens(0, 0) == 0
    assert estimate_image_visual_tokens(-1, 100) == 0


def test_inject_image_content_drops_oldest_when_over_visual_token_budget(tmp_path):
    """视觉 token 预算：每张 64x64=255 token，预算 300 只够放 1 张，最老的两张裁掉。"""

    msgs = [_reference_message(tmp_path, i, f"问题{i}") for i in (1, 2, 3)]

    _, injected = inject_image_content_from_references(msgs, max_visual_tokens=300)

    assert injected == 1
    joined_1 = "\n".join(
        part.get("text", "")
        for part in msgs[0]["content"]
        if isinstance(part, dict) and part.get("type") == "text"
    )
    joined_2 = "\n".join(
        part.get("text", "")
        for part in msgs[1]["content"]
        if isinstance(part, dict) and part.get("type") == "text"
    )
    assert "[image #1 omitted: request budget]" in joined_1
    assert "[image #2 omitted: request budget]" in joined_2
    assert not _has_image_block(msgs[0]["content"])
    assert not _has_image_block(msgs[1]["content"])
    assert _has_image_block(msgs[2]["content"])


def test_collect_image_references_from_messages_scans_multiple():
    msgs = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": build_image_reference_line(
                {"path": "/a.png", "filename": "a.png", "mime_type": "image/png"}, 1
            ),
        },
        {"role": "assistant", "content": "ok"},
        {
            "role": "user",
            "content": build_image_reference_line(
                {"path": "/b.png", "filename": "b.png", "mime_type": "image/png"}, 2
            ),
        },
    ]

    refs = collect_image_references_from_messages(msgs)

    assert [r["index"] for r in refs] == [1, 2]
    assert [r["path"] for r in refs] == ["/a.png", "/b.png"]


# ---- mutator：注入写历史 / 过滤走视图副本 ---- #


@pytest.mark.asyncio
async def test_ensure_multimodal_image_window_mutator_injects_into_history(tmp_path):
    p = tmp_path / "home.png"
    p.write_bytes(_tiny_png())
    line = build_image_reference_line(
        {"path": str(p), "filename": "home.png", "mime_type": "image/png"}, 1
    )
    msg = {"role": "user", "content": "对比\n" + line}
    context = _FakeContext([msg])

    installed = ensure_multimodal_image_window_mutator(context)

    assert installed is True
    assert len(context._window_mutators) == 1
    window = _FakeWindow([msg])
    result = await context._window_mutators[0](context, window)

    # 像素就地写入持久历史：window 是新副本，消息对象与 buffer 共享
    assert result is not window
    assert result.context_messages[0] is msg
    assert _has_image_block(msg["content"])
    assert "data:image/" in str(msg["content"])

    # 再次组窗口：状态行已记录，不再读文件重复注入，原 window 原样返回
    window2 = _FakeWindow([msg])
    result2 = await context._window_mutators[0](context, window2)
    assert result2 is window2


@pytest.mark.asyncio
async def test_ensure_multimodal_image_filter_window_mutator_filters_in_view():
    content = [
        {"type": "text", "text": "你好"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    msg = {"role": "user", "content": content}
    context = _FakeContext([msg])

    installed = ensure_multimodal_image_filter_window_mutator(context)

    assert installed is True
    assert len(context._window_mutators) == 1
    window = _FakeWindow([msg])
    result = await context._window_mutators[0](context, window)

    assert result is not window
    assert not _has_image_block(result.context_messages[0]["content"])
    assert msg["content"] is content
    assert content[1]["type"] == "image_url"


# ---- rail：按能力决策，唯一决策点 ---- #


@pytest.mark.asyncio
async def test_rail_capable_installs_inject_mutator():
    rail = MultimodalImageRail(enable_image_multimodal=True)
    context = _FakeContext([{"role": "user", "content": "hi"}])
    ctx = _ctx(context)

    await rail.before_model_call(ctx)

    assert len(context._window_mutators) == 1


@pytest.mark.asyncio
async def test_rail_not_capable_installs_filter_mutator():
    rail = MultimodalImageRail(enable_image_multimodal=False)
    context = _FakeContext([{"role": "user", "content": "hi"}])
    ctx = _ctx(context)

    await rail.before_model_call(ctx)

    assert len(context._window_mutators) == 1


@pytest.mark.asyncio
async def test_rail_context_none_is_noop():
    rail = MultimodalImageRail(enable_image_multimodal=True)

    await rail.before_model_call(_ctx(None))


def test_is_current_multimodal_image_path_matches_bound_upload():
    token = set_current_multimodal_image_files(
        [{"path": "/sessions/s/uploads/home.png", "mime_type": "image/png"}]
    )
    try:
        assert is_current_multimodal_image_path("/sessions/s/uploads/home.png")
        assert not is_current_multimodal_image_path("/sessions/s/uploads/other.png")
        assert not is_current_multimodal_image_path("")
    finally:
        reset_current_multimodal_image_files(token)

