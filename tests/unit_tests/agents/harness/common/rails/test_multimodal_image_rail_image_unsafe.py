# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MultimodalImageRail 在图片被上游内容安全拦截时的降级重试。

回归焦点：模型网关以 400 ``the content of image_url.url field is unsafe`` 拒绝请求
（0 chunk 结束、整轮任务失败）时，``on_model_exception`` 应识别该错误、就地摘除历史
里的图片块、把 ``attached`` 状态改写为 ``omitted: unsafe``，并 ``request_retry(0)``
用纯文本窗口重发一次；同一 ctx 只降级重试一次，避免无限重试。摘图前还必须把本轮
已发出像素的路径记入请求级黑名单，阻止模型 read_file 同一张图再次触发 400。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from jiuwenswarm.agents.harness.common.multimodal import (
    inject_image_content_from_references,
)
from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)
from jiuwenswarm.agents.harness.common.rails.multimodal_image_state import (
    UNSAFE_IMAGE_PATHS_KEY,
    mark_unsafe_image_paths,
    unsafe_image_paths,
)


class _FakeContext:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages

    def get_messages(self) -> list[dict[str, Any]]:
        return self.messages


def _ctx(messages: list[dict[str, Any]], exception: Exception | None):
    retries: list[float] = []
    context = _FakeContext(messages)
    ctx = SimpleNamespace(
        exception=exception,
        extra={},
        context=context,
        inputs=SimpleNamespace(messages=list(messages)),
        request_retry=lambda delay_seconds: retries.append(delay_seconds),
    )
    return ctx, retries


def _reference_image_message() -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "[image #1] path=/tmp/a.png filename=a.png mime=image/png size=12",
            },
            {"type": "text", "text": "[image #1 attached]"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }


def _read_file_image_message() -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "Image loaded from read_file: E:\\imgs\\w2006_p28_0.png"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
        ],
    }


def _unsafe_error() -> Exception:
    return Exception(
        "BadRequestError: Error code: 400 - the content of image_url.url field is unsafe"
    )


def _texts(message: dict[str, Any]) -> list[str]:
    return [
        str(part["text"])
        for part in message["content"]
        if isinstance(part, dict) and "text" in part
    ]


def _has_image_block(message: dict[str, Any]) -> bool:
    return any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in message["content"]
    )


def test_on_model_exception_strips_images_and_retries_once() -> None:
    """image_url unsafe：摘除两类图片块、改写状态行、请求一次重试。"""

    messages = [_reference_image_message(), _read_file_image_message()]
    ctx, retries = _ctx(messages, exception=_unsafe_error())
    rail = MultimodalImageRail(enable_image_multimodal=True)

    asyncio.run(rail.on_model_exception(ctx))

    assert retries == [0.0]
    # 图片块全部摘除。
    assert not _has_image_block(messages[0])
    assert not _has_image_block(messages[1])
    # 引用行注入的图：attached → omitted: unsafe，引用行保留。
    assert any(
        text.startswith("[image #1] path=/tmp/a.png")
        for text in _texts(messages[0])
    )
    assert "[image #1 omitted: unsafe]" in _texts(messages[0])
    # read_file 注入的图：路径说明行保留，像素块换成省略说明。
    assert "Image loaded from read_file: E:\\imgs\\w2006_p28_0.png" in _texts(messages[1])
    assert "[image omitted: unsafe]" in _texts(messages[1])


def test_on_model_exception_retries_only_once() -> None:
    """同一 ctx 第二次命中不再请求重试，异常交上层避免无限重试。"""

    messages = [_reference_image_message()]
    ctx, retries = _ctx(messages, exception=_unsafe_error())
    rail = MultimodalImageRail(enable_image_multimodal=True)

    asyncio.run(rail.on_model_exception(ctx))
    asyncio.run(rail.on_model_exception(ctx))

    assert len(retries) == 1


def test_on_model_exception_ignores_non_image_unsafe() -> None:
    """非 image_url unsafe 的异常不摘图、不重试。"""

    messages = [_reference_image_message()]
    ctx, retries = _ctx(messages, exception=Exception("some other error"))
    rail = MultimodalImageRail(enable_image_multimodal=True)

    asyncio.run(rail.on_model_exception(ctx))

    assert retries == []
    assert _has_image_block(messages[0])
    assert "[image #1 attached]" in _texts(messages[0])
    assert ctx.extra.get(UNSAFE_IMAGE_PATHS_KEY) is None


def test_on_model_exception_blacklists_every_sent_image_path() -> None:
    """摘图前把引用行图与 read_file 图的路径都记为请求级黑名单。

    上游 400 不返回具体哪张图违规，本地只能把本轮已发出像素的全部路径都记为嫌疑；
    之后 read_file 护栏 / 引用行注入按这张黑名单拦截，避免同一张图重新进窗口后
    二次 400 却已无降级额度。
    """

    messages = [_reference_image_message(), _read_file_image_message()]
    ctx, retries = _ctx(messages, exception=_unsafe_error())
    rail = MultimodalImageRail(enable_image_multimodal=True)

    asyncio.run(rail.on_model_exception(ctx))

    assert retries == [0.0]
    blocked = unsafe_image_paths(ctx=ctx)
    assert blocked is not None
    assert "/tmp/a.png" in blocked
    assert "E:\\imgs\\w2006_p28_0.png" in blocked


def test_before_model_call_clears_blacklist_for_new_request() -> None:
    """新 invoke 首次模型调用清空上一请求的 unsafe 黑名单（请求级生命周期）。"""

    ctx = SimpleNamespace(
        extra={UNSAFE_IMAGE_PATHS_KEY: ["/tmp/a.png"]},
        context=None,
        agent=None,
    )
    rail = MultimodalImageRail(enable_image_multimodal=True)

    asyncio.run(rail.before_model_call(ctx))

    assert ctx.extra[UNSAFE_IMAGE_PATHS_KEY] == []


def test_before_model_call_keeps_blacklist_on_degrade_retry() -> None:
    """同一 invoke 的摘图重试重跑 before_model_call 时不得清空黑名单。"""

    ctx = SimpleNamespace(
        extra={
            UNSAFE_IMAGE_PATHS_KEY: ["/tmp/a.png"],
            "_multimodal_image_unsafe_stripped": True,
        },
        context=None,
        agent=None,
    )
    rail = MultimodalImageRail(enable_image_multimodal=True)

    asyncio.run(rail.before_model_call(ctx))

    assert ctx.extra[UNSAFE_IMAGE_PATHS_KEY] == ["/tmp/a.png"]


def test_injection_skips_blacklisted_reference_path() -> None:
    """黑名单命中的引用行不物化像素，写 omitted: unsafe 终态。"""

    message = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "[image #1] path=/tmp/a.png filename=a.png mime=image/png size=12",
            }
        ],
    }
    manifest = [{"path": "/tmp/a.png", "mime_type": "image/png"}]

    messages, attached = inject_image_content_from_references(
        [message],
        image_files=manifest,
        blocked_paths=["/tmp/a.png"],
    )

    assert attached == 0
    assert not _has_image_block(messages[0])
    assert "[image #1 omitted: unsafe]" in _texts(messages[0])
    assert not any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in messages[0]["content"]
    )


def test_unsafe_paths_are_visible_without_ctx() -> None:
    """无 rail ctx 的工具执行路径从 agent 属性读取黑名单。"""

    agent = SimpleNamespace()
    mark_unsafe_image_paths(SimpleNamespace(extra={}, agent=None), ["/tmp/a.png"], agent=agent)

    assert unsafe_image_paths(agent=agent) == ["/tmp/a.png"]
