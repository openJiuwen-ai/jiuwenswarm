# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the admin env preset channel (preset_env + apply_managed_preset).

Covers: envelope parsing (fail-open, per-entry skip, transport/command
normalization), the timestamp fence (first apply / bump overwrites with
removals / equal-or-lower ts skips without restoring user deletions /
same-ts content change only warns), user-record shadowing, concurrent
applies, and the TenantAgentPool trigger (once per tenant, errors isolated).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from jiuwenswarm.enterprise.tenant import tenant_context
from jiuwenswarm.enterprise.tenant import tenant_agent_pool as pool_mod
from jiuwenswarm.server.runtime.mcp import preset_env
from jiuwenswarm.server.runtime.mcp import state_store as ss


@pytest.fixture(autouse=True)
def _isolate_tenant_context_and_cache():
    preset_env._reset_preset_cache()
    tenant_context.TenantContext.clear()
    yield
    tenant_context.TenantContext.clear()
    preset_env._reset_preset_cache()


def _set_env(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    monkeypatch.setenv(preset_env.ENV_KEY, json.dumps(payload))
    preset_env._reset_preset_cache()


# --------------------------------------------------------------------------- #
# Envelope parsing
# --------------------------------------------------------------------------- #

def test_parse_valid_remote_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {
        "ts": 1730000000000,
        "servers": {
            "dify-echo": {
                "transport": "streamable-http",
                "url": "http://192.168.1.90:8088/mcp",
                "headers": {"Authorization": "Bearer t"},
                "timeout_s": 600,
            }
        },
    })
    ts, defs = preset_env.preset_mcp_definitions()
    assert ts == 1730000000000
    assert defs == {
        "dify-echo": {
            "transport": "streamable-http",
            "url": "http://192.168.1.90:8088/mcp",
            "headers": {"Authorization": "Bearer t"},
            "timeout_s": 600,
            "server_id_scope": "mcp:dify-echo",
        }
    }


def test_parse_normalizes_http_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {
        "ts": 5,
        "servers": {"a": {"transport": "http", "url": "http://x/mcp"}},
    })
    _, defs = preset_env.preset_mcp_definitions()
    assert defs["a"]["transport"] == "streamable-http"


def test_parse_stdio_splits_full_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {
        "ts": 5,
        "servers": {
            "split": {"transport": "stdio", "command": "npx -y bing-cn-mcp"},
            "explicit": {
                "transport": "stdio",
                "command": "uvx",
                "args": ["mcp-server", "--flag"],
                "env": {"K": "V"},
            },
        },
    })
    _, defs = preset_env.preset_mcp_definitions()
    assert defs["split"]["command"] == "npx"
    assert defs["split"]["args"] == ["-y", "bing-cn-mcp"]
    assert defs["explicit"]["command"] == "uvx"
    assert defs["explicit"]["args"] == ["mcp-server", "--flag"]
    assert defs["explicit"]["env"] == {"K": "V"}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        json.dumps({"servers": {"a": {"transport": "stdio", "command": "x"}}}),
        json.dumps({"ts": "123", "servers": {}}),
        json.dumps({"ts": True, "servers": {}}),
        json.dumps({"ts": 0, "servers": {}}),
        json.dumps({"ts": 5, "servers": ["a"]}),
    ],
)
def test_parse_failopen_on_bad_envelope(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(preset_env.ENV_KEY, raw)
    preset_env._reset_preset_cache()
    assert preset_env.preset_mcp_definitions() == (0, {})


def test_parse_skips_invalid_entry_keeps_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {
        "ts": 5,
        "servers": {
            "good": {"transport": "streamable-http", "url": "http://x/mcp"},
            "no-url": {"transport": "streamable-http"},
            "no-command": {"transport": "stdio"},
            "bad-transport": {"transport": "grpc", "url": "http://x"},
        },
    })
    _, defs = preset_env.preset_mcp_definitions()
    assert list(defs) == ["good"]


def test_parse_cache_reuses_until_raw_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {"ts": 5, "servers": {"a": {"transport": "stdio", "command": "x"}}})
    _, first = preset_env.preset_mcp_definitions()
    _, second = preset_env.preset_mcp_definitions()
    assert first is second
    _set_env(monkeypatch, {"ts": 6, "servers": {"a": {"transport": "stdio", "command": "y"}}})
    _, third = preset_env.preset_mcp_definitions()
    assert third is not first


# --------------------------------------------------------------------------- #
# Timestamp fence (apply_managed_preset)
# --------------------------------------------------------------------------- #

def _apply(tmp_path: Path, ts: int, defs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    with patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        return ss.apply_managed_preset(ts, defs)


def _read_state(tmp_path: Path) -> dict[str, Any]:
    with patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        return ss.read_mcp_state()


REMOTE = {
    "transport": "streamable-http",
    "url": "http://192.168.1.90:8088/mcp",
    "server_id_scope": "mcp:dify-echo",
}


def test_first_apply_seeds_managed_records(tmp_path: Path) -> None:
    result = _apply(tmp_path, 1000, {"dify-echo": dict(REMOTE)})
    assert result["applied"] is True
    state = _read_state(tmp_path)
    rec = state["mcp"]["dify-echo"]
    assert rec["state"] == "connected"
    assert rec["enabled"] is False
    assert rec["managed"] == "env"
    assert rec["integration_type"] == "remote-mcp"
    assert rec["url"] == REMOTE["url"]
    assert state["preset"]["ts"] == 1000
    assert isinstance(state["preset"]["hash"], str)


def test_bumped_ts_overwrites_and_removes(tmp_path: Path) -> None:
    older = dict(REMOTE, headers={"Authorization": "Bearer old"})
    _apply(tmp_path, 1000, {"dify-echo": older, "withdrawn": dict(REMOTE)})
    with patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        ss.upsert_mcp_record(
            "mine",
            {"transport": "streamable-http", "url": "http://user/mcp",
             "server_id_scope": "mcp:mine"},
            state="registered",
        )
    result = _apply(tmp_path, 2000, {
        "dify-echo": dict(REMOTE),  # headers dropped from the new payload
    })
    assert result["applied"] is True
    assert result["removed"] == ["withdrawn"]
    state = _read_state(tmp_path)
    assert set(state["mcp"]) == {"dify-echo", "mine"}
    # Remove-then-insert: a field dropped from the new payload disappears.
    assert "headers" not in state["mcp"]["dify-echo"]
    assert state["mcp"]["dify-echo"]["state"] == "connected"
    # The user-created record is not part of the managed set — untouched.
    assert state["mcp"]["mine"]["state"] == "registered"
    assert state["preset"]["ts"] == 2000


def test_equal_ts_skips_and_keeps_user_deletion(tmp_path: Path) -> None:
    _apply(tmp_path, 1000, {"dify-echo": dict(REMOTE)})
    with patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        ss.remove_mcp_record("dify-echo")  # user deletes the preset record
    result = _apply(tmp_path, 1000, {"dify-echo": dict(REMOTE)})
    assert result["applied"] is False
    state = _read_state(tmp_path)
    assert state["mcp"] == {}  # deletion respected until the next push
    assert state["preset"]["ts"] == 1000


def test_lower_ts_skips(tmp_path: Path) -> None:
    _apply(tmp_path, 2000, {"dify-echo": dict(REMOTE)})
    result = _apply(tmp_path, 1000, {"other": dict(REMOTE)})
    assert result["applied"] is False
    state = _read_state(tmp_path)
    assert set(state["mcp"]) == {"dify-echo"}


def test_same_ts_content_change_warns_without_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same pattern as test_host_oauth_flow.capture_oauth_logs: swap the
    # module logger's handlers for caplog's (the repo logger tree doesn't
    # propagate to the root handler caplog listens on).
    target = ss.logger
    monkeypatch.setattr(target, "handlers", [caplog.handler])
    caplog.set_level("WARNING", logger=target.name)
    _apply(tmp_path, 1000, {"dify-echo": dict(REMOTE)})
    changed = dict(REMOTE, url="http://changed/mcp")
    result = _apply(tmp_path, 1000, {"dify-echo": changed})
    assert result["applied"] is False
    assert any("without a ts bump" in rec.message for rec in caplog.records)
    assert _read_state(tmp_path)["mcp"]["dify-echo"]["url"] == REMOTE["url"]


def test_user_created_record_wins_over_preset(tmp_path: Path) -> None:
    with patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        ss.upsert_mcp_record(
            "dify-echo",
            {"transport": "streamable-http", "url": "http://user/mcp",
             "server_id_scope": "mcp:dify-echo"},
            state="registered",
        )
    result = _apply(tmp_path, 1000, {"dify-echo": dict(REMOTE)})
    assert result["applied"] is True
    assert result["skipped_user"] == ["dify-echo"]
    rec = _read_state(tmp_path)["mcp"]["dify-echo"]
    assert rec["url"] == "http://user/mcp"
    assert rec["state"] == "registered"
    assert "managed" not in rec


def test_concurrent_applies_end_consistent(tmp_path: Path) -> None:
    """Two pod generations racing on the shared state.json: whichever ts is
    higher ends up owning the file whole — no mixed generation state.

    The workspace patch is installed once around the whole section —
    mock.patch mutates a module attribute globally, so per-thread patches
    would rip each other out mid-flight.
    """
    errors: list[Exception] = []

    def _worker(ts: int, defs: dict[str, dict[str, Any]]) -> None:
        try:
            ss.apply_managed_preset(ts, defs)
        except Exception as exc:  # noqa: BLE001 — recorded and asserted below
            errors.append(exc)

    with patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        threads = [
            threading.Thread(target=_worker, args=(1000, {"a": dict(REMOTE)})),
            threading.Thread(target=_worker, args=(2000, {"b": dict(REMOTE)})),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        state = ss.read_mcp_state()
    assert state["preset"]["ts"] == 2000
    assert set(state["mcp"]) == {"b"}


# --------------------------------------------------------------------------- #
# ensure_managed_mcps (env -> tenant state.json)
# --------------------------------------------------------------------------- #

def test_ensure_noop_without_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(preset_env.ENV_KEY, raising=False)
    with patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        preset_env.ensure_managed_mcps()
    assert not (tmp_path / "mcp" / "state.json").exists()


def test_ensure_skips_without_tenant_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env(monkeypatch, {"ts": 1000, "servers": {"dify-echo": dict(REMOTE)}})
    with patch.object(ss, "get_workspace_dir", return_value=tmp_path):
        preset_env.ensure_managed_mcps()
    assert not (tmp_path / "mcp" / "state.json").exists()


def test_ensure_seeds_explicit_tenant_without_ambient_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression for the HTTP-invoke ordering bug: the pool creates the
    manager BEFORE the request layer binds TenantContext, so the hook must
    bind the tenant itself from its explicit key — the ambient context is
    empty at that moment. Full chain through the real TenantContext.bind /
    tenant_paths resolution, hermetic via a patched workspace base."""
    _set_env(monkeypatch, {"ts": 1000, "servers": {"dify-echo": dict(REMOTE)}})
    import jiuwenswarm.common.utils as common_utils
    import jiuwenswarm.enterprise as enterprise_pkg
    from jiuwenswarm.enterprise.tenant import tenant_paths

    # is_enterprise is module-bound in three places on this chain (utils,
    # tenant_paths, and the package attr tenant_context resolves at call
    # time) — patch each binding or path resolution silently mixes layouts.
    with patch.object(enterprise_pkg, "is_enterprise", return_value=True), \
         patch.object(common_utils, "is_enterprise", return_value=True), \
         patch.object(tenant_paths, "is_enterprise", return_value=True), \
         patch.object(common_utils, "get_user_workspace_dir", return_value=tmp_path):
        preset_env.ensure_managed_mcps("t1")
    state_file = (
        tmp_path / "workspace_t1" / "agent" / "jiuwenclaw_workspace"
        / "mcp" / "state.json"
    )
    assert state_file.is_file()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["mcp"]["dify-echo"]["managed"] == "env"
    assert state["mcp"]["dify-echo"]["state"] == "connected"
    assert state["preset"]["ts"] == 1000


# --------------------------------------------------------------------------- #
# TenantAgentPool trigger
# --------------------------------------------------------------------------- #

class _FakeManager:
    pass


@pytest.mark.asyncio
async def test_pool_seeds_preset_once_per_tenant(tmp_path: Path) -> None:
    calls: list[str] = []
    pool = pool_mod.TenantAgentPool()
    with patch.object(pool_mod, "AgentManager", _FakeManager), \
         patch.object(pool_mod, "is_enterprise", return_value=True), \
         patch.object(
             pool_mod, "get_multi_tenant_user_workspace_dir",
             return_value=tmp_path / "workspace_t1",
         ), \
         patch.object(
             pool_mod, "seed_tenant_agent_workspace",
             side_effect=lambda root: calls.append("seed"),
         ), \
         patch(
             "jiuwenswarm.server.runtime.mcp.preset_env.ensure_managed_mcps",
             side_effect=lambda key: calls.append(f"preset:{key}"),
         ):
        first = await pool._ensure_agent_manager("t1")
        second = await pool._ensure_agent_manager("t1")
    assert first is second
    assert calls.count("seed") == 1
    # Seeded exactly once, with the pool's explicit tenant key (the hook
    # must not depend on the ambient context — see the ordering regression).
    assert calls.count("preset:t1") == 1


@pytest.mark.asyncio
async def test_pool_seed_failure_does_not_block_manager_creation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pool_mod.logger, "handlers", [caplog.handler])
    caplog.set_level("WARNING", logger=pool_mod.logger.name)
    pool = pool_mod.TenantAgentPool()
    with patch.object(pool_mod, "AgentManager", _FakeManager), \
         patch.object(pool_mod, "is_enterprise", return_value=True), \
         patch.object(
             pool_mod, "get_multi_tenant_user_workspace_dir",
             return_value=tmp_path / "workspace_t1",
         ), \
         patch.object(pool_mod, "seed_tenant_agent_workspace", lambda root: None), \
         patch(
             "jiuwenswarm.server.runtime.mcp.preset_env.ensure_managed_mcps",
             side_effect=RuntimeError("boom"),
         ):
        manager = await pool._ensure_agent_manager("t1")
    assert isinstance(manager, _FakeManager)
    assert any("preset mcp seed failed" in rec.message for rec in caplog.records)
