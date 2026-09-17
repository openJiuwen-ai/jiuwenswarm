# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwenswarm CLI chat module."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from jiuwenswarm.cli.chat import (
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
from jiuwenswarm.cli.events import (
    event_kind,
    is_terminal_event,
    needs_user_input,
)
from jiuwenswarm.cli.gateway_client import GatewayClient
from jiuwenswarm.cli.render import HumanRenderer, JsonRenderer, JsonlRenderer


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
        assert req["params"]["cwd"] == "/custom/cwd"

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
        assert req["params"]["project_dir"] == "/custom/project"

    @staticmethod
    def test_with_trusted_dirs(monkeypatch):
        monkeypatch.setattr(os, "getcwd", lambda: "/cwd")
        monkeypatch.setattr(
            "jiuwenswarm.cli.chat._get_persisted_external_dirs",
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
            ["/dir1", "/dir2"]
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


class TestEvents:
    @staticmethod
    def test_event_kind_delta():
        assert event_kind("chat.delta") == "delta"

    @staticmethod
    def test_event_kind_reasoning():
        assert event_kind("chat.reasoning") == "reasoning"

    @staticmethod
    def test_event_kind_tool_call():
        assert event_kind("chat.tool_call") == "tool_call"

    @staticmethod
    def test_event_kind_tool_result():
        assert event_kind("chat.tool_result") == "tool_result"

    @staticmethod
    def test_event_kind_final():
        assert event_kind("chat.final") == "final"

    @staticmethod
    def test_event_kind_error():
        assert event_kind("chat.error") == "error"

    @staticmethod
    def test_event_kind_interactive():
        assert event_kind("chat.ask_user_question") == "interactive"
        assert event_kind("plan.approval_required") == "interactive"

    @staticmethod
    def test_event_kind_processing_status():
        assert event_kind("chat.processing_status") == "processing_status"

    @staticmethod
    def test_event_kind_other():
        assert event_kind("chat.unknown") == "chat"
        assert event_kind("some.other") == "other"

    @staticmethod
    def test_is_terminal_chat_final():
        assert is_terminal_event("chat.final", {}) is True

    @staticmethod
    def test_is_terminal_chat_final_keepalive():
        assert is_terminal_event("chat.final", {"event_type": "keepalive"}) is False

    @staticmethod
    def test_is_terminal_chat_error():
        assert is_terminal_event("chat.error", {}) is True

    @staticmethod
    def test_is_terminal_processing_done():
        assert is_terminal_event("chat.processing_status", {"is_processing": False}) is True

    @staticmethod
    def test_is_terminal_processing_still_active():
        assert is_terminal_event("chat.processing_status", {"is_processing": True}) is False

    @staticmethod
    def test_needs_user_input_ask():
        assert needs_user_input("chat.ask_user_question") is True

    @staticmethod
    def test_needs_user_input_plan():
        assert needs_user_input("plan.approval_required") is True

    @staticmethod
    def test_needs_user_input_other():
        assert needs_user_input("chat.delta") is False


class TestHumanRenderer:
    @staticmethod
    def _make_renderer(*, show_reasoning=False, show_tools=False):
        content_io = io.StringIO()
        status_io = io.StringIO()
        renderer = HumanRenderer(
            show_reasoning=show_reasoning,
            show_tools=show_tools,
            content_writer=lambda text: content_io.write(text),
            status_writer=lambda text: status_io.write(text),
        )
        return renderer, content_io, status_io

    @staticmethod
    def test_delta_writes_to_stdout():
        r, cout, _ = TestHumanRenderer._make_renderer()
        r.handle_delta({"content": "hello"})
        assert cout.getvalue() == "hello"

    @staticmethod
    def test_delta_accumulates_text():
        r, _, _ = TestHumanRenderer._make_renderer()
        r.handle_delta({"content": "hello"})
        r.handle_delta({"content": " world"})
        assert r.streamed_text == "hello world"

    @staticmethod
    def test_delta_clears_loading():
        r, _, _ = TestHumanRenderer._make_renderer()
        r.ensure_loading()
        assert r.loading is True
        r.handle_delta({"content": "hi"})
        assert r.loading is False

    @staticmethod
    def test_final_deduplicates_delta():
        r, cout, _ = TestHumanRenderer._make_renderer()
        r.handle_delta({"content": "hello world"})
        cout.truncate(0)
        cout.seek(0)

        r.handle_final({"content": "hello world"})
        assert cout.getvalue() == ""

    @staticmethod
    def test_final_appends_suffix():
        r, cout, _ = TestHumanRenderer._make_renderer()
        r.handle_delta({"content": "hello"})
        cout.truncate(0)
        cout.seek(0)

        r.handle_final({"content": "hello world"})
        assert cout.getvalue() == " world"

    @staticmethod
    def test_final_no_duplicate_on_different_format():
        r, cout, _ = TestHumanRenderer._make_renderer()
        r.handle_delta({"content": "partial"})
        cout.truncate(0)
        cout.seek(0)

        r.handle_final({"content": "completely different"})
        # Terminal can't undo already-streamed text; final should not reprint
        assert cout.getvalue() == ""
        # Internal state keeps the longer version
        assert r.streamed_text == "completely different"

    @staticmethod
    def test_final_only_called_once():
        r, cout, _ = TestHumanRenderer._make_renderer()
        r.handle_final({"content": "first"})
        cout.truncate(0)
        cout.seek(0)
        r.handle_final({"content": "second"})
        assert cout.getvalue() == ""

    @staticmethod
    def test_final_skip_keepalive():
        r, cout, _ = TestHumanRenderer._make_renderer()
        r.handle_final({"content": ""})
        assert cout.getvalue() == ""

    @staticmethod
    def _capture_logging(logger_name, capture_builder):
        import logging
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.INFO)
        logger = logging.getLogger(logger_name)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            capture_builder(stream)
        finally:
            logger.removeHandler(handler)
            handler.close()
            logger.propagate = True

    @staticmethod
    def test_error_writes_to_stderr():
        def _run(stream):
            r = HumanRenderer()
            r.handle_error({"error": "bad thing"})
            assert "bad thing" in stream.getvalue()
        TestHumanRenderer._capture_logging("jiuwenswarm.cli.render", _run)

    @staticmethod
    def test_reasoning_hidden_by_default():
        def _run(stream):
            r = HumanRenderer()
            r.handle_reasoning({"content": "thinking..."})
            assert "thinking" not in stream.getvalue()
        TestHumanRenderer._capture_logging("jiuwenswarm.cli.render", _run)

    @staticmethod
    def test_reasoning_shown_when_enabled():
        def _run(stream):
            r = HumanRenderer(show_reasoning=True)
            r.handle_reasoning({"content": "thinking..."})
            assert "thinking..." in stream.getvalue()
        TestHumanRenderer._capture_logging("jiuwenswarm.cli.render", _run)

    @staticmethod
    def test_tool_call_hidden_by_default():
        def _run(stream):
            r = HumanRenderer()
            r.handle_tool_call({"tool_name": "bash", "arguments": "{}"})
            assert "bash" not in stream.getvalue()
        TestHumanRenderer._capture_logging("jiuwenswarm.cli.render", _run)

    @staticmethod
    def test_tool_call_shown_when_enabled():
        def _run(stream):
            r = HumanRenderer(show_tools=True)
            r.handle_tool_call({"tool_name": "bash", "arguments": '{"cmd":"ls"}'})
            assert "bash" in stream.getvalue()
        TestHumanRenderer._capture_logging("jiuwenswarm.cli.render", _run)

    @staticmethod
    def test_tool_result_hidden_by_default():
        def _run(stream):
            r = HumanRenderer()
            r.handle_tool_result({"tool_name": "bash", "status": "done"})
            assert "bash" not in stream.getvalue()
        TestHumanRenderer._capture_logging("jiuwenswarm.cli.render", _run)

    @staticmethod
    def test_tool_result_shown_when_enabled():
        def _run(stream):
            r = HumanRenderer(show_tools=True)
            r.handle_tool_result({"tool_name": "bash", "status": "done"})
            assert "bash" in stream.getvalue()
        TestHumanRenderer._capture_logging("jiuwenswarm.cli.render", _run)

    @staticmethod
    def test_ensure_loading_picks_random_verb():
        r = HumanRenderer()
        r.ensure_loading()
        assert r.verb
        assert r.start_time > 0


class TestJsonRenderer:
    @staticmethod
    def _make_renderer():
        content_io = io.StringIO()
        renderer = JsonRenderer(
            content_writer=lambda text: content_io.write(text),
        )
        return renderer, content_io

    @staticmethod
    def test_output_ok():
        r, cout = TestJsonRenderer._make_renderer()
        r.handle_event("chat.final", {"content": "result"})
        r.output()
        data = json.loads(cout.getvalue())
        assert data["ok"] is True
        assert data["content"] == "result"

    @staticmethod
    def test_output_error():
        r, cout = TestJsonRenderer._make_renderer()
        r.handle_error({"error": "fail"})
        r.output()
        data = json.loads(cout.getvalue())
        assert data["ok"] is False
        assert data["error"] == "fail"

    @staticmethod
    def test_output_uses_last_content():
        r, cout = TestJsonRenderer._make_renderer()
        r.handle_event("chat.final", {"content": "first"})
        r.handle_event("chat.final", {"content": "last"})
        r.output()
        data = json.loads(cout.getvalue())
        assert data["content"] == "last"


class TestJsonlRenderer:
    @staticmethod
    def test_handle_event_emits_frame():
        content_io = io.StringIO()
        r = JsonlRenderer(content_writer=lambda text: content_io.write(text))
        r.handle_event("chat.delta", {"content": "hi"})
        frame = json.loads(content_io.getvalue())
        assert frame["type"] == "event"
        assert frame["event"] == "chat.delta"
        assert frame["payload"]["content"] == "hi"


class TestGatewayClient:
    @pytest.mark.asyncio
    @staticmethod
    async def test_connect_waits_for_ack():
        async def _mock_connect(url, **kwargs):
            class FakeWs:
                async def recv(self):
                    return json.dumps({
                        "type": "event",
                        "event": "connection.ack",
                        "payload": {"protocol_version": "1.0", "transport": "tui"},
                    })

                async def close(self):
                    pass

            return FakeWs()

        with patch("jiuwenswarm.cli.gateway_client._connect_ws", _mock_connect):
            client = GatewayClient("ws://127.0.0.1:19001/tui")
            await client.connect()

    @pytest.mark.asyncio
    @staticmethod
    async def test_connect_rejects_non_ack():
        async def _mock_connect(url, **kwargs):
            class FakeWs:
                async def recv(self):
                    return json.dumps({"type": "req", "id": "x"})

                async def close(self):
                    pass

            return FakeWs()

        with patch("jiuwenswarm.cli.gateway_client._connect_ws", _mock_connect):
            client = GatewayClient("ws://127.0.0.1:19001/tui")
            with pytest.raises(ConnectionError):
                await client.connect()

    @pytest.mark.asyncio
    @staticmethod
    async def test_connect_rejects_invalid_json():
        async def _mock_connect(url, **kwargs):
            class FakeWs:
                async def recv(self):
                    return "not json"

                async def close(self):
                    pass

            return FakeWs()

        with patch("jiuwenswarm.cli.gateway_client._connect_ws", _mock_connect):
            client = GatewayClient("ws://127.0.0.1:19001/tui")
            with pytest.raises(ConnectionError):
                await client.connect()


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
    async def test_stream_delta_until_final(self):
        messages = [
            {"type": "event", "event": "chat.delta", "payload": {"content": "Hello"}},
            {"type": "event", "event": "chat.delta", "payload": {"content": " world"}},
            {"type": "event", "event": "chat.final", "payload": {"content": "Hello world"}},
        ]

        from jiuwenswarm.cli.chat import _run_interactive_loop

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

        from jiuwenswarm.cli.chat import _run_interactive_loop

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

        from jiuwenswarm.cli.chat import _run_interactive_loop

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

        from jiuwenswarm.cli.chat import _run_interactive_loop

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

        from jiuwenswarm.cli.chat import _run_interactive_loop

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

        from jiuwenswarm.cli.chat import _run_interactive_loop

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

        from jiuwenswarm.cli.chat import _run_interactive_loop

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
        from jiuwenswarm.cli.chat import _run_interactive_loop

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

        with patch("jiuwenswarm.cli.render.time.monotonic", mock_monotonic):
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


class TestSpinner:
    @staticmethod
    def test_spinner_frames_cycle():
        from jiuwenswarm.cli.render import _SPINNER_FRAMES as frames

        r = HumanRenderer()
        r.ensure_loading()
        first = r.spinner_idx
        r.tick_spinner()
        assert r.spinner_idx == (first + 1) % len(frames)

    @staticmethod
    def test_spinner_clears_on_delta():
        r = HumanRenderer()
        r.ensure_loading()
        assert r.loading is True
        r.handle_delta({"content": "x"})
        assert r.loading is False

    @staticmethod
    def test_spinner_idle_noop():
        r = HumanRenderer()
        r.tick_spinner()
        assert r.spinner_idx == 0

    @staticmethod
    def test_spinner_verb_rotates():
        from jiuwenswarm.cli.render import _VERBS

        fake_time = 1000.0

        def mock_monotonic():
            return fake_time

        with patch("jiuwenswarm.cli.render.time.monotonic", mock_monotonic):
            r = HumanRenderer()
            r.ensure_loading()
            initial_verb = r.verb

        fake_time += 5.5

        with patch("jiuwenswarm.cli.render.time.monotonic", mock_monotonic):
            r.tick_spinner()

        assert r.verb != initial_verb

    @staticmethod
    def test_spinner_restarts_on_processing_status():
        r = HumanRenderer()
        r.ensure_loading()
        assert r.loading is True
        r.handle_delta({"content": "x"})
        assert r.loading is False
        r.ensure_loading()
        assert r.loading is True


class TestTrustedDirsState:
    """Tests for _load_state / _save_state."""

    @staticmethod
    def test_load_state_missing_file(monkeypatch, tmp_path):
        state_file = tmp_path / "nonexistent.json"
        monkeypatch.setattr("jiuwenswarm.cli.chat._STATE_FILE", state_file)
        assert _load_state() == {}

    @staticmethod
    def test_save_and_load_state(monkeypatch, tmp_path):
        state_file = tmp_path / "state.json"
        monkeypatch.setattr("jiuwenswarm.cli.chat._STATE_FILE", state_file)
        _save_state({"/dir1": True, "/dir2": False})
        assert state_file.exists()
        loaded = _load_state()
        assert loaded == {"/dir1": True, "/dir2": False}

    @staticmethod
    def test_load_state_corrupted_file(monkeypatch, tmp_path):
        state_file = tmp_path / "bad.json"
        state_file.write_text("not json", encoding="utf-8")
        monkeypatch.setattr("jiuwenswarm.cli.chat._STATE_FILE", state_file)
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
