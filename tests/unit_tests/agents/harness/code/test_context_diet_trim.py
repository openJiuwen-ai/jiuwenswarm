# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the pure context-diet helpers (config, trimming, summaries)."""

from __future__ import annotations

import contextlib
import logging

from jiuwenswarm.agents.harness.code.context_diet import (
    CompactionConfig,
    ContextDietConfig,
    TrimConfig,
    load_context_diet_config,
    summarize_tool_result,
    trim_tool_output,
)
from jiuwenswarm.agents.harness.code.context_diet.trim import MARKER, classify_tool


@contextlib.contextmanager
def _capture_logger(caplog, name: str, level: int = logging.WARNING):
    """Capture records from logger ``name`` on any pytest version.

    ``setup_logger()`` sets ``propagate = False`` on the ``jiuwenswarm`` logger,
    so records never reach the root handler that ``caplog`` installs on the
    pytest pinned by uv.lock (9.0); pytest 9.1 also attaches to non-propagating
    loggers. Attach caplog's handler to the emitting logger itself (as the base
    tests do) and stop propagation meanwhile, so each record is captured once.
    """
    target = logging.getLogger(name)
    added = caplog.handler not in target.handlers
    saved = (target.propagate, target.level, caplog.handler.level)
    if added:
        target.addHandler(caplog.handler)
    target.propagate = False
    target.setLevel(level)
    caplog.handler.setLevel(level)
    try:
        yield
    finally:
        if added:
            target.removeHandler(caplog.handler)
        target.propagate = saved[0]
        target.setLevel(saved[1])
        caplog.handler.setLevel(saved[2])


def _shell_output(n_lines: int, *, signal_at: int | None = None, exit_code: int = 0) -> str:
    body = []
    for i in range(n_lines):
        if signal_at is not None and i == signal_at:
            body.append("FAILED tests/test_x.py::test_y - AssertionError: boom")
        else:
            body.append(f"line {i}")
    return "Command: pytest -q\nStdout: " + "\n".join(body) + f"\n\nStderr: (empty)\nExit Code: {exit_code}"


# ─── config ───────────────────────────────────────────────────────────────


def test_config_defaults_when_section_missing():
    cfg = load_context_diet_config({"modes": {"code": {}}})
    assert cfg.trim.enabled is True
    assert cfg.trim.read_file_max_lines == 0
    assert cfg.compaction.enabled is True
    assert cfg.compaction.shell_keep_last_k == 4
    assert cfg.preset == "default"
    assert cfg.compaction.read_keep_last_k == 0
    assert load_context_diet_config(None) == ContextDietConfig()
    assert load_context_diet_config({"modes": "garbage"}) == ContextDietConfig()


def test_config_parses_and_clamps_values():
    cfg = load_context_diet_config(
        {
            "modes": {
                "code": {
                    "context_diet": {
                        "trim": {"enabled": "false", "bash_head_lines": 1, "read_file_max_lines": "600", "spill_dir": "~/x"},
                        "compaction": {"shell_keep_last_k": -3, "read_keep_last_k": 12, "summary_chars": 10},
                    }
                }
            }
        }
    )
    assert cfg.trim.enabled is False
    assert cfg.trim.bash_head_lines == 5  # clamped to the minimum
    assert cfg.trim.read_file_max_lines == 600
    assert cfg.trim.spill_dir == "~/x"
    assert cfg.compaction.shell_keep_last_k == 0
    assert cfg.compaction.read_keep_last_k == 12
    assert cfg.compaction.summary_chars == 80  # clamped to the minimum


def test_config_ignores_invalid_types():
    trim = TrimConfig.from_mapping({"bash_max_lines": "many", "keep_signal_lines": "maybe"})
    assert trim.bash_max_lines == TrimConfig().bash_max_lines
    assert trim.keep_signal_lines is True
    comp = CompactionConfig.from_mapping(["not", "a", "mapping"])
    assert comp == CompactionConfig()


# ─── classification ───────────────────────────────────────────────────────


def test_classify_tool_by_suffix():
    assert classify_tool("bash") == "shell"
    assert classify_tool("fs.read_file") == "read_file"
    assert classify_tool("grep") == "grep"
    assert classify_tool("glob") == "grep"
    assert classify_tool("edit_file") == ""
    assert classify_tool("") == ""
    assert classify_tool(None) == ""


# ─── shell trimming ───────────────────────────────────────────────────────


def test_short_shell_output_is_untouched():
    text = _shell_output(20)
    res = trim_tool_output("bash", text, TrimConfig())
    assert res.changed is False
    assert res.content == text
    assert res.kind == "shell"


def test_long_shell_output_keeps_head_tail_and_signal_lines():
    spilled = {}

    def spill(name, text):
        spilled["name"] = name
        spilled["text"] = text
        return "/tmp/spill/bash_1.txt"

    text = _shell_output(400, signal_at=250, exit_code=1)
    res = trim_tool_output("bash", text, TrimConfig(), spill=spill)
    assert res.changed is True
    assert res.trimmed_chars < res.original_chars // 3
    lines = res.content.splitlines()
    assert lines[0].startswith("Command: pytest -q")
    assert lines[-1] == "Exit Code: 1"
    assert lines[1] == "Stdout: line 0" and lines[2] == "line 1"  # head kept
    assert any(l.startswith(MARKER) and "omitted" in l and "/tmp/spill/bash_1.txt" in l for l in lines)
    assert any("FAILED tests/test_x.py::test_y" in l and l.startswith(MARKER) for l in lines)
    assert res.kept_signal_lines == 1
    assert res.spill_path == "/tmp/spill/bash_1.txt"
    assert spilled["text"] == text and spilled["name"] == "bash"
    assert res.as_metadata()["kind"] == "shell"


def test_signal_lines_are_capped():
    body = "\n".join("ERROR something" for _ in range(500))
    text = "Command: x\nStdout: " + body + "\n\nStderr: (empty)\nExit Code: 2"
    res = trim_tool_output("bash", text, TrimConfig(max_signal_lines=5))
    assert res.kept_signal_lines == 5
    assert res.omitted_lines > 400


def test_signal_lines_can_be_disabled():
    text = _shell_output(400, signal_at=250)
    res = trim_tool_output("bash", text, TrimConfig(keep_signal_lines=False))
    assert res.kept_signal_lines == 0
    assert "FAILED tests" not in res.content


def test_long_single_lines_are_clipped_not_dropped():
    text = "Command: cat big.json\nStdout: " + "x" * 20000 + "\n\nStderr: (empty)\nExit Code: 0"
    res = trim_tool_output("bash", text, TrimConfig(), spill=lambda n, t: "/tmp/s.txt")
    assert res.changed is True
    assert "chars clipped" in res.content
    assert res.content.splitlines()[-2].startswith("Stderr") or "Exit Code: 0" in res.content
    assert res.trimmed_chars < 8000


def test_trim_disabled_returns_untouched():
    text = _shell_output(400)
    res = trim_tool_output("bash", text, TrimConfig(enabled=False))
    assert res.changed is False and res.content == text


def test_unmanaged_tools_and_non_text_results_pass_through():
    res = trim_tool_output("edit_file", "x" * 50000, TrimConfig())
    assert res.changed is False and res.kind == ""
    res2 = trim_tool_output("bash", {"structured": True}, TrimConfig())
    assert res2.changed is False and res2.content == {"structured": True}


def test_spill_failure_still_trims():
    text = _shell_output(400)
    res = trim_tool_output("bash", text, TrimConfig(), spill=lambda n, t: None)
    assert res.changed is True
    assert "not persisted" in res.content
    assert res.spill_path is None


# ─── grep trimming ────────────────────────────────────────────────────────


def test_grep_results_keep_head_and_count():
    text = "\n".join(f"src/file{i}.py:{i}: match" for i in range(500))
    res = trim_tool_output("grep", text, TrimConfig(), spill=lambda n, t: "/tmp/g.txt")
    lines = res.content.splitlines()
    assert res.changed is True
    assert len(lines) == TrimConfig().grep_head_lines + 1
    omitted = 500 - TrimConfig().grep_head_lines
    assert f"{omitted} more result lines omitted (500 total)" in lines[-1]
    assert "/tmp/g.txt" in lines[-1]


def test_grep_under_threshold_untouched():
    text = "\n".join(f"a{i}" for i in range(TrimConfig().grep_max_lines))
    assert trim_tool_output("glob", text, TrimConfig()).changed is False


# ─── read_file trimming ───────────────────────────────────────────────────


def _cat_n(n: int, start: int = 1) -> str:
    return "\n".join(f"{i:>6}\t# line {i}" for i in range(start, start + n))


def test_read_file_untouched_by_default():
    text = _cat_n(1500)
    res = trim_tool_output("read_file", text, TrimConfig(), tool_args={"file_path": "a.py"})
    assert res.changed is False


def test_read_file_window_with_resume_hint():
    text = _cat_n(1500)
    res = trim_tool_output("read_file", text, TrimConfig(read_file_max_lines=600), tool_args={"file_path": "a.py"})
    assert res.changed is True
    lines = res.content.splitlines()
    assert len(lines) == 601
    assert lines[599].strip().startswith("600\t")
    assert "lines 601–1500 not shown" in lines[-1]
    assert "offset=600" in lines[-1] and "a.py" in lines[-1]
    assert res.details["resume_offset"] == 600


def test_read_file_respects_explicit_limit_from_model():
    text = _cat_n(1500)
    res = trim_tool_output("read_file", text, TrimConfig(read_file_max_lines=600), tool_args={"limit": 1500})
    assert res.changed is False


def test_read_file_short_file_untouched():
    res = trim_tool_output("read_file", _cat_n(100), TrimConfig(read_file_max_lines=600))
    assert res.changed is False


# ─── summaries ────────────────────────────────────────────────────────────


def test_summary_for_shell_mentions_command_exit_and_signal():
    text = _shell_output(50, signal_at=10, exit_code=1)
    summary = summarize_tool_result("bash", {"command": "pytest -q tests"}, text)
    assert summary.startswith("bash `pytest -q tests` → exit 1;")
    assert "FAILED tests/test_x.py::test_y" in summary


def test_summary_for_grep_read_and_unknown():
    assert summarize_tool_result("grep", '{"pattern": "def foo", "path": "src"}', "a\nb\nc") == "grep /def foo/ src → 3 lines"
    assert summarize_tool_result("read_file", {"file_path": "src/a.py"}, _cat_n(30, start=11)) == "read_file src/a.py (L11–L40)"
    assert summarize_tool_result("edit_file", {}, "\nUpdated src/a.py\n") == "edit_file: Updated src/a.py"
    assert summarize_tool_result(None, None, "") == "tool: "


def test_summary_is_clipped():
    text = "Command: x\nStdout: " + "y" * 1000 + "\nExit Code: 0"
    summary = summarize_tool_result("bash", {"command": "c" * 500}, text, max_chars=100)
    assert len(summary) <= 100 and summary.endswith("…")


# ─── signal-line priority ─────────────────────────────────────────────────


def _pytest_verbose(n_passed: int, n_failed: int) -> str:
    body = [f"tests/test_ok.py::test_{i} PASSED" for i in range(n_passed)]
    for i in range(n_failed):
        body.append(f"tests/test_bad.py::test_{i} FAILED")
        body.append("Traceback (most recent call last):")
        body.append(f'  File "tests/test_bad.py", line {i + 1}, in test_{i}')
        body.append("    assert compute(i) == expected")
        body.append(f"E       AssertionError: case {i}")
    return "Command: pytest -v\nStdout: " + "\n".join(body) + "\n\nStderr: (empty)\nExit Code: 1"


def test_failures_win_over_passed_lines_regardless_of_order():
    # 300 PASSED lines come first; PASSED is not signal at all, so the 45
    # FAILED lines and their tracebacks fill the signal budget.
    out = trim_tool_output("bash", _pytest_verbose(300, 45), TrimConfig(max_signal_lines=40))
    kept = [ln for ln in out.content.splitlines() if ln.startswith(f"{MARKER} L")]
    assert len(kept) == 40
    assert not any("PASSED" in ln for ln in kept)
    assert all(
        ("FAILED" in ln) or ("Traceback" in ln) or ("AssertionError" in ln) or ('File "' in ln) or ("assert " in ln)
        for ln in kept
    )
    assert out.details["dropped_strong_signal_lines"] > 0
    assert "more error/failure line(s) exceeded max_signal_lines" in out.content
    assert out.content.splitlines()[-1] == "Exit Code: 1"


def test_errors_are_kept_before_warnings_and_notes():
    body = [f"src/mod{i}.c:12: warning: unused variable 'x{i}'" for i in range(200)]
    body += [
        "src/core.c:40: error: expected ';' before 'return'",
        "src/core.c:88: error: 'foo' undeclared",
        "src/util.c:3: error: conflicting types for 'bar'",
    ]
    body += [f"src/core.c:{i}: note: previous declaration was here" for i in range(60)]
    body.append("make: *** [Makefile:12: all] Error 1")
    content = "Command: make\nStdout: " + "\n".join(body) + "\n\nStderr: (empty)\nExit Code: 2"
    out = trim_tool_output("bash", content, TrimConfig(max_signal_lines=10))
    kept = [ln for ln in out.content.splitlines() if ln.startswith(f"{MARKER} L")]
    errors = [ln for ln in kept if ": error:" in ln]
    assert len(errors) == 3, kept
    # weak lines (the warnings that precede the errors) only fill the remaining budget
    assert len(kept) == 10
    assert sum(": warning:" in ln for ln in kept) == 7
    # kept lines stay in file order even though the errors were selected first
    numbers = [int(ln.split("L", 1)[1].split(":", 1)[0]) for ln in kept]
    assert numbers == sorted(numbers)
    assert out.details["dropped_strong_signal_lines"] == 0


def test_passed_and_skipped_are_not_signal_lines():
    from jiuwenswarm.agents.harness.code.context_diet.trim import (
        SIGNAL_PATTERN,
        STRONG_SIGNAL_PATTERN,
        WEAK_SIGNAL_PATTERN,
    )

    assert not SIGNAL_PATTERN.search("tests/test_a.py::test_b PASSED")
    assert WEAK_SIGNAL_PATTERN.search("tests/test_a.py::test_c SKIPPED (no network)")
    assert not STRONG_SIGNAL_PATTERN.search("tests/test_a.py::test_c SKIPPED (no network)")
    assert STRONG_SIGNAL_PATTERN.search("Exit Code: 1") and not STRONG_SIGNAL_PATTERN.search("Exit Code: 0")
    assert STRONG_SIGNAL_PATTERN.search("E       assert 1 == 2")
    assert STRONG_SIGNAL_PATTERN.search('  File "x.py", line 3, in f')


# ─── presets and data-derived defaults ───────────────────────────────────


def test_dataclass_defaults_match_the_default_preset():
    from jiuwenswarm.agents.harness.code.context_diet.config import PRESETS

    trim, comp = TrimConfig(), CompactionConfig()
    for key, value in PRESETS["default"]["trim"].items():
        assert getattr(trim, key) == value, key
    for key, value in PRESETS["default"]["compaction"].items():
        assert getattr(comp, key) == value, key


def test_aggressive_preset_enables_read_file_trimming_and_window():
    cfg = ContextDietConfig.from_mapping({"preset": "aggressive"})
    assert cfg.preset == "aggressive"
    assert cfg.trim.read_file_max_lines == 250
    assert cfg.compaction.read_keep_last_k == 6
    assert cfg.trim.bash_max_lines < TrimConfig().bash_max_lines


def test_explicit_keys_override_the_preset():
    cfg = ContextDietConfig.from_mapping(
        {"preset": "aggressive", "trim": {"read_file_max_lines": 0, "bash_max_lines": 99}}
    )
    assert cfg.trim.read_file_max_lines == 0
    assert cfg.trim.bash_max_lines == 99
    assert cfg.compaction.read_keep_last_k == 6  # untouched keys still come from the preset


def test_unknown_preset_falls_back_to_default():
    cfg = ContextDietConfig.from_mapping({"preset": "turbo"})
    assert cfg.preset == "default"
    assert cfg.trim == TrimConfig()


def test_trim_that_would_grow_the_output_is_dropped():
    # Just over the grep limit: the omission marker costs more than the removed lines.
    limit = TrimConfig().grep_max_lines
    text = "\n".join(f"a{i}" for i in range(limit + 3))
    res = trim_tool_output("glob", text, TrimConfig(), spill=lambda n, t: "/tmp/some/long/spill/path.txt")
    assert res.changed is False
    assert res.content == text
    assert res.details.get("skipped") == "trim_not_shorter"


def test_default_thresholds_trim_typical_large_shell_output():
    # 60 lines ~= the 75th percentile of bash results in the wave-1 A/B transcripts.
    text = "\n".join(f"collected line {i} " + "x" * 40 for i in range(60)) + "\nExit Code: 0"
    res = trim_tool_output("bash", text, TrimConfig(), spill=lambda n, t: "/tmp/s.txt")
    assert res.changed is True
    assert len(res.content) < len(text)
    assert "Exit Code: 0" in res.content


# ─── review fixes (R5) ────────────────────────────────────────────────────


def _cfg(preset: str) -> TrimConfig:
    return load_context_diet_config({"modes": {"code": {"context_diet": {"preset": preset}}}}).trim


def test_camelcase_exception_line_survives_default_preset():
    ok = [f"ok line {i}" for i in range(25)]
    trace = [
        "Traceback (most recent call last):",
        '  File "/app/pkg/mod.py", line 3, in <module>',
        "    import yaml",
        "ModuleNotFoundError: No module named 'yaml'",
    ]
    tail = [f"after line {i}" for i in range(25)]
    text = "Command: python -m pkg\nStdout: " + "\n".join(ok + trace + tail) + "\n\nStderr: (empty)\nExit Code: 1"
    result = trim_tool_output("bash", text, _cfg("default"))
    assert result.changed
    assert "ModuleNotFoundError: No module named 'yaml'" in result.content


def test_pytest_location_lines_survive_aggressive_preset():
    lines = [f"tests/test_a.py::test_ok_{i} PASSED" for i in range(40)]
    lines += [
        "=================================== FAILURES ===================================",
        "_________________________________ test_b _________________________________",
        "    def test_b():",
        ">       assert compute() == 3",
        "E       assert 2 == 3",
        "tests/test_b.py:12: AssertionError",
    ]
    lines += [f"filler {i}" for i in range(30)]
    lines += ["tests/test_c.py:40: in helper"]
    lines += [f"filler2 {i}" for i in range(30)]
    text = "Command: pytest -v\nStdout: " + "\n".join(lines) + "\n\nStderr: (empty)\nExit Code: 1"
    result = trim_tool_output("bash", text, _cfg("aggressive"))
    assert result.changed
    assert "tests/test_b.py:12: AssertionError" in result.content
    assert "tests/test_c.py:40: in helper" in result.content


def test_unknown_preset_warns_and_falls_back(caplog):
    with _capture_logger(caplog, load_context_diet_config.__module__):
        cfg = load_context_diet_config({"modes": {"code": {"context_diet": {"preset": "agressive"}}}})
    assert cfg.preset == "default"
    assert any("unknown preset" in r.getMessage() for r in caplog.records)


def test_discarded_trim_removes_its_spill_file(tmp_path):
    written: list[str] = []

    def spill(name: str, text: str) -> str:
        path = tmp_path / f"{name}-{len(written)}.txt"
        path.write_text(text)
        written.append(str(path))
        return str(path)

    cfg = TrimConfig.from_mapping(
        {"enabled": True, "bash_max_lines": 20, "bash_max_chars": 100000, "bash_head_lines": 5, "bash_tail_lines": 5}
    )
    # Just over the (clamped) line limit with one-char lines: the marker + spill path
    # is longer than the 12 characters that would be cut, so the trim is discarded.
    text = "\n".join(["a"] * 22)
    result = trim_tool_output("bash", text, cfg, spill=spill)
    assert result.content == text
    assert result.details.get("skipped") == "trim_not_shorter"
    assert written and not any(__import__("os").path.exists(p) for p in written)
