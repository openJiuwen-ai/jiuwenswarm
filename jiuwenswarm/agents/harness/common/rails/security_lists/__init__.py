# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一安全名单（security lists）。

聚合记录数据模型（:mod:`.models`）+ config.yaml 存储层（:mod:`.store`）。
投影归一、匹配合成与 Rail 见 normalize/composer/matcher/evaluate/rail（M2/M3）。
"""
from __future__ import annotations

from .composer import SecurityListComposer
from .evaluate import Verdict, evaluate
from .matcher import extract_targets, match_record
from .rail import (
    SECURITY_LIST_DENY_KEY,
    SECURITY_LISTS_RESUME_USER_INPUT_KEY,
    UnifiedSecurityListRail,
)
from .models import (
    ACTIONS,
    ALLOWED_MATCH,
    FILE_PATH_OPS,
    GENERIC_OPS,
    MODE_KEYS,
    DuplicateRecordError,
    SecurityListRecord,
    SecurityListsCorruptedError,
    new_record_id,
    record_from_dict,
    record_to_dict,
    resolve_cell,
    utc_now_iso,
    validate_record,
)
from .normalize import (
    project_approvals,
    project_builtin,
    project_sandbox_runtime_copy,
)
from .api import (
    check_command,
    check_domain,
    check_domain_static,
    check_path,
    check_path_static,
    export_domain_rules,
    export_path_rules,
)
from .store import (
    cloud_sync,
    delete_record,
    get_defaults,
    get_security_lists,
    migrate_sandbox_copy_once,
    patch_cells,
    set_defaults,
    upsert_record,
)

__all__ = [
    "ACTIONS",
    "ALLOWED_MATCH",
    "FILE_PATH_OPS",
    "GENERIC_OPS",
    "MODE_KEYS",
    "DuplicateRecordError",
    "SECURITY_LISTS_RESUME_USER_INPUT_KEY",
    "SECURITY_LIST_DENY_KEY",
    "SecurityListComposer",
    "SecurityListRecord",
    "SecurityListsCorruptedError",
    "UnifiedSecurityListRail",
    "Verdict",
    "check_command",
    "check_domain",
    "check_domain_static",
    "check_path",
    "check_path_static",
    "cloud_sync",
    "delete_record",
    "evaluate",
    "export_domain_rules",
    "export_path_rules",
    "extract_targets",
    "get_defaults",
    "get_security_lists",
    "match_record",
    "migrate_sandbox_copy_once",
    "new_record_id",
    "patch_cells",
    "project_approvals",
    "project_builtin",
    "project_sandbox_runtime_copy",
    "record_from_dict",
    "record_to_dict",
    "resolve_cell",
    "set_defaults",
    "upsert_record",
    "utc_now_iso",
    "validate_record",
]
