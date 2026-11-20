# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Session default equipment: config parsing, restore backfill, RPC handlers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest


def _req(params: dict[str, Any]) -> AgentRequest:
    return AgentRequest(
        request_id="r1",
        channel_id="web",
        session_id="s1",
        params=params,
    )


def _ctx(params: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        request=_req(params),
        sink=SimpleNamespace(send_response=AsyncMock()),
    )


async def _resp_payload(ctx: SimpleNamespace) -> dict[str, Any]:
    resp = ctx.sink.send_response.await_args.args[0]
    return resp.ok, resp.payload


class TestSessionEquipmentConfig:
    """config.yaml ``session.equipment`` parsing (common.config)."""

    def test_missing_block_defaults(self, monkeypatch) -> None:
        from jiuwenswarm.common import config as cfg_mod

        monkeypatch.setattr(cfg_mod, "get_config_raw", lambda: {})
        assert cfg_mod.get_session_equipment_config() == {"inherit": True, "mcp": []}

    def test_preset_and_inherit_false(self, monkeypatch) -> None:
        from jiuwenswarm.common import config as cfg_mod

        monkeypatch.setattr(
            cfg_mod,
            "get_config_raw",
            lambda: {
                "session": {
                    "equipment": {
                        "inherit": False,
                        "defaults": {"mcp": ["a", " a ", "", "b", "a"]},
                    }
                }
            },
        )
        assert cfg_mod.get_session_equipment_config() == {
            "inherit": False,
            "mcp": ["a", "b"],
        }

    def test_default_names_merge_preset_and_state(self, monkeypatch) -> None:
        from jiuwenswarm.common import config as cfg_mod

        monkeypatch.setattr(
            cfg_mod,
            "get_config_raw",
            lambda: {"session": {"equipment": {"defaults": {"mcp": ["preset-x"]}}}},
        )
        fake_state = SimpleNamespace(list_default_mcps=lambda: ["preset-x", "state-y"])
        monkeypatch.setattr(
            "jiuwenswarm.common.external_providers.provider",
            lambda domain: fake_state,
        )
        assert cfg_mod.get_default_equipment_mcp_names() == ["preset-x", "state-y"]

    def test_default_names_inherit_false_short_circuits(self, monkeypatch) -> None:
        from jiuwenswarm.common import config as cfg_mod

        monkeypatch.setattr(
            cfg_mod,
            "get_config_raw",
            lambda: {"session": {"equipment": {"inherit": False, "defaults": {"mcp": ["a"]}}}},
        )
        fake_state = SimpleNamespace(list_default_mcps=lambda: ["b"])
        monkeypatch.setattr(
            "jiuwenswarm.common.external_providers.provider",
            lambda domain: fake_state,
        )
        assert cfg_mod.get_default_equipment_mcp_names() == []

    def test_default_names_tolerate_state_failure(self, monkeypatch) -> None:
        from jiuwenswarm.common import config as cfg_mod

        monkeypatch.setattr(
            cfg_mod,
            "get_config_raw",
            lambda: {"session": {"equipment": {"defaults": {"mcp": ["preset-x"]}}}},
        )

        def _boom(domain):
            raise RuntimeError("state down")

        monkeypatch.setattr("jiuwenswarm.common.external_providers.provider", _boom)
        assert cfg_mod.get_default_equipment_mcp_names() == ["preset-x"]


class TestRestoreChatSendEquipmentDefaults:
    """Third-level backfill in restore_chat_send_equipment_params."""

    @pytest.fixture(autouse=True)
    def _patch_sources(self, monkeypatch):
        from jiuwenswarm.common import config as cfg_mod
        from jiuwenswarm.server.runtime.session import session_metadata

        self.saved: dict[str, Any] = {}
        self.defaults: list[str] = []
        monkeypatch.setattr(
            session_metadata, "get_session_equipment", lambda sid: self.saved
        )
        monkeypatch.setattr(
            cfg_mod, "get_default_equipment_mcp_names", lambda: list(self.defaults)
        )

    def _restore(self, params: dict[str, Any], session_id: str = "s1") -> dict[str, Any]:
        from jiuwenswarm.server.runtime.agent_adapter import interface

        return interface.restore_chat_send_equipment_params(session_id, params)

    def test_fresh_session_inherits_default_mcp(self) -> None:
        self.saved = {}
        self.defaults = ["worklog-rag", "gitcode"]
        params = self._restore({"query": "hi"})
        assert params["mcp"] == ["worklog-rag", "gitcode"]

    def test_explicit_empty_mcp_is_not_backfilled(self) -> None:
        self.saved = {}
        self.defaults = ["worklog-rag"]
        params = self._restore({"query": "hi", "mcp": []})
        assert params["mcp"] == []

    def test_explicit_mcp_wins_over_default(self) -> None:
        self.saved = {}
        self.defaults = ["worklog-rag"]
        params = self._restore({"query": "hi", "mcp": ["custom"]})
        assert params["mcp"] == ["custom"]

    def test_existing_snapshot_wins_over_default(self) -> None:
        self.saved = {"agent_template_name": "", "plugin_names": [], "mcp": ["a"]}
        self.defaults = ["worklog-rag"]
        params = self._restore({"query": "hi"})
        assert params["mcp"] == ["a"]

    def test_empty_snapshot_is_escape_hatch(self) -> None:
        """A persisted empty snapshot means the user cleared explicitly."""
        self.saved = {"agent_template_name": "", "plugin_names": [], "mcp": []}
        self.defaults = ["worklog-rag"]
        params = self._restore({"query": "hi"})
        assert params["mcp"] == []

    def test_no_default_leaves_params_untouched(self) -> None:
        self.saved = {}
        self.defaults = []
        params = self._restore({"query": "hi"})
        assert "mcp" not in params

    def test_default_does_not_seed_other_equipment_fields(self) -> None:
        self.saved = {}
        self.defaults = ["worklog-rag"]
        params = self._restore({"query": "hi"})
        assert "plugin_names" not in params
        assert "agent_template_name" not in params

    @pytest.mark.parametrize("mode", ["team", "team.plan", "auto_harness"])
    def test_skipped_modes_ignore_default(self, mode: str) -> None:
        self.saved = {}
        self.defaults = ["worklog-rag"]
        params = self._restore({"query": "hi", "mode": mode})
        assert "mcp" not in params

    def test_no_session_id_returns_untouched(self) -> None:
        self.defaults = ["worklog-rag"]
        params = self._restore({"query": "hi"}, session_id="")
        assert "mcp" not in params


class TestSessionEquipmentDefaultsHandlers:
    """session_equipment.defaults.get/set RPC handlers."""

    async def test_get_returns_merged_view(self, monkeypatch) -> None:
        from jiuwenswarm.common import config as cfg_mod
        from jiuwenswarm.server.handlers import session as session_handlers
        from jiuwenswarm.server.runtime.mcp import state_store as ss

        monkeypatch.setattr(
            cfg_mod, "get_session_equipment_config",
            lambda: {"inherit": True, "mcp": ["preset-x"]},
        )
        monkeypatch.setattr(
            cfg_mod, "get_default_equipment_mcp_names", lambda: ["preset-x", "live-y"]
        )
        monkeypatch.setattr(ss, "list_default_flagged_mcps", lambda: ["dead-z", "live-y"])
        monkeypatch.setattr(ss, "list_default_mcps", lambda: ["live-y"])

        ctx = _ctx({})
        await session_handlers.handle_session_equipment_defaults_get(ctx)

        ok, payload = await _resp_payload(ctx)
        assert ok is True
        assert payload["type"] == "session_equipment_defaults"
        assert payload["mcp"] == ["preset-x", "live-y"]
        assert payload["preset"] == ["preset-x"]
        assert payload["runtime"] == ["dead-z", "live-y"]
        assert payload["unavailable"] == ["dead-z"]
        assert payload["inherit"] is True
        assert payload["max"] == 10

    async def test_set_replaces_runtime_defaults(self, monkeypatch) -> None:
        from jiuwenswarm.common import config as cfg_mod
        from jiuwenswarm.server.handlers import session as session_handlers
        from jiuwenswarm.server.runtime.mcp import state_store as ss

        replaced: list[list[str]] = []
        monkeypatch.setattr(
            ss, "get_mcp_record", lambda name: {"state": "connected"}
        )
        monkeypatch.setattr(ss, "replace_default_mcps", lambda names: replaced.append(names))
        monkeypatch.setattr(
            cfg_mod, "get_default_equipment_mcp_names", lambda: ["a", "b"]
        )

        ctx = _ctx({"mcp": ["b", " a ", "b"]})
        await session_handlers.handle_session_equipment_defaults_set(ctx)

        ok, payload = await _resp_payload(ctx)
        assert ok is True
        assert replaced == [["b", "a"]]
        assert payload["mcp"] == ["a", "b"]
        assert payload["runtime"] == ["a", "b"]

    async def test_set_rejects_non_list(self, monkeypatch) -> None:
        from jiuwenswarm.server.handlers import session as session_handlers

        ctx = _ctx({"mcp": "not-a-list"})
        await session_handlers.handle_session_equipment_defaults_set(ctx)

        ok, payload = await _resp_payload(ctx)
        assert ok is False
        assert payload["code"] == "BAD_REQUEST"

    async def test_set_rejects_over_limit(self, monkeypatch) -> None:
        from jiuwenswarm.server.handlers import session as session_handlers
        from jiuwenswarm.server.runtime.mcp import state_store as ss

        monkeypatch.setattr(ss, "get_mcp_record", lambda name: {"state": "connected"})
        ctx = _ctx({"mcp": [f"mcp-{i}" for i in range(11)]})
        await session_handlers.handle_session_equipment_defaults_set(ctx)

        ok, payload = await _resp_payload(ctx)
        assert ok is False
        assert payload["code"] == "BAD_REQUEST"
        assert "10" in payload["error"]

    @pytest.mark.parametrize(
        "record,needle",
        [
            (None, "not installed"),
            ({"state": "disconnected"}, "state=disconnected"),
            ({"state": "registered"}, "state=registered"),
        ],
    )
    async def test_set_rejects_not_connected(self, monkeypatch, record, needle) -> None:
        from jiuwenswarm.server.handlers import session as session_handlers
        from jiuwenswarm.server.runtime.mcp import state_store as ss

        monkeypatch.setattr(ss, "get_mcp_record", lambda name: record)
        ctx = _ctx({"mcp": ["dead-mcp"]})
        await session_handlers.handle_session_equipment_defaults_set(ctx)

        ok, payload = await _resp_payload(ctx)
        assert ok is False
        assert payload["code"] == "BAD_REQUEST"
        assert needle in payload["error"]

    async def test_set_accepts_connecting_state(self, monkeypatch) -> None:
        from jiuwenswarm.common import config as cfg_mod
        from jiuwenswarm.server.handlers import session as session_handlers
        from jiuwenswarm.server.runtime.mcp import state_store as ss

        monkeypatch.setattr(
            ss, "get_mcp_record", lambda name: {"state": "connecting"}
        )
        monkeypatch.setattr(ss, "replace_default_mcps", lambda names: None)
        monkeypatch.setattr(cfg_mod, "get_default_equipment_mcp_names", lambda: ["x"])

        ctx = _ctx({"mcp": ["x"]})
        await session_handlers.handle_session_equipment_defaults_set(ctx)

        ok, _payload = await _resp_payload(ctx)
        assert ok is True
