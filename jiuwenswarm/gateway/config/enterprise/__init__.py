# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""企业专属表（instance_agent_resource / *_template / 密钥等）→ PersistentStore。

仅 DB；企业版启动时由 storage_assembly 注入 ``EnterpriseRecordRepository``。
"""

from jiuwenswarm.gateway.config.enterprise.access import (
    clear_enterprise_record_repositories,
    get_enterprise_record_repository,
    list_injected_store_names,
    set_enterprise_record_repository,
    set_enterprise_record_repositories,
)
from jiuwenswarm.gateway.config.enterprise.catalog import (
    ENTERPRISE_RECORD_SPECS,
    ENTERPRISE_RECORD_STORE_NAMES,
    EnterpriseRecordSpec,
    get_enterprise_record_spec,
)
from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository

__all__ = [
    "ENTERPRISE_RECORD_SPECS",
    "ENTERPRISE_RECORD_STORE_NAMES",
    "EnterpriseRecordRepository",
    "EnterpriseRecordSpec",
    "clear_enterprise_record_repositories",
    "get_enterprise_record_repository",
    "get_enterprise_record_spec",
    "list_injected_store_names",
    "set_enterprise_record_repository",
    "set_enterprise_record_repositories",
]
