# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exec env overrides share None-deletes semantics on every child path."""

from __future__ import annotations

from jiuwenbox.supervisor import sandbox_daemon


def test_none_override_deletes_and_strings_set() -> None:
    environ = {"KEEP": "1", "DROP": "x"}
    sandbox_daemon.apply_env_overrides(environ, {"DROP": None, "ADD": "2", "MISSING": None})
    assert environ == {"KEEP": "1", "ADD": "2"}


def test_build_child_env_drops_none_values() -> None:
    child_env = sandbox_daemon.build_child_env({"HOME": None, "GIT_AUTHOR_NAME": "member"})
    assert "HOME" not in child_env
    assert child_env["GIT_AUTHOR_NAME"] == "member"
