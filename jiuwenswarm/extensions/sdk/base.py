# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Keep manifest-less packages readable after the SDK moved to gateway-protocol."""

from __future__ import annotations

from gateway_protocol.sdk.base import MANIFEST_FILENAME
from gateway_protocol.sdk.base import BaseExtension as _BaseExtension
from gateway_protocol.types import ExtensionMetadata

from jiuwenswarm.common.utils import logger


class BaseExtension(_BaseExtension):
    def _load_metadata_from_yaml(self) -> ExtensionMetadata:
        root = self._get_extension_dir()
        if root is not None and (root / MANIFEST_FILENAME).exists():
            return super()._load_metadata_from_yaml()

        name = root.name if root is not None else type(self).__name__
        logger.warning("[BaseExtension] extension %s has no %s", name, MANIFEST_FILENAME)
        return ExtensionMetadata(
            id="",
            name=name,
            version="",
            description="",
            author="",
            min_jiuwenswarm_version="",
            dependencies={},
            config_schema=None,
        )

__all__ = ["MANIFEST_FILENAME", "BaseExtension"]
