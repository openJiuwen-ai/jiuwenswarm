# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mark connector-held messages as independent IM turns."""

from typing import Any

IM_QUEUED_NEW_TURN_KEY = "im_queued_new_turn"


def mark_queued_new_turn(metadata: dict[str, Any]) -> None:
    metadata[IM_QUEUED_NEW_TURN_KEY] = True


__all__ = ["mark_queued_new_turn"]
