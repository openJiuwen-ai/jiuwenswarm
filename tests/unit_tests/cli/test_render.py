# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwenswarm.cli.render."""

from __future__ import annotations

import io
import json

from jiuwenswarm.cli.render import HumanRenderer, JsonRenderer, JsonlRenderer


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
        from unittest.mock import patch

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
