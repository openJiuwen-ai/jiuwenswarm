# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shell commands that print file contents or diffs are treated like file reads.

In the 2026-10-02 SWE-bench Pro run (default preset), 35 of the 79 shell trims
hit commands such as ``sed -n '1,60p' file``, ``cat test_x.py``, ``git diff`` or
``python -c "print(inspect.getsource(f))"``: output the model builds its next
edit from. These tests drive the real agent-core shapes (``ToolCall`` with the
model's JSON-string arguments, ``ToolCallInputs``, ``ToolMessage``) through the
trim rail and the shell compaction window.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from openjiuwen.core.foundation.llm.schema.message import (
    AssistantMessage,
    ToolMessage,
    UserMessage,
)
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.foundation.tool.schema import ToolOutput
from openjiuwen.core.runner.runner import create_agent_session
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs

from jiuwenswarm.agents.harness.code.context_diet import CompactionConfig
from jiuwenswarm.agents.harness.code.context_diet.config import (
    ContextDietConfig,
    TrimConfig,
)
from jiuwenswarm.agents.harness.code.context_diet.processors import (
    ContextDietShellWindowProcessor,
    build_compaction_processors,
)
from jiuwenswarm.agents.harness.code.context_diet.source_print import (
    source_print_reason,
)
from jiuwenswarm.agents.harness.code.rails import ContextDietTrimRail

# ─── classifier: real commands from the benchmark transcripts ─────────────

SOURCE_PRINTS = [
    ("cd /app && sed -n '540,600p' tests/unit/completion/test_models.py", "sed -n"),
    (
        'cd /app && sed -n 360,420p qutebrowser/app.py && echo ===== && grep -rn "VersionChange" tests/ | head -40',
        "sed -n",
    ),
    (
        "cd /app && sed -n '238,255p;565,575p;1010,1020p' openlibrary/plugins/upstream/addbook.py",
        "sed -n",
    ),
    ("cd /app && git diff -- openlibrary/catalog/marc/parse.py", "git diff"),
    (
        "cd /app && git --no-pager diff --stat && echo '---' && git --no-pager diff -- a.py b.py",
        "git diff",
    ),
    ("cd /app && git diff --stat && git diff", "git diff"),
    ("cd /app && git show HEAD:openlibrary/catalog/marc/marc_base.py", "git show"),
    ("cd /app && git log -p -3 -- lib/x.py", "git log -p"),
    (
        "cd /app && ls -la scripts/monitoring/ && cat requirements_scripts.txt && cat scripts/monitoring/tests/test_utils_sh.py",
        "cat",
    ),
    (
        'cd /app/t && for f in a b; do echo "=== $f ==="; cat bin_expect/$f.json; done',
        "cat",
    ),
    ("cd /app && tail -100 lib/ansible/module_utils/urls.py", "tail"),
    ("cd /app && head -40 openlibrary/tests/solr/test_update_work.py", "head"),
    ("cd /app && cat lib/ansible/release.py | head -30", "cat"),
    (
        'cd /app && python -c "\nimport web, inspect\nprint(inspect.getsource(web.webapi.rawinput))\n" 2>&1 | head -100',
        "inspect.getsource",
    ),
    (
        "cd /app && python - <<'PY'\nimport web, inspect\nprint(inspect.getsource(web.webapi.rawinput))\nPY",
        "inspect.getsource",
    ),
    (
        "cd /app && grep -n \"def test_url_completion\" -A 30 tests/unit/completion/test_models.py | sed -n '1,90p'",
        "grep with context",
    ),
    ("cd /app && awk 'NR>=10 && NR<=40' src/mod.py", "awk"),
]

NOT_SOURCE_PRINTS = [
    "cd /app && python -m pytest test/units/module_utils/urls/ -q -rf 2>&1",
    "cd /app && QT_QPA_PLATFORM=offscreen python -m pytest tests/unit/config/ -q 2>&1 | sed -n '1,60p'",
    # a log the same command just wrote is not source
    "cd /app && python -m pytest tests/x.py -q > /tmp/tcd2.log 2>&1; echo \"exit=$?\"; sed -n '1,45p' /tmp/tcd2.log",
    "cd /app && tail -40 /tmp/jiuwenswarm-data/agent/workspace/run_tests/20260928-091708-unittest-81f740a8.log",
    # mixed with a test run: the test output dominates and is what trimming is for
    "cd /app && git diff && timeout 900 python -m pytest openlibrary/core -q 2>&1 | tail -4",
    "cd /app && cat pytest.ini && python -m pytest tests/unit -q",
    "cd /app && npm test",
    # summaries, listings, plain searches, filtered output
    "cd /app && git diff --stat",
    "cd /app && git --no-pager diff --name-only",
    "cd /app && git log --oneline -3",
    "cd /app && ls -la && git status --short",
    'find /app -maxdepth 2 -not -path "*/.git/*" | head -60',
    'cd /app && grep -n "body_format\\|json" lib/ansible/modules/uri.py | head -60',
    "cd /app && cat setup.py | grep version",
    "cd /app && sed -n '1,80p' tests/test_x.py | grep -n 'def '",
    "cd /app && diff -rq /usr/lib/python3/ansible /app/lib/ansible | head -20",
    "cd /app && diff /tmp/base_fail.txt /tmp/patched_fail.txt",
    # writing a file is not printing one; a here-document body is not parsed as shell
    "cd /app && cat > /tmp/probe.py <<'EOF'\nprint('hi')\nEOF\npython /tmp/probe.py",
    "cd /app && python - <<'EOF'\nimport os\nos.system('cat setup.py')\nEOF",
    "cd /app && python - <<'EOF'\nfrom pkg import f\nprint(f(1))\nEOF",
    # re-reading the diet's own spill file must stay trimmable
    "cat /data/agent/workspace/context_diet/sess/bash_call_1.txt",
    "",
]


@pytest.mark.parametrize(("command", "reason"), SOURCE_PRINTS)
def test_source_prints_are_recognised(command, reason):
    assert source_print_reason(command) == reason


@pytest.mark.parametrize("command", NOT_SOURCE_PRINTS)
def test_other_shell_output_is_not_a_source_print(command):
    assert source_print_reason(command) is None


def test_classifier_never_raises_on_odd_input():
    for command in [
        "'unterminated",
        "cat <<EOF",
        "echo $((1<<3))",
        "|||",
        "\\",
        None,
        42,
    ]:
        assert source_print_reason(command) is None or isinstance(
            source_print_reason(command), str
        )


# ─── trim rail with real agent-core shapes ────────────────────────────────


def _bash_ctx(command: str, output: str, call_id: str = "c1") -> AgentCallbackContext:
    tool_call = ToolCall(
        id=call_id,
        type="function",
        name="bash",
        arguments=json.dumps({"command": command}),
    )
    content = f"Command: {command}\nStdout: {output}\n\nStderr: (empty)\nExit Code: 0"
    return AgentCallbackContext(
        agent=None,
        session=create_agent_session(session_id="s-src"),
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="bash",
            tool_args=tool_call.arguments,  # agent-core passes the raw JSON string
            tool_result=ToolOutput(success=True, data={"stdout": output}),
            tool_msg=ToolMessage(content=content, tool_call_id=call_id),
        ),
        extra={},
    )


def _code(lines: int) -> str:
    return "\n".join(
        f"    value_{i} = compute({i})  # line {i}" for i in range(1, lines + 1)
    )


def _rail(tmp_path, **overrides: Any) -> ContextDietTrimRail:
    trim = ContextDietConfig.from_mapping({"preset": "default", "trim": overrides}).trim
    rail = ContextDietTrimRail(trim, spill_root=tmp_path)
    rail.init(None)
    return rail


@pytest.mark.asyncio
async def test_sed_range_of_a_source_file_is_kept_whole(tmp_path):
    rail = _rail(tmp_path)
    ctx = _bash_ctx("cd /app && sed -n '1,300p' pkg/module.py", _code(300))
    original = ctx.inputs.tool_msg

    await rail.after_tool_call(ctx)

    assert ctx.inputs.tool_msg is original
    assert "[context-diet]" not in ctx.inputs.tool_msg.content
    assert not list(tmp_path.rglob("*.txt"))  # nothing spilled


@pytest.mark.asyncio
async def test_git_diff_and_getsource_are_kept_whole(tmp_path):
    rail = _rail(tmp_path)
    diff = "\n".join(f"+    added_{i} = {i}" for i in range(200))
    for command in (
        "cd /app && git --no-pager diff -- pkg/a.py",
        'cd /app && python -c "\nimport inspect, pkg\nprint(inspect.getsource(pkg.f))\n"',
    ):
        ctx = _bash_ctx(command, diff)
        original = ctx.inputs.tool_msg
        await rail.after_tool_call(ctx)
        assert ctx.inputs.tool_msg is original, command


@pytest.mark.asyncio
async def test_test_runs_are_still_trimmed(tmp_path):
    rail = _rail(tmp_path)
    log = "\n".join(
        [f"tests/test_x.py::test_{i} PASSED" for i in range(200)]
        + ["FAILED tests/test_x.py::test_bad"]
    )
    ctx = _bash_ctx("cd /app && python -m pytest tests -q", log)

    await rail.after_tool_call(ctx)

    content = ctx.inputs.tool_msg.content
    assert "[context-diet]" in content
    assert "FAILED tests/test_x.py::test_bad" in content


@pytest.mark.asyncio
async def test_source_print_above_the_cap_keeps_its_first_lines_and_spills(tmp_path):
    rail = _rail(tmp_path, source_print_max_lines=100)
    ctx = _bash_ctx("cd /app && cat pkg/huge.py", _code(400))
    original = ctx.inputs.tool_msg.content

    await rail.after_tool_call(ctx)

    content = ctx.inputs.tool_msg.content
    assert "line 99" in content and "line 150" not in content
    assert "is longer than 100 lines" in content
    spills = list(tmp_path.rglob("*.txt"))
    assert len(spills) == 1 and spills[0].read_text() == original
    assert str(spills[0]) in content


@pytest.mark.asyncio
async def test_switch_restores_the_old_behaviour(tmp_path):
    rail = _rail(tmp_path, exempt_source_prints=False)
    ctx = _bash_ctx("cd /app && sed -n '1,300p' pkg/module.py", _code(300))

    await rail.after_tool_call(ctx)

    assert "[context-diet]" in ctx.inputs.tool_msg.content


@pytest.mark.asyncio
async def test_zero_cap_never_trims_a_source_print(tmp_path):
    rail = _rail(tmp_path, source_print_max_lines=0)
    ctx = _bash_ctx("cd /app && cat pkg/huge.py", _code(5000))
    original = ctx.inputs.tool_msg

    await rail.after_tool_call(ctx)

    assert ctx.inputs.tool_msg is original


def test_defaults_and_config_keys():
    default = ContextDietConfig.from_mapping({})
    assert (
        default.trim.exempt_source_prints is True
        and default.trim.source_print_max_lines == 2000
    )
    assert default.compaction.exempt_source_prints is True
    aggressive = ContextDietConfig.from_mapping({"preset": "aggressive"})
    assert (
        aggressive.trim.exempt_source_prints is True
        and aggressive.compaction.exempt_source_prints is True
    )
    off = ContextDietConfig.from_mapping(
        {
            "trim": {"exempt_source_prints": "false", "source_print_max_lines": -5},
            "compaction": {"exempt_source_prints": False},
        }
    )
    assert (
        off.trim.exempt_source_prints is False and off.trim.source_print_max_lines == 0
    )
    assert off.compaction.exempt_source_prints is False
    assert TrimConfig().exempt_source_prints is True


# ─── shell compaction window with real messages ───────────────────────────


class _FakeContext:
    def __init__(self, messages: list[Any]):
        self._messages = list(messages)

    def get_messages(self, *args, **kwargs):
        return list(self._messages)

    def set_messages(self, messages, *args, **kwargs):
        self._messages = list(messages)

    def __len__(self):
        return len(self._messages)

    def session_id(self):
        return "sess"

    def context_id(self):
        return "ctx"

    def workspace_dir(self):
        return ""

    def token_counter(self):
        return None

    def offload_messages(self, handle, messages):
        self.offloaded = getattr(self, "offloaded", {})
        self.offloaded[handle] = list(messages)


def _round(idx: int, command: str, result: str) -> list[Any]:
    call = ToolCall(
        id=f"call_{idx}",
        type="function",
        name="bash",
        arguments=json.dumps({"command": command}),
    )
    return [
        AssistantMessage(content="", tool_calls=[call]),
        ToolMessage(content=result, tool_call_id=f"call_{idx}", name="bash"),
    ]


def _shell_window(exempt: bool) -> ContextDietShellWindowProcessor:
    pairs = build_compaction_processors(
        CompactionConfig(
            shell_keep_last_k=1, min_offload_chars=10_000, exempt_source_prints=exempt
        )
    )
    assert pairs[0][0] == "ContextDietShellWindowProcessor"
    return ContextDietShellWindowProcessor(pairs[0][1])


@pytest.mark.asyncio
async def test_shell_window_leaves_source_prints_alone():
    proc = _shell_window(exempt=True)
    source = "Command: sed -n '1,40p' pkg/a.py\nStdout: " + _code(40) + "\nExit Code: 0"
    messages = [UserMessage(content="fix it")]
    messages += _round(1, "cd /app && sed -n '1,40p' pkg/a.py", source)
    messages += _round(
        2,
        "cd /app && python -m pytest -q",
        "Command: pytest\nStdout: 1 failed\nExit Code: 1",
    )
    new = _round(3, "cd /app && ls", "Command: ls\nStdout: a\nExit Code: 0")
    ctx = _FakeContext(messages)

    event, _ = await proc.on_add_messages(ctx, new)

    stored = ctx.get_messages()
    assert stored[2].content == source  # the source print stays verbatim
    assert (
        "[context-diet] older result compacted" in stored[4].content
    )  # the test run is folded
    assert event is not None and event.messages_to_modify == [4]


@pytest.mark.asyncio
async def test_shell_window_switch_restores_the_old_behaviour():
    proc = _shell_window(exempt=False)
    source = "Command: sed -n '1,40p' pkg/a.py\nStdout: " + _code(40) + "\nExit Code: 0"
    messages = _round(1, "cd /app && sed -n '1,40p' pkg/a.py", source)
    new = _round(2, "cd /app && ls", "Command: ls\nStdout: a\nExit Code: 0")
    ctx = _FakeContext(messages)

    await proc.on_add_messages(ctx, new)

    assert "[context-diet] older result compacted" in ctx.get_messages()[1].content


def test_default_compaction_config_passes_the_switch_to_the_window():
    pairs = build_compaction_processors(CompactionConfig())
    assert pairs[0][1].exempt_source_prints is True
