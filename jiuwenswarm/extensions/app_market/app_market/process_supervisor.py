from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys

logger = logging.getLogger(__name__)

# 可选依赖：psutil
try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

# Windows 专用：pywin32 Job Object
if sys.platform == "win32":
    try:
        import win32job

        _HAS_WIN32JOB = True
    except ImportError:
        _HAS_WIN32JOB = False

    # 预设 ctypes Windows API 函数类型（避免 windll 默认 c_int 截断 64 位句柄）
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
else:
    _HAS_WIN32JOB = False


class ProcessSupervisor:
    """进程生命周期管理器 — 负责应用进程的启动、停止和追踪"""

    def __init__(self, logger=None):
        self._logger = logger or logging.getLogger(__name__)
        # Windows: {pid: job_object}; Unix: {pid: pgid}
        self._tracked: dict[int, object] = {}
        # 存储 Popen 对象以便正确管理生命周期
        self._processes: dict[int, subprocess.Popen] = {}
        # Retain process identities before graceful shutdown can orphan children.
        self._children: dict[int, dict[int, psutil.Process]] = {}

    def track_children(self, pid: int) -> None:
        """Remember descendants while their parent is still available."""
        if not _HAS_PSUTIL:
            return
        process = self._processes.get(pid)
        if process is not None and process.poll() is not None:
            return
        try:
            children = self._children.setdefault(pid, {})
            for child in psutil.Process(pid).children(recursive=True):
                children[child.pid] = child
        except psutil.NoSuchProcess:
            pass

    async def _finish_stop(self, pid: int) -> bool:
        """Reclaim the owned tree before dropping its tracking handles."""
        if pid in self._tracked:
            await self.force_kill(pid)
        children = list(self._children.get(pid, {}).values())
        for child in children:
            try:
                # Process.kill checks identity, protecting against PID reuse.
                child.kill()
            except psutil.NoSuchProcess:
                pass
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            remaining = []
            for child in children:
                try:
                    if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                        remaining.append(child)
                except psutil.NoSuchProcess:
                    pass
            job_active = False
            if sys.platform == "win32" and _HAS_WIN32JOB and pid in self._tracked:
                accounting = win32job.QueryInformationJobObject(
                    self._tracked[pid], win32job.JobObjectBasicAccountingInformation,
                )
                job_active = accounting["ActiveProcesses"] > 0
            if not remaining and not job_active:
                self._cleanup_tracking(pid)
                return True
            if asyncio.get_running_loop().time() >= deadline:
                self._logger.warning("进程树尚未退出 pid=%d", pid)
                return False
            await asyncio.sleep(0.1)

    @staticmethod
    def script_command(script: str, interpreter: str | None, args: list[str]) -> list[str]:
        """Use the script's shell rather than executing shell source as a binary."""
        suffix = os.path.splitext(script)[1].lower()
        if suffix == ".ps1":
            return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script, *args]
        if suffix == ".sh":
            return ["/bin/sh", script, *args]
        return ([interpreter] if interpreter else []) + [script, *args]

    async def start(
        self,
        script: str,
        interpreter: str | None,
        args: list[str],
        env: dict[str, str],
        work_dir: str,
        port: int,
    ) -> int:
        """启动应用进程，返回 pid"""
        try:
            # 将端口信息注入环境变量
            env = dict(env)  # 复制避免修改原字典
            env["WORKSWARM_APP_PORT"] = str(port)
            env["PORT"] = str(port)

            # 构建完整命令
            cmd = self.script_command(script, interpreter, args)

            # 确保工作目录存在
            os.makedirs(work_dir, exist_ok=True)

            # 日志文件路径
            log_path = os.path.join(work_dir, "app.log")
            env["WORKSWARM_APP_LOG"] = log_path

            self._logger.info("启动进程: %s, work_dir=%s, port=%d", cmd, work_dir, port)

            # 打开日志文件
            log_file = open(log_path, "a", encoding="utf-8")

            try:
                if sys.platform == "win32":
                    # Windows: 使用 CREATE_NEW_PROCESS_GROUP 创建进程
                    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                    popen = subprocess.Popen(
                        cmd,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        env=env,
                        cwd=work_dir if work_dir else None,
                        creationflags=creationflags,
                    )
                    pid = popen.pid

                    # 尝试使用 Job Object 追踪进程树
                    if _HAS_WIN32JOB:
                        try:
                            job_handle = win32job.CreateJobObject(None, "")
                            win32job.AssignProcessToJobObject(job_handle, int(popen._handle))
                            self._tracked[pid] = job_handle
                            self._logger.info("进程 %d 已绑定 Job Object", pid)
                        except Exception as e:
                            self._logger.warning("绑定 Job Object 失败，退回到普通追踪: %s", e)
                else:
                    # macOS/Linux: 使用 start_new_session 创建独立进程组
                    popen = subprocess.Popen(
                        cmd,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL,
                        env=env,
                        cwd=work_dir if work_dir else None,
                        start_new_session=True,  # 等价于 setsid
                    )
                    pid = popen.pid
                    # 记录进程组 ID（等于 pid，因为使用了 start_new_session）
                    self._tracked[pid] = pid
            finally:
                # 父进程关闭文件句柄，子进程有独立的 fd 副本
                log_file.close()

            # 存储 Popen 对象以便后续管理生命周期
            self._processes[pid] = popen

            self._logger.info("进程已启动 pid=%d", pid)
            return pid

        except Exception as e:
            self._logger.error("启动进程失败: %s", e)
            raise

    async def stop(
        self,
        pid: int,
        stop_script: str | None = None,
        interpreter: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        work_dir: str = "",
        timeout: float = 10.0,
    ) -> bool:
        """停止进程，返回 True 表示成功停止"""
        try:
            self.track_children(pid)
            if not self.is_alive(pid):
                self._logger.info("进程 %d 已不在运行", pid)
                return await self._finish_stop(pid)

            # 如果提供停止脚本，先执行
            if stop_script:
                self._logger.info("执行停止脚本 pid=%d", pid)
                try:
                    cmd = self.script_command(stop_script, interpreter, args or [])
                    stop_env = dict(env) if env else dict(os.environ)
                    subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=stop_env,
                        cwd=work_dir if work_dir else None,
                    )
                except Exception as e:
                    self._logger.warning("执行停止脚本失败: %s", e)

            if not stop_script:
                if sys.platform == "win32":
                    await self.force_kill(pid)
                else:
                    os.killpg(self._tracked.get(pid, pid), signal.SIGTERM)

            # 等待进程退出
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                if not self.is_alive(pid):
                    self._logger.info("进程 %d 已停止", pid)
                    return await self._finish_stop(pid)
                await asyncio.sleep(0.5)

            # 超时后强制终止
            self._logger.warning("进程 %d 超时未退出，强制终止", pid)
            await self.force_kill(pid)
            for _ in range(20):
                if not self.is_alive(pid):
                    return await self._finish_stop(pid)
                await asyncio.sleep(0.1)
            return False

        except Exception as e:
            self._logger.error("停止进程 %d 失败: %s", pid, e)
            return False

    def is_alive(self, pid: int) -> bool:
        """检查进程是否存活"""
        try:
            process = self._processes.get(pid)
            if process is not None:
                return process.poll() is None
            if _HAS_PSUTIL:
                return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
            elif sys.platform == "win32":
                # Windows: os.kill(pid, 0) 会终止进程，不能用于存活检测
                # 使用 ctypes OpenProcess + GetExitCodeProcess 替代
                from ctypes import wintypes

                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                STILL_ACTIVE = 259
                handle = _kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, pid
                )
                if not handle:
                    return False
                exit_code = wintypes.DWORD()
                _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                _kernel32.CloseHandle(handle)
                return exit_code.value == STILL_ACTIVE
            else:
                os.kill(pid, 0)
                return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # 进程存在但没有权限发信号
            return True
        except Exception:
            return False

    async def force_kill(self, pid: int) -> None:
        """强制终止进程及其子进程树"""
        try:
            if sys.platform == "win32":
                # Windows: 优先使用 Job Object 终止整个进程树
                job_handle = self._tracked.get(pid)
                if job_handle is not None and _HAS_WIN32JOB:
                    try:
                        win32job.TerminateJobObject(job_handle, 1)
                        self._logger.info("通过 Job Object 终止进程树 pid=%d", pid)
                        return
                    except Exception as e:
                        self._logger.warning("Job Object 终止失败，退回 taskkill: %s", e)

                # 退回 taskkill
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                )
                self._logger.info("通过 taskkill 终止进程树 pid=%d", pid)
            else:
                # macOS/Linux: 终止整个进程组
                pgid = self._tracked.get(pid, pid)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                    self._logger.info("通过 killpg 终止进程组 pgid=%s", pgid)
                except ProcessLookupError:
                    pass
                except Exception as e:
                    self._logger.warning("killpg 失败，尝试 kill 单个进程: %s", e)
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except Exception as e:
            self._logger.error("强制终止进程 %d 失败: %s", pid, e)

    def _cleanup_tracking(self, pid: int) -> None:
        """清理进程追踪记录"""
        self._children.pop(pid, None)
        tracked = self._tracked.pop(pid, None)
        if tracked is not None and sys.platform == "win32" and _HAS_WIN32JOB:
            try:
                tracked.Close()
            except Exception:
                pass
        # 清理 Popen 对象，调用 poll() 防止 "subprocess is still running" 警告
        popen = self._processes.pop(pid, None)
        if popen is not None:
            try:
                popen.poll()
            except Exception:
                pass
