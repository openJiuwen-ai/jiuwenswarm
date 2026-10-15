# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Feishu rich-text (post) message content parsing."""

import asyncio
import json
from types import SimpleNamespace

from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import (
    FeishuChannel,
    parse_post_content,
)


def _post_message(content: dict | str) -> SimpleNamespace:
    raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return SimpleNamespace(message_type="post", content=raw)


def test_parse_post_direct_shape_with_all_node_types() -> None:
    content = {
        "title": "会议通知",
        "content": [
            [
                {"tag": "text", "text": "请查看 "},
                {"tag": "a", "text": "议程文档", "href": "https://example.com/doc"},
                {"tag": "at", "user_id": "ou_123", "user_name": "张三"},
            ],
            [{"tag": "text", "text": "附图如下"}, {"tag": "img", "image_key": "img_v2_x"}],
        ],
    }
    assert parse_post_content(json.dumps(content, ensure_ascii=False)) == (
        "会议通知\n请查看 议程文档(https://example.com/doc)@张三\n附图如下[image]"
    )


def test_parse_post_multilang_wrapper_prefers_zh_cn() -> None:
    content = {
        "data": {
            "post": {
                "en_us": {"title": "EN", "content": [[{"tag": "text", "text": "hello"}]]},
                "zh_cn": {"title": "", "content": [[{"tag": "text", "text": "你好"}]]},
            }
        }
    }
    assert parse_post_content(json.dumps(content, ensure_ascii=False)) == "你好"


def test_parse_post_multilang_wrapper_falls_back_to_first_lang() -> None:
    content = {
        "data": {
            "post": {
                "ja_jp": {"title": "タイトル", "content": [[{"tag": "text", "text": "こんにちは"}]]},
            }
        }
    }
    assert parse_post_content(json.dumps(content, ensure_ascii=False)) == "タイトル\nこんにちは"


def test_parse_post_at_without_user_name_and_at_all() -> None:
    content = {
        "content": [
            [{"tag": "at", "user_id": "ou_456"}, {"tag": "at", "user_id": "all"}],
        ]
    }
    assert parse_post_content(json.dumps(content)) == "@ou_456@all"


def test_parse_post_link_without_text_or_href() -> None:
    content = {
        "content": [
            [{"tag": "a", "href": "https://example.com"}, {"tag": "a", "text": "仅文字"}],
        ]
    }
    assert parse_post_content(json.dumps(content, ensure_ascii=False)) == (
        "https://example.com仅文字"
    )


def test_parse_post_invalid_json_returns_placeholder() -> None:
    assert parse_post_content("not a json") == "[post]"


def test_parse_post_unexpected_structure_returns_placeholder() -> None:
    assert parse_post_content(json.dumps([1, 2, 3])) == "[post]"
    assert parse_post_content(json.dumps({"title": 1})) == "[post]"
    assert parse_post_content(json.dumps({})) == "[post]"


def test_parse_post_empty_paragraphs_returns_placeholder() -> None:
    content = {"title": "", "content": [[], [{"tag": "unknown_tag"}]]}
    assert parse_post_content(json.dumps(content)) == "[post]"


def test_feishu_channel_parse_message_content_handles_post() -> None:
    channel = object.__new__(FeishuChannel)
    message = _post_message(
        {"title": "标题", "content": [[{"tag": "text", "text": "正文"}]]}
    )
    assert channel._parse_message_content(message) == "标题\n正文"


def test_feishu_channel_parse_message_content_with_file_handles_post() -> None:
    channel = object.__new__(FeishuChannel)
    channel._file_service = None
    message = _post_message({"content": [[{"tag": "text", "text": "正文"}]]})
    text, file_info = asyncio.run(channel._parse_message_content_with_file(message))
    assert text == "正文"
    assert file_info is None
