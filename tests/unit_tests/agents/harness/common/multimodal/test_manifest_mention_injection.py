# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""成员侧确定性分发：上下文任意位置提到清单图 → 补引用行（不依赖 leader 消息格式）。"""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.multimodal import image_history
from jiuwenswarm.agents.harness.common.multimodal.image_history import (
    ensure_manifest_images_referenced,
    inject_image_content_from_references,
)

_MANIFEST = [
    {
        "type": "image",
        "path": "C:/uploads/3.jpg",
        "filename": "3.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": 222,
    },
    {
        "type": "image",
        "path": "C:/uploads/2.jpg",
        "filename": "2.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": 111,
    },
]


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _tool(text: str) -> dict:
    return {"role": "tool", "content": text}


def _plain(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text") or ""
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def test_mention_in_tool_result_adds_reference_to_user_message() -> None:
    """任务看板（tool result）里的图片路径 → 补到最近 user 消息，交给 inject 物化。"""

    messages = [
        _user("团队上下文/任务通知"),
        _tool("任务内容：图片文件（绝对路径）：C:/uploads/3.jpg"),
    ]

    count = ensure_manifest_images_referenced(messages, _MANIFEST)

    assert count == 1
    text = _plain(messages[0])
    assert "[image #1] path=C:/uploads/3.jpg filename=3.jpg mime=image/jpeg size=222" in text
    assert "2.jpg" not in text


def test_mention_by_filename_in_message() -> None:
    messages = [_user("请识别 3.jpg")]

    assert ensure_manifest_images_referenced(messages, _MANIFEST) == 1
    assert "[image #1] path=C:/uploads/3.jpg" in _plain(messages[0])


def test_no_mention_is_noop() -> None:
    messages = [_user("请识别第二张图片"), _tool("与图片无关的任务内容")]

    assert ensure_manifest_images_referenced(messages, _MANIFEST) == 0
    assert _plain(messages[0]) == "请识别第二张图片"


def test_already_attached_is_not_repeated() -> None:
    """已有 [image #n attached] 的图不再补引用行（跨模型调用幂等）。"""

    messages = [
        _user(
            "任务\n"
            "[image #1] path=C:/uploads/3.jpg filename=3.jpg mime=image/jpeg size=222\n"
            "[image #1 attached]"
        ),
        _tool("图片文件（绝对路径）：C:/uploads/3.jpg"),
    ]

    assert ensure_manifest_images_referenced(messages, _MANIFEST) == 0


def test_respects_max_images_and_allocates_next_index() -> None:
    messages = [
        _user(
            "已有引用\n"
            "[image #5] path=C:/other.png filename=other.png mime=image/png\n"
            "以及 3.jpg 和 2.jpg"
        )
    ]

    count = ensure_manifest_images_referenced(messages, _MANIFEST, max_images=1)

    assert count == 1
    text = _plain(messages[0])
    assert "[image #6] path=C:/uploads/3.jpg" in text
    assert "2.jpg filename=2.jpg" not in text


def test_without_manifest_is_noop() -> None:
    messages = [_user("请识别 3.jpg")]
    assert ensure_manifest_images_referenced(messages, None) == 0
    assert ensure_manifest_images_referenced(messages, []) == 0


# ---------------------------------------------------------------------------
# CR-9634-1：omitted 是终态，不得每轮重新补引用行 / 重新读盘物化
# ---------------------------------------------------------------------------


def test_omitted_image_is_not_rematerialized() -> None:
    """已 omitted 的清单图视为已处理：跨模型调用幂等，不再补行、不再物化。"""

    messages = [
        _user(
            "任务\n"
            "[image #1] path=C:/uploads/3.jpg filename=3.jpg mime=image/jpeg size=222\n"
            "[image #1 omitted: request budget]"
        ),
        _tool("图片文件（绝对路径）：C:/uploads/3.jpg"),
    ]
    before = _plain(messages[0])

    count = ensure_manifest_images_referenced(messages, _MANIFEST)

    assert count == 0
    # 原故障：下一次 before_model_call 又补一条 [image #2] 引用行，再读盘物化一次。
    assert _plain(messages[0]) == before
    assert "[image #2]" not in _plain(messages[0])

    _, attached = inject_image_content_from_references(
        list(messages), image_files=_MANIFEST
    )
    assert attached == 0
    assert _plain(messages[0]) == before


def test_duplicate_omitted_image_is_not_rematerialized() -> None:
    """``omitted: duplicate`` 同样是终态：同路径已由别的消息提供像素，不再补行。"""

    messages = [
        _user(
            "任务\n"
            "[image #2] path=C:/uploads/3.jpg filename=3.jpg mime=image/jpeg size=222\n"
            "[image #2 omitted: duplicate]"
        ),
        _tool("图片文件（绝对路径）：C:/uploads/3.jpg"),
    ]
    before = _plain(messages[0])

    assert ensure_manifest_images_referenced(messages, _MANIFEST) == 0
    assert _plain(messages[0]) == before


# ---------------------------------------------------------------------------
# CR-9634-2：提及判定边界（裸子串会误命中更长的文件名 / 不同路径）
# ---------------------------------------------------------------------------

_SHORT_NAME_MANIFEST = [
    {
        "type": "image",
        "path": "C:/uploads/1.png",
        "filename": "1.png",
        "mime_type": "image/png",
    },
]


def test_short_filename_is_not_substring_mention() -> None:
    """清单 1.png 不得被 tool result 里的 11.png / data.png 误命中（裸子串过宽）。"""

    messages = [
        _user("团队上下文/任务通知"),
        _tool("任务看板：图片文件（路径）：C:/uploads/11.png；另见 data.png"),
    ]

    assert ensure_manifest_images_referenced(messages, _SHORT_NAME_MANIFEST) == 0
    assert _plain(messages[0]) == "团队上下文/任务通知"


def test_longer_manifest_name_is_not_matched_by_shorter_text() -> None:
    """反方向同样要求整名对齐：清单 11.png 不被文本里的 1.png 命中。"""

    messages = [_user("团队上下文/任务通知"), _tool("图片文件：C:/uploads/1.png")]

    assert ensure_manifest_images_referenced(messages, _MANIFEST_11) == 0
    assert _plain(messages[0]) == "团队上下文/任务通知"


_MANIFEST_11 = [
    {
        "type": "image",
        "path": "C:/uploads/11.png",
        "filename": "11.png",
        "mime_type": "image/png",
    },
]

# 上传方可能给图改名（filename 与路径 basename 不同）：此时路径命中只能靠路径归一，
# 不能依赖文件名兜底。
_RENAMED_MANIFEST = [
    {
        "type": "image",
        "path": "C:/uploads/3.jpg",
        "filename": "截图 3.jpg",
        "mime_type": "image/jpeg",
        "size_bytes": 222,
    },
]


def test_path_with_different_separator_and_case_matches() -> None:
    """Windows 反斜杠 / 大小写形态的路径文本仍命中清单路径（归一后比较）。"""

    messages = [_user("上下文"), _tool("图片文件（绝对路径）：C:\\Uploads\\3.JPG")]

    assert ensure_manifest_images_referenced(messages, _MANIFEST) == 1
    assert "[image #1] path=C:/uploads/3.jpg" in _plain(messages[0])


def test_path_mention_matches_across_separators_without_filename_fallback() -> None:
    """filename 与路径 basename 不同时，路径归一形态必须独立命中（不能只靠文件名兜底）。"""

    messages = [_user("上下文"), _tool("图片文件（绝对路径）：C:\\Uploads\\3.JPG")]

    assert ensure_manifest_images_referenced(messages, _RENAMED_MANIFEST) == 1
    assert "[image #1] path=C:/uploads/3.jpg" in _plain(messages[0])


def test_normalizes_each_message_text_once_for_all_images(monkeypatch) -> None:
    """N 张图 × 全量消息只做「每条消息一次」归一，不逐图重复 lower 长文本。"""

    origin = image_history.normalize_image_mention_text
    normalized_calls: list[str] = []

    def _counting_normalize(text: str) -> str:
        normalized_calls.append(text)
        return origin(text)

    monkeypatch.setattr(
        image_history, "normalize_image_mention_text", _counting_normalize
    )

    manifest = [
        {
            "type": "image",
            "path": "C:/uploads/1.png",
            "filename": "1.png",
            "mime_type": "image/png",
        },
        {
            "type": "image",
            "path": "C:/uploads/2.png",
            "filename": "2.png",
            "mime_type": "image/png",
        },
        {
            "type": "image",
            "path": "C:/uploads/3.png",
            "filename": "3.png",
            "mime_type": "image/png",
        },
    ]
    long_tool_text = "任务看板：" + ("无关内容 " * 5000) + " C:/uploads/2.png"
    messages = [_user("上下文"), _tool(long_tool_text)]

    assert ensure_manifest_images_referenced(messages, manifest) == 1
    # 逐 (图, 文本) 组合归一（旧实现 / 误用便捷封装）会让这段长文本被归一 3 次。
    assert normalized_calls.count(long_tool_text) == 1
