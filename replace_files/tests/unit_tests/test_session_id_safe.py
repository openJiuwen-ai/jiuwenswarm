# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenclaw.agentserver.session_id_safe import (
    normalize_safe_session_id,
    resolve_session_dir_under_root,
)


class TestNormalizeSafeSessionId:
    @staticmethod
    def test_accepts_sess_style_id() -> None:
        assert normalize_safe_session_id("  sess_ab12_cd34  ") == "sess_ab12_cd34"

    @staticmethod
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "../x",
            "a/../b",
            "x/y",
            r"a\b",
            "..",
            ".",
        ],
    )
    def test_rejects_unsafe(raw: str) -> None:
        assert normalize_safe_session_id(raw) is None


class TestResolveSessionDirUnderRoot:
    @staticmethod
    def test_resolves_under_root(tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        root.mkdir()
        got = resolve_session_dir_under_root(root, "sess_ok_1")
        assert got is not None
        assert got.parent == root.resolve()

    @staticmethod
    def test_rejects_escape(tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        root.mkdir()
        assert resolve_session_dir_under_root(root, "../outside") is None
