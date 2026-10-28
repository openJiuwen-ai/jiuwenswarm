"""企业版定时任务的模型名核对：读当前身份在企业库里可用的对话模型。

与网页 ``models.list`` 同源（``load_effective_enterprise_config`` +
``default_model`` 槽），不读本机 ``config.yaml`` 里的占位名。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

CRON_MODEL_SOURCE_ENTERPRISE = "enterprise"


async def list_enterprise_default_models(
    *,
    group_id: str = "",
    bot_id: str = "",
    user_id: str = "",
) -> list[dict[str, str]]:
    """返回当前身份可用的对话模型（只有名字和别名，不含密钥）。"""
    from jiuwenswarm.common.request_identity import (
        apply_routing_metadata,
        normalize_routing_identity,
    )
    from gateway.enterprise.config.effective.loader import (
        load_effective_enterprise_config,
    )
    from gateway.enterprise.config.effective.schemas import TemplateRefSlot

    identity = normalize_routing_identity(
        {
            "group_id": group_id,
            "bot_id": bot_id,
            "user_id": user_id,
        }
    )
    request = SimpleNamespace(metadata=apply_routing_metadata({}, identity))
    enterprise = await load_effective_enterprise_config(
        request,
        {TemplateRefSlot.DEFAULT_MODEL},
    )
    entities = (
        enterprise.models.get(TemplateRefSlot.DEFAULT_MODEL.value, [])
        if enterprise is not None
        else []
    )
    rows: list[dict[str, str]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        model_id = str(entity.get("model_id") or "").strip()
        if not model_id:
            continue
        rows.append(
            {
                "model_name": model_id,
                "alias": str(entity.get("template_name") or "").strip(),
            }
        )
    return rows


async def resolve_enterprise_cron_model(
    raw: Any,
    *,
    group_id: str = "",
    bot_id: str = "",
    user_id: str = "",
) -> str | None:
    """把传入的模型名核对企业库名单，返回规范 ``model_id``；空输入返回 None。"""
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None

    try:
        from jiuwenswarm.common.auth.login_credentials import bare_model_name

        bare = bare_model_name(value)
    except Exception:  # noqa: BLE001 — 后缀剥离失败时仍用原名匹配
        bare = value

    rows = await list_enterprise_default_models(
        group_id=group_id,
        bot_id=bot_id,
        user_id=user_id,
    )
    for row in rows:
        model_name = row["model_name"]
        alias = row.get("alias") or ""
        if value in {model_name, alias} or bare == model_name:
            return model_name

    available = [row["model_name"] for row in rows]
    hint = ", ".join(available[:20]) if available else "(no models configured)"
    if len(available) > 20:
        hint += f" ... and {len(available) - 20} more"
    raise ValueError(f"Unknown model {value!r}. Available models: {hint}")
