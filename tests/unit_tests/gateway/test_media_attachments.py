# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""上传侧图片张数上限必须跟随 ``react.multimodal_image_max_count``。

写死 8 会让「前端能传 8 张、模型侧预算只给 N 张」这类漂移无人察觉：配置调小后
多传的图仍会落盘并被引用，随后才在发送侧被预算裁掉，用户看到的是静默丢图。

反向同样成立：``0 = 不限``（与 rail / 模型侧三预算同语义），只有缺失、负数或
非整数配置才回退默认值。旧用例只上传 3 张（< 默认 8），「配置 0 保留 3 张」与
「配置 8 保留 3 张」结果相同，根本无法区分 8 与不限——回归必须用超过默认上限的
12 张上传件才能钉住语义（检视 CR-7）。
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from jiuwenswarm.gateway import media_attachments

# 必须大于默认上限 8：否则「截断到 8」与「不截断」在上传数量上不可区分。
_UPLOAD_COUNT = 12
_DEFAULT_MAX_IMAGE_COUNT = 8


def _image_item(index: int) -> dict:
    image = Image.new("RGB", (8, 8), (index * 20 % 255, 60, 90))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return {
        "type": "image",
        "filename": f"img{index}.png",
        "mimeType": "image/png",
        "base64Data": base64.b64encode(buf.getvalue()).decode(),
    }


def _patch_react_config(monkeypatch, react_config: dict) -> None:
    """固定 ``react`` 段配置（``_max_image_count`` 在函数体内导入 get_config）。"""

    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_config",
        lambda: {"react": react_config},
    )


def _run(monkeypatch, tmp_path, count: int) -> list[dict]:
    monkeypatch.setattr(media_attachments, "get_agent_sessions_dir", lambda: tmp_path)
    params = {"media_items": [_image_item(i) for i in range(count)]}
    media_attachments.normalize_chat_media_attachments(params, "session-1")
    return params.get("media_items") or []


def test_upload_count_follows_small_react_config(monkeypatch, tmp_path):
    _patch_react_config(monkeypatch, {"multimodal_image_max_count": 2})

    stored = _run(monkeypatch, tmp_path, 4)

    assert len(stored) == 2


@pytest.mark.parametrize(
    ("react_config", "expected_count"),
    [
        # 0 = 不限：12 张全部保留，不能切成 raw_items[:0] 全部丢掉。
        pytest.param({"multimodal_image_max_count": 0}, _UPLOAD_COUNT, id="zero-means-unlimited"),
        # 显式 8：与默认值同数，但语义是「截断」，只保留最老的 8 张。
        pytest.param({"multimodal_image_max_count": 8}, 8, id="explicit-eight-truncates"),
        # 配置大于上传数量：不截断。
        pytest.param({"multimodal_image_max_count": 20}, _UPLOAD_COUNT, id="above-upload-count"),
        # 缺失 / YAML 空值 / 非法 / 负数 → 默认 8（不是拒收，也不是不限）。
        pytest.param({}, _DEFAULT_MAX_IMAGE_COUNT, id="config-missing"),
        pytest.param(
            {"multimodal_image_max_count": None},
            _DEFAULT_MAX_IMAGE_COUNT,
            id="yaml-null-falls-back-to-default",
        ),
        pytest.param(
            {"multimodal_image_max_count": "eight"},
            _DEFAULT_MAX_IMAGE_COUNT,
            id="invalid-string-falls-back-to-default",
        ),
        pytest.param(
            {"multimodal_image_max_count": -1},
            _DEFAULT_MAX_IMAGE_COUNT,
            id="negative-falls-back-to-default",
        ),
        pytest.param(
            {"multimodal_image_max_count": 0.5},
            _DEFAULT_MAX_IMAGE_COUNT,
            id="non-integral-float-falls-back-to-default",
        ),
    ],
)
def test_upload_count_semantics_with_twelve_uploads(
    monkeypatch, tmp_path, react_config, expected_count
):
    """12 张上传件下，0=不限 / 8=截断 / 缺失与非法/负数=默认 8 三种语义可区分。"""

    _patch_react_config(monkeypatch, react_config)

    stored = _run(monkeypatch, tmp_path, _UPLOAD_COUNT)

    assert len(stored) == expected_count
    # 截断保留最老（上传顺序）的前 N 张，与模型侧「最老优先降级」方向一致。
    assert [item["filename"] for item in stored] == [
        f"img{index}.png" for index in range(expected_count)
    ]


def test_max_image_count_zero_is_unlimited_not_default(monkeypatch):
    """``_max_image_count()`` 本身就是 0：不能把 0 归一成默认 8。"""

    _patch_react_config(monkeypatch, {"multimodal_image_max_count": 0})

    assert media_attachments._max_image_count() == 0


@pytest.mark.parametrize(
    "react_config",
    [
        pytest.param({}, id="config-missing"),
        pytest.param({"multimodal_image_max_count": None}, id="config-null"),
        pytest.param({"multimodal_image_max_count": -1}, id="config-negative"),
        pytest.param({"multimodal_image_max_count": -8}, id="config-negative-default"),
        pytest.param({"multimodal_image_max_count": "eight"}, id="config-non-numeric"),
        pytest.param({"multimodal_image_max_count": 0.5}, id="config-non-integral"),
    ],
)
def test_max_image_count_falls_back_to_default(monkeypatch, react_config):
    """缺失 / null / 负数 / 非法 / 非整数 → 默认 8（0 是唯一「不限」语义）。"""

    _patch_react_config(monkeypatch, react_config)

    assert media_attachments._max_image_count() == _DEFAULT_MAX_IMAGE_COUNT


def test_max_image_count_passes_through_positive_value(monkeypatch):
    _patch_react_config(monkeypatch, {"multimodal_image_max_count": 3})

    assert media_attachments._max_image_count() == 3