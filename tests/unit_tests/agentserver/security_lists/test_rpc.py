# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists_rpc 契约测试：六方法、409/400/404/500、卡片视图合入、写后同步。"""
from __future__ import annotations

import pytest

import jiuwenswarm.common.config as config_mod
from jiuwenswarm.agents.harness.common.rails.security_lists import (
    audit,
    normalize,
    store,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server import security_lists_rpc
from jiuwenswarm.server.security_lists_rpc import dispatch_security_lists_request


@pytest.fixture
def rpc_env(tmp_path, monkeypatch):
    """隔离 config.yaml / 审计文件 / 沙箱同步触发；返回同步调用记录器。"""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("model:\n  name: test\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", cfg_path)
    monkeypatch.setattr(config_mod, "get_config_file", lambda: cfg_path)
    monkeypatch.setattr(audit, "_audit_file", lambda: tmp_path / "security_audit.jsonl")
    monkeypatch.setattr(normalize, "project_builtin", lambda: [])
    sync_calls: list[str] = []
    monkeypatch.setattr(
        security_lists_rpc, "_trigger_sandbox_sync", lambda: sync_calls.append("sync")
    )
    return {"cfg": cfg_path, "sync_calls": sync_calls, "tmp": tmp_path}


def call(method: ReqMethod, params: dict | None = None):
    return dispatch_security_lists_request(
        AgentRequest(request_id="r1", req_method=method, params=params or {})
    )


def rec_dict(**kw):
    base = {
        "type": "file_path",
        "pattern": "C:/data",
        "match": "prefix",
        "cells": {"*": {"read": "allow"}},
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_empty(rpc_env):
    resp = call(ReqMethod.SECURITY_LISTS_GET)
    assert resp.ok
    assert resp.payload["records"] == []
    assert resp.payload["builtin"] == []
    assert resp.payload["mode"] in ("default", "auto_approve", "full_access", "*")
    assert resp.payload["cloud_meta"] == {"sync_version": "", "synced_at": ""}


def test_get_bad_filter_400(rpc_env):
    assert not call(ReqMethod.SECURITY_LISTS_GET, {"type": "oops"}).ok
    assert not call(ReqMethod.SECURITY_LISTS_GET, {"source": "builtin"}).ok
    assert not call(ReqMethod.SECURITY_LISTS_GET, {"mode": "strict"}).ok


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


def test_upsert_create_and_get_card(rpc_env):
    resp = call(
        ReqMethod.SECURITY_LISTS_UPSERT,
        {"record": rec_dict(source="cloud")},  # source 强制改写为 user（防伪造）
    )
    assert resp.ok
    stored = resp.payload["record"]
    assert stored["id"].startswith("ul_")
    assert stored["source"] == "user"
    assert stored["created_at"] and stored["updated_at"]

    got = call(ReqMethod.SECURITY_LISTS_GET)
    cards = got.payload["records"]
    assert len(cards) == 1
    assert cards[0]["cells"] == {"*": {"read": {"action": "allow", "source": "user"}}}
    assert rpc_env["sync_calls"] == ["sync"]  # 写后触发沙箱双端同步


def test_upsert_conflict_409_with_existing_id(rpc_env):
    first = call(ReqMethod.SECURITY_LISTS_UPSERT, {"record": rec_dict()})
    assert first.ok
    dup = call(ReqMethod.SECURITY_LISTS_UPSERT, {"record": rec_dict(note="x")})
    assert not dup.ok
    assert dup.payload["code"] == "CONFLICT"
    assert dup.payload["existing_id"] == first.payload["record"]["id"]


def test_upsert_validation_400(rpc_env):
    resp = call(ReqMethod.SECURITY_LISTS_UPSERT, {"record": rec_dict(match="exact")})
    assert not resp.ok
    assert resp.payload["code"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# cells.patch
# ---------------------------------------------------------------------------


def _upsert(pattern="C:/data", cells=None, **kw) -> str:
    resp = call(
        ReqMethod.SECURITY_LISTS_UPSERT,
        {"record": rec_dict(pattern=pattern, cells=cells or {"*": {"read": "allow"}}, **kw)},
    )
    assert resp.ok
    return resp.payload["record"]["id"]


def test_cells_patch_set_and_unset(rpc_env):
    rid = _upsert()
    resp = call(
        ReqMethod.SECURITY_LISTS_CELLS_PATCH,
        {"id": rid, "set": {"default.read": "deny", "*.write": "ask"}},
    )
    assert resp.ok
    cells = resp.payload["record"]["cells"]
    assert cells["default"]["read"] == "deny"
    assert cells["*"]["write"] == "ask"

    resp2 = call(ReqMethod.SECURITY_LISTS_CELLS_PATCH, {"id": rid, "unset": ["default.read"]})
    assert resp2.ok
    assert "default" not in resp2.payload["record"]["cells"]


def test_cells_patch_404(rpc_env):
    resp = call(
        ReqMethod.SECURITY_LISTS_CELLS_PATCH,
        {"id": "ul_missing", "set": {"*.read": "deny"}},
    )
    assert not resp.ok
    assert resp.payload["code"] == "NOT_FOUND"


def test_cells_patch_400(rpc_env):
    rid = _upsert()
    assert not call(ReqMethod.SECURITY_LISTS_CELLS_PATCH, {"id": rid, "set": {"*.read": "oops"}}).ok
    assert not call(ReqMethod.SECURITY_LISTS_CELLS_PATCH, {"id": rid, "set": {"read": "deny"}}).ok
    assert not call(ReqMethod.SECURITY_LISTS_CELLS_PATCH, {"id": rid}).ok


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_and_404(rpc_env):
    rid = _upsert()
    resp = call(ReqMethod.SECURITY_LISTS_DELETE, {"id": rid})
    assert resp.ok and resp.payload["ok"]
    assert not call(ReqMethod.SECURITY_LISTS_DELETE, {"id": rid}).ok


# ---------------------------------------------------------------------------
# cloud.sync
# ---------------------------------------------------------------------------


def test_cloud_sync_and_card_view(rpc_env):
    resp = call(
        ReqMethod.SECURITY_LISTS_CLOUD_SYNC,
        {
            "sync_version": "v7",
            "synced_at": "2026-09-28T00:00:00+00:00",
            "records": [
                rec_dict(pattern="D:/payroll", cells={"*": {"read": "deny", "write": "deny"}}),
                rec_dict(type="domain", pattern="*.evil.com", match="wildcard", cells={"*": {"*": "deny"}}),
            ],
        },
    )
    assert resp.ok
    assert resp.payload["applied"] == 2

    got = call(ReqMethod.SECURITY_LISTS_GET, {"source": "cloud"})
    cards = got.payload["records"]
    assert len(cards) == 2
    assert all(c["source"] == "cloud" for c in cards)
    assert got.payload["cloud_meta"]["sync_version"] == "v7"


def test_cloud_sync_batch_rejected_400(rpc_env):
    good = rec_dict(pattern="D:/ok")
    bad = rec_dict(type="domain", pattern="bad*", match="exact")  # exact 域名含通配符
    resp = call(ReqMethod.SECURITY_LISTS_CLOUD_SYNC, {"sync_version": "v8", "records": [good, bad]})
    assert not resp.ok
    assert resp.payload["code"] == "BAD_REQUEST"
    # 整批拒绝：cloud 区未被写入
    assert store.get_security_lists()["cloud"]["records"] == []


# ---------------------------------------------------------------------------
# audit.query
# ---------------------------------------------------------------------------


def test_audit_query_after_writes(rpc_env):
    rid = _upsert()
    call(ReqMethod.SECURITY_LISTS_DELETE, {"id": rid})

    resp = call(ReqMethod.SECURITY_LISTS_AUDIT_QUERY, {"kind": "security.list.change"})
    assert resp.ok
    ops = [e["op"] for e in resp.payload["events"]]
    assert "upsert" in ops and "delete" in ops

    empty = call(ReqMethod.SECURITY_LISTS_AUDIT_QUERY, {"kind": "security.list.fallback"})
    assert empty.ok and empty.payload["events"] == []

    bad_limit = call(ReqMethod.SECURITY_LISTS_AUDIT_QUERY, {"limit": "abc"})
    assert not bad_limit.ok


# ---------------------------------------------------------------------------
# 卡片视图：审批合入
# ---------------------------------------------------------------------------


def _write_permissions(rpc_env, permissions: dict):
    """把 permissions 段写进隔离 config（审批投影数据源）。"""
    import yaml

    data = yaml.safe_load(rpc_env["cfg"].read_text(encoding="utf-8"))
    data["permissions"] = permissions
    rpc_env["cfg"].write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_get_card_approval_merge_and_override_badge(rpc_env):
    _upsert(pattern="curl*", cells={"*": {"*": "ask"}}, type="command", match="glob")
    _write_permissions(
        rpc_env,
        {
            "approval_overrides": [
                {"id": "ap1", "match_type": "command", "pattern": "curl*", "action": "allow"},
                {"id": "ap2", "match_type": "command", "pattern": "wget*", "action": "allow", "mode": "default"},
            ]
        },
    )

    got = call(ReqMethod.SECURITY_LISTS_GET)
    cards = got.payload["records"]
    user_card = next(c for c in cards if c["pattern"] == "curl*")
    # 审批压制同格用户值 → 徽标 + overridden 原值
    cell = user_card["cells"]["*"]["*"]
    assert cell["action"] == "allow"
    assert cell["source"] == "user_approval"
    assert cell["scope"] == "global"
    assert cell["overridden"] == {"action": "ask", "source": "user"}
    # 无对应卡片的审批自成卡片（置顶区，徽标审批记住）
    standalone = next(c for c in cards if c["pattern"] == "wget*")
    assert standalone["source"] == "user_approval"
    assert standalone["cells"] == {
        "default": {"*": {"action": "allow", "source": "user_approval", "scope": "global"}}
    }


def test_get_card_file_guard_merge_multi_axis(rpc_env):
    _upsert(pattern="D:/secrets", cells={"*": {"read": "deny"}})
    _write_permissions(
        rpc_env,
        {"file_guard": {"paths": [
            {"id": "fg1", "path": "D:/secrets", "match": "prefix",
             "read": "allow", "write": "allow", "mode": "auto_approve"},
        ]}},
    )

    got = call(ReqMethod.SECURITY_LISTS_GET)
    card = next(c for c in got.payload["records"] if c["pattern"] == "D:/secrets")
    assert card["cells"]["*"]["read"] == {"action": "deny", "source": "user"}  # 用户格不受影响
    row = card["cells"]["auto_approve"]
    assert row["read"] == {"action": "allow", "source": "user_approval", "scope": "global"}
    assert row["write"] == {"action": "allow", "source": "user_approval", "scope": "global"}


def test_get_card_session_scope(rpc_env, monkeypatch):
    session_perms = {
        "approval_overrides": [
            {"id": "ap_sess", "match_type": "command", "pattern": "scp*", "action": "allow"},
        ]
    }
    monkeypatch.setattr(
        normalize,
        "get_permissions_with_session_overlay",
        lambda base=None, *, session_id=None: session_perms,
    )

    without_session = call(ReqMethod.SECURITY_LISTS_GET)
    assert all(c["pattern"] != "scp*" for c in without_session.payload["records"])

    with_session = call(ReqMethod.SECURITY_LISTS_GET, {"session_id": "s1"})
    card = next(c for c in with_session.payload["records"] if c["pattern"] == "scp*")
    assert card["scope"] == "session"
    assert card["cells"]["*"]["*"]["scope"] == "session"


# ---------------------------------------------------------------------------
# 写后同步触发面
# ---------------------------------------------------------------------------


def test_read_methods_do_not_trigger_sync(rpc_env):
    call(ReqMethod.SECURITY_LISTS_GET)
    call(ReqMethod.SECURITY_LISTS_AUDIT_QUERY)
    assert rpc_env["sync_calls"] == []


def test_all_write_methods_trigger_sync(rpc_env):
    rid = _upsert()
    call(ReqMethod.SECURITY_LISTS_CELLS_PATCH, {"id": rid, "set": {"*.write": "deny"}})
    call(ReqMethod.SECURITY_LISTS_CLOUD_SYNC, {"sync_version": "v1", "records": []})
    call(ReqMethod.SECURITY_LISTS_DELETE, {"id": rid})
    assert len(rpc_env["sync_calls"]) == 4


# ---------------------------------------------------------------------------
# 损坏 fail-closed → 500
# ---------------------------------------------------------------------------


def test_corrupted_section_500(rpc_env):
    rpc_env["cfg"].write_text("security_lists: oops\n", encoding="utf-8")
    for method, params in (
        (ReqMethod.SECURITY_LISTS_GET, {}),
        (ReqMethod.SECURITY_LISTS_UPSERT, {"record": rec_dict()}),
        (ReqMethod.SECURITY_LISTS_DELETE, {"id": "ul_x"}),
    ):
        resp = call(method, params)
        assert not resp.ok
        assert resp.payload["code"] == "INTERNAL_ERROR"
