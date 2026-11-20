# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared primitives for the topic recommendation modules.

These checks were copied privately into nearly every topic module. One
definition keeps the JSON value predicates and the scope vocabulary identical
across contracts, collectors, pools and serving.
"""

from __future__ import annotations

from typing import Any

GLOBAL_NEW_CHAT_SCOPE = "global_new_chat"

# Project IDs under which chat keeps the sessions of the global new-chat page.
GLOBAL_PROJECT_BUCKETS = frozenset({"", "default", "default_code"})


def plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def valid_scope(value: Any) -> bool:
    """A scope is the global page or one exact ``project:<id>``."""

    if value == GLOBAL_NEW_CHAT_SCOPE:
        return True
    if not isinstance(value, str) or not value.startswith("project:"):
        return False
    project_id = value.removeprefix("project:")
    return bool(project_id) and project_id == project_id.strip()


def scope_matches_project(project_id: Any, context_scope: str) -> bool:
    """Whether one session's project belongs to the requested scope."""

    actual = str(project_id or "").strip()
    if context_scope == GLOBAL_NEW_CHAT_SCOPE:
        return actual in GLOBAL_PROJECT_BUCKETS
    return actual == context_scope.removeprefix("project:")
