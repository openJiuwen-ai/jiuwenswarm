# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows Job Object 资源限制 (等价 Linux cgroup).

对齐 docs/window沙箱.md 6.8:
  - 内存上限   -> JobObjectExtendedLimitInformation.ProcessMemoryLimit
  - CPU 速率   -> JobObjectCpuRateControlInformation.CpuRate
  - 进程数上限 -> JobObjectBasicLimitInformation.ActiveProcessLimit
  - 全部清理   -> JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

通过 ``ctypes`` 调用 kernel32.dll 实现. 所有 win32 调用延迟到函数体内,
模块顶层只定义结构体 (ctypes.Structure 无副作用), Linux 下可 import.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.supervisor import win_constants as const

configure_logging()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job Object 相关结构体 (JOBOBJECT_*_INFORMATION 的 C 布局).
# 详见 winnt.h / jobapi2.h.
# ---------------------------------------------------------------------------
class IoCounters(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    _fields_ = [
        ("ReadOperationCount", wintypes.ULARGE_INTEGER),
        ("WriteOperationCount", wintypes.ULARGE_INTEGER),
        ("OtherOperationCount", wintypes.ULARGE_INTEGER),
        ("ReadTransferCount", wintypes.ULARGE_INTEGER),
        ("WriteTransferCount", wintypes.ULARGE_INTEGER),
        ("OtherTransferCount", wintypes.ULARGE_INTEGER),
    ]


class JobObjBaseLimit(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(wintypes.ULONG)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JobObjExtenLimit(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    _fields_ = [
        ("BasicLimitInformation", JobObjBaseLimit),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobObjCpuRateControl(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    _fields_ = [
        ("ControlFlags", wintypes.DWORD),
        ("CpuRate", wintypes.DWORD),
    ]


_kernel32: ctypes.WinDLL | None = None


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError(
            f"win_job 仅在 Windows 平台可用; 当前平台 {sys.platform!r}"
        )


def get_kernel32() -> ctypes.WinDLL:
    global _kernel32
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 函数原型.
        _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        _kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,  # JOBOBJECTINFOCLASS
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        _kernel32.SetInformationJobObject.restype = wintypes.BOOL
        _kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE,
        ]
        _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32.CloseHandle.restype = wintypes.BOOL
        _kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        ]
        _kernel32.OpenProcess.restype = wintypes.HANDLE
        # ResumeThread: 恢复 CREATE_SUSPENDED 挂起的 runner 主线程 (设计 6.8
        # SUSPEND→Assign→Resume 的 Resume 步, 避免 Job 逃逸窗口).
        _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        _kernel32.ResumeThread.restype = wintypes.DWORD
    return _kernel32


def resume_process(thread_handle: int) -> None:
    """ResumeThread 恢复 CREATE_SUSPENDED 挂起的进程主线程.

    配合 two_hop_spawn(CREATE_SUSPENDED): runner 挂起 → assign_process →
    resume_process, 确保进程在加入 Job 后才开始执行 (review MAJOR #1).
    """
    _require_windows()
    kernel32 = get_kernel32()
    # ResumeThread 返回之前的 suspend count (非 0 表示曾挂起); 0xFFFFFFFF 表示
    # 失败. 成功时 count 通常为 1 (挂起一次).
    prev = kernel32.ResumeThread(wintypes.HANDLE(thread_handle))
    if prev == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())


def create_job(
    memory_max: int | None = None,
    cpu_rate: int | None = None,
    max_processes: int | None = None,
) -> int:
    """创建一个 Job Object 并施加资源限制, 返回 Job handle.

    所有参数为 None 表示无限制 (但仍创建 Job 以便 KILL_ON_JOB_CLOSE 生效).
    """
    _require_windows()
    kernel32 = get_kernel32()

    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    # --- 1. 扩展限制: 内存上限 + KILL_ON_JOB_CLOSE ---
    # memory_max 对齐 Linux cgroup memory.max (整个 job 的内存上限),
    # 用 JOB_OBJECT_LIMIT_JOB_MEMORY + JobMemoryLimit; 同时设
    # ProcessMemoryLimit 作为 per-process 上限双保险 (JOB_OBJECT_LIMIT_PROCESS_MEMORY).
    ext = JobObjExtenLimit()
    ext.BasicLimitInformation.LimitFlags = const.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if memory_max is not None and memory_max > 0:
        ext.BasicLimitInformation.LimitFlags |= (
            const.JOB_OBJECT_LIMIT_JOB_MEMORY
            | const.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        )
        ext.JobMemoryLimit = int(memory_max)  # noqa: N815 - Win32 SDK 字段名
        ext.ProcessMemoryLimit = int(memory_max)  # noqa: N815 - Win32 SDK 字段名
    if max_processes is not None and max_processes > 0:
        ext.BasicLimitInformation.LimitFlags |= const.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        ext.BasicLimitInformation.ActiveProcessLimit = int(max_processes)

    ok = kernel32.SetInformationJobObject(
        job_handle,
        const.JobObjectExtendedLimitInformation,
        ctypes.byref(ext),
        ctypes.sizeof(ext),
    )
    if not ok:
        kernel32.CloseHandle(job_handle)
        raise ctypes.WinError(ctypes.get_last_error())

    # --- 2. CPU 速率限制 ---
    if cpu_rate is not None and 0 < cpu_rate <= 100:
        rate = JobObjCpuRateControl()
        rate.ControlFlags = (  # noqa: N815 - Win32 SDK 字段名
            const.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
            | const.JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        )
        # CpuRate 以 0.01% 为单位, 百分比 * 100.
        rate.CpuRate = int(cpu_rate * 100)  # noqa: N815 - Win32 SDK 字段名
        ok = kernel32.SetInformationJobObject(
            job_handle,
            const.JobObjectCpuRateControlInformation,
            ctypes.byref(rate),
            ctypes.sizeof(rate),
        )
        if not ok:
            logger.warning(
                "设置 Job CPU 速率限制失败 (cpu_rate=%s), 继续运行无 CPU 限制",
                cpu_rate,
                exc_info=True,
            )

    logger.info(
        "创建 Job Object: handle=%s memory=%s cpu_rate=%s max_procs=%s",
        job_handle, memory_max, cpu_rate, max_processes,
    )
    return int(job_handle)


def assign_process(job_handle: int, process_handle: int) -> None:
    """把进程加入 Job Object. 子进程自动继承 (除非 CREATE_BREAKAWAY_FROM_JOB)."""
    _require_windows()
    kernel32 = get_kernel32()
    ok = kernel32.AssignProcessToJobObject(
        wintypes.HANDLE(job_handle), wintypes.HANDLE(process_handle),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def assign_process_by_pid(job_handle: int, pid: int) -> None:
    """按 pid 打开进程并加入 Job (用于无法直接拿到 handle 的子进程)."""
    _require_windows()
    kernel32 = get_kernel32()
    # process_set_quota (0x0100) | process_terminate (0x0001) | PROCESS_QUERY_LIMITED_INFORMATION (0x1000)
    process_set_quota = 0x0100  # noqa: N806 - Win32 SDK 常量风格
    process_terminate = 0x0001  # noqa: N806 - Win32 SDK 常量风格
    proc_handle = kernel32.OpenProcess(
        process_set_quota | process_terminate, False, pid,
    )
    if not proc_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        assign_process(job_handle, proc_handle)
    finally:
        kernel32.CloseHandle(proc_handle)


def close_job(job_handle: int) -> None:
    """关闭 Job handle -> 内核强制终止所有成员进程 (KILL_ON_JOB_CLOSE)."""
    if job_handle is None or job_handle == 0:
        return
    _require_windows()
    kernel32 = get_kernel32()
    kernel32.CloseHandle(wintypes.HANDLE(job_handle))
    logger.info("关闭 Job Object handle=%s, 成员进程被强制终止", job_handle)


def create_exec_job() -> int:
    """为单个 exec 创建专用 ephemeral Job (review #3).

    exec 超时只 TerminateProcess 杀 child 不杀孙进程, 孙进程继续占用端口/资源
    (node server / playwright). 用 ephemeral Job + KILL_ON_JOB_CLOSE: child
    加入此 Job, 超时直接 close_job 触发内核一次性杀整个进程树 (child + 所有
    孙进程). 不加内存/CPU 限制, 仅用于超时强杀整树.

    Windows 7+ 支持嵌套 Job (child 同时属于全局 Job + 本 ephemeral Job 不冲突).
    """
    _require_windows()
    kernel32 = get_kernel32()
    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    ext = JobObjExtenLimit()
    ext.BasicLimitInformation.LimitFlags = const.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job_handle,
        const.JobObjectExtendedLimitInformation,
        ctypes.byref(ext),
        ctypes.sizeof(ext),
    )
    if not ok:
        kernel32.CloseHandle(job_handle)
        raise ctypes.WinError(ctypes.get_last_error())
    logger.debug("创建 exec ephemeral Job: handle=%s", job_handle)
    return int(job_handle)


def teardown(job_handle: int | None) -> None:
    """teardown: 关闭 Job (best-effort, 不抛错)."""
    if job_handle is None:
        return
    try:
        close_job(job_handle)
    except Exception:  # noqa: BLE001
        logger.warning("Job teardown 失败 handle=%s", job_handle, exc_info=True)
