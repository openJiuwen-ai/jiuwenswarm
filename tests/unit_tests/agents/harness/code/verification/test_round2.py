# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the second adversarial review of run_tests.

Each case reproduces a verified finding: extra_args allow-list bypasses,
sandbox bypass, false passes, unbounded output, stray children holding the
output pipe, inherited runner variables and the spill-cap race.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.code.verification import runner as runner_mod
from jiuwenswarm.agents.harness.code.verification.argpolicy import (
    check_extra_args,
    npm_script_runner,
)
from jiuwenswarm.agents.harness.code.verification.discovery import Framework
from jiuwenswarm.agents.harness.code.verification.runner import classify
from jiuwenswarm.agents.harness.code.verification.tool import (
    RunTestsTool,
    sys_operation_is_sandboxed,
)

from .conftest import write

# ─── extra_args allow-list ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("runner", "extra"),
    [
        # pytest: clustered short flag, argfile, output files, paste service, abbreviations
        ("pytest", ["-qc", "/tmp/evil.ini"]),
        ("pytest", ["-vp", "evilplug"]),
        ("pytest", ["@/tmp/args.txt"]),
        ("pytest", ["--junitxml=/tmp/CLOBBER.xml"]),
        ("pytest", ["--debug=/tmp/DEBUG.log"]),
        ("pytest", ["--pastebin=all"]),
        ("pytest", ["--resultlog=/tmp/x"]),
        ("pytest", ["--rootd=/tmp"]),
        ("pytest", ["--override-in=addopts=-p x"]),
        ("pytest", ["-p", "x"]),
        ("pytest", ["-c=/tmp/evil.ini"]),
        ("pytest", ["-o", "addopts=-p x"]),
        ("pytest", ["-W", "error::evil.Category"]),
        ("pytest", ["--co"]),
        ("pytest", ["/elsewhere/test_x.py"]),  # bare positional = extra target
        ("pytest", ["-k", "--config=x"]),  # separate value that looks like an option
        ("pytest", ["--tb=evil"]),
        # jest: aliases and kebab-case forms of code-loading options
        ("jest", ["--setupFilesAfterEnv=/tmp/x.js"]),
        ("jest", ["--setup-files=/tmp/x.js"]),
        ("jest", ["--env=/tmp/x.js"]),
        ("jest", ["--filter=/tmp/x.js"]),
        ("jest", ["--runner=/tmp/x.js"]),
        ("jest", ["--testSequencer=/tmp/x.js"]),
        ("jest", ["--root-dir=/tmp"]),
        ("jest", ["--config", "/tmp/x.js"]),
        ("jest", ["-c", "/tmp/x.js"]),
        ("jest", ["--coverage"]),
        ("jest", ["--json"]),
        # vitest
        ("vitest", ["--config=/tmp/x.ts"]),
        ("vitest", ["--setup-files=/tmp/x.ts"]),
        ("vitest", ["--reporter=/tmp/x.ts"]),
        # go: double-dash forms and linker tricks
        ("go", ["--toolexec=/tmp/x"]),
        ("go", ["-toolexec", "/tmp/x"]),
        ("go", ["--exec=/tmp/x"]),
        ("go", ["-ldflags=-linkmode=external -extld=/tmp/x"]),
        ("go", ["-overlay=/tmp/o.json"]),
        ("go", ["/elsewhere/..."]),
        ("go", ["-v", "true"]),  # "true" would be read as a package
        ("go", ["-json"]),
        # cargo and the libtest harness
        ("cargo", ["--config", "build.rustc-wrapper='/tmp/x'"]),
        ("cargo", ["-Z", "unstable-options"]),
        ("cargo", ["--manifest-path=/tmp/Cargo.toml"]),
        ("cargo", ["--target-dir=/tmp/t"]),
        ("cargo", ["--", "--logfile", "/tmp/x"]),
        ("cargo", ["../escape"]),
        ("unittest", ["-s", "/elsewhere"]),
        ("npm", ["--anything"]),
    ],
)
def test_refused_extra_args(runner, extra):
    assert check_extra_args(runner, extra) is not None


@pytest.mark.parametrize(
    ("runner", "extra"),
    [
        ("pytest", ["-k", "not slow"]),
        ("pytest", ["-x", "--maxfail=2"]),
        ("pytest", ["-vv"]),
        ("pytest", ["-vx"]),
        ("pytest", ["-kname"]),
        ("pytest", ["-rA", "--tb=line", "--durations=5"]),
        ("pytest", ["-n", "auto"]),
        ("pytest", ["--deselect", "tests/test_a.py::test_slow"]),
        ("jest", ["-t", "adds"]),
        ("jest", ["--testNamePattern=adds numbers", "--coverage=false", "-i"]),
        ("vitest", ["-t", "x", "--bail=1"]),
        ("go", ["-run", "TestX", "-v", "-count=1", "--timeout=30s"]),
        ("go", ["-v=true", "-short"]),
        ("cargo", ["-q", "my_test", "--", "--exact", "--test-threads=1"]),
        ("unittest", ["-v", "-k", "test_add"]),
    ],
)
def test_allowed_extra_args(runner, extra):
    assert check_extra_args(runner, extra) is None


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("jest", "jest"),
        ("jest --ci", "jest"),
        ("vitest run", "vitest"),
        ("vitest", "vitest"),
        ("cross-env NODE_ENV=test jest", "jest"),
        ("NODE_ENV=test jest", "jest"),
        ("node node_modules/jest/bin/jest.js", None),
        ("jest && rm -rf build", None),
        ("mocha", None),
        ("vitest --watch", None),
    ],
)
def test_npm_script_runner(tmp_path, script, expected):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": script}}))
    assert npm_script_runner(tmp_path, "test") == expected


async def _invoke(tool, **inputs):
    return await tool.invoke(inputs)


@pytest.mark.asyncio
async def test_npm_script_that_is_not_a_known_runner_refuses_extra_args(tmp_path):
    write(tmp_path, "package.json", '{"scripts": {"test": "node node_modules/jest/bin/jest.js"}}')
    out = await _invoke(RunTestsTool(lambda: str(tmp_path), spill=False), extra_args=["--config=/tmp/cfg.js"])
    assert out.success is False and "not supported for the npm script" in out.error


@pytest.mark.asyncio
async def test_real_pytest_bypasses_do_not_load_outside_code(py_repo, tmp_path):
    """The verifier's PWNED cases, run against real pytest: nothing outside the repo loads."""
    out_dir = tmp_path / "outside"
    write(out_dir, "evilplug.py", f"open({str(tmp_path / 'PWNED')!r}, 'w').close()\n")
    write(out_dir, "evil.ini", "[pytest]\naddopts = -p evilplug\n")
    write(out_dir, "args.txt", f"-c\n{out_dir / 'evil.ini'}\n")
    tool = RunTestsTool(lambda: str(py_repo), spill=False)
    for extra in (["-qc", str(out_dir / "evil.ini")], [f"@{out_dir / 'args.txt'}"],
                  [f"--junitxml={tmp_path / 'CLOBBER.xml'}"], [str(out_dir / "evilplug.py")]):
        result = await _invoke(tool, tests=["tests/test_ops.py"], extra_args=extra)
        assert result.success is False and "Allowed pytest options" in result.error
    assert not (tmp_path / "PWNED").exists() and not (tmp_path / "CLOBBER.xml").exists()


# ─── sandbox mode ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_refuses_in_sandbox_mode(py_repo, tmp_path):
    write(py_repo, "tests/conftest.py", f"open({str(tmp_path / 'HOST_RAN')!r}, 'w').close()\n")
    out = await _invoke(RunTestsTool(lambda: str(py_repo), spill=False, sandboxed=lambda: True),
                        tests=["tests/test_ops.py"])
    assert out.success is False and "sandbox" in out.error
    assert not (tmp_path / "HOST_RAN").exists()


@pytest.mark.asyncio
async def test_tool_refuses_when_the_sandbox_state_is_unknown(py_repo):
    def broken() -> bool:
        raise RuntimeError("card missing")

    out = await _invoke(RunTestsTool(lambda: str(py_repo), spill=False, sandboxed=broken), tests=["tests/test_ops.py"])
    assert out.success is False and "sandbox" in out.error


def test_sys_operation_is_sandboxed():
    from openjiuwen.core.sys_operation.base import OperationMode

    assert sys_operation_is_sandboxed(SimpleNamespace(mode=OperationMode.SANDBOX)) is True
    assert sys_operation_is_sandboxed(SimpleNamespace(mode="sandbox")) is True
    assert sys_operation_is_sandboxed(SimpleNamespace(card=SimpleNamespace(mode=OperationMode.SANDBOX))) is True
    assert sys_operation_is_sandboxed(SimpleNamespace(mode=OperationMode.LOCAL)) is False
    assert sys_operation_is_sandboxed(None) is False


def test_adapter_does_not_register_run_tests_in_sandbox_mode(monkeypatch, tmp_path):
    from openjiuwen.core.sys_operation.base import OperationMode

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_code.get_config",
        lambda: {"modes": {"code": {"tools": ["run_tests"]}}},
    )
    adapter = JiuwenSwarmCodeAdapter()
    adapter._project_dir = str(tmp_path)
    adapter._sys_operation_card = SimpleNamespace(mode=OperationMode.SANDBOX)
    assert adapter._build_run_tests_tool("agent-id") is None
    adapter._sys_operation_card = SimpleNamespace(mode=OperationMode.LOCAL)
    tool = adapter._build_run_tests_tool("agent-id")
    assert isinstance(tool, RunTestsTool) and tool._is_sandboxed() is False
    # The SysOperation is replaced by a sandboxed one later: the tool re-checks.
    adapter._sys_operation_card = SimpleNamespace(mode=OperationMode.SANDBOX)
    assert tool._is_sandboxed() is True


def test_swarm_element_is_empty_in_sandbox_mode(tmp_path):
    from openjiuwen.core.sys_operation.base import OperationMode
    from openjiuwen.harness.schema.build_context import PARENT_SYS_OPERATION_EXTRAS_KEY

    from jiuwenswarm.agents.swarm.providers import tools as swarm_tools

    def ctx(mode):
        return SimpleNamespace(
            extras={PARENT_SYS_OPERATION_EXTRAS_KEY: SimpleNamespace(mode=mode)},
            project_dir=str(tmp_path), language="en", workspace=None,
        )

    params = {"enabled": True, "settings": {}}
    assert swarm_tools.build_run_tests(params, ctx(OperationMode.SANDBOX)) == []
    built = swarm_tools.build_run_tests(params, ctx(OperationMode.LOCAL))
    assert len(built) == 1 and isinstance(built[0], RunTestsTool)


# ─── false passes ────────────────────────────────────────────────────────

_PY = Framework("pytest", "python", "pytest.ini")
_GO = Framework("go", "go", "go.mod")
_UT = Framework("unittest", "python", "")


@pytest.mark.parametrize(
    ("fw", "text", "outcome"),
    [
        (_PY, "x\n1 xfailed in 0.01s\n", "no_tests"),
        (_PY, "X\n1 xpassed in 0.01s\n", "no_tests"),
        (_PY, "x.\n1 passed, 1 xfailed in 0.01s\n", "passed"),
        (_GO, "ok  \texample.com/m/pkg\t0.004s [no tests to run]\n", "no_tests"),
        (_GO, "?   \texample.com/m/a\t[no test files]\nok  \texample.com/m/pkg\t0.004s [no tests to run]\n", "no_tests"),
        (_GO, "ok  \texample.com/m/pkg\t0.004s\n", "passed"),
        (_UT, "x\n----\nRan 1 test in 0.000s\n\nOK (expected failures=1)\n", "no_tests"),
    ],
)
def test_nothing_verified_is_not_a_pass(fw, text, outcome):
    assert classify(fw, 0, text)[0] == outcome


@pytest.mark.asyncio
async def test_real_xfail_only_run_is_not_passed(py_repo):
    write(py_repo, "tests/test_xf.py", "import pytest\n\n@pytest.mark.xfail\ndef test_x():\n    assert 0\n")
    out = await _invoke(RunTestsTool(lambda: str(py_repo), spill=False), tests=["tests/test_xf.py"])
    assert out.data["outcome"] == "no_tests" and out.data["passed_languages"] == []


# ─── output and process handling ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_output_flood_is_bounded_in_memory_and_on_disk(tmp_path):
    code = "import sys\nc='x'*1023+'\\n'\nfor _ in range(24*1024): sys.stdout.write(c)\nprint('\\n1 passed in 0.1s')\n"
    sink = runner_mod._OutputSink(tmp_path / "spill.log", 1024 * 1024)
    exit_code, text, timed_out = await runner_mod._run([sys.executable, "-c", code], tmp_path, 60, sink)
    assert exit_code == 0 and not timed_out
    assert len(text) <= runner_mod._MEM_HEAD_BYTES + runner_mod._MEM_TAIL_BYTES + 200
    assert "not kept in memory" in text and text.rstrip().endswith("1 passed in 0.1s")
    data = (tmp_path / "spill.log").read_bytes()
    assert len(data) <= 1024 * 1024 and b"dropped to respect the spill cap" in data
    assert data.rstrip().endswith(b"1 passed in 0.1s")


@pytest.mark.asyncio
async def test_small_output_is_spilled_whole(tmp_path):
    sink = runner_mod._OutputSink(tmp_path / "s.log", 1024 * 1024)
    await runner_mod._run([sys.executable, "-c", "print('a' * 5000)"], tmp_path, 60, sink)
    assert (tmp_path / "s.log").read_text() == "a" * 5000 + "\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX sessions")
@pytest.mark.asyncio
async def test_helper_holding_stdout_does_not_turn_a_pass_into_a_timeout(tmp_path):
    pid_file = tmp_path / "pid"
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen(['/bin/sleep', '30'], start_new_session=True)\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "print('1 passed in 0.01s')\n"
    )
    started = time.monotonic()
    exit_code, text, timed_out = await runner_mod._run([sys.executable, "-c", code], tmp_path, 20, None)
    elapsed = time.monotonic() - started
    try:
        assert exit_code == 0 and not timed_out and "1 passed" in text
        assert elapsed < runner_mod._PIPE_DRAIN_SECONDS + 3
    finally:
        os.kill(int(pid_file.read_text()), signal.SIGKILL)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_background_children_in_the_group_are_killed(tmp_path):
    pid_file = tmp_path / "pid"
    code = (
        "import subprocess\n"
        "p = subprocess.Popen(['/bin/sleep', '30'], stdout=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
    )
    exit_code, _text, timed_out = await runner_mod._run([sys.executable, "-c", code], tmp_path, 20, None)
    assert exit_code == 0 and not timed_out
    pid = int(pid_file.read_text())
    alive = True
    for _ in range(40):  # the orphan is reaped by init shortly after SIGKILL
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
            break
        await asyncio.sleep(0.05)
    assert not alive


@pytest.mark.asyncio
async def test_timeout_still_reports_timeout(tmp_path):
    exit_code, _text, timed_out = await runner_mod._run(
        [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(30)"], tmp_path, 1, None
    )
    assert timed_out and exit_code is None


# ─── environment and spill directory ─────────────────────────────────────


def test_runner_option_variables_are_scrubbed(monkeypatch):
    for key in ("PYTEST_ADDOPTS", "NODE_OPTIONS", "GOFLAGS", "RUSTC_WRAPPER"):
        monkeypatch.setenv(key, "-p evil")
    env = runner_mod._clean_env()
    assert not {"PYTEST_ADDOPTS", "NODE_OPTIONS", "GOFLAGS", "RUSTC_WRAPPER"} & set(env)
    assert runner_mod._clean_env(inherit_runner_env=True)["PYTEST_ADDOPTS"] == "-p evil"


def test_spill_cap_spares_young_files_of_concurrent_calls(tmp_path):
    old = tmp_path / "old.log"
    old.write_bytes(b"o" * 600)
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    young = tmp_path / "young.log"
    young.write_bytes(b"y" * 600)
    mine = tmp_path / "mine.log"
    mine.write_bytes(b"m" * 600)
    runner_mod._enforce_cap(tmp_path, 1000, keep=mine)
    assert not old.exists() and young.exists() and mine.exists()


@pytest.mark.asyncio
async def test_spill_files_have_unique_names(py_repo, tmp_path):
    tool = RunTestsTool(lambda: str(py_repo), spill_dir=str(tmp_path / "spill"))
    a, b = await asyncio.gather(
        _invoke(tool, tests=["tests/test_ops.py"]), _invoke(tool, tests=["tests/test_ops.py"])
    )
    paths = {a.data["results"][0]["spill_path"], b.data["results"][0]["spill_path"]}
    assert len(paths) == 2 and all(Path(p).exists() for p in paths)
