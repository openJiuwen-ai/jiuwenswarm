# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Execute a test framework and turn its output into a compact, structured result.

Called by the ``run_tests`` tool. For each framework it builds an argv (no
shell), runs it in the workspace root with a timeout (killing the whole
process group on expiry), parses counts and failing test ids from the output,
writes the full output to a spill file the model can read back through the
Code-mode sandbox, and renders a summary capped at ``max_summary_chars``.

It never raises for test failures; only programming errors propagate. The
``outcome`` field is what the implementation gate consumes:

* ``passed``   — the command ran, exit 0, and at least one test *executed*
  (skipped / deselected tests do not count);
* ``failed``   — tests ran and at least one failed;
* ``no_tests`` — the command ran but executed nothing: nothing collected, every
  test skipped, or (JS runners) no parseable test counts;
* ``error``    — collection/usage error, missing runner, or unparseable failure;
* ``timeout``  — killed after ``timeout`` seconds.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import re
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from jiuwenswarm.agents.harness.code.verification.discovery import Framework

DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_SUMMARY_CHARS = 6000
DEFAULT_SPILL_MAX_BYTES = 50 * 1024 * 1024
_TAIL_LINES_PER_FAILURE = 12
_MAX_FAILURES_LISTED = 15

# Interpreter settings a bundled launcher may have exported must not reach the
# project's own test run (they break `import <pkg>` from the repository root).
_SCRUBBED_ENV = ("PYTHONSAFEPATH", "PYTHONNOUSERSITE", "PYTHONHOME")
# Variables that inject runner options, plugins or wrapper executables. They are
# dropped from the child environment unless ``inherit_runner_env`` is set, so the
# only option channel stays the allow-listed ``extra_args``.
RUNNER_ENV_VARS = (
    "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "NODE_OPTIONS", "GOFLAGS", "RUSTC_WRAPPER",
    "RUSTC_WORKSPACE_WRAPPER", "CARGO_BUILD_RUSTC_WRAPPER", "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS", "RUSTDOCFLAGS",
)
# In-memory output kept for parsing: the head and the tail of the stream.
_MEM_HEAD_BYTES = 1024 * 1024
_MEM_TAIL_BYTES = 4 * 1024 * 1024
# After the test process exits, how long to wait for children that still hold
# its stdout before the process group is killed.
_PIPE_DRAIN_SECONDS = 2.0
# Spill files younger than this are never deleted by another call's cap
# enforcement (a concurrent call may still be writing or reporting it).
_SPILL_GRACE_SECONDS = 120.0
_CHUNK = 64 * 1024
# Probe for pytest in the project's interpreter (config-free pytest projects).
_PYTEST_PROBE_SECONDS = 20.0
_PYTEST_PROBE = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pytest') else 3)"
# Interpreters known to have pytest; misses are re-probed (pytest may be installed later).
_PYTEST_OK: set[str] = set()

_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b", re.IGNORECASE
)


@dataclass(slots=True)
class Failure:
    test_id: str
    message: str = ""
    tail: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunResult:
    """Outcome of one framework invocation."""

    framework: str
    language: str
    argv: list[str]
    outcome: str  # passed | failed | no_tests | error | timeout
    exit_code: int | None
    duration_s: float
    counts: dict[str, int] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    note: str = ""
    spill_path: str = ""
    output_tail: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"


# ─── command construction ─────────────────────────────────────────────────


_VENV_BIN = "Scripts" if os.name == "nt" else "bin"
_VENV_PY = "python.exe" if os.name == "nt" else "python"


def find_python(root: str | Path | None = None, override: str | None = None) -> str | None:
    """The project's interpreter (never the bundled one).

    Order: ``override`` (``modes.code.run_tests.python``), ``<root>/.venv``,
    ``<root>/venv``, ``$VIRTUAL_ENV``, then ``python`` / ``python3`` on PATH.
    A desktop user rarely has an activated shell, so the project's own
    virtual environment is preferred over whatever is first on PATH.
    """

    if override:
        candidate = Path(str(override)).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        found = shutil.which(str(override))
        if found:
            return found
    candidates: list[Path] = []
    if root is not None:
        base = Path(root)
        candidates += [base / ".venv" / _VENV_BIN / _VENV_PY, base / "venv" / _VENV_BIN / _VENV_PY]
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        candidates.append(Path(venv) / _VENV_BIN / _VENV_PY)
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return found
    return None


def build_argv(
    fw: Framework,
    targets: list[str],
    extra_args: list[str],
    *,
    root: str | Path | None = None,
    python: str | None = None,
) -> list[str] | None:
    """Return the argv for ``fw`` or None when its runner is not installed.

    Targets always come last and, where the runner supports it, after ``--`` so
    a target can never be read as an option (callers reject ``-``-prefixed
    targets as well).
    """

    extra = [str(a) for a in extra_args or []]
    if fw.name in ("pytest", "unittest"):
        interpreter = find_python(root, python)
        if interpreter is None:
            return None
        if fw.name == "pytest":
            return [
                interpreter, "-m", "pytest", "-q", "-rfE", "--no-header", "--tb=short",
                "-p", "no:cacheprovider", *extra, *(["--", *targets] if targets else []),
            ]
        return [interpreter, "-m", "unittest", *extra, *(targets or ["discover"])]
    if fw.name in ("jest", "vitest"):
        if shutil.which("npx") is None:
            return None
        base = ["npx", "--no-install", "jest", "--ci"] if fw.name == "jest" else [
            "npx", "--no-install", "vitest", "run",
        ]
        if fw.name == "jest":
            return [*base, *extra, *(["--", *targets] if targets else [])]
        # vitest's CLI parser hands arguments after "--" to the test process
        # instead of treating them as filters, so targets stay positional here;
        # the tool rejects "-"-prefixed targets before they get this far.
        return [*base, *extra, *targets]
    if fw.name == "npm":
        if shutil.which("npm") is None:
            return None
        tail = [*extra, *targets]
        return ["npm", "run", fw.npm_script or "test", "--silent", *(["--", *tail] if tail else [])]
    if fw.name == "go":
        if shutil.which("go") is None:
            return None
        return ["go", "test", *extra, *(targets or ["./..."])]
    if fw.name == "cargo":
        if shutil.which("cargo") is None:
            return None
        return ["cargo", "test", *extra]
    return None


# ─── output parsing ───────────────────────────────────────────────────────


def _counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in text.splitlines()[-40:]:
        for num, word in _COUNT_RE.findall(line):
            key = word.lower()
            key = "errors" if key.startswith("error") else key
            counts[key] = int(num)
    return counts


def parse_pytest(text: str) -> tuple[dict[str, int], list[Failure]]:
    counts = _counts(text)
    failures: list[Failure] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$", line.strip())
        if m and m.group(2) not in seen:
            seen.add(m.group(2))
            failures.append(Failure(m.group(2), (m.group(3) or m.group(1)).strip()))
    # --tb=short sections: "____ test_name ____" followed by the traceback
    sections = re.split(r"^_{3,}\s+(.+?)\s+_{3,}\s*$", text, flags=re.MULTILINE)
    tails: dict[str, list[str]] = {}
    for i in range(1, len(sections) - 1, 2):
        body = [ln for ln in sections[i + 1].splitlines() if ln.strip()]
        body = [ln for ln in body if not re.match(r"^=+ .*=+$", ln)]
        tails[sections[i].strip()] = body[-_TAIL_LINES_PER_FAILURE:]
    for failure in failures:
        leaf = failure.test_id.split("::")[-1]
        # Exact section-title matches first ("test_a" must not take "test_ab"'s
        # traceback), then class-qualified titles, then the old substring match.
        body = tails.get(leaf)
        if body is None:
            body = next((b for key, b in tails.items() if key.endswith("." + leaf)), None)
        if body is None:
            body = next((b for key, b in tails.items() if leaf in key), None)
        if body is not None:
            failure.tail = body
    return counts, failures


def parse_go(text: str) -> tuple[dict[str, int], list[Failure]]:
    failures = [Failure(m, "") for m in re.findall(r"^\s*--- FAIL: (\S+)", text, re.MULTILINE)]
    # "ok pkg 0.01s [no tests to run]" (a -run filter matched nothing) and
    # "?   pkg [no test files]" executed nothing and must not count as a pass.
    ok_lines = [ln for ln in re.findall(r"^ok\s+\S+.*$", text, re.MULTILINE) if "[no tests to run]" not in ln]
    passed = len(re.findall(r"^\s*--- PASS: ", text, re.MULTILINE)) or len(ok_lines)
    counts = {"passed": passed, "failed": len(failures)} if (passed or failures) else {}
    return counts, failures


def parse_js(text: str) -> tuple[dict[str, int], list[Failure]]:
    counts: dict[str, int] = {}
    m = re.search(r"^\s*Tests:?\s+(.*)$", text, re.MULTILINE)
    if m:
        for num, word in re.findall(r"(\d+)\s+(passed|failed|skipped|todo)", m.group(1)):
            counts[word] = int(num)
    failures = [Failure(t.strip(), "") for t in re.findall(r"^\s*●\s+(.+)$", text, re.MULTILINE)]
    failures += [Failure(t.strip(), "") for t in re.findall(r"^\s*(?:FAIL|×)\s+(.+?)\s*$", text, re.MULTILINE)]
    return counts, failures[: _MAX_FAILURES_LISTED * 2]


def parse_cargo(text: str) -> tuple[dict[str, int], list[Failure]]:
    counts: dict[str, int] = {}
    for m in re.finditer(r"test result: \w+\. (\d+) passed; (\d+) failed", text):
        counts["passed"] = counts.get("passed", 0) + int(m.group(1))
        counts["failed"] = counts.get("failed", 0) + int(m.group(2))
    failures = [Failure(t, "") for t in re.findall(r"^test (\S+) \.\.\. FAILED", text, re.MULTILINE)]
    return counts, failures


def _executed(framework: str, counts: dict[str, int]) -> int:
    """Tests that actually ran and verified something.

    Skipped / deselected / todo never count, and neither do expected failures
    (``xfailed``) or unexpected passes of xfail-marked tests (``xpassed``): a
    run made only of those verifies nothing about the change.
    """

    if framework == "unittest" and "ran" in counts:
        return max(0, counts["ran"] - counts.get("skipped", 0) - counts.get("expected_failures", 0)
                   - counts.get("unexpected_successes", 0))
    return sum(counts.get(k, 0) for k in ("passed", "failed", "errors"))


def classify(fw: Framework, exit_code: int | None, text: str) -> tuple[str, dict[str, int], list[Failure], str]:
    """Return ``(outcome, counts, failures, note)`` for a finished run."""

    if fw.name in ("pytest",):
        counts, failures = parse_pytest(text)
        if exit_code == 5:
            return "no_tests", counts, failures, "pytest collected no tests"
        if exit_code in (2, 3, 4):
            note = "collection or usage error" if exit_code in (2, 4) else "pytest internal error"
            return "error", counts, failures, note
    elif fw.name == "unittest":
        counts, failures = parse_pytest(text)
        ran = re.search(r"^Ran (\d+) tests?", text, re.MULTILINE)
        if ran:
            counts.setdefault("ran", int(ran.group(1)))
            if int(ran.group(1)) == 0:
                return "no_tests", counts, failures, "unittest ran 0 tests"
        skipped = re.search(r"\bskipped=(\d+)", text)
        if skipped:
            counts["skipped"] = int(skipped.group(1))
        for key, pattern in (("expected_failures", r"\bexpected failures=(\d+)"),
                             ("unexpected_successes", r"\bunexpected successes=(\d+)")):
            found = re.search(pattern, text)
            if found:
                counts[key] = int(found.group(1))
        m = re.search(r"FAILED \((.*)\)", text)
        if m:
            for key, num in re.findall(r"(failures|errors)=(\d+)", m.group(1)):
                counts["failed" if key == "failures" else "errors"] = int(num)
    elif fw.name == "go":
        counts, failures = parse_go(text)
    elif fw.name == "cargo":
        counts, failures = parse_cargo(text)
    else:
        counts, failures = parse_js(text)

    executed = _executed(fw.name, counts)
    if exit_code == 0:
        if executed == 0:
            if counts.get("xfailed") or counts.get("xpassed") or counts.get("expected_failures"):
                return "no_tests", counts, failures, "only expected failures (xfail) ran; nothing was verified"
            if counts.get("skipped") or counts.get("deselected") or counts.get("todo"):
                return "no_tests", counts, failures, "every collected test was skipped or deselected"
            return "no_tests", counts, failures, "no executed tests in the output"
        return "passed", counts, failures, ""
    if failures or counts.get("failed") or counts.get("errors"):
        return "failed", counts, failures, ""
    return "error", counts, failures, f"exit code {exit_code} without parseable test results"


# ─── execution ────────────────────────────────────────────────────────────


def _clean_env(*, inherit_runner_env: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    for key in _SCRUBBED_ENV:
        env.pop(key, None)
    if not inherit_runner_env:
        for key in RUNNER_ENV_VARS:
            env.pop(key, None)
    env.setdefault("CI", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


class _OutputSink:
    """Collects a process's output with bounded memory and an optional spill file.

    Memory keeps the first ``_MEM_HEAD_BYTES`` and the last ``_MEM_TAIL_BYTES``
    (parsers only need the summary lines, which are at the end). The spill file
    receives the stream up to ``file_cap`` bytes: the head is written as it
    arrives and, when the stream was longer, a truncation marker plus the tail
    is appended at the end, so the file never exceeds its cap.
    """

    def __init__(self, path: Path | None, file_cap: int) -> None:
        self.head = bytearray()
        self._tail_chunks: collections.deque[bytes] = collections.deque()
        self._tail_len = 0
        self.total = 0
        self.path = path
        self._fh = None
        self._written = 0
        if file_cap > 0:
            self._tail_in_file = min(_MEM_TAIL_BYTES, file_cap // 2)
            self._head_limit = max(0, file_cap - self._tail_in_file - 256)
        else:
            self._tail_in_file = _MEM_TAIL_BYTES
            self._head_limit = -1  # unlimited
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = open(path, "wb")  # noqa: SIM115 - closed in finish()
            except OSError:
                self._fh = None
                self.path = None

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = _MEM_HEAD_BYTES - len(self.head)
        rest = chunk
        if room > 0:
            self.head += chunk[:room]
            rest = chunk[room:]
        if rest:
            # A deque of chunks: trimming a bytearray from the front keeps its
            # allocation, so memory would grow with the stream.
            self._tail_chunks.append(bytes(rest))
            self._tail_len += len(rest)
            while self._tail_chunks and self._tail_len - len(self._tail_chunks[0]) >= _MEM_TAIL_BYTES:
                self._tail_len -= len(self._tail_chunks.popleft())
        if self._fh is not None:
            if self._head_limit < 0:
                self._write(chunk)
            elif self._written < self._head_limit:
                self._write(chunk[: self._head_limit - self._written])

    @property
    def tail(self) -> bytes:
        data = b"".join(self._tail_chunks)
        return data[-_MEM_TAIL_BYTES:]

    def _write(self, data: bytes) -> None:
        try:
            self._fh.write(data)  # type: ignore[union-attr]
            self._written += len(data)
        except OSError:
            self._close()
            self.path = None

    def _close(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        except OSError:
            pass
        self._fh = None

    def finish(self) -> None:
        if self._fh is not None and self._head_limit >= 0:
            tail = self.tail
            missing = self.total - self._written
            if 0 < missing <= self._tail_in_file and missing <= len(tail):
                self._write(tail[-missing:])
            elif missing > 0:
                keep = min(self._tail_in_file, len(tail))
                dropped = self.total - self._written - keep
                self._write(f"\n... [run_tests: {dropped} bytes of output dropped to respect the spill cap] ...\n".encode())
                if keep:
                    self._write(tail[-keep:])
        self._close()

    def text(self) -> str:
        head = self.head.decode("utf-8", errors="replace")
        tail = self.tail
        dropped = self.total - len(self.head) - len(tail)
        middle = f"\n... [run_tests: {dropped} bytes of output not kept in memory] ...\n" if dropped > 0 else ""
        return head + middle + tail.decode("utf-8", errors="replace")


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


class _FdReader:
    """Reads a pipe the runner owns (POSIX), so it can be closed at any time.

    asyncio's own subprocess pipes keep ``Process.wait()`` pending until every
    holder of the write end exits; a helper the tests started in its own
    session would then turn a finished run into a timeout.
    """

    def __init__(self, fd: int, sink: _OutputSink) -> None:
        self.fd = fd
        self.sink = sink
        self.loop = asyncio.get_running_loop()
        self.eof = self.loop.create_future()
        os.set_blocking(fd, False)
        self.loop.add_reader(fd, self._on_readable)

    def _on_readable(self) -> None:
        for _ in range(64):  # bounded work per wake-up
            try:
                chunk = os.read(self.fd, _CHUNK)
            except BlockingIOError:
                return
            except OSError:
                chunk = b""
            if not chunk:
                self.close()
                return
            self.sink.feed(chunk)

    def close(self) -> None:
        if self.fd < 0:
            return
        with contextlib.suppress(Exception):
            self.loop.remove_reader(self.fd)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self.fd = -1
        if not self.eof.done():
            self.eof.set_result(None)


async def _wait_exit(proc: asyncio.subprocess.Process, timeout: float) -> bool:
    """Poll for the process's own exit; True when it exited within ``timeout``."""

    deadline = time.monotonic() + timeout
    delay = 0.01
    while proc.returncode is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, 0.1)
    return True


async def _run(
    argv: list[str],
    cwd: Path,
    timeout: float,
    sink: _OutputSink | None = None,
    *,
    inherit_runner_env: bool = False,
) -> tuple[int | None, str, bool]:
    """Run ``argv``; return ``(exit_code, output, timed_out)``.

    The exit code is the test process's own. Once it exits, children that still
    hold its output get ``_PIPE_DRAIN_SECONDS`` before the process group is
    killed and the pipe closed, so a stray background helper can neither turn a
    finished run into a timeout nor stall the call. The group is killed on every
    path; a child that moved to its own session survives but loses the pipe.
    """

    sink = sink or _OutputSink(None, 0)
    env = _clean_env(inherit_runner_env=inherit_runner_env)
    if os.name == "nt":  # no add_reader on the Proactor loop: plain pipes
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        )
        assert proc.stdout is not None
        stream = proc.stdout

        async def _pump() -> None:
            while chunk := await stream.read(_CHUNK):
                sink.feed(chunk)

        pump = asyncio.ensure_future(_pump())
        exited = await _wait_exit(proc, timeout)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(pump), timeout=_PIPE_DRAIN_SECONDS if exited else 0.01)
        _kill_group(proc)
        pump.cancel()
        sink.finish()
        return (proc.returncode if exited else None), sink.text(), not exited

    read_fd, write_fd = os.pipe()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
            stdout=write_fd, stderr=write_fd, env=env, start_new_session=True,
        )
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    os.close(write_fd)
    reader = _FdReader(read_fd, sink)
    try:
        exited = await _wait_exit(proc, timeout)
        if exited:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(reader.eof), timeout=_PIPE_DRAIN_SECONDS)
        _kill_group(proc)
        if not reader.eof.done():
            # Take what the killed processes already wrote, then drop the pipe.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(reader.eof), timeout=0.2)
        if not exited:
            await _wait_exit(proc, _PIPE_DRAIN_SECONDS)
    finally:
        reader.close()
    sink.finish()
    return (proc.returncode if exited else None), sink.text(), not exited


def default_spill_root() -> Path:
    """``<agent workspace>/run_tests`` — readable through the Code-mode sandbox.

    Mirrors the context-diet spill location: the agent's own workspace from the
    sys_operation cwd ContextVar when it differs from the project root and the
    shell cwd (``restrict_to_sandbox`` allows it, and nothing lands inside the
    user's checkout), otherwise the shared agent workspace directory.
    """
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    workspace: Path | None = None
    try:
        from openjiuwen.core.sys_operation.cwd import (
            get_cwd,
            get_project_root,
            get_workspace,
        )

        raw = get_workspace()
        if raw:
            candidate = Path(raw).expanduser().resolve()
            excluded = {Path(p).expanduser().resolve() for p in (get_project_root(), get_cwd()) if p}
            if candidate not in excluded:
                workspace = candidate
    except Exception:  # noqa: BLE001 - ContextVar not seeded (tests, offline use)
        workspace = None
    if workspace is None:
        workspace = get_agent_workspace_dir()
    return workspace / "run_tests"


def _enforce_cap(directory: Path, max_bytes: int, keep: Path) -> None:
    """Delete the oldest spill files until the directory fits ``max_bytes``.

    ``keep`` (this call's file) and files younger than ``_SPILL_GRACE_SECONDS``
    (a concurrent call may still be writing or reporting them) are never
    deleted; each file is already capped at ``max_bytes`` when written.
    """

    if max_bytes <= 0:
        return
    now = time.time()
    try:
        files = sorted((p for p in directory.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
    except OSError:
        return
    for path in files:
        if total <= max_bytes:
            break
        try:
            stat = path.stat()
        except OSError:
            continue
        if path == keep or now - stat.st_mtime < _SPILL_GRACE_SECONDS:
            continue
        try:
            path.unlink()
            total -= stat.st_size
        except OSError:
            continue


def _spill_file(spill_dir: Path | None, label: str) -> Path | None:
    if spill_dir is None:
        return None
    return spill_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}-{uuid.uuid4().hex}.log"


def spill(text: str, spill_dir: Path | None, label: str, max_bytes: int) -> str:
    """Write ``text`` as a spill file (capped at ``max_bytes``); return its path or ""."""

    path = _spill_file(spill_dir, label)
    if path is None:
        return ""
    sink = _OutputSink(path, max_bytes)
    sink.feed(text.encode("utf-8", errors="replace"))
    sink.finish()
    if sink.path is None:
        return ""
    _enforce_cap(path.parent, max_bytes, keep=path)
    return str(path)


async def pytest_importable(
    interpreter: str,
    root: Path,
    *,
    timeout: float = _PYTEST_PROBE_SECONDS,
    inherit_runner_env: bool = False,
) -> bool | None:
    """Whether ``interpreter`` can import pytest (None when the probe itself failed)."""

    if interpreter in _PYTEST_OK:
        return True
    try:
        proc = await asyncio.create_subprocess_exec(
            interpreter, "-c", _PYTEST_PROBE,
            cwd=str(root),
            env=_clean_env(inherit_runner_env=inherit_runner_env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=max(1.0, timeout))
    except TimeoutError:
        _kill_group(proc)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        return None
    if code == 0:
        _PYTEST_OK.add(interpreter)
        return True
    return False if code == 3 else None


def _unittest_targets(targets: list[str]) -> list[str]:
    """pytest targets (``file.py::test``) as unittest targets (files only)."""

    out: list[str] = []
    for target in targets:
        path = target.split("::", 1)[0]
        if path and path not in out:
            out.append(path)
    return out


async def run_framework(
    fw: Framework,
    root: Path,
    targets: list[str],
    extra_args: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    spill_dir: Path | None = None,
    spill_max_bytes: int = DEFAULT_SPILL_MAX_BYTES,
    python: str | None = None,
    inherit_runner_env: bool = False,
) -> RunResult:
    if fw.name == "pytest" and not fw.config_file:
        # Found only by test-file names: make sure the project's interpreter
        # actually has pytest before promising a pytest run.
        interpreter = find_python(root, python)
        if interpreter is not None:
            probe_started = time.monotonic()
            available = await pytest_importable(
                interpreter, root,
                timeout=min(_PYTEST_PROBE_SECONDS, max(1.0, timeout / 2)),
                inherit_runner_env=inherit_runner_env,
            )
            timeout = max(1.0, timeout - (time.monotonic() - probe_started))
            if available is False:
                missing = f"pytest is not installed in {interpreter}"
                if fw.unittest_compatible:
                    result = await run_framework(
                        Framework("unittest", fw.language, ""), root, _unittest_targets(targets), [],
                        timeout=timeout, spill_dir=spill_dir, spill_max_bytes=spill_max_bytes,
                        python=python, inherit_runner_env=inherit_runner_env,
                    )
                    dropped = " (extra_args not applied)" if extra_args else ""
                    prefix = f"{missing}; ran the tests with unittest instead{dropped}"
                    result.note = f"{prefix}; {result.note}" if result.note else prefix
                    return result
                return RunResult(
                    fw.name, fw.language, [], "error", None, 0.0, targets=list(targets),
                    note=(
                        f"{missing}. The project has pytest-style test files but no pytest "
                        "configuration. Install pytest in the project's environment (for example "
                        "its .venv), set modes.code.run_tests.python to an interpreter that has "
                        "it, or run the tests with bash."
                    ),
                )
    argv = build_argv(fw, targets, extra_args, root=root, python=python)
    if argv is None:
        where = "a project virtualenv or PATH" if fw.name in ("pytest", "unittest") else "PATH"
        return RunResult(
            fw.name, fw.language, [], "error", None, 0.0, targets=list(targets),
            note=f"{fw.name} runner not found ({where})",
        )
    sink = _OutputSink(_spill_file(spill_dir, fw.name), spill_max_bytes)
    started = time.monotonic()
    try:
        exit_code, text, timed_out = await _run(
            argv, root, timeout, sink, inherit_runner_env=inherit_runner_env
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        sink.finish()
        if sink.path is not None:
            try:
                sink.path.unlink()
            except OSError:
                pass
        return RunResult(
            fw.name, fw.language, argv, "error", None, time.monotonic() - started,
            targets=list(targets), note=f"could not start {argv[0]}: {exc}",
        )
    duration = time.monotonic() - started
    path = ""
    if sink.path is not None:
        path = str(sink.path)
        _enforce_cap(sink.path.parent, spill_max_bytes, keep=sink.path)
    tail = [ln for ln in text.splitlines()[-400:] if ln.strip()][-20:]
    if timed_out:
        return RunResult(
            fw.name, fw.language, argv, "timeout", None, duration, targets=list(targets),
            note=f"killed after {timeout:.0f}s", spill_path=path, output_tail=tail,
        )
    outcome, counts, failures, note = classify(fw, exit_code, text)
    return RunResult(
        fw.name, fw.language, argv, outcome, exit_code, duration, counts=counts,
        failures=failures, targets=list(targets), note=note, spill_path=path, output_tail=tail,
    )


# ─── rendering ────────────────────────────────────────────────────────────


def overall_outcome(results: list[RunResult]) -> str:
    """Aggregate: passed only when every framework that ran passed."""

    if not results:
        return "no_tests"
    order = ("timeout", "error", "failed", "no_tests", "passed")
    return min(results, key=lambda r: order.index(r.outcome)).outcome


def render_summary(results: list[RunResult], *, max_chars: int = DEFAULT_MAX_SUMMARY_CHARS, notes: list[str] | None = None) -> str:
    """Model-facing summary; the first line is machine-readable for the gate."""

    outcome = overall_outcome(results)
    langs = ",".join(sorted({r.language for r in results if r.passed}))
    lines = [f"run_tests: outcome={outcome} passed_languages={langs or '-'}"]
    for note in notes or []:
        lines.append(f"note: {note}")
    for r in results:
        counts = ", ".join(f"{v} {k}" for k, v in r.counts.items()) or "no counts"
        exit_text = "timeout" if r.exit_code is None and r.outcome == "timeout" else str(r.exit_code)
        lines.append(
            f"[{r.language} / {r.framework}] {r.outcome} (exit {exit_text}, {r.duration_s:.1f}s): {counts}"
        )
        if r.argv:
            lines.append("command: " + " ".join(r.argv[1:] if r.framework in ("pytest", "unittest") else r.argv))
        if r.note:
            lines.append(f"note: {r.note}")
        for failure in r.failures[:_MAX_FAILURES_LISTED]:
            lines.append(f"- {failure.test_id}" + (f" — {failure.message}" if failure.message else ""))
            for tail_line in failure.tail:
                lines.append("    " + tail_line[:300])
        if len(r.failures) > _MAX_FAILURES_LISTED:
            lines.append(f"... {len(r.failures) - _MAX_FAILURES_LISTED} more failing tests (see full output)")
        if r.outcome in ("error", "timeout") and not r.failures and r.output_tail:
            lines.append("output tail:")
            lines.extend("    " + ln[:300] for ln in r.output_tail[-12:])
        if r.spill_path:
            lines.append(f"full output: {r.spill_path} (read_file offset/limit or grep it)")
    text = "\n".join(lines)
    if len(text) > max_chars:
        cut = text[: max(0, max_chars - 80)]
        cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
        text = cut + "\n... summary truncated; the full output file has everything"
    return text


__all__ = [
    "DEFAULT_MAX_SUMMARY_CHARS",
    "DEFAULT_SPILL_MAX_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "RUNNER_ENV_VARS",
    "Failure",
    "RunResult",
    "build_argv",
    "classify",
    "default_spill_root",
    "find_python",
    "overall_outcome",
    "pytest_importable",
    "render_summary",
    "run_framework",
    "spill",
]
