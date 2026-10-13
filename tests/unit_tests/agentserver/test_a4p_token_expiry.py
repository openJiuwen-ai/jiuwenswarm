import json
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.agents.harness.common import a4p_token_expiry
from jiuwenswarm.agents.harness.common.a4p_runtime import A4PRuntime, A4PIdentity
from jiuwenswarm.agents.harness.common.a4p_cron_token_store import (
    SQLiteCronIntentTokenStore,
)


@pytest.mark.parametrize(("now", "expired"), [(0, False), (1, False), (1.01, True)])
def test_sdk_expiry_boundary(monkeypatch, now, expired):
    monkeypatch.setattr(a4p_token_expiry.time, "time", lambda: now)
    assert (
        a4p_token_expiry.token_expired({"expireAt": "1970-01-01T00:00:01Z"}) is expired
    )


@pytest.mark.parametrize(
    "value", [None, "", "invalid", "2099-01-01", "2099-01-01T00:00:00+00:00"]
)
def test_invalid_expiry(value):
    assert a4p_token_expiry.token_expired({"expireAt": value})


def runtime():
    instance = object.__new__(A4PRuntime)
    instance._session_tokens = {}
    return instance


def token(token_id="valid", expiry="2099-01-01T00:00:00Z"):
    return {"tokenId": token_id, "expireAt": expiry}


def test_storage_and_transfer_prune():
    instance = runtime()
    valid = token()
    expired = token("expired", "2000-01-01T00:00:00Z")
    instance.store_intent_token("s", valid)
    replacement = {**valid, "extra": True}
    instance.store_intent_token("s", replacement)
    instance.store_intent_token("s", expired)
    instance.store_intent_token("s", {"tokenId": "invalid"})
    assert instance._session_tokens == {"s": [replacement]}
    instance._session_tokens.update(
        {"empty": [], "old": [expired], "mixed": [expired, valid]}
    )
    exported = instance.export_session_tokens()
    assert exported == {"s": [replacement], "mixed": [valid]}
    assert instance._session_tokens == exported
    instance.import_session_tokens(
        {"empty": [], "old": [expired], "mixed": [expired, valid]}
    )
    assert instance._session_tokens == {"mixed": [valid]}
    exported["mixed"][0]["tokenId"] = "mutated"
    assert instance._session_tokens["mixed"][0]["tokenId"] == "valid"


@pytest.mark.asyncio
async def test_find_prunes_before_sdk_and_keeps_scope_mismatch():
    instance = runtime()
    expired = token("expired", "2000-01-01T00:00:00Z")
    valid = token()
    instance._session_tokens = {"s": [expired, {}, valid], "old": [expired]}
    instance._verify_intent_token = AsyncMock(return_value=False)
    kwargs = dict(identity=A4PIdentity("agent"), action="write_file", params={})
    assert await instance.find_valid_intent_token(session_id="s", **kwargs) is None
    assert instance._verify_intent_token.await_count == 1
    assert instance._verify_intent_token.call_args.kwargs["token"] == valid
    assert instance._session_tokens["s"] == [valid]
    assert await instance.find_valid_intent_token(session_id="old", **kwargs) is None
    assert "old" not in instance._session_tokens
    instance._verify_intent_token.return_value = True
    assert await instance.find_valid_intent_token(session_id="s", **kwargs) == valid


def test_cron_rejects_invalid_new_tokens_and_expires_existing(tmp_path, monkeypatch):
    store = SQLiteCronIntentTokenStore(tmp_path / "tokens.sqlite3")
    valid = token()
    store.put("s", valid)
    for invalid in [{}, token("old", "2000-01-01T00:00:00Z"), token("bad", "invalid")]:
        store.put("s", invalid)
        store.put("invalid", invalid)
    assert store.get("s") == valid
    assert store.get("invalid") is None
    monkeypatch.setattr(a4p_token_expiry.time, "time", lambda: 5_000_000_000)
    assert store.get("s") is None
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cron_intent_tokens").fetchone() == (0,)


def seed_tokens(store):
    store.put("valid", token())
    with closing(sqlite3.connect(store.path)) as connection:
        connection.executemany(
            "INSERT INTO cron_intent_tokens VALUES (?, ?, 0)",
            [
                ("expired", json.dumps(token("old", "2000-01-01T00:00:00Z"))),
                ("invalid-expiry", json.dumps(token("bad", "invalid"))),
                ("invalid-json", "{"),
                ("invalid-type", "[]"),
            ],
        )
        connection.commit()


def test_prune_expired_removes_all_stale_rows_without_accessing_jobs(tmp_path):
    store = SQLiteCronIntentTokenStore(tmp_path / "tokens.sqlite3")
    assert store.prune_expired() == 0
    assert not store.path.exists()
    seed_tokens(store)
    assert store.prune_expired() == 4
    assert store.prune_expired() == 0
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute("SELECT cron_job_id FROM cron_intent_tokens").fetchall() == [("valid",)]
    assert store.get("valid") == token()


def test_prune_preserves_concurrently_replaced_token(tmp_path, monkeypatch):
    store = SQLiteCronIntentTokenStore(tmp_path / "tokens.sqlite3")
    seed_tokens(store)
    remove = store._remove_if_unchanged

    def replace_before_remove(job_id, encoded):
        store.put(job_id, token("replacement"))
        return remove(job_id, encoded)

    monkeypatch.setattr(store, "_remove_if_unchanged", replace_before_remove)
    assert store.prune_expired() == 0
    assert store.get("expired") == token("replacement")


def test_runtime_initialization_prunes_persisted_expired_tokens(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common import a4p_runtime
    from jiuwenswarm.agents.harness.common.a4p_cron_token_store import CRON_INTENT_TOKENS_DB_FILENAME

    monkeypatch.setattr(a4p_runtime, "get_config_dir", lambda: tmp_path)
    store = SQLiteCronIntentTokenStore(tmp_path / "a4p" / CRON_INTENT_TOKENS_DB_FILENAME)
    seed_tokens(store)
    instance = A4PRuntime({"a4p": {"enabled": True}})
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute("SELECT cron_job_id FROM cron_intent_tokens").fetchall() == [("valid",)]
    assert instance.cron_tokens.get("valid") == token()
