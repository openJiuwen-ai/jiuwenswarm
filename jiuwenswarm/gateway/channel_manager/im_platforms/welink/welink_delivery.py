# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durable send intents and history evidence. Unknown intents are never retransmitted."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from jiuwenswarm.gateway.channel_manager.im_platforms.welink.welink_messages import WelinkHistoryMessage


class DeliveryJournal:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS receipts (
                idempotency_key TEXT PRIMARY KEY, group_id TEXT NOT NULL, text TEXT NOT NULL,
                baseline_id TEXT NOT NULL, assistants TEXT NOT NULL, status TEXT NOT NULL,
                wire_id TEXT, tool_session_id TEXT, trace_id TEXT, rejected INTEGER DEFAULT 0,
                external_message_id TEXT, UNIQUE(group_id, external_message_id))''')
        path.chmod(0o600)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, key: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM receipts WHERE idempotency_key=?', (key,)).fetchone()
        return dict(row) if row is not None else None

    def prepare(self, key: str, group_id: str, text: str, baseline_id: str, assistants: set[str],
                *, serialize_identical: bool = True) -> bool:
        """Commit before enqueueing any socket write; serialize indistinguishable replies."""
        with self._connection() as conn:
            if conn.execute('SELECT 1 FROM receipts WHERE idempotency_key=? OR '
                            '(? AND group_id=? AND text=? AND status=?)',
                            (key, serialize_identical, group_id, text, 'unknown')).fetchone():
                return False
            conn.execute('INSERT INTO receipts(idempotency_key,group_id,text,baseline_id,assistants,status) '
                         'VALUES (?,?,?,?,?,?)', (key, group_id, text, baseline_id, json.dumps(sorted(assistants)), 'unknown'))
        return True

    def mark_wire(self, key: str, wire_id: str, tool_session_id: str, trace_id: str) -> None:
        with self._connection() as conn:
            conn.execute('UPDATE receipts SET wire_id=?,tool_session_id=?,trace_id=? WHERE idempotency_key=?',
                         (wire_id, tool_session_id, trace_id, key))

    def record_error(self, tool_session_id: str, trace_id: str) -> None:
        """Only an error correlated to an actual outgoing send is evidence of rejection."""
        if not tool_session_id and not trace_id:
            return
        with self._connection() as conn:
            conn.execute('UPDATE receipts SET rejected=1 WHERE status=? '
                         'AND (? = \'\' OR tool_session_id=?) AND (? = \'\' OR trace_id=?) '
                         'AND wire_id IS NOT NULL',
                         ('unknown', tool_session_id, tool_session_id, trace_id, trace_id))

    def outcome(self, key: str) -> dict:
        row = self.get(key)
        if row is not None and row['status'] == 'delivered':
            return {'outcome': 'delivered', 'external_message_id': row['external_message_id']}
        if row is not None and row['status'] == 'failed':
            return {'outcome': 'failed', 'error_code': 'WELINK_PLATFORM_REJECTED'}
        return {'outcome': 'unknown', 'error_code': 'WELINK_AWAITING_HISTORY'}

    def reconcile(self, key: str, messages: list[WelinkHistoryMessage]) -> dict:
        learned = ""
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM receipts WHERE idempotency_key=?', (key,)).fetchone()
            if row is not None and row['status'] == 'unknown':
                assistants = set(json.loads(row['assistants']))
                baseline = row['baseline_id']
                matches = [message for message in messages
                           if row['wire_id'] and message.group_id == row['group_id'] and message.sender_id in assistants
                           and message.text == row['text']
                           and (message.message_id == row['wire_id'] or (
                               len(assistants) == 1 and baseline.isdigit() and message.message_id.isdigit()
                               and int(message.message_id) > int(baseline)))]
                ids = {message.message_id for message in matches}
                senders = {message.sender_id for message in matches}
                # With several possible senders, only the exact outgoing message id
                # can identify this assistant. Matching text alone can belong to a peer.
                if len(ids) == 1 and len(senders) == 1:
                    external_id = next(iter(ids))
                    used = conn.execute('SELECT 1 FROM receipts WHERE group_id=? AND external_message_id=?',
                                        (row['group_id'], external_id)).fetchone()
                    if used is None:
                        conn.execute('UPDATE receipts SET status=?,external_message_id=? WHERE idempotency_key=?',
                                     ('delivered', external_id, key))
                        learned = next(iter(senders))
                elif not ids and row['rejected']:
                    conn.execute('UPDATE receipts SET status=? WHERE idempotency_key=?', ('failed', key))
        result = self.outcome(key)
        if learned and result.get('outcome') == 'delivered':
            result['learned_sender'] = learned
        return result
