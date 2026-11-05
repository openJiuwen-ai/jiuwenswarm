# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MultimodalImageRail 在最终窗口确定后重算图片负载（压缩后护栏不再误拦）。

回归焦点：``before_model_call`` 在 ``get_context_window`` 压缩**之前**按完整历史
写入 ``images_sent`` / ``provided_paths``；若随后压缩把含图历史搬走，缓存仍宣称
"模型已收到图"，read_file 会被重复读护栏拒绝。``after_model_call`` 按
``ctx.inputs.messages``（最终窗口）重算，覆盖旧快照。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)
from jiuwenswarm.agents.harness.common.rails.multimodal_image_state import (
    mark_native_images_sent,
    mark_provided_image_paths,
    native_images_sent,
    provided_image_paths,
)


def _ctx(messages: list) -> SimpleNamespace:
    return SimpleNamespace(inputs=SimpleNamespace(messages=messages), extra={}, agent=None)


def _image_block_message() -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "[image #1] path=/tmp/a.png"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }


def test_after_model_call_resets_to_zero_when_compaction_dropped_images() -> None:
    """压缩后最终窗口无图：张数与已提供路径必须归零，否则 read_file 被误拒。"""

    rail = MultimodalImageRail(enable_image_multimodal=True)
    ctx = _ctx(messages=[])

    # 模拟压缩前 before_model_call 记录"1 张图 + 已提供路径 A"。
    mark_native_images_sent(ctx, None, 1)
    mark_provided_image_paths(ctx, ["/tmp/a.png"])

    asyncio.run(rail.after_model_call(ctx))

    assert native_images_sent(ctx=ctx) == 0
    assert provided_image_paths(ctx=ctx) == []


def test_after_model_call_keeps_count_from_final_window() -> None:
    """最终窗口仍带像素时按真实块数重算，不直写为 0。"""

    rail = MultimodalImageRail(enable_image_multimodal=True)
    ctx = _ctx(messages=[_image_block_message()])

    mark_native_images_sent(ctx, None, 0)
    mark_provided_image_paths(ctx, [])

    asyncio.run(rail.after_model_call(ctx))

    assert native_images_sent(ctx=ctx) == 1


def test_after_model_call_skips_when_disabled() -> None:
    """非原生分支不物化像素，重算不覆盖 before_model_call 已写的空快照。"""

    rail = MultimodalImageRail(enable_image_multimodal=False)
    ctx = _ctx(messages=[_image_block_message()])

    # 非原生时 before_model_call 写 0 / 空；after_model_call 应保持不动。
    mark_native_images_sent(ctx, None, 0)
    mark_provided_image_paths(ctx, [])

    asyncio.run(rail.after_model_call(ctx))

    assert native_images_sent(ctx=ctx) == 0
    assert provided_image_paths(ctx=ctx) == []
