"""Relay MCP stdio from SSH's service session to an interactive desktop.

The desktop process listens only on loopback and authenticates each connection
with a user-private token. Every connection owns an ordinary MCP subprocess;
JSON-RPC content, notifications and metadata pass through without translation.
"""
import argparse
import asyncio
import ctypes
import hmac
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
from pathlib import Path

DEFAULT_PORT = 18766
LOGGER = logging.getLogger(__name__)


class _CleanupError(RuntimeError):
    pass


def state_directory(value=None):
    return Path(value) if value else Path.home() / ".computer-use" / "desktop-bridge"


def _private_directory(folder):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        folder.chmod(0o700)
        return
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    result = subprocess.run([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"], capture_output=True, check=True)
    # User names can use the OEM code page; the SID itself is ASCII.
    identities = re.findall(rb'"(S-\d+(?:-\d+)+)"', result.stdout)
    if len(identities) != 1:
        raise RuntimeError("Cannot identify desktop bridge user.")
    sid = identities[0].decode('ascii')
    subprocess.run(
        [str(system / "icacls.exe"),
         str(folder), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True,
        check=True)


def _interactive_session():
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcessId.restype = ctypes.c_uint32
    kernel.ProcessIdToSessionId.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    kernel.ProcessIdToSessionId.restype = ctypes.c_int
    session = ctypes.c_uint32()
    if not kernel.ProcessIdToSessionId(kernel.GetCurrentProcessId(), ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    if session.value == 0:
        raise RuntimeError("Start the desktop bridge in the logged-in user's interactive session, not SSH Session 0.")
    return session.value


async def _retire(child, job):
    if child.stdin is not None:
        child.stdin.close()
    try:
        await asyncio.wait_for(child.wait(), 8)
        if job is not None:
            await asyncio.wait_for(job.wait_empty(), 5)
    except TimeoutError:
        if child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            await child.wait()
        raise _CleanupError("MCP cleanup exceeded its deadline; effects may already have occurred.") from None
    except Exception as error:
        raise _CleanupError(f"MCP cleanup could not be confirmed: {error}") from error
    finally:
        if job is not None:
            job.close()


async def _relay(reader, writer):
    job = None
    if os.name == "nt":
        from .windows_worker import WindowsJob, resume_suspended
        job = WindowsJob()
    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "computer_use.mcp", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr, limit=12 * 1024 * 1024,
            creationflags=(subprocess.CREATE_NO_WINDOW | 4) if os.name == "nt" else 0)
    except BaseException:
        if job is not None:
            job.close()
        raise
    transfers = []
    try:
        if job is not None:
            job.assign(child.pid)
            resume_suspended(child.pid)
        writer.write(b"OK\n")
        await writer.drain()

        async def upload():
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    child.stdin.write(data)
                    await child.stdin.drain()
            finally:
                child.stdin.close()

        async def download():
            while True:
                data = await child.stdout.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()

        uploads, downloads = asyncio.create_task(upload()), asyncio.create_task(download())
        transfers = [uploads, downloads]
        done, _ = await asyncio.wait(transfers, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if uploads in done and downloads not in done:
            await asyncio.wait_for(downloads, 8)
    finally:
        for task in transfers:
            task.cancel()
        if transfers:
            await asyncio.gather(*transfers, return_exceptions=True)
        await _retire(child, job)


async def serve(port=DEFAULT_PORT, folder=None):
    session = _interactive_session()
    folder = state_directory(folder)
    token = secrets.token_hex(32)
    stopping, stopped = asyncio.Event(), asyncio.Event()
    peers, controls = set(), set()
    failures = []

    async def accept(reader, writer):
        current = asyncio.current_task()
        peers.add(current)
        authenticated = False
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            if len(line) > 256:
                raise ValueError("Invalid bridge handshake.")
            request = json.loads(line)
            claim = request.get("token") if isinstance(request, dict) else None
            if not isinstance(claim, str) or not hmac.compare_digest(claim, token):
                raise ValueError("Desktop bridge authentication failed.")
            authenticated = True
            if request.get("mode") == "stop":
                controls.add(current)
                stopping.set()
                await stopped.wait()
                writer.write(b"ERROR: cleanup incomplete\n" if failures else b"OK\n")
                await writer.drain()
            elif request.get("mode") == "connect" and not stopping.is_set():
                await _relay(reader, writer)
            else:
                raise ValueError("Desktop bridge is stopping or the request is invalid.")
        except asyncio.CancelledError:
            pass
        except Exception as error:
            if authenticated and isinstance(error, _CleanupError):
                failures.append(str(error))
            LOGGER.error("%s", error)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            peers.discard(current)
            controls.discard(current)

    # Bind before publishing a token: a duplicate start cannot invalidate the
    # token of a service that already owns this port.
    server = await asyncio.start_server(accept, "127.0.0.1", port, start_serving=False)
    token_file = folder / "token"
    try:
        _private_directory(folder)
        temporary = folder / (".token-" + secrets.token_hex(8))
        try:
            temporary.write_text(token, encoding="ascii")
            if os.name != "nt":
                temporary.chmod(0o600)
            temporary.replace(token_file)
        finally:
            temporary.unlink(missing_ok=True)
        await server.start_serving()
        LOGGER.info("Desktop bridge ready: 127.0.0.1:%s; session=%s; pid=%s", port, session, os.getpid())
        await stopping.wait()
    finally:
        server.close()
        pending = peers - controls
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            if token_file.exists() and token_file.read_text(encoding="ascii") == token:
                token_file.unlink()
        finally:
            stopped.set()
            if controls:
                await asyncio.gather(*tuple(controls), return_exceptions=True)
            await server.wait_closed()


def _connect(port, folder, mode):
    token_file = state_directory(folder) / "token"
    try:
        token = token_file.read_text(encoding="ascii")
    except FileNotFoundError:
        raise RuntimeError("Desktop bridge is not running; start the interactive desktop task.") from None
    connection = socket.create_connection(("127.0.0.1", port), timeout=20 if mode == "stop" else 10)
    connection.sendall((json.dumps(dict(token=token, mode=mode)) + "\n").encode("ascii"))
    # Consume only the acknowledgement. Unbuffered byte reads cannot swallow
    # the beginning of the first MCP message.
    acknowledgement = bytearray()
    while len(acknowledgement) < 512:
        value = connection.recv(1)
        if not value:
            break
        acknowledgement.extend(value)
        if value == b"\n":
            break
    if acknowledgement != b"OK\n":
        connection.close()
        raise RuntimeError("Desktop bridge rejected the connection or did not complete cleanup.")
    connection.settimeout(None)
    return connection


def connect(port=DEFAULT_PORT, folder=None):
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    connection = _connect(port, folder, "connect")

    def upload():
        try:
            while True:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    break
                connection.sendall(data)
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            try:
                connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    thread = threading.Thread(target=upload, daemon=True)
    thread.start()
    try:
        while True:
            data = connection.recv(65536)
            if not data:
                break
            remaining = memoryview(data)
            while remaining:
                remaining = remaining[os.write(sys.stdout.fileno(), remaining):]
    finally:
        connection.close()


def cli():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "connect", "stop"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--state-dir")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.mode == "serve":
        asyncio.run(serve(args.port, args.state_dir))
    elif args.mode == "connect":
        connect(args.port, args.state_dir)
    else:
        connection = _connect(args.port, args.state_dir, "stop")
        connection.close()


if __name__ == "__main__":
    cli()
