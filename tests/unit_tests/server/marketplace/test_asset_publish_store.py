# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading

import pytest

from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity
from jiuwenswarm.server.runtime.marketplace.hub_publish_client import PublishRequest
from jiuwenswarm.server.runtime.marketplace.asset_publish_store import (
    PublishStore,
    PublishStoreError,
)


@pytest.fixture
def prepared(tmp_path):
    return PublishRequest(
        PublishIdentity("plugin", "sales", "1.0.0"),
        tmp_path / "asset.zip",
        "a" * 64,
        display_name="Sales",
        description="Useful",
        tags=("crm",),
        visibility="private",
    )


@pytest.fixture
def store(tmp_path):
    value = PublishStore(tmp_path / "publish.sqlite")
    yield value
    value.close()


def save(store, prepared, scope="alice", local_id="local-sales"):
    return store.save_draft(scope, local_id, prepared, expires_at=200, now=100)


def assert_code(code, call):
    with pytest.raises(PublishStoreError) as caught:
        call()
    assert caught.value.code == code


def test_draft_snapshot_survives_reopen_with_owner_and_expiry_checks(
    tmp_path, prepared
):
    path = tmp_path / "db.sqlite"
    store = PublishStore(path)
    draft = save(store, prepared)
    store.close()
    store = PublishStore(path)
    try:
        assert store.get_draft("alice", draft, now=199) == prepared
        assert_code("not_found", lambda: store.get_draft("bob", draft, now=199))
        assert_code("draft_expired", lambda: store.get_draft("alice", draft, now=200))
        assert_code(
            "draft_expired", lambda: store.commit_draft("alice", draft, "late", now=200)
        )
    finally:
        store.close()


def test_commit_is_deduplicated_per_draft_and_request_alias(store, prepared):
    draft = save(store, prepared)
    op, created = store.commit_draft("alice", draft, "click-1", now=101)
    assert created
    assert store.commit_draft("alice", draft, "click-1", now=102) == (op, False)
    assert store.commit_draft("alice", draft, "click-2", now=250) == (op, False)
    other = save(
        store, replace(prepared, identity=PublishIdentity("plugin", "other", "1.0.0"))
    )
    assert_code(
        "request_conflict",
        lambda: store.commit_draft("alice", other, "click-2", now=110),
    )
    assert_code("not_found", lambda: store.status("bob", op))
    assert_code("not_found", lambda: store.start("bob", op))
    assert store.records("bob", "plugin", "local-sales") == []
    record = store.status("alice", op)
    assert record["execution_status"] == "queued"
    assert record["result"] is None
    assert "artifact_path" not in json.dumps(record)
    assert "scope" not in record


def test_two_connections_commit_same_request_once(tmp_path, prepared):
    first = PublishStore(tmp_path / "db.sqlite")
    second = PublishStore(tmp_path / "db.sqlite")
    draft = save(first, prepared)
    barrier = threading.Barrier(2)

    def commit(store):
        barrier.wait()
        return store.commit_draft("alice", draft, "same-click", now=101)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(commit, (first, second)))
        assert values[0][0] == values[1][0]
        assert sorted(item[1] for item in values) == [False, True]
        assert len(first.records("alice", "plugin", "local-sales")) == 1
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("state", ["queued", "uploading", "unknown"])
def test_target_conflict_until_outcome_known(store, prepared, state):
    first = save(store, prepared)
    op, _ = store.commit_draft("alice", first, "one", now=101)
    if state != "queued":
        assert store.start("alice", op)
    if state == "unknown":
        store.finish(
            "alice",
            op,
            execution_status="unknown",
            error={"code": "upload_timeout", "outcome_unknown": True},
        )
    second = save(store, prepared)
    assert_code(
        "target_conflict", lambda: store.commit_draft("alice", second, "two", now=102)
    )
    bob = save(store, prepared, scope="bob")
    assert store.commit_draft("bob", bob, "one", now=102)[1]


def test_queue_bound_and_failed_target_can_be_retried(tmp_path, prepared):
    store = PublishStore(tmp_path / "db.sqlite", max_active_per_scope=1)
    try:
        one = save(store, prepared)
        op, _ = store.commit_draft("alice", one, "one", now=101)
        two = save(
            store,
            replace(prepared, identity=PublishIdentity("plugin", "other", "1.0.0")),
        )
        assert_code(
            "queue_full", lambda: store.commit_draft("alice", two, "two", now=102)
        )
        store.finish(
            "alice", op, execution_status="failed", error={"code": "interrupted"}
        )
        assert store.commit_draft("alice", two, "two", now=103)[1]
    finally:
        store.close()


@pytest.mark.parametrize("visibility", ["private", None])
def test_start_is_claimed_once_and_success_is_saved_atomically(
    tmp_path, prepared, visibility
):
    path = tmp_path / "db.sqlite"
    store = PublishStore(path)
    draft = save(store, prepared)
    op, _ = store.commit_draft("alice", draft, "one", now=101)
    assert store.start("alice", op)
    assert not store.start("alice", op)
    result = dict(
        asset_id="remote-id",
        kind="plugin",
        package_name="sales",
        version="1.0.0",
        publish_result="pending_moderation",
        visibility=visibility,
        deduplicated=False,
    )
    store.finish("alice", op, execution_status="completed", result=result)
    store.close()
    store = PublishStore(path)
    try:
        record = store.status("alice", op)
        assert record["result"] == result
        assert record["execution_status"] == "completed"
        assert record["error"] is None
        assert store.recover_interrupted() == 0
        assert_code(
            "invalid_transition",
            lambda: store.finish(
                "alice", op, execution_status="failed", error={"code": "interrupted"}
            ),
        )
    finally:
        store.close()


def test_restart_marks_queued_failed_and_uploading_unknown(store, prepared):
    one = save(store, prepared)
    op1, _ = store.commit_draft("alice", one, "one", now=101)
    two = save(
        store, replace(prepared, identity=PublishIdentity("plugin", "other", "1.0.0"))
    )
    op2, _ = store.commit_draft("alice", two, "two", now=102)
    store.start("alice", op2)
    assert store.recover_interrupted() == 2
    assert store.status("alice", op1)["execution_status"] == "failed"
    assert store.status("alice", op1)["error"]["code"] == "interrupted"
    assert store.status("alice", op2)["execution_status"] == "unknown"
    assert store.status("alice", op2)["error"]["outcome_unknown"]
    assert not store.start("alice", op1)
    assert store.recover_interrupted() == 0
    assert [
        r["operation_id"] for r in store.records("alice", "plugin", "local-sales")
    ] == [op2, op1]


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "upload_timeout", "access_token": "TOP-SECRET"},
        {"code": "TOP-SECRET"},
        {"code": "upload_timeout", "http_status": "TOP-SECRET"},
    ],
)
def test_unsafe_error_payload_never_persists(tmp_path, store, prepared, payload):
    draft = save(store, prepared)
    op, _ = store.commit_draft("alice", draft, "one", now=101)
    store.start("alice", op)
    assert_code(
        "invalid_record",
        lambda: store.finish("alice", op, execution_status="failed", error=payload),
    )
    assert store.status("alice", op)["execution_status"] == "uploading"
    assert all(
        b"TOP-SECRET" not in path.read_bytes()
        for path in tmp_path.glob("publish.sqlite*")
    )


def test_extra_request_attributes_are_not_serialized(tmp_path, store, prepared):
    class ExtendedRequest(PublishRequest):
        access_token = "TOP-SECRET"

    extended = ExtendedRequest(
        prepared.identity, prepared.artifact_path, prepared.artifact_sha256
    )
    draft = save(store, extended)
    assert type(store.get_draft("alice", draft, now=101)) is PublishRequest
    assert all(
        b"TOP-SECRET" not in path.read_bytes()
        for path in tmp_path.glob("publish.sqlite*")
    )


@pytest.mark.parametrize(
    "code",
    [
        "artifact_unavailable",
        "invalid_artifact",
        "artifact_read_failed",
        "unsupported_response_encoding",
        "upload_outcome_unknown",
        "hub_rejected",
        "hub_request_failed",
    ],
)
def test_upload_client_safe_failures_can_be_recorded(store, prepared, code):
    draft = save(store, prepared)
    op, _ = store.commit_draft("alice", draft, "one", now=101)
    store.start("alice", op)
    store.finish(
        "alice",
        op,
        execution_status="unknown",
        error={"code": code, "outcome_unknown": True},
    )
    assert store.status("alice", op)["error"]["code"] == code


@pytest.mark.parametrize(
    "change",
    [
        {"access_token": "TOP-SECRET"},
        {"kind": "mcp"},
        {"visibility": "public"},
        {"publish_result": {"token": "TOP-SECRET"}},
        {"deduplicated": "TOP-SECRET"},
    ],
)
def test_invalid_results_do_not_change_status_or_persist(
    tmp_path, store, prepared, change
):
    draft = save(store, prepared)
    op, _ = store.commit_draft("alice", draft, "one", now=101)
    store.start("alice", op)
    result = dict(
        asset_id="remote-id",
        kind="plugin",
        package_name="sales",
        version="1.0.0",
        publish_result="pending_moderation",
        visibility="private",
        deduplicated=False,
    )
    result.update(change)
    assert_code(
        "invalid_record",
        lambda: store.finish("alice", op, execution_status="completed", result=result),
    )
    assert store.status("alice", op)["execution_status"] == "uploading"
    assert all(
        b"TOP-SECRET" not in path.read_bytes()
        for path in tmp_path.glob("publish.sqlite*")
    )


def test_target_id_conflicts_even_if_package_name_changes(store, prepared):
    first = save(
        store,
        replace(
            prepared, identity=PublishIdentity("plugin", "sales", "1.0.0", "remote-id")
        ),
    )
    store.commit_draft("alice", first, "one", now=101)
    second = save(
        store,
        replace(
            prepared, identity=PublishIdentity("plugin", "other", "1.0.0", "remote-id")
        ),
    )
    assert_code(
        "target_conflict", lambda: store.commit_draft("alice", second, "two", now=102)
    )


def test_operation_request_survives_draft_expiry_and_checks_owner(store, prepared):
    draft = save(store, prepared)
    op, _ = store.commit_draft("alice", draft, "one", now=199)
    assert_code("draft_expired", lambda: store.get_draft("alice", draft, now=201))
    assert store.operation_request("alice", op) == prepared
    assert_code("not_found", lambda: store.operation_request("bob", op))
    assert_code("not_found", lambda: store.operation_request("alice", "missing"))


def test_future_publish_result_is_preserved(store, prepared):
    draft = save(store, prepared)
    op, _ = store.commit_draft("alice", draft, "one", now=101)
    store.start("alice", op)
    result = dict(
        asset_id="remote-id",
        kind="plugin",
        package_name="sales",
        version="1.0.0",
        publish_result="future_status",
        visibility="private",
        deduplicated=False,
    )
    store.finish("alice", op, execution_status="completed", result=result)
    assert store.status("alice", op)["result"]["publish_result"] == "future_status"
