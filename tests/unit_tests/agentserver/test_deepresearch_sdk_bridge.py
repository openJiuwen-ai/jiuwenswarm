"""Contract tests for the isolated report-style SDK bridge."""

from __future__ import annotations

import base64
import asyncio
import io
import json
import os
import stat
import sys
import types
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch import sdk_bridge as bridge
from jiuwenswarm.agents.harness.common.tools.deepresearch import runtime
from jiuwenswarm.common.local_env_config import get_task_env_overlay


def _zip_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("report_bundle/report.html", "ok")
    return output.getvalue()


def _private_file(path: Path) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)


def _request() -> dict:
    return {
        "schema_version": 2,
        "final_result": {"response_content": "report"},
        "llm_config": {"general": {"api_key": "secret", "model_name": "m"}},
        "llm_auth": {},
        "tls": {"LLM_SSL_VERIFY": False, "TOOL_SSL_VERIFY": True},
    }


def test_bridge_rejects_oversized_input_without_importing_sdk(monkeypatch):
    imported = []
    real_import = __import__

    def guarded(name, *args, **kwargs):
        if name.startswith("openjiuwen_deepsearch"):
            imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded)
    payload = io.BytesIO(b"x" * (bridge.BRIDGE_INPUT_MAX_BYTES + 1))
    with pytest.raises(bridge.BridgeError, match="bridge_input_too_large"):
        bridge.read_request(payload)
    assert imported == []


def test_bridge_streaming_reader_handles_short_reads():
    encoded = json.dumps(_request()).encode()

    class ShortReader:
        def __init__(self, payload):
            self.payload = bytearray(payload)

        def read(self, size):
            take = min(size, 3, len(self.payload))
            chunk = bytes(self.payload[:take])
            del self.payload[:take]
            return chunk

    assert bridge.read_request(ShortReader(encoded)) == _request()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda request: request.update(extra=True),
        lambda request: request.update(schema_version=1),
        lambda request: request.update(tls={"LLM_SSL_VERIFY": False}),
        lambda request: request.update(tls={"LLM_SSL_VERIFY": "false", "TOOL_SSL_VERIFY": True}),
    ],
)
def test_request_schema_is_strict(mutation):
    request = _request()
    mutation(request)
    with pytest.raises(bridge.BridgeError, match="bridge_request_invalid"):
        bridge.read_request(io.BytesIO(json.dumps(request).encode()))


@pytest.mark.parametrize(
    "llm_auth",
    [
        None,
        {"default_headers": ""},
        {"default_headers": "not-json"},
        {"default_headers": "{}"},
        {"default_headers": '{"X-Extra":"1"}'},
        {
            "default_headers": '{"Authorization":"Basic abc","X-Extra":"1"}'
        },
        {"default_headers": '{"Authorization":"Basic abc"}', "extra": "x"},
    ],
)
def test_request_schema_rejects_nonminimal_llm_auth(llm_auth):
    request = _request()
    request["llm_auth"] = llm_auth
    with pytest.raises(bridge.BridgeError, match="bridge_request_invalid"):
        bridge.read_request(io.BytesIO(json.dumps(request).encode()))


@pytest.mark.parametrize("schema_version", [True, 2.0, "2", -1])
def test_request_schema_version_requires_exact_integer(schema_version):
    request = _request()
    request["schema_version"] = schema_version
    with pytest.raises(bridge.BridgeError, match="bridge_request_invalid"):
        bridge.read_request(io.BytesIO(json.dumps(request).encode()))


def test_bridge_writes_only_to_precreated_private_regular_file(tmp_path: Path):
    output = tmp_path / "styled.zip"
    _private_file(output)
    bridge.write_convert_content(output, base64.b64encode(_zip_bytes()).decode())
    assert output.stat().st_mode & 0o777 == 0o600
    assert zipfile.is_zipfile(output)


def test_bridge_writes_archive_in_binary_mode_on_windows(tmp_path: Path, monkeypatch):
    output = tmp_path / "styled.zip"
    _private_file(output)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("report_bundle/report.html", b"line-1\nline-2")

    binary_flag = 1 << 29
    opened_flags: dict[int, int] = {}
    original_open = os.open
    original_write = os.write

    def windows_open(path, flags, mode=0o777, **kwargs):
        descriptor = original_open(path, flags & ~binary_flag, mode, **kwargs)
        opened_flags[descriptor] = flags
        return descriptor

    def windows_write(descriptor: int, data: bytes) -> int:
        payload = bytes(data)
        if opened_flags[descriptor] & binary_flag:
            return original_write(descriptor, payload)
        translated = payload.replace(b"\n", b"\r\n")
        assert original_write(descriptor, translated) == len(translated)
        return len(payload)

    monkeypatch.setattr(bridge.os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(bridge.os, "open", windows_open)
    monkeypatch.setattr(bridge.os, "write", windows_write)

    bridge.write_convert_content(output, base64.b64encode(archive.getvalue()).decode())

    with zipfile.ZipFile(output) as bundle:
        assert bundle.testzip() is None
        assert bundle.read("report_bundle/report.html") == b"line-1\nline-2"


@pytest.mark.parametrize(
    "kind", ["missing", "symlink", "hardlink", "mode", "nonempty", "reparse"]
)
def test_bridge_rejects_unsafe_output(tmp_path: Path, kind: str, monkeypatch):
    output = tmp_path / "styled.zip"
    if kind == "symlink":
        source = tmp_path / "source"
        _private_file(source)
        output.symlink_to(source)
    elif kind == "hardlink":
        source = tmp_path / "source"
        _private_file(source)
        os.link(source, output)
    elif kind == "mode":
        _private_file(output)
        output.chmod(0o640)
    elif kind == "nonempty":
        _private_file(output)
        output.write_bytes(b"occupied")
    elif kind == "reparse":
        _private_file(output)
        original_lstat = Path.lstat

        def reparse_lstat(path: Path):
            metadata = original_lstat(path)
            if path == output:
                return types.SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_dev=metadata.st_dev,
                    st_ino=metadata.st_ino,
                    st_nlink=metadata.st_nlink,
                    st_size=metadata.st_size,
                    st_ctime_ns=metadata.st_ctime_ns,
                    st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return metadata

        monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(bridge.BridgeError, match="bridge_output_invalid"):
        bridge.write_convert_content(output, base64.b64encode(_zip_bytes()).decode())


@pytest.mark.parametrize("content", ["%%%", base64.b64encode(b"not zip").decode()])
def test_bridge_rejects_bad_archive(tmp_path: Path, content: str):
    output = tmp_path / "styled.zip"
    _private_file(output)
    with pytest.raises(bridge.BridgeError, match="bridge_archive_invalid"):
        bridge.write_convert_content(output, content)
    assert output.stat().st_size == 0


def test_bridge_rejects_output_path_substitution_before_write(tmp_path: Path, monkeypatch):
    artifact = runtime._create_bridge_artifact()
    output = artifact.path
    original_open = bridge.os.open
    replaced = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if Path(path) == output and not replaced:
            replaced = True
            output.unlink()
            replacement = original_open(
                output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.close(replacement)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(bridge.os, "open", swapping_open)
    try:
        with pytest.raises(bridge.BridgeError, match="bridge_output_invalid"):
            bridge.write_convert_content(output, base64.b64encode(_zip_bytes()).decode())
        assert output.read_bytes() == b""
    finally:
        runtime._remove_bridge_artifact(artifact)


def test_bridge_file_identity_rejects_reused_inode():
    original = types.SimpleNamespace(st_dev=1, st_ino=2, st_ctime_ns=3)
    replacement = types.SimpleNamespace(st_dev=1, st_ino=2, st_ctime_ns=4)

    assert not bridge._same_unchanged_file(original, replacement)


def test_parent_keeps_bridge_output_open_until_cleanup():
    artifact = runtime._create_bridge_artifact()
    try:
        opened = os.fstat(artifact.descriptor)
        assert (opened.st_dev, opened.st_ino) == artifact.file_identity
    finally:
        runtime._remove_bridge_artifact(artifact)

    with pytest.raises(OSError):
        os.fstat(artifact.descriptor)


def test_parent_bridge_artifact_accepts_windows_synthetic_mode_bits(
    tmp_path: Path, monkeypatch
):
    directory = tmp_path / "deepresearch-sdk-bridge"
    directory.mkdir(mode=0o700)
    original_lstat = Path.lstat
    original_fstat = os.fstat

    def windows_lstat(path: Path):
        metadata = original_lstat(path)
        if path == directory:
            return types.SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o777,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino,
                st_file_attributes=0,
            )
        return metadata

    def windows_fstat(descriptor: int):
        metadata = original_fstat(descriptor)
        return types.SimpleNamespace(
            st_mode=stat.S_IFREG | 0o666,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_nlink=1,
            st_file_attributes=0,
        )

    monkeypatch.setattr(runtime.tempfile, "mkdtemp", lambda **_kwargs: str(directory))
    monkeypatch.setattr(Path, "lstat", windows_lstat)
    monkeypatch.setattr(runtime.os, "fstat", windows_fstat)

    artifact = runtime._create_bridge_artifact()
    try:
        assert artifact.path == directory / "styled.zip"
    finally:
        runtime._remove_bridge_artifact(artifact)


def test_parent_validates_windows_synthetic_archive_mode(monkeypatch):
    artifact = runtime._create_bridge_artifact()
    original_lstat = Path.lstat
    try:
        os.write(artifact.descriptor, _zip_bytes())
        os.fsync(artifact.descriptor)

        def windows_lstat(path: Path):
            metadata = original_lstat(path)
            if path == artifact.path:
                return types.SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o666,
                    st_dev=metadata.st_dev,
                    st_ino=metadata.st_ino,
                    st_nlink=metadata.st_nlink,
                    st_size=metadata.st_size,
                    st_file_attributes=0,
                )
            return metadata

        monkeypatch.setattr(Path, "lstat", windows_lstat)
        runtime._validate_bridge_output(artifact)
    finally:
        runtime._remove_bridge_artifact(artifact)


def test_child_bridge_accepts_windows_synthetic_mode_bits(
    tmp_path: Path, monkeypatch
):
    output = tmp_path / "styled.zip"
    _private_file(output)
    original_lstat = Path.lstat
    original_fstat = os.fstat

    def windows_lstat(path: Path):
        metadata = original_lstat(path)
        if path == output:
            return types.SimpleNamespace(
                st_mode=stat.S_IFREG | 0o666,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino,
                st_nlink=metadata.st_nlink,
                st_size=metadata.st_size,
                st_ctime_ns=metadata.st_ctime_ns,
                st_file_attributes=0,
            )
        return metadata

    def windows_fstat(descriptor: int):
        metadata = original_fstat(descriptor)
        return types.SimpleNamespace(
            st_mode=stat.S_IFREG | 0o666,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_nlink=metadata.st_nlink,
            st_size=metadata.st_size,
            st_ctime_ns=metadata.st_ctime_ns,
            st_file_attributes=0,
        )

    monkeypatch.setattr(Path, "lstat", windows_lstat)
    monkeypatch.setattr(bridge.os, "fstat", windows_fstat)

    bridge.write_convert_content(output, base64.b64encode(_zip_bytes()).decode())
    assert zipfile.is_zipfile(output)


@pytest.mark.asyncio
async def test_sdk_is_lazy_logs_only_to_stderr_and_restores_tls(tmp_path: Path, monkeypatch, capsys):
    output = tmp_path / "styled.zip"
    _private_file(output)
    observed = {}

    @asynccontextmanager
    async def context(config):
        observed["config"] = config
        observed["tls"] = (os.environ["LLM_SSL_VERIFY"], os.environ["TOOL_SSL_VERIFY"])
        print("sdk-context-log")
        yield {"llm": True}

    async def stylize(final_result, llm):
        print("sdk-style-log")
        return types.SimpleNamespace(
            convert_content=base64.b64encode(_zip_bytes()).decode(),
            style_applied=False,
            style_status="fallback",
            style_phase="invoke_llm",
            style_reason_code="llm_call_failed",
        )

    modules = {
        "openjiuwen_deepsearch": types.ModuleType("openjiuwen_deepsearch"),
        "openjiuwen_deepsearch.algorithm": types.ModuleType("openjiuwen_deepsearch.algorithm"),
        "openjiuwen_deepsearch.algorithm.report_style": types.ModuleType("openjiuwen_deepsearch.algorithm.report_style"),
        "openjiuwen_deepsearch.algorithm.report_style.service": types.ModuleType("openjiuwen_deepsearch.algorithm.report_style.service"),
        "openjiuwen_deepsearch.framework": types.ModuleType("openjiuwen_deepsearch.framework"),
        "openjiuwen_deepsearch.framework.openjiuwen": types.ModuleType("openjiuwen_deepsearch.framework.openjiuwen"),
        "openjiuwen_deepsearch.framework.openjiuwen.llm": types.ModuleType("openjiuwen_deepsearch.framework.openjiuwen.llm"),
        "openjiuwen_deepsearch.framework.openjiuwen.llm.report_style_runtime": types.ModuleType("openjiuwen_deepsearch.framework.openjiuwen.llm.report_style_runtime"),
    }
    modules["openjiuwen_deepsearch.algorithm.report_style.service"].stylize_report = stylize
    modules["openjiuwen_deepsearch.framework.openjiuwen.llm.report_style_runtime"].report_style_llm_context = context
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("LLM_SSL_VERIFY", "parent-llm")
    monkeypatch.delenv("TOOL_SSL_VERIFY", raising=False)

    result = await bridge.stylize_request(_request(), output)
    captured = capsys.readouterr()
    assert result["status"] == "completed"
    assert result["style_phase"] == "invoke_llm"
    assert result["style_reason_code"] == "llm_call_failed"
    assert captured.out == ""
    assert "sdk-context-log" in captured.err and "sdk-style-log" in captured.err
    assert observed["tls"] == ("false", "true")
    assert observed["config"]["general"]["api_key"] == bytearray(b"secret")
    assert os.environ["LLM_SSL_VERIFY"] == "parent-llm"
    assert "TOOL_SSL_VERIFY" not in os.environ


@pytest.mark.asyncio
async def test_sdk_binds_request_scoped_auth_overlay_and_restores_it(
    tmp_path: Path, monkeypatch
):
    output = tmp_path / "styled.zip"
    _private_file(output)
    authorization = "Basic c3R5bGUtY2hpbGQ="
    observed = {}

    @asynccontextmanager
    async def context(_config):
        observed["overlay"] = get_task_env_overlay()
        from openjiuwen.core.foundation.llm.model_clients.openai_model_client import (
            OpenAIModelClient,
        )
        from openjiuwen.core.foundation.llm.schema.config import (
            ModelClientConfig,
            ModelRequestConfig,
        )

        model_client = OpenAIModelClient(
            ModelRequestConfig(),
            ModelClientConfig(
                api_key="huawei-maas-session",
                api_base="https://example.invalid/v1",
                client_provider="OpenAI",
                use_shared_llm_http_client=False,
            ),
        )
        client = model_client._create_async_openai_client()
        observed["authorization"] = client._custom_headers.get("Authorization")
        await client.close()
        yield {"llm": True}

    async def stylize(_final_result, _llm):
        return types.SimpleNamespace(
            convert_content=base64.b64encode(_zip_bytes()).decode(),
            style_applied=True,
            style_status="applied",
        )

    service = types.ModuleType("openjiuwen_deepsearch.algorithm.report_style.service")
    style_runtime = types.ModuleType(
        "openjiuwen_deepsearch.framework.openjiuwen.llm.report_style_runtime"
    )
    service.stylize_report = stylize
    style_runtime.report_style_llm_context = context
    monkeypatch.setitem(sys.modules, service.__name__, service)
    monkeypatch.setitem(sys.modules, style_runtime.__name__, style_runtime)
    request = _request()
    request["llm_auth"] = {
        "default_headers": json.dumps({"Authorization": authorization})
    }

    result = await bridge.stylize_request(request, output)

    assert result["status"] == "completed"
    assert observed["overlay"] == request["llm_auth"]
    assert observed["authorization"] == authorization
    assert get_task_env_overlay() is None


@pytest.mark.asyncio
async def test_sdk_exception_is_safe_and_restores_tls(tmp_path: Path, monkeypatch):
    output = tmp_path / "styled.zip"
    _private_file(output)
    service = types.ModuleType("openjiuwen_deepsearch.algorithm.report_style.service")
    runtime = types.ModuleType("openjiuwen_deepsearch.framework.openjiuwen.llm.report_style_runtime")

    @asynccontextmanager
    async def context(_config):
        raise RuntimeError("secret payload")
        yield

    service.stylize_report = lambda *_: None
    runtime.report_style_llm_context = context
    monkeypatch.setitem(sys.modules, service.__name__, service)
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    with pytest.raises(bridge.BridgeError, match="SDK report styling failed") as caught:
        await bridge.stylize_request(_request(), output)
    assert "secret" not in str(caught.value)
    assert get_task_env_overlay() is None


@pytest.mark.asyncio
async def test_sdk_cancellation_restores_auth_overlay(tmp_path: Path, monkeypatch):
    output = tmp_path / "styled.zip"
    _private_file(output)
    started = asyncio.Event()
    observed = {}

    @asynccontextmanager
    async def context(_config):
        observed["overlay"] = get_task_env_overlay()
        yield {"llm": True}

    async def stylize(_final_result, _llm):
        started.set()
        await asyncio.Event().wait()

    service = types.ModuleType("openjiuwen_deepsearch.algorithm.report_style.service")
    style_runtime = types.ModuleType(
        "openjiuwen_deepsearch.framework.openjiuwen.llm.report_style_runtime"
    )
    service.stylize_report = stylize
    style_runtime.report_style_llm_context = context
    monkeypatch.setitem(sys.modules, service.__name__, service)
    monkeypatch.setitem(sys.modules, style_runtime.__name__, style_runtime)
    request = _request()
    request["llm_auth"] = {
        "default_headers": '{"Authorization":"Basic Y2FuY2Vs"}'
    }

    task = asyncio.create_task(bridge.stylize_request(request, output))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert observed["overlay"] == request["llm_auth"]
    assert get_task_env_overlay() is None


def test_cli_stdout_is_exactly_one_versioned_result_line(tmp_path: Path, monkeypatch, capsys):
    output = tmp_path / "styled.zip"

    class Input:
        buffer = io.BytesIO(json.dumps(_request()).encode())

    expected = {
            "schema_version": 1,
            "status": "completed",
            "output_path": str(output),
            "style_applied": True,
            "style_status": "applied",
            "style_phase": None,
            "style_reason_code": None,
    }

    async def stylize(_request_value, _output_value):
        return expected

    def run_without_replacing_process_loop(coroutine):
        coroutine.close()
        return expected

    monkeypatch.setattr(bridge.sys, "stdin", Input())
    monkeypatch.setattr(bridge, "stylize_request", stylize)
    monkeypatch.setattr(bridge.asyncio, "run", run_without_replacing_process_loop)
    assert bridge.main([
        "stylize-report", "--config-stdin", "--output", str(output)
    ]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == expected


class _Writer:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class _Reader:
    def __init__(self, payload=b""):
        self.payload = payload

    async def read(self, _size=-1):
        payload, self.payload = self.payload, b""
        return payload


class _Process:
    def __init__(self, stdout=b"", stderr=b"", running=False):
        self.stdin = _Writer()
        self.stdout = _Reader(stdout)
        self.stderr = _Reader(stderr)
        self.returncode = None if running else 0
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    async def wait(self):
        self.waited += 1
        if self.returncode is None:
            self.returncode = -15 if self.terminated else 0
        return self.returncode

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1
        self.returncode = -9


class _BlockingReader:
    async def read(self, _size=-1):
        await asyncio.Event().wait()
        return b""


class _CancellationProcess(_Process):
    def __init__(self):
        super().__init__(running=True)
        self.stdout = _BlockingReader()
        self.release_wait = asyncio.Event()

    async def wait(self):
        self.waited += 1
        await self.release_wait.wait()
        self.returncode = -15 if self.terminated else -9
        return self.returncode


@pytest.mark.asyncio
async def test_parent_client_tracks_validates_and_cleans_owned_archive(tmp_path: Path, monkeypatch):
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("python")
    output_holder = {}
    authorization = "Basic cGFyZW50LXRvLWNoaWxk"
    manager = types.SimpleNamespace(track_process=lambda *_: None, untrack_process=lambda *_: None)

    async def spawn(*args, **kwargs):
        output = Path(args[args.index("--output") + 1])
        output.write_bytes(_zip_bytes())
        output_holder["path"] = output
        frame = {
            "schema_version": 1,
            "status": "completed",
            "output_path": str(output),
            "style_applied": False,
            "style_status": "fallback",
            "style_phase": "invoke_llm",
            "style_reason_code": "llm_call_failed",
        }
        output_holder["env"] = kwargs["env"]
        output_holder["args"] = args
        process = _Process((json.dumps(frame) + "\n").encode(), b"sdk logs")
        output_holder["process"] = process
        return process

    monkeypatch.setattr(runtime, "resolve_python_executable", lambda: executable)
    monkeypatch.setattr(runtime, "build_child_env", lambda _: {
        "PATH": "/isolated",
        "LLM_SSL_VERIFY": "ambient",
        "TOOL_SSL_VERIFY": "ambient",
    })
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    async with runtime.stylize_report_archive(
        final_result={"response_content": "r"},
        llm_config={"general": {"api_key": "secret"}},
        llm_auth={
            "default_headers": json.dumps({"Authorization": authorization})
        },
        tls={"LLM_SSL_VERIFY": False, "TOOL_SSL_VERIFY": False},
        manager=manager,
        session_id="S1",
    ) as archive:
        assert archive.path == output_holder["path"]
        assert archive.path.exists()
        assert archive.style_applied is False
        assert archive.style_status == "fallback"
        assert archive.style_phase == "invoke_llm"
        assert archive.style_reason_code == "llm_call_failed"
    assert not output_holder["path"].exists()
    assert not output_holder["path"].parent.exists()
    assert "LLM_SSL_VERIFY" not in output_holder["env"]
    assert "TOOL_SSL_VERIFY" not in output_holder["env"]
    request = json.loads(bytes(output_holder["process"].stdin.data))
    assert request["schema_version"] == 2
    assert request["llm_auth"] == {
        "default_headers": json.dumps({"Authorization": authorization})
    }
    assert authorization not in " ".join(map(str, output_holder["args"]))
    assert authorization not in json.dumps(output_holder["env"])


@pytest.mark.parametrize(
    "llm_auth",
    [
        {"default_headers": "{}"},
        {"default_headers": '{"Authorization":"Basic abc","X-Extra":"1"}'},
        {"unexpected": "value"},
    ],
)
def test_parent_rejects_nonminimal_llm_auth_before_spawn(llm_auth):
    with pytest.raises(
        runtime.DeepResearchRuntimeError, match="sdk_bridge_request_invalid"
    ):
        runtime._encode_bridge_request(
            final_result={},
            llm_config={},
            llm_auth=llm_auth,
            tls={"LLM_SSL_VERIFY": False, "TOOL_SSL_VERIFY": False},
        )


@pytest.mark.asyncio
async def test_parent_client_rejects_stdout_injection_and_cleans(tmp_path: Path, monkeypatch):
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("python")
    observed = {}
    manager = types.SimpleNamespace(track_process=lambda *_: None, untrack_process=lambda *_: None)

    async def spawn(*args, **_kwargs):
        output = Path(args[args.index("--output") + 1])
        output.write_bytes(_zip_bytes())
        observed["output"] = output
        return _Process(b"noise\n{}\n")

    monkeypatch.setattr(runtime, "resolve_python_executable", lambda: executable)
    monkeypatch.setattr(runtime, "build_child_env", lambda _: {"PATH": "/isolated"})
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    with pytest.raises(runtime.DeepResearchRuntimeError, match="sdk_bridge_protocol_invalid"):
        async with runtime.stylize_report_archive(
            final_result={}, llm_config={}, llm_auth={},
            tls={"LLM_SSL_VERIFY": False, "TOOL_SSL_VERIFY": False},
            manager=manager, session_id="S1",
        ):
            pass
    assert not observed["output"].exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", [True, 1.0, "1", -1])
async def test_parent_client_requires_exact_integer_schema_version(
    tmp_path: Path,
    monkeypatch,
    schema_version,
):
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("python")
    observed = {}
    manager = types.SimpleNamespace(track_process=lambda *_: None, untrack_process=lambda *_: None)

    async def spawn(*args, **_kwargs):
        output = Path(args[args.index("--output") + 1])
        output.write_bytes(_zip_bytes())
        observed["output"] = output
        frame = {
            "schema_version": schema_version,
            "status": "completed",
            "output_path": str(output),
            "style_applied": True,
            "style_status": "applied",
        }
        return _Process((json.dumps(frame) + "\n").encode())

    monkeypatch.setattr(runtime, "resolve_python_executable", lambda: executable)
    monkeypatch.setattr(runtime, "build_child_env", lambda _: {"PATH": "/isolated"})
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    with pytest.raises(runtime.DeepResearchRuntimeError, match="sdk_bridge_failed"):
        async with runtime.stylize_report_archive(
            final_result={},
            llm_config={},
            llm_auth={},
            tls={"LLM_SSL_VERIFY": False, "TOOL_SSL_VERIFY": False},
            manager=manager,
            session_id="S1",
        ):
            pass
    assert not observed["output"].exists()


@pytest.mark.asyncio
async def test_parent_client_track_failure_reaps_and_does_not_untrack(tmp_path: Path, monkeypatch):
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("python")
    process = _Process(running=True)
    manager = types.SimpleNamespace(
        track_process=lambda *_: (_ for _ in ()).throw(RuntimeError("closed")),
        untrack_process=lambda *_: pytest.fail("must not untrack"),
    )
    monkeypatch.setattr(runtime, "resolve_python_executable", lambda: executable)
    monkeypatch.setattr(runtime, "build_child_env", lambda _: {"PATH": "/isolated"})
    async def spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="closed"):
        async with runtime.stylize_report_archive(
            final_result={}, llm_config={}, llm_auth={},
            tls={"LLM_SSL_VERIFY": False, "TOOL_SSL_VERIFY": False},
            manager=manager, session_id="S1",
        ):
            pass
    assert process.terminated == 1
    assert process.waited >= 1


@pytest.mark.asyncio
async def test_parent_client_repeated_cancel_reaps_untracks_and_cleans(tmp_path: Path, monkeypatch):
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.write_text("python")
    process = _CancellationProcess()
    tracked = asyncio.Event()
    untracked = []
    manager = types.SimpleNamespace(
        track_process=lambda *args: tracked.set(),
        untrack_process=lambda *args: untracked.append(args),
    )
    monkeypatch.setattr(runtime, "resolve_python_executable", lambda: executable)
    monkeypatch.setattr(runtime, "build_child_env", lambda _: {"PATH": "/isolated"})

    async def spawn(*_args, **_kwargs):
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)

    async def invoke():
        async with runtime.stylize_report_archive(
            final_result={}, llm_config={}, llm_auth={},
            tls={"LLM_SSL_VERIFY": False, "TOOL_SSL_VERIFY": False},
            manager=manager, session_id="S1",
        ):
            pass

    task = asyncio.create_task(invoke())
    await tracked.wait()
    task.cancel()
    for _ in range(100):
        if process.terminated:
            break
        await asyncio.sleep(0)
    task.cancel()
    process.release_wait.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.waited >= 1
    assert untracked == [("S1", process)]
