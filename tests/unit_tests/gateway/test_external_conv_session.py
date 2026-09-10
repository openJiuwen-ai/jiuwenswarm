"""Tests for cloud conv_id → local session reverse lookup."""

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.gateway.message_handler.external_conv_session import (
    find_local_session_for_external_conv_id,
    to_local_conv_id,
)


def test_to_local_conv_id_matches_desktop_report_shape() -> None:
    sid = "desktop_1a061727c1f_65690918081e"
    assert to_local_conv_id(sid) == "conv_desktop1a061727c1f656909"
    assert to_local_conv_id("conv_already") == "conv_already"
    assert to_local_conv_id("") == ""


def test_find_prefers_desktop_over_xiaoyi_for_same_conv(tmp_path: Path) -> None:
    desktop = "desktop_1a061727c1f_65690918081e"
    conv = to_local_conv_id(desktop)
    assert conv == "conv_desktop1a061727c1f656909"

    desk_dir = tmp_path / desktop
    desk_dir.mkdir()
    (desk_dir / "metadata.json").write_text(
        json.dumps(
            {
                "session_id": desktop,
                "title": "今天天气真好",
                "last_message_at": 100.0,
                "channel_id": "desktop",
            }
        ),
        encoding="utf-8",
    )

    xiaoyi = "xiaoyi_1a061734c32_b7a0cc1f59ad"
    x_dir = tmp_path / xiaoyi
    x_dir.mkdir()
    (x_dir / "metadata.json").write_text(
        json.dumps(
            {
                "session_id": xiaoyi,
                "title": "你真的在办公室吗",
                "last_message_at": 200.0,
                "channel_id": "xiaoyi",
                "channel_metadata": {
                    "xiaoyi_conversation_id": conv,
                    "xiaoyi_session_id": conv,
                },
            }
        ),
        encoding="utf-8",
    )

    found = find_local_session_for_external_conv_id(conv, sessions_dir=tmp_path)
    assert found == desktop


def test_find_by_channel_metadata_when_prefix_mismatch(tmp_path: Path) -> None:
    sid = "xiaoyi_abc123"
    conv = "conv_desktop1a0615d0634e1a588"
    s_dir = tmp_path / sid
    s_dir.mkdir()
    (s_dir / "metadata.json").write_text(
        json.dumps(
            {
                "session_id": sid,
                "last_message_at": 10.0,
                "channel_metadata": {"xiaoyi_conversation_id": conv},
            }
        ),
        encoding="utf-8",
    )
    assert find_local_session_for_external_conv_id(conv, sessions_dir=tmp_path) == sid


def test_find_returns_none_for_non_conv_or_missing(tmp_path: Path) -> None:
    assert find_local_session_for_external_conv_id("1788330011489", sessions_dir=tmp_path) is None
    assert find_local_session_for_external_conv_id("conv_nosuch", sessions_dir=tmp_path) is None


def test_find_conv_path_hits_desktop_by_to_local_conv_id(tmp_path: Path) -> None:
    """conv_*（desktop-mirror）路径按 to_local_conv_id 命中。"""
    desktop = "desktop_1a061727c1f_65690918081e"
    conv = to_local_conv_id(desktop)
    desk_dir = tmp_path / desktop
    desk_dir.mkdir()
    (desk_dir / "metadata.json").write_text(
        json.dumps(
            {"session_id": desktop, "channel_id": "desktop", "last_message_at": 1.0}
        ),
        encoding="utf-8",
    )
    assert find_local_session_for_external_conv_id(conv, sessions_dir=tmp_path) == desktop
