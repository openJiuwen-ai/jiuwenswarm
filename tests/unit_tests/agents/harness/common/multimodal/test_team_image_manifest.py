# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""team 上传清单经 ``request_metadata`` 传递（S_22 team 通道）。"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.multimodal.image_reference import (
    store_team_request_image_files,
    team_request_image_files,
)

_IMAGES = [
    {
        "type": "image",
        "path": "/tmp/0.jpg",
        "filename": "0.jpg",
        "mime_type": "image/jpeg",
    },
    {
        "type": "image",
        "path": "/tmp/1.jpg",
        "filename": "1.jpg",
        "mime_type": "image/jpeg",
    },
]


def test_store_then_read_round_trip() -> None:
    metadata: dict = {}

    store_team_request_image_files(metadata, _IMAGES)

    assert team_request_image_files(metadata) == _IMAGES


def test_read_returns_copy_not_shared_list() -> None:
    """读侧返回副本：成员 rail 改动不得回写 build context 的 metadata。"""

    metadata: dict = {}
    store_team_request_image_files(metadata, _IMAGES)

    first = team_request_image_files(metadata)
    first.append(
        {"type": "image", "path": "/tmp/injected.jpg", "mime_type": "image/jpeg"}
    )

    assert len(team_request_image_files(metadata)) == 2


def test_empty_list_is_authoritative_clear() -> None:
    """空列表是"本轮没有上传"的权威事实，不是"没写"——必须覆盖旧值。"""

    metadata = {"multimodal_image_files": _IMAGES}

    store_team_request_image_files(metadata, [])

    assert team_request_image_files(metadata) == []


def test_read_missing_or_invalid_returns_none() -> None:
    assert team_request_image_files({}) is None
    assert team_request_image_files(None) is None
    assert team_request_image_files({"multimodal_image_files": "not-a-list"}) is None


@pytest.mark.parametrize("metadata", [None, "text", 0])
def test_store_ignores_non_mapping(metadata) -> None:
    """非 mapping 入参静默忽略：装配链不得因为 metadata 形态异常而失败。"""

    store_team_request_image_files(metadata, _IMAGES)
