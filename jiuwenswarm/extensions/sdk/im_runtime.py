# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""History records supplied by IM channel extensions."""

from dataclasses import dataclass


@dataclass
class IMHistoryMessage:
    user_id: str
    user_name: str
    content: str
    timestamp_ms: int


__all__ = ["IMHistoryMessage"]
