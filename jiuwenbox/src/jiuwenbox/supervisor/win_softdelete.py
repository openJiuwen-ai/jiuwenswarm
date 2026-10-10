# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows 软删除 DLL 的路径、沙箱 ACL 和挂起进程注入.

DLL 本体在进程里挂钩 ntdll。本模块只负责:
- 找到 ``jiuwen_softdelete.dll``
- 给 jbx-sandbox 读和执行权限
- 在 CREATE_SUSPENDED 的 runner 里装入一次

子进程由 DLL 里的 CreateProcessInternalW 钩子继续装, 这里不按每次删除注入.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

ENV_ENABLED = "JIUWENBOX_SOFT_DELETE"
ENV_ARCHIVE = "JIUWENBOX_SOFT_DELETE_ARCHIVE"
ENV_DLL = "JIUWENBOX_SOFT_DELETE_DLL"
ENV_RECYCLE_PORT = "JIUWENBOX_SOFT_DELETE_RECYCLE_PORT"
ENV_RECYCLE_TOKEN = "JIUWENBOX_SOFT_DELETE_RECYCLE_TOKEN"
_DLL_NAME = "jiuwen_softdelete.dll"

_LOADER_PATH_OFF = 0x100
_LOADER_RESULT_OFF = 0x80
_LOADER_ERROR_OFF = 0x88
_MEM_COMMIT = 0x1000
_MEM_RESERVE = 0x2000
_PAGE_EXECUTE_READWRITE = 0x40
_WAIT_OBJECT_0 = 0


def enabled_from_environ() -> bool:
    """只有显式的 ``1`` 才打开. 字符串 true 或其他值都保持关闭."""
    return os.environ.get(ENV_ENABLED) == "1"


def _frozen_dll_candidates(meipass: str) -> list[Path]:
    """冻结包里的查找顺序. 新包在 jiuwenbox_native, 旧包在改名后的目录."""
    root = Path(meipass)
    return [
        root / "jiuwenbox_native" / "native" / _DLL_NAME,
        root / "jiuwenbox_legacy_overlay" / "native" / _DLL_NAME,
    ]


def dll_path() -> Path:
    """返回软删除 DLL 路径。

    环境变量优先。开发布局用本包 ``native`` 目录。
    冻结包不能把 DLL 留在 ``jiuwenbox`` 目录里, 那个目录会盖住 PYZ 里的同名包,
    打包脚本把它放到 ``jiuwenbox_native/native``。
    ``jiuwenbox_legacy_overlay`` 只留给已经把整个目录改名的旧安装包。
    """
    raw = (os.environ.get(ENV_DLL) or "").strip()
    if raw:
        return Path(raw)
    packaged = Path(__file__).resolve().parents[1] / "native" / _DLL_NAME
    if packaged.is_file():
        return packaged
    meipass = getattr(sys, "_MEIPASS", None)
    frozen = _frozen_dll_candidates(meipass) if meipass else []
    for candidate in frozen:
        if candidate.is_file():
            return candidate
    if frozen:
        return frozen[0]
    return packaged


def merge_soft_delete_env(env: dict[str, str]) -> None:
    """把 runner 里的软删除变量补进子进程环境.

    CreateProcessAsUserW 使用调用方拼出来的环境块, 不会自动继承 runner
    的环境. 缺了归档目录或回收站转发端口, 子进程里的 DLL 就无法按
    box-server 的身份回收文件.
    """
    for key in (ENV_ENABLED, ENV_ARCHIVE, ENV_DLL, ENV_RECYCLE_PORT, ENV_RECYCLE_TOKEN):
        value = os.environ.get(key)
        if value:
            env.setdefault(key, value)


def prepare_dll_for_sandbox_user() -> Path:
    """让 jbx-sandbox 能 LoadLibrary 这支 DLL. 失败直接抛出."""
    if sys.platform != "win32":
        raise RuntimeError("soft-delete DLL injection is Windows-only")
    from jiuwenbox.supervisor import win_acl, win_constants, win_setup

    dll = dll_path()
    if not dll.is_file():
        raise FileNotFoundError(f"soft-delete DLL not found: {dll}")
    sid = win_setup.get_sandbox_user_sid()
    if not sid:
        raise RuntimeError("jbx-sandbox SID is missing; cannot grant DLL access")
    win_acl.grant_parent_traverse(str(dll), sid)
    win_acl.grant_ace(
        str(dll),
        sid,
        rights=win_constants.ALLOW_READ_EXECUTE_RIGHTS,
        mode="ALLOW",
        recursive=False,
    )
    return dll


def _patch_lea(code: bytearray, at: int, target: int) -> None:
    disp = target - (at + 7)
    code[at + 3:at + 7] = int(disp).to_bytes(4, "little", signed=True)


def build_loader_shellcode(load_library: int, get_last_error: int) -> bytes:
    """生成远程 LoadLibraryW 桩, 把 64 位模块句柄写回同一页.

    布局与 softdelete.c 的 build_loader 一致: 代码在页首, 结果在 0x80,
    错误码在 0x88, DLL 路径 UTF-16 在 0x100.
    """
    code = bytearray()
    code += b"\x48\x83\xEC\x28"
    lea_path = len(code)
    code += b"\x48\x8D\x0D\x00\x00\x00\x00"
    code += b"\x48\xB8" + int(load_library).to_bytes(8, "little")
    code += b"\xFF\xD0"
    lea_res = len(code)
    code += b"\x48\x8D\x0D\x00\x00\x00\x00"
    code += b"\x48\x89\x01"
    code += b"\x48\x85\xC0"
    jnz_at = len(code)
    code += b"\x75\x00"
    code += b"\x48\xB8" + int(get_last_error).to_bytes(8, "little")
    code += b"\xFF\xD0"
    lea_err = len(code)
    code += b"\x48\x8D\x0D\x00\x00\x00\x00"
    code += b"\x89\x01"
    done = len(code)
    code[jnz_at + 1] = (done - (jnz_at + 2)) & 0xFF
    code += b"\x31\xC0\x48\x83\xC4\x28\xC3"
    _patch_lea(code, lea_path, _LOADER_PATH_OFF)
    _patch_lea(code, lea_res, _LOADER_RESULT_OFF)
    _patch_lea(code, lea_err, _LOADER_ERROR_OFF)
    return bytes(code)


def _kernel32() -> ctypes.WinDLL:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    kernel.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    kernel.GetProcAddress.restype = ctypes.c_void_p
    kernel.VirtualAllocEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
        wintypes.DWORD, wintypes.DWORD,
    ]
    kernel.VirtualAllocEx.restype = ctypes.c_void_p
    kernel.WriteProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.WriteProcessMemory.restype = wintypes.BOOL
    kernel.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel.ReadProcessMemory.restype = wintypes.BOOL
    kernel.CreateRemoteThread.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.CreateRemoteThread.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.VirtualFreeEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
    ]
    kernel.VirtualFreeEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.IsWow64Process.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL),
    ]
    kernel.IsWow64Process.restype = wintypes.BOOL
    return kernel


def inject_suspended(process_handle: int) -> None:
    """往仍处于挂起状态的进程装入软删除 DLL.

    不恢复主线程. 调用方在注入成功后再 ResumeThread.
    32 位子进程装不了这支 64 位 DLL, 直接失败, 避免无保护地跑起来.
    """
    if sys.platform != "win32":
        raise RuntimeError("soft-delete DLL injection is Windows-only")
    dll = dll_path()
    if not dll.is_file():
        raise FileNotFoundError(f"soft-delete DLL not found: {dll}")
    kernel = _kernel32()
    process = wintypes.HANDLE(process_handle)
    wow = wintypes.BOOL()
    if kernel.IsWow64Process(process, ctypes.byref(wow)) and wow.value:
        raise RuntimeError(f"refusing to start a 32-bit process without soft-delete: {dll}")
    k32 = kernel.GetModuleHandleW("kernel32.dll")
    load_library = kernel.GetProcAddress(k32, b"LoadLibraryW")
    get_last_error = kernel.GetProcAddress(k32, b"GetLastError")
    if not load_library or not get_last_error:
        raise RuntimeError("kernel32!LoadLibraryW is unavailable")
    code = build_loader_shellcode(int(load_library), int(get_last_error))
    remote = kernel.VirtualAllocEx(
        process, None, 8192, _MEM_COMMIT | _MEM_RESERVE, _PAGE_EXECUTE_READWRITE,
    )
    if not remote:
        raise ctypes.WinError(ctypes.get_last_error())
    path_bytes = str(dll).encode("utf-16-le") + b"\x00\x00"
    if _LOADER_PATH_OFF + len(path_bytes) > 8192:
        kernel.VirtualFreeEx(process, remote, 0, 0x8000)
        raise RuntimeError(f"soft-delete DLL path is too long: {dll}")
    written = ctypes.c_size_t()
    code_buf = ctypes.create_string_buffer(code)
    path_buf = ctypes.create_string_buffer(path_bytes)
    wrote_code = kernel.WriteProcessMemory(
        process, remote, code_buf, len(code), ctypes.byref(written),
    )
    wrote_path = kernel.WriteProcessMemory(
        process, remote + _LOADER_PATH_OFF, path_buf, len(path_bytes), ctypes.byref(written),
    )
    if not wrote_code or not wrote_path:
        err = ctypes.get_last_error()
        kernel.VirtualFreeEx(process, remote, 0, 0x8000)
        raise ctypes.WinError(err)
    thread = kernel.CreateRemoteThread(process, None, 0, remote, None, 0, None)
    if not thread:
        err = ctypes.get_last_error()
        kernel.VirtualFreeEx(process, remote, 0, 0x8000)
        raise ctypes.WinError(err)
    try:
        wait_rc = kernel.WaitForSingleObject(thread, 15000)
        if wait_rc != _WAIT_OBJECT_0:
            raise RuntimeError(f"soft-delete DLL load timed out (wait={wait_rc})")
        result = ctypes.c_uint64()
        remote_error = ctypes.c_uint32()
        kernel.ReadProcessMemory(
            process, remote + _LOADER_RESULT_OFF, ctypes.byref(result),
            ctypes.sizeof(result), ctypes.byref(written),
        )
        kernel.ReadProcessMemory(
            process, remote + _LOADER_ERROR_OFF, ctypes.byref(remote_error),
            ctypes.sizeof(remote_error), ctypes.byref(written),
        )
        if result.value == 0:
            raise RuntimeError(
                f"LoadLibraryW failed in suspended process "
                f"(error={remote_error.value}, dll={dll})"
            )
    finally:
        kernel.CloseHandle(thread)
        kernel.VirtualFreeEx(process, remote, 0, 0x8000)
