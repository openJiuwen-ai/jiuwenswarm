# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows 两跳启动 (box-server -> runner -> 沙箱子进程).

第一跳 box-server 用 CreateProcessWithLogonW 以 jbx-sandbox 身份拉起 runner;
第二跳 runner 在 jbx-sandbox 上下文用 CreateProcessAsUserW 起沙箱子进程.
两跳是为绕开 SeAssignPrimaryTokenPrivilege 权限墙. box-server 与 runner
经 TCP loopback 控制端口通信, 帧协议复用 daemon_ipc.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading as _threading
from ctypes import wintypes
from typing import Any

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.supervisor import win_constants as const
from jiuwenbox.supervisor.daemon_ipc import (
    LOG_FIELD_LEVEL,
    LOG_FIELD_MESSAGE,
    LOG_FIELD_TIMESTAMP,
    LOG_FIELD_TRACEBACK,
    LOG_FRAME_TYPE,
    MAX_FILE_BYTES,
    MAX_HEADER_BYTES,
    MAX_STDIN_BYTES,
    MAX_STDOUT_BYTES,
    REQUEST_TYPE_SUBSCRIBE_LOG,
    recv_frame,
    send_frame,
)

configure_logging()
logger = logging.getLogger(__name__)

# runner 脚本路径 (本模块的 runner 入口函数以 `python -m` 形式启动).
RUNNER_MODULE = "jiuwenbox.supervisor.win_exec"
RUNNER_SUBCOMMAND = "runner"

# runner shutdown 时等待在途 worker 完成的窗口. 对齐 Linux daemon 的
# SHUTDOWN_DRAIN_TIMEOUT_SECONDS 但缩短: box-server 侧 _send_runner_shutdown
# 走 DAEMON_SHUTDOWN_TIMEOUT_SECONDS=3s 等响应, runner 这边 5s 给 exec child
# 一个优雅退出的机会, 超时则 daemon 线程被强收 (child 由 ephemeral Job 兜杀).
WIN_RUNNER_SHUTDOWN_DRAIN_SECONDS = 5.0

# box-server 分配空闲端口经命令行传 runner, runner bind 做 server,
# box-server 每次 exec connect. 对齐 Linux AF_UNIX (Windows 不能传 fd).
LISTENER_PORT_ENV = "JIUWENBOX_CONTROL_LISTENER_PORT"

# 日志订阅长连集合: box-server connect 后 runner 保持连接, 任意阶段往里 push log.
# runner 在 CREATE_NO_WINDOW 下 stderr 无落盘, 早期异常靠这条长连发回 box-server.

_log_subscribers: list = []  # list[socket.socket]
_log_sub_lock = _threading.Lock()

# runner 启动时记下代理端口范围, 供 _create_process_as_user 注入 HTTP(S)_PROXY env.
# box-server 起 win_proxy 监听这些端口, 沙箱出网经 WFP 只能到这些端口.
_proxy_port_start: int = const.DEFAULT_PROXY_PORT_RANGE_START
_proxy_port_end: int = const.DEFAULT_PROXY_PORT_RANGE_END

# 本地落盘日志: runner 过程日志写 jbx-sandbox profile 下, 不依赖长连回传.
# 路径: C:\Users\jbx-sandbox\jiuwenbox-logs\<sandbox_id>\runner.log
_local_log_path: str | None = None
_local_log_file = None  # file object
_local_log_lock = _threading.Lock()


def _init_local_log(sandbox_id: str) -> None:
    """初始化本地落盘日志文件 (runner 启动时调一次). 失败降级为只回传."""
    global _local_log_path, _local_log_file
    import os as _os  # noqa: A004 - 别名访问, 避免与函数内局部变量 os 重名
    try:
        # runner env 可能缺 USERPROFILE, 多路兜底拿 profile 根: env / API / 标准路径.
        home = _os.environ.get("USERPROFILE") or ""
        if not home or not _os.path.isdir(home):
            try:
                api_home = get_sandbox_profile_dir()
                if api_home and _os.path.isdir(api_home):
                    home = api_home
            except Exception:  # noqa: BLE001
                pass
        if not home or not _os.path.isdir(home):
            _sd = _os.environ.get("SystemDrive", r"C:")
            home = _os.path.join(_sd, "Users", "jbx-sandbox")
        if not _os.path.isdir(home):
            return
        log_dir = _os.path.join(home, "jiuwenbox-logs", sandbox_id)
        _os.makedirs(log_dir, exist_ok=True)
        path = _os.path.join(log_dir, "runner.log")
        _local_log_path = path
        _local_log_file = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        _local_log_path = None
        _local_log_file = None


def _local_log(level: str, msg: str, exc: str | None = None) -> None:
    """写一行本地日志 (带时间戳, 线程安全). best-effort, 不抛."""
    import time as _time
    if _local_log_file is None:
        return
    try:
        ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime())
        line = f"{ts} [{level}] {msg}"
        if exc:
            line += f"\n{exc}"
        with _local_log_lock:
            _local_log_file.write(line + "\n")
            _local_log_file.flush()
    except (OSError, ValueError):
        pass


def _push_log(level: str, msg: str, exc: str | None = None) -> None:
    """往所有日志订阅连接 push 一个 log 帧 (best-effort, 不抛). 同时落盘+本地 logger."""
    import json as _json  # noqa: A004
    import time as _time
    payload = {
        "v": 1,
        "type": LOG_FRAME_TYPE,
        LOG_FIELD_LEVEL: level,
        LOG_FIELD_MESSAGE: msg,
        LOG_FIELD_TIMESTAMP: _time.time(),
    }
    if exc:
        payload[LOG_FIELD_TRACEBACK] = exc
    blob = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    dead: list = []
    # send_frame 在 _log_sub_lock 内串行: 并发化后多个 worker 可能同时 _push_log,
    # 若不加锁, 两个 send_frame 的长度前缀+payload 会在同一 subscribe sock 上
    # 交错, box-server 侧 recv_frame 解析错乱. push 是低频日志操作, 串行可接受.
    with _log_sub_lock:
        subs = list(_log_subscribers)
        for sock in subs:
            try:
                send_frame(sock, blob)
            except (OSError, ValueError):  # ConnectionError 是 OSError 子类, 不重复捕获
                dead.append(sock)
        for sock in dead:
            if sock in _log_subscribers:
                _log_subscribers.remove(sock)
    # 本地也记一份 (与 push 同时, 方便有 console 的场景).
    try:
        if level == "ERROR":
            logger.error("[runner] %s", msg)
        elif level == "WARNING":
            logger.warning("[runner] %s", msg)
        else:
            logger.info("[runner] %s", msg)
    except Exception:  # noqa: BLE001
        pass
    # 落盘到本地日志文件 (不依赖回传链, control_port 断/卡死时仍可查过程).
    _local_log(level, msg, exc)


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError(
            f"win_exec 仅在 Windows 平台可用; 当前平台 {sys.platform!r}"
        )


# ---------------------------------------------------------------------------
# advapi32 / kernel32 函数签名.
# ---------------------------------------------------------------------------
_advapi32: ctypes.WinDLL | None = None
_kernel32: ctypes.WinDLL | None = None


class STARTUPINFOEX(ctypes.Structure):
    pass  # 前向声明


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SecurityAttr(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class SidAndAttr(ctypes.Structure):
    """SID_AND_ATTRIBUTES (Win32): {PVOID Sid; DWORD Attributes}."""
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TokenGroup(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    """TOKEN_GROUPS 变长结构, Groups 用 SidAndAttr 数组保证 64 位对齐."""
    _fields_ = [
        ("GroupCount", wintypes.DWORD),
        ("Groups", SidAndAttr * 1),  # 变长, 实际按 count 重新 cast
    ]


def _get_advapi32() -> ctypes.WinDLL:
    global _advapi32
    if _advapi32 is None:
        _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _advapi32.CreateProcessWithLogonW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
            wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW),
            ctypes.POINTER(ProcessInfo),
        ]
        _advapi32.CreateProcessWithLogonW.restype = wintypes.BOOL
        _advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        ]
        _advapi32.OpenProcessToken.restype = wintypes.BOOL
        _advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        _advapi32.GetTokenInformation.restype = wintypes.BOOL
        _advapi32.CreateRestrictedToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD,
            wintypes.DWORD, ctypes.c_void_p,  # disabling sids
            wintypes.DWORD, ctypes.c_void_p,  # deleting privileges
            wintypes.DWORD, ctypes.POINTER(SidAndAttr),  # restricting sids
            ctypes.POINTER(wintypes.HANDLE),
        ]
        _advapi32.CreateRestrictedToken.restype = wintypes.BOOL
        _advapi32.CreateProcessAsUserW.argtypes = [
            wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR,
            ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(STARTUPINFOW),
            ctypes.POINTER(ProcessInfo),
        ]
        _advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
        _advapi32.LookupAccountNameW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR,
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        _advapi32.LookupAccountNameW.restype = wintypes.BOOL
        _advapi32.AllocateAndInitializeSid.argtypes = [
            ctypes.c_void_p, wintypes.BYTE,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        _advapi32.AllocateAndInitializeSid.restype = wintypes.BOOL
        _advapi32.CreateWellKnownSid.argtypes = [
            wintypes.DWORD, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
        ]
        _advapi32.CreateWellKnownSid.restype = wintypes.BOOL
    return _advapi32


def get_kernel32() -> ctypes.WinDLL:
    global _kernel32
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32.CloseHandle.restype = wintypes.BOOL
        _kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(SecurityAttr),
            wintypes.DWORD,
        ]
        _kernel32.CreatePipe.restype = wintypes.BOOL
        _kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
        ]
        _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        _kernel32.WaitForSingleObject.argtypes = [
            wintypes.HANDLE, wintypes.DWORD,
        ]
        _kernel32.WaitForSingleObject.restype = wintypes.DWORD
        _kernel32.TerminateProcess.argtypes = [
            wintypes.HANDLE, wintypes.UINT,
        ]
        _kernel32.TerminateProcess.restype = wintypes.BOOL
        # SetHandleInformation: 关闭 box-server 持有端的继承, 防 runner/child
        # 拿到多余 pipe 句柄 (对标 Linux close_fds=True 隔离).
        _kernel32.SetHandleInformation.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        ]
        _kernel32.SetHandleInformation.restype = wintypes.BOOL
        # WriteFile: review #2 — 把 control_token 写进匿名 pipe (传 runner stdin).
        _kernel32.WriteFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
        ]
        _kernel32.WriteFile.restype = wintypes.BOOL
        # GetStdHandle: review #2 — runner 读 stdin pipe 取 control_token.
        _kernel32.GetStdHandle.argtypes = [wintypes.HANDLE]
        _kernel32.GetStdHandle.restype = wintypes.HANDLE
    return _kernel32


# HANDLE_FLAG_INHERIT = 0x1, HANDLE_FLAG_PROTECT_FROM_CLOSE = 0x2.
HANDLE_FLAG_INHERIT = 0x1


def _clear_inherit(handle: int) -> None:
    """关闭 handle 的继承位 (box-server 持有端, 不让 runner 拿到副本)."""
    kernel32 = get_kernel32()
    kernel32.SetHandleInformation(
        wintypes.HANDLE(handle), HANDLE_FLAG_INHERIT, 0,
    )


# ---------------------------------------------------------------------------
# broker 侧: 第一跳.
# ---------------------------------------------------------------------------
def _build_env_block(env: dict[str, str]) -> ctypes.c_wchar_p:
    """构造 CreateProcessWithLogonW 的 lpEnvironment (UTF-16 环境块).

    格式: 一串 "KEY=VALUE\\0" 序列, 末尾额外一个 "\\0" (双 null 终止).
    CreateProcessWithLogonW 要求 UNICODE (UTF-16) 环境块.
    """
    parts = [f"{k}={v}" for k, v in env.items()]
    block = "\0".join(parts) + "\0\0"
    return ctypes.create_unicode_buffer(block)


def _hmac_compare(a: str, b: str) -> bool:
    """恒定时间字符串比较 (防时序侧信道泄露 token)."""
    import hmac
    return hmac.compare_digest(a, b)


def _build_runner_command(
    sandbox_id: str,
    workspace: str,
    proxy_port_start: int,
    proxy_port_end: int,
    control_port: int,
    *,
    control_token: str | None = None,  # noqa: ARG001 - 兼容旧签名, token 改走 pipe 不再进命令行
) -> str:
    """构造 runner 命令行 (CreateProcessWithLogonW 的 lpCommandLine).

    用 ``python -m jiuwenbox.supervisor.win_exec runner --sandbox-id ...``.
    control_port 经命令行参数传 (而非 env), 避开 CreateProcessWithLogonW
    传 env 块时 WinError 87 (ctypes 传 c_wchar_Array 给 c_void_p 参数报错).

    review #2: 鉴权 control_token 不再拼进命令行 (旧版 ``--control-token <token>``
    会被本机任意进程经 WMI Win32_Process.CommandLine / NtQueryInformationProcess
    读 PEB 命令行泄露, 越权 exec). 改走匿名 pipe + STARTUPINFO.hStdInput:
    box-server CreatePipe, 把读端经 hStdInput 传 runner, runner 启动后从 stdin
    读 [4 字节长度][token 字节], 关 stdin 再切 socket accept. token 只存在于
    进程内存, 不暴露给其他进程的 PEB/WMI.

    runner python 用标准 CPython (非 uv trampoline/venv launcher):
    jbx-sandbox 对 uv 缓存/AppData 无读权限, 任何 uv 体系 venv python (`.venv`
    / isolation_venv / uv 全局) 第一跳 CreateProcessWithLogonW 都报 WinError 5
    或 trampoline spawn child permission denied. 必须用自包含的标准 CPython
    (jbx-sandbox grant RX 到安装目录即可跑).

    优先级: ``JIUWENBOX_RUNNER_PYTHON`` env (显式指定) > 默认系统 python 路径.
    dev 实测设 ``JIUWENBOX_RUNNER_PYTHON`` 指向系统 CPython 安装;
    打包环境设 tools/python/python.exe. 系统 python 需先装 jiuwenbox_dev.pth
    指向源码 (否则 ``-m jiuwenbox...`` 找不到) + pip install uvicorn
    (logging_config 触发).
    """
    py = (os.environ.get("JIUWENBOX_RUNNER_PYTHON") or "").strip()
    if not py or not os.path.isfile(py):
        py = sys.executable or "python"
    parts = [
        py,
        "-m", RUNNER_MODULE,
        RUNNER_SUBCOMMAND,
        "--sandbox-id", sandbox_id,
        "--workspace", workspace,
        "--proxy-port-start", str(proxy_port_start),
        "--proxy-port-end", str(proxy_port_end),
        "--control-port", str(control_port),
    ]
    # review #2: --control-token 不再拼进命令行 (token 走 pipe). runner 端
    # argparse 仍保留 --control-token 默认空串, 兼容旧 CLI 调用.
    # P2-18: 用 subprocess.list2cmdline 正确转义命令行 (修复旧版只加外层引号不
    # 转义内部双引号的 bug). runner 参数一般不含双引号, 但防御性统一处理.
    import subprocess as _sp
    return _sp.list2cmdline(parts)


def two_hop_spawn(
    sandbox_id: str,
    *,
    sandbox_user: str,
    sandbox_password: str,
    workspace: str,
    proxy_port_start: int,
    proxy_port_end: int,
    control_port: int,
    env: dict[str, str] | None = None,
    control_token: str | None = None,
) -> "tuple[int, int, int, int]":
    """第一跳: 以 jbx-sandbox 身份启动 runner (CREATE_SUSPENDED).

    调用方在 assign Job 后 resume thread_handle. 返回
    (pid, process_handle, thread_handle, token_write_handle):
    thread_handle 供 resume 后 CloseHandle; token_write_handle 是 box-server
    持有的 pipe 写端 (runner 通过继承的读端 hStdInput 读 token), 调用方需在
    resume thread 前 write_control_token 写入 token 并 CloseHandle 此写端
    (或直接用 two_hop_spawn_and_authorize 一步完成).

    review #2: control_token 不再经命令行传 (WMI/PEB 泄露), 改走匿名 pipe +
    STARTUPINFO.hStdInput: 本函数建 pipe (读端继承给 runner, 写端 box-server
    持有不继承), 把读端经 hStdInput 传 runner. resume 前 buffer 里写好 token,
    runner resume 后从 stdin 读 token 再切 socket accept.
    """
    _require_windows()
    advapi32 = _get_advapi32()
    kernel32 = get_kernel32()

    cmd = _build_runner_command(
        sandbox_id, workspace, proxy_port_start, proxy_port_end, control_port,
        control_token=control_token,
    )

    # review #2: 建匿名 pipe 传 control_token (读端继承给 runner, 写端 box-server 持有).
    token_write_handle = 0
    token_read_handle = wintypes.HANDLE()
    token_write_h = wintypes.HANDLE()
    if control_token:
        sa = SecurityAttr()
        sa.nLength = ctypes.sizeof(SecurityAttr)  # noqa: N815 - Win32 SDK 字段名
        sa.bInheritHandle = True  # noqa: N815 - 读端继承给 runner
        if not kernel32.CreatePipe(
            ctypes.byref(token_read_handle), ctypes.byref(token_write_h),
            ctypes.byref(sa), 0,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # 写端关闭继承 (box-server 持有, 不让 runner 拿到副本 → 否则 runner
        # 读 stdin 后不 EOF, 无法切 socket accept).
        _clear_inherit(int(token_write_h.value))
        token_write_handle = int(token_write_h.value)

    # control_port 走命令行参数 (避 env 块 WinError 87). token 走 h_std_input pipe.
    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(STARTUPINFOW)  # noqa: N815
    startf_usestdhanle = 0x00000100  # noqa: N806 - Win32 SDK 常量风格
    if control_token:
        startup.dwFlags = startf_usestdhanle  # noqa: N815 - 传 hStdInput
        startup.hStdInput = wintypes.HANDLE(token_read_handle.value)  # noqa: N815
    else:
        startup.dwFlags = 0  # noqa: N815
    pi = ProcessInfo()

    # dwLogonFlags 不能为 0 (WinError 87); LOGON_NETCREDENTIALS_ONLY 会用调用方
    # 凭据跑破坏隔离, 故只能 logon_with_profile (加载 profile 是其代价).
    logon_with_profile = 0x00000001  # noqa: N806 - Win32 SDK 常量风格
    # CREATE_SUSPENDED: 调用方 assign Job 后再 ResumeThread. CREATE_UNICODE_ENVIRONMENT: 传 env 块必须带.
    # bInheritHandles 必须为 True: pipe 读端需继承到 runner 子进程 (CreateProcessWithLogonW
    # 的第 9 参是 lpEnvironment, 第 8 参是 dwCreationFlags, 句柄继承由 STARTUPINFO + USESTDHANDLE 驱动,
    # 但 CreateProcessWithLogonW 不像 CreateProcess 有显式 bInheritHandles 形参 — 它默认继承可继承句柄,
    # 故 SECURITY_ATTRIBUTES.bInheritHandle=True 的 pipe 读端会被继承).
    creation_flags = const.CREATE_NO_WINDOW | const.CREATE_SUSPENDED
    # env 块含 box-server 拼好的 PATH (tool_paths), runner 必须继承, 否则起 child 时 WinError 2.
    # env_block_buf 须存活到调用返回 (悬垂指针防护).
    env_block_buf = None
    env_block_ptr = None
    if env:
        env_block_buf = _build_env_block(env)
        env_block_ptr = ctypes.cast(env_block_buf, ctypes.c_void_p)
        creation_flags |= const.CREATE_UNICODE_ENVIRONMENT

    # lpCommandLine 会被原地修改, 需可变 buffer.
    cmd_buf = ctypes.create_unicode_buffer(cmd)
    ok = advapi32.CreateProcessWithLogonW(
        sandbox_user, None, sandbox_password,
        logon_with_profile,
        None, cmd_buf,
        creation_flags,
        env_block_ptr,  # env 块; NULL 则用调用方环境
        None, ctypes.byref(startup), ctypes.byref(pi),
    )
    if not ok:
        err = ctypes.WinError(ctypes.get_last_error())
        # 失败时关 pipe 句柄防泄漏.
        if token_write_handle:
            kernel32.CloseHandle(wintypes.HANDLE(token_write_handle))
        if token_read_handle.value:
            kernel32.CloseHandle(token_read_handle)
        raise RuntimeError(
            f"两跳第一跳 CreateProcessWithLogonW 失败 (sandbox_id={sandbox_id}): {err}"
        )

    # CreateProcessWithLogonW 后 runner 已继承 pipe 读端, box-server 持有的读端副本可关.
    if token_read_handle.value:
        kernel32.CloseHandle(token_read_handle)

    # pi.hThread 不在此关闭: 调用方 assign Job 后 resume 主线程, 再 CloseHandle.
    logger.info(
        "两跳第一跳成功 (suspended): sandbox_id=%s runner_pid=%d control_port=%d token_via=pipe",
        sandbox_id, pi.dwProcessId, control_port,
    )
    return (
        int(pi.dwProcessId),
        int(pi.hProcess),
        int(pi.hThread),
        token_write_handle,
    )


def stop_runner(pid: int, process_handle: int, timeout_ms: int = 5000) -> None:
    """停止 runner: TerminateProcess 兜底."""
    _require_windows()
    kernel32 = get_kernel32()
    try:
        kernel32.TerminateProcess(wintypes.HANDLE(process_handle), 1)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(process_handle))


def is_runner_alive(process_handle: int) -> bool:
    """返回 runner 进程是否仍在运行 (未退出).

    用 WaitForSingleObject(handle, 0) 探测: 返回 WAIT_TIMEOUT(0x102) 表示
    仍在运行, 返回 WAIT_OBJECT_0(0x0) 表示已退出. 用于 _create_windows 后
    等待 runner 进入 listen() 期间做存活兜底.任何调用异常一律视作"未知/不可判定",
    返回 False 让调用方走清理路径.
    """
    _require_windows()
    try:
        kernel32 = get_kernel32()
        result = kernel32.WaitForSingleObject(
            wintypes.HANDLE(process_handle), 0,
        )
        return int(result) == 0x102  # WAIT_TIMEOUT
    except Exception:  # noqa: BLE001
        return False


def probe_runner_listen(control_port: int, timeout: float = 0.5) -> bool:
    """探测 runner 控制端口是否已进入 listen() 状态.

    runner 在 resume 后才 bind/listen 控制端口 因此 _create_windows 须
    主动 TCP connect 确认内核已可接受连接.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", control_port))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _write_token_to_pipe(write_handle: int, token: str) -> None:
    """把 control_token 写入匿名 pipe 写端, 协议 [4 字节大端长度][token 字节].

    review #2: token 经 pipe 传 (避命令行/env 泄露). 写完不在此 CloseHandle —
    由调用方在 resume thread 前关写端, runner 读到 EOF 后切 socket accept.
    CREATE_SUSPENDED 状态下 runner 还没跑, pipe buffer 缓存 token, resume 后
    runner 从 stdin 读到 [len][token] 再 EOF.
    """
    _require_windows()
    kernel32 = get_kernel32()
    data = token.encode("utf-8")
    prefix = len(data).to_bytes(4, "big")  # 4 字节大端长度
    written = wintypes.DWORD(0)
    buf = (ctypes.c_ubyte * len(prefix))(*prefix)
    ok = kernel32.WriteFile(
        wintypes.HANDLE(write_handle), ctypes.cast(buf, ctypes.c_void_p),
        ctypes.sizeof(buf), ctypes.byref(written), None,
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    if data:
        data_buf = (ctypes.c_ubyte * len(data))(*data)
        ok = kernel32.WriteFile(
            wintypes.HANDLE(write_handle), ctypes.cast(data_buf, ctypes.c_void_p),
            ctypes.sizeof(data_buf), ctypes.byref(written), None,
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())


def two_hop_spawn_and_authorize(
    sandbox_id: str,
    *,
    sandbox_user: str,
    sandbox_password: str,
    workspace: str,
    proxy_port_start: int,
    proxy_port_end: int,
    control_port: int,
    env: dict[str, str] | None = None,
    control_token: str | None = None,
) -> "tuple[int, int]":
    """两跳拉起 runner + 写 token + resume 主线程 (一站式, review #2).

    封装 two_hop_spawn (CREATE_SUSPENDED) → _write_token_to_pipe → CloseHandle
    写端 → resume_process → CloseHandle thread_handle 全流程, 调用方只拿
    (pid, process_handle). 避免 process.py 直接管理 pipe 句柄时序.

    返回 (runner_pid, process_handle); thread_handle / token_write_handle
    在内部 resume + CloseHandle 后已释放.
    """
    _require_windows()
    from jiuwenbox.supervisor import win_job
    runner_pid, proc_handle, thread_handle, token_write_handle = two_hop_spawn(
        sandbox_id,
        sandbox_user=sandbox_user,
        sandbox_password=sandbox_password,
        workspace=workspace,
        proxy_port_start=proxy_port_start,
        proxy_port_end=proxy_port_end,
        control_port=control_port,
        env=env,
        control_token=control_token,
    )
    kernel32 = get_kernel32()
    try:
        # 1. resume 前 (CREATE_SUSPENDED) 把 token 写进 pipe, 关写端让 runner 读到 EOF.
        # review #2 安全关键: token 写入失败必须终止 spawn, 否则 runner 以空 token
        # 跑起来后 accept 循环的 control_token 为空会跳过鉴权 (runner_main 里
        # `if control_token:` 为空时直接放行), 本机任意进程可越权 exec.
        token_write_ok = True
        if control_token and token_write_handle:
            try:
                _write_token_to_pipe(token_write_handle, control_token)
            except Exception as exc:  # noqa: BLE001
                token_write_ok = False
                logger.error(
                    "写 control_token 到 pipe 失败, 终止 runner spawn (sandbox=%s): %s",
                    sandbox_id, exc,
                )
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(token_write_handle))
        if not token_write_ok and control_token:
            # token 没传过去, runner 起来也是无鉴权状态 — 杀掉已起的 runner (仍挂起),
            # 关 process/thread handle, 抛错让调用方走清理路径.
            kernel32.TerminateProcess(wintypes.HANDLE(proc_handle), 1)
            kernel32.CloseHandle(wintypes.HANDLE(proc_handle))
            try:
                kernel32.CloseHandle(wintypes.HANDLE(thread_handle))
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"control_token 写入 pipe 失败, runner 未启动 (sandbox_id={sandbox_id})"
            )
        # 2. resume runner 主线程 (CREATE_SUSPENDED 必须被唤醒).
        try:
            win_job.resume_process(thread_handle)
        except Exception:  # noqa: BLE001
            logger.warning(
                "Windows runner resume 失败 (sandbox=%s)", sandbox_id,
                exc_info=True,
            )
        # 3. thread_handle resume 后不再需要, 关闭避免句柄泄漏.
        try:
            kernel32.CloseHandle(wintypes.HANDLE(thread_handle))
        except Exception:  # noqa: BLE001
            logger.debug("CloseHandle thread_handle 失败", exc_info=True)
    except RuntimeError:
        raise
    except Exception:  # noqa: BLE001
        # 未预期异常: 关 process_handle 防泄漏, runner 已起但 resume 异常时
        # 会被 box-server 当作起不来清理 (调用方负责 stop_runner).
        try:
            kernel32.CloseHandle(wintypes.HANDLE(proc_handle))
        except Exception:  # noqa: BLE001
            pass
        raise
    return runner_pid, proc_handle


# ---------------------------------------------------------------------------
# runner 侧: 第二跳 (运行在 jbx-sandbox 上下文).
# ---------------------------------------------------------------------------
# 以下三个 SID helper 已内联进 _create_restricted_token 并修复旧版悬垂指针/
# padding 错位/nSubAuthorityCount 漏 21 的缺陷. P2-25 删除死代码 (需恢复见 git 5f841f7a).


def _create_restricted_token() -> int:
    """第二跳核心: 在 runner 上下文创建 Write-Restricted Token.

    受限 SID 列表 = [Everyone, 当前 LogonSession, JHXSandboxWrite].
    Flags = DISABLE_MAX_PRIVILEGE | SANDBOX_INERT | WRITE_RESTRICTED.
    """
    advapi32 = _get_advapi32()
    kernel32 = get_kernel32()
    h_token = wintypes.HANDLE()
    token_assign_primary = 0x0001  # noqa: N806 - Win32 SDK 常量风格
    token_duplicate = 0x0002  # noqa: N806 - Win32 SDK 常量风格
    token_query = 0x0008  # noqa: N806 - Win32 SDK 常量风格
    token_adjust_default = 0x0080  # noqa: N806 - Win32 SDK 常量风格
    desired = (
        token_assign_primary | token_duplicate | token_query | token_adjust_default
    )
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), desired, ctypes.byref(h_token),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # 内联构造 SID buffer 并持有引用直到 CreateRestrictedToken 返回 (防悬垂指针 → WinError 998).
        # Everyone SID (CreateWellKnownSid, 持久 buffer).
        everyone_buf = (ctypes.c_byte * 64)()
        everyone_size = wintypes.DWORD(64)
        if not advapi32.CreateWellKnownSid(
            const.WIN_WORLD_SID, None, everyone_buf, ctypes.byref(everyone_size),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        # Logon session SID (从 token TokenGroups 提取, 持久 buffer).
        ret_len = wintypes.DWORD(0)
        advapi32.GetTokenInformation(
            h_token, const.TOKEN_GROUPS, None, 0, ctypes.byref(ret_len),
        )
        logon_buf = (ctypes.c_byte * ret_len.value)()
        if not advapi32.GetTokenInformation(
            h_token, const.TOKEN_GROUPS, logon_buf, ret_len, ctypes.byref(ret_len),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        groups_struct = ctypes.cast(logon_buf, ctypes.POINTER(TokenGroup)).contents
        count = groups_struct.GroupCount
        arr_t = SidAndAttr * count
        # groups 起始用 Groups.offset (ctypes 自动算对齐 padding), 手动加 sizeof(DWORD) 在 64 位会漏 padding.
        groups = ctypes.cast(
            ctypes.addressof(groups_struct) + TokenGroup.Groups.offset,
            ctypes.POINTER(arr_t),
        ).contents
        se_group_logon_id = 0x40000000  # noqa: N806 - Win32 SDK 常量风格
        logon_sid_val = None
        for g in groups:
            if g.Attributes & se_group_logon_id:
                logon_sid_val = g.Sid
                break
        if logon_sid_val is None:
            logon_sid_val = groups[0].Sid if count else None
        # 拿不到 logon SID 时只用 [Everyone, JHXSandboxWrite], 数组大小动态调整 (硬塞 NULL 会 WinError 87).
        entries = [
            SidAndAttr(ctypes.cast(everyone_buf, ctypes.c_void_p), 0),
        ]
        if logon_sid_val is not None:
            entries.append(SidAndAttr(logon_sid_val, 0))

        # 合成 JHXSandboxWrite SID (AllocateAndInitializeSid 堆分配, 不悬垂).
        sid_auth_nt = (ctypes.c_byte * 6)(0, 0, 0, 0, 0, 5)  # noqa: N806 - Win32 SDK 常量风格
        write_sid_ptr = ctypes.c_void_p()
        ok = advapi32.AllocateAndInitializeSid(
            ctypes.byref(sid_auth_nt), 4,
            21,
            const.SYNTHETIC_WRITE_SID_SUBAUTHS[0],
            const.SYNTHETIC_WRITE_SID_SUBAUTHS[1],
            const.SYNTHETIC_WRITE_SID_RID,
            0, 0, 0, 0,
            ctypes.byref(write_sid_ptr),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        entries.append(SidAndAttr(write_sid_ptr, 0))

        restricting = (SidAndAttr * len(entries))(*entries)
        restricted = wintypes.HANDLE()
        logger.info(
            "CreateRestrictedToken 调用: restricting_sids=%d, flags=0x%x",
            len(entries), const.RESTRICTED_TOKEN_FLAGS,
        )
        ok = advapi32.CreateRestrictedToken(
            h_token, const.RESTRICTED_TOKEN_FLAGS,
            0, None,       # disabling sids (空)
            0, None,       # deleting privileges (空)
            len(entries), restricting,  # restricting sids (PSID_AND_ATTRIBUTES, 数组对象自动转指针)
            ctypes.byref(restricted),
        )
        if not ok:
            err = ctypes.WinError(ctypes.get_last_error())
            logger.error("CreateRestrictedToken 失败: %s", err)
            raise err
        logger.info("CreateRestrictedToken 成功: handle=%d", int(restricted.value))
        return int(restricted.value)
    finally:
        kernel32.CloseHandle(h_token)


def _get_runner_primary_token() -> int:
    """拿 runner 自身进程的 primary token (未受限), 供 exec 起 child 用.

    受限 token 会让 child 0xC0000142 (desktop/全局对象机制硬限制), 故弃用受限 token,
    改用未受限 primary token. 代价: 失去双重写检查, 写控制只剩 ACL. 调用方用完须 CloseHandle.
    """
    advapi32 = _get_advapi32()
    kernel32 = get_kernel32()
    h_token = wintypes.HANDLE()
    token_query = 0x0008  # noqa: N806 - Win32 SDK 常量风格
    token_duplicate = 0x0002  # noqa: N806 - Win32 SDK 常量风格
    token_assign_primary = 0x0001  # noqa: N806 - Win32 SDK 常量风格
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        token_query | token_duplicate | token_assign_primary,
        ctypes.byref(h_token),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(h_token.value)


# userenv.dll: GetUserProfileDirectoryW (拿 jbx-sandbox profile 路径).
_userenv: ctypes.WinDLL | None = None


def _get_userenv() -> ctypes.WinDLL:
    """userenv.dll 的 GetUserProfileDirectoryW: 从 token 拿用户 profile 目录."""
    global _userenv
    if _userenv is None:
        _userenv = ctypes.WinDLL("userenv", use_last_error=True)
        _userenv.GetUserProfileDirectoryW.argtypes = [
            wintypes.HANDLE,             # hToken
            wintypes.LPWSTR,             # lpProfileDir
            ctypes.POINTER(wintypes.DWORD),  # lpcchSize (in/out, WCHAR 数)
        ]
        _userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL
    return _userenv


def get_sandbox_profile_dir() -> str | None:
    """拿 jbx-sandbox 的 profile 目录 (如 C:\\Users\\jbx-sandbox).

    用 runner primary token (jbx-sandbox 身份) 调 GetUserProfileDirectoryW,
    返回 profile 根目录. 失败返回 None (调用方降级). 比 hardcode
    C:\\Users\\jbx-sandbox 稳: 同名残留 profile 会建 .000 后缀, 路径不固定,
    必须 API 拿真实路径. token 用完即关.
    """
    kernel32 = get_kernel32()
    try:
        token = _get_runner_primary_token()
    except OSError:
        return None
    try:
        userenv = _get_userenv()
        size = wintypes.DWORD(0)
        # 第一次探 size (传 NULL buf, 返回所需 WCHAR 数含末尾 NUL).
        userenv.GetUserProfileDirectoryW(wintypes.HANDLE(token), None, ctypes.byref(size))
        if size.value == 0:
            return None
        buf = ctypes.create_unicode_buffer(size.value)
        if not userenv.GetUserProfileDirectoryW(
            wintypes.HANDLE(token), buf, ctypes.byref(size),
        ):
            return None
        return buf.value or None
    except OSError:
        return None
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(token))


def _create_process_as_user(  # pylint: disable=huawei-too-many-arguments
    restricted_token: int,
    command: list[str],
    env: dict[str, str] | None,
    workdir: str | None,
    stdin_fd: int,
    stdout_fd: int,
    workspace: str | None = None,
) -> "tuple[int, int]":
    """CreateProcessAsUserW 以受限 token 启动子命令.

    Returns: (child_pid, child_process_handle).
    """
    advapi32 = _get_advapi32()
    # P2-18: 用 subprocess.list2cmdline 正确转义命令行. 旧版 " ".join 只对含空格参数加外层双引号不转义内部 ",
    # command 形如 ['bash','-lc','python -c "print("ok")"'] 时 bash 收到的双引号被 Windows CRT 当边界 → argv 错位 → SyntaxError.
    # list2cmdline 按 MSVCRT argv 规则把内部 " 转义为 \".
    import subprocess as _sp
    cmd_line = _sp.list2cmdline(command)
    # CreateProcessAsUserW 的 lpCommandLine 需可变 buffer (Windows 原地修改),
    # 不能直接传 str (ctypes 转 c_wchar_p 只读, 修改触发段错误).
    cmd_line_buf = ctypes.create_unicode_buffer(cmd_line)
    # 始终构造 env block (env=None 回退 os.environ). 不能传 NULL 给 CreateProcessAsUserW
    # (空环境无 PATH → WinError 2). 自动注入 HTTP(S)_PROXY 指向代理端口 (文档 §6.6).
    env_block_buf = None
    env_block_ptr = None
    if env is None:
        env = dict(os.environ)
    else:
        env = dict(env)
    # 兜底补齐 DLL 加载/子进程运行所需基本变量: 缺 SystemRoot 等会 STATUS_DLL_INIT_FAILED (0xC0000142).
    # box-server 端 _build_windows_exec_env 已补一次, 经 IPC 序列化后调用方 env 可能残缺, 第二跳再补 (setdefault 不覆盖).
    for _var in ("SystemRoot", "windir", "PATHEXT", "COMSPEC"):
        _val = os.environ.get(_var)
        if _val:
            env.setdefault(_var, _val)
    # 沙箱可写临时区 + profile 变量补全: child env 来自 header 不带 profile 变量,
    # 用 runner token 拿 jbx-sandbox profile 目录, TEMP 指向每沙箱隔离子目录
    # (<profile>\AppData\Local\Temp\jiuwenbox\<sandbox_id>\). ms-playwright 安装目录跨沙箱共用不重下.
    # 降级: 拿不到 profile → 回落 workspace/.tmp.
    _sandbox_id = os.path.basename(workspace.rstrip("\\/")) if workspace else None
    if _sandbox_id:
        _profile_dir = get_sandbox_profile_dir()
        if _profile_dir:
            _child_tmp = os.path.join(
                _profile_dir, "AppData", "Local", "Temp", "jiuwenbox", _sandbox_id,
            )
            try:
                os.makedirs(_child_tmp, exist_ok=True)
            except OSError as _e:
                _push_log("WARNING",
                          f"runner 预建沙箱 Temp 失败 (照设路径, 沙箱内自建): "
                          f"{_child_tmp}: {_e}")
            env["TEMP"] = _child_tmp
            env["TMP"] = _child_tmp
            # 补全 profile 变量 (header env 不带).
            env.setdefault("USERPROFILE", _profile_dir)
            env.setdefault("LOCALAPPDATA", os.path.join(_profile_dir, "AppData", "Local"))
            env.setdefault("APPDATA", os.path.join(_profile_dir, "AppData", "Roaming"))
    # 降级: 拿不到 profile → 回落 workspace/.tmp.
    if "TEMP" not in env and workspace:
        _child_tmp = os.path.join(workspace, ".tmp")
        try:
            os.makedirs(_child_tmp, exist_ok=True)
        except OSError:
            pass
        env["TEMP"] = _child_tmp
        env["TMP"] = _child_tmp
    elif "TEMP" not in env:
        for _var in ("TEMP", "TMP"):
            _val = os.environ.get(_var)
            if _val:
                env.setdefault(_var, _val)
    # 自动注入代理 env (文档 §6.6): 即使调用方没传, 也让遵守代理协议的程序
    # (pip/git/curl/node 等) 走 win_proxy, WFP 兜底拦截不走代理的出网.
    proxy_url = f"http://127.0.0.1:{_proxy_port_start}"
    env.setdefault("HTTP_PROXY", proxy_url)
    env.setdefault("HTTPS_PROXY", proxy_url)
    env.setdefault("http_proxy", proxy_url)
    env.setdefault("https_proxy", proxy_url)
    env.setdefault("ALL_PROXY", proxy_url)
    # NO_PROXY 放行 loopback 自身 (代理 → 代理 不该再走代理).
    env.setdefault("NO_PROXY", "127.0.0.1,localhost,::1")
    # JIUWENBOX_INJECT_ENV 通用约定键: 调用方把"子进程需要的额外 env"以 JSON 编码塞进此键,
    # 沙箱解析后 setdefault 注入子进程, 再删该键本身 (不泄漏). 沙箱不认识工具语义,
    # 新工具只改调用方. 解析失败只 warning 不阻断.
    _inject_raw = env.pop("JIUWENBOX_INJECT_ENV", None)
    if _inject_raw:
        try:
            import json as _json  # noqa: A004
            _injected = _json.loads(_inject_raw)
            if not isinstance(_injected, dict):
                raise ValueError(f"expected JSON object, got {type(_injected).__name__}")
        except (ValueError, TypeError) as _e:
            _injected = None
            _push_log(
                "WARNING",
                f"JIUWENBOX_INJECT_ENV 解析失败, 已忽略: {_e} raw_len={len(_inject_raw)}",
            )
        if _injected:
            # P1-17: 注入键黑名单 — 防代码注入类键 (LD_PRELOAD/PYTHONPATH/PATH 等) 绕过沙箱. 纵深防御.
            forbidden_inject_keys = frozenset({  # noqa: N806 - Win32 SDK 常量风格
                "LD_PRELOAD", "LD_LIBRARY_PATH",
                "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONSAFEPATH",
                "NODE_OPTIONS", "NODE_PATH", "NODE_EXTRA_CA_CERTS",
                "PATH",  # 覆盖 PATH 可劫持可执行解析
                "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
            })
            _injected_keys = []
            _blocked_keys = []
            for _k, _v in _injected.items():
                _ks = str(_k)
                if _ks.upper() in forbidden_inject_keys:
                    _blocked_keys.append(_ks)
                    continue
                env.setdefault(_ks, str(_v))
                _injected_keys.append(_ks)
            if _blocked_keys:
                _push_log(
                    "WARNING",
                    f"JIUWENBOX_INJECT_ENV 拒绝注入黑名单键: {_blocked_keys}",
                )
            _push_log(
                "INFO",
                f"injected env from JIUWENBOX_INJECT_ENV: keys={_injected_keys}",
            )
    _push_log(
        "INFO",
        f"child proxy injected: HTTPS_PROXY={env.get('HTTPS_PROXY')} "
        f"NO_PROXY={env.get('NO_PROXY')} "
        f"LOCALAPPDATA={env.get('LOCALAPPDATA')} "
        f"PLAYWRIGHT_BROWSERS_PATH={env.get('PLAYWRIGHT_BROWSERS_PATH')} "
        f"USERPROFILE={env.get('USERPROFILE')} HOME={env.get('HOME')} "
        f"TEMP={env.get('TEMP')}",
    )
    parts = [f"{k}={v}" for k, v in env.items()]
    block = "\0".join(parts) + "\0\0"
    env_block_buf = ctypes.create_unicode_buffer(block)
    env_block_ptr = ctypes.cast(env_block_buf, ctypes.c_void_p)

    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(STARTUPINFOW)
    startup.dwFlags = 0x00000100  # STARTF_USESTDHANDLES noqa: N815 - Win32 SDK 字段名
    startup.hStdInput = wintypes.HANDLE(stdin_fd)  # noqa: N815
    startup.hStdOutput = wintypes.HANDLE(stdout_fd)  # noqa: N815
    startup.hStdError = wintypes.HANDLE(stdout_fd)  # noqa: N815
    pi = ProcessInfo()

    # env block 始终构造 (env=None 回退 os.environ), 故必须带 UNICODE flag,
    # 否则 CreateProcessAsUserW 按 ANSI 解析 env block → WinError 87.
    creation_flags = (
        const.CREATE_NO_WINDOW
        | const.CREATE_NEW_PROCESS_GROUP
        | const.CREATE_UNICODE_ENVIRONMENT
    )
    cwd = workdir if workdir else None

    ok = advapi32.CreateProcessAsUserW(
        wintypes.HANDLE(restricted_token),
        None,
        cmd_line_buf,
        None, None,
        True,  # inherit handles
        creation_flags,
        env_block_ptr,  # CreateProcessAsUserW 在此返回前读取 env block
        cwd,
        ctypes.byref(startup), ctypes.byref(pi),
    )
    if not ok:
        # 诊断: CreateProcessAsUserW 失败区分"PATH 找不到" vs "ACL 读不了" (WinError 2 常见).
        # 打印 command[0]/PATH 片段/目标存在性+可读性, 经日志长连发回 box-server.
        _cmd0 = str(command[0]) if command else "<empty>"
        _path_val = env.get("PATH", "") if isinstance(env, dict) else ""
        _path_segs = (_path_val or "").split(os.pathsep)[:8]
        import ntpath as _ntp
        _resolved = None
        for _seg in _path_segs:
            if not _seg:
                continue
            _cand = _ntp.join(_seg, _cmd0)
            if os.path.isfile(_cand):
                _resolved = _cand
                break
        _exists = os.path.isfile(_cmd0)
        _readable = os.access(_cmd0, os.R_OK) if _exists else False
        _push_log(
            "ERROR",
            f"CreateProcessAsUserW 失败 cmd0={_cmd0!r} resolved_in_PATH={_resolved!r} "
            f"cmd0_exists={_exists} cmd0_readable={_readable} "
            f"PATH_segs(8)={_path_segs}",
        )
        raise ctypes.WinError(ctypes.get_last_error())
    return int(pi.dwProcessId), int(pi.hProcess)


def _read_control_token_from_stdin(fallback: str) -> str:
    """从 stdin pipe 读 control_token (review #2).

    协议 [4 字节大端长度][token 字节]. box-server 在 resume runner 前把 token
    写进 pipe 并 CloseHandle 写端, runner resume 后从 stdin 读到 [len][token]
    再 EOF. 读到非空 token 则用之; 读不到 (无 pipe / 格式错 / EOF) 回退
    fallback (命令行 --control-token, 兼容旧调用).

    读完后 CloseHandle(stdin) 释放 pipe 读端, 再让 runner 切到 socket accept
    (避免 stdin 残留影响后续).
    """
    import msvcrt  # type: ignore[import-not-found]
    kernel32 = get_kernel32()
    stdin_handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE = -10  # noqa: N806
    if not stdin_handle:
        # 无 stdin handle (非 pipe 启动), 用 fallback.
        _push_log("DEBUG", f"control_token 用命令行回退 (无 stdin handle): {bool(fallback)}")
        return fallback
    fd = -1
    try:
        fd = msvcrt.open_osfhandle(int(stdin_handle), os.O_RDONLY | os.O_BINARY)
    except OSError:
        return fallback
    try:
        # 读 4 字节大端长度前缀.
        prefix = b""
        while len(prefix) < 4:
            chunk = os.read(fd, 4 - len(prefix))
            if not chunk:
                # EOF 前没读够 4 字节 → 不是 pipe token 协议, 回退 fallback.
                return fallback
            prefix += chunk
        if len(prefix) != 4:
            return fallback
        token_len = int.from_bytes(prefix, "big")
        if token_len <= 0 or token_len > 1024:
            # 长度不合理 (token_urlsafe(32) ≈ 43 字节), 回退 fallback.
            _push_log("WARNING", f"control_token pipe 长度异常: {token_len}, 用命令行回退")
            return fallback
        token_bytes = b""
        while len(token_bytes) < token_len:
            chunk = os.read(fd, token_len - len(token_bytes))
            if not chunk:
                return fallback  # EOF 前未读完, 回退.
            token_bytes += chunk
        token = token_bytes.decode("utf-8")
        _push_log("INFO", "control_token 从 stdin pipe 读取成功")
        return token
    except OSError:
        # stdin 不是 pipe (普通空 stdin) → read 立即 EOF 或报错, 用 fallback.
        return fallback
    finally:
        # os.close(fd) 会同时关闭底层 Win32 stdin handle (open_osfhandle 绑定),
        # 切勿再单独 CloseHandle(stdin_handle) — 否则双重关闭报 ERROR_INVALID_HANDLE.
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Runner 并发状态: 对齐 Linux sandbox_daemon.DaemonState.
# ---------------------------------------------------------------------------

# 单沙箱 worker 并发上限. Linux daemon 无界 (每连接 fork), 但 Windows 线程比
# fork 重, 且 runner 跑在受限 token 下, 无界线程在突发请求时易耗尽栈/句柄.
# 与 box-server 的 EXEC_CONCURRENCY 对齐: 优先读 JIUWENBOX_EXEC_CONCURRENCY env,
# 与全局信号量同值, 避免 box-server 并发发 >limit 个 exec 时 runner 侧排队;
# env 未设时回落到 16 (覆盖常见 CPU 核数, 超大核机器需显式设 env 提高).
_WIN_RUNNER_WORKER_LIMIT_ENV = "JIUWENBOX_EXEC_CONCURRENCY"
_DEFAULT_WIN_RUNNER_WORKER_LIMIT = 16


def _resolve_win_runner_worker_limit() -> int:
    """读 env 解析 worker 上限, 失败回落到默认 16."""
    raw = os.environ.get(_WIN_RUNNER_WORKER_LIMIT_ENV)
    if not raw:
        return _DEFAULT_WIN_RUNNER_WORKER_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_WIN_RUNNER_WORKER_LIMIT
    return value if value >= 1 else _DEFAULT_WIN_RUNNER_WORKER_LIMIT


class _WinRunnerState:
    """Runner 共享可变状态, guarded by ``lock``."""

    def __init__(self, worker_limit: int = _DEFAULT_WIN_RUNNER_WORKER_LIMIT) -> None:
        self.shutdown_event = _threading.Event()
        self.in_flight = 0
        self.lock = _threading.Lock()
        self.completion = _threading.Condition(self.lock)
        # worker 并发上限: 防突发请求耗尽 runner 线程/句柄; 超限的请求在 acquire
        self.worker_sem = _threading.Semaphore(
            max(1, worker_limit)
        ) if worker_limit > 0 else None

    def begin_request(self) -> None:
        with self.lock:
            self.in_flight += 1

    def end_request(self) -> None:
        with self.lock:
            self.in_flight -= 1
            if self.in_flight <= 0:
                self.completion.notify_all()

    def acquire_worker(self) -> None:
        if self.worker_sem is not None:
            self.worker_sem.acquire()

    def release_worker(self) -> None:
        if self.worker_sem is not None:
            self.worker_sem.release()

    def wait_drain(self, timeout: float) -> bool:
        import time as _time
        deadline = _time.monotonic() + max(0.0, timeout)
        with self.lock:
            while self.in_flight > 0:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    return False
                self.completion.wait(timeout=remaining)
            return True


def _handle_one_request(  # pylint: disable=huawei-too-many-arguments
    conn: Any,
    header: dict,
    req_type: str,
    restricted_token: int | None,
    workspace: str,
    state: _WinRunnerState,
) -> None:
    """单个业务请求的 worker 线程入口 (exec/write_file/read_file/list_dir).

    主线程已完成 accept + recv header + 鉴权, 本函数接管 conn 做业务派发 +
    (exec 的) stdin 读取 + conn.close.
    """
    state.begin_request()
    try:
        state.acquire_worker()
        try:
            if req_type == "exec":
                stdin_size = int(header.get("stdin_size", 0))
                stdin_bytes = recv_frame(conn, MAX_STDIN_BYTES) if stdin_size > 0 else b""
                _handle_exec_request(
                    conn, header, restricted_token, workspace, stdin_bytes,
                )
            elif req_type == "write_file":
                _handle_write_file_request(conn, header, conn)
            elif req_type == "read_file":
                _handle_read_file_request(conn, header)
            elif req_type == "list_dir":
                _handle_list_dir_request(conn, header)
            else:
                _send_error_response(conn, f"unknown request type: {req_type!r}")
        finally:
            state.release_worker()
        return
    except OSError as exc:
        # 连接已断 (box-server 超时关闭 / 对端崩). 单连接失败不杀 runner.
        logger.debug("runner 处理 %s 请求连接异常, 跳过: %s", req_type, exc)
    except Exception as exc:  # noqa: BLE001
        import traceback as _tb_req
        logger.debug("runner 处理 %s 请求异常: %s", req_type, exc, exc_info=True)
        _push_log("WARNING",
                  f"runner 处理 {req_type} 请求异常 (单连接, 不杀 runner): "
                  f"{exc}", exc=_tb_req.format_exc())
    finally:
        try:
            conn.close()
        except OSError:
            pass
        state.end_request()


def runner_main(argv: list[str]) -> int:
    """runner 入口 (运行在 jbx-sandbox 上下文, 由 broker 第一跳拉起).

    职责:
      1. 创建 Write-Restricted Token.
      2. 循环从 stdin 读长度前缀帧 (exec / write_file / read_file /
         list_dir / shutdown).
      3. 对每个 exec 请求, 以受限 token CreateProcessAsUserW 起子命令,
         收集 stdout/stderr/exit, 写回 stdout 帧.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="win_exec-runner")
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--proxy-port-start", type=int, default=const.DEFAULT_PROXY_PORT_RANGE_START)
    parser.add_argument("--proxy-port-end", type=int, default=const.DEFAULT_PROXY_PORT_RANGE_END)
    parser.add_argument("--control-port", type=int, required=True)
    # P0-6: 鉴权 token (box-server 分配, 首帧校验). 旧版 runner 无鉴权, 本机任意
    # 进程可 connect control_port 越权 exec; 现要求首帧 header["token"] 匹配.
    # review #2: token 优先从 stdin pipe 读 (避命令行 WMI/PEB 泄露), --control-token
    # 保留为兼容回退 (默认空串). pipe 读不到时用命令行值.
    parser.add_argument("--control-token", default="")
    args = parser.parse_args(argv)
    control_token = _read_control_token_from_stdin(args.control_token)

    # 记录代理端口到模块级, 供 _create_process_as_user 自动注入 HTTP_PROXY env.
    global _proxy_port_start, _proxy_port_end
    _proxy_port_start = args.proxy_port_start
    _proxy_port_end = args.proxy_port_end

    _require_windows()
    logger.info(
        "runner 启动: sandbox_id=%s workspace=%s control_port=%s",
        args.sandbox_id, args.workspace, args.control_port,
    )
    # 初始化本地落盘日志, 必须在 _push_log 之前 (之后 _push_log 自动落盘一份).
    _init_local_log(args.sandbox_id)
    _push_log("INFO", f"runner 启动: sandbox_id={args.sandbox_id} "
               f"workspace={args.workspace} control_port={args.control_port}"
               f" local_log={_local_log_path}")

    # 受限 token 当前未被 exec 消费 (exec 用 _get_runner_primary_token 起 child, 因受限 token
    # 会让 child 0xC0000142). 保留构造为未来恢复双重写检查留底: 改 _handle_exec_request 内
    # _self_token 取值回入参 restricted_token 即可. 构造失败降级 best-effort, 不杀 runner.
    restricted_token: int | None = None
    try:
        restricted_token = _create_restricted_token()
    except Exception:  # noqa: BLE001
        logger.warning(
            "_create_restricted_token 失败, 已弃用故不阻断 runner "
            "(sandbox_id=%s)", args.sandbox_id, exc_info=True,
        )
        _push_log("WARNING",
                  f"受限 token 构造失败但已弃用, 不阻断 runner "
                  f"(sandbox_id={args.sandbox_id})")
    if restricted_token is not None:
        _push_log("INFO", f"restricted token 创建成功: handle={restricted_token}")

    # TCP loopback 控制端口 (box-server 分配, 命令行参数传入). runner bind + listen,
    # box-server 每次 exec connect 一条新连接, 发一帧请求读一帧响应后 close.
    # 对齐 Linux AF_UNIX 模型 (Windows 不能传 fd, 改传端口号).
    port = args.control_port
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(64)
    except OSError as exc:
        logger.error("runner bind 127.0.0.1:%d 失败: %s", port, exc)
        _push_log("ERROR", f"runner bind 127.0.0.1:{port} 失败: {exc}")
        return 1
    logger.info("runner 监听 127.0.0.1:%d (sandbox_id=%s)", port, args.sandbox_id)
    _push_log("INFO", f"runner 监听 127.0.0.1:{port} (sandbox_id={args.sandbox_id})")

    state = _WinRunnerState(worker_limit=_resolve_win_runner_worker_limit())

    try:
        while not state.shutdown_event.is_set():
            try:
                conn, _ = listener.accept()
            except OSError as exc:
                if state.shutdown_event.is_set():
                    break
                logger.warning("runner accept() 失败: %s", exc)
                continue
            try:
                header_frame = recv_frame(conn, MAX_HEADER_BYTES)
            except (OSError, ValueError):  # ConnectionError 是 OSError 子类, 不重复捕获
                # client 连上后立刻断开 (探活/异常), 直接关连接等下一个.
                conn.close()
                continue
            try:
                header = json.loads(header_frame.decode("utf-8"))
            except Exception as exc:
                _send_error_response(conn, f"invalid request header: {exc}")
                conn.close()
                continue

            # P0-6: 鉴权 token 校验 (首帧). control_token 非空时, header 必须带
            # 匹配的 "token"; 不匹配/缺失则拒绝 (防本机任意进程越权 exec).
            # control_token 为空 (旧版兼容/未传) 时跳过校验.
            if control_token:
                _req_token = header.get("token")
                if not _req_token or not _hmac_compare(str(_req_token), control_token):
                    _push_log("WARNING",
                              f"拒绝未授权连接: token 不匹配 (req_type={header.get('type')})")
                    try:
                        _send_error_response(conn, "unauthorized: invalid or missing token")
                    except OSError:
                        pass
                    conn.close()
                    continue

            req_type = header.get("type")
            # subscribe_log / shutdown 必须在主线程串行处理: 前者把 conn 加入长连
            # 订阅集 (不能并发 append); 后者要 set shutdown_event 让 accept 循环
            # 退出 + drain 在途 worker.
            if req_type == REQUEST_TYPE_SUBSCRIBE_LOG:
                try:
                    conn.setblocking(True)
                    with _log_sub_lock:
                        _log_subscribers.append(conn)
                    _push_log("INFO",
                              "日志订阅连接已建立 (box-server -> runner 长连)")
                except OSError as exc:
                    _push_log("WARNING", f"订阅连接入集失败: {exc}")
                    try:
                        conn.close()
                    except OSError:
                        pass
                continue
            if req_type == "shutdown":
                _send_response(conn, {"ok": True})
                conn.close()
                state.shutdown_event.set()
                break
            # exec / write_file / read_file / list_dir: 派发 worker 线程并发处理
            # (对齐 Linux daemon 的 fork+execve 并发模型).
            worker = _threading.Thread(
                target=_handle_one_request,
                args=(conn, header, req_type, restricted_token,
                       args.workspace, state),
                name=f"win-runner-worker",
                daemon=True,
            )
            worker.start()
    except Exception:  # noqa: BLE001
        import traceback as _tb
        tb = _tb.format_exc()
        logger.exception("runner accept 循环异常 (sandbox_id=%s)", args.sandbox_id)
        _push_log("ERROR",
                  f"runner accept 循环异常 (sandbox_id={args.sandbox_id})", exc=tb)
    finally:
        if state.in_flight > 0:
            _push_log("INFO",
                      f"runner shutdown: drain {state.in_flight} 个在途 worker "
                      f"(最多等 {WIN_RUNNER_SHUTDOWN_DRAIN_SECONDS}s)")
            state.wait_drain(WIN_RUNNER_SHUTDOWN_DRAIN_SECONDS)
        _push_log("INFO", f"runner 退出 (sandbox_id={args.sandbox_id})")
        # P2-26: 显式关闭本地落盘日志文件.
        if _local_log_file is not None:
            try:
                _local_log_file.close()
            except OSError:
                pass
        kernel32 = get_kernel32()
        kernel32.CloseHandle(wintypes.HANDLE(restricted_token))
        listener.close()
        # 关闭所有日志订阅连接, 通知订阅方 runner 已退出.
        with _log_sub_lock:
            subs = list(_log_subscribers)
            _log_subscribers.clear()
        for sock in subs:
            try:
                sock.close()
            except OSError:
                pass
    return 0


def _send_response(stream, payload: dict) -> None:
    send_frame(stream, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _send_error_response(stream, detail: str) -> None:
    _send_response(stream, {"ok": False, "error": "io_error", "detail": detail})


# 匹配双引号包围的片段 (不含内部双引号). 用于规整 bash -lc script 中双引号内的
# Windows 路径反斜杠, 把 \ 替换为 / (bash 双引号内会吞掉非特殊反斜杠, 详见
# _handle_exec_request 注释). 单引号/双引号外内容不动, 避免误伤 shell 元字符.
_DQ_SEGMENT_RE = re.compile(r'"[^"]*"')


def _normalize_bash_script_backslashes(command: list) -> list | None:
    """对 ["bash"|"sh", "-lc"|"-c", script, ...] 的 script 做反斜杠规整.

    返回新 command list; 不命中模式返回 None (调用方保持原样).
    """
    if len(command) < 3:
        return None
    exe = str(command[0])
    flag = str(command[1])
    exe_base = os.path.basename(exe).lower()
    if exe_base not in ("bash", "bash.exe", "sh", "sh.exe"):
        return None
    if flag not in ("-lc", "-c"):
        return None
    script = str(command[2])
    if "\\" not in script:
        return None

    def _norm_dq(m: "re.Match[str]") -> str:
        seg = m.group(0)
        # seg 形如 "...", 把内部的反斜杠换成正斜杠 (Windows/node 能正确解析).
        inner = seg[1:-1].replace("\\", "/")
        return f'"{inner}"'

    new_script = _DQ_SEGMENT_RE.sub(_norm_dq, script)
    if new_script == script:
        return None
    new_command = list(command)
    new_command[2] = new_script
    _push_log(
        "DEBUG",
        f"bash script backslash normalized (flag={flag}): "
        f"before={script[:120]!r} after={new_script[:120]!r}",
    )
    return new_command


# box-server 在 _create_windows 把 policy 探测到的 bash 绝对路径 (git 的
# usr/bin/bash.exe) 经 env 注入 runner. runner exec 时若 cmd0 是裸 "bash"/"sh"
# (无路径分隔符), 替换成该绝对路径
_BASH_PATH_ENV = "JIUWENBOX_BASH_PATH"


def _rewrite_bash_to_absolute(command: list) -> None:
    """把裸 bash/sh cmd0 重写为 JIUWENBOX_BASH_PATH 指向的绝对路径 (原地改 command).

    不命中 (无 env / cmd0 非裸名 / 绝对路径文件不存在) 则静默, 回退原 PATH 搜索.
    """
    if not command:
        return
    exe = str(command[0])
    exe_base = os.path.basename(exe).lower()
    if exe_base not in ("bash", "bash.exe", "sh", "sh.exe"):
        return
    # 已带路径分隔符 (绝对/相对路径) 不重写, 尊重调用方显式指定.
    if "\\" in exe or "/" in exe:
        return
    bash_path = (os.environ.get(_BASH_PATH_ENV) or "").strip()
    if not bash_path or not os.path.isfile(bash_path) or _is_wsl_bash(bash_path):
        return
    command[0] = bash_path
    _push_log(
        "DEBUG",
        f"bash cmd0 rewritten to absolute path: {exe!r} -> {bash_path!r}",
    )


def _decode_child_output(buf: bytearray | bytes) -> str:
    """解码 child stdout 原始字节, 自动探测编码.

    Windows 子进程 (cmd.exe / wsl.exe / powershell) 被重定向到匿名管道时,
    输出编码不固定: cmd 的本地化资源串 / WSL 横幅常为 UTF-16LE; Git bash 的
    中文常为 GBK/GB18030; 一般工具多为 UTF-8. 旧实现硬解 UTF-8 导致 UTF-16LE
    输出变 mojibake (故障机 WSL 横幅乱码). 这里按优先级探测:

    1. BOM 判定 (UTF-16LE/BE/UTF-8-sig);
    2. UTF-8 严格 (ASCII/UTF-8 文本在此成功, 不会误判为 UTF-16);
    3. UTF-16LE 启发式 (严格 UTF-8 失败后, 高位字节 0x00 占比 >0.3);
    4. GB18030 (中文 Windows OEM/ANSI 兜底);
    5. UTF-8 errors=replace (保持旧兜底行为, 永不抛).
    """
    data = bytes(buf)
    if not data:
        return ""

    # 1. BOM.
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")

    # 2. UTF-8 严格. 纯 ASCII / UTF-8 文本在此成功, 不会误判为 UTF-16.
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 3. UTF-16LE 启发式: 严格 UTF-8 失败后, 试 UTF-16LE.
    sample = data[:128]
    hi = sample[1::2]
    if len(hi) >= 4:
        zero_ratio = sum(1 for b in hi if b == 0) / len(hi)
        if zero_ratio > 0.3:
            try:
                return data.decode("utf-16-le", errors="replace")
            except (UnicodeDecodeError, ValueError):  # pylint: disable=not-get-same-exception
                pass

    # 4. GB18030 (GBK 超集, 中文 Windows 最可能 OEM/ANSI 代码页).
    try:
        return data.decode("gb18030", errors="replace")
    except (UnicodeDecodeError, LookupError):
        pass

    # 5. 兜底: 保持旧实现行为.
    return data.decode("utf-8", errors="replace")


_BASH_GONE_CACHE: dict[str, bool] = {}


def _is_wsl_bash(path: str) -> bool:
    """path 是否指向 WSL launcher (C:\\Windows\\System32\\bash.exe = wsl.exe 别名).

    WSL bash 在沙箱里只打印 "没有已安装的发行版" 横幅后退出, 脚本不执行, 视同不可用.
    """
    if not path:
        return True
    low = os.path.normpath(path).lower()
    base = os.path.basename(low)
    if base != "bash.exe":
        return False
    # System32\\bash.exe 即 WSL launcher (真 bash 不会装在 System32).
    return low.endswith(os.sep + "system32" + os.sep + "bash.exe") or \
        low.endswith("/system32/bash.exe")


def _bash_unavailable() -> bool:
    """沙箱机器上是否找不到可用 bash (PATH 搜索 + JIUWENBOX_BASH_PATH 都无/仅 WSL)."""
    cached = _BASH_GONE_CACHE.get("gone")
    if cached is not None:
        return cached
    env_bash = (os.environ.get(_BASH_PATH_ENV) or "").strip()
    if env_bash and os.path.isfile(env_bash) and not _is_wsl_bash(env_bash):
        _BASH_GONE_CACHE["gone"] = False
        return False
    found = shutil.which("bash") or shutil.which("sh")
    # 搜到的若是 WSL launcher, 视同没有
    if found and _is_wsl_bash(found):
        found = None
    gone = not found
    _BASH_GONE_CACHE["gone"] = gone
    return gone


def _looks_like_powershell(script: str) -> bool:
    """粗判 script 是否含 powershell 语法 (cmd 跑不了)."""
    low = script.lower()
    return any(tok in low for tok in (  # pylint: disable=complicate-comprehension
        "get-childitem", "set-location", "remove-item", "test-path",
        "select-object", "where-object", "foreach-object", "invoke-",
        "$psversiontable", "write-output", "new-item", "copy-item",
        "-erroraction", "-recurse", "-force",
    ))


def _fallback_bash_to_native_shell(command: list) -> None:
    """bash 不可用时, 把 ["bash","-lc",script] 改写为 cmd /c 或 powershell -Command."""
    if not command or len(command) != 3:
        return
    exe = str(command[0])
    flag = str(command[1])
    # 已带路径分隔符 (绝对/相对路径) 说明 _rewrite 成功找到了 bash, 不退化.
    if "\\" in exe or "/" in exe:
        return
    exe_base = os.path.basename(exe).lower()
    if exe_base not in ("bash", "bash.exe", "sh", "sh.exe"):
        return
    if flag not in ("-lc", "-c"):
        return
    script = str(command[2])
    if _looks_like_powershell(script):
        command[0] = "powershell"
        command[1] = "-Command"
        _push_log("INFO",
                  f"bash unavailable, fallback to powershell -Command: {script[:120]!r}")
    else:
        command[0] = "cmd"
        command[1] = "/c"
        _push_log("INFO",
                  f"bash unavailable, fallback to cmd /c: {script[:120]!r}")


def _handle_exec_request(stream, header, restricted_token, workspace, stdin_bytes) -> None:
    """处理 exec 请求: 起 child 子命令, 回传 stdout/stderr/exit. stdin_bytes 透传给子进程."""
    command = header.get("command", [])
    if not command:
        _send_error_response(stream, "exec requires non-empty command")
        return
    # 归一化裸名 python3/python3.x → python (venv\Scripts 只有 python.exe). 只改裸名, 不碰带路径的.
    _c0 = str(command[0])
    if _c0 in ("python3", "python3.13", "python3.12", "python3.11") or _c0.startswith("python3."):
        if "\\" not in _c0 and "/" not in _c0:
            command[0] = "python"
    # bash -lc 双引号段内反斜杠规整为正斜杠: bash 双引号内会吞掉路径反斜杠致 MODULE_NOT_FOUND.
    # 只动双引号内, 不碰单引号/双引号外, 避免误伤 shell 元字符. 只对 ["bash"|"sh","-lc"|" -c",script,...] 生效.
    _norm_command = _normalize_bash_script_backslashes(command)
    if _norm_command is not None:
        command = _norm_command
    # 裸 bash/sh cmd0 重写为 JIUWENBOX_BASH_PATH 绝对路径, 绕开 PATH 搜索误解析到 WSL.
    _rewrite_bash_to_absolute(command)
    # 若 bash 确实不可用 (机器无 Git/WSL), 退化为 cmd /c 或 powershell -Command.
    if _bash_unavailable():
        _fallback_bash_to_native_shell(command)
    # 建立 pipe 收集子进程 stdout/stderr.
    kernel32 = get_kernel32()
    sa = SecurityAttr()
    sa.nLength = ctypes.sizeof(SecurityAttr)  # noqa: N815
    sa.bInheritHandle = True  # noqa: N815
    child_out_read = wintypes.HANDLE()
    child_out_write = wintypes.HANDLE()
    kernel32.CreatePipe(
        ctypes.byref(child_out_read), ctypes.byref(child_out_write),
        ctypes.byref(sa), 0,
    )
    # 建立 stdin pipe: runner 写, child 读 (继承读端).
    child_in_read = wintypes.HANDLE()
    child_in_write = wintypes.HANDLE()
    kernel32.CreatePipe(
        ctypes.byref(child_in_read), ctypes.byref(child_in_write),
        ctypes.byref(sa), 0,
    )
    # runner 持有的写端/读端关闭继承, 防 child 拿到.
    _clear_inherit(int(child_in_write.value))
    _clear_inherit(int(child_out_read.value))  # P2-29: 防 child 继承 stdout 读端
    # review #3: 为本次 exec 创建专用 ephemeral Job (KILL_ON_JOB_CLOSE), child
    # 起后立即 assign 进 Job, 超时/正常退出时 close_job 一次性杀整个进程树
    # (含孙进程). 旧版只 TerminateProcess 杀 child, 孙进程继续占用端口/资源.
    from jiuwenbox.supervisor import win_job
    exec_job_handle = 0
    try:
        exec_job_handle = win_job.create_exec_job()
    except Exception as exc:  # noqa: BLE001 - Job 创建失败降级, 不阻断 exec
        _push_log("WARNING", f"exec ephemeral Job 创建失败, 超时将只杀 child: {exc}")
    try:
        workdir = header.get("workdir")
        env = header.get("env")
        # 用受限 token 起 child 会 0xC0000142 (desktop/全局对象机制硬限制, 非 ACL/env),
        # 故 exec 用 runner 自身未受限 primary token. 代价: 失去双重写检查, 写控制只剩 ACL.
        _self_token = _get_runner_primary_token()
        try:
            pid, proc_handle = _create_process_as_user(
                _self_token, list(command), env, workdir,
                stdin_fd=int(child_in_read.value),
                stdout_fd=int(child_out_write.value),
                workspace=workspace,
            )
        finally:
            get_kernel32().CloseHandle(wintypes.HANDLE(_self_token))
        # child 起后立即 assign 进 ephemeral Job (按 pid). 此后 child 起的孙进程
        # 自动继承进 Job; 超时 close_job 一次性杀整树. assign 失败降级 (Job 创建
        # 已成功但 assign 失败的概率极低, 失败则保留旧 TerminateProcess 兜底).
        # 注: child 用 CreateProcessAsUserW 非 SUSPENDED 起的, assign 赶在 child
        # spawn 第一跳孙进程前执行 (微秒级 vs child 加载+解析命令的毫秒级),
        # race 窗口极小但非零 — 极端情况下 child 在 assign 前已 spawn 的孙进程
        # 逃逸 Job (close_job 杀不到). 完全消除需改 CREATE_SUSPENDED+assign+resume,
        # 改动 child 起动时序影响面大, 暂不取; 当前实现已实质性改善 (旧版完全不
        # 杀孙进程).
        if exec_job_handle:
            try:
                win_job.assign_process_by_pid(exec_job_handle, pid)
            except Exception as exc:  # noqa: BLE001
                _push_log("WARNING", f"exec child 加入 ephemeral Job 失败: {exc}")
        _push_log("INFO",
                   f"exec child 已启动: pid={pid} cmd={command[:3] if command else []!r} "
                   f"workdir={workdir} timeout_s={header.get('timeout')}")
        # runner 不再需要 child 端的写端/读端副本.
        kernel32.CloseHandle(child_in_read)
        kernel32.CloseHandle(child_out_write)
        # 透传 stdin (若有).
        if stdin_bytes:
            import msvcrt  # type: ignore[import-not-found]
            in_write_fd = msvcrt.open_osfhandle(
                int(child_in_write.value), os.O_WRONLY | os.O_BINARY,
            )
            with os.fdopen(in_write_fd, "wb") as in_wf:
                in_wf.write(stdin_bytes)
            # 写完关闭写端让 child 读到 EOF.
            kernel32.CloseHandle(child_in_write)
        else:
            kernel32.CloseHandle(child_in_write)
        # 后台 drain stdout pipe + 主线程 wait 进程 (并行, 防 pipe 写满死锁).
        # 旧版先 wait 再读, child 写满 64KB pipe 后阻塞在 write, runner 在 wait → 死锁.
        import msvcrt  # type: ignore[import-not-found]
        read_fd = msvcrt.open_osfhandle(
            int(child_out_read.value), os.O_RDONLY | os.O_BINARY,
        )
        out_buf = bytearray()
        _drain_exc: list = []

        def _drain_pipe() -> None:
            """后台线程: 持续读 stdout pipe 直到 EOF 或出错. 超过 MAX_STDOUT_BYTES 截断."""
            try:
                while True:
                    try:
                        chunk = os.read(read_fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    out_buf.extend(chunk)
                    if len(out_buf) > MAX_STDOUT_BYTES:
                        del out_buf[MAX_STDOUT_BYTES:]
                        break
            except Exception as exc:  # noqa: BLE001
                _drain_exc.append(exc)

        _drain_thread = _threading.Thread(target=_drain_pipe, name="stdout-drain", daemon=True)
        _drain_thread.start()

        wait_timeout = 500  # noqa: N806 - Win32 SDK 常量风格
        deadline_waited_ms = 0
        _caller_timeout_s = header.get("timeout")
        try:
            _caller_timeout_s = int(_caller_timeout_s) if _caller_timeout_s is not None else 120
        except (TypeError, ValueError):
            _caller_timeout_s = 120
        if _caller_timeout_s <= 0:
            _caller_timeout_s = 120
        wait_budgets = _caller_timeout_s * 1000  # noqa: N806 - Win32 SDK 常量风格
        _child_killed = False  # child 是否被超时强杀 (强杀后 stdout pipe 可能不 EOF)
        _last_heartbeat_ms = 0
        while True:
            result = kernel32.WaitForSingleObject(
                wintypes.HANDLE(proc_handle), wait_timeout,
            )
            if result == 0:  # wait_obj_0: child 已退出
                break
            deadline_waited_ms += wait_timeout
            # 每 30s 打一次心跳, 记录 child 仍在跑.
            if deadline_waited_ms - _last_heartbeat_ms >= 30000:
                _last_heartbeat_ms = deadline_waited_ms
                _push_log("INFO",
                          f"exec child 等待中: pid={pid} waited={deadline_waited_ms}ms/"
                          f"{wait_budgets}ms stdout_buf={len(out_buf)}B "
                          f"cmd={command[:3] if command else []!r}")
            if deadline_waited_ms >= wait_budgets:
                # child 长时间不退出, 强杀整个进程树. review #3: 优先 close_job
                # 触发 KILL_ON_JOB_CLOSE 杀 child + 所有孙进程 (旧版只 TerminateProcess
                # 杀 child, 孙进程继续占用端口/资源). Job 失败兜底 TerminateProcess.
                if exec_job_handle:
                    try:
                        win_job.close_job(exec_job_handle)
                        exec_job_handle = 0
                    except Exception:  # noqa: BLE001
                        _push_log("WARNING", "close_job 失败, 回退 TerminateProcess", exc_info=True)
                        kernel32.TerminateProcess(wintypes.HANDLE(proc_handle), 1)
                else:
                    kernel32.TerminateProcess(wintypes.HANDLE(proc_handle), 1)
                _child_killed = True
                _push_log("WARNING",
                          f"exec child 超时未退出 ({wait_budgets}ms) 强杀, "
                          f"cmd={command[:3] if command else []!r}")
                break
        # 进程已退出 (正常或强杀). 等 drain 线程结束 (最多 5s, 防孙进程持写端不 EOF).
        _drain_thread.join(timeout=5.0)
        if _drain_thread.is_alive():
            # drain 还活着 = pipe 不 EOF (孙进程仍持写端). 放弃读剩余, 关 fd 强制 drain 退出.
            _push_log("DEBUG",
                      f"exec stdout drain 超时未结束 (孙进程持 pipe 写端), "
                      f"cmd={command[:3] if command else []!r}")
            try:
                os.close(read_fd)
            except OSError:
                pass
        else:
            # drain 正常结束 (EOF), 统一关 fd 防泄漏.
            try:
                os.close(read_fd)
            except OSError:
                pass
        if _drain_exc:
            _push_log("WARNING", f"exec stdout drain 线程异常: {_drain_exc[0]}")
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(
            wintypes.HANDLE(proc_handle), ctypes.byref(exit_code),
        )
        kernel32.CloseHandle(wintypes.HANDLE(proc_handle))
        # review #3: child 已退出, 关闭 ephemeral Job 杀掉残留孙进程 (child 起的
        # node server / playwright 进程等), 释放端口/资源, 防 EADDRINUSE + 句柄泄漏.
        if exec_job_handle:
            try:
                win_job.close_job(exec_job_handle)
            except Exception:  # noqa: BLE001
                _push_log("DEBUG", "正常退出后 close_job 失败 (best-effort)", exc_info=True)
            exec_job_handle = 0
        ec = int(exit_code.value)
        out_text = _decode_child_output(out_buf)
        _push_log("INFO",
                   f"exec child 结束: pid={pid} exit_code={ec} killed={_child_killed} "
                   f"stdout_len={len(out_text)} cmd={command[:3] if command else []!r}")
        _send_response(stream, {
            "ok": True,
            "exit_code": ec,
            "stdout": out_text,
            "stderr": "",
            "killed": _child_killed,  # P2-28: 回传超时强杀标志
        })
        # 上报 exec 结果: command 摘要 + exit + 输出前缀经日志长连发回 box-server. 输出截断防撑爆日志帧.
        cmd_summary = " ".join(str(c) for c in (header.get("command") or []))[:200]
        # 失败含 EPERM 时取更长 preview (含完整错误路径).
        if ec != 0 and "EPERM" in out_text:
            out_preview = out_text[:4000]
        else:
            out_preview = out_text[:512]
        if ec != 0:
            # 失败时把完整 stdout 落盘到 workspace (.exec_failed.log), 供宿主机直接读.
            if workspace:
                try:
                    _failed_log = os.path.join(str(workspace), ".exec_failed.log")
                    with open(_failed_log, "w", encoding="utf-8", errors="replace") as _fh:
                        _fh.write(f"cmd: {cmd_summary}\nexit: {ec}\n")
                        _fh.write(f"stdout_len: {len(out_text)}\n")
                        _fh.write("---- stdout ----\n")
                        _fh.write(out_text)
                except OSError:
                    pass
            _push_log("WARNING",
                      f"exec 失败 exit={ec} cmd={cmd_summary!r} "
                      f"stdout_len={len(out_text)} stdout_preview={out_preview!r}")
        else:
            _push_log("INFO",
                      f"exec 成功 exit=0 cmd={cmd_summary!r} "
                      f"stdout_len={len(out_text)}")
    except Exception as exc:  # noqa: BLE001
        import traceback as _tb
        tb = _tb.format_exc()
        # 连接已断时发 error response 会再抛 ConnectionAbortedError, 兜底吞掉 — 不让单个 exec 连接异常杀整个 runner (见上方 accept 循环注释).
        try:
            _send_error_response(stream, f"exec failed: {exc}")
        except OSError:
            pass
        _push_log("ERROR", f"exec 处理异常: {exc}", exc=tb)
        try:
            kernel32.CloseHandle(child_out_write)
            kernel32.CloseHandle(child_out_read)
            kernel32.CloseHandle(child_in_write)
        except Exception:  # noqa: BLE001 - 清理句柄兜底, 不抛
            pass
        # review #3: 异常时也关 ephemeral Job 杀残留孙进程 (best-effort).
        if exec_job_handle:
            try:
                win_job.close_job(exec_job_handle)
            except Exception:  # noqa: BLE001
                pass
            exec_job_handle = 0


def _handle_write_file_request(stream, header, stdin) -> None:
    path = header.get("path", "")
    size = int(header.get("content_size", 0))
    mkdir_parents = bool(header.get("mkdir_parents", True))
    mode = header.get("mode")
    try:
        content = recv_frame(stdin, MAX_FILE_BYTES) if size > 0 else b""
        if size and len(content) != size:
            raise ConnectionError(
                f"write_file content size mismatch: got {len(content)} want {size}"
            )
        if mkdir_parents:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
            if mode is not None:
                os.chmod(path, int(str(mode), 8) if isinstance(mode, str) else mode)
        _send_response(stream, {"ok": True})
    except OSError as exc:
        _send_response(stream, {
            "ok": False, "error": "io_error", "errno": exc.errno,
            "stderr": str(exc),
        })
    except Exception as exc:  # noqa: BLE001
        _send_error_response(stream, str(exc))


def _handle_read_file_request(stream, header) -> None:
    path = header.get("path", "")
    try:
        with open(path, "rb") as fh:
            content = fh.read(MAX_FILE_BYTES)
        _send_response(stream, {
            "ok": True, "content_size": len(content),
        })
        send_frame(stream, content)
    except OSError as exc:
        _send_response(stream, {
            "ok": False, "error": "io_error", "errno": exc.errno,
            "stderr": str(exc),
        })
    except Exception as exc:  # noqa: BLE001
        _send_error_response(stream, str(exc))


def _handle_list_dir_request(stream, header) -> None:
    path = header.get("path", "")
    recursive = bool(header.get("recursive", False))
    include_files = bool(header.get("include_files", True))
    include_dirs = bool(header.get("include_dirs", True))
    try:
        items: list[dict] = []
        if recursive:
            for root, dirs, files in os.walk(path):
                if include_dirs:
                    for d in dirs:
                        items.append({"type": "dir", "name": d, "path": os.path.join(root, d)})
                if include_files:
                    for f in files:
                        items.append({"type": "file", "name": f, "path": os.path.join(root, f)})
        else:
            for entry in os.listdir(path):
                full = os.path.join(path, entry)
                is_dir = os.path.isdir(full)
                if is_dir and include_dirs:
                    items.append({"type": "dir", "name": entry, "path": full})
                elif not is_dir and include_files:
                    items.append({"type": "file", "name": entry, "path": full})
        _send_response(stream, {"ok": True, "items": items})
    except OSError as exc:
        _send_response(stream, {
            "ok": False, "error": "io_error", "errno": exc.errno,
            "stderr": str(exc),
        })
    except Exception as exc:  # noqa: BLE001
        _send_error_response(stream, str(exc))


if __name__ == "__main__":  # pragma: no cover - runner 入口
    sys.argv.pop(0)  # 去掉脚本名
    if sys.argv and sys.argv[0] == RUNNER_SUBCOMMAND:
        sys.argv.pop(0)
        raise SystemExit(runner_main(sys.argv))
