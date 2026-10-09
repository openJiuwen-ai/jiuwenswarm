# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""box-server 上的回收站转发.

沙箱进程跑在 jbx-sandbox 名下, 不能把文件放进当前登录用户看得见的回收站.
DLL 先在沙箱进程里把相对路径变成完整路径, 再发给本服务. 本服务用
box-server 自己的身份调用回收站接口. 接口成功且原文件消失才返回成功,
调用方必须留下原文件.

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
import time
from collections.abc import Callable

from jiuwenbox.supervisor.win_softdelete import (
    ENV_ARCHIVE,
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


def _is_volume_recycle_bin(path: str) -> bool:
    """只认盘符根上的系统回收站, 工作区里的同名文件夹不算."""
    text = path[4:] if path.startswith("\\\\?\\") else path
    drive, rest = os.path.splitdrive(text)
    if len(drive) != 2 or drive[1] != ":":
        return False
    parts = [part for part in rest.replace("/", "\\").split("\\") if part]
    return bool(parts) and parts[0].lower() == "$recycle.bin"


def _under_archive(path: str) -> bool:
    archive = _norm_key((os.environ.get(ENV_ARCHIVE) or "").strip())
    if not archive:
        return False
    key = _norm_key(path)
    return key == archive or key.startswith(archive + os.sep)


_REPARSE_POINT = 0x400
_INVALID_ATTR = 0xFFFFFFFF


def _file_attrs(path: str) -> int | None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    kernel.GetFileAttributesW.restype = wintypes.DWORD
    value = int(kernel.GetFileAttributesW(path))
    if value == _INVALID_ATTR:
        return None
    return value


def _path_has_reparse(path: str) -> bool:
    """路径自己或任一父目录是连接点/符号链接时返回 True."""
    current = os.path.abspath(path)
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        attrs = _file_attrs(current)
        if attrs is not None and attrs & _REPARSE_POINT:
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent
    return False


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
    """返回可以回收的路径, 不展开连接点.

    沙箱钩子会先把相对路径变成完整路径再发过来. 这里仍拒绝没带盘符的路径,
    因为本服务的当前目录不是那条命令的目录. 系统回收站、归档目录、不存在的
    路径、符号链接和目录连接点仍然拒绝.
    """
    if not path or "\x00" in path or not os.path.isabs(path):
        return None
    normalized = os.path.normpath(os.path.abspath(path))
    if _is_volume_recycle_bin(normalized) or _under_archive(normalized):
        return None
    if not os.path.lexists(normalized) or os.path.islink(normalized):
        return None
    if _path_has_reparse(normalized) or not os.path.exists(normalized):
        return None
    return normalized


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


def _recycle_bin_ready(path: str) -> bool:
    """回收站关掉或这个文件放不下时返回 False, 避免接口把它直接删掉."""
    import ctypes
    import winreg
    from ctypes import wintypes

    try:
        root = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket",
        )
    except OSError:
        return False
    try:
        try:
            nuke, typ = winreg.QueryValueEx(root, "NukeOnDelete")
        except OSError:
            nuke, typ = 0, winreg.REG_DWORD
        if typ == winreg.REG_DWORD and int(nuke) != 0:
            return False
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetVolumePathNameW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ]
        kernel.GetVolumePathNameW.restype = wintypes.BOOL
        kernel.GetVolumeNameForVolumeMountPointW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ]
        kernel.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
        kernel.GetDiskFreeSpaceExW.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(ctypes.c_ulonglong),
        ]
        kernel.GetDiskFreeSpaceExW.restype = wintypes.BOOL
        vol_root = ctypes.create_unicode_buffer(32768)
        vol_name = ctypes.create_unicode_buffer(32768)
        if not kernel.GetVolumePathNameW(path, vol_root, 32768):
            return False
        if not kernel.GetVolumeNameForVolumeMountPointW(vol_root, vol_name, 32768):
            return False
        text = vol_name.value or ""
        brace = text.find("{")
        if brace < 0:
            return False
        subkey = "Volume\\" + text[brace:].rstrip("\\")
        try:
            volume = winreg.OpenKey(root, subkey)
        except OSError:
            return False
        try:
            try:
                vol_nuke, vol_typ = winreg.QueryValueEx(volume, "NukeOnDelete")
            except OSError:
                vol_nuke, vol_typ = 0, winreg.REG_DWORD
            if vol_typ == winreg.REG_DWORD and int(vol_nuke) != 0:
                return False
            try:
                max_mb, max_typ = winreg.QueryValueEx(volume, "MaxCapacity")
            except OSError:
                max_mb, max_typ = None, None
        finally:
            winreg.CloseKey(volume)
        if max_typ == winreg.REG_DWORD:
            budget = int(max_mb) * 1024 * 1024
        else:
            free_bytes = ctypes.c_ulonglong()
            total_bytes = ctypes.c_ulonglong()
            if not kernel.GetDiskFreeSpaceExW(
                vol_root, ctypes.byref(free_bytes), ctypes.byref(total_bytes), None,
            ) or total_bytes.value == 0:
                budget = 64 * 1024 * 1024
            else:
                budget = min(total_bytes.value // 100, 512 * 1024 * 1024)
    finally:
        winreg.CloseKey(root)
    size = _tree_bytes(path)
    if size is None:
        return False
    return size < budget


def _tree_bytes(path: str) -> int | None:
    """文件或目录的总字节数. 数不清或里面有连接点时返回 None."""
    if os.path.isfile(path) and not os.path.islink(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return None

    class _TreeUnread(Exception):
        pass

    def _fail(_err: OSError) -> None:
        raise _TreeUnread

    total = 0
    count = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path, onerror=_fail, followlinks=False):
            for dirname in dirnames:
                attrs = _file_attrs(os.path.join(dirpath, dirname))
                if attrs is not None and attrs & _REPARSE_POINT:
                    return None
            for filename in filenames:
                count += 1
                if count > 100000:
                    return None
                full = os.path.join(dirpath, filename)
                attrs = _file_attrs(full)
                if attrs is not None and attrs & _REPARSE_POINT:
                    return None
                try:
                    total += os.path.getsize(full)
                except OSError:
                    return None
    except _TreeUnread:
        return None
    return total


def _archive_destination(archive: str, path: str) -> str | None:
    """最外层名字后面加到秒的时间. 同一秒重名时再加序号. 目录内部的名字不动."""
    leaf = os.path.basename(path.rstrip("\\/")) or "file"
    safe_chars = []
    for char in leaf[:80]:
        if char in "\\/:*?\"<>|" or ord(char) < 32:
            safe_chars.append("_")
        else:
            safe_chars.append(char)
    safe = "".join(safe_chars) or "file"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for index in range(1000):
        suffix = f"_{stamp}" if index == 0 else f"_{stamp}_{index}"
        dest = os.path.join(archive, safe + suffix)
        if not os.path.lexists(dest):
            return dest
    return None


def _move_to_archive(path: str) -> bool:
    """登录用户把还在原地的文件挪进归档目录. 跨盘不挪, 避免变成复制后删除."""
    archive = (os.environ.get(ENV_ARCHIVE) or "").strip()
    if not archive:
        return False
    if os.path.splitdrive(os.path.abspath(path))[0].casefold() != os.path.splitdrive(
        os.path.abspath(archive),
    )[0].casefold():
        logger.warning("archive skipped: different volume path=%s", path)
        return False
    try:
        os.makedirs(archive, exist_ok=True)
    except OSError:
        logger.warning("archive directory could not be created: %s", archive, exc_info=True)
        return False
    dest = _archive_destination(archive, path)
    if dest is None:
        return False
    try:
        os.rename(path, dest)
    except OSError:
        logger.warning("archive move failed %s -> %s", path, dest, exc_info=True)
        return False
    logger.info("archive %s -> %s", path, dest)
    return True


def recycle_with_shell(path: str) -> bool:
    """用当前登录用户的身份回收 ``path``.

    回收接口成功且原文件不在, 算成功. 回收站不收但文件还在时, 挪到归档目录,
    也算成功. 和本机进程里的软删除同一条路.
    """
    if sys.platform != "win32":
        return False
    # SHFileOperationW without the long-path prefix stops at MAX_PATH.
    shell_ok = len(path) < 260 and _recycle_bin_ready(path) and _shell_delete(path)
    if shell_ok:
        return True
    if os.path.lexists(path):
        return _move_to_archive(path)
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
