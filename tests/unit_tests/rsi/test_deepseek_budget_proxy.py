import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from scripts.deepseek_budget_proxy import (
    BudgetExceeded,
    BudgetLedger,
    BudgetPolicy,
    LedgerLocked,
    SseUsageParser,
    Usage,
    _stream_upstream,
    bounded_chat_body,
    create_app,
    extract_usage,
    ProxySettings,
    validate_loopback_host,
)


def _policy(*, budget: str = "19.90") -> BudgetPolicy:
    return BudgetPolicy(
        model="deepseek-flash",
        budget_cny=Decimal(budget),
        input_cny_per_million=Decimal("2"),
        output_cny_per_million=Decimal("8"),
        max_input_tokens=1_000_000,
        max_output_tokens=32_768,
    )


def test_policy_reserves_peak_uncached_worst_case() -> None:
    policy = _policy()

    assert policy.budget_micro_cny == 19_900_000
    assert policy.worst_call_micro_cny == 2_262_144


@pytest.mark.asyncio
async def test_reservation_is_persisted_before_a_call_and_refunded_from_usage(tmp_path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger(path, _policy())

    reservation = await ledger.reserve()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["charged_micro_cny"] == 2_262_144
    assert reservation.reservation_id in persisted["active_reservations"]

    await ledger.settle(reservation, Usage(input_tokens=100, output_tokens=10))

    snapshot = ledger.snapshot()
    assert snapshot["charged_micro_cny"] == 280
    assert snapshot["active_reservations"] == {}
    assert snapshot["settled_calls"] == 1


@pytest.mark.asyncio
async def test_missing_usage_keeps_the_full_reservation_across_reload(tmp_path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger(path, _policy())
    reservation = await ledger.reserve()

    await ledger.settle(reservation, None)
    ledger.close()

    reloaded = BudgetLedger(path, _policy())
    assert reloaded.snapshot()["charged_micro_cny"] == 2_262_144
    assert reloaded.snapshot()["unmetered_calls"] == 1
    reloaded.close()


@pytest.mark.asyncio
async def test_concurrent_reservation_waits_for_this_process_settlement(tmp_path) -> None:
    policy = _policy(budget="2.30")
    ledger = BudgetLedger(tmp_path / "queued-budget.json", policy)
    first = await ledger.reserve()
    queued = asyncio.create_task(ledger.reserve())
    try:
        await asyncio.sleep(0)
        assert not queued.done()
        assert ledger.snapshot()["reserved_calls"] == 1
        await ledger.settle(first, Usage(input_tokens=10, output_tokens=1))
        second = await asyncio.wait_for(queued, timeout=1)
        assert ledger.snapshot()["reserved_calls"] == 2
        assert len(ledger.snapshot()["active_reservations"]) == 1
        assert ledger.snapshot()["charged_micro_cny"] <= policy.budget_micro_cny
        await ledger.settle(second, Usage(input_tokens=10, output_tokens=1))
    finally:
        if not queued.done():
            queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        ledger.close()


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_cross_the_budget(tmp_path) -> None:
    policy = _policy(budget="4.50")
    ledger = BudgetLedger(tmp_path / "budget.json", policy)

    async def call_and_settle():
        reservation = await ledger.reserve()
        assert len(ledger.snapshot()["active_reservations"]) == 1
        assert ledger.snapshot()["charged_micro_cny"] <= policy.budget_micro_cny
        await asyncio.sleep(0)
        await ledger.settle(reservation, Usage(input_tokens=100, output_tokens=10))

    await asyncio.wait_for(asyncio.gather(*(call_and_settle() for _ in range(3))), timeout=1)
    assert ledger.snapshot()["settled_calls"] == 3
    assert ledger.snapshot()["charged_micro_cny"] == 840
    assert ledger.snapshot()["active_reservations"] == {}
    ledger.close()


@pytest.mark.asyncio
async def test_queued_reservation_rejects_after_unmetered_call_keeps_peak_cost(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.json", _policy(budget="2.30"))
    first = await ledger.reserve()
    queued = asyncio.create_task(ledger.reserve())
    await asyncio.sleep(0)
    assert not queued.done()

    await ledger.settle(first, None)

    with pytest.raises(BudgetExceeded):
        await asyncio.wait_for(queued, timeout=1)
    assert ledger.snapshot()["unmetered_calls"] == 1
    assert ledger.snapshot()["reserved_calls"] == 1
    ledger.close()


@pytest.mark.asyncio
async def test_reloaded_reservation_never_waits_for_an_old_process(tmp_path) -> None:
    path = tmp_path / "budget.json"
    policy = _policy(budget="2.30")
    original = BudgetLedger(path, policy)
    stale = await original.reserve()
    original.close()
    reloaded = BudgetLedger(path, policy)

    with pytest.raises(BudgetExceeded):
        await asyncio.wait_for(reloaded.reserve(), timeout=1)

    assert reloaded.snapshot()["active_reservations"] == {stale.reservation_id: stale.worst_micro_cny}
    assert reloaded.snapshot()["reserved_calls"] == 1
    assert reloaded.snapshot()["charged_micro_cny"] == stale.worst_micro_cny
    reloaded.close()


@pytest.mark.asyncio
async def test_cancelled_queue_waiter_does_not_reserve_or_block_another_call(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.json", _policy(budget="2.30"))
    first = await ledger.reserve()
    queued = asyncio.create_task(ledger.reserve())
    await asyncio.sleep(0)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert ledger.snapshot()["reserved_calls"] == 1
    await ledger.settle(first, Usage(input_tokens=100, output_tokens=10))
    second = await asyncio.wait_for(ledger.reserve(), timeout=1)
    await ledger.settle(second, Usage(input_tokens=100, output_tokens=10))
    assert ledger.snapshot()["reserved_calls"] == 2
    ledger.close()


@pytest.mark.asyncio
async def test_policy_mismatch_fails_closed(tmp_path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger(path, _policy())
    await ledger.reserve()
    ledger.close()

    with pytest.raises(ValueError, match="policy"):
        BudgetLedger(path, _policy(budget="19.80"))


def test_second_process_owner_cannot_open_the_same_ledger(tmp_path) -> None:
    path = tmp_path / "budget.json"
    ledger = BudgetLedger(path, _policy())

    with pytest.raises(LedgerLocked):
        BudgetLedger(path, _policy())

    ledger.close()


@pytest.mark.parametrize(
    "field,value",
    [("n", 2), ("best_of", 2), ("max_completion_tokens", 10)],
)
def test_chat_body_rejects_ambiguous_generation_fields(field, value) -> None:
    body = {"model": "deepseek-flash", "messages": [], field: value}

    with pytest.raises(ValueError, match=field):
        bounded_chat_body(body, _policy())


def test_corrupt_numeric_ledger_fails_closed(tmp_path) -> None:
    path = tmp_path / "budget.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "policy": _policy().as_dict(),
                "charged_micro_cny": -1,
                "reserved_calls": 0,
                "settled_calls": 0,
                "unmetered_calls": 0,
                "active_reservations": {},
                "updated_at": None,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ledger"):
        BudgetLedger(path, _policy())


def test_sse_usage_parser_handles_split_chunks_without_storing_content() -> None:
    parser = SseUsageParser()
    parser.feed(b'data: {"choices":[{"delta":{"content":"secret"}}]}\n\ndata: {"usage":')
    parser.feed(b'{"prompt_tokens":7,"completion_tokens":2},"choices":[]}\n\n')
    parser.finish()

    assert parser.usage == Usage(input_tokens=7, output_tokens=2)


def test_proxy_host_must_be_loopback() -> None:
    assert validate_loopback_host("127.0.0.1") == "127.0.0.1"
    assert validate_loopback_host("localhost") == "localhost"
    with pytest.raises(ValueError, match="loopback"):
        validate_loopback_host("0.0.0.0")


def test_chat_body_forces_exact_model_and_output_cap() -> None:
    body = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 100_000,
    }

    bounded = bounded_chat_body(body, _policy())

    assert bounded["max_tokens"] == 32_768
    assert body["max_tokens"] == 100_000


def test_chat_body_rejects_a_different_model() -> None:
    with pytest.raises(ValueError, match="model"):
        bounded_chat_body({"model": "another-model", "messages": []}, _policy())


def test_extract_usage_supports_json_and_sse_without_content() -> None:
    json_usage = extract_usage(
        b'{"choices":[{"message":{"content":"secret"}}],'
        b'"usage":{"prompt_tokens":12,"completion_tokens":3}}',
        "application/json",
    )
    sse_usage = extract_usage(
        b'data: {"choices":[],"usage":{"input_tokens":20,"output_tokens":4}}\n\n'
        b"data: [DONE]\n\n",
        "text/event-stream",
    )

    assert json_usage == Usage(input_tokens=12, output_tokens=3)
    assert sse_usage == Usage(input_tokens=20, output_tokens=4)


@pytest.mark.asyncio
async def test_http_proxy_reserves_before_upstream_and_settles_usage(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.json", _policy())
    upstream_calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        snapshot = ledger.snapshot()
        upstream_calls.append(
            {
                "authorization": request.headers.get("authorization"),
                "charged": snapshot["charged_micro_cny"],
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )

    app = create_app(
        ProxySettings("https://api.deepseek.com", "upstream"),
        ledger,
        upstream_transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/chat/completions",
            headers={"Authorization": "Bearer local"},
            json={"model": "deepseek-flash", "messages": [], "max_tokens": 8},
        )

    assert response.status_code == 200
    assert upstream_calls == [
        {
            "authorization": "Bearer upstream",
            "charged": _policy().worst_call_micro_cny,
            "body": {"model": "deepseek-flash", "messages": [], "max_tokens": 8},
        }
    ]
    assert ledger.snapshot()["charged_micro_cny"] == 48
    assert "upstream" not in json.dumps(ledger.snapshot())
    ledger.close()


@pytest.mark.asyncio
async def test_http_budget_rejection_never_calls_upstream(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.json", _policy(budget="1"))
    upstream_call_count = 0

    def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_call_count
        upstream_call_count += 1
        return httpx.Response(500)

    app = create_app(
        ProxySettings("https://api.deepseek.com", "upstream"),
        ledger,
        upstream_transport=httpx.MockTransport(upstream),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "deepseek-flash", "messages": []},
        )

    assert response.status_code == 429
    assert upstream_call_count == 0
    assert ledger.snapshot()["charged_micro_cny"] == 0
    ledger.close()


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


class _ClosableClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stream_forwards_first_chunk_then_settles_final_usage(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.json", _policy())
    reservation = await ledger.reserve()
    first = b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
    last = b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_ChunkStream([first, last, b"data: [DONE]\n\n"]),
    )
    client = _ClosableClient()
    stream = _stream_upstream(response, client, ledger, reservation)

    assert await anext(stream) == first
    assert ledger.snapshot()["active_reservations"]
    remaining = [chunk async for chunk in stream]

    assert remaining == [last, b"data: [DONE]\n\n"]
    assert ledger.snapshot()["charged_micro_cny"] == 26
    assert client.closed is True
    ledger.close()


@pytest.mark.asyncio
async def test_stream_disconnect_keeps_full_reservation(tmp_path) -> None:
    ledger = BudgetLedger(tmp_path / "budget.json", _policy())
    reservation = await ledger.reserve()
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_ChunkStream([b'data: {"choices":[]}\n\n', b"data: [DONE]\n\n"]),
    )
    client = _ClosableClient()
    stream = _stream_upstream(response, client, ledger, reservation)

    await anext(stream)
    await stream.aclose()

    assert ledger.snapshot()["charged_micro_cny"] == _policy().worst_call_micro_cny
    assert ledger.snapshot()["unmetered_calls"] == 1
    assert client.closed is True
    ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_usage,has_done", [(True, True), (True, False), (False, True)])
async def test_stream_protocol_done_can_settle_usage_before_transport_eof(tmp_path, has_usage, has_done) -> None:
    ledger = BudgetLedger(tmp_path / "budget.json", _policy())
    reservation = await ledger.reserve()
    chunks = []
    if has_usage:
        chunks.append(b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n')
    if has_done:
        chunks.append(b"data: [DONE]\n\n")
    # Unread tail makes the close occur before natural transport exhaustion.
    response = httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_ChunkStream([*chunks, b": tail still open\n\n"]))
    client = _ClosableClient()
    stream = _stream_upstream(response, client, ledger, reservation)
    for chunk in chunks:
        assert await anext(stream) == chunk
    await stream.aclose()

    assert ledger.snapshot()["charged_micro_cny"] == (26 if has_usage and has_done else _policy().worst_call_micro_cny)
    assert ledger.snapshot()["unmetered_calls"] == (0 if has_usage and has_done else 1)
    assert ledger.snapshot()["active_reservations"] == {}
    assert client.closed is True
    ledger.close()
