# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwenswarm CLI chat module."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.channels.cli.chat import (
    MODE_ALIASES,
    VALID_MODES,
    _build_default_gateway_url,
    _build_request,
    _generate_session_id,
    _get_persisted_external_dirs,
    _load_state,
    _normalize_dir,
    _remove_dir_from_config,
    _save_state,
    _validate_args,
    build_parser,
    resolve_mode,
)
from jiuwenswarm.channels.cli.gateway_client import GatewayClient
from jiuwenswarm.channels.cli.render import HumanRenderer


class TestResolveMode:
    @staticmethod
    def test_canonical_values_pass_through():
        assert resolve_mode("code.normal") == "code.normal"
        assert resolve_mode("agent") == "agent"
        assert resolve_mode("code.plan") == "code.plan"
        assert resolve_mode("code.team") == "code.team"
        assert resolve_mode("team") == "team"
        assert resolve_mode("team.plan.normal") == "team.plan.normal"
        assert resolve_mode("team.plan.code") == "team.plan.code"

    @staticmethod
    def test_alias_resolution():
        assert resolve_mode("plan") == "agent"
        assert resolve_mode("fast") == "agent"
        assert resolve_mode("agent.plan") == "agent"
        assert resolve_mode("agent.fast") == "agent"
        assert resolve_mode("code") == "code.normal"
        assert resolve_mode("team.plan") == "team.plan.normal"

    @staticmethod
    def test_case_insensitive():
        assert resolve_mode("AGENT") == "agent"
        assert resolve_mode("Code.Normal") == "code.normal"
        assert resolve_mode("  agent.fast  ") == "agent"

    @staticmethod
    def test_invalid_mode_raises():
        with pytest.raises(ValueError, match="invalid mode"):
            resolve_mode("garbage")

    @staticmethod
    def test_empty_string_raises():
        with pytest.raises(ValueError, match="invalid mode"):
            resolve_mode("")

    @staticmethod
    def test_alias_set_is_complete():
        for alias, canonical in MODE_ALIASES.items():
            assert canonical in VALID_MODES


class TestGatewayUrl:
    @staticmethod
    def test_default_with_env_port(monkeypatch):
        monkeypatch.setenv("GATEWAY_PORT", "20000")
        monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
        assert _build_default_gateway_url() == "ws://127.0.0.1:20000/tui"

    @staticmethod
    def test_default_without_env(monkeypatch):
        monkeypatch.delenv("GATEWAY_PORT", raising=False)
        monkeypatch.delenv("GATEWAY_HOST", raising=False)
        assert _build_default_gateway_url() == "ws://127.0.0.1:19001/tui"

    @staticmethod
    def test_custom_path():
        url = _build_default_gateway_url(path="/tui")
        assert url.endswith("/tui")


class TestSessionId:
    @staticmethod
    def test_generated_id_format():
        sid = _generate_session_id()
        assert sid.startswith("cli-")
        parts = sid[4:].split("-")
        assert len(parts) == 3
        assert len(parts[0]) == 8
        assert len(parts[1]) == 6
        assert len(parts[2]) == 8

    @staticmethod
    def test_generated_ids_are_unique():
        ids = {_generate_session_id() for _ in range(20)}
        assert len(ids) == 20


class TestBuildRequest:
    @staticmethod
    def test_minimal_request(monkeypatch):
        monkeypatch.setattr(os, "getcwd", lambda: "/tmp/test_cwd")
        args = argparse.Namespace(
            mode="code.normal",
            session=None,
            cwd=None,
            project_dir=None,
            trusted_dir=None,
        )
        req = _build_request(args, "hello world")

        assert req["type"] == "req"
        assert req["method"] == "chat.send"
        assert req["is_stream"] is True
        assert req["params"]["content"] == "hello world"
        assert req["params"]["query"] == "hello world"
        assert req["params"]["mode"] == "code.normal"
        assert req["params"]["cwd"].endswith("test_cwd")
        assert req["params"]["project_dir"].endswith("test_cwd")
        assert req["params"]["session_id"].startswith("cli-")
        assert req["params"]["supports_user_interaction"] is False

    @staticmethod
    def test_repl_request_supports_user_interaction(monkeypatch):
        monkeypatch.setattr(os, "getcwd", lambda: "/tmp/test_cwd")
        args = argparse.Namespace(
            mode="code.normal",
            session=None,
            cwd=None,
            project_dir=None,
            trusted_dir=None,
        )

        req = _build_request(args, "hello", supports_user_interaction=True)

        assert req["params"]["supports_user_interaction"] is True

    @staticmethod
    def test_with_session(monkeypatch):
        monkeypatch.setattr(os, "getcwd", lambda: "/home/test")
        args = argparse.Namespace(
            mode="code.normal",
            session="my-session",
            cwd=None,
            project_dir=None,
            trusted_dir=None,
        )
        req = _build_request(args, "test")
        assert req["params"]["session_id"] == "my-session"

    @staticmethod
    def test_with_cwd_override(monkeypatch):
        monkeypatch.setattr(os, "getcwd", lambda: "/cwd")
        args = argparse.Namespace(
            mode="code.normal",
            session=None,
            cwd="/custom/cwd",
            project_dir=None,
            trusted_dir=None,
        )
        req = _build_request(args, "test")
        assert req["params"]["cwd"] == str(Path("/custom/cwd").resolve())

    @staticmethod
    def test_with_project_dir(monkeypatch):
        monkeypatch.setattr(os, "getcwd", lambda: "/cwd")
        args = argparse.Namespace(
            mode="code.normal",
            session=None,
            cwd=None,
            project_dir="/custom/project",
            trusted_dir=None,
        )
        req = _build_request(args, "test")
        assert req["params"]["project_dir"] == str(Path("/custom/project").resolve())

    @staticmethod
    def test_with_trusted_dirs(monkeypatch):
        monkeypatch.setattr(os, "getcwd", lambda: "/cwd")
        monkeypatch.setattr(
            "jiuwenswarm.channels.cli.chat._get_persisted_external_dirs",
            lambda: [],
        )
        args = argparse.Namespace(
            mode="code.normal",
            session=None,
            cwd=None,
            project_dir=None,
            trusted_dir=["/dir1", "/dir2"],
        )
        req = _build_request(args, "test")
        assert sorted(str(d) for d in req["params"]["trusted_dirs"]) == sorted(
            str(Path(path).resolve()) for path in ("/dir1", "/dir2")
        )

    @staticmethod
    def test_mode_included(monkeypatch):
        monkeypatch.setattr(os, "getcwd", lambda: "/cwd")
        args = argparse.Namespace(
            mode="agent.fast",
            session=None,
            cwd=None,
            project_dir=None,
            trusted_dir=None,
        )
        req = _build_request(args, "test")
        assert req["params"]["mode"] == "agent.fast"

    @staticmethod
    def test_request_id_is_different_each_call(monkeypatch):
        monkeypatch.setattr(os, "getcwd", lambda: "/cwd")
        args = argparse.Namespace(
            mode="code.normal",
            session=None,
            cwd=None,
            project_dir=None,
            trusted_dir=None,
        )
        ids = {_build_request(args, "test")["id"] for _ in range(5)}
        assert len(ids) == 5


class TestValidateArgs:
    @staticmethod
    def test_valid():
        args = argparse.Namespace(mode="code.normal", json=False, jsonl=False,
                                  show_reasoning=False, show_tools=False, timeout=None)
        assert _validate_args(args) is None
        assert args.mode == "code.normal"

    @staticmethod
    def test_invalid_mode():
        args = argparse.Namespace(mode="garbage", json=False, jsonl=False,
                                  show_reasoning=False, show_tools=False, timeout=None)
        assert _validate_args(args) == 2

    @staticmethod
    def test_json_and_jsonl_conflict():
        args = argparse.Namespace(mode="code.normal", json=True, jsonl=True,
                                  show_reasoning=False, show_tools=False, timeout=None)
        assert _validate_args(args) == 2

    @staticmethod
    def test_negative_timeout():
        args = argparse.Namespace(mode="code.normal", json=False, jsonl=False,
                                  show_reasoning=False, show_tools=False, timeout=-1.0)
        assert _validate_args(args) == 2

    @staticmethod
    def test_zero_timeout():
        args = argparse.Namespace(mode="code.normal", json=False, jsonl=False,
                                  show_reasoning=False, show_tools=False, timeout=0)
        assert _validate_args(args) == 2

    @staticmethod
    def test_mode_resolved():
        args = argparse.Namespace(mode="agent", json=False, jsonl=False,
                                  show_reasoning=False, show_tools=False, timeout=None)
        assert _validate_args(args) is None
        assert args.mode == "agent"


class TestParser:
    @staticmethod
    def test_root_parser_has_chat():
        p = build_parser()
        assert p.prog == "jiuwenswarm chat"

    @staticmethod
    def test_default_mode():
        p = build_parser()
        ns = p.parse_args(["hello"])
        assert ns.mode == "code.normal"

    @staticmethod
    def test_prompt_collects_remaining():
        p = build_parser()
        ns = p.parse_args(["hello", "world", "test"])
        assert ns.prompt == ["hello", "world", "test"]

    @staticmethod
    def test_no_prompt():
        p = build_parser()
        ns = p.parse_args([])
        assert ns.prompt == []

    @staticmethod
    def test_all_options_parsed():
        p = build_parser()
        ns = p.parse_args(
            [
                "--mode", "agent.fast",
                "--session", "s1",
                "--cwd", "/tmp",
                "--project-dir", "/proj",
                "--trusted-dir", "/d1",
                "--trusted-dir", "/d2",
                "--gateway-url", "ws://h:1/tui",
                "--name", "inst",
                "--dotenv", "/path/.env",
                "--json",
                "--show-reasoning",
                "--show-tools",
                "--timeout", "60",
                "prompt",
            ]
        )
        assert ns.mode == "agent.fast"
        assert ns.session == "s1"
        assert ns.cwd == "/tmp"
        assert ns.project_dir == "/proj"
        assert ns.trusted_dir == ["/d1", "/d2"]
        assert ns.gateway_url == "ws://h:1/tui"
        assert ns.name == "inst"
        assert ns.dotenv == "/path/.env"
        assert ns.json is True
        assert ns.show_reasoning is True
        assert ns.show_tools is True
        assert ns.timeout == 60


def _permission_question(card_id: str) -> dict:
    return {"card_id": card_id}


class TestPermissionCardAnswer:
    @staticmethod
    @pytest.mark.parametrize("size", [128, 129, 512, 513])
    def test_call_id_alias_boundary(size: int):
        from jiuwenswarm.channels.cli.chat import _permission_answer_for_card

        call_id = "c" * size
        answers = _permission_answer_for_card(
            [_permission_question(call_id)], selected="allow_once", custom_input="",
        )
        assert answers == (
            [{"selected_options": ["allow_once"], "custom_input": "", "card_id": call_id}]
            if size <= 512 else []
        )

    @staticmethod
    def test_builds_one_opaque_card_answer():
        from jiuwenswarm.channels.cli.chat import _permission_answer_for_card

        assert _permission_answer_for_card(
            [_permission_question("invocation-1")],
            selected="本次允许",
            custom_input="1",
        ) == [
            {
                "selected_options": ["本次允许"],
                "custom_input": "1",
                "card_id": "invocation-1",
            }
        ]

    @staticmethod
    @pytest.mark.parametrize(
        "questions",
        [
            [],
            [{}],
            [_permission_question("one"), _permission_question("two")],
            [_permission_question("same"), _permission_question("same")],
        ],
    )
    def test_missing_or_duplicate_host_key_fails_closed(questions: list[dict]):
        from jiuwenswarm.channels.cli.chat import _permission_answer_for_card

        assert _permission_answer_for_card(
            questions,
            selected="本次允许",
            custom_input="1",
        ) == []


class TestInteractiveLoop:
    @staticmethod
    async def _make_connected_client(messages: list[dict]):
        class FakeWs:
            def __init__(self):
                self._idx = 0

            async def recv(self):
                if self._idx < len(messages):
                    data = json.dumps(messages[self._idx])
                    self._idx += 1
                    return data
                await asyncio.sleep(10)
                return json.dumps({})

            async def send(self, _data):
                pass

            async def close(self):
                pass

        client = GatewayClient("ws://127.0.0.1:19001/tui")
        client.set_mock_ws(FakeWs())
        return client

    @pytest.mark.asyncio
    @pytest.mark.parametrize("interrupt_id", ["intr-1", ""])
    async def test_interrupt_answer_echoes_the_interrupt_id_of_the_question(self, interrupt_id):
        from jiuwenswarm.channels.cli.chat import _run_interactive_loop

        question = {
            "request_id": "call-1",
            "source": "ask_user_interrupt",
            "questions": [{"question": "Which?", "options": [{"label": "A"}]}],
        }
        if interrupt_id:
            question["interrupt_id"] = interrupt_id
        client = await self._make_connected_client([
            {"type": "event", "event": "chat.ask_user_question", "payload": question},
            {"type": "event", "event": "chat.final", "payload": {"content": "before the answer"}},
            {"type": "event", "event": "chat.final", "payload": {"content": "done"}},
        ])
        sent = []

        async def record(frame):
            sent.append(frame)

        request = {
            "type": "req", "id": "r1", "method": "chat.send", "is_stream": True,
            "params": {"session_id": "s1", "query": "hi", "mode": "code.normal"},
        }
        with (
            patch.object(client, "send_request", record),
            patch("jiuwenswarm.channels.cli.chat.sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="1"),
        ):
            assert await _run_interactive_loop(client, HumanRenderer(), request) == 0

        params = sent[-1]["params"]
        assert sent[-1]["method"] == "chat.send"
        assert params["request_id"] == "call-1"
        assert params.get("interrupt_id", "") == interrupt_id
        assert ("interrupt_id" in params) is bool(interrupt_id)

    @pytest.mark.asyncio
    async def test_stream_delta_until_final(self):
        messages = [
            {"type": "event", "event": "chat.delta", "payload": {"content": "Hello"}},
            {"type": "event", "event": "chat.delta", "payload": {"content": " world"}},
            {"type": "event", "event": "chat.final", "payload": {"content": "Hello world"}},
        ]

        from jiuwenswarm.channels.cli.chat import _run_interactive_loop

        client = await self._make_connected_client(messages)
        renderer = HumanRenderer()
        request = {
            "type": "req", "id": "r1", "method": "chat.send",
            "is_stream": True,
            "params": {"session_id": "s1", "content": "hi", "query": "hi",
                       "mode": "code.normal", "cwd": "/tmp", "project_dir": "/tmp",
                       "trusted_dirs": ["/tmp"]},
        }
        code = await _run_interactive_loop(client, renderer, request)
        assert code == 0
        assert renderer.streamed_text == "Hello world"

    @pytest.mark.asyncio
    async def test_team_control_final_envelopes_do_not_hide_answer(self):
        messages = [
            {
                "type": "event",
                "event": "chat.processing_status",
                "payload": {"is_processing": True},
            },
            {
                "type": "event",
                "event": "chat.final",
                "payload": {"event_type": "team.runtime_ready"},
            },
            {
                "type": "event",
                "event": "chat.final",
                "payload": {"event_type": "chat.llm_usage"},
            },
            {
                "type": "event",
                "event": "chat.delta",
                "payload": {"content": "当前是集群模式"},
            },
            {
                "type": "event",
                "event": "chat.final",
                "payload": {
                    "event_type": "chat.final",
                    "content": "当前是集群模式。",
                },
            },
            {
                "type": "event",
                "event": "chat.processing_status",
                "payload": {"is_processing": False},
            },
        ]

        from jiuwenswarm.channels.cli.chat import _run_interactive_loop

        client = await self._make_connected_client(messages)
        renderer = HumanRenderer()
        request = {
            "type": "req", "id": "r1", "method": "chat.send",
            "is_stream": True,
            "params": {"session_id": "s1", "content": "hi", "query": "hi",
                       "mode": "team", "cwd": "/tmp", "project_dir": "/tmp",
                       "trusted_dirs": ["/tmp"]},
        }
        code = await _run_interactive_loop(client, renderer, request)
        assert code == 0
        assert renderer.streamed_text == "当前是集群模式。"

    @pytest.mark.asyncio
    async def test_chat_error_returns_1(self):
        messages = [
            {"type": "event", "event": "chat.error", "payload": {"error": "something broke"}},
        ]

        from jiuwenswarm.channels.cli.chat import _run_interactive_loop

        client = await self._make_connected_client(messages)
        renderer = HumanRenderer()
        request = {
            "type": "req", "id": "r1", "method": "chat.send",
            "is_stream": True,
            "params": {"session_id": "s1", "content": "hi", "query": "hi",
                       "mode": "code.normal", "cwd": "/tmp", "project_dir": "/tmp",
                       "trusted_dirs": ["/tmp"]},
        }
        code = await _run_interactive_loop(client, renderer, request)
        assert code == 1

    @pytest.mark.asyncio
    async def test_non_interactive_request_rejects_ask_user_event(self):
        messages = [
            {
                "type": "event",
                "event": "chat.ask_user_question",
                "payload": {
                    "question": "A or B?",
                    "options": [{"label": "A"}, {"label": "B"}],
                },
            },
        ]

        from jiuwenswarm.channels.cli.chat import _run_interactive_loop

        client = await self._make_connected_client(messages)
        renderer = HumanRenderer()
        request = {
            "type": "req",
            "id": "r1",
            "method": "chat.send",
            "is_stream": True,
            "params": {
                "session_id": "s1",
                "content": "hi",
                "query": "hi",
                "mode": "code.normal",
                "supports_user_interaction": False,
            },
        }

        code = await _run_interactive_loop(client, renderer, request)

        assert code == 4

    @pytest.mark.asyncio
    async def test_keepalive_final_does_not_terminate(self):
        messages = [
            {"type": "event", "event": "chat.processing_status", "payload": {"is_processing": True}},
            {"type": "event", "event": "chat.final", "payload": {"event_type": "keepalive"}},
            {"type": "event", "event": "chat.delta", "payload": {"content": "ok"}},
            {"type": "event", "event": "chat.final", "payload": {"content": "ok"}},
        ]

        from jiuwenswarm.channels.cli.chat import _run_interactive_loop

        client = await self._make_connected_client(messages)
        renderer = HumanRenderer()
        request = {
            "type": "req", "id": "r1", "method": "chat.send",
            "is_stream": True,
            "params": {"session_id": "s1", "content": "hi", "query": "hi",
                       "mode": "code.normal", "cwd": "/tmp", "project_dir": "/tmp",
                       "trusted_dirs": ["/tmp"]},
        }
        code = await _run_interactive_loop(client, renderer, request)
        assert code == 0
        assert renderer.streamed_text == "ok"

    @pytest.mark.asyncio
    @staticmethod
    async def test_connection_closed_gracefully():
        class FakeWs:
            def __init__(self):
                self._called = 0

            async def recv(self):
                self._called += 1
                if self._called == 1:
                    return json.dumps({
                        "type": "event",
                        "event": "connection.ack",
                        "payload": {"protocol_version": "1.0", "transport": "cli"},
                    })
                raise ConnectionError("closed")

            async def send(self, _data):
                pass

            async def close(self):
                pass

        from jiuwenswarm.channels.cli.chat import _run_interactive_loop

        client = GatewayClient("ws://127.0.0.1:19001/tui")
        client.set_mock_ws(FakeWs())
        renderer = HumanRenderer()
        request = {
            "type": "req", "id": "r1", "method": "chat.send",
            "is_stream": True,
            "params": {"session_id": "s1", "content": "hi", "query": "hi",
                       "mode": "code.normal", "cwd": "/tmp", "project_dir": "/tmp",
                       "trusted_dirs": ["/tmp"]},
        }
        code = await _run_interactive_loop(client, renderer, request)
        assert code == 4

    @pytest.mark.asyncio
    async def test_processing_status_restarts_spinner(self):
        messages = [
            {"type": "event", "event": "chat.processing_status", "payload": {"is_processing": True}},
            {"type": "event", "event": "chat.delta", "payload": {"content": "step1"}},
            {"type": "event", "event": "chat.processing_status", "payload": {"is_processing": True}},
            {"type": "event", "event": "chat.delta", "payload": {"content": "step2"}},
            {"type": "event", "event": "chat.final", "payload": {"content": "step1step2"}},
        ]

        from jiuwenswarm.channels.cli.chat import _run_interactive_loop

        client = await self._make_connected_client(messages)
        renderer = HumanRenderer()
        request = {
            "type": "req", "id": "r1", "method": "chat.send",
            "is_stream": True,
            "params": {"session_id": "s1", "content": "hi", "query": "hi",
                       "mode": "code.normal", "cwd": "/tmp", "project_dir": "/tmp",
                       "trusted_dirs": ["/tmp"]},
        }
        code = await _run_interactive_loop(client, renderer, request)
        assert code == 0
        assert renderer.streamed_text == "step1step2"

    @pytest.mark.asyncio
    @staticmethod
    async def test_delta_gap_inserts_line_break():
        from jiuwenswarm.channels.cli.chat import _run_interactive_loop

        messages = [
            {"type": "event", "event": "chat.delta", "payload": {"content": "part1"}},
            {"type": "event", "event": "chat.delta", "payload": {"content": "part2"}},
            {"type": "event", "event": "chat.final", "payload": {"content": "part1\n\npart2"}},
        ]

        fake_time = [0.0]

        class FakeWs:
            def __init__(self):
                self._idx = 0

            async def recv(self):
                if self._idx == 0:
                    fake_time[0] = 0.0
                elif self._idx == 1:
                    fake_time[0] = 1.0
                if self._idx < len(messages):
                    data = json.dumps(messages[self._idx])
                    self._idx += 1
                    return data
                await asyncio.sleep(10)
                return json.dumps({})

            async def send(self, _data):
                pass

            async def close(self):
                pass

        def mock_monotonic():
            return fake_time[0]

        with patch("jiuwenswarm.channels.cli.render.time.monotonic", mock_monotonic):
            client = GatewayClient("ws://127.0.0.1:19001/tui")
            client.set_mock_ws(FakeWs())
            renderer = HumanRenderer()
            request = {
                "type": "req", "id": "r1", "method": "chat.send",
                "is_stream": True,
                "params": {"session_id": "s1", "content": "hi", "query": "hi",
                           "mode": "code.normal", "cwd": "/tmp", "project_dir": "/tmp",
                           "trusted_dirs": ["/tmp"]},
            }
            code = await _run_interactive_loop(client, renderer, request)
            assert code == 0
            assert renderer.streamed_text == "part1\n\npart2"


class TestTrustedDirsState:
    """Tests for _load_state / _save_state."""

    @staticmethod
    def test_load_state_missing_file(monkeypatch, tmp_path):
        state_file = tmp_path / "nonexistent.json"
        monkeypatch.setattr("jiuwenswarm.channels.cli.chat._STATE_FILE", state_file)
        assert _load_state() == {}

    @staticmethod
    def test_save_and_load_state(monkeypatch, tmp_path):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr("jiuwenswarm.channels.cli.chat._STATE_FILE", state_file)
        _save_state({"/dir1": True, "/dir2": False})
        assert state_file.exists()
        loaded = _load_state()
        assert loaded == {"/dir1": True, "/dir2": False}

    @staticmethod
    def test_load_state_corrupted_file(monkeypatch, tmp_path):
        state_file = tmp_path / "bad.json"
        state_file.write_text("not json", encoding="utf-8")
        monkeypatch.setattr("jiuwenswarm.channels.cli.chat._STATE_FILE", state_file)
        assert _load_state() == {}


class TestExternalDirs:
    """Tests for _get_persisted_external_dirs / _remove_dir_from_config."""

    @staticmethod
    def test_get_persisted_external_dirs_empty(monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.dump({"permissions": {"external_directory": {"*": "ask"}}}, cfg_path)
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", cfg_path)
        assert _get_persisted_external_dirs() == []

    @staticmethod
    def test_get_persisted_external_dirs_with_allows(monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.dump(
            {
                "permissions": {
                    "external_directory": {
                        "*": "deny",
                        "/Users/hwz/mcore/foo": "allow",
                        "/tmp/bar": "deny",
                        "/opt/baz": "allow",
                    }
                }
            },
            cfg_path,
        )
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", cfg_path)
        dirs = _get_persisted_external_dirs()
        assert _normalize_dir("/Users/hwz/mcore/foo") in dirs
        assert _normalize_dir("/opt/baz") in dirs
        assert _normalize_dir("/tmp/bar") not in dirs
        assert "*" not in dirs
        assert len(dirs) == 2

    @staticmethod
    def test_get_persisted_dirs_from_file_guard_paths(monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.dump(
            {
                "permissions": {
                    "external_directory": {"*": "ask"},
                    "file_guard": {
                        "enabled": True,
                        "paths": [
                            {
                                "path": "/trusted/a",
                                "read": "allow",
                                "write": "allow",
                                "exec": "ask",
                            },
                            {
                                "path": "/trusted/ro",
                                "read": "allow",
                                "write": "ask",
                                "exec": "ask",
                            },
                        ],
                    },
                }
            },
            cfg_path,
        )
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", cfg_path)
        dirs = _get_persisted_external_dirs()
        assert _normalize_dir("/trusted/a") in dirs
        assert _normalize_dir("/trusted/ro") not in dirs

    @staticmethod
    def test_remove_dir_from_config(monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        save_cfg_path = cfg_path
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.dump(
            {
                "permissions": {
                    "external_directory": {
                        "*": "ask",
                        "/Users/hwz/mcore/foo": "allow",
                    }
                }
            },
            cfg_path,
        )
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", save_cfg_path)
        assert _remove_dir_from_config("/Users/hwz/mcore/foo") is True
        assert _get_persisted_external_dirs() == []

    @staticmethod
    def test_remove_dir_from_file_guard_paths(monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.dump(
            {
                "permissions": {
                    "file_guard": {
                        "enabled": True,
                        "paths": [
                            {
                                "path": "/trusted/a",
                                "read": "allow",
                                "write": "allow",
                                "exec": "ask",
                            }
                        ],
                    }
                }
            },
            cfg_path,
        )
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", cfg_path)
        assert _remove_dir_from_config("/trusted/a") is True
        assert _get_persisted_external_dirs() == []

    @staticmethod
    def test_remove_dir_from_config_nonexistent(monkeypatch, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.dump(
            {"permissions": {"external_directory": {"*": "ask"}}},
            cfg_path,
        )
        monkeypatch.setattr("jiuwenswarm.common.config.CONFIG_YAML_PATH", cfg_path)
        assert _remove_dir_from_config("/nonexistent") is False


class TestGatewayClientClose:
    """Tests for GatewayClient.close() exception handling."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_close_swallows_error():
        client = GatewayClient("ws://127.0.0.1:19001/tui")

        class FakeFailingWs:
            async def close(self):
                raise RuntimeError("connection already closed")

        client.set_mock_ws(FakeFailingWs())
        # Should not raise
        await client.close()
        # Internal ws reference should be cleared
        assert client.is_open is False

    @pytest.mark.asyncio
    @staticmethod
    async def test_close_ws_none():
        client = GatewayClient("ws://127.0.0.1:19001/tui")
        # _ws is None by default
        await client.close()  # Should not raise


def test_version_flag_prints_runtime_version_and_exits_zero(capsys) -> None:
    with patch(
        "jiuwenswarm.channels.cli.chat.get_runtime_version_label",
        return_value="0.2.8.dev20261026",
    ):
        parser = build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "jiuwenswarm 0.2.8.dev20261026"
    