# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging

from .appliers import TABLE_APPLIERS, TableApplyContext
from .db import POLL_TABLES, list_table_records, row_snapshot

logger = logging.getLogger(__name__)

if frozenset(TABLE_APPLIERS) != frozenset(POLL_TABLES):
    raise RuntimeError(
        f"[ConfigPoll] TABLE_APPLIERS keys {sorted(TABLE_APPLIERS)} "
        f"!= POLL_TABLES {list(POLL_TABLES)}"
    )


class ConfigPollSyncer:
    """按行拉取全表，用 ``row_key -> updated_at`` 判断行更新或行删除。"""

    def __init__(self) -> None:
        self._last_rows: dict[str, dict[str, str]] = {}

    async def run_once(self) -> None:
        for table in POLL_TABLES:
            apply_fn = TABLE_APPLIERS.get(table)
            if apply_fn is None:
                logger.warning("[ConfigPoll] no applier registered for table=%s", table)
                continue
            try:
                await self._sync_table(table, apply_fn)
            except Exception:  # noqa: BLE001
                logger.exception("[ConfigPoll] sync failed table=%s", table)

    async def _sync_table(self, table: str, apply_fn) -> None:
        rows = await list_table_records(table)
        current = row_snapshot(table, rows)
        previous = self._last_rows.get(table, {})
        if current == previous:
            return

        removed_channel_ids = frozenset()
        if table == "channel_config":
            removed_channel_ids = frozenset(previous.keys()) - frozenset(current.keys())

        ctx = TableApplyContext(rows=rows, removed_channel_ids=removed_channel_ids)
        await apply_fn(ctx)
        self._last_rows[table] = current

        logger.info(
            "[ConfigPoll] table synced table=%s rows=%d removed=%d",
            table,
            len(current),
            len(removed_channel_ids),
        )
