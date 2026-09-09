# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the Agent/Code debug_trace package (Phase 1: request-level /debug)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime.debug_trace import (
    DebugTraceLogger,
    resolve_debug_trace_settings,
)
from jiuwenswarm.server.runtime.debug_trace import config as debug_config
from jiuwenswarm.server.runtime.debug_trace import directives as directives_mod
from jiuwenswarm.server.runtime.debug_trace import paths as paths_mod
from jiuwenswarm.server.runtime.debug_trace.directives import (
    DEBUG_PREFIX,
    strip_debug_directive,
    strip_slash_directive,
)


# ── helpers ────────────────────────────────────────────────────────────────
def _chunk(ctype: str, payload: Any) -> SimpleNamespace:
    return SimpleNamespace(type=ctype, payload=payload)


def _logger(tmp_path: Path, *, mode: str = "code.normal", session_id: str = "sess") -> DebugTraceLogger:
    s = resolve_debug_trace_settings(mode=mode, request_debug=True)
    return DebugTraceLogger(
        file_path=tmp_path / f"dump-{mode.split('.')[0]}-{session_id}.txt",
        mode=mode,
        session_id=session_id,
        request_id="req-1",
        settings=s,
    )


def _read(log: DebugTraceLogger) -> str:
    log.flush()
    # _file is closed by flush; read directly from the path.
    return Path(log._path).read_text(encoding="utf-8")


# ── directives ─────────────────────────────────────────────────────────────
class TestStripDebugDirective:
    def test_strips_prefix_and_prompt(self):
        assert strip_debug_directive("/debug 你好") == ("你好", True)

    def test_requires_whitespace_after_prefix(self):
        # /debugfoo is NOT the directive (no whitespace after /debug).
        assert strip_debug_directive("/debugfoo x") == ("/debugfoo x", False)

    def test_bare_debug_not_recognised(self):
        # No prompt -> not recognised, so an empty query is never sent to the model.
        assert strip_debug_directive("/debug") == ("/debug", False)

    def test_leading_whitespace_and_multiple_words(self):
        assert strip_debug_directive("  /debug hello world") == ("hello world", True)

    def test_no_prefix_unchanged(self):
        assert strip_debug_directive("hello") == ("hello", False)

    def test_non_str_unchanged(self):
        assert strip_debug_directive(None) == (None, False)

    def test_plan_mode_system_reminder_prefix(self):
        # code.plan / Plan mode prepends a <system-reminder>...</system-reminder>
        # block to the query BEFORE the adapter sees it. /debug lives in the user
        # text after the reminder; the reminder must be preserved for the model.
        reminder = (
            "\n\n<system-reminder>\nPlan mode is active. You must only plan.\n"
            "</system-reminder>"
        )
        cleaned, present = strip_debug_directive(reminder + "/debug 你好")
        assert present is True
        assert cleaned == reminder + "你好"
        assert "/debug" not in cleaned

    def test_system_reminder_without_debug_unchanged(self):
        reminder = "\n\n<system-reminder>\nPlan mode is active.\n</system-reminder>"
        cleaned, present = strip_debug_directive(reminder + "just planning")
        assert present is False
        assert cleaned == reminder + "just planning"


# ── paths ──────────────────────────────────────────────────────────────────
class TestPaths:
    def test_agent_modes_use_agent_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths_mod, "get_user_workspace_dir", lambda: tmp_path)
        assert paths_mod.debug_trace_dir("agent.plan") == tmp_path / ".agent" / "traces"
        assert paths_mod.debug_trace_dir("agent.fast") == tmp_path / ".agent" / "traces"

    def test_code_mode_uses_code_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths_mod, "get_user_workspace_dir", lambda: tmp_path)
        assert paths_mod.debug_trace_dir("code.normal") == tmp_path / ".code" / "traces"

    def test_original_agent_plan_keeps_agent_dir_after_code_profile_resolution(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(paths_mod, "get_user_workspace_dir", lambda: tmp_path)
        mode = paths_mod.resolve_debug_trace_mode("code.plan", "agent.plan")

        assert paths_mod.debug_trace_file(mode, "sess") == (
            tmp_path / ".agent" / "traces" / "dump-agent-sess.txt"
        )

    def test_plain_web_agent_in_code_profile_remains_code_dump(self):
        assert paths_mod.resolve_debug_trace_mode("code.normal", "agent") == "code.normal"

    def test_explicit_code_plan_remains_code_dump(self):
        assert paths_mod.resolve_debug_trace_mode("code.plan", "code.plan") == "code.plan"

    def test_original_agent_plan_uses_agent_debug_settings(self, monkeypatch):
        monkeypatch.setattr(
            debug_config,
            "_load_debug_trace_config",
            lambda: {
                "agent": {"enabled": True},
                "code": {"enabled": False},
            },
        )
        mode = paths_mod.resolve_debug_trace_mode("code.plan", "agent.plan")

        assert resolve_debug_trace_settings(mode=mode, request_debug=False).enabled is True

    def test_file_names(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths_mod, "get_user_workspace_dir", lambda: tmp_path)
        assert paths_mod.debug_trace_file("agent.plan", "sess").name == "dump-agent-sess.txt"
        assert paths_mod.debug_trace_file("code.normal", "sess").name == "dump-code-sess.txt"

    def test_session_id_sanitised(self, monkeypatch, tmp_path):
        monkeypatch.setattr(paths_mod, "get_user_workspace_dir", lambda: tmp_path)
        # Slashes / dots stripped so the segment can't escape the traces dir.
        f = paths_mod.debug_trace_file("agent.plan", "../evil")
        assert f.parent == tmp_path / ".agent" / "traces"
        assert ".." not in f.name
        assert "/" not in f.name


# ── config ─────────────────────────────────────────────────────────────────
class TestSettings:
    def _cfg(self, monkeypatch, cfg):
        monkeypatch.setattr(debug_config, "_load_debug_trace_config", lambda: cfg)

    def test_request_debug_enables(self, monkeypatch):
        self._cfg(monkeypatch, {})
        s = resolve_debug_trace_settings(mode="code.normal", request_debug=True)
        assert s.enabled and s.dump_enabled
        assert not s.otel_enabled  # default: otel off unless debug_trace.<mode>.otel_enabled

    def test_no_request_debug_disables(self, monkeypatch):
        self._cfg(monkeypatch, {})
        s = resolve_debug_trace_settings(mode="agent.plan", request_debug=False)
        assert not s.enabled and not s.dump_enabled

    def test_otel_enabled_requires_debug_and_flag(self, monkeypatch):
        # debug + otel_enabled -> on
        self._cfg(monkeypatch, {"code": {"otel_enabled": True}})
        assert resolve_debug_trace_settings(mode="code.normal", request_debug=True).otel_enabled
        # otel_enabled but no debug -> off (debug_enabled gate)
        assert not resolve_debug_trace_settings(mode="code.normal", request_debug=False).otel_enabled
        # debug but otel_enabled false -> off (default)
        self._cfg(monkeypatch, {"code": {"otel_enabled": False}})
        assert not resolve_debug_trace_settings(mode="code.normal", request_debug=True).otel_enabled

    def test_config_mode_enabled(self, monkeypatch):
        # only agent mode enabled -> code disabled
        self._cfg(monkeypatch, {"agent": {"enabled": True}})
        assert resolve_debug_trace_settings(mode="agent.plan", request_debug=False).enabled
        assert not resolve_debug_trace_settings(mode="code.normal", request_debug=False).enabled

    def test_config_dump_disabled_escape_hatch(self, monkeypatch):
        self._cfg(monkeypatch, {"code": {"enabled": True, "dump_enabled": False}})
        s = resolve_debug_trace_settings(mode="code.normal", request_debug=False)
        assert s.enabled and not s.dump_enabled

    def test_config_include_toggles(self, monkeypatch):
        self._cfg(monkeypatch, {"code": {"include_reasoning": False}})
        s = resolve_debug_trace_settings(mode="code.normal", request_debug=False)
        assert s.include_reasoning is False
        assert s.include_model_output is True  # untouched default

    def test_config_limits_override(self, monkeypatch):
        self._cfg(monkeypatch, {"limits": {"tool_args_max_chars": 100}})
        s = resolve_debug_trace_settings(mode="agent.plan", request_debug=False)
        assert s.tool_args_max_chars == 100
        assert s.tool_result_max_chars == 8000  # untouched default

    def test_config_max_model_output_chars(self, monkeypatch):
        self._cfg(monkeypatch, {"limits": {"max_model_output_chars": 500}})
        s = resolve_debug_trace_settings(mode="agent.plan", request_debug=False)
        assert s.max_model_output_chars == 500
        # empty/null -> no cap
        self._cfg(monkeypatch, {"limits": {"max_model_output_chars": ""}})
        assert resolve_debug_trace_settings(mode="agent.plan", request_debug=False).max_model_output_chars is None

    def test_config_redaction(self, monkeypatch):
        self._cfg(monkeypatch, {"redaction": {"redact_completions": True}})
        s = resolve_debug_trace_settings(mode="agent.plan", request_debug=False)
        assert s.redact_completions is True
        assert s.redact_prompts is False

    def test_request_debug_wins_over_config_off(self, monkeypatch):
        self._cfg(monkeypatch, {})  # nothing in config
        s = resolve_debug_trace_settings(mode="code.normal", request_debug=True)
        assert s.enabled  # request-level still works (regression)


# ── logger feed / format ───────────────────────────────────────────────────
class TestDebugTraceLoggerFeed:
    def test_records_model_text(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run(input_text="hi")
        lg.feed(_chunk("llm_output", {"content": "hello world"}))
        lg.end_run(status="ok")
        out = _read(lg)
        assert "run start" in out and "run end" in out and "status=ok" in out
        assert "category=text" in out and "hello world" in out

    def test_run_start_writes_otel_ids(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run(input_text="hi", otel_trace_id="abc123", otel_span_id="def456")
        lg.end_run(status="ok")
        out = _read(lg)
        assert "otel_trace_id=abc123" in out
        assert "otel_span_id=def456" in out

    def test_run_start_otel_ids_empty_by_default(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run(input_text="hi")  # no otel ids (OTel not enabled)
        lg.end_run(status="ok")
        out = _read(lg)
        assert "otel_trace_id=" in out  # key present, value empty
        assert "otel_span_id=" in out

    def test_records_reasoning(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.feed(_chunk("llm_reasoning", {"content": "thinking..."}))
        lg.end_run(status="ok")
        assert "category=reasoning" in _read(lg)

    def test_records_tool_call(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.feed(_chunk("tool_call", {"tool_call": {
            "name": "shell_command", "arguments": {"command": "pytest -q"}, "id": "call_1",
        }}))
        lg.end_run(status="ok")
        out = _read(lg)
        assert "category=tool_call" in out
        assert "tool_name=shell_command" in out and "tool_call_id=call_1" in out
        assert "pytest -q" in out

    def test_records_tool_result(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.feed(_chunk("tool_result", {"tool_result": {
            "tool_name": "shell_command", "tool_call_id": "call_1",
            "result": "10 passed", "is_error": False,
        }}))
        lg.end_run(status="ok")
        out = _read(lg)
        assert "category=tool_result" in out and "10 passed" in out

    def test_records_usage(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.feed(_chunk("llm_usage", {"usage_metadata": {
            "input_tokens": 100, "output_tokens": 20, "total_tokens": 120, "model_name": "GLM-5.2",
        }}))
        lg.end_run(status="ok")
        out = _read(lg)
        assert "category=context_usage" in out
        assert "input_tokens=100" in out and "model_name=GLM-5.2" in out


    def test_answer_chunk_dropped_when_model_output_off(self, tmp_path):
        # include_model_output=False must suppress the trailing `answer`
        # chunk too — it re-sends the whole reply and is classified as
        # text, so without this guard the reply leaks into the dump as a
        # category=text JSON blob even though llm_output was suppressed.
        s = debug_config.DebugTraceSettings(
            mode="code.normal",
            enabled=True,
            dump_enabled=True,
            otel_enabled=False,
            include_model_output=False,
        )
        lg = DebugTraceLogger(
            file_path=tmp_path / "dump.txt",
            mode="code.normal",
            session_id="sess",
            request_id="req-1",
            settings=s,
        )
        lg.start_run()
        lg.feed(_chunk("llm_output", {"content": "streamed reply"}))
        lg.feed(_chunk("answer", {"content": "streamed reply"}))
        lg.end_run(status="ok")
        out = _read(lg)
        assert "streamed reply" not in out
        assert "category=text" not in out


# ── session registry (cross-task logger recovery) ──────────────────────────
class TestSessionRegistry:
    """Dispatch sites run in the DeepAgent supervisor task, where the per-request
    ContextVar is invisible: the agent run moved from in-request streaming to a
    session-setup supervisor task. They recover the logger by session_id."""

    def test_register_lookup_unregister_roundtrip(self, tmp_path):
        from jiuwenswarm.server.runtime.debug_trace.context import (
            get_debug_trace_logger_for_session,
            register_debug_trace_logger,
            unregister_debug_trace_logger,
        )

        lg = _logger(tmp_path, session_id="sess-A")
        try:
            register_debug_trace_logger("sess-A", lg)
            assert get_debug_trace_logger_for_session("sess-A") is lg
            # unknown session / empty id are safe no-ops
            assert get_debug_trace_logger_for_session("other") is None
            assert get_debug_trace_logger_for_session("") is None
            unregister_debug_trace_logger("sess-A")
            assert get_debug_trace_logger_for_session("sess-A") is None
            # unregister of unknown / empty id must not raise
            unregister_debug_trace_logger("nope")
            unregister_debug_trace_logger("")
        finally:
            lg.flush()  # close the dump file opened on construction

    def test_registry_recovers_logger_when_contextvar_invisible(self, tmp_path):
        # A task created in a context where the ContextVar was never set (mirrors
        # the supervisor task, created at session setup before the /debug request)
        # must NOT see the ContextVar, yet MUST recover the logger via the registry.
        from jiuwenswarm.server.runtime.debug_trace.context import (
            get_debug_trace_logger,
            get_debug_trace_logger_for_session,
            register_debug_trace_logger,
            unregister_debug_trace_logger,
        )

        lg = _logger(tmp_path, session_id="sess-B")

        async def supervisor_like():
            assert get_debug_trace_logger() is None  # ContextVar not inherited
            assert get_debug_trace_logger_for_session("sess-B") is lg  # registry works

        async def main():
            register_debug_trace_logger("sess-B", lg)
            # Deliberately do NOT set_debug_trace_logger here — the task below is
            # created in a context with no ContextVar binding, like the real
            # supervisor task. The registry (a module global) is still visible.
            await asyncio.create_task(supervisor_like())
            unregister_debug_trace_logger("sess-B")

        try:
            asyncio.run(main())
        finally:
            lg.flush()  # close the dump file opened on construction


# ── OTel single-agent span mechanics ───────────────────────────────────────
# Root-span fallback, run-output stamping, the single-agent team marker and
# the create_subagent hook now live in the SDK; they are covered by
# ``tests/unit_tests/harness/observability`` in the openjiuwen repository.


def test_assemble_run_answer_does_not_double_count_the_repeated_final():
    """An ``answer`` chunk re-sends the whole reply the deltas already carried."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _assemble_run_answer,
    )

    assert _assemble_run_answer(["hello ", "world"], "hello world") == "hello world"


def test_assemble_run_answer_keeps_a_flushed_tail():
    """A cut-short round flushes only its tail as chat.final — keep both parts."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _assemble_run_answer,
    )

    assert _assemble_run_answer(["hello "], "world") == "hello world"
    assert _assemble_run_answer([], "only final") == "only final"
    assert _assemble_run_answer([], "") == ""


# ── truncation / redaction ─────────────────────────────────────────────────
class TestTruncationAndRedaction:
    def test_tool_args_truncated(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        big = "x" * 5000
        lg.feed(_chunk("tool_call", {"tool_call": {"name": "t", "arguments": {"cmd": big}, "id": "c"}}))
        lg.end_run(status="ok")
        out = _read(lg)
        assert "truncated, original_chars=" in out
        # full payload not present
        assert big not in out

    def test_tool_result_truncated(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        big = "y" * 20000
        lg.feed(_chunk("tool_result", {"tool_result": {
            "tool_name": "t", "tool_call_id": "c", "result": big,
        }}))
        lg.end_run(status="ok")
        out = _read(lg)
        assert "truncated, original_chars=" in out
        assert big not in out

    def test_secret_keys_masked(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.feed(_chunk("tool_result", {"tool_result": {
            "tool_name": "t", "tool_call_id": "c",
            "result": {"api_key": "sk-super-secret", "password": "hunter2", "ok": "visible"},
        }}))
        lg.end_run(status="ok")
        out = _read(lg)
        assert "sk-super-secret" not in out
        assert "hunter2" not in out
        assert "***" in out
        assert "visible" in out

    def test_token_counts_not_masked(self, tmp_path):
        # "tokens_used" / "total_tokens" are token COUNTS, not secrets — the
        # 'token' substring must not trigger masking.
        from jiuwenswarm.server.runtime.debug_trace.stream_logger import _looks_secret
        assert not _looks_secret("tokens_used")
        assert not _looks_secret("total_tokens")
        assert not _looks_secret("input_tokens")
        assert not _looks_secret("output_tokens")
        # genuine secret names still match
        assert _looks_secret("access_token")
        assert _looks_secret("api_key")
        assert _looks_secret("api-key")
        assert _looks_secret("password")
        assert _looks_secret("set-cookie")

    def test_tool_args_secret_masked_even_when_shown(self, tmp_path):
        # include_tool_args=True: arguments are SHOWN, yet secret values are
        # still masked as ***. Arguments are fed as a JSON STRING — the real
        # shape LLM tool-calls deliver — so this guards the parse-then-mask path
        # (without it, _mask_secrets would skip the string and leak the secret).
        s = debug_config.DebugTraceSettings(
            mode="code.normal",
            enabled=True,
            dump_enabled=True,
            otel_enabled=False,
            include_tool_args=True,
        )
        lg = DebugTraceLogger(
            file_path=tmp_path / "dump.txt",
            mode="code.normal",
            session_id="sess",
            request_id="req-1",
            settings=s,
        )
        lg.start_run()
        # Real link delivers arguments as a JSON STRING (not a dict).
        args_str = json.dumps({
            "api_key": "sk-secret-12345",
            "password": "hunter2",
            "token": "tok-abc",
            "authorization": "Bearer xyz",
            "url": "https://example.com",   # not a secret -> stays
            "tokens_used": 42,               # plural token count -> not masked
        })
        lg.feed(_chunk("tool_call", {"tool_call": {
            "name": "set_credentials", "id": "call_1",
            "arguments": args_str,
        }}))
        lg.end_run(status="ok")
        out = _read(lg)

        # secret plaintext never appears
        for secret in ("sk-secret-12345", "hunter2", "tok-abc", "Bearer xyz"):
            assert secret not in out
        # secret values masked (key visible, value redacted). This branch masks
        # as ``******(fp:xxxxxxxx)`` (fingerprint, matches SensitiveDataFilter);
        # only the format differs from the upstream ``***`` — the key point is
        # the JSON-string arguments were parsed-then-masked, not leaked.
        for key in ("api_key", "password", "token", "authorization"):
            assert re.search(rf'"{key}": "\*+\(fp:[0-9a-f]+\)"', out), (key, out)
        # non-secret args stay visible -> masking is per-field, not whole-arg redaction
        assert "https://example.com" in out
        # plural token count must not be mis-masked
        assert '"tokens_used": 42' in out

    def test_nested_secrets_masked_and_original_unchanged(self, tmp_path):
        # Secrets buried deep in nested dict/list must still be masked, AND the
        # masking must not mutate the original payload (the live chunk is shared
        # with the agent pipeline / real tool execution — mutating it would
        # corrupt the run). Dict shape is used so non-mutation is observable.
        import copy
        s = debug_config.DebugTraceSettings(
            mode="code.normal",
            enabled=True,
            dump_enabled=True,
            otel_enabled=False,
            include_tool_args=True,
        )
        lg = DebugTraceLogger(
            file_path=tmp_path / "dump.txt",
            mode="code.normal",
            session_id="sess",
            request_id="req-1",
            settings=s,
        )
        original = {"tool_call": {
            "name": "register_service", "id": "call_1",
            "arguments": {
                "service": {
                    "name": "demo",  # not a secret -> stays
                    "credentials": {"api_key": "sk-real-xxx", "token": "tok-real"},  # dict->dict->dict
                    "endpoints": [{"url": "https://e.example", "auth": {"password": "pw-real"}}],  # dict->list->dict
                }
            },
        }}
        snapshot = copy.deepcopy(original)

        lg.start_run()
        lg.feed(_chunk("tool_call", original))
        lg.end_run(status="ok")
        out = _read(lg)

        # 1) recursive masking: deep secret values never appear, leaves masked
        for secret in ("sk-real-xxx", "tok-real", "pw-real"):
            assert secret not in out
        for key in ("api_key", "token", "password"):
            assert re.search(rf'"{key}": "\*+\(fp:[0-9a-f]+\)"', out), (key, out)
        # non-secret fields survive and the nested structure is preserved
        assert '"name": "demo"' in out
        assert "https://e.example" in out

        # 2) original payload NOT mutated (live data intact for tool execution)
        assert original == snapshot
        assert original["tool_call"]["arguments"]["service"]["credentials"]["api_key"] == "sk-real-xxx"
        assert original["tool_call"]["arguments"]["service"]["endpoints"][0]["auth"]["password"] == "pw-real"


# ── error handling / best-effort ───────────────────────────────────────────
class TestBestEffort:
    def test_write_failure_does_not_raise(self, tmp_path):
        # parent is a regular file -> mkdir fails -> logger disables itself.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        s = resolve_debug_trace_settings(mode="agent.plan", request_debug=True)
        lg = DebugTraceLogger(
            file_path=blocker / "dump.txt",
            mode="agent.plan", session_id="s", request_id="r", settings=s,
        )
        assert lg._disabled is True
        # all public methods are no-ops and never raise
        lg.start_run(input_text="hi")
        lg.feed(_chunk("llm_output", {"content": "x"}))
        lg.end_run(status="ok")
        lg.flush()

    def test_end_run_error_records_metadata(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        exc = RuntimeError("boom")
        lg.end_run(status="error", error=exc)
        out = _read(lg)
        assert "status=error" in out
        assert "error_type=RuntimeError" in out
        assert "error=boom" in out

    def test_end_run_idempotent(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.end_run(status="ok")
        lg.end_run(status="error", error=RuntimeError("x"))  # second call: no-op
        out = _read(lg)
        assert out.count("run end") == 1
        assert "status=ok" in out
        assert "status=error" not in out

    def test_cancelled_status(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.end_run(status="cancelled")
        assert "status=cancelled" in _read(lg)


# ── generic slash-directive primitive (shared with team_helpers) ───────────
class TestStripSlashDirective:
    def test_basic(self):
        assert strip_slash_directive("/debug 你好", "/debug") == ("你好", True)

    def test_requires_whitespace(self):
        assert strip_slash_directive("/debugfoo x", "/debug") == ("/debugfoo x", False)

    def test_bare_prefix_recognised(self):
        # generic primitive DOES recognise a bare prefix (team semantics);
        # agent/code reject it at the strip_debug_directive wrapper layer.
        assert strip_slash_directive("/debug", "/debug") == ("", True)

    def test_unknown_prefix(self):
        assert strip_slash_directive("hello", "/debug") == ("hello", False)

    def test_works_for_hide_dm(self):
        # team uses it for /hide_dm too
        assert strip_slash_directive("/hide_dm hello", "/hide_dm") == ("hello", True)


# ── directive parity with team_helpers (single source of truth) ────────────
class TestTeamParity:
    def test_team_reuses_shared_primitive(self):
        from jiuwenswarm.server.runtime.agent_adapter import team_helpers
        # team's aliases ARE the shared objects (no duplicate implementation)
        assert team_helpers._DEBUG_PREFIX is DEBUG_PREFIX
        assert team_helpers._strip_directive is strip_slash_directive


# ── agent_observability force-enable + sticky teardown ─────────────────────
class TestAgentObservabilityForce:
    """sync_agent_observability(force=) pulls up OTel when config is off, and
    once force is used the provider stays up (sticky) to avoid init/shutdown
    churn. The normal config-gated teardown still works when force was never used."""

    def _reset(self):
        import jiuwenswarm.agents.harness.agent_observability as ao
        ao._agent_observability_active = False
        ao._force_ever_enabled = False

    def test_force_inits_and_sticky_blocks_teardown(self, monkeypatch):
        import jiuwenswarm.agents.harness.agent_observability as ao
        self._reset()
        calls = {"init": 0, "shutdown": 0}

        def fake_acquire(_cfg):
            calls["init"] += 1
            return False

        monkeypatch.setattr(ao, "get_config", lambda: {"agent_observability": {"enabled": False}})
        monkeypatch.setattr(ao, "acquire_observability", fake_acquire)
        monkeypatch.setattr(
            ao, "shutdown_agent_observability",
            lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1),
        )
        # force=True with config off -> init + sticky flag set + active
        ao.sync_agent_observability(force=True)
        assert calls["init"] == 1
        assert ao._force_ever_enabled is True
        assert ao._agent_observability_active is True
        # next request: force=False, config off, active -> sticky blocks teardown
        ao.sync_agent_observability()
        assert calls["shutdown"] == 0
        assert ao._agent_observability_active is True

    def test_normal_path_still_tears_down_without_force(self, monkeypatch):
        import jiuwenswarm.agents.harness.agent_observability as ao
        self._reset()
        calls = {"shutdown": 0}
        # simulate a config-gated active provider (force never used)
        ao._agent_observability_active = True
        ao._force_ever_enabled = False
        monkeypatch.setattr(ao, "get_config", lambda: {"agent_observability": {"enabled": False}})
        monkeypatch.setattr(
            ao, "shutdown_agent_observability",
            lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1),
        )
        ao.sync_agent_observability()  # enabled off + active + never forced -> teardown
        assert calls["shutdown"] == 1
        assert ao._force_ever_enabled is False


# ── subagent capture (Phase 2: record subagent streams inline) ──────────────
class TestSubagentCapture:
    """begin/feed/end_subagent + source tagging + helper + patch."""

    def _settings(self, **over: Any) -> Any:
        base = resolve_debug_trace_settings(mode="code.normal", request_debug=True)
        return dataclasses.replace(base, **over)

    def test_config_include_subagent_flow_default_and_override(self, monkeypatch):
        from jiuwenswarm.server.runtime.debug_trace import config as debug_config

        monkeypatch.setattr(debug_config, "_load_debug_trace_config", lambda: {})
        s = resolve_debug_trace_settings(mode="code.normal", request_debug=True)
        assert s.include_subagent_flow is True  # default on
        monkeypatch.setattr(
            debug_config, "_load_debug_trace_config", lambda: {"code": {"include_subagent_flow": False}}
        )
        assert resolve_debug_trace_settings(mode="code.normal", request_debug=True).include_subagent_flow is False

    def test_captures_subagent_flow_flag(self, tmp_path):
        lg_on = DebugTraceLogger(
            file_path=tmp_path / "a.txt", mode="code.normal", session_id="s",
            request_id="r", settings=self._settings(include_subagent_flow=True),
        )
        lg_off = DebugTraceLogger(
            file_path=tmp_path / "b.txt", mode="code.normal", session_id="s",
            request_id="r", settings=self._settings(include_subagent_flow=False),
        )
        assert lg_on.captures_subagent_flow() is True
        assert lg_off.captures_subagent_flow() is False
        lg_on.flush()
        lg_off.flush()

    def test_begin_end_subagent_write_boundaries(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.begin_subagent(source="subagent:builtin:explore_agent", prompt="find foo")
        lg.end_subagent(source="subagent:builtin:explore_agent", status="ok")
        lg.end_run(status="ok")
        out = _read(lg)
        assert "subagent start" in out and "subagent end" in out
        assert "source=subagent:builtin:explore_agent" in out
        assert "prompt=find foo" in out
        assert "status=ok" in out

    def test_feed_subagent_tags_source_not_main(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.begin_subagent(source="subagent:custom:x")
        lg.feed_subagent(
            source="subagent:custom:x",
            chunk=_chunk("llm_output", {"content": "sub text"}),
        )
        lg.end_subagent(source="subagent:custom:x")
        lg.end_run(status="ok")
        out = _read(lg)
        assert "source=subagent:custom:x category=text" in out
        assert "sub text" in out

    def test_main_and_subagent_streams_do_not_bleed(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.feed(_chunk("llm_output", {"content": "main text"}))
        lg.begin_subagent(source="subagent:custom:x")
        lg.feed_subagent(
            source="subagent:custom:x",
            chunk=_chunk("llm_output", {"content": "sub text"}),
        )
        lg.end_subagent(source="subagent:custom:x")
        lg.feed(_chunk("llm_output", {"content": "main text 2"}))
        lg.end_run(status="ok")
        out = _read(lg)
        assert "source=main category=text" in out
        assert "source=subagent:custom:x category=text" in out
        assert "main text" in out and "sub text" in out and "main text 2" in out

    def test_end_subagent_flushes_pending_accumulation(self, tmp_path):
        lg = _logger(tmp_path)
        lg.start_run()
        lg.begin_subagent(source="subagent:custom:x")
        lg.feed_subagent(
            source="subagent:custom:x",
            chunk=_chunk("llm_output", {"content": "buffered"}),
        )
        lg.end_subagent(source="subagent:custom:x")  # must flush the buffered text
        lg.end_run(status="ok")
        assert "buffered" in _read(lg)

    def test_feed_subagent_noop_when_flag_off(self, tmp_path):
        lg = DebugTraceLogger(
            file_path=tmp_path / "off.txt", mode="code.normal", session_id="s",
            request_id="r", settings=self._settings(include_subagent_flow=False),
        )
        lg.start_run()
        lg.begin_subagent(source="subagent:custom:x")
        lg.feed_subagent(
            source="subagent:custom:x",
            chunk=_chunk("llm_output", {"content": "secret"}),
        )
        lg.end_subagent(source="subagent:custom:x")
        lg.end_run(status="ok")
        out = _read(lg)
        assert "secret" not in out  # feed_subagent was a no-op

    def test_invoke_subagent_with_trace_no_debug_calls_invoke(self):
        from jiuwenswarm.server.runtime.debug_trace import invoke_subagent_with_trace
        from jiuwenswarm.server.runtime.debug_trace.context import _DEBUG_TRACE_LOGGER

        assert _DEBUG_TRACE_LOGGER.get() is None  # clean baseline
        calls = {"invoke": 0, "stream": 0}

        class FakeSub:
            async def invoke(self, inputs, session=None):
                calls["invoke"] += 1
                return {"output": "from-invoke", "result_type": "answer"}

            async def stream(self, inputs, session=None):
                calls["stream"] += 1
                if False:  # pragma: no cover - never entered in no-debug path
                    yield None

        result = asyncio.run(invoke_subagent_with_trace(
            FakeSub(), inputs={"query": "q"}, session=None,
            source_label="subagent:builtin:explore_agent",
        ))
        assert calls == {"invoke": 1, "stream": 0}
        assert result["output"] == "from-invoke"

    def test_invoke_subagent_with_trace_debug_drives_stream(self, tmp_path):
        from jiuwenswarm.server.runtime.debug_trace import (
            invoke_subagent_with_trace,
            reset_debug_trace_logger,
            set_debug_trace_logger,
        )

        lg = _logger(tmp_path)
        lg.start_run()
        token = set_debug_trace_logger(lg)
        try:

            class FakeSub:
                async def invoke(self, inputs, session=None):  # pragma: no cover
                    return {"output": "should-not-happen"}

                async def stream(self, inputs, session=None):
                    yield _chunk("llm_output", {"content": "hello "})
                    yield _chunk("llm_output", {"content": "world"})

            result = asyncio.run(invoke_subagent_with_trace(
                FakeSub(), inputs={"query": "do thing"}, session=None,
                source_label="subagent:builtin:explore_agent",
            ))
        finally:
            reset_debug_trace_logger(token)
        assert result["output"] == "hello world"  # chunks reduced via SDK helper
        lg.end_run(status="ok")
        out = _read(lg)
        assert "subagent start" in out and "subagent end" in out
        assert "source=subagent:builtin:explore_agent" in out
        assert "hello world" in out

    def test_invoke_subagent_with_trace_uses_isolated_session(self, tmp_path):
        # Fix D: the helper MUST pass session=None to subagent.stream() so the
        # subagent drains an isolated session queue, not the parent session
        # (which the parent run also drains — round-robin token split, see
        # ReActAgent._inner_stream). Guards against regression.
        from jiuwenswarm.server.runtime.debug_trace import (
            invoke_subagent_with_trace,
            reset_debug_trace_logger,
            set_debug_trace_logger,
        )

        lg = _logger(tmp_path)
        lg.start_run()
        token = set_debug_trace_logger(lg)
        try:
            captured: dict[str, Any] = {}

            class FakeSub:
                async def invoke(self, inputs, session=None):  # pragma: no cover
                    return {"output": "x"}

                async def stream(self, inputs, session=None):
                    captured["session"] = session
                    yield _chunk("llm_output", {"content": "hi"})

            asyncio.run(invoke_subagent_with_trace(
                FakeSub(), inputs={"query": "q"}, session="PARENT_SESSION",
                source_label="subagent:builtin:browser_agent",
            ))
        finally:
            reset_debug_trace_logger(token)
        assert captured.get("session") is None  # isolated, NOT parent_session
        lg.end_run(status="ok")
        lg.flush()

    def test_invoke_subagent_with_trace_flag_off_falls_back_to_invoke(self, tmp_path):
        from jiuwenswarm.server.runtime.debug_trace import (
            invoke_subagent_with_trace,
            reset_debug_trace_logger,
            set_debug_trace_logger,
        )

        lg = DebugTraceLogger(
            file_path=tmp_path / "off.txt", mode="code.normal", session_id="s",
            request_id="r", settings=self._settings(include_subagent_flow=False),
        )
        lg.start_run()
        token = set_debug_trace_logger(lg)
        try:
            calls = {"invoke": 0, "stream": 0}

            class FakeSub:
                async def invoke(self, inputs, session=None):
                    calls["invoke"] += 1
                    return {"output": "ok"}

                async def stream(self, inputs, session=None):  # pragma: no cover
                    yield None

            result = asyncio.run(invoke_subagent_with_trace(
                FakeSub(), inputs={"query": "q"}, session=None,
                source_label="subagent:custom:x",
            ))
        finally:
            reset_debug_trace_logger(token)
        assert calls == {"invoke": 1, "stream": 0}
        assert result["output"] == "ok"
        lg.end_run(status="ok")
        assert "subagent start" not in _read(lg)  # no capture section written

    def test_background_task_inherits_contextvar(self, tmp_path):
        # asyncio.create_task copies the current ContextVar snapshot, so a
        # background subagent (AgentTool background=True) inherits the logger.
        from jiuwenswarm.server.runtime.debug_trace import (
            get_debug_trace_logger,
            reset_debug_trace_logger,
            set_debug_trace_logger,
        )

        lg = _logger(tmp_path)
        token = set_debug_trace_logger(lg)
        seen: dict[str, Any] = {}
        try:

            async def child():
                seen["child_sees_logger"] = get_debug_trace_logger() is lg

            async def main():
                task = asyncio.create_task(child())
                await task

            asyncio.run(main())
        finally:
            reset_debug_trace_logger(token)
            lg.flush()  # close the dump file handle (avoid ResourceWarning)
        assert seen.get("child_sees_logger") is True

    def test_task_tool_patch_idempotent(self):
        from openjiuwen.harness.tools.subagent.task_tool import TaskTool

        from jiuwenswarm.server.runtime.debug_trace.task_tool_patch import (
            apply_task_tool_debug_patch,
        )

        apply_task_tool_debug_patch()
        apply_task_tool_debug_patch()  # second call must be a no-op
        assert getattr(TaskTool, "debug_trace_patch_applied", False) is True

    # Sub-agent rail attachment moved into the SDK
    # (``openjiuwen.harness.observability.rail``); it is covered by
    # ``tests/unit_tests/harness/observability/test_rail.py`` there.
