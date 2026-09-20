#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""并发向 Gateway WebChannel 发送 chat.send，验证 Runtime 转发与 AgentServer 池的端到端压测。

脚本：`enterprise_runtime_concurrent_test.py`（与 ``enterprise_runtime_service_config.py`` 同属
Gateway Runtime 联调/压测工具，前者读库校验 ``service_config`` 槽位，本脚本经 WebSocket 发真实
``chat.send`` 做并发端到端验证。）

默认发起 30 路并发（``--concurrency 30``），均匀分布到 3 个 AgentServer（``--shards 3``），
即每个 AgentServer 10 路；**同一分片内共用同一个 ``group_id``**
（默认 ``loadtest_s0`` / ``loadtest_s1`` / ``loadtest_s2``），以便经 Gateway 路由
均匀打到 3 个 AgentServer 实例上。

可选 ``--shards2 N``：在同一 AgentServer（同一 ``group_id``）内，再按 ``user_id`` 打到
N 个 Agent 实例（``user_id`` 形如 ``{prefix}_s{shard}_a{j}``，同桶多路共用同一 ``user_id``）。
不要求 ``concurrency`` 与 ``shards`` / ``shards2`` 整除，余数路由由靠前的分片/实例多承接。
``shards2=0``（默认）时每路独立 ``user_id``（``{prefix}_{idx:02d}``）；``shards2=1`` 时同一
AgentServer 内全部打到同一 Agent 实例；``shards2>=2`` 时在实例间轮询。``session_id`` / ``req_id`` 始终每路唯一。

依赖：主仓库已安装 ``websockets``（``uv sync`` 或 ``pip install websockets``）。

压测时可配合 ``mock_llm_server.py`` 替代真实 LLM（见设计文档 **§3.3 Mock LLM**）。

典型用法（项目根目录）::

    # 本地 provision 后的 Gateway Web 端口
    uv run python packages/jiuwenclaw-ee/claw_manager/scripts/enterprise_runtime_concurrent_test.py \\
        --web-port 19234

    # 远程 Gateway
    uv run python packages/jiuwenclaw-ee/claw_manager/scripts/enterprise_runtime_concurrent_test.py \\
        --ws-url ws://10.0.0.1:19001/ws

    # K8s 仅暴露 Web NodePort（5173 -> 30105）时，经 HTTP 端口的 /ws 代理连入
    # jiuwenclaw-web:19000 为 ClusterIP，集群外不可直连，须走 NodePort + /ws
    uv run python packages/jiuwenclaw-ee/claw_manager/scripts/enterprise_runtime_concurrent_test.py \\
        --ws-url ws://<节点IP>:30105/ws

    # 等价写法（--web-port 填 NodePort 外部端口，非 19000）
    uv run python .../enterprise_runtime_concurrent_test.py \\
        --host <节点IP> --web-port 30105

    # 自定义总并发与 AgentServer 分片（60 路均匀打到 6 个 AgentServer，每个 10 路）
    uv run python .../enterprise_runtime_concurrent_test.py \\
        --web-port 19234 --concurrency 60 --shards 6

    # 每个 AgentServer 内再均匀打到 2 个 Agent 实例（user_id 分桶）
    uv run python .../enterprise_runtime_concurrent_test.py \\
        --ws-url ws://host:30105/ws --concurrency 6 --shards 3 --shards2 2

    # 按 bot_id 分片路由（固定 group_id，每 shard 不同 bot_id），配合 Gateway AGENT_BOT_ID_GROUP_NUM 联调
    uv run python .../enterprise_runtime_concurrent_test.py \\
        --ws-url ws://host:30105/ws --concurrency 9 --shards 3 --service-shard-key bot_id

    # loadtest 四步流程（travel → skill → file → cron，配合 mock_llm_server --profile loadtest）
    uv run python .../enterprise_runtime_concurrent_test.py \\
        --ws-url ws://host:30105/ws --concurrency 3 --flow loadtest

默认等待整轮 Agent 任务结束。``--flow loadtest``（默认）时，每路会话在同一 ``session_id`` 内
**依次**发送 4 条用户消息（travel → skill → file 上传扩写 → cron），与 ``mock_llm_server.py``
``--profile loadtest`` 场景顺序一致；``--flow single`` 时仍为单条 ``--content`` 消息。
含文件交付的步骤须先收到 ``chat.file``（或 ``send_file_to_user`` 的 ``chat.tool_result``），
再采纳 ``chat.processing_status idle`` / ``chat.usage_summary`` 等终态信号；定时任务步骤须等
约 1 分钟后收到喝水提醒投递文案（``🥤 喝水时间到啦！…``），创建确认 alone 不算完成。
``file`` 扩写步骤在收到 ``chat.file`` 后，会把交付文件下载到脚本目录下带时间戳的
``download_YYYYMMDD_HHMMSS/``（同一次压测共用一个目录），本地文件名再追加请求编号
（如 ``童趣的春天_扩写版_00.md``）以免并发覆盖。
DeepAgent 在流式文本 iteration 结束时可能发出 **content 为空** 的 ``chat.final`` 标记，
该帧仅表示当前 LLM 轮次结束，**不是**整轮任务完成，脚本会忽略并继续等待。
权限放行（``auto-allow``）后，忽略紧随其后的假 idle / 子流 ``usage_summary``，
直至新一轮 ``chat.delta`` 表明 Agent 已继续执行。
每路真正完成时打印 ``[done]`` 行（含 idx / session_id / 耗时）。Agent 弹出权限/追问（``chat.ask_user_question``）
时自动全部允许（权限类选「总是允许」）。长任务可通过 ``--final-timeout`` 调整上限。
资源打满（100001/100002）时 Gateway 常提前 ``is_processing=false`` 且不一定下发
``chat.error``；脚本会识别 payload 错误关键字，并将「未交付即 idle」记为失败，避免挂死。
若仅需验证 Gateway 接受请求、不等 Agent 跑完，加 ``--accept-only``；禁用自动放行加 ``--no-auto-allow``。
排查权限/追问 WS 事件时加 ``--ws-event-log``，会打印每路收到的 event 名（含 frame.event
与 payload.event_type 对照，便于发现事件名不匹配）。

Ctrl+C 退出时，脚本会对**已接受且未完成**的会话发送 ``chat.interrupt``（``intent=cancel``），
与 ``web_enterprise`` 页面点击「取消」一致；仅关闭 WebSocket **不会**自动停止 AgentServer 上的任务。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_DEFAULT_BOT_ID = "bot_main"
_SERVICE_SHARD_KEYS = frozenset({"group_id", "bot_id"})


@dataclass(frozen=True)
class RoutePlan:
    """单路 chat.send 的 Gateway 路由参数（shard→AgentServer，shard2→Agent 实例）。"""

    shard: int
    shard2: int
    group_id: str
    bot_id: str
    user_id: str


_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_SPRING_ESSAY = _SCRIPT_DIR / "童趣的春天.md"
_DOWNLOAD_DIR_PREFIX = _SCRIPT_DIR / "download"


def _make_run_download_dir(*, when: time.struct_time | None = None) -> Path:
    """生成本次压测专用下载目录：download_YYYYMMDD_HHMMSS。"""
    stamp = time.strftime("%Y%m%d_%H%M%S", when or time.localtime())
    return Path(f"{_DOWNLOAD_DIR_PREFIX}_{stamp}")


@dataclass(frozen=True)
class LoadTestStep:
    """loadtest 单步用户请求（与 mock_llm_server loadtest 场景顺序一致）。"""

    name: str
    content: str
    expect_file: bool = True
    files: tuple[dict[str, str], ...] = ()
    expect_delayed_text: bool = False
    download_deliverable: bool = False


_CRON_CREATION_MARKERS = ("喝水提醒已创建", "执行时间：")
_CRON_DELIVERY_MARKERS = ("喝水时间到啦",)


def build_default_loadtest_steps(essay_path: Path) -> tuple[LoadTestStep, ...]:
    if not essay_path.is_file():
        raise FileNotFoundError(f"loadtest 附件不存在: {essay_path}")
    resolved = str(essay_path.resolve())
    essay_name = essay_path.name
    return (
        LoadTestStep(
            name="travel",
            content=(
                "帮我写一篇十万字的小说，主题是旅行的意义，写完后保存到txt发给我。"
                "直接开始写，不要问我其他问题"
            ),
            expect_file=True,
        ),
        LoadTestStep(
            name="skill",
            content=(
                "先使用skillnet安装这个旅游攻略技能"
                "https://github.com/Asif2BD/openclaw.tours/tree/main，"
                "然后再给我制作一个北京3日游的旅游攻略"
            ),
            expect_file=True,
        ),
        LoadTestStep(
            name="file",
            content="帮我把这个文件里的作文扩写到6000字，然后发回给我",
            expect_file=True,
            files=({"path": resolved, "name": essay_name},),
            download_deliverable=True,
        ),
        LoadTestStep(
            name="cron",
            content="创建一个定时任务，1分钟后提醒我喝水",
            expect_file=False,
            expect_delayed_text=True,
        ),
    )


_DEFAULT_CONTENT = (
    "帮我写一篇十万字的小说，主题是旅行的意义，写完后保存到txt发给我。直接开始写，不要问我其他问题"
)


@dataclass
class RequestResult:
    index: int
    shard: int
    shard2: int
    session_id: str
    req_id: str
    group_id: str
    bot_id: str
    user_id: str
    ok: bool
    accepted: bool
    error: str = ""
    accept_ms: float = 0.0
    total_ms: float = 0.0
    final_received: bool = False
    steps_completed: int = 0
    steps_total: int = 0
    failed_step: str = ""


@dataclass
class LoadTestStats:
    total: int
    completed: int
    failed: int
    elapsed_s: float
    accept_ms: list[float] = field(default_factory=list)
    total_ms: list[float] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"total={self.total} completed={self.completed} failed={self.failed} "
            f"elapsed={self.elapsed_s:.2f}s",
        ]
        if self.accept_ms:
            sorted_accept = sorted(self.accept_ms)
            lines.append(
                f"accept_ms: avg={statistics.mean(sorted_accept):.0f} "
                f"min={min(sorted_accept):.0f} "
                f"max={max(sorted_accept):.0f} "
                f"p50={_percentile(sorted_accept, 0.5):.0f} "
                f"p95={_percentile(sorted_accept, 0.95):.0f}"
            )
        if self.total_ms:
            sorted_total = sorted(self.total_ms)
            lines.append(
                f"total_ms: avg={statistics.mean(sorted_total):.0f} "
                f"min={min(sorted_total):.0f} "
                f"max={max(sorted_total):.0f} "
                f"p50={_percentile(sorted_total, 0.5):.0f} "
                f"p95={_percentile(sorted_total, 0.95):.0f}"
            )
        return "\n".join(lines)


@dataclass
class ProgressTracker:
    total: int
    completed: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def mark_done(self) -> int:
        async with self.lock:
            self.completed += 1
            return self.completed


@dataclass
class _ActiveSession:
    index: int
    session_id: str
    ws: Any
    accepted: bool = False
    finished: bool = False
    cancel_sent: bool = False

    def can_begin_cancel(self) -> bool:
        return self.accepted and not self.finished and not self.cancel_sent


class ActiveSessionRegistry:
    """跟踪进行中的 WS 会话；退出时对已接受的请求发送 chat.interrupt(cancel)。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _ActiveSession] = {}
        self._lock = asyncio.Lock()

    async def add(self, session: _ActiveSession) -> None:
        async with self._lock:
            self._sessions[session.session_id] = session

    async def remove(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def mark_accepted(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.accepted = True

    async def mark_finished(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.finished = True

    async def try_begin_cancel(self, session_id: str) -> _ActiveSession | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or not session.can_begin_cancel():
                return None
            session.cancel_sent = True
            return session

    async def cancel_all(self, *, wait_ack: float = 3.0) -> int:
        async with self._lock:
            sessions = list(self._sessions.values())
        if not sessions:
            return 0
        results = await asyncio.gather(
            *[
                _send_chat_interrupt_cancel(
                    s.ws,
                    session_id=s.session_id,
                    index=s.index,
                    wait_ack=wait_ack,
                    registry=self,
                )
                for s in sessions
            ],
            return_exceptions=True,
        )
        return sum(1 for item in results if item is True)


async def _send_chat_interrupt_cancel(
    ws: Any,
    *,
    session_id: str,
    index: int,
    wait_ack: float = 3.0,
    registry: ActiveSessionRegistry | None = None,
) -> bool:
    """与 web_enterprise 点击「取消」一致：chat.interrupt intent=cancel。"""
    if registry is not None:
        session = await registry.try_begin_cancel(session_id)
        if session is None:
            return False

    req_id = f"req_cancel_{index:02d}_{uuid.uuid4().hex[:8]}"
    frame = {
        "type": "req",
        "id": req_id,
        "method": "chat.interrupt",
        "params": {"session_id": session_id, "intent": "cancel"},
    }
    try:
        await ws.send(json.dumps(frame, ensure_ascii=False))
        logger.info("[cancel] idx=%d session_id=%s", index, session_id)
        if wait_ack > 0:
            deadline = time.perf_counter() + wait_ack
            while time.perf_counter() < deadline:
                try:
                    remaining = max(0.05, deadline - time.perf_counter())
                    msg = await _recv_json(ws, remaining)
                except asyncio.TimeoutError:
                    break
                if msg.get("type") == "res" and msg.get("id") == req_id:
                    return bool(msg.get("ok"))
        return True
    except Exception as err:
        logger.warning(
            "[cancel-failed] idx=%d session_id=%s err=%s",
            index,
            session_id,
            err,
        )
        return False


def _percentile(sorted_values: list[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int((len(sorted_values) - 1) * ratio)
    return sorted_values[idx]


def _configure_cli_logging() -> None:
    class _TimestampFormatter(logging.Formatter):
        def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
            from datetime import datetime

            dt = datetime.fromtimestamp(record.created)
            base = dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")
            return f"{base}.{int(record.msecs):03d}"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    fmt = _TimestampFormatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.INFO)
    out.setFormatter(fmt)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.ERROR)
    err.setFormatter(fmt)
    root.addHandler(out)
    root.addHandler(err)


def _load_web_port_from_provision(path: Path) -> int:
    raw = json.loads(path.read_text(encoding="utf-8"))
    data = raw.get("data", raw)
    ports = data.get("ports") if isinstance(data, dict) else None
    if not isinstance(ports, dict):
        raise ValueError(f"无法在 {path} 中找到 data.ports")
    web = ports.get("web")
    if web is None:
        raise ValueError(f"无法在 {path} 中找到 data.ports.web")
    return int(web)


def _resolve_ws_url(args: argparse.Namespace) -> str:
    if args.ws_url:
        url = str(args.ws_url).strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise ValueError(f"--ws-url 须为 ws:// 或 wss://，当前 scheme={parsed.scheme!r}")
        if not parsed.netloc:
            raise ValueError(f"--ws-url 无效（缺少 host）: {url!r}")
        return url
    if args.provision_json is not None:
        web_port = _load_web_port_from_provision(args.provision_json)
    else:
        web_port = int(args.web_port)
    return f"ws://{args.host}:{web_port}{args.ws_path}"


def _browser_origin_header(ws_url: str) -> dict[str, str]:
    parsed = urlparse(ws_url)
    host = parsed.hostname or "127.0.0.1"
    http_scheme = "https" if parsed.scheme == "wss" else "http"
    port = parsed.port
    default_port = 443 if http_scheme == "https" else 80
    if port is not None and port != default_port:
        origin = f"{http_scheme}://{host}:{port}"
    else:
        origin = f"{http_scheme}://{host}"
    return {"Origin": origin}


def _with_identity_query(
    ws_url: str,
    *,
    group_id: str,
    bot_id: str,
    user_id: str,
) -> str:
    """在握手 URL 上带上租户身份，与前端 runtimeScope query 一致。"""
    parsed = urlparse(ws_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("group_id", group_id)
    query.setdefault("bot_id", bot_id)
    query.setdefault("user_id", user_id)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _tenant_headers(
    *,
    group_id: str,
    bot_id: str,
    user_id: str,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    headers = dict(extra or {})
    headers["X-Group-Id"] = group_id
    headers["X-Bot-Id"] = bot_id
    headers["X-User-Id"] = user_id
    return headers


def _http_origin_from_ws_url(ws_url: str) -> str:
    """由 WebSocket URL 推导 HTTP Origin（用于相对 download_url）。"""
    return _browser_origin_header(ws_url)["Origin"]


def _absolute_download_url(ws_url: str, download_url: str) -> str:
    url = str(download_url or "").strip()
    if not url:
        raise ValueError("download_url 为空")
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return urljoin(_http_origin_from_ws_url(ws_url) + "/", url.lstrip("/"))


def _indexed_download_filename(name: str, index: int) -> str:
    """并发下载时在文件名后追加请求编号，避免互相覆盖。"""
    path = Path(str(name or "download.bin").strip() or "download.bin")
    return f"{path.stem}_{index:02d}{path.suffix}"


def _extract_downloadable_files(payload: dict[str, Any]) -> list[dict[str, str]]:
    """从 chat.file / tool_result payload 提取带 download_url 的文件项。"""
    raw = payload.get("files")
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        download_url = str(item.get("download_url") or "").strip()
        if not download_url:
            continue
        name = str(item.get("name") or item.get("filename") or Path(download_url).name or "download.bin")
        entries.append({"name": name, "download_url": download_url})
    return entries


def _http_download_to_path(url: str, dest: Path, *, timeout: float = 60.0) -> None:
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — loadtest 下载 Gateway 自身签发的 URL
        data = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


async def _download_deliverable_files(
    *,
    ws_url: str,
    files: list[dict[str, str]],
    index: int,
    download_dir: Path,
) -> list[Path]:
    """把 chat.file 交付物下载到 download_dir，文件名带请求编号。"""
    if not files:
        return []
    download_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for item in files:
        abs_url = _absolute_download_url(ws_url, item["download_url"])
        local_name = _indexed_download_filename(item["name"], index)
        dest = download_dir / local_name
        await asyncio.to_thread(_http_download_to_path, abs_url, dest)
        saved.append(dest)
        logger.info(
            "[download] idx=%d saved=%s bytes=%d url=%s",
            index,
            dest,
            dest.stat().st_size,
            abs_url,
        )
    return saved


def _build_route_plan(
    concurrency: int,
    shards: int,
    shards2: int,
    group_prefix: str,
    user_id_prefix: str,
    *,
    bot_id: str = _DEFAULT_BOT_ID,
    service_shard_key: str = "group_id",
) -> list[RoutePlan]:
    """返回每路路由计划：按 shard 分 AgentServer，user_id 按 shard2 分 Agent 实例。

    ``service_shard_key`` 控制默认 ``service_id`` 路由维度的分片方式（Gateway 默认
    ``service_id = group_id + hash_bucket(bot_id)``）：

    - ``group_id``（默认）：``group_id={prefix}_s{shard}``，``bot_id`` 固定；
    - ``bot_id``：``group_id={prefix}`` 固定，``bot_id={bot_id}_s{shard}``（测 AGENT_BOT_ID_GROUP_NUM）。

    按全局序号轮询分配 shard / shard2，不要求 concurrency 与 shards、shards2 整除；
    不能均分时，序号靠前的分片/实例多承接余数路由。
    """
    if shards <= 0:
        raise ValueError("--shards 须 > 0")
    if shards2 < 0:
        raise ValueError("--shards2 须 >= 0")
    if concurrency <= 0:
        raise ValueError("--concurrency 须 > 0")
    if service_shard_key not in _SERVICE_SHARD_KEYS:
        raise ValueError(f"--service-shard-key 须为 {sorted(_SERVICE_SHARD_KEYS)} 之一")

    plans: list[RoutePlan] = []
    shard_route_counts = [0] * shards
    for global_idx in range(concurrency):
        shard = global_idx % shards
        if service_shard_key == "group_id":
            route_group_id = f"{group_prefix}_s{shard}"
            route_bot_id = bot_id
        else:
            route_group_id = group_prefix
            route_bot_id = f"{bot_id}_s{shard}"
        if shards2 == 0:
            user_id = f"{user_id_prefix}_{global_idx:02d}"
            plan_shard2 = 0
        else:
            shard2 = shard_route_counts[shard] % shards2
            user_id = f"{user_id_prefix}_s{shard}_a{shard2}"
            plan_shard2 = shard2
            shard_route_counts[shard] += 1
        plans.append(
            RoutePlan(
                shard=shard,
                shard2=plan_shard2,
                group_id=route_group_id,
                bot_id=route_bot_id,
                user_id=user_id,
            )
        )
    return plans


async def _recv_json(ws: Any, timeout: float) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError(f"非 JSON 对象: {raw!r}")
    return data


def _normalize_event_frame(frame: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """从 WS event 帧解析 event 名与 payload（兼容 payload 内嵌 event_type）。"""
    if frame.get("type") != "event":
        return None, {}
    event = frame.get("event")
    payload = frame.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    if not event:
        nested = payload.get("event_type") or payload.get("event")
        if isinstance(nested, str):
            event = nested
    return (str(event) if event else None), payload


def _final_content(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if content is None:
        return ""
    return str(content).strip()


def _event_session_id(frame: dict[str, Any], payload: dict[str, Any]) -> str:
    """从 WS event 帧提取 session_id（WebChannel 广播时靠它区分会话）。"""
    for source in (payload, frame):
        if not isinstance(source, dict):
            continue
        sid = source.get("session_id")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
    return ""


def _is_cron_creation_text(content: str) -> bool:
    text = content.strip()
    return bool(text) and any(marker in text for marker in _CRON_CREATION_MARKERS)


def _is_cron_delivery_text(content: str) -> bool:
    """到点投递文案；创建确认里也含提醒正文，须排除。"""
    text = content.strip()
    if not text or _is_cron_creation_text(text):
        return False
    return any(marker in text for marker in _CRON_DELIVERY_MARKERS)


def _matches_cron_delivery(
    *,
    expect_delayed_text: bool,
    content: str,
    logged_cron_creation: bool,
    step_text_buf: str,
) -> bool:
    """delayed-text 步：当前片段或累计缓冲是否已是到点投递文案。"""
    if not expect_delayed_text:
        return False
    if _is_cron_delivery_text(content):
        return True
    return logged_cron_creation and _is_cron_delivery_text(step_text_buf)


def _matches_cron_creation(
    *,
    expect_delayed_text: bool,
    logged_cron_creation: bool,
    content: str,
    step_text_buf: str,
) -> bool:
    """delayed-text 步：尚未记过创建确认，且当前/缓冲命中创建文案。"""
    if not expect_delayed_text or logged_cron_creation:
        return False
    return _is_cron_creation_text(content) or _is_cron_creation_text(step_text_buf)


def _content_marks_step_done(
    *,
    expect_file: bool,
    expect_delayed_text: bool,
    content: str,
) -> bool:
    text = content.strip()
    if not text or expect_file:
        return False
    if expect_delayed_text:
        return _is_cron_delivery_text(text)
    return True


def _tool_name_from_payload(payload: dict[str, Any]) -> str:
    """从 chat.tool_call / chat.tool_result payload 提取工具名。"""
    for key in ("tool_name", "name"):
        val = payload.get(key)
        if val:
            return str(val).strip()
    tool_call = payload.get("tool_call")
    if isinstance(tool_call, dict):
        for key in ("name", "tool_name"):
            val = tool_call.get(key)
            if val:
                return str(val).strip()
    return ""


def _is_send_file_tool_success(payload: dict[str, Any]) -> bool:
    """send_file_to_user 成功时 Gateway 可能只有 tool_result、没有 chat.file。"""
    tool_name = _tool_name_from_payload(payload)
    blob = _payload_text_blob(payload)
    if "send_file" not in tool_name.lower() and "send_file_to_user" not in blob:
        return False
    result = str(payload.get("result") or payload.get("raw_output") or blob)
    if "成功发送" in result:
        return True
    files = payload.get("files")
    return isinstance(files, list) and bool(files)


def _is_intra_turn_chat_final(event: str | None, payload: dict[str, Any]) -> bool:
    """DeepAgent 流式文本 iteration 结束时的空 chat.final，仅标记当前 LLM 轮次完成。"""
    return event == "chat.final" and not _final_content(payload)


def _is_invoke_terminal_event(event: str | None, payload: dict[str, Any]) -> bool:
    """可能是整轮结束的 WS event（仍需结合 HITL 状态判断是否采纳）。"""
    if event == "chat.usage_summary":
        return True
    if event == "chat.final" and not _is_intra_turn_chat_final(event, payload):
        return True
    return False


def _loadtest_terminal_ready(
    *,
    expect_file: bool = True,
    saw_deliverable_file: bool,
    saw_step_text: bool = False,
    saw_post_deliverable_text: bool = False,
) -> bool:
    """单步完成判定：有文件交付的步骤须 chat.file；cron 须收到到点喝水提醒文案。"""
    if expect_file:
        _ = saw_post_deliverable_text
        return saw_deliverable_file
    return saw_step_text or saw_post_deliverable_text


def _should_complete_invoke(
    *,
    expect_file: bool = True,
    accepted: bool,
    saw_agent_output: bool,
    hitl_paused: bool,
    hitl_await_agent_resume: bool,
    saw_deliverable_file: bool,
    saw_step_text: bool = False,
    saw_post_deliverable_text: bool = False,
    event: str | None,
    payload: dict[str, Any],
) -> bool:
    """usage_summary / chat.final 完成判定。"""
    if not accepted or not saw_agent_output:
        return False
    if hitl_paused:
        return False
    terminal_ready = _loadtest_terminal_ready(
        expect_file=expect_file,
        saw_deliverable_file=saw_deliverable_file,
        saw_step_text=saw_step_text,
        saw_post_deliverable_text=saw_post_deliverable_text,
    )
    # 放行后若交付里程碑已达成，usage_summary 可直接收尾（skill_complete 后未必再有 delta）。
    if hitl_await_agent_resume and not (
        terminal_ready
        and event in {"chat.usage_summary", "chat.final"}
    ):
        return False
    if not terminal_ready:
        return False
    return _is_invoke_terminal_event(event, payload)


def _is_processing_idle(payload: dict[str, Any]) -> bool:
    """chat.processing_status 是否表示 Agent 已停止处理（与 web_enterprise 一致）。"""
    if "is_processing" not in payload:
        return False
    return not bool(payload.get("is_processing"))


def _should_complete_on_processing_idle(
    *,
    expect_file: bool = True,
    accepted: bool,
    saw_agent_output: bool,
    hitl_paused: bool,
    hitl_suppress_next_idle: bool,
    hitl_await_agent_resume: bool,
    saw_deliverable_file: bool,
    saw_step_text: bool = False,
    saw_post_deliverable_text: bool = False,
    payload: dict[str, Any],
) -> bool:
    if not accepted or not saw_agent_output:
        return False
    if hitl_paused or hitl_suppress_next_idle:
        return False
    terminal_ready = _loadtest_terminal_ready(
        expect_file=expect_file,
        saw_deliverable_file=saw_deliverable_file,
        saw_step_text=saw_step_text,
        saw_post_deliverable_text=saw_post_deliverable_text,
    )
    if hitl_await_agent_resume and not terminal_ready:
        return False
    if not terminal_ready:
        return False
    return _is_processing_idle(payload)


_RUNTIME_CAPACITY_ERROR_MARKERS = (
    "资源已满",
    "服务并发度超过上限",
    "无足够并发",
    "服务启动失败",
    "100001",
    "100002",
)


def _payload_text_blob(payload: dict[str, Any]) -> str:
    """把 payload 关键为便于关键字扫描的文本。"""
    parts: list[str] = []
    for key in ("error", "message", "content", "detail", "reason", "code", "error_code"):
        val = payload.get(key)
        if val is not None and val != "":
            parts.append(str(val))
    try:
        parts.append(json.dumps(payload, ensure_ascii=False))
    except (TypeError, ValueError):
        parts.append(str(payload))
    return " ".join(parts)


def _extract_runtime_failure(event: str | None, payload: dict[str, Any]) -> str | None:
    """识别应立即失败的运行时错误（含资源打满 100001/100002）。

    Gateway 在资源拒绝时经常只下发带 error 的 chunk / chat.error，或甚至只有
    processing idle；脚本必须显式识别，不能干等到 --final-timeout。
    """
    if not isinstance(payload, dict):
        return None

    code = payload.get("code", payload.get("error_code"))
    code_s = str(code).strip() if code is not None else ""
    blob = _payload_text_blob(payload)
    is_capacity = code_s in {"100001", "100002"} or any(
        marker in blob for marker in _RUNTIME_CAPACITY_ERROR_MARKERS
    )

    if event == "chat.error" or payload.get("error") not in (None, ""):
        err = payload.get("error") or payload.get("message") or blob
        prefix = "capacity_error" if is_capacity else "runtime_error"
        return f"{prefix}: {err}"

    if is_capacity:
        return f"capacity_error: {blob}"

    return None


def _should_fail_on_premature_idle(
    *,
    expect_file: bool = True,
    expect_delayed_text: bool = False,
    accepted: bool,
    hitl_paused: bool,
    hitl_suppress_next_idle: bool,
    hitl_await_agent_resume: bool,
    saw_deliverable_file: bool,
    saw_step_text: bool = False,
    saw_post_deliverable_text: bool = False,
    payload: dict[str, Any],
) -> bool:
    """processing idle 已到，但交付未完成 → 视为失败（避免资源拒绝后挂死）。

    - HITL 等待用户作答（paused 且尚未放行）时仍可能出现 idle，不能当失败。
    - 紧随 auto-allow 的假 idle 由 suppress 分支吞掉，不进入本判断。
    - 放行后仍 await resume、却一直未交付就收到 idle：Agent 流已死，应失败。
    - cron 创建完成后须再等约 1 分钟投递，创建子流的 idle 不算失败。
    """
    if expect_delayed_text:
        return False
    if not accepted or hitl_suppress_next_idle:
        return False
    if hitl_paused and not hitl_await_agent_resume:
        return False
    if _loadtest_terminal_ready(
        expect_file=expect_file,
        saw_deliverable_file=saw_deliverable_file,
        saw_step_text=saw_step_text,
        saw_post_deliverable_text=saw_post_deliverable_text,
    ):
        return False
    return _is_processing_idle(payload)


_AGENT_ACTIVITY_EVENTS = frozenset({
    "chat.delta",
    "chat.tool_call",
    "chat.tool_result",
    "chat.tool_calls.delta",
    "chat.tool_update",
    "todo.updated",
    "chat.file",
    "chat.final",
})

# 权限放行后：chat.delta / chat.file 表示 Agent 已恢复；tool_result 表示工具环继续推进。
_HITL_RESUME_CLEAR_EVENTS = frozenset({
    "chat.delta",
    "chat.file",
    "chat.tool_result",
})


def _log_ws_event(
    *,
    index: int,
    session_id: str,
    frame: dict[str, Any],
    resolved_event: str | None,
    payload: dict[str, Any],
) -> None:
    """打印 WS event 帧，便于对照 frame.event 与 payload.event_type。"""
    frame_event = frame.get("event")
    nested_event_type = payload.get("event_type") if isinstance(payload, dict) else None
    nested_event = payload.get("event") if isinstance(payload, dict) else None
    request_id = frame.get("request_id")
    source = payload.get("source") if isinstance(payload, dict) else None
    parts = [
        f"idx={index}",
        f"session_id={session_id}",
        f"resolved={resolved_event or '<none>'}",
        f"frame.event={frame_event!r}",
    ]
    if nested_event_type is not None:
        parts.append(f"payload.event_type={nested_event_type!r}")
    if nested_event is not None and nested_event != nested_event_type:
        parts.append(f"payload.event={nested_event!r}")
    if request_id:
        parts.append(f"request_id={request_id}")
    if source:
        parts.append(f"source={source!r}")
    logger.info("[ws-event] %s", " ".join(parts))


def _pick_allow_option(options: list[Any]) -> str:
    """从选项列表中选取「允许」类答案，优先「总是允许」。"""
    labels: list[str] = []
    for opt in options:
        if isinstance(opt, dict):
            labels.append(str(opt.get("label") or "").strip())
        elif isinstance(opt, str):
            labels.append(opt.strip())
    prefer = (
        "总是允许",
        "Always allow",
        "Allow always",
        "本次允许",
        "Allow once",
        "Allow",
        "允许",
        "接收",
        "Create",
        "Yes",
        "是",
        "确认",
    )
    deny = frozenset({"拒绝", "Reject", "Deny", "否", "No"})
    for token in prefer:
        for lab in labels:
            if lab == token or token in lab:
                return lab
    for lab in labels:
        if lab and lab not in deny:
            return lab
    return labels[0] if labels else "总是允许"


def _build_allow_answers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """为 ask_user_question / 权限审批构造「全部允许」答案列表。"""
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        return [{"selected_options": ["总是允许"]}]
    answers: list[dict[str, Any]] = []
    for question in questions:
        if not isinstance(question, dict):
            answers.append({"selected_options": ["总是允许"]})
            continue
        options = question.get("options")
        option_list = options if isinstance(options, list) else []
        answers.append({"selected_options": [_pick_allow_option(option_list)]})
    return answers


async def _send_auto_allow(
    ws: Any,
    *,
    index: int,
    session_id: str,
    payload: dict[str, Any],
    bot_id: str,
    group_id: str,
    user_id: str,
    mode: str,
    answered_ids: set[str],
) -> bool:
    """响应 Agent 权限/追问：默认选「总是允许」类选项。"""
    interrupt_request_id = str(payload.get("request_id") or "").strip()
    if not interrupt_request_id or interrupt_request_id in answered_ids:
        return False
    answered_ids.add(interrupt_request_id)

    source = str(payload.get("source") or "").strip()
    answers = _build_allow_answers(payload)
    first_choice = ""
    if answers and isinstance(answers[0], dict):
        opts = answers[0].get("selected_options")
        if isinstance(opts, list) and opts:
            first_choice = str(opts[0])

    if source == "permission_interrupt":
        method = "chat.send"
        params: dict[str, Any] = {
            "session_id": session_id,
            "query": "",
            "content": "",
            "request_id": interrupt_request_id,
            "answers": answers,
            "mode": mode,
            "group_id": group_id,
            "bot_id": bot_id,
            "user_id": user_id,
        }
    else:
        method = "chat.user_answer"
        params = {
            "session_id": session_id,
            "request_id": interrupt_request_id,
            "answers": answers,
        }
        if source:
            params["source"] = source

    approve_req_id = f"req_allow_{index:02d}_{uuid.uuid4().hex[:8]}"
    await ws.send(
        json.dumps(
            {"type": "req", "id": approve_req_id, "method": method, "params": params},
            ensure_ascii=False,
        )
    )
    logger.info(
        "[auto-allow] idx=%d session_id=%s interrupt_request_id=%s method=%s choice=%r",
        index,
        session_id,
        interrupt_request_id,
        method,
        first_choice or "总是允许",
    )
    return True


async def run_single_request(
    *,
    ws_url: str,
    ws_headers: dict[str, str],
    index: int,
    shard: int,
    shard2: int,
    group_id: str,
    bot_id: str,
    user_id: str,
    content: str,
    mode: str,
    accept_timeout: float,
    accept_only: bool,
    final_timeout: float,
    cron_delivery_timeout: float,
    auto_allow: bool,
    ws_event_log: bool,
    progress: ProgressTracker,
    registry: ActiveSessionRegistry,
    steps: tuple[LoadTestStep, ...] | None = None,
    download_dir: Path | None = None,
    session_id: str | None = None,
) -> RequestResult:
    import websockets

    run_download_dir = download_dir or _make_run_download_dir()
    session_id = session_id or f"sess_load_{index:02d}_{uuid.uuid4().hex[:8]}"
    run_steps: tuple[LoadTestStep, ...] = steps or (
        LoadTestStep(name="single", content=content, expect_file=True),
    )
    if accept_only:
        run_steps = run_steps[:1]
    req_id = f"req_load_{index:02d}_{uuid.uuid4().hex[:8]}"
    result = RequestResult(
        index=index,
        shard=shard,
        shard2=shard2,
        session_id=session_id,
        req_id=req_id,
        group_id=group_id,
        bot_id=bot_id,
        user_id=user_id,
        ok=False,
        accepted=False,
        steps_total=len(run_steps),
    )
    logger.info(
        "[send] idx=%d shard=%d shard2=%d session_id=%s steps=%d group_id=%s bot_id=%s user_id=%s",
        index,
        shard,
        shard2,
        session_id,
        len(run_steps),
        group_id,
        bot_id,
        user_id,
    )
    t0 = time.perf_counter()
    deadline = t0 + (accept_timeout if accept_only else final_timeout)

    async def _log_terminal(*, success: bool, event: str, detail: str = "") -> None:
        done_n = await progress.mark_done()
        level = logger.info if success else logger.error
        level(
            "[%s] %d/%d idx=%d shard=%d shard2=%d total_ms=%.0f session_id=%s req_id=%s "
            "group_id=%s user_id=%s steps=%d/%d%s",
            event,
            done_n,
            progress.total,
            index,
            shard,
            shard2,
            result.total_ms,
            session_id,
            req_id,
            group_id,
            user_id,
            result.steps_completed,
            result.steps_total,
            f" {detail}" if detail else "",
        )

    try:
        connect_url = _with_identity_query(
            ws_url, group_id=group_id, bot_id=bot_id, user_id=user_id
        )
        connect_headers = _tenant_headers(
            group_id=group_id,
            bot_id=bot_id,
            user_id=user_id,
            extra=ws_headers,
        )
        async with websockets.connect(
            connect_url,
            open_timeout=15,
            additional_headers=connect_headers,
        ) as ws:
            await registry.add(_ActiveSession(index=index, session_id=session_id, ws=ws))
            try:
                answered_interrupt_ids: set[str] = set()

                for step_idx, step in enumerate(run_steps):
                    expect_file = step.expect_file
                    expect_delayed_text = step.expect_delayed_text
                    req_id = f"req_load_{index:02d}_s{step_idx}_{uuid.uuid4().hex[:8]}"
                    result.req_id = req_id

                    params: dict[str, Any] = {
                        "session_id": session_id,
                        "content": step.content,
                        "query": step.content,
                        "mode": mode,
                        "group_id": group_id,
                        "bot_id": bot_id,
                        "user_id": user_id,
                    }
                    if step.files:
                        params["files"] = [dict(item) for item in step.files]

                    req = {
                        "type": "req",
                        "id": req_id,
                        "method": "chat.send",
                        "params": params,
                    }
                    logger.info(
                        "[send-step] idx=%d step=%d/%d name=%s session_id=%s req_id=%s files=%d",
                        index,
                        step_idx + 1,
                        len(run_steps),
                        step.name,
                        session_id,
                        req_id,
                        len(step.files),
                    )

                    accepted = False
                    hitl_paused = False
                    hitl_suppress_next_idle = False
                    hitl_await_agent_resume = False
                    saw_agent_output = False
                    saw_deliverable_file = False
                    saw_post_deliverable_text = False
                    saw_step_text = False
                    step_text_buf = ""
                    step_done = False
                    logged_cron_creation = False
                    downloaded_paths: list[Path] = []

                    async def _maybe_download_deliverable(
                        payload: dict[str, Any],
                        *,
                        _paths: list[Path],
                        _step=step,
                    ) -> bool:
                        """file 扩写步：下载 chat.file 交付物。成功返回 True；无需下载也返回 True。"""
                        if not _step.download_deliverable or _paths:
                            return True
                        entries = _extract_downloadable_files(payload)
                        if not entries:
                            return True
                        try:
                            paths = await _download_deliverable_files(
                                ws_url=ws_url,
                                files=entries,
                                index=index,
                                download_dir=run_download_dir,
                            )
                            _paths.extend(paths)
                            return True
                        except Exception as download_err:
                            result.error = f"download_failed@{_step.name}: {download_err}"
                            result.failed_step = _step.name
                            result.total_ms = (time.perf_counter() - t0) * 1000
                            await _log_terminal(
                                success=False,
                                event="fail",
                                detail=f"error={result.error}",
                            )
                            return False

                    async def _complete_current_step(
                        *,
                        reason: str,
                        saw_deliverable_file: bool,
                        saw_step_text: bool,
                        saw_post_deliverable_text: bool,
                        _paths: list[Path],
                        _step=step,
                        _step_idx=step_idx,
                        _run_download_dir=run_download_dir,
                    ) -> bool:
                        """标记当前步完成；若需下载但未落盘则失败。返回是否成功完成。"""
                        nonlocal step_done
                        if _step.download_deliverable and not _paths:
                            result.error = (
                                f"download_missing@{_step.name}: 已交付但未下载到本地 "
                                f"(期望目录={_run_download_dir})"
                            )
                            result.failed_step = _step.name
                            result.total_ms = (time.perf_counter() - t0) * 1000
                            await _log_terminal(
                                success=False,
                                event="fail",
                                detail=f"error={result.error}",
                            )
                            return False
                        step_done = True
                        result.steps_completed += 1
                        logger.info(
                            "[step-done] idx=%d step=%d/%d name=%s reason=%s file=%s text=%s post=%s "
                            "downloaded=%d",
                            index,
                            _step_idx + 1,
                            len(run_steps),
                            _step.name,
                            reason,
                            saw_deliverable_file,
                            saw_step_text,
                            saw_post_deliverable_text,
                            len(_paths),
                        )
                        return True

                    await ws.send(json.dumps(req, ensure_ascii=False))

                    while time.perf_counter() < deadline and not step_done:
                        remaining = max(0.1, deadline - time.perf_counter())
                        try:
                            frame = await _recv_json(ws, remaining)
                        except asyncio.TimeoutError:
                            break

                        ftype = frame.get("type")

                        if ftype == "res" and frame.get("id") == req_id:
                            ok = bool(frame.get("ok"))
                            payload = frame.get("payload") or {}
                            result.ok = ok
                            if not ok:
                                err = frame.get("error") or payload.get("error") or frame
                                result.error = json.dumps(err, ensure_ascii=False)
                                result.failed_step = step.name
                                result.total_ms = (time.perf_counter() - t0) * 1000
                                await _log_terminal(
                                    success=False,
                                    event="fail",
                                    detail=f"step={step.name} error={result.error}",
                                )
                                return result
                            accepted = bool(payload.get("accepted", True))
                            result.accepted = accepted
                            if result.accept_ms <= 0:
                                result.accept_ms = (time.perf_counter() - t0) * 1000
                            if accepted:
                                await registry.mark_accepted(session_id)
                            if not accepted:
                                result.error = "chat.send 未被接受"
                                result.failed_step = step.name
                                result.total_ms = (time.perf_counter() - t0) * 1000
                                await _log_terminal(
                                    success=False,
                                    event="reject",
                                    detail=f"step={step.name}",
                                )
                                return result
                            logger.info(
                                "[accepted] idx=%d step=%d/%d name=%s accept_ms=%.0f session_id=%s req_id=%s",
                                index,
                                step_idx + 1,
                                len(run_steps),
                                step.name,
                                result.accept_ms,
                                session_id,
                                req_id,
                            )
                            if accept_only:
                                result.ok = True
                                result.final_received = False
                                result.steps_completed = 1
                                result.total_ms = (time.perf_counter() - t0) * 1000
                                await _log_terminal(success=True, event="done", detail=f"step={step.name}")
                                return result
                            continue

                        if ftype != "event":
                            continue

                        event, payload = _normalize_event_frame(frame)
                        event_sid = _event_session_id(frame, payload)
                        # WebChannel 向所有连接广播；并发压测必须忽略其他 session 的事件。
                        if event_sid and event_sid != session_id:
                            if ws_event_log:
                                logger.info(
                                    "[ws-event] idx=%d session_id=%s skip foreign session_id=%s event=%s",
                                    index,
                                    session_id,
                                    event_sid,
                                    event,
                                )
                            continue
                        if ws_event_log:
                            _log_ws_event(
                                index=index,
                                session_id=session_id,
                                frame=frame,
                                resolved_event=event,
                                payload=payload,
                            )
                        failure = _extract_runtime_failure(event, payload)
                        if failure:
                            result.error = failure
                            result.failed_step = step.name
                            result.total_ms = (time.perf_counter() - t0) * 1000
                            await _log_terminal(
                                success=False,
                                event="fail",
                                detail=f"step={step.name} error={result.error}",
                            )
                            return result
                        if auto_allow and event == "chat.ask_user_question":
                            allowed = await _send_auto_allow(
                                ws,
                                index=index,
                                session_id=session_id,
                                payload=payload,
                                bot_id=bot_id,
                                group_id=group_id,
                                user_id=user_id,
                                mode=mode,
                                answered_ids=answered_interrupt_ids,
                            )
                            if allowed:
                                hitl_suppress_next_idle = True
                                hitl_await_agent_resume = True
                                hitl_paused = False
                            continue
                        if event == "chat.ask_user_question":
                            hitl_paused = True
                            continue
                        if event == "chat.invocation_paused":
                            hitl_paused = True
                            hitl_suppress_next_idle = True
                            hitl_await_agent_resume = True
                            continue
                        if event in _AGENT_ACTIVITY_EVENTS:
                            saw_agent_output = True
                            hitl_suppress_next_idle = False
                            if event in _HITL_RESUME_CLEAR_EVENTS:
                                hitl_await_agent_resume = False
                            if event == "chat.file":
                                hitl_paused = False
                                hitl_await_agent_resume = False
                                saw_deliverable_file = True
                                if not await _maybe_download_deliverable(
                                    payload, _paths=downloaded_paths
                                ):
                                    return result
                            elif event == "chat.tool_result" and _is_send_file_tool_success(payload):
                                hitl_paused = False
                                hitl_await_agent_resume = False
                                saw_deliverable_file = True
                                if not await _maybe_download_deliverable(
                                    payload, _paths=downloaded_paths
                                ):
                                    return result
                            elif event == "chat.delta":
                                content = _final_content(payload)
                                if expect_delayed_text and content:
                                    step_text_buf += content
                                if expect_file and saw_deliverable_file:
                                    saw_post_deliverable_text = True
                                elif _matches_cron_delivery(
                                    expect_delayed_text=expect_delayed_text,
                                    content=content,
                                    logged_cron_creation=logged_cron_creation,
                                    step_text_buf=step_text_buf,
                                ):
                                    saw_step_text = True
                                    logger.info(
                                        "[cron-delivery] idx=%d step=%d/%d name=%s session_id=%s",
                                        index,
                                        step_idx + 1,
                                        len(run_steps),
                                        step.name,
                                        session_id,
                                    )
                                    if not await _complete_current_step(
                                        reason="cron_delivery",
                                        saw_deliverable_file=saw_deliverable_file,
                                        saw_step_text=saw_step_text,
                                        saw_post_deliverable_text=saw_post_deliverable_text,
                                        _paths=downloaded_paths,
                                    ):
                                        return result
                                    break
                                elif _matches_cron_creation(
                                    expect_delayed_text=expect_delayed_text,
                                    logged_cron_creation=logged_cron_creation,
                                    content=content,
                                    step_text_buf=step_text_buf,
                                ):
                                    logged_cron_creation = True
                                    step_text_buf = ""
                                    if cron_delivery_timeout > 0:
                                        deadline = min(
                                            deadline,
                                            time.perf_counter() + cron_delivery_timeout,
                                        )
                                    logger.info(
                                        "[cron-wait] idx=%d step=%d/%d name=%s session_id=%s "
                                        "creation confirmed, waiting for delivery "
                                        "(timeout=%.0fs)...",
                                        index,
                                        step_idx + 1,
                                        len(run_steps),
                                        step.name,
                                        session_id,
                                        cron_delivery_timeout if cron_delivery_timeout > 0 else final_timeout,
                                    )
                                elif _content_marks_step_done(
                                    expect_file=expect_file,
                                    expect_delayed_text=expect_delayed_text,
                                    content=content,
                                ):
                                    saw_step_text = True
                            elif (
                                event == "chat.final"
                                and not _is_intra_turn_chat_final(event, payload)
                            ):
                                content = _final_content(payload)
                                if expect_delayed_text and content:
                                    step_text_buf += content
                                if expect_file and saw_deliverable_file:
                                    saw_post_deliverable_text = True
                                elif _matches_cron_delivery(
                                    expect_delayed_text=expect_delayed_text,
                                    content=content,
                                    logged_cron_creation=logged_cron_creation,
                                    step_text_buf=step_text_buf,
                                ):
                                    saw_step_text = True
                                    logger.info(
                                        "[cron-delivery] idx=%d step=%d/%d name=%s session_id=%s",
                                        index,
                                        step_idx + 1,
                                        len(run_steps),
                                        step.name,
                                        session_id,
                                    )
                                    if not await _complete_current_step(
                                        reason="cron_delivery",
                                        saw_deliverable_file=saw_deliverable_file,
                                        saw_step_text=saw_step_text,
                                        saw_post_deliverable_text=saw_post_deliverable_text,
                                        _paths=downloaded_paths,
                                    ):
                                        return result
                                    break
                                elif _matches_cron_creation(
                                    expect_delayed_text=expect_delayed_text,
                                    logged_cron_creation=logged_cron_creation,
                                    content=content,
                                    step_text_buf=step_text_buf,
                                ):
                                    logged_cron_creation = True
                                    step_text_buf = ""
                                    if cron_delivery_timeout > 0:
                                        deadline = min(
                                            deadline,
                                            time.perf_counter() + cron_delivery_timeout,
                                        )
                                    logger.info(
                                        "[cron-wait] idx=%d step=%d/%d name=%s session_id=%s "
                                        "creation confirmed, waiting for delivery "
                                        "(timeout=%.0fs)...",
                                        index,
                                        step_idx + 1,
                                        len(run_steps),
                                        step.name,
                                        session_id,
                                        cron_delivery_timeout if cron_delivery_timeout > 0 else final_timeout,
                                    )
                                elif _content_marks_step_done(
                                    expect_file=expect_file,
                                    expect_delayed_text=expect_delayed_text,
                                    content=content,
                                ):
                                    saw_step_text = True
                        if event == "chat.processing_status":
                            if payload.get("is_processing") is True:
                                hitl_paused = False
                                hitl_suppress_next_idle = False
                                hitl_await_agent_resume = False
                            elif _is_processing_idle(payload):
                                if hitl_suppress_next_idle:
                                    hitl_suppress_next_idle = False
                                    hitl_paused = False
                                    continue
                                if _should_complete_on_processing_idle(
                                    expect_file=expect_file,
                                    accepted=accepted,
                                    saw_agent_output=saw_agent_output,
                                    hitl_paused=hitl_paused,
                                    hitl_suppress_next_idle=False,
                                    hitl_await_agent_resume=hitl_await_agent_resume,
                                    saw_deliverable_file=saw_deliverable_file,
                                    saw_step_text=saw_step_text,
                                    saw_post_deliverable_text=saw_post_deliverable_text,
                                    payload=payload,
                                ):
                                    if not await _complete_current_step(
                                        reason="processing_status_idle",
                                        saw_deliverable_file=saw_deliverable_file,
                                        saw_step_text=saw_step_text,
                                        saw_post_deliverable_text=saw_post_deliverable_text,
                                        _paths=downloaded_paths,
                                    ):
                                        return result
                                    break
                                if _should_fail_on_premature_idle(
                                    expect_file=expect_file,
                                    expect_delayed_text=expect_delayed_text,
                                    accepted=accepted,
                                    hitl_paused=hitl_paused,
                                    hitl_suppress_next_idle=False,
                                    hitl_await_agent_resume=hitl_await_agent_resume,
                                    saw_deliverable_file=saw_deliverable_file,
                                    saw_step_text=saw_step_text,
                                    saw_post_deliverable_text=saw_post_deliverable_text,
                                    payload=payload,
                                ):
                                    result.error = (
                                        f"premature_idle@{step.name}: Agent 已 is_processing=false，但未完成交付 "
                                        f"(file={saw_deliverable_file} text={saw_step_text} "
                                        f"post={saw_post_deliverable_text} "
                                        f"agent_output={saw_agent_output} "
                                        f"hitl_await={hitl_await_agent_resume} hitl_paused={hitl_paused})；"
                                        "常见于资源已满(100001)/无法预留 session(100002) 后流提前结束"
                                    )
                                    result.failed_step = step.name
                                    result.total_ms = (time.perf_counter() - t0) * 1000
                                    await _log_terminal(
                                        success=False,
                                        event="fail",
                                        detail=f"error={result.error}",
                                    )
                                    return result
                            continue
                        if _is_intra_turn_chat_final(event, payload):
                            if ws_event_log:
                                logger.info(
                                    "[ws-event] idx=%d session_id=%s skip intra-turn chat.final "
                                    "(empty content; waiting for terminal usage_summary / chat.final / idle)",
                                    index,
                                    session_id,
                                )
                            continue
                        if _should_complete_invoke(
                            expect_file=expect_file,
                            accepted=accepted,
                            saw_agent_output=saw_agent_output,
                            hitl_paused=hitl_paused,
                            hitl_await_agent_resume=hitl_await_agent_resume,
                            saw_deliverable_file=saw_deliverable_file,
                            saw_step_text=saw_step_text,
                            saw_post_deliverable_text=saw_post_deliverable_text,
                            event=event,
                            payload=payload,
                        ):
                            if not await _complete_current_step(
                                reason=str(event),
                                saw_deliverable_file=saw_deliverable_file,
                                saw_step_text=saw_step_text,
                                saw_post_deliverable_text=saw_post_deliverable_text,
                                _paths=downloaded_paths,
                            ):
                                return result
                            break

                    if not step_done:
                        if not accepted:
                            result.error = f"超时@{step.name}：未收到 chat.send 确认"
                        elif (
                            expect_delayed_text
                            and logged_cron_creation
                            and not saw_step_text
                        ):
                            result.error = (
                                f"cron_delivery_timeout@{step.name}: 已创建但未收到投递"
                            )
                        else:
                            result.error = (
                                f"超时@{step.name}：已接受但未收到步骤完成信号 "
                                f"(expect_file={expect_file} expect_delayed_text={expect_delayed_text} "
                                f"file={saw_deliverable_file} "
                                f"text={saw_step_text} post={saw_post_deliverable_text} "
                                f"hitl_await={hitl_await_agent_resume} hitl_paused={hitl_paused})"
                            )
                        result.failed_step = step.name
                        result.total_ms = (time.perf_counter() - t0) * 1000
                        await _log_terminal(success=False, event="timeout", detail=result.error)
                        return result

                result.final_received = True
                result.ok = True
                result.accepted = True
                result.total_ms = (time.perf_counter() - t0) * 1000
                await _log_terminal(
                    success=True,
                    event="done",
                    detail=f"all_steps={result.steps_completed}/{result.steps_total}",
                )
                return result
            except asyncio.CancelledError:
                if result.accepted and not result.final_received:
                    await _send_chat_interrupt_cancel(
                        ws,
                        session_id=session_id,
                        index=index,
                        registry=registry,
                    )
                raise
            finally:
                await registry.mark_finished(session_id)
                await registry.remove(session_id)
    except Exception as err:
        result.error = str(err)
        result.total_ms = (time.perf_counter() - t0) * 1000
        await _log_terminal(success=False, event="fail", detail=f"exception={result.error}")
        return result


async def _run_loadtest(args: argparse.Namespace) -> int:
    ws_url = _resolve_ws_url(args)
    ws_headers = _browser_origin_header(ws_url)
    route_plan = _build_route_plan(
        args.concurrency,
        args.shards,
        args.shards2,
        args.group_prefix,
        args.user_id_prefix,
        bot_id=args.bot_id,
        service_shard_key=args.service_shard_key,
    )

    loadtest_steps: tuple[LoadTestStep, ...] | None = None
    if args.flow == "loadtest":
        loadtest_steps = build_default_loadtest_steps(args.essay_file)
    run_download_dir = _make_run_download_dir()
    need_download = bool(
        loadtest_steps and any(step.download_deliverable for step in loadtest_steps)
    )

    logger.info(
        "[plan] ws=%s concurrency=%d shards=%d shards2=%d service_shard_key=%s flow=%s",
        ws_url,
        args.concurrency,
        args.shards,
        args.shards2,
        args.service_shard_key,
        args.flow,
    )
    if loadtest_steps:
        logger.info(
            "[plan] loadtest steps=%s essay_file=%s",
            " -> ".join(step.name for step in loadtest_steps),
            args.essay_file,
        )
    else:
        logger.info("[plan] content=%r", args.content)
    if need_download:
        logger.info("[plan] download_dir=%s", run_download_dir)
    logger.info(
        "[plan] accept_only=%s auto_allow=%s ws_event_log=%s accept_timeout=%ss "
        "final_timeout=%ss cron_delivery_timeout=%ss",
        args.accept_only,
        args.auto_allow,
        args.ws_event_log,
        args.accept_timeout,
        args.final_timeout,
        args.cron_delivery_timeout,
    )
    for shard in range(args.shards):
        indices = [i for i, plan in enumerate(route_plan) if plan.shard == shard]
        if not indices:
            continue
        group_id = route_plan[indices[0]].group_id
        bot_id = route_plan[indices[0]].bot_id
        logger.info(
            "[plan] shard=%d group_id=%s bot_id=%s requests=%d (idx %d..%d)",
            shard,
            group_id,
            bot_id,
            len(indices),
            indices[0],
            indices[-1],
        )
        if args.shards2 > 0:
            for shard2 in range(args.shards2):
                sub = [i for i in indices if route_plan[i].shard2 == shard2]
                if not sub:
                    continue
                logger.info(
                    "[plan] shard=%d shard2=%d user_id=%s requests=%d (idx %d..%d)",
                    shard,
                    shard2,
                    route_plan[sub[0]].user_id,
                    len(sub),
                    sub[0],
                    sub[-1],
                )

    progress = ProgressTracker(total=args.concurrency)
    registry = ActiveSessionRegistry()
    t0 = time.perf_counter()
    task_objs = [
        asyncio.create_task(
            run_single_request(
                ws_url=ws_url,
                ws_headers=ws_headers,
                index=idx,
                shard=plan.shard,
                shard2=plan.shard2,
                group_id=plan.group_id,
                bot_id=plan.bot_id,
                user_id=plan.user_id,
                content=args.content,
                mode=args.mode,
                accept_timeout=args.accept_timeout,
                accept_only=args.accept_only,
                final_timeout=args.final_timeout,
                cron_delivery_timeout=args.cron_delivery_timeout,
                auto_allow=args.auto_allow,
                ws_event_log=args.ws_event_log,
                progress=progress,
                registry=registry,
                steps=loadtest_steps,
                download_dir=run_download_dir if need_download else None,
            )
        )
        for idx, plan in enumerate(route_plan)
    ]
    try:
        raw_results = await asyncio.gather(*task_objs, return_exceptions=True)
    except asyncio.CancelledError:
        logger.info("[shutdown] 收到中断信号，正在 cancel 进行中的会话（chat.interrupt）...")
        cancelled = await registry.cancel_all()
        logger.info("[shutdown] 已向 %d 路会话发送 cancel", cancelled)
        for task in task_objs:
            task.cancel()
        await asyncio.gather(*task_objs, return_exceptions=True)
        raise
    elapsed = time.perf_counter() - t0

    results: list[RequestResult] = []
    for idx, item in enumerate(raw_results):
        if isinstance(item, Exception):
            plan = route_plan[idx]
            fail_result = RequestResult(
                index=idx,
                shard=plan.shard,
                shard2=plan.shard2,
                session_id="",
                req_id="",
                group_id=plan.group_id,
                bot_id=plan.bot_id,
                user_id=plan.user_id,
                ok=False,
                accepted=False,
                error=str(item),
            )
            done_n = await progress.mark_done()
            logger.error(
                "[fail] %d/%d idx=%d shard=%d shard2=%d session_id= req_id= group_id=%s user_id=%s "
                "exception=%s",
                done_n,
                progress.total,
                idx,
                plan.shard,
                plan.shard2,
                plan.group_id,
                fail_result.user_id,
                fail_result.error,
            )
            results.append(fail_result)
        else:
            results.append(item)

    def _is_success(r: RequestResult) -> bool:
        if args.accept_only:
            return r.accepted and r.ok
        return r.final_received and r.ok

    completed = sum(1 for r in results if _is_success(r))
    failed = args.concurrency - completed
    stats = LoadTestStats(
        total=args.concurrency,
        completed=completed,
        failed=failed,
        elapsed_s=elapsed,
        accept_ms=[r.accept_ms for r in results if r.accept_ms > 0],
        total_ms=[r.total_ms for r in results if r.total_ms > 0],
    )

    logger.info("\n[requests] 各请求路由参数汇总（按 idx 排序）:")
    for r in sorted(results, key=lambda x: x.index):
        status = "ok" if _is_success(r) else "fail"
        logger.info(
            "[requests] idx=%02d shard=%d shard2=%d status=%s final=%s steps=%d/%d failed_step=%s "
            "total_ms=%.0f accept_ms=%.0f session_id=%s req_id=%s group_id=%s bot_id=%s user_id=%s",
            r.index,
            r.shard,
            r.shard2,
            status,
            r.final_received,
            r.steps_completed,
            r.steps_total,
            r.failed_step or "-",
            r.total_ms,
            r.accept_ms,
            r.session_id,
            r.req_id,
            r.group_id,
            r.bot_id,
            r.user_id,
        )

    shard_counts: dict[int, list[RequestResult]] = {s: [] for s in range(args.shards)}
    for r in results:
        shard_counts[r.shard].append(r)
    for shard in range(args.shards):
        shard_ok = sum(1 for r in shard_counts[shard] if _is_success(r))
        logger.info("[shard] shard=%d completed=%d/%d", shard, shard_ok, len(shard_counts[shard]))

    if args.shards2 > 0:
        agent_buckets: dict[tuple[int, int], list[RequestResult]] = {}
        for r in results:
            agent_buckets.setdefault((r.shard, r.shard2), []).append(r)
        for (shard, shard2), bucket in sorted(agent_buckets.items()):
            ok_n = sum(1 for r in bucket if _is_success(r))
            logger.info(
                "[shard2] shard=%d shard2=%d user_id=%s completed=%d/%d",
                shard,
                shard2,
                bucket[0].user_id if bucket else "",
                ok_n,
                len(bucket),
            )

    logger.info("\n[result] %s", stats.summary())

    return 0 if failed == 0 else 1


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Gateway Runtime 并发 chat.send 压测（经 WebChannel /ws 验证 AgentServer 池）",
    )
    p.add_argument("--host", default="127.0.0.1", help="Gateway 主机，默认 127.0.0.1")
    p.add_argument("--ws-path", default="/ws", help="WebSocket 路径，默认 /ws")
    p.add_argument(
        "--flow",
        choices=("loadtest", "single"),
        default="loadtest",
        help=(
            "loadtest（默认）：每路会话依次发送 travel/skill/file/cron 四条消息，"
            "与 mock_llm_server --profile loadtest 对齐；single：仅发送一条 --content"
        ),
    )
    p.add_argument(
        "--content",
        default=_DEFAULT_CONTENT,
        help="--flow single 时的用户消息正文（同时写入 content 与 query）",
    )
    p.add_argument(
        "--essay-file",
        type=Path,
        default=_DEFAULT_SPRING_ESSAY,
        help="loadtest 第 3 步上传的作文附件路径，默认 scripts/童趣的春天.md",
    )
    p.add_argument(
        "--bot-id",
        default=_DEFAULT_BOT_ID,
        help=(
            "企业策略 bot_id 基名；service-shard-key=bot_id 时为 {bot_id}_s{shard}"
        ),
    )
    p.add_argument("--user-id-prefix", default="loadtest_user", help="user_id 前缀，实际为 {prefix}_{idx:02d}")
    p.add_argument("--mode", default="agent.plan", help="运行模式，如 agent.plan")
    p.add_argument(
        "--concurrency",
        "--total",
        dest="concurrency",
        type=int,
        default=30,
        metavar="N",
        help="总并发请求数，默认 30（按 shards / shards2 轮询分发，不要求整除）",
    )
    p.add_argument(
        "--shards",
        "--agent-shards",
        dest="shards",
        type=int,
        default=3,
        metavar="K",
        help="轮询分布到的 AgentServer 数量，默认 3",
    )
    p.add_argument(
        "--shards2",
        "--agent-instance-shards",
        dest="shards2",
        type=int,
        default=0,
        metavar="M",
        help=(
            "同一 AgentServer 内按 user_id 轮询分布到的 Agent 实例数，默认 0（每路独立 "
            "{user_id_prefix}_{idx:02d}）；M>=1 时 user_id 为 {user_id_prefix}_s{shard}_a{j}，"
            "同桶多路共用；M=1 时同一 AgentServer 内全部打到同一 Agent 实例；"
            "不要求整除，余数由序号靠前的实例多承接"
        ),
    )
    p.add_argument(
        "--group-prefix",
        default="loadtest",
        help="group_id 前缀；service-shard-key=group_id 时为 {prefix}_s{shard}，=bot_id 时固定为 {prefix}",
    )
    p.add_argument(
        "--service-shard-key",
        choices=sorted(_SERVICE_SHARD_KEYS),
        default="group_id",
        help=(
            "默认 service_id 路由分片维度：group_id（每 shard 不同 group_id，bot_id 固定，默认）或 "
            "bot_id（group_id 固定，每 shard 不同 bot_id，用于 AGENT_BOT_ID_GROUP_NUM 联调）"
        ),
    )
    p.add_argument(
        "--accept-timeout",
        type=float,
        default=60.0,
        help="等待 chat.send 被接受的最长时间（秒），默认 60",
    )
    p.add_argument(
        "--accept-only",
        action="store_true",
        help="仅等待 chat.send 被接受，不等待任务完成（默认会等到 usage_summary / final / processing idle）",
    )
    p.add_argument(
        "--no-auto-allow",
        action="store_true",
        help="禁用自动响应 Agent 权限/追问（默认自动选「总是允许」）",
    )
    p.add_argument(
        "--ws-event-log",
        action="store_true",
        help="打印每路 WS event 名（frame.event / payload.event_type），排查权限事件是否到达",
    )
    p.add_argument(
        "--final-timeout",
        type=float,
        default=7200.0,
        help="等待任务完成的最长时间（秒），默认 7200",
    )
    p.add_argument(
        "--cron-delivery-timeout",
        type=float,
        default=120.0,
        help="cron 步创建确认后等待到点投递的最长时间（秒），默认 120",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--web-port", type=int, help="Gateway WebChannel 端口")
    src.add_argument("--provision-json", type=Path, help="provision-local 响应 JSON（读取 data.ports.web）")
    src.add_argument("--ws-url", help="完整 WebSocket URL，如 ws://host:19001/ws")
    args = p.parse_args()
    args.auto_allow = not args.no_auto_allow
    return args


def main() -> int:
    _configure_cli_logging()
    try:
        import websockets  # noqa: F401
    except ImportError:
        logger.error(
            "缺少 websockets，请在 jiuwenclaw 仓库根目录执行: uv sync 或 pip install websockets"
        )
        return 1

    args = _parse_args()
    try:
        return asyncio.run(_run_loadtest(args))
    except KeyboardInterrupt:
        return 130
    except ValueError as err:
        logger.error("[invalid-args] %s", err)
        return 2
    except OSError as connect_err:
        logger.error("[connect-failed] %s", connect_err)
        logger.error(
            "请确认 Gateway 已启动，且 --web-port / --provision-json / --ws-url 指向可访问的 WebChannel。"
        )
        return 1
    except Exception as err:
        logger.error("[failed] %s", err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
