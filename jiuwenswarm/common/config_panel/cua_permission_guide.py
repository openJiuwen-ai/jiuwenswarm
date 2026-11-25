# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Open the Jiuwen CUA permission guide from desktop settings."""

import logging
import sys
from pathlib import Path

from jiuwenswarm.common.cua_config import is_cua_repl_available

logger = logging.getLogger(__name__)


def open_macos_cua_permission_guide() -> None:
    """Let the installed driver show the guide when a permission is missing."""
    if sys.platform != "darwin":
        return
    try:
        if not is_cua_repl_available():
            return
        from computer_use.native.macos_app import open_permission_guide
        from computer_use.paths import native_driver

        # Use the same materialized bundle as the REPL so both share permissions.
        bundle = Path(native_driver()["command"]).parents[2]
        open_permission_guide(bundle, timeout=5)
    except Exception:  # noqa: BLE001 - SDK launch failures leave the saved preference intact.
        logger.warning("Could not open the Jiuwen CUA permission guide", exc_info=True)
