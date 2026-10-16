"""Install real filters only in disposable Linux children, without a server.

These checks validate kernel loading, inheritance and common workload behavior.
They do not stand in for the deployment's bwrap/namespace/Landlock acceptance.
"""

from pathlib import Path
import platform
import shutil
import subprocess
import sys
import textwrap

import pytest

from jiuwenbox.models.policy import SyscallPolicy
from jiuwenbox.supervisor.seccomp import build_seccomp_filter


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or platform.machine().lower() not in {"x86_64", "aarch64"},
    reason="requires a native Linux x86_64/aarch64 kernel",
)

INSTALL = """
import ctypes, errno, json, os, subprocess, sys, threading
from pathlib import Path
class SockFilter(ctypes.Structure):
    _fields_ = [('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
class SockFprog(ctypes.Structure):
    _fields_ = [('len',ctypes.c_ushort),('filter',ctypes.POINTER(SockFilter))]
raw = bytes.fromhex(sys.argv[1])
instructions = (SockFilter * (len(raw)//8)).from_buffer_copy(raw)
program = SockFprog(len(instructions), instructions)
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
if libc.prctl(38, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'PR_SET_NO_NEW_PRIVS failed')
if libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'PR_SET_SECCOMP failed')
exec(sys.argv[2])
"""


def filtered(code, *, policy=None, cwd=None):
    program = build_seccomp_filter(policy or SyscallPolicy())
    return subprocess.run(
        [sys.executable, "-c", INSTALL, program.hex(), textwrap.dedent(code)],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=45,
    )


def test_real_kernel_blocks_configured_syscall_and_descendants():
    # getppid needs no privileges: unlike mount/ptrace, its EPERM proves that
    # our filter was installed rather than another capability denying a call.
    nr = 110 if platform.machine() == "x86_64" else 173
    code = f"""
    assert libc.syscall({nr}) == -1 and ctypes.get_errno() == errno.EPERM
    child = subprocess.run([sys.executable, '-c',
        'import ctypes; c=ctypes.CDLL(None,use_errno=True); '
        'assert c.syscall({nr}) == -1 and ctypes.get_errno() == 1'], check=True)
    print('inherited-filter-ok')
    """
    result = filtered(
        code,
        policy=SyscallPolicy(
            x86_64={"blocked": ["getppid"]},
            arm64={"blocked": ["getppid"]},
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "inherited-filter-ok" in result.stdout


def test_python_files_threads_subprocess_and_network(tmp_path):
    result = filtered(
        """
        import socket
        Path('payload').write_text('hello')
        assert Path('payload').read_text() == 'hello'
        values = []
        worker = threading.Thread(target=lambda: values.append(42))
        worker.start(); worker.join()
        assert values == [42]
        assert subprocess.check_output(['/bin/sh', '-c', 'printf shell-ok']) == b'shell-ok'
        # Permit socket creation/connect; this is a local echo, not internet access.
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0)); server.listen(1)
            with socket.create_connection(server.getsockname()) as client:
                peer, _ = server.accept()
                with peer:
                    client.sendall(b'ok'); assert peer.recv(2) == b'ok'
        print('python-ok')
    """,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "python-ok" in result.stdout


def test_node_threads_and_file_operations(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    javascript = """
        const fs = require('node:fs');
        const { Worker } = require('node:worker_threads');
        fs.writeFileSync('node-file', 'hello');
        if (fs.readFileSync('node-file', 'utf8') !== 'hello') throw Error('file I/O');
        const worker = new Worker('require("node:worker_threads").parentPort.postMessage(42)', { eval: true });
        worker.on('message', value => { if (value !== 42) throw Error('thread'); console.log('node-ok'); });
    """
    result = filtered(f"subprocess.run({[node, '-e', javascript]!r}, check=True)", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "node-ok" in result.stdout


def test_java_threads_and_file_operations(tmp_path):
    java, javac = shutil.which("java"), shutil.which("javac")
    if not java or not javac:
        pytest.skip("Java JDK is not installed")
    source = Path(tmp_path) / "Workload.java"
    source.write_text(
        """
        import java.nio.file.*;
        public class Workload {
            public static void main(String[] args) throws Exception {
                Path path = Paths.get("java-file");
                Files.write(path, "hello".getBytes());
                if (!new String(Files.readAllBytes(path)).equals("hello")) throw new Exception("I/O");
                Thread thread = new Thread(() -> System.out.println("java-thread-ok"));
                thread.start(); thread.join();
            }
        }
    """,
        encoding="utf-8",
    )
    # Compile and run under the same inherited filter, including JIT/threads.
    code = f"subprocess.run({[javac, str(source)]!r}, check=True)\nsubprocess.run({[java, '-cp', str(tmp_path), 'Workload']!r}, check=True)"
    result = filtered(code, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "java-thread-ok" in result.stdout


def test_git_and_native_compiler(tmp_path):
    git, cc = shutil.which("git"), shutil.which("cc")
    if not git or not cc:
        pytest.skip("Git or C compiler is not installed")
    (tmp_path / "main.c").write_text('#include <stdio.h>\nint main(void){puts("compiler-ok");}\n')
    commands = [
        [git, "init", "--quiet"],
        [git, "status", "--porcelain"],
        [cc, "main.c", "-o", "main"],
        [str(tmp_path / "main")],
    ]
    result = filtered(
        f"\nfor command in {commands!r}: subprocess.run(command, check=True)", cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert "compiler-ok" in result.stdout


def test_baseline_rejects_dangerous_calls():
    table = {"x86_64": [101, 165, 250, 321], "aarch64": [117, 40, 219, 280]}
    # Invalid/null arguments are harmless even if the filter regresses. The
    # separate getppid test proves filtering independent of privilege checks.
    code = f"""
    for nr in {table[platform.machine()]!r}:
        ctypes.set_errno(0)
        assert libc.syscall(nr, 0, 0, 0, 0, 0, 0) == -1
        assert ctypes.get_errno() == errno.EPERM
    print('baseline-ok')
    """
    result = filtered(code)
    assert result.returncode == 0, result.stderr
    assert "baseline-ok" in result.stdout
