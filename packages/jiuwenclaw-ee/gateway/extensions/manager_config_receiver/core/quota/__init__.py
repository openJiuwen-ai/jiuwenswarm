# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved
"""配额业务包（workspace-quota 等）。"""

from .workspace_quota_policy import WorkspaceQuotaPolicyService
from .workspace_quota_usage import WorkspaceQuotaUsageService

__all__ = (
    "WorkspaceQuotaPolicyService",
    "WorkspaceQuotaUsageService",
)
