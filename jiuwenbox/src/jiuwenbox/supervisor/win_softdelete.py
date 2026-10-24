# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows 软删除 DLL 的路径、沙箱 ACL 和挂起进程注入.

DLL 本体在普通 Windows 进程里用 Detours 挂钩删除和创建进程。
Git Bash 不注入；它拉起的 rm 等仍由 DLL 的调试循环装入，并使用手写钩子。
本模块负责:
- 找到 ``jiuwen_softdelete.dll``
- 给 jbx-sandbox 读和执行权限
- 在 CREATE_SUSPENDED 的 runner 上改导入表，让加载器在主线程起来前映射 DLL

子进程由 DLL 里的 CreateProcessInternalW 钩子继续装, 这里不按每次删除注入.
"""

from __future__ import annotations

import ctypes
import logging
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
_PATCH_ONLY = "JIUWENBOX_SOFT_DELETE_PATCH_ONLY"
logger = logging.getLogger(__name__)


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


def inject_suspended(process_handle: int) -> None:
    """往仍处于挂起状态的进程改导入表，让加载器映射软删除 DLL.

    不恢复主线程. 导入表改失败或目标是 32 位进程时直接返回,
    调用方仍按原方式 ResumeThread.
    """
    if sys.platform != "win32":
        logger.error("软删除注入被跳过: 非 Windows")
        return
    dll = dll_path()
    if not dll.is_file():
        raise FileNotFoundError(f"soft-delete DLL not found: {dll}")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    kernel.LoadLibraryW.argtypes = [wintypes.LPCWSTR]
    kernel.LoadLibraryW.restype = wintypes.HMODULE
    kernel.FreeLibrary.argtypes = [wintypes.HMODULE]
    kernel.FreeLibrary.restype = wintypes.BOOL
    kernel.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
    kernel.GetProcAddress.restype = ctypes.c_void_p
    kernel.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel.IsWow64Process.restype = wintypes.BOOL
    process = wintypes.HANDLE(process_handle)
    wow = wintypes.BOOL()
    if kernel.IsWow64Process(process, ctypes.byref(wow)) and wow.value:
        logger.warning(
            "软删除跳过 32 位进程, 进程将不带软删除继续启动 dll=%s", dll,
        )
        return
    already = kernel.GetModuleHandleW(_DLL_NAME)
    loaded_here = None
    if not already:
        os.environ[_PATCH_ONLY] = "1"
        try:
            loaded_here = kernel.LoadLibraryW(str(dll))
        finally:
            os.environ.pop(_PATCH_ONLY, None)
        if not loaded_here:
            err = ctypes.get_last_error()
            logger.error(
                "软删除 DLL 无法在监督进程加载, 子进程将不带软删除启动 winerror=%s dll=%s",
                err, dll,
            )
            raise ctypes.WinError(err)
    module = already or loaded_here
    try:
        addr = kernel.GetProcAddress(module, b"jiuwen_softdelete_patch_process")
        if not addr:
            logger.error("软删除 DLL 缺少 jiuwen_softdelete_patch_process, dll=%s", dll)
            raise RuntimeError("jiuwen_softdelete_patch_process is missing")
        patch_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HANDLE, use_last_error=True)
        patch = patch_type(addr)
        if not patch(process):
            err = ctypes.get_last_error()
            logger.error(
                "软删除导入表注入失败, 子进程将不带软删除启动 winerror=%s dll=%s",
                err, dll,
            )
            raise RuntimeError(
                f"soft-delete import patch failed (winerror={err}, dll={dll})"
            )
    finally:
        if loaded_here:
            kernel.FreeLibrary(loaded_here)
