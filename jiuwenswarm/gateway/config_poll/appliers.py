# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TableApplyContext:
    rows: list[dict[str, Any]]
    removed_channel_ids: frozenset[str] = frozenset()


ApplyTableFn = Callable[[TableApplyContext], Awaitable[None]]


def _enabled_log_masking_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enabled_rows: list[dict[str, Any]] = []
    for row in rows:
        enabled = row.get("enabled", True)
        if isinstance(enabled, int):
            enabled = bool(enabled)
        if enabled:
            enabled_rows.append(row)
    enabled_rows.sort(
        key=lambda item: (-int(item.get("priority") or 0), int(item.get("id") or 0)),
    )
    return enabled_rows


async def apply_logging_config_table(ctx: TableApplyContext) -> None:
    from jiuwenswarm.common.utils import (
        _logging_config_row_to_dict,
        apply_logging_config_payload,
    )

    row = ctx.rows[0] if ctx.rows else None
    if len(ctx.rows) > 1:
        logger.warning(
            "[ConfigPoll] logging_config expected one row, got %d; using first",
            len(ctx.rows),
        )
    apply_logging_config_payload(
        _logging_config_row_to_dict(row) if row is not None else None,
    )
    logger.info("[ConfigPoll] logging_config applied rows=%d", len(ctx.rows))


async def apply_audit_log_config_table(ctx: TableApplyContext) -> None:
    from jiuwenswarm.gateway.config.audit.access import apply_audit_log_config_row

    row = ctx.rows[0] if ctx.rows else None
    if len(ctx.rows) > 1:
        logger.warning(
            "[ConfigPoll] audit_log_config expected one row, got %d; using first",
            len(ctx.rows),
        )
    apply_audit_log_config_row(row if isinstance(row, dict) else None)
    logger.info("[ConfigPoll] audit_log_config applied rows=%d", len(ctx.rows))


async def apply_log_masking_rule_table(ctx: TableApplyContext) -> None:
    from jiuwenswarm.infrastructure.log_masking.engine import LogMaskingEngine

    rows = _enabled_log_masking_rows(ctx.rows)
    LogMaskingEngine.reload_from_rows(rows, db_authoritative=True)
    logger.info("[ConfigPoll] log_masking_rule applied rows=%d", len(rows))


async def apply_channel_config_table(ctx: TableApplyContext) -> None:
    try:
        from jiuwenswarm.gateway.channel_config_overlay import ChannelConfigChange
        from jiuwenswarm.gateway.channel_config_reload import maybe_trigger_channel_config_reload
    except ImportError as exc:
        raise RuntimeError(
            "channel_config reload unavailable; manager_config_receiver not loaded"
        ) from exc

    for channel_id in sorted(ctx.removed_channel_ids):
        await maybe_trigger_channel_config_reload(
            ChannelConfigChange.remove({"channel_id": channel_id})
        )

    for record in ctx.rows:
        status = str(record.get("status") or "").strip().lower()
        if status == "active":
            await maybe_trigger_channel_config_reload(ChannelConfigChange.upsert(record))
        else:
            await maybe_trigger_channel_config_reload(ChannelConfigChange.remove(record))

    logger.info(
        "[ConfigPoll] channel_config reloaded rows=%d removed=%d",
        len(ctx.rows),
        len(ctx.removed_channel_ids),
    )


TABLE_APPLIERS: dict[str, ApplyTableFn] = {
    "channel_config": apply_channel_config_table,
    "logging_config": apply_logging_config_table,
    "log_masking_rule": apply_log_masking_rule_table,
    "audit_log_config": apply_audit_log_config_table,
}
