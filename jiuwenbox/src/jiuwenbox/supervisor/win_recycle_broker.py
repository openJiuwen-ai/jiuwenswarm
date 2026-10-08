# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""box-server 上的回收站转发.

沙箱进程跑在 jbx-sandbox 名下, 不能把文件放进当前登录用户看得见的回收站.
DLL 拦住删除后, 把路径发给本服务. 本服务用 box-server 自己的身份调用
回收站接口. 回收没有确认成功时返回失败, 调用方必须留下原文件.

监听端口必须落在 WFP 已经放行的回环端口里. 沙箱出站只允许连
127.0.0.1 上的代理端口范围, 随机端口会被直接拒绝, 删除就会停在原地.
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import struct
import sys
import threading
from collections.abc import Callable

from jiuwenbox.supervisor.win_softdelete import (
    ENV_RECYCLE_PORT,
    ENV_RECYCLE_TOKEN,
    enabled_from_environ,
)

logger = logging.getLogger(__name__)

_FRAME_MAX = 256 * 1024
_TOKEN_MAX = 256
_PATH_MAX = 48 * 1024
_OK = b"ok"
_NO = b"no"

_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOCONFIRMMKDIR = 0x0200
_FOF_NOERRORUI = 0x0400

Recycler = Callable[[str], bool]

_broker: RecycleBroker | None = None
_broker_lock = threading.Lock()


def _norm_key(path: str) -> str:
    text = path
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.normpath(text))


def _has_recycle_component(path: str) -> bool:
    return any(part.lower() == "$recycle.bin" for part in _norm_key(path).split(os.sep))


def _root_variants(path: str) -> list[str]:
    if not path or "\x00" in path:
        return []
    raw = os.path.normpath(os.path.abspath(path))
    if not os.path.isabs(raw) or not os.path.isdir(raw):
        return []
    found = [raw]
    real = os.path.normpath(os.path.realpath(raw))
    if _norm_key(real) != _norm_key(raw) and os.path.isdir(real):
        found.append(real)
    return found


def path_allowed(path: str) -> str | None:
    """返回可以回收的真实路径.

    沙箱钩子拦下的删除不再按工作目录收口. 临时目录里的文件也能进回收站.
    相对路径、回收站自身、不存在的路径和符号链接仍然拒绝.
    """
    if not path or "\x00" in path or not os.path.isabs(path):
        return None
    if _has_recycle_component(path):
        return None
    normalized = os.path.normpath(os.path.abspath(path))
    if not os.path.lexists(normalized) or os.path.islink(normalized):
        return None
    real = os.path.normpath(os.path.realpath(normalized))
    if not os.path.exists(real):
        return None
    return real


def recycle_info_original_path(data: bytes) -> str | None:
    """从回收站 ``$I`` 文件 (version 2) 读出原始路径."""
    if len(data) < 30:
        return None
    version = struct.unpack_from("<Q", data, 0)[0]
    if version != 2:
        return None
    chars = struct.unpack_from("<I", data, 24)[0]
    if chars <= 1 or chars > _PATH_MAX:
        return None
    blob = data[28:28 + chars * 2]
    text = blob.decode("utf-16-le", errors="ignore").split("\x00", 1)[0]
    if len(text) < 3 or not os.path.isabs(text):
        return None
    return text


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = conn.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("recycle broker got a short read")
        buf += chunk
    return bytes(buf)


def _read_frame(conn: socket.socket) -> bytes:
    raw_len = _recv_exact(conn, 4)
    length = struct.unpack(">I", raw_len)[0]
    if length == 0 or length > _FRAME_MAX:
        raise ValueError(f"recycle frame length {length} is out of range")
    return _recv_exact(conn, length)


def _write_frame(conn: socket.socket, payload: bytes) -> None:
    conn.sendall(struct.pack(">I", len(payload)) + payload)


def _parse_request(payload: bytes) -> tuple[str, str] | None:
    if len(payload) < 8:
        return None
    token_len = struct.unpack_from(">I", payload, 0)[0]
    if token_len == 0 or token_len > _TOKEN_MAX:
        return None
    token_end = 4 + token_len
    if token_end + 4 > len(payload):
        return None
    path_len = struct.unpack_from(">I", payload, token_end)[0]
    if path_len == 0 or path_len > _PATH_MAX:
        return None
    path_end = token_end + 4 + path_len
    if path_end != len(payload):
        return None
    try:
        token = payload[4:token_end].decode("utf-8")
        path = payload[token_end + 4:path_end].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in token or "\x00" in path:
        return None
    return token, path


def _set_hook_bypass(on: bool) -> None:
    """已装入本进程的软删除 DLL 在这条线程上放行, 避免回收调用再次进钩子."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    kernel.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
    kernel.GetProcAddress.restype = ctypes.c_void_p
    module = kernel.GetModuleHandleW("jiuwen_softdelete.dll")
    if not module:
        return
    addr = kernel.GetProcAddress(module, b"jiuwen_softdelete_set_bypass")
    if not addr:
        return
    ctypes.CFUNCTYPE(None, ctypes.c_int)(addr)(1 if on else 0)


def _current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p),
    ]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD(0)
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(
            token, 1, buf, needed.value, ctypes.byref(needed),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        text = ctypes.c_wchar_p()
        if not advapi.ConvertSidToStringSidW(sid_ptr, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value or ""
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def _recycle_bin_dir(path: str, sid: str) -> str | None:
    drive = os.path.splitdrive(path)[0]
    if len(drive) != 2 or drive[1] != ":":
        return None
    return drive + "\\$Recycle.Bin\\" + sid


def _info_snapshot(bin_dir: str) -> dict[str, int]:
    found: dict[str, int] = {}
    if not os.path.isdir(bin_dir):
        return found
    with os.scandir(bin_dir) as entries:
        for entry in entries:
            if not entry.name.startswith("$I"):
                continue
            try:
                found[entry.name] = entry.stat().st_size
            except OSError:
                continue
    return found


def _new_info_names(before: dict[str, int], after: dict[str, int]) -> list[str]:
    names: list[str] = []
    for name, size in after.items():
        if name not in before or before[name] != size:
            names.append(name)
    return names


def _info_matches(bin_dir: str, names: list[str], wanted: str) -> bool:
    wanted_key = _norm_key(wanted)
    for name in names:
        try:
            with open(os.path.join(bin_dir, name), "rb") as handle:
                recorded = recycle_info_original_path(handle.read(65536))
        except OSError:
            continue
        if recorded is not None and _norm_key(recorded) == wanted_key:
            return True
    return False


def _shell_delete(path: str) -> bool:
    import ctypes
    from ctypes import wintypes

    class _FileOp(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHFileOperationW.argtypes = [ctypes.POINTER(_FileOp)]
    shell.SHFileOperationW.restype = ctypes.c_int
    source = ctypes.create_unicode_buffer(path + "\0")
    op = _FileOp()
    op.wFunc = _FO_DELETE
    op.pFrom = ctypes.cast(source, wintypes.LPCWSTR)
    op.fFlags = (
        _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT
        | _FOF_NOERRORUI | _FOF_NOCONFIRMMKDIR
    )
    _set_hook_bypass(True)
    try:
        rc = shell.SHFileOperationW(ctypes.byref(op))
    finally:
        _set_hook_bypass(False)
    if rc != 0 or op.fAnyOperationsAborted:
        logger.warning("SHFileOperation recycle failed rc=%s path=%s", rc, path)
        return False
    return not os.path.exists(path)


def recycle_with_shell(path: str) -> bool:
    """用当前进程身份回收 ``path``. 源文件消失且回收站有对应记录才算成功."""
    if sys.platform != "win32":
        return False
    try:
        sid = _current_user_sid()
    except OSError:
        logger.warning("recycle broker could not read the process SID", exc_info=True)
        return False
    bin_dir = _recycle_bin_dir(path, sid)
    if bin_dir is None:
        return False
    try:
        before = _info_snapshot(bin_dir)
    except OSError:
        logger.warning("recycle broker could not read %s", bin_dir, exc_info=True)
        return False
    if not _shell_delete(path):
        return False
    if os.path.exists(path):
        return False
    try:
        after = _info_snapshot(bin_dir)
    except OSError:
        logger.warning("recycle broker could not re-read %s", bin_dir, exc_info=True)
        return False
    if _info_matches(bin_dir, _new_info_names(before, after), path):
        return True
    logger.warning(
        "recycle call removed %s but no matching recycle-bin entry was found", path,
    )
    return False


class RecycleBroker:
    """本机回环上的回收请求. 一个沙箱一把令牌, 令牌有效就回收它报来的文件."""

    def __init__(self, recycler: Recycler | None = None) -> None:
        self._recycler = recycler or recycle_with_shell
        self._lock = threading.Lock()
        self._shell_lock = threading.Lock()
        self._grants: set[str] = set()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self.port = 0

    @property
    def running(self) -> bool:
        return self._sock is not None and not self._stopped.is_set()

    def start(self, ports: list[int] | None = None) -> int:
        """监听 ``127.0.0.1``. ``ports`` 按顺序尝试, 省略时由系统分配.

        沙箱里要传 WFP 放行范围内、且不是代理端口的候选. 重复调用返回已有端口.
        """
        candidates = list(ports) if ports else [0]
        if not candidates:
            raise RuntimeError("回收站转发没有可监听的放行端口")
        with self._lock:
            if self.running:
                return self.port
            self._stopped.clear()
            sock: socket.socket | None = None
            last_error: OSError | None = None
            for port in candidates:
                trial = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    trial.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    trial.bind(("127.0.0.1", port))
                    trial.listen(16)
                except OSError as exc:
                    last_error = exc
                    trial.close()
                    continue
                sock = trial
                break
            if sock is None:
                raise RuntimeError("回收站转发未能绑定放行端口") from last_error
            self.port = int(sock.getsockname()[1])
            self._sock = sock
            self._thread = threading.Thread(
                target=self._serve, name="jiuwen-recycle-broker", daemon=True,
            )
            self._thread.start()
        logger.info("recycle broker listening on 127.0.0.1:%s", self.port)
        return self.port

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            sock = self._sock
            self._sock = None
            thread = self._thread
            self._thread = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def register(self, roots: list[str]) -> str:
        """确认沙箱至少有一个真实目录, 然后发令牌.

        令牌只用来认出是哪一个沙箱. 之后这个沙箱报来的删除不再按目录收口.
        """
        if not any(_root_variants(root or "") for root in roots):
            raise RuntimeError("回收站转发没有可写根目录, 无法登记沙箱")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._grants.add(token)
        return token

    def unregister(self, token: str) -> None:
        with self._lock:
            self._grants.discard(token)

    def handle_payload(self, payload: bytes) -> bytes:
        """处理一帧请求. 只有回收被确认后才返回 ``ok``."""
        try:
            if self._accept(payload):
                return _OK
        except Exception:  # noqa: BLE001
            logger.exception("recycle request failed")
        return _NO

    def _accept(self, payload: bytes) -> bool:
        parsed = _parse_request(payload)
        if parsed is None:
            return False
        token, path = parsed
        with self._lock:
            known = token in self._grants
        if not known:
            logger.warning("recycle broker rejected an unknown token")
            return False
        real = path_allowed(path)
        if real is None:
            logger.warning("recycle broker rejected path %s", path)
            return False
        with self._shell_lock:
            return bool(self._recycler(real))

    def _serve(self) -> None:
        while not self._stopped.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle_conn, args=(conn,), name="jiuwen-recycle-conn",
                daemon=True,
            ).start()

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(15)
            payload = _read_frame(conn)
            _write_frame(conn, self.handle_payload(payload))
        except (OSError, ValueError):
            logger.debug("recycle connection closed", exc_info=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass


def permitted_loopback_ports(port_start: int, port_end: int) -> list[int]:
    """返回沙箱能连上的回环端口, 去掉代理自己占用的起始端口.

    WFP 放行 ``[port_start, port_end]``. 出站代理只绑定 ``port_start``,
    其余端口从高到低留给回收转发.
    """
    if port_end <= port_start:
        return []
    return list(range(port_end, port_start, -1))


def get_broker() -> RecycleBroker | None:
    return _broker


def start_broker(ports: list[int] | None = None) -> RecycleBroker:
    """启动进程内唯一的转发服务."""
    global _broker
    with _broker_lock:
        if _broker is not None and _broker.running:
            return _broker
        broker = RecycleBroker()
        broker.start(ports)
        _broker = broker
        return broker


def stop_broker() -> None:
    global _broker
    with _broker_lock:
        broker = _broker
        _broker = None
    if broker is not None:
        broker.stop()


def prepare_runner_recycle(env: dict[str, str], roots: list[str]) -> str | None:
    """软删除开启时登记根目录, 并把端口和令牌写进 runner 环境.

    服务没起来时直接失败, 避免沙箱退回在 jbx-sandbox 身份里调用回收站.
    """
    if not enabled_from_environ():
        return None
    broker = get_broker()
    if broker is None or not broker.running:
        raise RuntimeError(
            "软删除已开启, 但回收站转发服务没有在监听"
        )
    token = broker.register(roots)
    env[ENV_RECYCLE_PORT] = str(broker.port)
    env[ENV_RECYCLE_TOKEN] = token
    return token


def detach_runner_recycle(token: str | None) -> None:
    if not token:
        return
    broker = get_broker()
    if broker is not None:
        broker.unregister(token)
