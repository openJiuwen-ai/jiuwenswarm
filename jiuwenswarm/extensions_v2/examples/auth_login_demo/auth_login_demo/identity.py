# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent local mock accounts and non-expiring sessions owned by the plugin."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class MockIdentity:
    """Use isolated plugin data, without changing platform user-data routing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS accounts (username TEXT PRIMARY KEY, salt BLOB NOT NULL, digest BLOB NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES accounts(username))")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=3)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def valid_fields(username: object, password: object) -> bool:
        return (isinstance(username, str) and username == username.strip() and 4 <= len(username) <= 64
                and not any(ord(char) < 32 for char in username)
                and isinstance(password, str) and 8 <= len(password) <= 128
                and re.search(r"[A-Za-z]", password) is not None and re.search(r"[0-9]", password) is not None
                and re.search(r"[^A-Za-z0-9]", password) is not None)

    @staticmethod
    def _digest(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)

    def register(self, username: object, password: object) -> tuple[str, str | None]:
        """Create an account and its first session in one transaction."""
        if not self.valid_fields(username, password):
            return "invalid_fields", None
        salt = secrets.token_bytes(16)
        digest = self._digest(password, salt)
        with self._connect() as connection:
            try:
                connection.execute("INSERT INTO accounts VALUES (?, ?, ?)", (username, salt, digest))
            except sqlite3.IntegrityError:
                return "user_exists", None
            token = secrets.token_urlsafe(32)
            connection.execute("INSERT INTO sessions VALUES (?, ?)", (token, username))
        return "registered", token

    def login(self, username: object, password: object) -> str | None:
        if not self.valid_fields(username, password):
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT salt, digest FROM accounts WHERE username=?", (username,)).fetchone()
            if row is None or not hmac.compare_digest(row[1], self._digest(password, row[0])):
                return None
            token = secrets.token_urlsafe(32)
            connection.execute("INSERT INTO sessions VALUES (?, ?)", (token, username))
            return token

    def verify(self, token: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT username FROM sessions WHERE token=?", (token,)).fetchone()
        return row[0] if row else None

    def logout(self, token: str) -> bool:
        with self._connect() as connection:
            result = connection.execute("DELETE FROM sessions WHERE token=?", (token,))
        return result.rowcount == 1
