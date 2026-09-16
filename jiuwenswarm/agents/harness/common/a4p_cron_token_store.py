# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent JiuwenSwarm ownership store for A4P cron intent tokens."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.a4p_token_expiry import token_expired


CRON_INTENT_TOKENS_DB_FILENAME = "cron_intent_tokens.sqlite3"
CRON_INTENT_TOKENS_VERSION = 1


class SQLiteCronIntentTokenStore:
    """Atomically maps JiuwenSwarm cron job ids to A4P intent tokens."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = float(timeout_seconds)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        try:
            connection.execute(
                f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cron_intent_tokens (
                    cron_job_id TEXT PRIMARY KEY,
                    token_json TEXT NOT NULL,
                    created_at_epoch INTEGER NOT NULL
                )
                """
            )
            connection.execute(f"PRAGMA user_version = {CRON_INTENT_TOKENS_VERSION}")
        except Exception:
            connection.close()
            raise
        return connection

    def put(self, cron_job_id: str, token: dict[str, Any]) -> None:
        job_id = str(cron_job_id or "").strip()
        if not job_id or not isinstance(token, dict) or token_expired(token):
            return
        encoded = json.dumps(token, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO cron_intent_tokens (
                    cron_job_id,
                    token_json,
                    created_at_epoch
                ) VALUES (?, ?, ?)
                ON CONFLICT(cron_job_id) DO UPDATE SET
                    token_json = excluded.token_json,
                    created_at_epoch = excluded.created_at_epoch
                """,
                (job_id, encoded, int(time.time())),
            )
            connection.commit()

    def get(self, cron_job_id: str) -> dict[str, Any] | None:
        job_id = str(cron_job_id or "").strip()
        if not job_id or not self.path.exists():
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT token_json
                FROM cron_intent_tokens
                WHERE cron_job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        encoded = str(row[0])
        try:
            token = json.loads(encoded)
        except (TypeError, ValueError):
            self._remove_if_unchanged(job_id, encoded)
            return None
        if not isinstance(token, dict) or token_expired(token):
            self._remove_if_unchanged(job_id, encoded)
            return None
        return token

    def remove(self, cron_job_id: str) -> None:
        job_id = str(cron_job_id or "").strip()
        if not job_id or not self.path.exists():
            return
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM cron_intent_tokens WHERE cron_job_id = ?",
                (job_id,),
            )
            connection.commit()

    def _remove_if_unchanged(self, cron_job_id: str, token_json: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM cron_intent_tokens
                WHERE cron_job_id = ? AND token_json = ?
                """,
                (cron_job_id, token_json),
            )
            connection.commit()

    def summaries(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT cron_job_id, token_json, created_at_epoch
                FROM cron_intent_tokens
                ORDER BY cron_job_id
                """
            ).fetchall()
        summaries: dict[str, dict[str, Any]] = {}
        stale_rows: list[tuple[str, str]] = []
        for job_id, token_json, created_at_epoch in rows:
            encoded = str(token_json)
            try:
                token = json.loads(encoded)
            except (TypeError, ValueError):
                stale_rows.append((str(job_id), encoded))
                continue
            if not isinstance(token, dict) or token_expired(token):
                stale_rows.append((str(job_id), encoded))
                continue
            summaries[str(job_id)] = {
                "tokenId": token.get("tokenId"),
                "expireAt": token.get("expireAt"),
                "subject": deepcopy(token.get("subject")),
                "intent": deepcopy(token.get("intent")),
                "createdAtEpoch": int(created_at_epoch),
            }
        for job_id, token_json in stale_rows:
            self._remove_if_unchanged(job_id, token_json)
        return summaries


__all__ = [
    "CRON_INTENT_TOKENS_DB_FILENAME",
    "CRON_INTENT_TOKENS_VERSION",
    "SQLiteCronIntentTokenStore",
]
