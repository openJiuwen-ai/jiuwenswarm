#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from jiuwenclaw.channel.feishu import FeishuChannel, FeishuConfig
from jiuwenclaw.extensions.yuanrong_frontend_client import YuanrongFrontendAgentClient
from jiuwenclaw.gateway.channel_manager import ChannelManager
from jiuwenclaw.gateway.message_handler import MessageHandler
from jiuwenclaw.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenclaw.schema.message import EventType


@dataclass
class PerfStats:
    total: int
    success: int
    failed: int
    elapsed_s: float
    qps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    stdev_ms: float


def _percentile(sorted_values: list[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    idx = int((len(sorted_values) - 1) * ratio)
    return sorted_values[idx]


def _build_fake_event(message_id: str, chat_id: str, open_id: str, text: str) -> Any:
    return SimpleNamespace(
        event=SimpleNamespace(
            message=SimpleNamespace(
                message_id=message_id,
                chat_id=chat_id,
                message_type="text",
                content=json.dumps({"text": text}, ensure_ascii=False),
                chat_type="p2p",
            ),
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id=open_id),
            ),
        )
    )


class _BenchFeishuChannel(FeishuChannel):
    def __init__(self, config: FeishuConfig, *, completed_ids: set[str]) -> None:
        super().__init__(config, router=SimpleNamespace())
        self.completed_ids = completed_ids

    async def send(self, msg) -> None:  # noqa: ANN001
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        event_name = getattr(msg.event_type, "value", None) or payload.get("event_type") or ""
        is_complete = bool(payload.get("is_complete", False))
        if event_name == EventType.CHAT_FINAL.value or is_complete:
            self.completed_ids.add(str(msg.id))


class _DelayEchoAgentClient:
    def __init__(self, delay_s: float = 3.0) -> None:
        self._delay_s = delay_s

    async def connect(self, uri: str) -> None:
        return

    async def disconnect(self) -> None:
        return

    @staticmethod
    def set_or_update_server_config(*, config: dict, env: dict | None = None) -> None:
        return

    async def send_request(self, envelope) -> AgentResponse:  # noqa: ANN001
        await asyncio.sleep(self._delay_s)
        text = str((envelope.params or {}).get("query") or (envelope.params or {}).get("content") or "")
        return AgentResponse(
            request_id=envelope.request_id,
            channel_id=envelope.channel,
            ok=True,
            payload={"event_type": EventType.CHAT_FINAL.value, "content": text, "is_complete": True},
            metadata=envelope.metadata,
        )

    async def send_request_stream(self, envelope):  # noqa: ANN001
        await asyncio.sleep(self._delay_s)
        text = str((envelope.params or {}).get("query") or (envelope.params or {}).get("content") or "")
        yield AgentResponseChunk(
            request_id=envelope.request_id,
            channel_id=envelope.channel,
            payload={"event_type": EventType.CHAT_FINAL.value, "content": text, "is_complete": True},
            is_complete=True,
        )
        yield AgentResponseChunk(
            request_id=envelope.request_id,
            channel_id=envelope.channel,
            payload=None,
            is_complete=True,
        )


async def _run_fullchain_benchmark(args: argparse.Namespace) -> PerfStats:
    if args.agent_mode == "mock_server":
        client = _DelayEchoAgentClient(delay_s=args.mock_delay_s)
        await client.connect("mock://delay-echo")
    else:
        client = YuanrongFrontendAgentClient(
            frontend_endpoint=args.frontend_endpoint,
            function_version_urn=args.function_version_urn,
            concurrency=args.frontend_concurrency,
            invoke_timeout_s=args.invoke_timeout_s,
        )
        await client.connect(args.frontend_endpoint)

    message_handler = MessageHandler(client)
    await message_handler.start_forwarding()
    completed_ids: set[str] = set()
    channel = _BenchFeishuChannel(
        FeishuConfig(enabled=True, app_id="mock_app_id", app_secret="mock_app_secret", channel_id="feishu_bench"),
        completed_ids=completed_ids,
    )
    channel_manager = ChannelManager(message_handler)
    channel_manager.register_channel(channel)
    await channel_manager.start_dispatch()

    sem = asyncio.Semaphore(args.concurrency)
    latencies: list[float] = []
    success = 0
    failed = 0

    async def _one(idx: int) -> tuple[bool, float]:
        req_id = f"mock-msg-{idx}"
        event = _build_fake_event(
            req_id,
            f"oc_mock_chat_{idx % 8}",
            f"ou_mock_user_{idx % 1000}",
            f"fullchain-loadtest #{idx}"
        )
        started = time.perf_counter()
        try:
            async with sem:
                await channel._on_message(event)  # pylint: disable=protected-access
                deadline = time.perf_counter() + args.deadline_s
                while req_id not in completed_ids:
                    if time.perf_counter() > deadline:
                        return False, (time.perf_counter() - started) * 1000
                    await asyncio.sleep(0.001)
                return True, (time.perf_counter() - started) * 1000
        except Exception:
            return False, (time.perf_counter() - started) * 1000

    try:
        started_at = time.perf_counter()
        results = await asyncio.gather(*[_one(i) for i in range(args.total)], return_exceptions=False)
        elapsed_s = max(time.perf_counter() - started_at, 1e-9)
        for ok, lat in results:
            latencies.append(lat)
            if ok:
                success += 1
            else:
                failed += 1
    finally:
        await channel_manager.stop_dispatch()
        await message_handler.stop_forwarding()
        await client.disconnect()

    latencies.sort()
    return PerfStats(
        total=args.total,
        success=success,
        failed=failed,
        elapsed_s=elapsed_s,
        qps=args.total / elapsed_s,
        p50_ms=_percentile(latencies, 0.5),
        p95_ms=_percentile(latencies, 0.95),
        p99_ms=_percentile(latencies, 0.99),
        max_ms=max(latencies) if latencies else 0.0,
        stdev_ms=statistics.pstdev(latencies) if len(latencies) > 1 else 0.0,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mock concurrent stress test for Feishu full chain.")
    parser.add_argument("-n", "--total", type=int, default=2000)
    parser.add_argument("-c", "--concurrency", type=int, default=100)
    parser.add_argument("--frontend-endpoint", default=os.getenv("YR_FRONTEND_ENDPOINT", ""))
    parser.add_argument("--function-version-urn", default=os.getenv("YR_FUNCTION_VERSION_URN", ""))
    parser.add_argument("--frontend-concurrency", type=int, default=int(os.getenv("YR_FRONTEND_CONCURRENCY", "1")))
    parser.add_argument("--invoke-timeout-s", type=float, default=float(os.getenv("YR_INVOKE_TIMEOUT_S", "60")))
    parser.add_argument("--agent-mode", choices=["yuanrong", "mock_server"], default="mock_server")
    parser.add_argument("--mock-delay-s", type=float, default=3.0)
    parser.add_argument("--deadline-s", type=float, default=60.0)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.agent_mode == "yuanrong":
        if not args.frontend_endpoint.strip():
            raise ValueError("--frontend-endpoint is required")
        if not args.function_version_urn.strip():
            raise ValueError("--function-version-urn is required")
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stats = asyncio.run(_run_fullchain_benchmark(args))
    logging.info(f"total=%d success=%d failed=%d", stats.total, stats.success, stats.failed)
    logging.info(f"elapsed_s=%.3f qps=%.2f", stats.elapsed_s, stats.qps)
    logging.info(
        "p50=%.2fms p95=%.2fms p99=%.2fms max=%.2fms stdev=%.2fms",
        stats.p50_ms, stats.p95_ms, stats.p99_ms, stats.max_ms, stats.stdev_ms
    )


if __name__ == "__main__":
    main()
