# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Instrumentor for MessageHandler queues — queue depth, throughput, wait time."""

from __future__ import annotations

import time

from opentelemetry.metrics import Observation

from jiuwenclaw.utils import logger
from jiuwenclaw.telemetry.attributes import JIUWENCLAW_CHANNEL_ID
from jiuwenclaw.telemetry.metrics import (
    add_message_processed,
    add_queue_dequeued,
    add_queue_enqueued,
    record_queue_wait_duration,
    set_queue_depth_observer,
)

_QUEUE_LABEL = "queue"


def instrument_queue() -> None:
    """Monkey-patch MessageHandler to collect queue metrics."""
    try:
        from jiuwenclaw.gateway.message_handler import MessageHandler
    except ImportError:
        logger.debug("[Telemetry] MessageHandler not available, skipping queue instrumentor")
        return

    _patch_publish(MessageHandler, "publish_user_messages", "user", is_async=True)
    _patch_publish(MessageHandler, "publish_user_messages_nowait", "user", is_async=False)
    _patch_publish(MessageHandler, "publish_robot_messages", "robot", is_async=True)
    _patch_publish(MessageHandler, "publish_robot_messages_nowait", "robot", is_async=False)
    _patch_consume(MessageHandler, "consume_user_messages", "user")
    _patch_consume(MessageHandler, "consume_robot_messages", "robot")
    _setup_depth_observer(MessageHandler)


def _patch_publish(cls, method_name: str, queue_name: str, *, is_async: bool) -> None:
    original = getattr(cls, method_name)

    if is_async:
        async def wrapper(self, msg):
            _on_enqueue(msg, queue_name)
            return await original(self, msg)
    else:
        def wrapper(self, msg):
            _on_enqueue(msg, queue_name)
            return original(self, msg)

    setattr(cls, method_name, wrapper)


def _patch_consume(cls, method_name: str, queue_name: str) -> None:
    original = getattr(cls, method_name)

    async def wrapper(self, timeout=None):
        msg = await original(self, timeout)
        if msg is not None:
            _on_dequeue(msg, queue_name)
        return msg

    setattr(cls, method_name, wrapper)


def _on_enqueue(msg, queue_name: str) -> None:
    try:
        channel_id = getattr(msg, "channel_id", "") or ""
        setattr(msg, "_otel_enqueue_time", time.monotonic())
        add_queue_enqueued(1, {_QUEUE_LABEL: queue_name, JIUWENCLAW_CHANNEL_ID: channel_id})
    except Exception as e:
        logger.warning(f"[Telemetry] Failed to record enqueue metrics for queue '{queue_name}': {e}")


def _on_dequeue(msg, queue_name: str) -> None:
    try:
        channel_id = getattr(msg, "channel_id", "") or ""
        attrs = {_QUEUE_LABEL: queue_name, JIUWENCLAW_CHANNEL_ID: channel_id}

        add_queue_dequeued(1, attrs)
        add_message_processed(1, {**attrs, "status": "success"})

        enqueue_time = getattr(msg, "_otel_enqueue_time", None)
        if enqueue_time is not None:
            elapsed_ms = (time.monotonic() - enqueue_time) * 1000
            record_queue_wait_duration(elapsed_ms, attrs)
    except Exception as e:
        logger.warning(f"[Telemetry] Failed to record dequeue metrics for queue '{queue_name}': {e}")


def _setup_depth_observer(cls) -> None:
    def observe():
        try:
            instance = cls.get_instance(None)
        except Exception:
            return []
        return [
            Observation(instance.user_messages_size, {_QUEUE_LABEL: "user"}),
            Observation(instance.robot_messages_size, {_QUEUE_LABEL: "robot"}),
        ]

    set_queue_depth_observer(observe)
