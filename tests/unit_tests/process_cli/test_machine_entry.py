# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
import builtins
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from jiuwenswarm.channels.process_cli import machine_entry
from jiuwenswarm.channels.process_cli import main as cli_main
from jiuwenswarm.channels.process_cli.machine_io import MachineInputError, OneShotWriter
from jiuwenswarm.channels.process_cli.protocol import (
    OneShotRunInput,
    OneShotRunResult,
    WorkspaceSpec,
    decode_jsonl,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MACHINE_MODULE = "jiuwenswarm.channels.process_cli.machine"
_GUARDED_MAIN = """
import importlib.abc
import sys

class NoRuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = (
            'jiuwenswarm.runtime',
            'jiuwenswarm.channels.process_cli.machine',
            'jiuwenswarm.channels.process_cli.client',
            'jiuwenswarm.channels.process_cli.repl',
            'jiuwenswarm.channels.process_cli.app',
        )
        for prefix in blocked:
            if fullname == prefix or fullname.startswith(prefix + '.'):
                raise AssertionError('Unexpected execution import: ' + fullname)
        return None

sys.meta_path.insert(0, NoRuntimeImports())
from jiuwenswarm.channels.process_cli.main import main
main()
"""

_FAKE_MACHINE_MAIN = """
import os
import subprocess
import sys
import types

from jiuwenswarm.channels.process_cli.protocol import OneShotRunResult

async def run_with_signals(run_input, writer):
    writer.session_id = 'resolved-session'
    if os.environ.get('MACHINE_TEST_DIAGNOSTICS') == '1':
        print('PYTHON_DIAGNOSTIC', flush=True)
        os.write(1, b'NATIVE_DIAGNOSTIC\\n')
        subprocess.run(
            [sys.executable, '-c',
             'import os; os.write(1, b"CHILD_DIAGNOSTIC\\\\n")'],
            check=True,
        )
    if os.environ.get('MACHINE_TEST_EVENT') == '1':
        event = types.SimpleNamespace(
            request_id='internal-request', session_id='internal-session',
            event_type='chat.delta', payload={'delta': 'hello'},
        )
        writer.write_event(event)
    return OneShotRunResult(
        sequence=99, request_id='placeholder', session_id='resolved-session',
        status='completed', exit_code=0, output='done',
    )

module = types.ModuleType('jiuwenswarm.channels.process_cli.machine')
module.run_with_signals = run_with_signals
sys.modules[module.__name__] = module
from jiuwenswarm.channels.process_cli.main import main
main()
"""


def _document(**overrides: Any) -> str:
    record = {
        "schema_version": "0.1",
        "type": "run",
        "request_id": "external-request",
        "input": "hello",
    }
    record.update(overrides)
    return json.dumps(record)


def _success(writer: OneShotWriter) -> OneShotRunResult:
    writer.session_id = "resolved-session"
    return OneShotRunResult(
        sequence=99,
        request_id="placeholder",
        session_id="resolved-session",
        status="completed",
        exit_code=0,
        output="done",
    )


def _install_runner(monkeypatch: pytest.MonkeyPatch, runner: Any) -> None:
    module = ModuleType(MACHINE_MODULE)
    module.run_with_signals = runner
    monkeypatch.setitem(sys.modules, MACHINE_MODULE, module)


def _isolated_stdio(monkeypatch: pytest.MonkeyPatch, document: str) -> io.StringIO:
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(document))
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    return output


def _environment(**overrides: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment.update(overrides)
    return environment


def _run_main(
    script: str, arguments: list[str], *, document: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *arguments],
        input=document,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=PROJECT_ROOT,
        env=_environment(),
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize("arguments", [["--run-json", "-"], ["--run-json=run.json"]])
def test_machine_option_dispatches_directly_without_legacy_execution(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, bool]] = []

    def execute(source: str, *, conflicting_arguments: bool = False) -> int:
        calls.append((source, conflicting_arguments))
        return 7

    monkeypatch.setattr(machine_entry, "execute_source", execute)
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-process", *arguments])

    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == 7
    source = "-" if len(arguments) == 2 else "run.json"
    assert calls == [(source, False)]


@pytest.mark.parametrize(
    "legacy_arguments",
    [
        ["prompt"],
        ["--session", "existing-session"],
        ["--cwd", "."],
        ["--project-dir", "."],
        ["--trusted-dir", "."],
        ["--mode", "code.normal"],
        ["--work-mode", "code"],
        ["--output", "human"],
        ["--timeout", "10"],
        ["--show-tools"],
        ["--show-reasoning"],
        ["--_interactive-worker"],
        ["--_operation", "chat"],
    ],
)
def test_machine_entry_rejects_every_explicit_legacy_argument(
    legacy_arguments: list[str],
) -> None:
    result = _run_main(
        _GUARDED_MAIN,
        ["--run-json", "-", *legacy_arguments],
        document=_document(),
    )

    assert result.returncode == 2, result.stderr
    records = decode_jsonl(result.stdout)
    assert len(records) == 1
    assert records[0].error.code == "INVALID_INPUT"
    assert "legacy" in records[0].error.message
    assert "Traceback" not in result.stderr


def test_machine_option_does_not_change_legacy_parser_defaults_or_help() -> None:
    parser = cli_main.build_parser()
    defaults = parser.parse_args([])

    assert defaults.prompt is None
    assert defaults.run_json is None
    assert defaults.mode == "code.normal"
    assert defaults.work_mode == "code"
    assert defaults.output == "human"
    assert defaults.trusted_dir == []
    assert defaults.timeout is None
    help_text = parser.format_help()
    for option in ("--run-json", "--session", "--cwd", "--output", "--show-tools"):
        assert option in help_text
    assert "--_interactive-worker" not in help_text


def test_help_exits_without_machine_or_runtime_imports() -> None:
    result = _run_main(_GUARDED_MAIN, ["--run-json", "-", "--help"])

    assert result.returncode == 0, result.stderr
    assert "--run-json" in result.stdout
    assert "--output" in result.stdout
    assert result.stderr == ""


def test_no_arguments_still_enters_the_legacy_repl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def run_repl(args: Any) -> int:
        calls.append(args.output)
        return 9

    module = ModuleType("jiuwenswarm.channels.process_cli.repl")
    module.run_repl = run_repl
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-process"])

    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == 9
    assert calls == ["human"]


def test_legacy_prompt_still_calls_the_existing_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    async def run(args: Any, *, stdout: Any, stderr: Any) -> int:
        calls.append((args.prompt, args.output))
        stdout.write("legacy-output")
        return 4

    module = ModuleType("jiuwenswarm.channels.process_cli.app")
    module.run = run
    monkeypatch.setitem(sys.modules, module.__name__, module)
    output = _isolated_stdio(monkeypatch, "")
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-process", "hello", "--output=json"])

    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == 4
    assert calls == [("hello", "json")]
    assert output.getvalue() == "legacy-output"


@pytest.mark.parametrize("source_kind", ["stdin", "file"])
def test_execute_source_reads_one_request_and_runs_once(
    source_kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[OneShotRunInput] = []

    async def run(
        run_input: OneShotRunInput, writer: OneShotWriter
    ) -> OneShotRunResult:
        requests.append(run_input)
        return _success(writer)

    _install_runner(monkeypatch, run)
    output = _isolated_stdio(monkeypatch, _document())
    source = "-"
    if source_kind == "file":
        path = tmp_path / "run.json"
        path.write_text(_document(), encoding="utf-8")
        source = str(path)

    result = machine_entry.execute_source(source)

    assert result == 0
    assert len(requests) == 1
    assert requests[0].input == "hello"
    final = decode_jsonl(output.getvalue())[0]
    assert final.request_id == "external-request"
    assert final.session_id == "resolved-session"
    assert final.sequence == 0


@pytest.mark.parametrize(
    "document,request_id",
    [
        ("", None),
        ("{", None),
        (_document(input=""), "external-request"),
        (_document(schema_version="future"), "external-request"),
        (_document(secret_field="secret-value"), "external-request"),
    ],
)
def test_bad_input_cold_start_has_one_result_and_never_imports_runtime(
    document: str, request_id: str | None
) -> None:
    result = _run_main(_GUARDED_MAIN, ["--run-json", "-"], document=document)

    assert result.returncode == 2, result.stderr
    records = decode_jsonl(result.stdout)
    assert len(records) == 1
    final = records[0]
    assert final.sequence == 0
    assert final.session_id is None
    assert final.status == "failed"
    assert final.exit_code == 2
    assert final.error.code == "INVALID_INPUT"
    assert final.request_id
    if request_id is not None:
        assert final.request_id == request_id
    assert "secret-value" not in result.stdout + result.stderr
    assert "Unexpected execution import" not in result.stdout + result.stderr


def test_workspace_resolves_cwd_project_and_trusted_paths_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "working").mkdir()
    workspace = WorkspaceSpec(
        cwd="working", project_dir="project", trusted_dirs=("trusted",)
    )
    run_input = OneShotRunInput(input="hello", workspace=workspace)

    prepared = machine_entry.prepare_workspace(run_input)

    assert Path.cwd() == tmp_path / "working"
    assert prepared.workspace.cwd == str(tmp_path / "working")
    assert prepared.workspace.project_dir == str(tmp_path / "project")
    assert prepared.workspace.trusted_dirs == (str(tmp_path / "trusted"),)
    assert run_input.workspace == workspace
    assert workspace.cwd == "working"


def test_omitted_workspace_preserves_cwd_and_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_input = OneShotRunInput(input="hello")

    assert machine_entry.prepare_workspace(run_input) is run_input
    assert Path.cwd() == tmp_path


def test_invalid_cwd_is_an_input_error(tmp_path: Path) -> None:
    run_input = OneShotRunInput(
        input="hello", workspace=WorkspaceSpec(cwd=str(tmp_path / "missing"))
    )

    with pytest.raises(MachineInputError, match="workspace"):
        machine_entry.prepare_workspace(run_input)


def test_cwd_is_applied_before_importing_or_starting_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "working").mkdir()
    calls: list[str] = []
    expected_cwd = tmp_path / "working"

    async def run(
        run_input: OneShotRunInput, writer: OneShotWriter
    ) -> OneShotRunResult:
        calls.append("run")
        assert Path.cwd() == expected_cwd
        assert run_input.workspace.project_dir == str(tmp_path / "project")
        return _success(writer)

    _install_runner(monkeypatch, run)
    original_import = builtins.__import__

    def observed_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == MACHINE_MODULE:
            calls.append("import")
            assert Path.cwd() == expected_cwd
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", observed_import)
    output = _isolated_stdio(
        monkeypatch, _document(workspace={"cwd": "working", "project_dir": "project"})
    )

    assert machine_entry.execute_source("-") == 0
    assert calls == ["import", "run"]
    assert decode_jsonl(output.getvalue())[-1].status == "completed"


@pytest.mark.parametrize("error_type", [ImportError, RuntimeError])
def test_bootstrap_import_failure_emits_safe_terminal_result(
    error_type: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    original_import = builtins.__import__

    def broken_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == MACHINE_MODULE:
            raise error_type("secret-bootstrap-detail")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    output = _isolated_stdio(monkeypatch, _document())

    assert machine_entry.execute_source("-") == 1
    records = decode_jsonl(output.getvalue())
    assert len(records) == 1
    assert records[0].error.code == "STARTUP_FAILED"
    assert records[0].request_id == "external-request"
    assert "secret-bootstrap-detail" not in output.getvalue()
    assert "secret-bootstrap-detail" not in sys.stderr.getvalue()


def test_final_result_is_written_after_asyncio_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle: list[str] = []
    generators: list[Any] = []

    async def background() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            lifecycle.append("background-stopped")

    async def observed_generator() -> Any:
        try:
            yield "ready"
        finally:
            lifecycle.append("generator-closed")

    async def run(
        run_input: OneShotRunInput, writer: OneShotWriter
    ) -> OneShotRunResult:
        asyncio.create_task(background())
        await asyncio.sleep(0)
        generator = observed_generator()
        generators.append(generator)
        await anext(generator)
        lifecycle.append("run-returned")
        return _success(writer)

    class OrderedOutput(io.StringIO):
        def write(self, value: str) -> int:
            assert "background-stopped" in lifecycle
            assert "generator-closed" in lifecycle
            lifecycle.append("result-written")
            return super().write(value)

    _install_runner(monkeypatch, run)
    _isolated_stdio(monkeypatch, _document())
    output = OrderedOutput()
    monkeypatch.setattr(sys, "stdout", output)

    assert machine_entry.execute_source("-") == 0
    assert lifecycle[0] == "run-returned"
    assert lifecycle[-1] == "result-written"
    assert decode_jsonl(output.getvalue())[-1].status == "completed"


def test_python_native_and_child_diagnostics_do_not_pollute_jsonl() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _FAKE_MACHINE_MAIN, "--run-json", "-"],
        input=_document(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=PROJECT_ROOT,
        env=_environment(MACHINE_TEST_DIAGNOSTICS="1", MACHINE_TEST_EVENT="1"),
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    records = decode_jsonl(result.stdout)
    assert len(records) == 2
    assert records[0].event_type == "chat.delta"
    assert records[-1].status == "completed"
    for marker in ("PYTHON_DIAGNOSTIC", "NATIVE_DIAGNOSTIC", "CHILD_DIAGNOSTIC"):
        assert marker in result.stderr
        assert marker not in result.stdout


@pytest.mark.parametrize("emit_event", ["0", "1"])
def test_closed_output_pipe_exits_without_traceback(emit_event: str) -> None:
    with subprocess.Popen(
        [sys.executable, "-c", _FAKE_MACHINE_MAIN, "--run-json", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=PROJECT_ROOT,
        env=_environment(MACHINE_TEST_EVENT=emit_event),
    ) as process:
        assert process.stdout is not None
        process.stdout.close()
        process.stdout = None
        _, stderr = process.communicate(input=_document(), timeout=20)

    assert process.returncode == 1, stderr
    assert "Traceback" not in stderr
    assert "Exception ignored" not in stderr
    assert "BrokenPipeError:" not in stderr
