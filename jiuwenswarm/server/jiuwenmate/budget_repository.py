"""Model budget accounts, per-call reservations and settlements, and limit adjustments.

A group Session is charged to its group's account so every colleague in the
group shares one limit; other Sessions are charged to the colleague. Admission
checks and reserves in one immediate transaction, so concurrent calls never
oversell. Each ``model_call_id`` is charged at most once; unknown usage charges
the reservation and stays flagged for reconciliation. A limit only changes
through ``adjust_limit``, and every change is kept with a new revision.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from jiuwenswarm.server.jiuwenmate.execution_models import PersistentContractError
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError, require_identifier
from jiuwenswarm.server.jiuwenmate.sqlite_store import SQLiteStore
from openjiuwen.harness.persistent_agent import InvocationRef, ModelUsage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_accounts (
    namespace_id TEXT NOT NULL,
    account_kind TEXT NOT NULL,
    account_key TEXT NOT NULL,
    limit_tokens INTEGER,
    used_tokens INTEGER NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, account_kind, account_key)
);
CREATE TABLE IF NOT EXISTS budget_calls (
    namespace_id TEXT NOT NULL,
    model_call_id TEXT NOT NULL,
    account_kind TEXT NOT NULL,
    account_key TEXT NOT NULL,
    ref_json TEXT NOT NULL,
    status TEXT NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    account_revision INTEGER NOT NULL,
    reason TEXT,
    usage_json TEXT,
    charged_tokens INTEGER,
    needs_reconciliation INTEGER NOT NULL DEFAULT 0,
    admitted_at TEXT NOT NULL,
    settled_at TEXT,
    PRIMARY KEY (namespace_id, model_call_id)
);
CREATE TABLE IF NOT EXISTS budget_adjustments (
    namespace_id TEXT NOT NULL,
    account_kind TEXT NOT NULL,
    account_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    limit_tokens INTEGER,
    reason TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    adjusted_at TEXT NOT NULL,
    PRIMARY KEY (namespace_id, account_kind, account_key, revision)
);
"""

AccountKind = Literal["group", "agent"]


@dataclass(frozen=True)
class BudgetAccountRef:
    kind: AccountKind
    key: str


@dataclass(frozen=True, kw_only=True)
class BudgetAccountView:
    account: BudgetAccountRef
    limit_tokens: int | None
    used_tokens: int
    reserved_tokens: int
    revision: int
    unreconciled_calls: int


@dataclass(frozen=True, kw_only=True)
class BudgetAdjustment:
    revision: int
    limit_tokens: int | None
    reason: str
    actor_ref: str
    adjusted_at: datetime


@dataclass(frozen=True)
class CallDecision:
    """``admitted`` / ``denied`` for admission, ``confirmed`` / ``rejected`` for settlement."""

    status: Literal["admitted", "denied", "confirmed", "rejected"]
    reason: str | None = None


def reservation_ref(model_call_id: str) -> str:
    return f"res:{model_call_id}"


def receipt_ref(model_call_id: str) -> str:
    return f"rcpt:{model_call_id}"


def _account_args(namespace_id: str, account: BudgetAccountRef) -> tuple[str, str, str]:
    return namespace_id, account.kind, account.key


class BudgetRepository(SQLiteStore):
    _schema = _SCHEMA

    @staticmethod
    def _ensure_account(conn: sqlite3.Connection, namespace_id: str, account: BudgetAccountRef, now: datetime):
        conn.execute(
            "INSERT OR IGNORE INTO budget_accounts VALUES (?, ?, ?, NULL, 0, 0, 0, ?)",
            (*_account_args(namespace_id, account), now.isoformat()),
        )
        return conn.execute(
            "SELECT * FROM budget_accounts WHERE namespace_id = ? AND account_kind = ? AND account_key = ?",
            _account_args(namespace_id, account),
        ).fetchone()

    def reserve(
        self, ref: InvocationRef, model_call_id: str, account: BudgetAccountRef, tokens: int, *, now: datetime
    ) -> CallDecision:
        require_identifier("model_call_id", model_call_id)
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM budget_calls WHERE namespace_id = ? AND model_call_id = ?",
                (ref.namespace_id, model_call_id),
            ).fetchone()
            if existing is not None:
                if existing["ref_json"] != ref.model_dump_json():
                    raise PersistentContractError("model_call_id belongs to another Execution")
                status = "admitted" if existing["status"] != "denied" else "denied"
                return CallDecision(status, existing["reason"])
            row = self._ensure_account(conn, ref.namespace_id, account, now)
            limit = row["limit_tokens"]
            committed = row["used_tokens"] + row["reserved_tokens"]
            admitted = limit is None or committed + tokens <= limit
            reason = None
            if admitted:
                conn.execute(
                    "UPDATE budget_accounts SET reserved_tokens = reserved_tokens + ?, updated_at = ?"
                    " WHERE namespace_id = ? AND account_kind = ? AND account_key = ?",
                    (tokens, now.isoformat(), *_account_args(ref.namespace_id, account)),
                )
            else:
                reason = f"{account.kind} budget exhausted: {committed}/{limit} tokens"
            conn.execute(
                "INSERT INTO budget_calls (namespace_id, model_call_id, account_kind, account_key, ref_json, status,"
                " reserved_tokens, account_revision, reason, admitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ref.namespace_id,
                    model_call_id,
                    account.kind,
                    account.key,
                    ref.model_dump_json(),
                    "reserved" if admitted else "denied",
                    tokens if admitted else 0,
                    row["revision"],
                    reason,
                    now.isoformat(),
                ),
            )
            return CallDecision("admitted" if admitted else "denied", reason)

    def settle(self, ref: InvocationRef, model_call_id: str, usage: ModelUsage, *, now: datetime) -> CallDecision:
        with self._transaction() as conn:
            call = conn.execute(
                "SELECT * FROM budget_calls WHERE namespace_id = ? AND model_call_id = ?",
                (ref.namespace_id, model_call_id),
            ).fetchone()
            if call is None or call["ref_json"] != ref.model_dump_json():
                return CallDecision("rejected", "model call was not admitted for this Execution")
            if call["status"] == "denied":
                return CallDecision("rejected", "model call was denied")
            if call["status"] == "settled":
                if call["usage_json"] == usage.model_dump_json():
                    return CallDecision("confirmed")
                return CallDecision("rejected", "model call was already settled with other usage")
            unknown = usage.status == "unknown"
            charged = call["reserved_tokens"] if unknown else usage.input_tokens + usage.output_tokens
            conn.execute(
                "UPDATE budget_calls SET status = 'settled', usage_json = ?, charged_tokens = ?,"
                " needs_reconciliation = ?, settled_at = ? WHERE namespace_id = ? AND model_call_id = ?",
                (usage.model_dump_json(), charged, int(unknown), now.isoformat(), ref.namespace_id, model_call_id),
            )
            conn.execute(
                "UPDATE budget_accounts SET used_tokens = used_tokens + ?, reserved_tokens = reserved_tokens - ?,"
                " updated_at = ? WHERE namespace_id = ? AND account_kind = ? AND account_key = ?",
                (
                    charged,
                    call["reserved_tokens"],
                    now.isoformat(),
                    ref.namespace_id,
                    call["account_kind"],
                    call["account_key"],
                ),
            )
            return CallDecision("confirmed")

    def account(self, namespace_id: str, account: BudgetAccountRef) -> BudgetAccountView:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM budget_accounts WHERE namespace_id = ? AND account_kind = ? AND account_key = ?",
                _account_args(namespace_id, account),
            ).fetchone()
            unreconciled = conn.execute(
                "SELECT COUNT(*) FROM budget_calls WHERE namespace_id = ? AND account_kind = ? AND account_key = ?"
                " AND needs_reconciliation = 1",
                _account_args(namespace_id, account),
            ).fetchone()[0]
        if row is None:
            return BudgetAccountView(
                account=account, limit_tokens=None, used_tokens=0, reserved_tokens=0, revision=0, unreconciled_calls=0
            )
        return BudgetAccountView(
            account=account,
            limit_tokens=row["limit_tokens"],
            used_tokens=row["used_tokens"],
            reserved_tokens=row["reserved_tokens"],
            revision=row["revision"],
            unreconciled_calls=unreconciled,
        )

    def adjust_limit(
        self,
        namespace_id: str,
        account: BudgetAccountRef,
        limit_tokens: int | None,
        *,
        reason: str,
        actor_ref: str,
        now: datetime,
    ) -> int:
        """Set a new limit (``None`` removes it) and keep the change; returns the new revision."""
        require_identifier("namespace_id", namespace_id)
        require_identifier("actor_ref", actor_ref)
        valid_limit = isinstance(limit_tokens, int) and not isinstance(limit_tokens, bool) and limit_tokens >= 0
        if limit_tokens is not None and not valid_limit:
            raise PersistentAgentError("limit_tokens must be a non-negative integer or None")
        if not isinstance(reason, str) or not reason.strip():
            raise PersistentAgentError("an adjustment needs a reason")
        with self._transaction() as conn:
            revision = self._ensure_account(conn, namespace_id, account, now)["revision"] + 1
            conn.execute(
                "UPDATE budget_accounts SET limit_tokens = ?, revision = ?, updated_at = ?"
                " WHERE namespace_id = ? AND account_kind = ? AND account_key = ?",
                (limit_tokens, revision, now.isoformat(), *_account_args(namespace_id, account)),
            )
            conn.execute(
                "INSERT INTO budget_adjustments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (*_account_args(namespace_id, account), revision, limit_tokens, reason, actor_ref, now.isoformat()),
            )
        return revision

    def adjustments_since_denial(self, namespace_id: str, model_call_id: str) -> list[BudgetAdjustment]:
        """Adjustments made after the call was denied; empty means the condition has not changed."""
        with self._transaction() as conn:
            call = conn.execute(
                "SELECT * FROM budget_calls WHERE namespace_id = ? AND model_call_id = ? AND status = 'denied'",
                (namespace_id, model_call_id),
            ).fetchone()
            if call is None:
                raise PersistentAgentError("denied model call not found", code="NOT_FOUND")
            rows = conn.execute(
                "SELECT * FROM budget_adjustments WHERE namespace_id = ? AND account_kind = ? AND account_key = ?"
                " AND revision > ? ORDER BY revision",
                (namespace_id, call["account_kind"], call["account_key"], call["account_revision"]),
            ).fetchall()
        return [
            BudgetAdjustment(
                revision=row["revision"],
                limit_tokens=row["limit_tokens"],
                reason=row["reason"],
                actor_ref=row["actor_ref"],
                adjusted_at=datetime.fromisoformat(row["adjusted_at"]),
            )
            for row in rows
        ]
