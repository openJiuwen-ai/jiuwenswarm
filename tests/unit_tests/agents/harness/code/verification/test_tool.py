# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RunTestsTool end to end, spill location, evidence parsing and the card."""

from __future__ import annotations

import contextvars
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.tool.schema import ToolOutput

from jiuwenswarm.agents.harness.code.verification import (
    RunTestsTool,
    parse_run_tests_evidence,
)

from .conftest import write


async def _run(tool: RunTestsTool, **inputs) -> ToolOutput:
    return await tool.invoke(inputs)


@pytest.mark.asyncio
async def test_tool_maps_files_and_returns_structured_evidence(py_repo, tmp_path):
    tool = RunTestsTool(lambda: str(py_repo), spill_dir=str(tmp_path / "spill"))
    out = await _run(tool, files=["calc/ops.py"])
    assert out.success is True
    assert out.data["outcome"] == "passed" and out.data["passed_languages"] == ["python"]
    assert out.data["results"][0]["targets"] == ["tests/test_ops.py"]
    assert out.data["content"].startswith("run_tests: outcome=passed passed_languages=python")



@pytest.mark.asyncio
async def test_tool_falls_back_to_whole_suite_with_a_note(py_repo):
    tool = RunTestsTool(lambda: str(py_repo), spill=False, fallback_whole_suite=True)
    out = await _run(tool, files=["calc/util.py"])
    assert out.data["results"][0]["targets"] == []
    assert "no focused python tests mapped" in out.data["content"]


@pytest.mark.asyncio
async def test_tool_without_fallback_reports_no_tests_and_asks_for_tests(py_repo):
    tool = RunTestsTool(lambda: str(py_repo), spill=False)
    out = await _run(tool, files=["calc/util.py"])
    assert out.success is True and out.data["outcome"] == "no_tests"
    assert out.data["results"][0]["command"] == ""  # nothing was executed
    assert "pass `tests` explicitly" in out.data["content"]
    assert parse_run_tests_evidence(out).outcome == "no_tests"



@pytest.mark.asyncio
async def test_tool_explicit_tests_and_failures_are_a_successful_call(py_repo):
    write(py_repo, "calc/ops.py", "def add(a, b):\n    return 0\n")
    tool = RunTestsTool(lambda: str(py_repo), spill=False)
    out = await _run(tool, tests=["tests/test_ops.py::test_add"])
    assert out.success is True and out.data["outcome"] == "failed"
    assert out.data["results"][0]["failing"] == ["tests/test_ops.py::test_add"]



@pytest.mark.asyncio
async def test_tool_without_framework_is_an_error(tmp_path):
    (tmp_path / "empty").mkdir()
    out = await _run(RunTestsTool(lambda: str(tmp_path / "empty")))
    assert out.success is False and "no test framework detected" in out.error



def test_default_spill_path_is_readable_through_the_code_sandbox(tmp_path):
    from openjiuwen.core.sys_operation.cwd import init_cwd
    from openjiuwen.core.sys_operation.local.fs_operation import FsOperation

    from jiuwenswarm.agents.harness.code.verification.runner import (
        default_spill_root,
        spill,
    )

    project = tmp_path / "repo"
    agent_ws = tmp_path / "data" / "agent" / "workspace"
    project.mkdir()
    agent_ws.mkdir(parents=True)

    def run():
        init_cwd(str(project), project_root=str(project), workspace=str(agent_ws))
        path = spill("full output", default_spill_root(), "pytest", 0)
        assert path
        fs = FsOperation.__new__(FsOperation)
        fs._run_config = SimpleNamespace(restrict_to_sandbox=True, sandbox_root=None)
        resolved = fs._resolve_path(path)
        assert resolved.read_text() == "full output"
        assert not FsOperation._is_within(resolved, project.resolve())

    contextvars.copy_context().run(run)



def test_evidence_from_tooloutput_dict_and_text():
    data = {"content": "run_tests: outcome=passed passed_languages=python,go", "outcome": "passed",
            "passed_languages": ["python", "go"], "results": [{"command": "py -m pytest"}]}
    ev = parse_run_tests_evidence(ToolOutput(success=True, data=data))
    assert ev.success and ev.passed_languages == {"python", "go"}
    assert parse_run_tests_evidence({"data": data}).passed_languages == {"python", "go"}
    ev = parse_run_tests_evidence("run_tests: outcome=failed passed_languages=-\n...")
    assert ev.outcome == "failed" and not ev.passed_languages
    assert parse_run_tests_evidence(ToolOutput(success=False, error="x")) is None
    assert parse_run_tests_evidence("unrelated text") is None



def test_card_is_direct_and_compact():
    from openjiuwen.core.foundation.tool import ToolExposure

    from jiuwenswarm.agents.harness.code.verification import build_run_tests_card

    card = build_run_tests_card(agent_id="a")
    assert card.name == "run_tests" and card.exposure == ToolExposure.DIRECT
    assert len(card.description) < 800  # description budget




# ─── review fixes: containment, option safety, allow-list ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tests", "files"])
async def test_entries_outside_the_root_are_rejected(py_repo, tmp_path, field):
    outside = write(tmp_path, "outside/test_evil.py", "def test_x():\n    open('pwned', 'w')\n")
    tool = RunTestsTool(lambda: str(py_repo), spill=False)
    for entry in (str(outside), "../outside/test_evil.py"):
        out = await _run(tool, **{field: [entry]})
        assert out.success is False and "outside the project root" in out.error
    assert not (tmp_path / "pwned").exists() and not (py_repo / "pwned").exists()


@pytest.mark.asyncio
async def test_symlink_escaping_the_root_is_rejected(py_repo, tmp_path):
    write(tmp_path, "elsewhere/test_far.py", "def test_x():\n    pass\n")
    (py_repo / "tests" / "link").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    out = await _run(RunTestsTool(lambda: str(py_repo), spill=False), tests=["tests/link/test_far.py"])
    assert out.success is False and "outside the project root" in out.error


@pytest.mark.asyncio
async def test_option_like_targets_are_rejected(py_repo):
    out = await _run(RunTestsTool(lambda: str(py_repo), spill=False), tests=["--co"])
    assert out.success is False and "looks like an option" in out.error


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [["-p", "x"], ["-pxyz"], ["--rootdir=/"], ["-o", "addopts=-p x"], ["--pyargs"]])
async def test_denied_extra_args_are_rejected(py_repo, extra):
    out = await _run(RunTestsTool(lambda: str(py_repo), spill=False), tests=["tests/test_ops.py"], extra_args=extra)
    assert out.success is False and "Allowed pytest options" in out.error


@pytest.mark.asyncio
async def test_allowed_extra_args_still_work(py_repo):
    out = await _run(RunTestsTool(lambda: str(py_repo), spill=False), tests=["tests/test_ops.py"], extra_args=["-k", "zero"])
    assert out.success is True and out.data["outcome"] == "passed"
    assert out.data["results"][0]["counts"].get("passed") == 1


@pytest.mark.asyncio
async def test_targets_follow_a_double_dash_for_pytest(py_repo):
    out = await _run(RunTestsTool(lambda: str(py_repo), spill=False), tests=["tests/test_ops.py::test_add"])
    command = out.data["results"][0]["command"].split()
    assert command[command.index("--") + 1] == "tests/test_ops.py::test_add"


def test_contain_entry_keeps_go_patterns_and_node_ids(tmp_path):
    from jiuwenswarm.agents.harness.code.verification.tool import contain_entry

    (tmp_path / "pkg").mkdir()
    assert contain_entry(tmp_path, "./...") == ("./...", "")
    assert contain_entry(tmp_path, "./pkg/...") == ("./pkg/...", "")
    assert contain_entry(tmp_path, "./pkg") == ("./pkg", "")
    assert contain_entry(tmp_path, "tests/test_a.py::TestX::test_y") == ("tests/test_a.py::TestX::test_y", "")
    assert contain_entry(tmp_path, "../x")[0] is None


# ─── review fixes: per-language evidence ──────────────────────────────────


def test_python_pass_survives_a_js_suite_without_tests():
    data = {
        "outcome": "no_tests",
        "passed_languages": ["python"],
        "language_outcomes": {"python": "passed", "typescript/javascript": "no_tests"},
        "results": [],
    }
    evidence = parse_run_tests_evidence(ToolOutput(success=True, data=data))
    assert evidence.outcome == "passed" and evidence.passed_languages == frozenset({"python"})
    data["language_outcomes"]["typescript/javascript"] = "failed"
    assert parse_run_tests_evidence(ToolOutput(success=True, data=data)).outcome == "failed"


def test_card_declares_outer_timeout_above_the_run_budget() -> None:
    """agent-core's default 300 s tool deadline must not cut a longer run_tests budget short."""
    from jiuwenswarm.agents.harness.code.verification.tool import (
        OUTER_TIMEOUT_MARGIN_SECONDS,
        RunTestsTool,
    )

    tool = RunTestsTool(timeout=600)
    declared = tool.card.properties["resilience"]["timeout_s"]
    assert declared == 600 + OUTER_TIMEOUT_MARGIN_SECONDS


async def test_budget_is_shared_across_frameworks(tmp_path, monkeypatch) -> None:
    """Frameworks run one after another share one budget; an exhausted budget skips the rest."""
    import jiuwenswarm.agents.harness.code.verification.tool as tool_mod
    from jiuwenswarm.agents.harness.code.verification.runner import RunResult

    seen: list[float] = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(tool_mod.time, "monotonic", lambda: clock["t"])

    async def fake_run(fw, root, targets, extra, *, timeout, **_kw):
        seen.append(timeout)
        clock["t"] += 50.0  # each framework run consumes 50 s
        return RunResult(fw.name, fw.language, ["x"], "passed", 0, 50.0, counts={"passed": 1})

    class _Fw:
        def __init__(self, name, language):
            self.name, self.language = name, language

    monkeypatch.setattr(tool_mod, "run_framework", fake_run)
    monkeypatch.setattr(tool_mod, "discover_tests", lambda root: type("D", (), {"frameworks": [1]})())
    monkeypatch.setattr(
        tool_mod, "plan_runs",
        lambda *a, **k: ([(_Fw("pytest", "python"), []), (_Fw("jest", "typescript/javascript"), []),
                          (_Fw("go", "go"), [])], [], []),
    )
    tool = tool_mod.RunTestsTool(root_resolver=lambda: str(tmp_path), timeout=100, spill=False)
    out = await tool.invoke({})
    assert seen == [100.0, 50.0]  # third framework not started: budget used up
    outcomes = [r["outcome"] for r in out.data["results"]]
    assert outcomes[-1] == "timeout"
