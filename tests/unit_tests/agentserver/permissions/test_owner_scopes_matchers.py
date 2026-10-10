# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""owner_scopes matcher tests that avoid rails/__init__ team imports."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_owner_scopes_match_args():
    import sys

    mod_path = (
        Path(__file__).resolve().parents[4]
        / "jiuwenswarm"
        / "agents"
        / "harness"
        / "common"
        / "rails"
        / "permissions"
        / "owner_scopes.py"
    )
    spec = importlib.util.spec_from_file_location("owner_scopes_under_test", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod._match_args


def test_match_args_uses_permission_engine_command_matcher() -> None:
    match_args = _load_owner_scopes_match_args()
    assert match_args("ls *", {"command": "ls -la"}) is True
    assert match_args("ls *", {"command": "rm -rf /"}) is False


def test_match_args_uses_permission_engine_path_and_url_matchers() -> None:
    match_args = _load_owner_scopes_match_args()
    assert match_args("/tmp/*", {"path": "/tmp/a.txt"}) is True
    assert match_args("https://example.com/*", {"url": "https://example.com/x"}) is True
    assert match_args("https://example.com/*", {"url": "https://other.example/x"}) is False
