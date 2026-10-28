"""Windows worker IPC and lifetime supervision, without inherited POSIX FDs.

The worker joins a kill-on-close Job before any user code is sent. A reset
first stops the worker and allows its native children to clean up on stdin EOF;
the job's active-process count supplies the same retirement barrier as the
inherited lifetime pipe on Unix. The loopback channel is private and one-use.
"""
import asyncio
import ctypes
import hmac
import os
import secrets
import subprocess
import sys


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("processTime", ctypes.c_int64),
        ("jobTime", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("minimumWorkingSet", ctypes.c_size_t),
        ("maximumWorkingSet", ctypes.c_size_t),
        ("activeProcessLimit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priorityClass", ctypes.c_uint32),
        ("schedulingClass", ctypes.c_uint32),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [
        ("readOperations", ctypes.c_uint64), ("writeOperations", ctypes.c_uint64),
        ("otherOperations", ctypes.c_uint64), ("readBytes", ctypes.c_uint64),
        ("writeBytes", ctypes.c_uint64), ("otherBytes", ctypes.c_uint64),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IOCounters)] + [
        (name, ctypes.c_size_t) for name in ("processMemory", "jobMemory", "peakProcessMemory", "peakJobMemory")
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in ("userTime", "kernelTime", "periodUserTime", "periodKernelTime")] + [
        (name, ctypes.c_uint32) for name in ("pageFaults", "totalProcesses", "activeProcesses", "terminatedProcesses")
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32)
                for name in ('size', 'usage', 'threadId', 'processId')] + [('basePriority', ctypes.c_int32),
                                                                           ('deltaPriority', ctypes.c_int32),
                                                                           ('flags', ctypes.c_uint32)]


def resume_suspended(pid):
    """Resume our newly created primary thread only after Job assignment.

    asyncio/Popen closes the CreateProcess thread handle. The documented
    Toolhelp thread snapshot recovers it while the child is still suspended.
    """
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    handle, uint, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    declarations = {
        'CreateToolhelp32Snapshot': ([uint, uint], handle),
        'Thread32First': ([handle, ctypes.POINTER(_ThreadEntry)], boolean),
        'Thread32Next': ([handle, ctypes.POINTER(_ThreadEntry)], boolean),
        'OpenThread': ([uint, boolean, uint], handle),
        'ResumeThread': ([handle], uint),
        'CloseHandle': ([handle], boolean),
    }
    for name, (arguments, result) in declarations.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    threads = []
    try:
        entry = _ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        more = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.processId == pid:
                threads.append(entry.threadId)
            entry.size = ctypes.sizeof(entry)
            more = kernel.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    if len(threads) != 1:
        raise RuntimeError('Cannot identify the suspended child primary thread.')
    thread = kernel.OpenThread(2, False, threads[0])  # THREAD_SUSPEND_RESUME
    if not thread:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        previous = kernel.ResumeThread(thread)
        if previous == 0xffffffff:
            raise ctypes.WinError(ctypes.get_last_error())
        if previous != 1:
            raise RuntimeError('Child was not suspended exactly once.')
    finally:
        kernel.CloseHandle(thread)


class WindowsJob:

    def __init__(self):
        self.handle = None
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, uint, boolean, pointer = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p
        declarations = {
            "CreateJobObjectW": ([pointer, ctypes.c_wchar_p], handle),
            "SetInformationJobObject": ([handle, ctypes.c_int, pointer, uint], boolean),
            "QueryInformationJobObject": ([handle, ctypes.c_int, pointer, uint, pointer], boolean),
            "AssignProcessToJobObject": ([handle, handle], boolean),
            "IsProcessInJob": ([handle, handle, ctypes.POINTER(boolean)], boolean),
            "OpenProcess": ([uint, boolean, uint], handle),
            "CloseHandle": ([handle], boolean),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, pid):
        process = self.kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(process)

    def contains(self, pid):
        process = self.kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            included = ctypes.c_int()
            if not self.kernel.IsProcessInJob(process, self.handle, ctypes.byref(included)):
                raise ctypes.WinError(ctypes.get_last_error())
            return bool(included.value)
        finally:
            self.kernel.CloseHandle(process)

    async def wait_empty(self):
        while True:
            state = _Accounting()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(state), ctypes.sizeof(state),
                                                         None):
                raise ctypes.WinError(ctypes.get_last_error())
            if state.activeProcesses == 0:
                return
            await asyncio.sleep(.025)

    def close(self):
        handle, self.handle = self.handle, None
        if handle:
            self.kernel.CloseHandle(handle)


async def start_windows_worker(env):
    loop = asyncio.get_running_loop()
    connected = loop.create_future()
    token = secrets.token_hex(32).encode("ascii")
    candidates = set()

    async def accept(reader, writer):
        current = asyncio.current_task()
        candidates.add(current)
        accepted = False
        authenticated = False
        try:
            claimed = await asyncio.wait_for(reader.readline(), 5)
            credential, separator, pid_text = claimed.rstrip(b"\n").partition(b"\t")
            valid_worker_claim = (len(claimed) <= 80 and separator and pid_text.isdigit()
                                  and hmac.compare_digest(credential, token) and not connected.done())
            if valid_worker_claim:
                authenticated = True
                pid = int(pid_text)
                if not 0 < pid <= 0xffffffff:
                    raise ValueError("Invalid worker process identity.")
                if not job.contains(pid):
                    raise RuntimeError(('Worker interpreter escaped its lifetime Job. Use an ordinary CPython '
                                        'installation (uv --managed-python), not a Microsoft Store execution '
                                        'alias.'))
                connected.set_result((reader, writer))
                accepted = True
        except (asyncio.IncompleteReadError, ValueError, OSError, RuntimeError) as error:
            if authenticated and not connected.done():
                connected.set_exception(error)
        finally:
            if not accepted:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
            candidates.discard(current)

    server = await asyncio.start_server(accept, "127.0.0.1", 0, limit=12 * 1024 * 1024)
    job = child = None
    try:
        job = WindowsJob()
        env = dict(env, COMPUTER_USE_WORKER_PORT=str(server.sockets[0].getsockname()[1]),
                   COMPUTER_USE_WORKER_TOKEN=token.decode("ascii"))
        env.pop("COMPUTER_USE_LIFETIME_FD", None)
        env.pop("COMPUTER_USE_PREVIEW_FD", None)
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "computer_use.mcp.runtime", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env,
            creationflags=(subprocess.CREATE_NO_WINDOW | 4) if os.name == "nt" else 0)
        job.assign(child.pid)
        if os.name == "nt":
            resume_suspended(child.pid)
        reader, writer = await asyncio.wait_for(connected, 15)
        return child, reader, writer, job, server
    except BaseException:
        if job is not None:
            job.close()
        if child is not None and child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            await child.wait()
        if connected.done() and not connected.cancelled() and connected.exception() is None:
            _, writer = connected.result()
            writer.close()
        raise
    finally:
        server.close()
        for task in tuple(candidates):
            task.cancel()
        if candidates:
            await asyncio.gather(*tuple(candidates), return_exceptions=True)
        # wait_closed includes accepted streams on newer Python versions.
        # The session owns that barrier until the worker channel has closed.
        if child is None or child.returncode is not None:
            await server.wait_closed()
