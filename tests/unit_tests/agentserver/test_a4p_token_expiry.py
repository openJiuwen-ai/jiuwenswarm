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
    assert store.summaries() == {}
