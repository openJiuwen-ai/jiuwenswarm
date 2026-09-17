# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""管理本地 jiuwenbox uvicorn 子进程 — 由 ``/sandbox enable`` 触发启动.

设计要点:
- 默认地址 ``http://127.0.0.1:8321``; 端口是否空闲由 ``agent_ws_server`` 在
  调用 ``ensure_running`` *之前* 决定 (占用就换随机空闲端口, 避免与未知第三方
  进程冲突); 本 runner 不再尝试识别 / 接管端口上的"外部进程".
- ``startup_mode='internal'`` 时: 若 runner 已经在该 host:port 上拥有一个
  与当前 ``policy_path`` 匹配的进程, 直接复用; 否则停掉旧进程并重新 spawn
  ``uvicorn jiuwenbox.server.app:app`` (通过 ``JIUWENBOX_POLICY_PATH`` 注入
  policy 文件路径)。
- ``startup_mode='external'`` 时: 仅做健康检查; 不可达则直接失败, 提示用户
  自行 (含 ``sudo``) 启动 jiuwenbox-server, 配合 ``policy_path`` 传入相应 policy;
- agent_ws_server 进程退出时调用 ``stop()`` 终止子进程, 避免悬挂.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Linux ``prctl`` PR_SET_PDEATHSIG 选项常量 (来自 ``<linux/prctl.h>``);
# 模块级常量, 避免在 ``_try_set_pdeathsig`` 函数内出现 UPPER_CASE 局部变量。
_PR_SET_PDEATHSIG = 1


def _resolve_jiuwenbox_src_dir() -> Optional[Path]:
    """探测仓库内 ``code_agent/jiuwenbox/src``; 若存在则供 PYTHONPATH 注入用.

    便于无需 ``pip install -e jiuwenbox/`` 也能直接运行本地源码版 jiuwenbox.
    返回 ``None`` 表示未找到 (则依赖 site-packages 中的安装版).
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents[1:7]:
        candidate = ancestor / "jiuwenbox" / "src" / "jiuwenbox" / "__init__.py"
        if candidate.exists():
            return candidate.parent.parent
    return None


def _try_set_pdeathsig() -> None:
    """Linux: 让子进程在父进程退出时收到 SIGTERM, 避免 SIGKILL 父进程时 jiuwenbox 残留.

    通过 ``preexec_fn`` 调用; 在非 Linux 平台是 no-op.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except Exception:  # noqa: BLE001
        pass


class JiuwenBoxRunner:
    """单例形态管理本地 jiuwenbox 子进程."""

    _INSTANCE: "JiuwenBoxRunner | None" = None
    # stderr 滚动缓冲最大行数; 类级常量, 避免实例级 UPPER_CASE 属性。
    _STDERR_TAIL_MAX: int = 80

    def __init__(self) -> None:
        self._process: Optional[asyncio.subprocess.Process] = None
        self._host: str = "127.0.0.1"
        self._port: int = 8321
        self._lock = asyncio.Lock()
        # 标记进程是否由本 runner 启动; 若用户在外部已起服务, 不应在 stop() 时杀掉
        self._owns_process: bool = False
        # 进程退出兜底: atexit 同步钩子, 即便 stop() 没被走到也尝试终止子进程
        self._atexit_registered: bool = False
        # 持续 drain 的后台任务以及 stderr 滚动缓冲, 便于子进程异常退出时反查原因
        self._stdout_pump_task: Optional[asyncio.Task] = None
        self._stderr_pump_task: Optional[asyncio.Task] = None
        self._stderr_tail: list[str] = []
        # 记录最近一次 ensure_running 用到的 startup_mode, 便于诊断 / 日志透出。
        self._last_startup_mode: str = "internal"
        # 记录本 runner 上次 spawn 子进程时实际注入的 ``JIUWENBOX_POLICY_PATH``;
        # 下次 ``ensure_running`` 若发现期望值与之不一致, 必须停掉旧实例重启,
        # 避免老进程继续用旧 policy (例如 default-policy.yaml) 服务新 sandbox。
        self._spawned_policy_path: Optional[Path] = None

    @classmethod
    def instance(cls) -> "JiuwenBoxRunner":
        if cls._INSTANCE is None:
            cls._INSTANCE = JiuwenBoxRunner()
        return cls._INSTANCE

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def get_stderr_tail(self, lines: int = 40) -> str:
        """返回最近 ``lines`` 行子进程 stderr, 便于错误诊断."""
        if not self._stderr_tail:
            return ""
        return "\n".join(self._stderr_tail[-lines:])

    def is_owned_listener(self, host: str, port: int) -> bool:
        """``True`` 表示当前 runner 持有一个仍在跑的子进程, 且监听在 ``host:port``.

        ``agent_ws_server`` 在分配端口前用它判断"8321 是不是我自己刚才拉起的",
        避免误把自己的进程当成外部占用而无谓换端口。
        """
        proc = self._process
        if proc is None or proc.returncode is not None:
            return False
        if not self._owns_process:
            return False
        return self._host == host and self._port == port

    def get_owned_endpoint(self) -> Optional[tuple[str, int]]:
        """返回当前由本 runner 拥有的 (host, port); 没有就返回 None."""
        proc = self._process
        if proc is None or proc.returncode is not None:
            return None
        if not self._owns_process:
            return None
        return (self._host, self._port)

    async def health_check(self, host: str | None = None, port: int | None = None) -> bool:
        target_host = host or self._host
        target_port = port or self._port
        url = f"http://{target_host}:{target_port}/health"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(url)
                return resp.status_code == 200
        except Exception:
            return False

    async def fetch_health(self, host: str | None = None, port: int | None = None) -> dict[str, Any] | None:
        """Return parsed jiuwenbox ``/health`` JSON, or ``None`` on failure."""
        target_host = host or self._host
        target_port = port or self._port
        url = f"http://{target_host}:{target_port}/health"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return None
                data = resp.json()
                return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def ensure_running(
        self,
        host: str = "127.0.0.1",
        port: int = 8321,
        *,
        timeout: float = 30.0,
        startup_mode: str = "internal",
        policy_path: Optional[Path] = None,
    ) -> bool:
        """确保 jiuwenbox 在 ``host:port`` 已就绪。

        Args:
            host / port: jiuwenbox 监听地址。
            timeout: 健康检查 / 等待就绪的总超时秒数。
            startup_mode: ``internal`` (agent-server 拉起) 或 ``external``
                (用户自己启动 jiuwenbox); ``external`` 模式下本方法不会 spawn 子进程,
                仅做健康检查。
            policy_path: jiuwenbox 启动时使用的 policy 文件路径; 仅在
                ``startup_mode='internal'`` 下生效, 通过 ``JIUWENBOX_POLICY_PATH``
                环境变量传给子进程。

        Returns:
            True 表示启动 / 已运行并通过健康检查; False 表示超时未就绪。
        """
        async with self._lock:
            normalized_mode = (startup_mode or "internal").strip().lower()
            if normalized_mode not in ("internal", "external"):
                normalized_mode = "internal"
            self._last_startup_mode = normalized_mode

            # external 模式: 只做健康检查, 不 spawn / 不 kill 任何进程。
            # 用户负责保证 jiuwenbox-server 使用合适的 JIUWENBOX_POLICY_PATH 启动。
            if normalized_mode == "external":
                self._host = host
                self._port = port
                if await self.health_check(host, port):
                    logger.info(
                        "[JiuwenBoxRunner] external jiuwenbox alive at %s:%d "
                        "(policy_path env is user's responsibility, expected=%s)",
                        host,
                        port,
                        policy_path,
                    )
                    return True
                logger.warning(
                    "[JiuwenBoxRunner] startup_mode=external but %s:%d unreachable; "
                    "user is expected to start jiuwenbox-server manually",
                    host,
                    port,
                )
                return False

            # internal 模式: agent-server 自己管理 jiuwenbox 生命周期。
            # 不再尝试识别端口上的外部进程; 上游 agent_ws_server 已经保证传进来的
            # ``port`` 是 (a) 我们自己之前拉起的同 host:port 或 (b) 一个空闲端口。
            #
            # 决策矩阵:
            # - 我们拥有的进程仍然 alive 且 host/port/policy_path 全部匹配 → 复用;
            # - 否则: 停掉旧进程 (如有), 在新的 host:port 上 spawn 全新实例。
            owned_match = (
                self._process is not None
                and self._process.returncode is None
                and self._owns_process
                and self._host == host
                and self._port == port
                and self._spawned_policy_path == policy_path
            )
            if owned_match:
                if await self.health_check(host, port):
                    logger.info(
                        "[JiuwenBoxRunner] reuse owned jiuwenbox at %s:%d "
                        "(policy_path=%s)",
                        host,
                        port,
                        policy_path,
                    )
                    return True
                # 进程在跑但还没 ready, 继续等
                return await self._wait_until_ready(host, port, timeout=timeout)

            # 任何 mismatch (端口变了 / policy 变了 / 进程已退) 都先把旧的清掉。
            if self._process is not None and self._owns_process:
                logger.info(
                    "[JiuwenBoxRunner] stopping owned jiuwenbox before spawning new one "
                    "(prev host=%s port=%d policy=%s -> new host=%s port=%d policy=%s)",
                    self._host,
                    self._port,
                    self._spawned_policy_path,
                    host,
                    port,
                    policy_path,
                )
                await self._stop_no_lock()

            self._host = host
            self._port = port

            cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                "jiuwenbox.server.app:app",
                "--host",
                host,
                "--port",
                str(port),
            ]
            # 若 jiuwenbox 未安装到 site-packages, 尝试用仓库内源码目录注入 PYTHONPATH
            env = dict(os.environ)
            local_src = _resolve_jiuwenbox_src_dir()
            if local_src is not None:
                existing = env.get("PYTHONPATH", "")
                parts = [str(local_src)]
                if existing:
                    parts.append(existing)
                env["PYTHONPATH"] = os.pathsep.join(parts)
                logger.info(
                    "[JiuwenBoxRunner] prepending local jiuwenbox src to PYTHONPATH: %s",
                    local_src,
                )
            # 把 policy 路径通过环境变量传给 jiuwenbox-server (与 README 中
            # ``JIUWENBOX_POLICY_PATH`` 用法一致, 即 ``server/app.py`` 启动时读取)。
            # 如果调用方没给, 显式删掉父进程继承下来的同名变量, 避免误用旧值。
            if policy_path is not None:
                env["JIUWENBOX_POLICY_PATH"] = str(policy_path)
                logger.info(
                    "[JiuwenBoxRunner] injecting JIUWENBOX_POLICY_PATH=%s",
                    policy_path,
                )
            else:
                env.pop("JIUWENBOX_POLICY_PATH", None)

            logger.info("[JiuwenBoxRunner] spawning: %s", " ".join(cmd))
            try:
                spawn_kwargs: dict = {
                    "stdout": asyncio.subprocess.PIPE,
                    "stderr": asyncio.subprocess.PIPE,
                    "env": env,
                }
                # Linux: 父进程退出时让子进程收到 SIGTERM (PR_SET_PDEATHSIG)
                if sys.platform.startswith("linux"):
                    spawn_kwargs["preexec_fn"] = _try_set_pdeathsig
                self._process = await asyncio.create_subprocess_exec(
                    *cmd,
                    **spawn_kwargs,
                )
                self._owns_process = True
                self._spawned_policy_path = policy_path
                # 同步退出兜底: 即便没走 stop() 也尽可能 terminate 子进程
                self._register_atexit_once()
                # 后台持续 drain stdout/stderr, 防止管道堆积阻塞子进程; 同时
                # 记录滚动 stderr 尾部, 便于失败时反查 uvicorn 的导入/启动错误
                self._stderr_tail = []
                self._stdout_pump_task = asyncio.create_task(
                    self._pump_stream(self._process.stdout, "stdout")
                )
                self._stderr_pump_task = asyncio.create_task(
                    self._pump_stream(self._process.stderr, "stderr")
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[JiuwenBoxRunner] spawn failed: %s", exc)
                self._process = None
                self._owns_process = False
                self._spawned_policy_path = None
                return False

            ok = await self._wait_until_ready(host, port, timeout=timeout)
            if not ok:
                tail = "\n".join(self._stderr_tail[-40:])
                if self._process is not None and self._process.returncode is not None:
                    logger.error(
                        "[JiuwenBoxRunner] jiuwenbox subprocess exited rc=%s during startup. "
                        "stderr tail:\n%s",
                        self._process.returncode,
                        tail or "(empty; check if uvicorn / jiuwenbox is installed)",
                    )
                else:
                    logger.warning(
                        "[JiuwenBoxRunner] health check timeout after %ss; pid=%s. "
                        "stderr tail:\n%s",
                        timeout,
                        self._process.pid if self._process else None,
                        tail or "(empty)",
                    )
            return ok

    async def _pump_stream(self, stream: Any, kind: str) -> None:  # type: ignore[override]
        """持续读取子进程 stdout/stderr, 写入 logger debug; stderr 额外保留滚动尾部."""
        if stream is None:
            return
        try:
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    return
                try:
                    line = line_bytes.decode("utf-8", errors="replace").rstrip()
                except Exception:  # noqa: BLE001
                    line = repr(line_bytes)
                if kind == "stderr":
                    self._stderr_tail.append(line)
                    if len(self._stderr_tail) > self._STDERR_TAIL_MAX:
                        # 保留尾部 N 行
                        del self._stderr_tail[0:len(self._stderr_tail) - self._STDERR_TAIL_MAX]
                logger.debug("[jiuwenbox/%s] %s", kind, line)
        # ``asyncio.CancelledError`` 是 ``BaseException`` 子类 (Python 3.8+),
        # 不会被 ``except Exception`` 捕获, 因此无需显式 ``except ... raise``。
        except Exception as exc:  # noqa: BLE001
            logger.debug("[JiuwenBoxRunner] pump %s stopped: %s", kind, exc)

    async def _wait_until_ready(self, host: str, port: int, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.returncode is not None:
                logger.warning(
                    "[JiuwenBoxRunner] subprocess exited prematurely (rc=%s)",
                    self._process.returncode,
                )
                return False
            if await self.health_check(host, port):
                logger.info(
                    "[JiuwenBoxRunner] jiuwenbox ready at %s:%d",
                    host,
                    port,
                )
                return True
            await asyncio.sleep(0.1)
        return False

    def _register_atexit_once(self) -> None:
        if self._atexit_registered:
            return
        try:
            atexit.register(self._sync_terminate)
            self._atexit_registered = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[JiuwenBoxRunner] atexit register failed: %s", exc)

    def _sync_terminate(self) -> None:
        """同步退出兜底: ``atexit`` / 异常退出场景调用, 不依赖事件循环.

        - 若 ``stop()`` 已正常清理, 则什么都不做;
        - 否则尽可能 ``terminate`` / ``kill`` 子进程, 避免 jiuwenbox 残留.
        """
        proc = self._process
        if proc is None or not self._owns_process:
            return
        # asyncio.subprocess.Process exposes returncode / pid 同步可读
        if proc.returncode is not None:
            return
        pid = proc.pid
        logger.info("[JiuwenBoxRunner] atexit: terminating subprocess pid=%s", pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("[JiuwenBoxRunner] atexit SIGTERM failed: %s", exc)
            return
        # 等待最多 3s
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                # 0 信号: 探测进程是否存在
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except Exception:  # noqa: BLE001
                return
            time.sleep(0.1)
        # 超时则 SIGKILL
        with contextlib.suppress(ProcessLookupError, Exception):
            os.kill(pid, signal.SIGKILL)

    async def stop(self) -> None:
        """优雅停止由本 runner 启动的子进程."""
        async with self._lock:
            await self._stop_no_lock()

    async def _stop_no_lock(self) -> None:
        """``stop()`` 的去锁版本; 调用方必须已经持有 ``self._lock``.

        被 ``ensure_running`` 复用: 监测到 policy_path 变更, 需要重启子进程以让
        新的 ``JIUWENBOX_POLICY_PATH`` 生效。
        """
        for task_attr in ("_stdout_pump_task", "_stderr_pump_task"):
            task = getattr(self, task_attr, None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            setattr(self, task_attr, None)

        proc = self._process
        if proc is None or proc.returncode is not None:
            self._process = None
            self._spawned_policy_path = None
            return
        if not self._owns_process:
            self._process = None
            self._spawned_policy_path = None
            return
        logger.info("[JiuwenBoxRunner] stopping subprocess pid=%s", proc.pid)
        try:
            proc.terminate()
        except ProcessLookupError:
            self._process = None
            self._spawned_policy_path = None
            return
        # uvicorn 收到 SIGTERM 后会跑 FastAPI lifespan shutdown, 期间会调
        # ``SandboxManager.shutdown_all_sandboxes`` 给每个活的 sandbox 做
        # SIGTERM -> wait -> SIGKILL 三段式 teardown (每个最坏要 ~15s,
        # 见 jiuwenbox.server.runtime.process.SandboxRuntime.stop)。如果这里
        # 留的 grace 不够长, lifespan 还没清完就被 SIGKILL, sandbox-daemon.py
        # 会被 reparent 到 init 成为孤儿进程, 留在 host 上一直跑。所以这里
        # 给一个相对宽松的 60s 上限; 实操中没活 sandbox 时 uvicorn 自己很快
        # 就退了, 不会真等满。
        try:
            await asyncio.wait_for(proc.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            logger.warning(
                "[JiuwenBoxRunner] terminate timeout (60s); killing pid=%s "
                "(sandbox-daemon orphans may remain on host)",
                proc.pid,
            )
            try:
                proc.kill()
                await proc.wait()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[JiuwenBoxRunner] kill failed: %s", exc)
        self._process = None
        self._owns_process = False
        self._spawned_policy_path = None
