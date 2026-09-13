# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""企业专属 PersistentStore name → 业务主键。

仅 DB 布局（见 ``storage_assembly.layouts``）；personal 无 file，调用应 fail-fast。
每网关独立数据库，无实例隔离列。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnterpriseRecordSpec:
    """一行企业表的定位方式。

    - ``key_fields``：业务主键；空元组表示库内至多一行。
    - ``scope_field``：历史兼容字段；默认 ``None``（每网关独立 DB，不再做行级实例隔离）。
    """

    key_fields: tuple[str, ...] = ()
    scope_field: str | None = None


# store name == DB 表名；与 layouts / EE TableDefinition 对齐。
# ``cron_job`` 另有 personal JSON 布局（见 ``layouts._legacy_gateway_cron_job_layout``）。
ENTERPRISE_RECORD_SPECS: dict[str, EnterpriseRecordSpec] = {
    "model_template": EnterpriseRecordSpec(key_fields=("template_id",)),
    "embedding_template": EnterpriseRecordSpec(key_fields=("template_id",)),
    "extension_config_template": EnterpriseRecordSpec(key_fields=("template_id",)),
    "skill_prebuilt_template": EnterpriseRecordSpec(key_fields=("template_id",)),
    "mcp_template": EnterpriseRecordSpec(key_fields=("template_id",)),
    "permissions_template": EnterpriseRecordSpec(key_fields=("template_id",)),
    "agent_template": EnterpriseRecordSpec(key_fields=("template_id",)),
    "a2a_outbound_template": EnterpriseRecordSpec(key_fields=("template_id",)),
    "a2a_access_policy_template": EnterpriseRecordSpec(key_fields=("policy_id",)),
    "a2a_outbound_user_state": EnterpriseRecordSpec(key_fields=("template_id",)),
    "a2a_outbound_runtime_state": EnterpriseRecordSpec(key_fields=("template_id",)),
    "a2a_outbound_dispatch": EnterpriseRecordSpec(key_fields=("dispatch_id",)),
    "instance_agent_resource": EnterpriseRecordSpec(key_fields=("resource_id",)),
    "log_masking_rule": EnterpriseRecordSpec(key_fields=("rule_id",)),
    "cron_job": EnterpriseRecordSpec(key_fields=("job_id",)),
    "task_memory_config": EnterpriseRecordSpec(key_fields=()),
    "manager_sign_pubkey": EnterpriseRecordSpec(
        key_fields=("id",),
        scope_field=None,
    ),
    "gateway_enc_keypair": EnterpriseRecordSpec(
        key_fields=("id",),
        scope_field=None,
    ),
    "gateway_sign_keypair": EnterpriseRecordSpec(
        key_fields=("id",),
        scope_field=None,
    ),
}

ENTERPRISE_RECORD_STORE_NAMES: tuple[str, ...] = tuple(ENTERPRISE_RECORD_SPECS.keys())


def get_enterprise_record_spec(store_name: str) -> EnterpriseRecordSpec:
    try:
        return ENTERPRISE_RECORD_SPECS[store_name]
    except KeyError as exc:
        raise KeyError(f"unknown enterprise record store: {store_name!r}") from exc


__all__ = [
    "ENTERPRISE_RECORD_SPECS",
    "ENTERPRISE_RECORD_STORE_NAMES",
    "EnterpriseRecordSpec",
    "get_enterprise_record_spec",
]
