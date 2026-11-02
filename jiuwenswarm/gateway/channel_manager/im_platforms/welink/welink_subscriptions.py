# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Durable group subscriptions established only by assistant-link mentions."""
from contextlib import closing
from pathlib import Path
import sqlite3


class MentionSubscriptions:
    def __init__(self, path: Path, account: str):
        self.path, self.account = path, account.casefold()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS subscriptions ('
                         'account TEXT, credential TEXT, agent TEXT, group_id TEXT, '
                         'PRIMARY KEY(account, credential, agent, group_id))')
        path.chmod(0o600)

    def register(self, credential: str, agent: str, group: str) -> None:
        if not all((credential, agent, group)) or group.startswith('p2p:'):
            return
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('INSERT OR IGNORE INTO subscriptions VALUES (?,?,?,?)',
                         (self.account, credential, agent, group))

    def targets(self, group: str) -> set[tuple[str, str]]:
        with closing(sqlite3.connect(self.path)) as conn:
            return set(conn.execute('SELECT credential,agent FROM subscriptions '
                                     'WHERE account=? AND group_id=?', (self.account, group)))

    def groups(self, bindings: dict[str, str]) -> list[str]:
        with closing(sqlite3.connect(self.path)) as conn:
            rows = conn.execute('SELECT credential,agent,group_id FROM subscriptions WHERE account=?',
                                (self.account,)).fetchall()
        return sorted({group for credential, agent, group in rows if bindings.get(credential) == agent})

    def revoke(self, group: str) -> None:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('DELETE FROM subscriptions WHERE account=? AND group_id=?', (self.account, group))
