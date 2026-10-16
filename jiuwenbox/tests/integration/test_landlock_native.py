"""Real Landlock + Seccomp only in disposable Linux child processes.

No deployed server or host credentials are used. bwrap deployment E2E remains
a separate acceptance step. Optional toolchains skip when not installed.
"""

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import textwrap

import pytest

from jiuwenbox.supervisor import landlock_launcher as launcher


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "aarch64"},
    reason="requires native Linux Landlock and Seccomp",
)

CHILD = """
import ctypes, json, os, socket, subprocess, sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from jiuwenbox.supervisor.landlock_launcher import apply_landlock
from jiuwenbox.supervisor.seccomp import build_seccomp_filter
from jiuwenbox.models.policy import SyscallPolicy
class Filter(ctypes.Structure):
    _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
class Program(ctypes.Structure):
    _fields_=[('len',ctypes.c_ushort),('filter',ctypes.POINTER(Filter))]
raw=build_seccomp_filter(SyscallPolicy())
instructions=(Filter*(len(raw)//8)).from_buffer_copy(raw)
program=Program(len(instructions), instructions)
libc=ctypes.CDLL(None,use_errno=True)
assert libc.prctl(38,1,0,0,0)==0
assert libc.prctl(22,2,ctypes.byref(program),0,0)==0
status=apply_landlock(json.loads(sys.argv[2]))
assert status['state']=='enforced' and status['abi']>=3
exec(sys.argv[3])
"""


@pytest.fixture
def kernel():
    if launcher._detect_abi() < 3:
        pytest.skip("real path enforcement tests require Landlock ABI >= 3")


def filtered(payload, code, cwd):
    source_root = str(Path(launcher.__file__).parents[2])
    env = {**os.environ, "HOME": str(cwd), "TMPDIR": str(cwd)}
    return subprocess.run(
        [sys.executable, "-c", CHILD, source_root, json.dumps(payload), textwrap.dedent(code)],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def rules(*, read_only=(), read_write=()):
    runtime = [
        sys.base_prefix,
        sys.prefix,
        *[p for p in sys.path if p],
        "/usr",
        "/bin",
        "/lib",
        "/lib64",
        "/proc",
        "/etc/ld.so.cache",
        "/etc/localtime",
        "/etc/hosts",
        "/etc/resolv.conf",
    ]
    # Never grant a broad root read rule in this test: denied siblings must stay denied.
    runtime = [p for p in runtime if Path(p).is_absolute() and p != "/"]
    return {
        "compatibility": "hard_requirement",
        "read_only": [*runtime, *map(str, read_only)],
        "read_write": ["/dev", *map(str, read_write)],
        "directory_only": ["/"],
    }


def test_exact_read_file_denies_adjacent_secret(kernel, tmp_path):
    allowed, secret = tmp_path / "allowed", tmp_path / "secret"
    allowed.write_text("public")
    secret.write_text("private")
    result = filtered(
        rules(read_only=[allowed]),
        f"""
        assert Path({str(allowed)!r}).read_text() == 'public'
        try: Path({str(secret)!r}).read_text()
        except PermissionError: pass
        else: raise AssertionError('sibling secret became readable')
        try: Path({str(allowed)!r}).write_text('overwrite')
        except PermissionError: pass
        else: raise AssertionError('read-only file became writable')
        print('exact-read-ok')
    """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "exact-read-ok" in result.stdout
    assert allowed.read_text() == "public"


def test_exact_write_file_cannot_modify_siblings(kernel, tmp_path):
    allowed, secret = tmp_path / "allowed", tmp_path / "secret"
    allowed.write_text("public")
    secret.write_text("private")
    result = filtered(
        rules(read_write=[allowed]),
        f"""
        Path({str(allowed)!r}).write_text('updated')
        assert Path({str(allowed)!r}).read_text() == 'updated'
        for path in [{str(secret)!r}, {str(tmp_path / "new-sibling")!r}]:
            try: Path(path).write_text('bad')
            except PermissionError: pass
            else: raise AssertionError('sibling became writable')
        print('exact-write-ok')
    """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert allowed.read_text() == "updated" and secret.read_text() == "private"


def test_directory_operations_threads_and_child_inheritance(kernel, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("private")
    child = f"""from pathlib import Path
try: Path({str(secret)!r}).read_text()
except PermissionError: print('child-denied')
else: raise AssertionError('child lost Landlock')
"""
    result = filtered(
        rules(read_write=[work]),
        f"""
        Path('first').write_text('hello')
        Path('first').rename('second')
        os.link('second', 'third')
        Path('second').write_text('x')
        Path('third').unlink()
        worker=threading.Thread(target=lambda: Path('thread-file').write_text('ok'))
        worker.start(); worker.join()
        assert Path('thread-file').read_text() == 'ok'
        subprocess.run([sys.executable, '-c', {child!r}], check=True)
        print('directory-ok')
    """,
        work,
    )
    assert result.returncode == 0, result.stderr
    assert "child-denied" in result.stdout and "directory-ok" in result.stdout


@pytest.mark.parametrize("tool", ["node", "java", "git-cc"])
def test_common_workload_with_both_filters(kernel, tmp_path, tool):
    work = tmp_path / "work"
    work.mkdir()
    extra = []
    if tool == "node":
        node = shutil.which("node")
        if not node:
            pytest.skip("Node.js is not installed")
        script = """const fs=require('fs'); const {Worker}=require('worker_threads');
            fs.writeFileSync('node-file','ok');
            new Worker('require("worker_threads").parentPort.postMessage(42)',{eval:true})
              .on('message',value=>{if(value!==42)throw Error('thread');console.log('node-ok');});"""
        commands = [[node, "-e", script]]
    elif tool == "java":
        java, javac = shutil.which("java"), shutil.which("javac")
        if not java or not javac:
            pytest.skip("Java JDK is not installed")
        extra = [str(Path(java).resolve().parents[1]), str(Path(javac).resolve().parents[1])]
        (work / "Workload.java").write_text("""import java.nio.file.*;
            public class Workload {public static void main(String[] args) throws Exception {
              Files.write(Paths.get("java-file"), "ok".getBytes());
              Thread t=new Thread(()->System.out.println("java-ok"));t.start();t.join();}}""")
        commands = [
            [javac, "-J-Djava.io.tmpdir=" + str(work), "Workload.java"],
            [java, "-Djava.io.tmpdir=" + str(work), "-cp", ".", "Workload"],
        ]
    else:
        git, cc = shutil.which("git"), shutil.which("cc")
        if not git or not cc:
            pytest.skip("Git or C compiler is not installed")
        (work / "main.c").write_text('#include <stdio.h>\nint main(void){puts("compiler-ok");}\n')
        commands = [
            [git, "init", "--quiet"],
            [git, "status", "--porcelain"],
            [cc, "main.c", "-o", "main"],
            [str(work / "main")],
        ]
    result = filtered(
        rules(read_only=extra, read_write=[work]),
        f"\nfor command in {commands!r}: subprocess.run(command, check=True)",
        work,
    )
    assert result.returncode == 0, result.stderr


def test_native_missing_optional_file_does_not_open_parent(kernel, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("private")
    result = filtered(
        rules(read_only=[tmp_path / "missing"]),
        f"""
        try: Path({str(secret)!r}).read_text()
        except PermissionError: print('missing-rule-safe')
        else: raise AssertionError('missing file widened parent access')
    """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
