# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request performance summary collection and persistence."""

from jiuwenswarm.perf.config import get_perf_summary_config, init_perf_summary_config

__all__ = [
    "get_perf_summary_config",
    "init_perf_summary_config",
]


def get_perf_collector():
    from jiuwenswarm.perf.collector import get_perf_collector as _get

    return _get()


def mark_first_byte_latency() -> None:
    from jiuwenswarm.perf.context import mark_first_byte_latency as _mark

    _mark()
