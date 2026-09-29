"""用户态 IM 渠道关联编排（应用配置 + 设备码授权）。

背景：托管/学习链依赖各渠道 CLI 的用户态登录。CLI 二进制能响应 ``--help``
不代表已完成账号关联（GW-01 E2E 曾出现 discover 静默空列表）。本模块把
「关联」做成服务端编排的两阶段状态机，前端浮层只展示链接/二维码并轮询：

  app_setup → 运行 ``config init --new``（阻塞式子进程），从 stdout 提取
              浏览器配置链接，用户完成后进程退出；
  user_auth → ``auth login --no-wait --json`` 发起设备码授权取
              verification_url/device_code，后台 ``auth login --device-code``
              等待用户在浏览器完成授权；
  logged_in / failed → 终态。

渠道策略注册表 ``_LOGIN_STRATEGIES`` 与 ``connectors.build_connector`` 对齐：
新增渠道 = 注册一个策略，前端「即时关联」入口数量不随渠道增长。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from jiuwenswarm.server.im.im_hosting.cli_resolve import resolve_cli_path
from jiuwenswarm.server.im.im_hosting.connectors import channel_label

LOGGER = logging.getLogger(__name__)

# ── 会话阶段（前端按 phase 渲染浮层）──
PHASE_IDLE = "idle"
PHASE_APP_SETUP = "app_setup"
PHASE_USER_AUTH = "user_auth"
PHASE_LOGGED_IN = "logged_in"
PHASE_FAILED = "failed"

# ── 错误码（随 WS res payload.code 下发，前端据此触发即时关联浮层）──
CODE_NOT_LOGGED_IN = "IM_NOT_LOGGED_IN"
CODE_LOGIN_FAILED = "IM_LOGIN_FAILED"
CODE_LOGIN_UNSUPPORTED = "IM_LOGIN_UNSUPPORTED"
CODE_CLI_UNAVAILABLE = "IM_CLI_UNAVAILABLE"

# 探活结论（内部）
_PROBE_LOGGED_IN = "logged_in"
_PROBE_NEED_APP_SETUP = "need_app_setup"
_PROBE_NEED_USER_AUTH = "need_user_auth"

_ACTIVE_PHASES = frozenset({PHASE_APP_SETUP, PHASE_USER_AUTH})

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")

# config init 打印链接前的 banner 很短；授权等待则给足浏览器操作时间。
URL_READ_TIMEOUT_S = 60.0
APP_SETUP_WAIT_S = 900.0
USER_AUTH_WAIT_S = 900.0
PROBE_TIMEOUT_S = 15.0
BEGIN_TIMEOUT_S = 20.0


class ImError(RuntimeError):
    """IM 用户态错误基类：code 随 WS res payload 下发，供前端分支。"""

    code = "IM_ERROR"


class ImNotLoggedInError(ImError):
    """渠道账号未关联：前端应弹出统一关联浮层后重放原动作。"""

    code = CODE_NOT_LOGGED_IN


class ImLoginError(ImError):
    code = CODE_LOGIN_FAILED


class ImLoginUnsupportedError(ImError):
    code = CODE_LOGIN_UNSUPPORTED


class ImCliUnavailableError(ImError):
    code = CODE_CLI_UNAVAILABLE


@dataclass
class DeviceAuthInfo:
    device_code: str
    verification_url: str


@dataclass
class LoginSession:
    """一次关联会话的内存态快照；跨进程不持久化（重启后按 idle 重新探活）。"""

    channel_id: str
    # idle = 已受理但 monitor 尚未探活出真实阶段（app_setup/user_auth）。
    phase: str = PHASE_IDLE
    url: Optional[str] = None
    message: str = ""
    error: Optional[str] = None
    started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    task: Optional[asyncio.Task] = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "phase": self.phase,
            "url": self.url,
            "message": self.message,
            "error": self.error,
            "started_at_ms": self.started_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _spawn(cli_path: str, args: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        cli_path,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _drain(stream: Optional[asyncio.StreamReader]) -> None:
    """后台排水，防止长阻塞子进程写满管道缓冲。"""
    if stream is None:
        return
    try:
        while True:
            line = await stream.readline()
            if not line:
                return
    except Exception:  # noqa: BLE001
        pass


async def _run_once(
    cli_path: str, args: list[str], *, timeout_s: float, cli_name: str
) -> tuple[int, str, str]:
    """跑一条短命令并收敛输出；超时杀进程并抛 ImLoginError。"""
    proc = await _spawn(cli_path, args)
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        _kill(proc)
        raise ImLoginError(f"{cli_name} {' '.join(args)} 超时（{timeout_s:.0f}s）") from None
    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    return (proc.returncode if proc.returncode is not None else -1), stdout, stderr


async def _wait_url(proc: asyncio.subprocess.Process, *, timeout_s: float, cli_name: str) -> str:
    """逐行读 stdout 直到出现第一个 URL（config init 打印配置链接后阻塞）。"""
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            line_b = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if not line_b:
            break
        match = _URL_RE.search(line_b.decode("utf-8", errors="replace"))
        if match:
            return match.group(0)
    raise ImLoginError(f"{cli_name} 未输出配置链接")


async def _wait_proc(proc: asyncio.subprocess.Process, *, timeout_s: float, cli_name: str) -> int:
    """等待长阻塞子进程退出；超时杀进程并抛 ImLoginError。"""
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        _kill(proc)
        raise ImLoginError(f"{cli_name} 等待超时（{timeout_s:.0f}s）") from None


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _find_nested_text(value: Any, key: str) -> Optional[str]:
    if isinstance(value, dict):
        current = value.get(key)
        if isinstance(current, str) and current.strip():
            return current.strip()
        for nested in value.values():
            found = _find_nested_text(nested, key)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _find_nested_text(nested, key)
            if found:
                return found
    return None


class FeishuLoginStrategy:
    """lark-cli 两阶段关联。

    app_setup：``auth status`` 报 ``error.type=config`` 时需要先跑
    ``config init --new --lang zh_cn``（阻塞式，stdout 打印浏览器配置链接）。
    user_auth：``auth login --domain im --no-wait --json`` 发起设备码授权，
    后台 ``auth login --device-code`` 等待浏览器完成。
    """

    channel_id = "feishu"
    CLI_NAME = "lark-cli"

    async def probe(self, cli_path: str) -> str:
        code, stdout, _stderr = await _run_once(
            cli_path,
            ["auth", "status", "--json", "--verify"],
            timeout_s=PROBE_TIMEOUT_S,
            cli_name=self.CLI_NAME,
        )
        payload = _parse_json(stdout)
        if isinstance(payload, dict):
            if payload.get("ok") is True:
                return _PROBE_LOGGED_IN
            error = payload.get("error")
            if isinstance(error, dict) and str(error.get("type") or "") == "config":
                return _PROBE_NEED_APP_SETUP
            return _PROBE_NEED_USER_AUTH
        if code != 0:
            detail = " ".join((_stderr or stdout).split())[:160]
            raise ImLoginError(f"{self.CLI_NAME} auth status 输出无法解析（退出码 {code}）：{detail}")
        return _PROBE_NEED_USER_AUTH

    async def begin_app_setup(self, cli_path: str) -> tuple[str, asyncio.subprocess.Process]:
        proc = await _spawn(cli_path, ["config", "init", "--new", "--lang", "zh_cn"])
        url = await _wait_url(proc, timeout_s=URL_READ_TIMEOUT_S, cli_name=self.CLI_NAME)
        # 链接已取得：剩余输出转后台排水，退出等待交给 _wait_proc。
        asyncio.create_task(_drain(proc.stdout))
        asyncio.create_task(_drain(proc.stderr))
        return url, proc

    async def begin_user_auth(self, cli_path: str) -> DeviceAuthInfo:
        _, stdout, _stderr = await _run_once(
            cli_path,
            ["auth", "login", "--domain", "im", "--no-wait", "--json"],
            timeout_s=BEGIN_TIMEOUT_S,
            cli_name=self.CLI_NAME,
        )
        payload = _parse_json(stdout)
        device_code = _find_nested_text(payload, "device_code")
        url = _find_nested_text(payload, "verification_url")
        if not device_code or not url or not url.startswith("https://"):
            detail = " ".join(stdout.split())[:160]
            raise ImLoginError(f"{self.CLI_NAME} 授权响应缺少有效的验证链接：{detail}")
        return DeviceAuthInfo(device_code=device_code, verification_url=url)

    async def finish_user_auth(self, cli_path: str, device_code: str) -> None:
        proc = await _spawn(cli_path, ["auth", "login", "--device-code", device_code])
        asyncio.create_task(_drain(proc.stdout))
        asyncio.create_task(_drain(proc.stderr))
        code = await _wait_proc(proc, timeout_s=USER_AUTH_WAIT_S, cli_name=self.CLI_NAME)
        if code != 0:
            raise ImLoginError(f"{self.CLI_NAME} 授权未完成（退出码 {code}）")


_LOGIN_STRATEGIES: dict[str, FeishuLoginStrategy] = {
    "feishu": FeishuLoginStrategy(),
}


class ImLoginManager:
    """按渠道单飞编排关联会话；status 只读快照，前端轮询消费。"""

    def __init__(
        self,
        *,
        strategies: Optional[dict[str, Any]] = None,
        cli_path_resolver: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self._strategies: dict[str, Any] = (
            strategies if strategies is not None else dict(_LOGIN_STRATEGIES)
        )
        self._cli_path_resolver = cli_path_resolver or resolve_cli_path
        self._sessions: dict[str, LoginSession] = {}
        self._lock = asyncio.Lock()

    def _resolve(self, channel_id: str) -> tuple[Any, str]:
        strategy = self._strategies.get(channel_id)
        if strategy is None:
            raise ImLoginUnsupportedError(f"{channel_label(channel_id)} 暂不支持在线关联")
        cli_path = self._cli_path_resolver(channel_id)
        if not cli_path:
            raise ImCliUnavailableError(f"{channel_label(channel_id)} CLI 不可用，请先安装并登录后重试")
        return strategy, cli_path

    async def start(self, channel_id: str) -> dict[str, Any]:
        strategy, cli_path = self._resolve(channel_id)
        async with self._lock:
            session = self._sessions.get(channel_id)
            if session is not None and session.task is not None and not session.task.done():
                # 单飞：进行中的会话直接复用，避免并发拉起多个 CLI 授权进程。
                return session.snapshot()
            session = LoginSession(channel_id=channel_id)
            self._sessions[channel_id] = session
            session.task = asyncio.create_task(
                self._run_session(session, strategy, cli_path),
                name=f"im-login-{channel_id}",
            )
            return session.snapshot()

    async def status(self, channel_id: str) -> dict[str, Any]:
        strategy, cli_path = self._resolve(channel_id)
        session = self._sessions.get(channel_id)
        if session is not None:
            if (
                session.phase in _ACTIVE_PHASES
                and session.task is not None
                and session.task.done()
            ):
                # monitor 已结束但阶段未落终态（不应发生）：标记失败避免浮层卡死。
                self._finish(session, PHASE_FAILED, error="关联会话已中断，请重试")
            return session.snapshot()
        # 无会话（如服务重启后）：现场探活，已登录场景可直接返回成功。
        phase = await strategy.probe(cli_path)
        return {
            "channel_id": channel_id,
            "phase": PHASE_LOGGED_IN if phase == _PROBE_LOGGED_IN else PHASE_IDLE,
            "url": None,
            "message": "",
            "error": None,
        }

    @staticmethod
    def _finish(
        session: LoginSession,
        phase: str,
        *,
        message: str = "",
        error: Optional[str] = None,
    ) -> None:
        session.phase = phase
        session.message = message
        session.error = error
        session.updated_at_ms = _now_ms()
        session.task = None

    async def _run_session(self, session: LoginSession, strategy: Any, cli_path: str) -> None:
        try:
            state = await strategy.probe(cli_path)
            if state == _PROBE_LOGGED_IN:
                self._finish(session, PHASE_LOGGED_IN, message="已登录")
                return
            if state == _PROBE_NEED_APP_SETUP:
                session.phase = PHASE_APP_SETUP
                session.message = "请在浏览器完成应用配置"
                session.updated_at_ms = _now_ms()
                url, proc = await strategy.begin_app_setup(cli_path)
                session.url = url
                session.updated_at_ms = _now_ms()
                exit_code = await _wait_proc(
                    proc, timeout_s=APP_SETUP_WAIT_S, cli_name=strategy.CLI_NAME
                )
                if exit_code != 0:
                    self._finish(session, PHASE_FAILED, error=f"应用配置未完成（退出码 {exit_code}）")
                    return
                state = await strategy.probe(cli_path)
                if state == _PROBE_LOGGED_IN:
                    self._finish(session, PHASE_LOGGED_IN, message="已登录")
                    return
                if state == _PROBE_NEED_APP_SETUP:
                    self._finish(session, PHASE_FAILED, error="应用配置未生效，请重试")
                    return
            session.phase = PHASE_USER_AUTH
            session.message = "请扫码或在浏览器完成账号授权"
            # 清掉 app_setup 阶段的旧链接，避免二维码短暂显示上一步的 URL。
            session.url = None
            session.updated_at_ms = _now_ms()
            info = await strategy.begin_user_auth(cli_path)
            session.url = info.verification_url
            session.updated_at_ms = _now_ms()
            await strategy.finish_user_auth(cli_path, info.device_code)
            state = await strategy.probe(cli_path)
            if state == _PROBE_LOGGED_IN:
                self._finish(session, PHASE_LOGGED_IN, message="已登录")
            else:
                self._finish(session, PHASE_FAILED, error="授权流程结束但账号仍未登录，请重试")
        except ImError as exc:
            self._finish(session, PHASE_FAILED, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("[im_login] %s session failed", session.channel_id)
            self._finish(session, PHASE_FAILED, error=str(exc))


_manager: Optional[ImLoginManager] = None


def get_login_manager() -> ImLoginManager:
    global _manager
    if _manager is None:
        _manager = ImLoginManager()
    return _manager


def reset_login_manager() -> None:
    """测试隔离用：丢弃单例。"""
    global _manager
    _manager = None
