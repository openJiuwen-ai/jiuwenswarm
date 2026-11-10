# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Safe YAML parsing with the optional C implementation."""

from typing import Any, TextIO

import yaml


def safe_yaml_load(stream: str | TextIO) -> Any:
    """Read one safe document and release the parser on success or failure."""
    loader_type = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    loader = loader_type(stream)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()
