# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Callback-local Reviewer metadata used by progress and terminal projection."""

from __future__ import annotations

import copy
from collections.abc import Mapping, MutableMapping
from typing import Any

REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY = "permission_reviewer_metadata_by_tool_call_id"


def record_reviewer_tool_result_metadata(
    extra: MutableMapping[str, Any] | None,
    *,
    tool_call_id: str | None,
    metadata: Mapping[str, Any],
) -> None:
    if extra is None or not tool_call_id or not metadata:
        return
    bucket = extra.setdefault(REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY, {})
    if not isinstance(bucket, MutableMapping):
        bucket = {}
        extra[REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY] = bucket
    bucket[str(tool_call_id)] = copy.deepcopy(dict(metadata))


def consume_reviewer_tool_result_metadata(
    extra: MutableMapping[str, Any] | None,
    *,
    tool_call_id: str | None,
) -> dict[str, Any] | None:
    if extra is None or not tool_call_id:
        return None
    bucket = extra.get(REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY)
    if not isinstance(bucket, MutableMapping):
        extra.pop(REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY, None)
        return None
    value = bucket.pop(str(tool_call_id), None)
    if not bucket:
        extra.pop(REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY, None)
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else None


def peek_reviewer_tool_result_metadata(
    extra: MutableMapping[str, Any] | None,
    *,
    tool_call_id: str | None,
) -> dict[str, Any] | None:
    if extra is None or not tool_call_id:
        return None
    bucket = extra.get(REVIEWER_TOOL_RESULT_METADATA_EXTRA_KEY)
    if not isinstance(bucket, Mapping):
        return None
    value = bucket.get(str(tool_call_id))
    return copy.deepcopy(dict(value)) if isinstance(value, Mapping) else None
