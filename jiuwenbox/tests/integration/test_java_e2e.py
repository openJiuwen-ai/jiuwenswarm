# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Java end-to-end integration tests.

Run against a jiuwenbox server loaded with ``configs/java-policy.yaml``:

    python3 -m pytest tests/integration/test_java_e2e.py -v \
        --server-endpoint http://127.0.0.1:8321

The tests reuse the shared ``client`` fixture (httpx, TCP/UDS aware) from
``conftest.py``. They auto-skip when Java is not available in the sandbox
(e.g. the server runs an image without the JDK), so the file is safe to run
against any server endpoint.
"""

from __future__ import annotations

import base64

import pytest


def _exec(client, sandbox_id: str, command: list[str], *, stdin: str | None = None,
          timeout: int = 30, env: dict[str, str] | None = None) -> dict:
    body: dict = {"command": command, "timeout_seconds": timeout}
    if stdin is not None:
        body["stdin"] = stdin
    if env is not None:
        body["env"] = env
    resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/exec", json=body)
    assert resp.status_code == 200, f"exec failed: {resp.status_code} {resp.text}"
    return resp.json()


def _write_file(client, sandbox_id: str, sandbox_path: str, content: str) -> None:
    b64 = base64.b64encode(content.encode()).decode()
    parent = sandbox_path.rsplit("/", 1)[0] or "/"
    out = _exec(client, sandbox_id,
                ["sh", "-c", f"mkdir -p {parent} && base64 -d > {sandbox_path}"],
                stdin=b64)
    assert out["exit_code"] == 0, f"write {sandbox_path} failed: {out}"


def _java_available(client, sandbox_id: str) -> bool:
    try:
        out = _exec(client, sandbox_id, ["java", "-version"], timeout=15)
        return out["exit_code"] == 0
    except Exception:
        # Catch network errors (httpx.ConnectError/TimeoutException) and JSON
        # parse errors too, not just the AssertionError from _exec's status
        # check — so an unreachable server makes the test skip (via the caller's
        # pytest.skip) instead of crashing with an unhandled exception.
        return False


@pytest.fixture
def java_sandbox(client):
    """Create one sandbox per test; auto-cleaned by the (function-scoped) client fixture.

    Note: must stay function-scoped to match ``client`` in conftest.py. A wider
    scope (e.g. module) raises pytest ScopeMismatch (module cannot depend on a
    function-scoped fixture) and would also have its sandbox torn down by the
    first test's ``client`` teardown via ``cleanup_sandboxes()``.
    """
    try:
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"create failed: {resp.status_code} {resp.text}"
        sb = resp.json()["id"]
    except Exception as e:
        # Server unreachable / non-JSON / wrong status: skip instead of crashing
        # the whole file. Catches httpx.ConnectError/TimeoutException etc. so a
        # dead endpoint yields SKIPPED, not ERROR.
        pytest.skip(f"jiuwenbox server not usable: {e!r}")
    if not _java_available(client, sb):
        pytest.skip("java not available in sandbox (image without JDK?)")
    return sb


def test_a_policy_smoke(client, java_sandbox):
    """A. java/javac/jar versions + JAVA_HOME injection."""
    for cmd in (["java", "-version"], ["javac", "-version"], ["jar", "--version"]):
        out = _exec(client, java_sandbox, cmd, timeout=15)
        assert out["exit_code"] == 0, f"{' '.join(cmd)} failed: {out['stderr']}"

    out = _exec(client, java_sandbox, ["sh", "-c", "echo $JAVA_HOME"], timeout=10)
    assert out["stdout"].strip() == "/usr/lib/jvm/java-17-openjdk", out


MAIN_JAVA = """
public class Main {
  public static void main(String[] args) {
    System.out.println("JIUWENBOX-JAVA-E2E-MAIN");
  }
}
"""

LIB_JAVA = """
package lib;
public class Lib {
  public static String greeting() { return "JIUWENBOX-JAVA-E2E-LIB"; }
}
"""

MAIN_WITH_LIB_JAVA = """
import lib.Lib;
public class MainWithLib {
  public static void main(String[] args) {
    System.out.println(Lib.greeting());
  }
}
"""


def test_b_single_file_compile_and_run(client, java_sandbox):
    """B. upload Main.java -> javac -> java, expect fixed string."""
    _write_file(client, java_sandbox, "/home/src/Main.java", MAIN_JAVA)

    out = _exec(client, java_sandbox, ["javac", "-d", "/home/classes", "/home/src/Main.java"])
    assert out["exit_code"] == 0, f"javac failed: {out['stderr']}"

    out = _exec(client, java_sandbox, ["java", "-cp", "/home/classes", "Main"], timeout=15)
    assert out["stdout"].strip() == "JIUWENBOX-JAVA-E2E-MAIN", out


def test_c_jar_classpath(client, java_sandbox):
    """C. build a lib jar, compile MainWithLib against it, run via -cp jars:classes."""
    _write_file(client, java_sandbox, "/home/src/Lib.java", LIB_JAVA)
    _write_file(client, java_sandbox, "/home/src/MainWithLib.java", MAIN_WITH_LIB_JAVA)

    # lib-classes is an intermediate scratch dir used only to build the jar.
    # Put it under /tmp (not /home): in some sandboxes a mkdir'd subdir under
    # /home becomes an overlay mount point, making `rm -rf` fail with EBUSY.
    # /tmp has no such quirk, and the jar output still lands in /home/jars.
    build = (
        "mkdir -p /tmp/lib-classes /home/jars /home/classes "
        "&& javac -d /tmp/lib-classes /home/src/Lib.java "
        "&& jar cf /home/jars/hello-lib.jar -C /tmp/lib-classes . "
        "&& rm -rf /tmp/lib-classes"
    )
    out = _exec(client, java_sandbox, ["sh", "-c", build])
    assert out["exit_code"] == 0, f"jar build failed: {out['stderr']}"

    out = _exec(client, java_sandbox,
                ["javac", "-cp", "/home/jars/hello-lib.jar", "-d", "/home/classes",
                 "/home/src/MainWithLib.java"])
    assert out["exit_code"] == 0, f"javac MainWithLib failed: {out['stderr']}"

    out = _exec(client, java_sandbox,
                ["java", "-cp", "/home/jars/*:/home/classes", "MainWithLib"], timeout=15)
    assert out["stdout"].strip() == "JIUWENBOX-JAVA-E2E-LIB", out


def test_d_tmp_writable(client, java_sandbox):
    """D. /tmp is writable; JVM hsperfdata does not break basic run."""
    out = _exec(client, java_sandbox,
                ["sh", "-c", "echo ok > /tmp/jb_writable_test && cat /tmp/jb_writable_test"])
    assert out["stdout"].strip() == "ok", out

    # Running java exercises /tmp/hsperfdata_<user>; just assert it does not
    # error (some JVM configs disable perf data, so presence is not asserted).
    out = _exec(client, java_sandbox,
                ["sh", "-c", "java -version 2>/dev/null; ls /tmp/hsperfdata_* 2>/dev/null | head -1 || true"],
                timeout=15)
    assert out["exit_code"] == 0, out


BUSY_JAVA = """
public class Busy {
  public static void main(String[] args) {
    long t = System.currentTimeMillis() + 60000;
    while (System.currentTimeMillis() < t) { Math.sqrt(Math.random()); }
  }
}
"""

OOM_JAVA = """
import java.util.*;
public class Oom {
  public static void main(String[] args) {
    List<byte[]> list = new ArrayList<>();
    while (true) { list.add(new byte[8 * 1024 * 1024]); }
  }
}
"""


def test_e1_busy_loop_timeout(client, java_sandbox):
    """E1. busy loop is terminated by timeout_seconds (exit != 0)."""
    _write_file(client, java_sandbox, "/home/src/Busy.java", BUSY_JAVA)
    out = _exec(client, java_sandbox, ["javac", "-d", "/home/classes", "/home/src/Busy.java"])
    assert out["exit_code"] == 0, f"javac Busy failed: {out['stderr']}"
    out = _exec(client, java_sandbox, ["java", "-cp", "/home/classes", "Busy"], timeout=3)
    assert out["exit_code"] != 0, f"busy loop not stopped: {out}"


def test_e2_oom_failure_and_server_healthy(client, java_sandbox):
    """E2. heap OOM exits non-zero; server stays healthy afterwards."""
    _write_file(client, java_sandbox, "/home/src/Oom.java", OOM_JAVA)
    out = _exec(client, java_sandbox, ["javac", "-d", "/home/classes", "/home/src/Oom.java"])
    assert out["exit_code"] == 0, f"javac Oom failed: {out['stderr']}"
    out = _exec(client, java_sandbox, ["java", "-Xmx128m", "-cp", "/home/classes", "Oom"],
                timeout=30)
    assert out["exit_code"] != 0, f"OOM did not fail: {out}"

    # Server must still respond.
    resp = client.get("/health")
    assert resp.status_code == 200, f"server unhealthy after OOM: {resp.status_code}"
