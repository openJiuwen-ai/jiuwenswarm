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
    publish_calls: list[str] = []
    monkeypatch.setattr(
        security_lists_rpc, "_trigger_sandbox_sync", lambda: sync_calls.append("sync")
    )
    # P3 是进程级状态；测试里不能真发布（会污染其它用例），只记录调用
    monkeypatch.setattr(
        security_lists_rpc, "_publish_enforcement", lambda: publish_calls.append("publish")
    )
    return {
        "cfg": cfg_path,
        "sync_calls": sync_calls,
        "publish_calls": publish_calls,
        "tmp": tmp_path,
    }


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
        "default": {"*": {"action": "allow", "source": "user_approval",
                          "origin": "approval", "scope": "global"}}
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
    assert row["read"] == {"action": "allow", "source": "user_approval",
                           "origin": "file_guard", "scope": "global"}
    assert row["write"] == {"action": "allow", "source": "user_approval",
                            "origin": "file_guard", "scope": "global"}


def test_get_card_origin_distinguishes_file_guard_from_approval(rpc_env):
    """`file_guard.paths` 的投影 `source` 也是 user_approval，但**不是**审批记住。

    只靠 source 选徽标会把「文件安全护栏」里配的路径规则显示成"审批记住"。
    origin 让前端能正确取文案；source 不动（它决定优先级层）。
    """
    _write_permissions(rpc_env, {
        "approval_overrides": [
            {"id": "ap1", "match_type": "command", "pattern": "git *", "action": "allow"},
        ],
        "file_guard": {"paths": [{"path": "D:/secrets", "read": "allow"}]},
    })

    cards = call(ReqMethod.SECURITY_LISTS_GET).payload["records"]

    approval_card = next(c for c in cards if c["pattern"] == "git *")
    fg_card = next(c for c in cards if c["pattern"] == "D:/secrets")
    assert approval_card["origin"] == "approval"
    assert fg_card["origin"] == "file_guard"
    assert approval_card["source"] == fg_card["source"] == "user_approval"
    assert fg_card["cells"]["*"]["read"]["origin"] == "file_guard"

    # 物理记录没有 origin（source 已自证）
    call(ReqMethod.SECURITY_LISTS_UPSERT, {"record": rec_dict()})
    user_card = next(c for c in call(ReqMethod.SECURITY_LISTS_GET).payload["records"]
                     if c["pattern"] == "C:/data")
    assert user_card["origin"] == ""


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


def test_write_republishes_host_exit_policy(rpc_env):
    """名单写后必须重发 P3：否则新加的域名 deny 只在新会话组建 rail 时才进 net_guard，
    "热更新生效"就成半截（S2）。"""
    resp = call(ReqMethod.SECURITY_LISTS_UPSERT, {"record": rec_dict(
        type="domain", pattern="evil.example", match="exact", cells={"*": {"*": "deny"}},
    )})
    assert resp.ok
    assert rpc_env["publish_calls"] == ["publish"]

    rpc_env["publish_calls"].clear()
    call(ReqMethod.SECURITY_LISTS_GET)
    assert rpc_env["publish_calls"] == []      # 读方法不触发


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


# ---------------------------------------------------------------------------
# v3 兜底档 defaults：get / set / 随卡片视图下发 / 云端下发
# ---------------------------------------------------------------------------


def test_defaults_get_then_set_roundtrip(rpc_env):
    assert call(ReqMethod.SECURITY_LISTS_DEFAULTS_GET).payload["defaults"] == {}

    resp = call(
        ReqMethod.SECURITY_LISTS_DEFAULTS_SET,
        {"defaults": {"*": {"domain": "deny"}}},
    )

    assert resp.ok
    assert resp.payload["defaults"] == {"*": {"domain": "deny"}}
    assert call(ReqMethod.SECURITY_LISTS_DEFAULTS_GET).payload["defaults"] == {
        "*": {"domain": "deny"}
    }
    assert rpc_env["sync_calls"] == ["sync"]   # 写后触发双端同步


def test_defaults_set_rejects_illegal_keyspace(rpc_env):
    resp = call(
        ReqMethod.SECURITY_LISTS_DEFAULTS_SET,
        {"defaults": {"*": {"domain": "block"}}},
    )
    assert not resp.ok
    assert resp.payload["code"] == "BAD_REQUEST"
    assert call(ReqMethod.SECURITY_LISTS_DEFAULTS_GET).payload["defaults"] == {}


def test_defaults_set_requires_object(rpc_env):
    resp = call(ReqMethod.SECURITY_LISTS_DEFAULTS_SET, {"defaults": "deny"})
    assert not resp.ok and resp.payload["code"] == "BAD_REQUEST"


def test_card_view_carries_defaults(rpc_env):
    """前端一次 get 就能拿到当前兜底档（免二次请求）。"""
    call(ReqMethod.SECURITY_LISTS_DEFAULTS_SET, {"defaults": {"*": {"domain": "deny"}}})
    payload = call(ReqMethod.SECURITY_LISTS_GET).payload
    assert payload["defaults"] == {"*": {"domain": "deny"}}


def test_cloud_sync_can_carry_defaults(rpc_env):
    resp = call(
        ReqMethod.SECURITY_LISTS_CLOUD_SYNC,
        {
            "records": [],
            "sync_version": "v1",
            "synced_at": "2026-09-30T00:00:00+00:00",
            "defaults": {"*": {"domain": "deny"}},
        },
    )
    assert resp.ok
    assert call(ReqMethod.SECURITY_LISTS_DEFAULTS_GET).payload["defaults"] == {
        "*": {"domain": "deny"}
    }


# ---------------------------------------------------------------------------
# type="tool"（工具级并入，设计 §10.3）
# ---------------------------------------------------------------------------


def test_upsert_and_get_tool_record(rpc_env):
    resp = call(ReqMethod.SECURITY_LISTS_UPSERT, {"record": rec_dict(
        type="tool", pattern="bash", match="exact", cells={"*": {"*": "ask"}},
    )})
    assert resp.ok and resp.payload["record"]["type"] == "tool"

    payload = call(ReqMethod.SECURITY_LISTS_GET, {"type": "tool"}).payload
    assert [c["pattern"] for c in payload["records"]] == ["bash"]


def test_upsert_tool_rejects_non_exact_match(rpc_env):
    resp = call(ReqMethod.SECURITY_LISTS_UPSERT, {"record": rec_dict(
        type="tool", pattern="bash", match="glob", cells={"*": {"*": "ask"}},
    )})
    assert not resp.ok and resp.payload["code"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# migrate：legacy 段一次性搬进 user 区
# ---------------------------------------------------------------------------


def _write_legacy(rpc_env):
    import yaml

    cfg = rpc_env["cfg"]
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["permissions"] = {
        "net_guard": {"enabled": True, "urls": {"evil.example": "deny"}},
        "file_guard": {"enabled": True, "paths": [{"path": "C:/data", "read": "allow"}]},
    }
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_migrate_dry_run_then_apply(rpc_env):
    _write_legacy(rpc_env)

    preview = call(ReqMethod.SECURITY_LISTS_MIGRATE, {"dry_run": True}).payload
    assert preview["candidates"] == 2 and preview["created"] == 0
    assert store.get_security_lists()["user"] == []   # 预览不落盘
    assert rpc_env["sync_calls"] == []                # 预览不触发双端同步

    applied = call(ReqMethod.SECURITY_LISTS_MIGRATE)
    assert applied.ok and applied.payload["created"] == 2
    assert {r["type"] for r in applied.payload["records"]} == {"domain", "file_path"}
    assert rpc_env["sync_calls"] == ["sync"]
    # 幂等：再搬为 0（标记已盖章）
    assert call(ReqMethod.SECURITY_LISTS_MIGRATE).payload["created"] == 0


def test_migrate_rejects_unknown_source(rpc_env):
    resp = call(ReqMethod.SECURITY_LISTS_MIGRATE, {"sources": ["approval_overrides"]})
    assert not resp.ok and resp.payload["code"] == "BAD_REQUEST"


def test_migrate_rejects_malformed_sources(rpc_env):
    resp = call(ReqMethod.SECURITY_LISTS_MIGRATE, {"sources": "net_guard"})
    assert not resp.ok and resp.payload["code"] == "BAD_REQUEST"
