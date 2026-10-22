# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Tests for Feishu post (rich-text) inbound message parsing (#5233)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import (
    FeishuChannel,
    FeishuConfig,
    _extract_feishu_post_text,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter


def _make_channel(*, with_file_service: bool = False) -> FeishuChannel:
    config = FeishuConfig(
        enabled=True,
        app_id="test_app_id",
        app_secret="test_app_secret",
    )
    router = MagicMock(spec=RobotMessageRouter)
    ch = FeishuChannel(config, router)
    if with_file_service:
        ch._file_service = MagicMock()
    else:
        ch._file_service = None
    return ch


def _post_payload_flat():
    return {
        "title": "标题",
        "content": [
            [
                {"tag": "text", "text": "请把 provider 改成 "},
                {"tag": "a", "text": "Halogen", "href": "https://example.com"},
            ],
            [
                {"tag": "at", "user_id": "ou_1", "text": "@小明"},
                {"tag": "text", "text": " 看看"},
                {"tag": "img", "image_key": "img_x"},
            ],
        ],
    }


def test_extract_flat_post():
    text = _extract_feishu_post_text(json.dumps(_post_payload_flat(), ensure_ascii=False))
    assert "标题" in text
    assert "请把 provider 改成 Halogen(https://example.com)" in text
    assert "@小明 看看[图片]" in text


def test_extract_locale_wrapped_prefers_zh_cn():
    payload = {
        "en_us": {
            "title": "EN",
            "content": [[{"tag": "text", "text": "hello"}]],
        },
        "zh_cn": {
            "title": "",
            "content": [[{"tag": "text", "text": "请用纯文本"}]],
        },
    }
    text = _extract_feishu_post_text(json.dumps(payload, ensure_ascii=False))
    assert text == "请用纯文本"
    assert "hello" not in text


def test_extract_invalid_json():
    assert _extract_feishu_post_text("not-json") == "[富文本消息解析失败]"


def test_extract_empty_content():
    assert _extract_feishu_post_text(json.dumps({"title": "", "content": []})) == (
        "[富文本消息解析失败]"
    )


def test_parse_message_content_post_not_placeholder():
    ch = _make_channel()
    msg = SimpleNamespace(
        message_type="post",
        content=json.dumps(_post_payload_flat(), ensure_ascii=False),
    )
    result = ch._parse_message_content(msg)
    assert result != "[post]"
    assert "请把 provider 改成" in result


@pytest.mark.asyncio
async def test_parse_message_content_with_file_post():
    ch = _make_channel(with_file_service=True)
    msg = SimpleNamespace(
        message_type="post",
        content=json.dumps(
            {"zh_cn": {"content": [[{"tag": "text", "text": "富文本正文"}]]}},
            ensure_ascii=False,
        ),
    )
    text, file_info = await ch._parse_message_content_with_file(msg)
    assert text == "富文本正文"
    assert file_info is None
    assert text != "[post]"
