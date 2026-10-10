# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio

from jiuwenswarm.gateway.channel_manager.sdk import (
    RetryPolicy,
    SessionIdPolicy,
    TokenBucketRateLimiter,
)


# ----------------------------------------------------------- session policy
def test_session_id_single_format_with_thread():
    policy = SessionIdPolicy()
    assert policy.build("slack", "C42", "T7") == "slack:C42:T7"


def test_session_id_omits_missing_thread():
    policy = SessionIdPolicy()
    assert policy.build("discord", "C42") == "discord:C42"
    assert policy.build("discord", "C42", None) == "discord:C42"


# ------------------------------------------------------------- retry policy
def test_retry_delays_grow_exponentially_and_are_capped():
    policy = RetryPolicy(base_delay=1.0, max_delay=8.0)
    assert [policy.delay_for(attempt) for attempt in range(5)] == [1.0, 2.0, 4.0, 8.0, 8.0]


# -------------------------------------------------------------- rate limiter
async def test_rate_limiter_allows_burst_up_to_capacity():
    limiter = TokenBucketRateLimiter(capacity=3, rate=1000.0)
    for _ in range(3):
        await asyncio.wait_for(limiter.acquire(), timeout=0.5)


async def test_rate_limiter_blocks_then_refills():
    limiter = TokenBucketRateLimiter(capacity=1, rate=1000.0)
    await limiter.acquire()
    # bucket empty; with rate=1000/s the wait is ~1 ms — must still complete
    await asyncio.wait_for(limiter.acquire(), timeout=1.0)