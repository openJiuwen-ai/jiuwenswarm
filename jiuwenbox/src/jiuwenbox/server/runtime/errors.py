# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Shared server / runtime exceptions mapped to HTTP responses."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class BackgroundJobNotFoundError(Exception):
    """Raised when a background job id is unknown for a sandbox."""


class SandboxNotFoundError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        logger.warning("%s: %s", self.__class__.__name__, str(self))


class SandboxStateError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        # Expected client/API conflicts (e.g. Conch stop unsupported, exec while
        # stopped) map to HTTP 4xx; keep them off ERROR to avoid alarm noise.
        logger.warning("%s: %s", self.__class__.__name__, str(self))


class SandboxConflictError(Exception):
    """Raised for expected request conflicts such as duplicate sandbox IDs."""


class PolicyValidationError(Exception):
    """Raised when a policy fails validation."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        # Expected request validation failures map to HTTP 400; avoid ERROR noise.
        logger.warning("%s: %s", self.__class__.__name__, str(self))


class InvalidSandboxIdError(Exception):
    """Raised when a user-supplied sandbox_id fails format validation."""


class InvalidJobIdError(Exception):
    """Raised when a user-supplied job_id fails format validation."""
