# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Tests for inline-image download in Feishu post (rich-text) messages (#5685)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import (
    FeishuChannel,
    FeishuConfig,
    _extract_feishu_post_image_keys,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.errors import (
    AttachmentPersistError,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter


def _make_channel() -> FeishuChannel:
    config = FeishuConfig(
        enabled=True,
        app_id="test_app_id",
        app_secret="test_app_secret",
    )
    router = MagicMock(spec=RobotMessageRouter)
    ch = FeishuChannel(config, router)
    ch._file_service = MagicMock()
    return ch


def _post_with_images(*image_keys):
    paragraph = []
    for key in image_keys:
        paragraph.append({"tag": "img", "image_key": key, "width": 100, "height": 100})
    return {
        "title": "",
        "content": [
            [{"tag": "text", "text": "看图"}],
            paragraph,
        ],
    }


def _message(payload):
    return SimpleNamespace(
        message_type="post",
        content=json.dumps(payload, ensure_ascii=False),
        message_id="om_test_1",
    )


# ---------- extractor: positive coverage ----------

def test_extractor_single_image():
    keys = _extract_feishu_post_image_keys(_post_with_images("img_a"))
    assert keys == ["img_a"]


def test_extractor_multiple_images_document_order():
    keys = _extract_feishu_post_image_keys(
        _post_with_images("img_b", "img_a", "img_c")
    )
    assert keys == ["img_b", "img_a", "img_c"]


def test_extractor_dedupes_repeated_keys():
    keys = _extract_feishu_post_image_keys(_post_with_images("img_a", "img_a"))
    assert keys == ["img_a"]


def test_extractor_locale_wrapped_payload():
    payload = {"zh_cn": _post_with_images("img_zh")}
    assert _extract_feishu_post_image_keys(payload) == ["img_zh"]


def test_extractor_accepts_json_string():
    assert _extract_feishu_post_image_keys(
        json.dumps(_post_with_images("img_s"), ensure_ascii=False)
    ) == ["img_s"]


# ---------- extractor: negative / edge coverage ----------

def test_extractor_no_image_returns_empty():
    payload = {"title": "t", "content": [[{"tag": "text", "text": "hi"}]]}
    assert _extract_feishu_post_image_keys(payload) == []


def test_extractor_img_without_key_ignored():
    payload = {"title": "", "content": [[{"tag": "img"}]]}
    assert _extract_feishu_post_image_keys(payload) == []


def test_extractor_malformed_json_returns_empty():
    assert _extract_feishu_post_image_keys("not-json") == []


def test_extractor_non_dict_returns_empty():
    assert _extract_feishu_post_image_keys("[1, 2, 3]") == []


# ---------- post branch: download pipeline ----------

@pytest.mark.asyncio
async def test_post_branch_downloads_each_image_once():
    ch = _make_channel()
    ch._file_service.download_image = AsyncMock(
        side_effect=lambda file_key, message_id: {"path": f"/tmp/{file_key}.jpg", "name": file_key}
    )
    text, file_info = await ch._parse_message_content_with_file(
        _message(_post_with_images("img_1", "img_2"))
    )
    assert ch._file_service.download_image.await_count == 2
    assert isinstance(file_info, list) and len(file_info) == 2
    assert "看图" in text


@pytest.mark.asyncio
async def test_post_branch_without_image_skips_download():
    ch = _make_channel()
    ch._file_service.download_image = AsyncMock()
    payload = {"title": "", "content": [[{"tag": "text", "text": "纯文字"}]]}
    text, file_info = await ch._parse_message_content_with_file(_message(payload))
    ch._file_service.download_image.assert_not_awaited()
    assert file_info is None
    assert text == "纯文字"


@pytest.mark.asyncio
async def test_post_branch_partial_failure_keeps_successes():
    ch = _make_channel()

    async def flaky(file_key, message_id):
        if file_key == "img_bad":
            raise RuntimeError("network")
        return {"path": "/tmp/ok.jpg"}

    ch._file_service.download_image = AsyncMock(side_effect=flaky)
    text, file_info = await ch._parse_message_content_with_file(
        _message(_post_with_images("img_ok", "img_bad"))
    )
    assert isinstance(file_info, list) and len(file_info) == 1
    assert "图片下载失败" not in text  # partial success: successes delivered


@pytest.mark.asyncio
async def test_post_branch_all_failed_marks_failure():
    ch = _make_channel()
    ch._file_service.download_image = AsyncMock(side_effect=RuntimeError("down"))
    text, file_info = await ch._parse_message_content_with_file(
        _message(_post_with_images("img_x"))
    )
    assert file_info is None
    assert "[图片下载失败]" in text


@pytest.mark.asyncio
async def test_post_branch_persist_error_propagates():
    ch = _make_channel()
    ch._file_service.download_image = AsyncMock(
        side_effect=AttachmentPersistError("agent server unreachable")
    )
    with pytest.raises(AttachmentPersistError):
        await ch._parse_message_content_with_file(_message(_post_with_images("img_e")))


@pytest.mark.asyncio
async def test_post_branch_without_file_service_degrades_to_text():
    ch = _make_channel()
    ch._file_service = None
    text, file_info = await ch._parse_message_content_with_file(
        _message(_post_with_images("img_a"))
    )
    assert file_info is None
    assert "看图" in text
