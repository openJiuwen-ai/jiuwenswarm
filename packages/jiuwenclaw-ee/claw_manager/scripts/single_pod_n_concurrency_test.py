#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""单 AgentServer Pod、N 路会话数据面压测。

对齐 ``dev/enterprise_kub`` 的 WebChannel ``chat.send`` 压测方式，以及
``single-pod-n-concurrency`` 规格的 SP-02 / SP-12 主路径：

- 1 个 AgentServer（同一 ``group_id`` + 同一 ``bot_id``）
- N 个不同 ``user_id``（``u_1_<run>`` … ``u_N_<run>``）→ N 个工作区
- N 个不同 ``session_id``（``sess_1_<run>`` … ``sess_N_<run>``）→ N 个执行器
- 齐发 N 路 ``chat.send``，断言每路被接受且对话跑完

``session_id`` 用下划线 ``sess_*``，以便现成 Mock LLM 按会话拆状态；``<run>`` 默认每轮
随机 8 位 hex，避免复用 MySQL checkpoint / 工作区导致 loadtest 被当成上一轮收尾。

默认 ``--flow single``：每路一条短消息，配合 Mock LLM 验证功能是否正常。
``--flow loadtest`` 时复用 ``enterprise_runtime_concurrent_test.py`` 的
travel → skill → file → cron 四步（需 ``mock_llm_server.py --profile loadtest``）。

典型用法（jiuwenswarm 仓库根目录）::

    # 当前 mz 环境：Web NodePort 30086，N=8
    .venv/bin/python packages/jiuwenclaw-ee/claw_manager/scripts/single_pod_n_concurrency_test.py \\
        --host 192.168.1.90 --web-port 30086 --concurrency 8

    # 完整四步 loadtest
    .venv/bin/python packages/jiuwenclaw-ee/claw_manager/scripts/single_pod_n_concurrency_test.py \\
        --ws-url ws://192.168.1.90:30086/ws --concurrency 8 --flow loadtest
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_SCRIPT_DIR = Path(__file__).resolve().parent
_CONCURRENT_SCRIPT = _SCRIPT_DIR / "enterprise_runtime_concurrent_test.py"
_DOWNLOAD_DIR_PREFIX = _SCRIPT_DIR / "download"

logger = logging.getLogger(__name__)

_DEFAULT_SINGLE_CONTENT = "请只回复：mock-ok。不要调用任何工具，不要提问。"


def _load_concurrent_mod():
    spec = importlib.util.spec_from_file_location(
        "enterprise_runtime_concurrent_test", _CONCURRENT_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {_CONCURRENT_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _resolve_run_id(run_id: str | None) -> str:
    text = (run_id or "").strip()
    if text:
        return text
    return uuid.uuid4().hex[:8]


def build_identities(
    concurrency: int,
    *,
    group_id: str,
    bot_id: str,
    user_prefix: str,
    session_prefix: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """SP-02 身份：每路独立 user_id / session_id，共享 group_id / bot_id。

    使用 ``{prefix}_{i}_{run_id}``（下划线）。``sess_*`` 能被 Mock LLM 现有正则识别，
    每轮不同的 ``run_id`` 避免撞上上一轮 checkpoint。
    """
    if concurrency <= 0:
        raise ValueError("--concurrency 须 > 0")
    if not str(run_id).strip():
        raise ValueError("run_id 不能为空")
    suffix = str(run_id).strip()
    return [
        {
            "index": i,
            "group_id": group_id,
            "bot_id": bot_id,
            "user_id": f"{user_prefix}_{i}_{suffix}",
            "session_id": f"{session_prefix}_{i}_{suffix}",
        }
        for i in range(1, concurrency + 1)
    ]


def _resolve_ws_url(args: argparse.Namespace) -> str:
    if args.ws_url:
        url = str(args.ws_url).strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise ValueError(f"--ws-url 须为 ws:// 或 wss://，当前 scheme={parsed.scheme!r}")
        if not parsed.netloc:
            raise ValueError(f"--ws-url 无效（缺少 host）: {url!r}")
        return url
    return f"ws://{args.host}:{args.web_port}{args.ws_path}"


def _browser_origin_header(ws_url: str) -> dict[str, str]:
    """由 WebSocket URL 推导浏览器 Origin 头。"""
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


def _make_run_download_dir(*, when: time.struct_time | None = None) -> Path:
    """生成本次压测专用下载目录：download_YYYYMMDD_HHMMSS。"""
    stamp = time.strftime("%Y%m%d_%H%M%S", when or time.localtime())
    return Path(f"{_DOWNLOAD_DIR_PREFIX}_{stamp}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="单 AgentServer Pod N 并发 chat.send（不同 user_id + session_id）",
    )
    p.add_argument("--host", default="192.168.1.90", help="Gateway / Web Node 地址")
    p.add_argument("--web-port", type=int, default=30086, help="Web NodePort，默认 30086")
    p.add_argument("--ws-path", default="/ws", help="WebSocket 路径，默认 /ws")
    p.add_argument("--ws-url", help="完整 WebSocket URL，设置后忽略 --host/--web-port")
    p.add_argument(
        "--concurrency",
        "-n",
        type=int,
        default=8,
        metavar="N",
        help="并发会话数，默认 8（每路一个 user_id + 一个 session_id）",
    )
    p.add_argument("--group-id", default="load-g", help="全员同一 group_id，默认 load-g")
    p.add_argument(
        "--bot-id",
        default="bot_main",
        help="全员同一 bot_id，默认 bot_main（须与 Manager 里已配置的智能体一致）",
    )
    p.add_argument(
        "--user-prefix",
        default="u",
        help="user_id 形如 {prefix}_1_<run-id> … {prefix}_N_<run-id>",
    )
    p.add_argument(
        "--session-prefix",
        default="sess",
        help="session_id 形如 {prefix}_1_<run-id> … {prefix}_N_<run-id>（须为 sess_ 以便 Mock 识别）",
    )
    p.add_argument(
        "--run-id",
        default="",
        help="本轮身份后缀，默认自动生成 8 位 hex；显式传入可复现同一批 user/session",
    )
    p.add_argument(
        "--flow",
        choices=("single", "loadtest"),
        default="single",
        help="single：每路一条短消息（默认）；loadtest：travel/skill/file/cron 四步",
    )
    p.add_argument(
        "--content",
        default=_DEFAULT_SINGLE_CONTENT,
        help="--flow single 时的用户消息",
    )
    p.add_argument("--mode", default="agent.fast", help="运行模式，默认 agent.fast")
    p.add_argument(
        "--accept-timeout",
        type=float,
        default=60.0,
        help="等待 chat.send 被接受的最长时间（秒）",
    )
    p.add_argument(
        "--final-timeout",
        type=float,
        default=180.0,
        help="--flow single 等待整轮完成的最长时间（秒），默认 180",
    )
    p.add_argument(
        "--loadtest-final-timeout",
        type=float,
        default=7200.0,
        help="--flow loadtest 等待整轮完成的最长时间（秒）",
    )
    p.add_argument(
        "--cron-delivery-timeout",
        type=float,
        default=120.0,
        help="loadtest cron 步等待到点投递的最长时间（秒）",
    )
    p.add_argument(
        "--accept-only",
        action="store_true",
        help="只等 Gateway 接受，不等 Agent 跑完",
    )
    p.add_argument("--no-auto-allow", action="store_true", help="禁用权限弹窗自动放行")
    p.add_argument("--ws-event-log", action="store_true", help="打印每路 WS event 名")
    p.add_argument(
        "--essay-file",
        type=Path,
        default=_SCRIPT_DIR / "童趣的春天.md",
        help="loadtest 作文附件路径",
    )
    return p.parse_args()


async def _run(args: argparse.Namespace) -> int:
    mod = _load_concurrent_mod()
    run_id = _resolve_run_id(args.run_id)
    identities = build_identities(
        args.concurrency,
        group_id=args.group_id,
        bot_id=args.bot_id,
        user_prefix=args.user_prefix,
        session_prefix=args.session_prefix,
        run_id=run_id,
    )
    ws_url = _resolve_ws_url(args)
    ws_headers = _browser_origin_header(ws_url)
    final_timeout = (
        args.loadtest_final_timeout if args.flow == "loadtest" else args.final_timeout
    )

    loadtest_steps = None
    single_steps = None
    if args.flow == "loadtest":
        loadtest_steps = mod.build_default_loadtest_steps(args.essay_file)
    else:
        single_steps = (
            mod.LoadTestStep(name="single", content=args.content, expect_file=False),
        )

    user_ids = [item["user_id"] for item in identities]
    session_ids = [item["session_id"] for item in identities]
    logger.info(
        "[plan] ws=%s concurrency=%d flow=%s mode=%s",
        ws_url,
        args.concurrency,
        args.flow,
        args.mode,
    )
    logger.info(
        "[plan] 五列口径: 会话=%d / Pod=1 / Bot=1 (%s) / 工作区=%d / 执行器=%d",
        args.concurrency,
        args.bot_id,
        args.concurrency,
        args.concurrency,
    )
    logger.info(
        "[plan] run_id=%s group_id=%s bot_id=%s user_id=%s..%s session_id=%s..%s",
        run_id,
        args.group_id,
        args.bot_id,
        user_ids[0],
        user_ids[-1],
        session_ids[0],
        session_ids[-1],
    )
    if args.flow == "single":
        logger.info("[plan] content=%r", args.content)
    else:
        logger.info(
            "[plan] loadtest steps=%s",
            " -> ".join(step.name for step in loadtest_steps or ()),
        )

    progress = mod.ProgressTracker(total=args.concurrency)
    registry = mod.ActiveSessionRegistry()
    download_dir = None
    if loadtest_steps and any(step.download_deliverable for step in loadtest_steps):
        download_dir = _make_run_download_dir()
        logger.info("[plan] download_dir=%s", download_dir)

    t0 = time.perf_counter()
    task_objs = [
        asyncio.create_task(
            mod.run_single_request(
                ws_url=ws_url,
                ws_headers=ws_headers,
                index=item["index"] - 1,
                shard=0,
                shard2=0,
                group_id=item["group_id"],
                bot_id=item["bot_id"],
                user_id=item["user_id"],
                content=args.content,
                mode=args.mode,
                accept_timeout=args.accept_timeout,
                accept_only=args.accept_only,
                final_timeout=final_timeout,
                cron_delivery_timeout=args.cron_delivery_timeout,
                auto_allow=not args.no_auto_allow,
                ws_event_log=args.ws_event_log,
                progress=progress,
                registry=registry,
                steps=loadtest_steps or single_steps,
                download_dir=download_dir,
                session_id=item["session_id"],
            )
        )
        for item in identities
    ]
    try:
        raw_results = await asyncio.gather(*task_objs, return_exceptions=True)
    except asyncio.CancelledError:
        logger.info("[shutdown] 收到中断，正在 cancel 进行中的会话…")
        await registry.cancel_all()
        for task in task_objs:
            task.cancel()
        await asyncio.gather(*task_objs, return_exceptions=True)
        raise
    elapsed = time.perf_counter() - t0

    results = []
    for item, raw in zip(identities, raw_results, strict=True):
        if isinstance(raw, Exception):
            fail = mod.RequestResult(
                index=item["index"] - 1,
                shard=0,
                shard2=0,
                session_id=item["session_id"],
                req_id="",
                group_id=item["group_id"],
                bot_id=item["bot_id"],
                user_id=item["user_id"],
                ok=False,
                accepted=False,
                error=str(raw),
            )
            results.append(fail)
        else:
            results.append(raw)

    def _is_success(r: Any) -> bool:
        if args.accept_only:
            return bool(r.accepted and r.ok)
        return bool(r.final_received and r.ok)

    completed = sum(1 for r in results if _is_success(r))
    failed = args.concurrency - completed
    distinct_users = {r.user_id for r in results}
    distinct_sessions = {r.session_id for r in results if r.session_id}

    logger.info("\n[requests] 各路身份与结果:")
    for r in sorted(results, key=lambda x: x.index):
        status = "ok" if _is_success(r) else "fail"
        logger.info(
            "[requests] idx=%02d status=%s accepted=%s final=%s total_ms=%.0f "
            "user_id=%s session_id=%s group_id=%s bot_id=%s error=%s",
            r.index,
            status,
            r.accepted,
            r.final_received,
            r.total_ms,
            r.user_id,
            r.session_id,
            r.group_id,
            r.bot_id,
            r.error or "-",
        )

    stats = mod.LoadTestStats(
        total=args.concurrency,
        completed=completed,
        failed=failed,
        elapsed_s=elapsed,
        accept_ms=[r.accept_ms for r in results if r.accept_ms > 0],
        total_ms=[r.total_ms for r in results if r.total_ms > 0],
    )
    logger.info("\n[assert] distinct_user_id=%d (期望 %d)", len(distinct_users), args.concurrency)
    logger.info(
        "[assert] distinct_session_id=%d (期望 %d)",
        len(distinct_sessions),
        args.concurrency,
    )
    logger.info("[assert] distinct_group_id=%s distinct_bot_id=%s", {args.group_id}, {args.bot_id})
    logger.info("[result] %s", stats.summary())

    identity_ok = (
        len(distinct_users) == args.concurrency
        and len(distinct_sessions) == args.concurrency
    )
    if not identity_ok:
        logger.error("[fail] user_id / session_id 未按 N 路展开")
        return 1
    return 0 if failed == 0 else 1


def main() -> int:
    try:
        _configure_cli_logging()
        _load_concurrent_mod()
    except Exception:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        logger.exception("加载 enterprise_runtime_concurrent_test.py 失败")
        return 1

    try:
        import websockets  # noqa: F401
    except ImportError:
        logger.error("缺少 websockets，请: pip install websockets 或使用仓库 .venv")
        return 1

    args = _parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except ValueError as err:
        logger.error("[invalid-args] %s", err)
        return 2
    except OSError as connect_err:
        logger.error("[connect-failed] %s", connect_err)
        logger.error("请确认 Web NodePort / Gateway 可访问，且 --host/--web-port/--ws-url 正确。")
        return 1
    except Exception as err:
        logger.error("[failed] %s", err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
