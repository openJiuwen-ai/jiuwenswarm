"""将 ``EffectiveEnterpriseConfig`` 中的模型模板写入 config 快照（覆盖 ``config.yaml`` 对应槽位）。"""

from __future__ import annotations

import copy
import logging
from typing import Any

from .schemas import (
    EffectiveEnterpriseConfig,
    TemplateRefSlot,
)

logger = logging.getLogger(__name__)

EXEC_PREFIX = "exec:"

SLOT_TO_CONFIG_KEY: dict[TemplateRefSlot, str] = {
    TemplateRefSlot.DEFAULT_MODEL: "default",
    TemplateRefSlot.VISION_MODEL: "vision",
    TemplateRefSlot.AUDIO_MODEL: "audio",
    TemplateRefSlot.VIDEO_MODEL: "video",
    TemplateRefSlot.IMAGE_GEN_MODEL: "image_gen",
}


def model_headers_from_data(data: Any) -> dict[str, str] | None:
    """读取 ``model_template.data.headers``，供 ``model_client_config.custom_headers`` 使用。"""
    if not isinstance(data, dict):
        return None
    raw = data.get("headers")
    if not isinstance(raw, dict) or not raw:
        return None
    headers: dict[str, str] = {}
    for raw_name, raw_value in raw.items():
        name = str(raw_name).strip()
        if not name or not isinstance(raw_value, str):
            continue
        headers[name] = raw_value
    return headers or None


def model_entity_to_config_entry(entity: dict[str, Any]) -> dict[str, Any]:
    """将 ``model_template`` 行转为 ``get_default_models`` 兼容的条目。

    页面「重试次数」字段 ``retry_count`` 写入：
    - ``transient_max_retries``：模型配置正式字段，供
      ``execution_guard.model_anomaly_detection_rail.transient_max_retries`` 同步；
    - ``model_client_config.max_retries``：兼容旧字段名（SDK 仍固定 0，不由此驱动）。
    """
    parameters = entity.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}

    mcc: dict[str, Any] = {
        "api_base": str(entity.get("api_base") or "").strip(),
        "api_key": str(entity.get("api_key") or "").strip(),
        "model_name": str(entity.get("model_id") or "").strip(),
        "client_provider": str(entity.get("model_provider") or "").strip(),
        "timeout": entity.get("timeout", 1800),
        "verify_ssl": bool(entity.get("verify_ssl", False)),
    }
    # Template api_key is a database plaintext field, not a config.yaml
    # declaration surface: an "exec:" string here is undefined input that
    # resolve_env_vars would otherwise treat as a command to run. Reject it
    # and fall back to an empty key rather than executing DB-sourced text.
    if mcc["api_key"].startswith(EXEC_PREFIX):
        logger.warning(
            "[enterprise] model template %s api_key looks like an exec: reference; "
            "references only support config.yaml — treating it as empty",
            str(entity.get("template_name") or entity.get("template_id") or "?"),
        )
        mcc["api_key"] = ""
    headers = model_headers_from_data(entity.get("data"))
    if headers:
        # Same trust boundary as api_key above: template headers come from the
        # database, and the registry routes custom_headers.* values through
        # resolve_env_vars — an "exec:" string there would be executed. Drop
        # offending headers with a warning instead.
        offending = [name for name, value in headers.items() if str(value).startswith(EXEC_PREFIX)]
        for name in offending:
            logger.warning(
                "[enterprise] model template %s header %s looks like an exec: reference; "
                "references only support config.yaml — dropping the header",
                str(entity.get("template_name") or entity.get("template_id") or "?"),
                name,
            )
            headers.pop(name)
        if headers:
            mcc["custom_headers"] = headers
    retry_count = entity.get("retry_count")
    if retry_count is not None:
        mcc["max_retries"] = retry_count

    return {
        "template_name": str(entity.get("template_name") or "").strip(),
        "template_id": str(entity.get("template_id") or "").strip(),
        "model_client_config": mcc,
        "model_config_obj": parameters,
        "transient_max_retries": retry_count,
    }


def embedding_entity_to_config_section(entity: dict[str, Any]) -> dict[str, str]:
    """将 ``embedding_template`` 行转为 ``config.yaml`` 的 ``embed`` 配置段。"""
    return {
        "embed_api_key": str(entity.get("api_key") or "").strip(),
        "embed_base_url": str(entity.get("api_base") or "").strip(),
        "embed_model": str(entity.get("model_id") or "").strip(),
    }


def _sync_default_model_retry_to_anomaly_rail(
    merged: dict[str, Any],
    default_entry: dict[str, Any],
) -> None:
    """把默认模型的 ``transient_max_retries`` 写入 anomaly rail 配置段。

    Rail 构建只读 ``execution_guard.model_anomaly_detection_rail``，与其它字段一致；
    企业页 ``retry_count`` 在此落地，避免构建期再反查 models。

    同时确保 ``enabled=True``：挂载入口以 ``enabled is True`` 为前提；若段缺失或
    仍为 ``false``，只写 ``transient_max_retries`` 不会触发重试。
    """
    value = default_entry.get("transient_max_retries")
    if value is None:
        return
    guard = merged.get("execution_guard")
    if not isinstance(guard, dict):
        guard = {}
        merged["execution_guard"] = guard
    rail = guard.get("model_anomaly_detection_rail")
    if not isinstance(rail, dict):
        rail = {}
        guard["model_anomaly_detection_rail"] = rail
    # Enterprise retry_count implies the rail must be mounted; builders skip
    # when enabled is missing/false (default False in .get).
    if rail.get("enabled") is not True:
        logger.info(
            "[enterprise] enabling model_anomaly_detection_rail so default-model "
            "retry_count=%s can take effect",
            value,
        )
        rail["enabled"] = True
    rail["transient_max_retries"] = value


def apply_enterprise_models_to_config(
    config_base: dict[str, Any],
    enterprise: EffectiveEnterpriseConfig,
) -> tuple[dict[str, Any], bool]:
    """深拷贝 ``config_base`` 并写入企业模型与 Embedding 槽位；返回 ``(merged, applied_any)``。

    ``template_ref`` 各槽位可解析出多个 ``template_id``，但写入 ``config.yaml`` 兼容结构时，
    **每个模型槽位仅取列表首项**（第一个有效模板）写入 ``SLOT_TO_CONFIG_KEY`` 对应的 config 键
    （如 ``default_model`` → ``models.default``）。其余 ``template_id`` 不在此函数中展开。
    """
    embedding_entities = getattr(enterprise, "embedding", None)
    if not enterprise.models and not embedding_entities:
        return config_base, False

    merged = copy.deepcopy(config_base)
    models_section = merged.get("models")
    if not isinstance(models_section, dict):
        models_section = {}
        merged["models"] = models_section

    applied = False
    default_entry: dict[str, Any] | None = None

    for slot, entities in enterprise.models.items():
        if not isinstance(entities, list):
            entities = [entities] if isinstance(entities, dict) else []
        try:
            slot_key = TemplateRefSlot(slot)
        except ValueError:
            continue
        config_key = SLOT_TO_CONFIG_KEY.get(slot_key)
        if not config_key:
            continue

        entry: dict[str, Any] | None = None
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            candidate = model_entity_to_config_entry(entity)
            mcc = candidate.get("model_client_config") or {}
            if not str(mcc.get("model_name") or "").strip():
                continue
            entry = candidate
            break

        if entry is None:
            continue

        mcc = entry.get("model_client_config") or {}
        models_section[config_key] = {
            "model_client_config": dict(mcc),
            "model_config_obj": dict(entry.get("model_config_obj") or {}),
            "transient_max_retries": entry.get("transient_max_retries"),
        }
        if config_key == "default":
            default_entry = entry
        applied = True

    if default_entry is not None:
        # Template slots replace the same config.yaml sections wholesale; an
        # exec: reference stored there would be dropped silently while the
        # user assumes it still applies. Surface the loss before overwriting.
        for section in ("defaults", "default"):
            existing = models_section.get(section)
            entries = (
                existing if isinstance(existing, list)
                else ([existing] if isinstance(existing, dict) else [])
            )
            for item in entries:
                if not isinstance(item, dict):
                    continue
                mcc = item.get("model_client_config") or {}
                api_key = str(mcc.get("api_key") or "")
                if api_key.startswith(EXEC_PREFIX):
                    logger.warning(
                        "[enterprise] config.yaml models.%s api_key uses an exec: reference; "
                        "the enterprise template for the default model slot replaces this section "
                        "and the reference will not take effect",
                        section,
                    )
        models_section["defaults"] = [default_entry]
        models_section["default"] = default_entry
        react = merged.get("react")
        if isinstance(react, dict):
            mcc = default_entry.get("model_client_config") or {}
            if mcc.get("model_name"):
                react["model_name"] = mcc["model_name"]
            if mcc:
                react["model_client_config"] = dict(mcc)
            mco = default_entry.get("model_config_obj")
            if isinstance(mco, dict) and mco:
                react["model_config_obj"] = dict(mco)
        _sync_default_model_retry_to_anomaly_rail(merged, default_entry)

    if isinstance(embedding_entities, dict):
        embedding_entities = [embedding_entities]
    if isinstance(embedding_entities, list):
        for entity in embedding_entities:
            if not isinstance(entity, dict):
                continue
            embed_section = embedding_entity_to_config_section(entity)
            if not all(embed_section.values()):
                continue
            merged["embed"] = embed_section
            applied = True
            break

    return merged, applied
