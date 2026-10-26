# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Todo接入输出策略；只由宿主绑定，不接受模型或请求参数覆盖。"""

from contextvars import ContextVar

TODO_SNAPSHOT_MODE = ContextVar("todo_snapshot_mode", default="legacy")


def versioned_todo_output() -> bool:
    """当前连接/请求是否已明确启用版本消费。"""
    return TODO_SNAPSHOT_MODE.get() == "versioned"


def resolve_todo_snapshot_mode(config: dict, app_id: str | None) -> str:
    """按身份提供者确认的应用ID读取服务端配置，未配置保持原协议。"""
    if not app_id:
        return "legacy"
    modes = config.get("todo", {}).get("snapshot_modes", {})
    mode = modes.get(app_id, "legacy")
    if mode not in ("legacy", "versioned"):
        raise ValueError("Todo snapshot mode must be legacy or versioned")
    return mode
