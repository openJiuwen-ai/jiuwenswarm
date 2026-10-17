# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Configuration services for extensions."""

from typing import Any


def get_config() -> dict[str, Any]:
    from jiuwenswarm.common.config import get_config as read_config

    return read_config()


def get_default_models(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    from jiuwenswarm.common.config import get_default_models as read_models

    return read_models() if config is None else read_models(config)


def build_reasoning_model_request_kwargs(
    *, model_client_config: dict[str, Any], model_config_obj: Any, model_name: str
) -> dict[str, Any]:
    from jiuwenswarm.common.reasoning_injector import (
        build_reasoning_model_request_kwargs as build_kwargs,
    )

    return build_kwargs(
        model_client_config=model_client_config,
        model_config_obj=model_config_obj,
        model_name=model_name,
    )
