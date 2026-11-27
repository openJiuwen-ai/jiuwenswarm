# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations
from typing import Any

from jiuwenclaw.agentserver.skilldev.utils.path_utils import (
    resolve_and_validate_path,
    resolve_workspace_dir,
    validate_path_in_workspace,
)


def create_upload_file_obs(session_id="default") -> Any:
    from jiuwenclaw.sandbox import sandbox_routing_enabled
    if sandbox_routing_enabled():
        from jiuwenclaw.agentserver.skilldev.utils.upload_file_obs_sandbox import UploadFileByOSMS
        return UploadFileByOSMS(session_id=session_id)
    from jiuwenclaw.agentserver.skilldev.utils.upload_file_obs import UploadFileOSMS
    return UploadFileOSMS()


__all__ = [
    "create_upload_file_obs",
    "resolve_workspace_dir",
    "validate_path_in_workspace",
    "resolve_and_validate_path",
]
