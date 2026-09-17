# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""端到端 CLI 集成测试.

仅做端到端: 通过 ``subprocess.run`` 真实拉起已安装的 ``jiuwenbox`` 可执行脚本
(由 ``pyproject.toml`` 的 ``[project.scripts]`` 声明, 类似 ``uvicorn``),
断言 stdout / stderr / 退出码 / HTTP 副作用; 不写单元测试、不 mock httpx。

运行测试前请先在仓库根 (``code_agent/jiuwenbox/``) 安装本包::

    pip install -e .

若 ``jiuwenbox`` 不在 PATH 上, 测试会自动到 ``sys.executable`` 同级目录寻找;
均找不到则在 collect 阶段直接报错并提示安装方式。

假设 jiuwenbox server 已在运行 (与既有 ``test_server_api_default.py`` 一致),
由 ``--server-endpoint`` 或 ``JIUWENBOX_TEST_SERVER`` 指定。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import yaml

from jiuwenbox.bundled_configs import default_policy_path

pytestmark = pytest.mark.integration

_DEFAULT_FILESYSTEM_POLICY = yaml.safe_load(
    default_policy_path().read_text(encoding="utf-8")
)["filesystem_policy"]
_DEFAULT_BIND_MOUNTS = copy.deepcopy(_DEFAULT_FILESYSTEM_POLICY["bind_mounts"])
_DEFAULT_DIRECTORIES = copy.deepcopy(_DEFAULT_FILESYSTEM_POLICY["directories"])


def _with_runtime_support(policy: dict) -> dict:
    """Ensure override policies expose host python/runtime paths via bind_mounts.

    ``read_only`` alone only affects Landlock remounts; bubblewrap only exposes
    host paths listed in ``bind_mounts``. Mount list matches the bundled
    default policy used by successful server-api override tests — not the
    outdated ``conftest.SYSTEM_BIND_MOUNTS`` (which incorrectly includes
    ``/etc/ssl/openssl.cnf``).
    """
    runtime_policy = copy.deepcopy(policy)
    filesystem_policy = runtime_policy.setdefault("filesystem_policy", {})
    bind_mounts = filesystem_policy.setdefault("bind_mounts", [])
    for mount in _DEFAULT_BIND_MOUNTS:
        if mount not in bind_mounts:
            bind_mounts.append(copy.deepcopy(mount))
    directories = filesystem_policy.setdefault("directories", [])
    for directory in _DEFAULT_DIRECTORIES:
        if directory not in directories:
            directories.append(copy.deepcopy(directory))
    return runtime_policy


def _resolve_jiuwenbox_bin() -> str:
    """定位已安装的 ``jiuwenbox`` 可执行脚本。

    查找顺序:
    1. ``PATH`` 上的 ``jiuwenbox`` / ``jiuwenbox.exe``;
    2. 当前 Python 解释器同级目录 (venv ``bin/`` 或 ``Scripts/``);
    3. 找不到则报错, 提示先 ``pip install -e .``。
    """
    found = shutil.which("jiuwenbox")
    if found:
        return found
    py_dir = Path(sys.executable).resolve().parent
    candidates = [py_dir / "jiuwenbox", py_dir / "jiuwenbox.exe"]
    # venv 上 ``python`` 可能在 ``bin/``, scripts 也在 ``bin/``; Windows venv
    # 则 ``python.exe`` 与 ``jiuwenbox.exe`` 同在 ``Scripts/``。
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError(
        "jiuwenbox CLI is not installed. Run `pip install -e .` from "
        "`code_agent/jiuwenbox/` before running the CLI integration tests.",
    )


_JIUWENBOX_BIN = _resolve_jiuwenbox_bin()


_PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


def _subprocess_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """为子进程构造环境变量。

    本测试运行已安装的脚本, 无需再注入 ``PYTHONPATH``;
    保留 ``NO_COLOR`` 以确保 stderr 不含 ANSI 转义码, 便于断言。
    """
    env = os.environ.copy()
    for key in _PROXY_ENV_VARS:
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env.setdefault("NO_COLOR", "1")
    if extra_env:
        env.update(extra_env)
    return env


def _run_cli(
    args: list[str],
    *,
    base_url: str | None = None,
    extra_env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    timeout: float = 60.0,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """运行 CLI 并返回 ``CompletedProcess`` (stdout/stderr 为 bytes)。"""
    cmd: list[str] = [_JIUWENBOX_BIN]
    if base_url is not None:
        cmd += ["--base-url", base_url]
    cmd += args
    return subprocess.run(
        cmd,
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        check=check,
        env=_subprocess_env(extra_env),
    )


def _run_cli_json(args: list[str], *, base_url: str, **kwargs) -> tuple[subprocess.CompletedProcess, object]:
    """运行 CLI, 期待 stdout 是合法 JSON, 返回 (proc, parsed_json)。"""
    proc = _run_cli(args, base_url=base_url, **kwargs)
    assert proc.returncode == 0, (
        f"CLI exited {proc.returncode}\n"
        f"args={args!r}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    return proc, json.loads(proc.stdout.decode("utf-8"))


def _wait_phase(client, sandbox_id: str, phase: str, *, timeout: float = 10.0) -> dict:
    """轮询直到 ``sandbox.phase == phase`` 或超时, 返回最后一次 state。"""
    deadline = time.monotonic() + timeout
    state: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        if response.status_code == 200:
            state = response.json()
            if state.get("phase") == phase:
                return state
        time.sleep(0.3)
    raise AssertionError(
        f"sandbox {sandbox_id} did not reach phase={phase} within {timeout}s; "
        f"last state: {state}"
    )


@pytest.fixture
def tracking_sandboxes(client):
    """模块内手动登记 sandbox_id, 测试结束兜底删除。"""
    ids: list[str] = []
    yield ids
    for sandbox_id in reversed(ids):
        try:
            client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        except Exception:  # noqa: BLE001
            pass


def _create_sandbox_via_cli(server_url: str, tracking_sandboxes: list[str]) -> str:
    """CLI 创建 sandbox, 登记到 tracking 列表, 返回 sandbox_id。"""
    proc, data = _run_cli_json(["sandbox", "create"], base_url=server_url)
    assert isinstance(data, dict), data
    sandbox_id = data.get("id")
    assert isinstance(sandbox_id, str) and sandbox_id, data
    tracking_sandboxes.append(sandbox_id)
    return sandbox_id


# ────────────────────────────── health ──────────────────────────────


def test_cli_health_ok(server_url):
    proc, data = _run_cli_json(["health"], base_url=server_url)
    assert isinstance(data, dict)
    assert data.get("status") == "ok"


# ────────────────────────────── sandbox ──────────────────────────────


def test_cli_sandbox_create_and_get(server_url, tracking_sandboxes):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)

    proc, data = _run_cli_json(
        ["sandbox", "get", sandbox_id], base_url=server_url,
    )
    assert data.get("id") == sandbox_id
    assert data.get("phase") in ("ready", "provisioning", "stopped")


def test_cli_sandbox_create_with_custom_id(server_url, tracking_sandboxes):
    custom_id = "my-sb_cli01"
    proc, data = _run_cli_json(
        ["sandbox", "create", "--sandbox-id", custom_id],
        base_url=server_url,
    )
    assert data.get("id") == custom_id
    tracking_sandboxes.append(custom_id)


def test_cli_sandbox_ls(server_url, tracking_sandboxes):
    _create_sandbox_via_cli(server_url, tracking_sandboxes)

    proc, data = _run_cli_json(["sandbox", "ls"], base_url=server_url)
    assert isinstance(data, list)
    assert len(data) >= 1


def test_cli_sandbox_ls_phase_filter(server_url, tracking_sandboxes, client):
    """``--phase`` 在 CLI 侧本地过滤; 不匹配时返回空列表。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    _, ready = _run_cli_json(
        ["sandbox", "ls", "--phase", "ready"], base_url=server_url,
    )
    assert isinstance(ready, list)
    assert sandbox_id in {item.get("id") for item in ready if isinstance(item, dict)}

    _, empty = _run_cli_json(
        ["sandbox", "ls", "--phase", "no_such_phase"], base_url=server_url,
    )
    assert empty == []


def test_cli_sandbox_lifecycle(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    _, stopped = _run_cli_json(
        ["sandbox", "stop", sandbox_id], base_url=server_url,
    )
    assert stopped.get("phase") == "stopped"

    _, started = _run_cli_json(
        ["sandbox", "start", sandbox_id], base_url=server_url,
    )
    assert started.get("phase") == "ready"

    _, restarted = _run_cli_json(
        ["sandbox", "restart", sandbox_id], base_url=server_url,
    )
    assert restarted.get("phase") == "ready"


def test_cli_sandbox_exec_stdout_and_exitcode(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        [
            "sandbox", "exec", sandbox_id, "--",
            "python3", "-c", "print(7); import sys; sys.exit(3)",
        ],
        base_url=server_url,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert proc.stdout.strip() == b"7"


def test_cli_sandbox_exec_stderr(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        [
            "sandbox", "exec", sandbox_id, "--",
            "python3", "-c", "import sys; sys.stderr.write('e_marker')",
        ],
        base_url=server_url,
    )
    assert proc.returncode == 0, proc.stderr
    assert b"e_marker" in proc.stderr


def test_cli_sandbox_exec_stdin_dash(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        [
            "sandbox", "exec", sandbox_id, "--stdin", "-", "--",
            "cat",
        ],
        base_url=server_url,
        input_bytes=b"hello from stdin",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"hello from stdin"


def test_cli_sandbox_exec_env_cwd(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        [
            "sandbox", "exec", sandbox_id,
            "--cwd", "/tmp",
            "--env", "MY_TEST_VAR=marker42",
            "--",
            "python3", "-c",
            "import os; print(os.environ.get('MY_TEST_VAR')); print(os.getcwd())",
        ],
        base_url=server_url,
    )
    assert proc.returncode == 0, proc.stderr
    out_text = proc.stdout.decode("utf-8")
    assert "marker42" in out_text
    assert "/tmp" in out_text


def test_cli_sandbox_exec_missing_command(server_url, tracking_sandboxes, client):
    """``sandbox exec`` 不带任何命令时应抛 ``_CliError`` → exit 3。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        ["sandbox", "exec", sandbox_id], base_url=server_url,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert b"missing command" in proc.stderr


def test_cli_sandbox_exec_stdin_inline(server_url, tracking_sandboxes, client):
    """``--stdin <text>``: 直接把命令行参数作为 stdin 喂给沙箱命令。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        [
            "sandbox", "exec", sandbox_id,
            "--stdin", "inline-payload",
            "--", "cat",
        ],
        base_url=server_url,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"inline-payload"


def test_cli_sandbox_exec_timeout_seconds(server_url, tracking_sandboxes, client):
    """``--timeout-seconds`` 透传到沙箱; 极短超时 + 长 sleep → 非零退出。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        [
            "sandbox", "exec", sandbox_id,
            "--timeout-seconds", "1",
            "--",
            "python3", "-c", "import time; time.sleep(30); print('should-not-print')",
        ],
        base_url=server_url,
        timeout=30.0,
    )
    # 超时后沙箱 kill 进程; 不绑定具体退出码 (137/124/-9/etc 都可能),
    # 只断言"没有完整执行" + "退出码非零"。
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    assert b"should-not-print" not in proc.stdout


def test_cli_sandbox_bg_exec(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc, data = _run_cli_json(
        [
            "sandbox", "bg-exec", sandbox_id, "--",
            "python3", "-c", "import time; time.sleep(60)",
        ],
        base_url=server_url,
    )
    assert isinstance(data, dict)
    assert data.get("started") is True
    job_id = data.get("job_id")
    assert isinstance(job_id, str) and 4 <= len(job_id) <= 16
    assert isinstance(data.get("pid"), int) and data["pid"] > 0


def test_cli_sandbox_bg_exec_custom_job_id(
    server_url, tracking_sandboxes, client,
):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")
    job_id = f"cli-{uuid.uuid4().hex[:4]}"

    proc, data = _run_cli_json(
        [
            "sandbox", "bg-exec", sandbox_id,
            "--job-id", job_id,
            "--",
            "python3", "-c", "import time; time.sleep(3600)",
        ],
        base_url=server_url,
    )
    assert data.get("started") is True
    assert data.get("job_id") == job_id

    kill = _run_cli(
        ["sandbox", "bg-kill", sandbox_id, job_id],
        base_url=server_url,
    )
    assert kill.returncode == 0, (kill.stdout, kill.stderr)


def test_cli_sandbox_bg_get_instant_task(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    _, started = _run_cli_json(
        [
            "sandbox", "bg-exec", sandbox_id, "--job-id", "ver-cli",
            "--", "python3", "--version",
        ],
        base_url=server_url,
    )
    assert started.get("started") is True

    deadline = time.monotonic() + 10.0
    status: dict = {}
    while time.monotonic() < deadline:
        _, status = _run_cli_json(
            ["sandbox", "bg-get", sandbox_id, "ver-cli"],
            base_url=server_url,
        )
        if not status.get("running"):
            break
        time.sleep(0.1)
    assert status.get("running") is False
    assert status.get("exit_code") == 0
    assert (status.get("stdout") or "") == ""


def test_cli_sandbox_bg_list_and_kill(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")
    job_id = f"lst-{uuid.uuid4().hex[:4]}"

    _run_cli_json(
        [
            "sandbox", "bg-exec", sandbox_id, "--job-id", job_id,
            "--", "python3", "-c", "import time; time.sleep(3600)",
        ],
        base_url=server_url,
    )

    _, listed = _run_cli_json(
        ["sandbox", "bg-list", sandbox_id],
        base_url=server_url,
    )
    assert isinstance(listed, dict)
    job_ids = [item.get("job_id") for item in listed.get("items", [])]
    assert job_id in job_ids

    _, kill_data = _run_cli_json(
        ["sandbox", "bg-kill", sandbox_id, job_id],
        base_url=server_url,
    )
    assert kill_data.get("killed") is True


def test_cli_sandbox_bg_get_not_found(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        ["sandbox", "bg-get", sandbox_id, "no-such-job"],
        base_url=server_url,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)


def test_cli_sandbox_upload_download_roundtrip(
    server_url, tracking_sandboxes, client, tmp_path,
):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    payload = b"hello-roundtrip\n\x00\x01binary\xff"
    local = tmp_path / "src.bin"
    local.write_bytes(payload)
    remote = "/tmp/roundtrip.bin"

    up = _run_cli(
        [
            "sandbox", "upload", sandbox_id,
            str(local), remote,
        ],
        base_url=server_url,
    )
    assert up.returncode == 0, up.stderr

    down = _run_cli(
        ["sandbox", "download", sandbox_id, remote, "-"],
        base_url=server_url,
    )
    assert down.returncode == 0, down.stderr
    assert down.stdout == payload


def test_cli_sandbox_upload_from_stdin(server_url, tracking_sandboxes, client):
    """``upload <id> - <remote>``: 把 host stdin 的内容写入沙箱文件。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    payload = b"from-stdin-content\nbinary:\x00\x7f"
    remote = "/tmp/from-stdin.bin"

    up = _run_cli(
        ["sandbox", "upload", sandbox_id, "-", remote],
        base_url=server_url,
        input_bytes=payload,
    )
    assert up.returncode == 0, up.stderr

    down = _run_cli(
        ["sandbox", "download", sandbox_id, remote, "-"],
        base_url=server_url,
    )
    assert down.returncode == 0, down.stderr
    assert down.stdout == payload


def test_cli_sandbox_upload_local_not_found(
    server_url, tracking_sandboxes, client, tmp_path,
):
    """本地路径不存在 → CLI 抛 ``_CliError`` → exit 3 (不打到 server)。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    missing = tmp_path / "definitely-missing.bin"
    proc = _run_cli(
        ["sandbox", "upload", sandbox_id, str(missing), "/tmp/x.bin"],
        base_url=server_url,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert b"local file not found" in proc.stderr


def test_cli_sandbox_download_to_file(
    server_url, tracking_sandboxes, client, tmp_path,
):
    """``download <id> <remote> <local_path>``: 写到本地文件而非 stdout。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    payload = b"download-target\x00\xfe"
    src = tmp_path / "src.bin"
    src.write_bytes(payload)
    remote = "/tmp/download-target.bin"
    _run_cli(
        ["sandbox", "upload", sandbox_id, str(src), remote],
        base_url=server_url, check=True,
    )

    dst = tmp_path / "downloaded.bin"
    proc = _run_cli(
        ["sandbox", "download", sandbox_id, remote, str(dst)],
        base_url=server_url,
    )
    assert proc.returncode == 0, proc.stderr
    assert dst.read_bytes() == payload
    # 写文件路径时, 进度提示应落在 stderr 而不是 stdout, stdout 不应有内容。
    assert proc.stdout == b""


def test_cli_sandbox_files(server_url, tracking_sandboxes, client, tmp_path):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    local = tmp_path / "sample.txt"
    local.write_bytes(b"hello")
    _run_cli(
        [
            "sandbox", "upload", sandbox_id,
            str(local), "/tmp/sample.txt",
        ],
        base_url=server_url, check=True,
    )

    proc, data = _run_cli_json(
        [
            "sandbox", "files", sandbox_id, "/tmp",
            "--recursive", "--max-depth", "2",
        ],
        base_url=server_url,
    )
    assert isinstance(data, list)
    paths = {item.get("path") for item in data if isinstance(item, dict)}
    assert "/tmp/sample.txt" in paths


def test_cli_sandbox_files_no_dirs(
    server_url, tracking_sandboxes, client, tmp_path,
):
    """``--no-dirs`` 只返回文件项, 不包含目录条目。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    local = tmp_path / "only-file.txt"
    local.write_bytes(b"hi")
    _run_cli(
        ["sandbox", "upload", sandbox_id, str(local), "/tmp/only-file.txt"],
        base_url=server_url, check=True,
    )

    proc, data = _run_cli_json(
        [
            "sandbox", "files", sandbox_id, "/tmp",
            "--recursive", "--max-depth", "2",
            "--no-dirs",
        ],
        base_url=server_url,
    )
    assert isinstance(data, list)
    for item in data:
        assert isinstance(item, dict), data
        assert item.get("is_directory") is False, item
    paths = {item.get("path") for item in data}
    assert "/tmp/only-file.txt" in paths


def test_cli_sandbox_find(server_url, tracking_sandboxes, client, tmp_path):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    local = tmp_path / "hit.txt"
    local.write_bytes(b"x")
    _run_cli(
        [
            "sandbox", "upload", sandbox_id,
            str(local), "/tmp/hit.txt",
        ],
        base_url=server_url, check=True,
    )

    proc, data = _run_cli_json(
        ["sandbox", "find", sandbox_id, "/tmp", "*.txt"],
        base_url=server_url,
    )
    assert isinstance(data, list)
    paths = {item.get("path") for item in data if isinstance(item, dict)}
    assert "/tmp/hit.txt" in paths


def test_cli_sandbox_create_policy_file_not_found(server_url, tmp_path):
    """``--policy-file`` 指向不存在路径 → ``_CliError`` → exit 3。"""
    missing = tmp_path / "no-such.json"
    proc = _run_cli(
        ["sandbox", "create", "--policy-file", str(missing)],
        base_url=server_url,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert b"policy file not found" in proc.stderr


def test_cli_sandbox_create_policy_file_invalid_json(server_url, tmp_path):
    """``--policy-file`` 内容非合法 JSON → 本地解析失败 → exit 3。"""
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json,,,", encoding="utf-8")
    proc = _run_cli(
        ["sandbox", "create", "--policy-file", str(bad)],
        base_url=server_url,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert b"not valid JSON" in proc.stderr


def test_cli_sandbox_rm_yes(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)

    proc = _run_cli(
        ["sandbox", "rm", sandbox_id, "--yes"],
        base_url=server_url,
    )
    assert proc.returncode == 0, proc.stderr
    # 之后 GET 应为 404
    response = client.get(f"/api/v1/sandboxes/{sandbox_id}")
    assert response.status_code == 404


def test_cli_sandbox_rm_not_found(server_url):
    proc = _run_cli(
        ["sandbox", "rm", "nonexistent-id-12345", "--yes"],
        base_url=server_url,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert b"HTTP 404" in proc.stderr


# ────────────────────────────── policy ──────────────────────────────


def test_cli_policy_get(server_url, tracking_sandboxes, client):
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc, data = _run_cli_json(
        ["policy", "get", sandbox_id], base_url=server_url,
    )
    assert isinstance(data, dict)
    # policy 至少含 name 字段
    assert "name" in data


def test_cli_policy_get_not_found(server_url):
    """policy get 不存在的 sandbox → HTTP 404 → exit 1 + stderr 含状态码。"""
    proc = _run_cli(
        ["policy", "get", "nonexistent-policy-target-9999"],
        base_url=server_url,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert b"HTTP 404" in proc.stderr


def test_cli_policy_update_override(server_url, tracking_sandboxes, client):
    """``policy update --policy`` override 后 stdout 与 ``policy get`` 一致。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    policy_fragment = json.dumps({
        "network": {
            "egress": {
                "default": "deny",
                "allowed_domains": ["example.com"],
                "allowed_ips": ["198.51.100.10/32"],
                "allowed_ports": [443],
            },
            "ingress": {
                "default": "allow",
                "blocked_ports": [22],
            },
        },
    })
    _, updated = _run_cli_json(
        [
            "policy", "update", sandbox_id,
            "--policy-mode", "override",
            "--policy", policy_fragment,
        ],
        base_url=server_url,
    )
    assert isinstance(updated, dict)
    assert updated["network"]["egress"]["default"] == "deny"
    assert updated["network"]["egress"]["allowed_domains"] == ["example.com"]
    assert updated["network"]["egress"]["allowed_ips"] == ["198.51.100.10/32"]
    assert updated["network"]["egress"]["allowed_ports"] == [443]
    assert updated["network"]["ingress"]["blocked_ports"] == [22]

    _, fetched = _run_cli_json(
        ["policy", "get", sandbox_id], base_url=server_url,
    )
    assert fetched["network"]["egress"] == updated["network"]["egress"]
    assert fetched["network"]["ingress"] == updated["network"]["ingress"]


def test_cli_policy_update_append_via_policy_file(
    server_url, tracking_sandboxes, client, tmp_path,
):
    """``policy update --policy-file --policy-mode append`` 列表追加去重。"""
    create_policy = tmp_path / "create-isolated.json"
    create_policy.write_text(
        json.dumps(_with_runtime_support({
            "name": "cli-update-append",
            "filesystem_policy": {
                "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                "read_write": ["/tmp"],
            },
            "network": {
                "mode": "isolated",
                "egress": {
                    "default": "allow",
                    "allowed_domains": ["baidu.com"],
                },
                "ingress": {
                    "default": "allow",
                    "allowed_ports": [8080],
                },
            },
        })),
        encoding="utf-8",
    )
    _, created = _run_cli_json(
        [
            "sandbox", "create",
            "--policy-mode", "override",
            "--policy-file", str(create_policy),
        ],
        base_url=server_url,
    )
    sandbox_id = created["id"]
    tracking_sandboxes.append(sandbox_id)
    _wait_phase(client, sandbox_id, "ready")

    update_policy = tmp_path / "update-append.json"
    update_policy.write_text(
        json.dumps({
            "network": {
                "egress": {
                    "allowed_domains": ["example.com", "baidu.com"],
                },
                "ingress": {
                    "allowed_ports": [9090, 8080],
                },
            },
        }),
        encoding="utf-8",
    )
    _, updated = _run_cli_json(
        [
            "policy", "update", sandbox_id,
            "--policy-mode", "append",
            "--policy-file", str(update_policy),
        ],
        base_url=server_url,
    )
    assert updated["network"]["egress"]["default"] == "allow"
    assert updated["network"]["egress"]["allowed_domains"] == [
        "baidu.com",
        "example.com",
    ]
    assert updated["network"]["ingress"]["allowed_ports"] == [8080, 9090]


def test_cli_policy_update_requires_policy_arg(server_url, tracking_sandboxes, client):
    """未传 ``--policy`` / ``--policy-file`` → 本地错误 exit 3。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        ["policy", "update", sandbox_id],
        base_url=server_url,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert b"--policy or --policy-file is required" in proc.stderr


def test_cli_policy_update_rejects_both_policy_args(
    server_url, tracking_sandboxes, client, tmp_path,
):
    """同时传 ``--policy`` 与 ``--policy-file`` → 本地错误 exit 3。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")
    policy_file = tmp_path / "frag.json"
    policy_file.write_text(
        json.dumps({"network": {"egress": {"default": "allow"}}}),
        encoding="utf-8",
    )

    proc = _run_cli(
        [
            "policy", "update", sandbox_id,
            "--policy", '{"network":{"egress":{"default":"allow"}}}',
            "--policy-file", str(policy_file),
        ],
        base_url=server_url,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert b"pass only one of --policy / --policy-file" in proc.stderr


def test_cli_policy_update_invalid_json(server_url, tracking_sandboxes, client):
    """``--policy`` 非法 JSON → 本地错误 exit 3。"""
    sandbox_id = _create_sandbox_via_cli(server_url, tracking_sandboxes)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        [
            "policy", "update", sandbox_id,
            "--policy", "{not-json",
        ],
        base_url=server_url,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert b"--policy is not valid JSON" in proc.stderr


def test_cli_policy_update_not_found(server_url):
    """更新不存在的 sandbox → HTTP 404 → exit 1。"""
    proc = _run_cli(
        [
            "policy", "update", "nonexistent-policy-target-9999",
            "--policy", json.dumps({
                "network": {"egress": {"default": "allow"}},
            }),
        ],
        base_url=server_url,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert b"HTTP 404" in proc.stderr
    assert b"Sandbox 'nonexistent-policy-target-9999' not found" in proc.stderr


def test_cli_policy_update_rejects_host_mode(
    server_url, tracking_sandboxes, client, tmp_path,
):
    """host 模式沙箱动态更新 → HTTP 400, stderr 含完整 error。"""
    create_policy = tmp_path / "host-policy.json"
    create_policy.write_text(
        json.dumps(_with_runtime_support({
            "name": "cli-host-update-reject",
            "filesystem_policy": {
                "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                "read_write": ["/tmp"],
            },
            "network": {
                "mode": "host",
                "egress": {"default": "allow"},
            },
        })),
        encoding="utf-8",
    )
    _, created = _run_cli_json(
        [
            "sandbox", "create",
            "--policy-mode", "override",
            "--policy-file", str(create_policy),
        ],
        base_url=server_url,
    )
    sandbox_id = created["id"]
    tracking_sandboxes.append(sandbox_id)
    _wait_phase(client, sandbox_id, "ready")

    proc = _run_cli(
        [
            "policy", "update", sandbox_id,
            "--policy", json.dumps({
                "network": {"egress": {"default": "deny"}},
            }),
        ],
        base_url=server_url,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert b"HTTP 400" in proc.stderr
    expected = (
        f"Cannot update network rules for sandbox '{sandbox_id}': "
        "network.mode is 'host' "
        "(only isolated sandboxes support dynamic network updates)"
    )
    assert expected.encode("utf-8") in proc.stderr


def test_cli_policy_update_all(server_url, tracking_sandboxes, client, tmp_path):
    """``policy update-all`` 对 isolated 写入 updated, host 写入 skipped。"""
    isolated_policy = tmp_path / "isolated.json"
    isolated_policy.write_text(
        json.dumps(_with_runtime_support({
            "name": "cli-batch-isolated",
            "filesystem_policy": {
                "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                "read_write": ["/tmp"],
            },
            "network": {
                "mode": "isolated",
                "egress": {"default": "allow"},
            },
        })),
        encoding="utf-8",
    )
    host_policy = tmp_path / "host.json"
    host_policy.write_text(
        json.dumps(_with_runtime_support({
            "name": "cli-batch-host",
            "filesystem_policy": {
                "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                "read_write": ["/tmp"],
            },
            "network": {
                "mode": "host",
                "egress": {"default": "allow"},
            },
        })),
        encoding="utf-8",
    )

    _, isolated = _run_cli_json(
        [
            "sandbox", "create",
            "--policy-mode", "override",
            "--policy-file", str(isolated_policy),
        ],
        base_url=server_url,
    )
    _, host = _run_cli_json(
        [
            "sandbox", "create",
            "--policy-mode", "override",
            "--policy-file", str(host_policy),
        ],
        base_url=server_url,
    )
    tracking_sandboxes.extend([isolated["id"], host["id"]])
    _wait_phase(client, isolated["id"], "ready")
    _wait_phase(client, host["id"], "ready")

    _, result = _run_cli_json(
        [
            "policy", "update-all",
            "--policy-mode", "override",
            "--policy", json.dumps({
                "network": {
                    "egress": {
                        "default": "deny",
                        "allowed_ips": ["127.0.0.1/32"],
                    },
                },
            }),
        ],
        base_url=server_url,
    )
    assert isinstance(result, dict)
    assert isolated["id"] in result["updated"]
    assert {
        "sandbox_id": host["id"],
        "reason": "network.mode is host",
    } in result["skipped"]
    assert result["failed"] == []

    _, fetched = _run_cli_json(
        ["policy", "get", isolated["id"]], base_url=server_url,
    )
    assert fetched["network"]["egress"]["default"] == "deny"
    assert fetched["network"]["egress"]["allowed_ips"] == ["127.0.0.1/32"]


# ────────────────────────────── global / parsing ──────────────────────────────


def test_cli_global_env_var_base_url(server_url):
    # 不传 --base-url, 改用 JIUWENBOX_URL 环境变量
    proc = _run_cli(
        ["health"], base_url=None,
        extra_env={"JIUWENBOX_URL": server_url},
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout.decode("utf-8"))
    assert data.get("status") == "ok"


def test_cli_invalid_env_token(server_url):
    proc = _run_cli(
        ["sandbox", "create", "--env", "BAD_TOKEN"],
        base_url=server_url,
    )
    assert proc.returncode == 3, (proc.stdout, proc.stderr)
    assert b"KEY=VAL" in proc.stderr


def test_cli_help_exits_zero():
    for args in (["--help"], ["sandbox", "--help"], ["sandbox", "exec", "--help"]):
        proc = _run_cli(args, base_url="http://127.0.0.1:8321")
        assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
        assert proc.stdout, args


def test_cli_unknown_subcommand(server_url):
    """未知顶层子命令 → argparse 报错并以非零退出。"""
    proc = _run_cli(["no_such_group"], base_url=server_url)
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    # argparse 把错误打到 stderr; 内容里通常含 ``invalid choice`` 或 ``usage:``。
    err = proc.stderr.lower()
    assert (b"invalid choice" in err) or (b"usage:" in err), proc.stderr


def test_cli_verbose_debug_logs(server_url):
    """``-v`` 开启 DEBUG 日志, ``_CliClient`` 的 GET 调试行应出现在 stderr 上。"""
    proc = _run_cli(["-v", "health"], base_url=server_url)
    assert proc.returncode == 0, proc.stderr
    # 至少能看到 DEBUG 标记 (logging.basicConfig 格式: "%(levelname)s") 或 GET 请求行。
    err = proc.stderr
    assert b"DEBUG" in err, err
    assert b"GET" in err and b"/health" in err, err

# ────────────────────────────── proxy Basic auth ──────────────────────────────


@pytest.fixture
def tracking_proxy_routes(client):
    """Register proxy route names created via CLI; best-effort delete on teardown."""
    names: list[str] = []
    yield names
    for name in reversed(names):
        try:
            client.post(f"/api/v1/proxies/{name}/stop")
            client.delete(f"/api/v1/proxies/{name}")
        except Exception:  # noqa: BLE001
            pass


class TestCliProxyBasicAuth:
    """CLI ``proxy create``/``update`` Basic auth options (P2)."""

    @staticmethod
    def _unique_prefix() -> str:
        return f"/cli-basic-{uuid.uuid4().hex[:8]}"

    def test_cli_proxy_create_basic_password_redacted(
        self, server_url, tracking_proxy_routes
    ):
        """``--password`` create: detail/list output is redacted (no plaintext)."""
        secret = "cli-secret-pw-AAAA"
        prefix = self._unique_prefix()
        name = prefix.lstrip("/").replace("/", "-")
        tracking_proxy_routes.append(name)

        proc, data = _run_cli_json(
            ["proxy", "create", "--prefix", prefix, "--target", "http://upstream:7474",
             "--username", "neo4j", "--password", secret],
            base_url=server_url,
        )
        assert data["name"] == name
        assert secret not in proc.stdout.decode("utf-8")
        assert secret not in proc.stderr.decode("utf-8")

        proc, detail = _run_cli_json(["proxy", "get", name], base_url=server_url)
        assert detail["route"]["auth_type"] == "basic"
        assert detail["route"]["basic_auth"]["username"] == "neo4j"
        assert detail["route"]["basic_auth"]["password_configured"] is True
        assert "password" not in detail["route"]["basic_auth"]
        out = proc.stdout.decode("utf-8")
        assert secret not in out

        proc, listing = _run_cli_json(["proxy", "ls"], base_url=server_url)
        entry = next(r for r in listing if r["name"] == name)
        assert entry["route"]["auth_type"] == "basic"
        assert "password" not in entry["route"]["basic_auth"]
        assert secret not in proc.stdout.decode("utf-8")

    def test_cli_proxy_create_basic_password_stdin(
        self, server_url, tracking_proxy_routes
    ):
        """``--password-stdin`` reads stdin, sends via REST; secret not in argv."""
        secret = "stdin-secret-pw-BBBB"
        prefix = self._unique_prefix()
        name = prefix.lstrip("/").replace("/", "-")
        tracking_proxy_routes.append(name)

        proc, data = _run_cli_json(
            ["proxy", "create", "--prefix", prefix, "--target", "http://upstream:7474",
             "--username", "neo4j", "--password-stdin"],
            base_url=server_url,
            input_bytes=secret.encode("utf-8") + b"\n",
        )
        assert data["name"] == name
        assert secret not in proc.stdout.decode("utf-8")
        assert secret not in proc.stderr.decode("utf-8")

        proc, detail = _run_cli_json(["proxy", "get", name], base_url=server_url)
        assert detail["route"]["auth_type"] == "basic"
        assert detail["route"]["basic_auth"]["password_configured"] is True
        assert "password" not in detail["route"]["basic_auth"]

    def test_cli_proxy_create_basic_password_file(
        self, server_url, tracking_proxy_routes, tmp_path
    ):
        """``--password-file`` passes a server-side path; CLI never reads it."""
        secret = "file-secret-pw-CCCC"
        pw_file = tmp_path / "neo4j_pw"
        pw_file.write_text(secret + "\n")
        prefix = self._unique_prefix()
        name = prefix.lstrip("/").replace("/", "-")
        tracking_proxy_routes.append(name)

        proc, data = _run_cli_json(
            ["proxy", "create", "--prefix", prefix, "--target", "http://upstream:7474",
             "--username", "neo4j", "--password-file", str(pw_file)],
            base_url=server_url,
        )
        assert data["name"] == name
        assert secret not in proc.stdout.decode("utf-8")
        assert secret not in proc.stderr.decode("utf-8")

        proc, detail = _run_cli_json(["proxy", "get", name], base_url=server_url)
        assert detail["route"]["auth_type"] == "basic"
        assert detail["route"]["basic_auth"]["password_file"] == str(pw_file)
        assert "password" not in detail["route"]["basic_auth"]
        assert secret not in proc.stdout.decode("utf-8")

    @staticmethod
    def test_cli_proxy_basic_password_sources_mutually_exclusive(server_url):
        secret = "leakcheck-value-XYZ"
        proc = _run_cli(
            ["proxy", "create", "--prefix", "/mux1", "--target", "http://up:7474",
             "--username", "u", "--password", secret, "--password-file", "/tmp/x"],
            base_url=server_url,
        )
        assert proc.returncode != 0
        err = proc.stderr.decode("utf-8")
        assert "mutually exclusive" in err
        assert secret not in err  # rejected password value is not echoed

    @staticmethod
    def test_cli_proxy_basic_password_and_stdin_mutually_exclusive(server_url):
        proc = _run_cli(
            ["proxy", "create", "--prefix", "/mux2", "--target", "http://up:7474",
             "--username", "u", "--password", "p", "--password-stdin"],
            base_url=server_url,
            input_bytes=b"q\n",
        )
        assert proc.returncode != 0
        assert "mutually exclusive" in proc.stderr.decode("utf-8")

    @staticmethod
    def test_cli_proxy_basic_username_required(server_url):
        proc = _run_cli(
            ["proxy", "create", "--prefix", "/nouser", "--target", "http://up:7474",
             "--password", "p"],
            base_url=server_url,
        )
        assert proc.returncode != 0
        assert "username" in proc.stderr.decode("utf-8").lower()

    @staticmethod
    def test_cli_proxy_basic_password_source_required(server_url):
        proc = _run_cli(
            ["proxy", "create", "--prefix", "/nosrc", "--target", "http://up:7474",
             "--username", "u"],
            base_url=server_url,
        )
        assert proc.returncode != 0
        assert "one of --password, --password-file or --password-stdin" in proc.stderr.decode("utf-8")

    @staticmethod
    def test_cli_proxy_basic_api_key_mutex(server_url):
        proc = _run_cli(
            ["proxy", "create", "--prefix", "/keymux", "--target", "http://up:7474",
             "--api-key", "sk", "--username", "u", "--password", "p"],
            base_url=server_url,
        )
        assert proc.returncode != 0
        assert "mutually exclusive" in proc.stderr.decode("utf-8")

    def test_cli_proxy_update_basic_redacted(
        self, server_url, tracking_proxy_routes
    ):
        """``proxy update`` accepts Basic options; output stays redacted."""
        prefix = self._unique_prefix()
        name = prefix.lstrip("/").replace("/", "-")
        tracking_proxy_routes.append(name)

        _run_cli_json(
            ["proxy", "create", "--prefix", prefix, "--target", "http://up:7474"],
            base_url=server_url,
        )
        secret = "upd-secret-pw-DDDD"
        proc, _ = _run_cli_json(
            ["proxy", "update", name, "--prefix", prefix, "--target", "http://up:7474",
             "--username", "neo4j", "--password", secret],
            base_url=server_url,
        )
        assert secret not in proc.stdout.decode("utf-8")
        assert secret not in proc.stderr.decode("utf-8")

        proc, detail = _run_cli_json(["proxy", "get", name], base_url=server_url)
        assert detail["route"]["auth_type"] == "basic"
        assert detail["route"]["basic_auth"]["password_configured"] is True
        assert "password" not in detail["route"]["basic_auth"]
        assert secret not in proc.stdout.decode("utf-8")

    @staticmethod
    def test_cli_proxy_help_documents_basic_options():
        """Help lists Basic options and the --password dev/test warning."""
        proc = _run_cli(["proxy", "create", "--help"], base_url="http://127.0.0.1:8321")
        assert proc.returncode == 0, proc.stderr
        text = proc.stdout.decode("utf-8")
        assert "--username" in text
        assert "--password" in text
        assert "--password-file" in text
        assert "--password-stdin" in text
        assert "DEV/TEST" in text or "dev/test" in text.lower()
