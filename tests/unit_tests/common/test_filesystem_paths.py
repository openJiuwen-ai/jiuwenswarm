# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Extended paths preserve drive, UNC and non-Windows filesystem semantics."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import filesystem_paths


@pytest.mark.parametrize(("source", "expected"), [
    (r"C:\data\team\..\member", r"\\?\C:\data\member"),
    ("C:/data/team", r"\\?\C:\data\team"),
    ("C:\\", "\\\\?\\C:\\"),
    (r"D:\用户目录\expert team", r"\\?\D:\用户目录\expert team"),
    (r"\\server\share\team", r"\\?\UNC\server\share\team"),
    (r"\\server\share", r"\\?\UNC\server\share"),
    (r"\\server\share\专家目录", r"\\?\UNC\server\share\专家目录"),
    (r"\\?\C:\data\team", r"\\?\C:\data\team"),
    (r"\\?\UNC\server\share\team", r"\\?\UNC\server\share\team"),
    (r"\\.\pipe\channel", r"\\.\pipe\channel"),
])
def test_extended_windows_path(monkeypatch, source, expected):
    monkeypatch.setattr(filesystem_paths, "sys", SimpleNamespace(platform="win32"))
    assert filesystem_paths.extended_path(source) == expected


def test_extended_path_preserves_non_windows_relative_paths(monkeypatch):
    monkeypatch.setattr(filesystem_paths, "sys", SimpleNamespace(platform="darwin"))
    assert filesystem_paths.extended_path(Path("relative/team")) == "relative/team"
