# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Resolve a process policy user or group to a numeric id."""

from __future__ import annotations

import grp
import pwd

_POSIX_ID_MAX = 2**32 - 1


def resolve_posix_id(value: str, *, kind: str) -> int:
    """Return a uid or gid.

    A string of digits is the id itself, the same rule Conch uses.
    Any other string is looked up as a user or group name.
    """
    text = value.strip()
    if text.isdigit():
        number = int(text)
        if number > _POSIX_ID_MAX:
            raise ValueError(f"{kind} id {number} is out of range")
        return number
    try:
        if kind == "user":
            return int(pwd.getpwnam(text).pw_uid)
        return int(grp.getgrnam(text).gr_gid)
    except KeyError as exc:
        raise ValueError(f"{kind} {value!r} could not be resolved") from exc
