"""Request admission must stop locally without consuming model budget."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import sys

import httpx
import pytest

from scripts import deepseek_budget_proxy as proxy
from scripts.deepseek_budget_proxy import BudgetLedger, BudgetPolicy, ProxySettings, Usage, create_app


def _policy():
    return BudgetPolicy(
        model="deepseek-flash",
        budget_cny=Decimal("2.30"),
        input_cny_per_million=Decimal("2"),
        output_cny_per_million=Decimal("8"),
        max_input_tokens=1_000_000,
        max_output_tokens=32_768,
    )


def _write_policy(path, *, paused=False, stop_at_utc=None):
    path.write_text(json.dumps({"paused": paused, "stop_at_utc": stop_at_utc}), encoding="utf-8")


@pytest.mark.asyncio
async def test_paused_request_is_rejected_before_reserving_or_sending(tmp_path):
    path = tmp_path / "request-policy.json"
    _write_policy(path, paused=True)
    ledger = BudgetLedger(tmp_path / "budget.json", _policy())
    upstream_calls = []

    def upstream(request):
        upstream_calls.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    try:
        app = create_app(
            ProxySettings("https://api.deepseek.com", "unused-local-test"),
            ledger,
            request_policy=path,
            upstream_transport=httpx.MockTransport(upstream),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy.test") as client:
            response = await client.post("/v1/chat/completions", json={"model": "deepseek-flash", "messages": []})
        assert response.status_code == 423
        assert response.json()["detail"]["code"] == "RSI_REQUEST_PAUSED"
        assert upstream_calls == []
        assert ledger.snapshot()["charged_micro_cny"] == 0
        assert ledger.snapshot()["reserved_calls"] == 0
        assert ledger.snapshot()["unmetered_calls"] == 0
    finally:
        ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["paused", "deadline", "invalid"])
async def test_budget_queue_rechecks_policy_before_admitting_another_upstream_call(tmp_path, blocked):
    path = tmp_path / "request-policy.json"
    _write_policy(path)
    ledger = BudgetLedger(tmp_path / "budget.json", _policy())
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    queue_entered = asyncio.Event()
    upstream_calls = []
    original_reserve = ledger.reserve

    async def reserve():
        if ledger.snapshot()["active_reservations"]:
            queue_entered.set()
        return await original_reserve()

    ledger.reserve = reserve

    async def upstream(request):
        upstream_calls.append(request)
        first_started.set()
        await release_first.wait()
        return httpx.Response(200, json={"usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    tasks = []
    try:
        app = create_app(
            ProxySettings("https://api.deepseek.com", "unused-local-test"),
            ledger,
            request_policy=path,
            upstream_transport=httpx.MockTransport(upstream),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy.test") as client:
            body = {"model": "deepseek-flash", "messages": []}
            tasks.append(asyncio.create_task(client.post("/chat/completions", json=body)))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            tasks.extend(asyncio.create_task(client.post("/chat/completions", json=body)) for _ in range(3))
            await asyncio.wait_for(queue_entered.wait(), timeout=1)
            await asyncio.sleep(0)
            assert all(not task.done() for task in tasks[1:])
            if blocked == "paused":
                _write_policy(path, paused=True)
            elif blocked == "deadline":
                _write_policy(path, stop_at_utc=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
            else:
                path.write_text("{", encoding="utf-8")
            release_first.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert responses[0].status_code == 200
        expected_status = 503 if blocked == "invalid" else 423
        assert [response.status_code for response in responses[1:]] == [expected_status] * 3
        assert len(upstream_calls) == 1
        state = ledger.snapshot()
        assert state["charged_micro_cny"] == 48
        assert state["active_reservations"] == {}
        assert state["reserved_calls"] == state["settled_calls"] == 1
        assert state["unmetered_calls"] == 0
    finally:
        release_first.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_policy",
    [
        "{", "[]", "null", "{}",
        '{"paused": "false", "stop_at_utc": null}',
        '{"paused": 0, "stop_at_utc": null}',
        '{"paused": false, "stop_at_utc": 12}',
        '{"paused": false, "stop_at_utc": "2026-10-08T08:00:00"}',
        '{"paused": false, "stop_at_utc": "2026-10-08T08:00:00+08:00"}',
        '{"paused": false, "stop_at_utc": "invalid"}',
        '{"paused": false, "stop_at_utc": null, "stop_at": "ignored"}',
        None,
    ],
)
async def test_invalid_or_missing_policy_never_reserves_or_calls_upstream(tmp_path, raw_policy):
    path = tmp_path / "request-policy.json"
    if raw_policy is not None:
        path.write_text(raw_policy, encoding="utf-8")
    ledger = BudgetLedger(tmp_path / "budget.json", _policy())
    upstream_calls = []

    def upstream(request):
        upstream_calls.append(request)
        return httpx.Response(200)

    try:
        app = create_app(
            ProxySettings("https://api.deepseek.com", "unused-local-test"), ledger,
            request_policy=path, upstream_transport=httpx.MockTransport(upstream),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy.test") as client:
            response = await client.post("/chat/completions", json={"model": "deepseek-flash", "messages": []})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "RSI_REQUEST_POLICY_INVALID"
        assert upstream_calls == []
        assert ledger.snapshot()["charged_micro_cny"] == ledger.snapshot()["reserved_calls"] == 0
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_deadline_equality_blocks_and_live_policy_can_resume(tmp_path, monkeypatch):
    now = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(proxy, "datetime", FixedDatetime)
    path = tmp_path / "request-policy.json"
    _write_policy(path, stop_at_utc="2026-10-08T08:00:00Z")
    ledger = BudgetLedger(tmp_path / "budget.json", _policy())
    upstream_calls = []

    def upstream(request):
        upstream_calls.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 12, "completion_tokens": 3}})

    try:
        app = create_app(
            ProxySettings("https://api.deepseek.com", "unused-local-test"), ledger,
            request_policy=path, upstream_transport=httpx.MockTransport(upstream),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy.test") as client:
            body = {"model": "deepseek-flash", "messages": []}
            blocked = await client.post("/chat/completions", json=body)
            assert blocked.status_code == 423
            assert blocked.json()["detail"]["code"] == "RSI_REQUEST_DEADLINE"
            _write_policy(path, stop_at_utc="2026-10-08T08:00:01+00:00")
            assert (await client.post("/chat/completions", json=body)).status_code == 200
            _write_policy(path, paused=True)
            assert (await client.post("/chat/completions", json=body)).status_code == 423
            assert (await client.get("/health")).status_code == 200
            _write_policy(path)
            assert (await client.post("/chat/completions", json=body)).status_code == 200
        assert len(upstream_calls) == 2
        assert ledger.snapshot()["charged_micro_cny"] == 96
        assert ledger.snapshot()["unmetered_calls"] == 0
    finally:
        ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/models", "/v1/models"])
async def test_paused_model_listing_never_forwards(tmp_path, route):
    path = tmp_path / "request-policy.json"
    _write_policy(path, paused=True)
    ledger = BudgetLedger(tmp_path / "budget.json", _policy())
    upstream_calls = []

    def upstream(request):
        upstream_calls.append(request)
        return httpx.Response(200)

    try:
        app = create_app(
            ProxySettings("https://api.deepseek.com", "unused-local-test"), ledger,
            request_policy=path, upstream_transport=httpx.MockTransport(upstream),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy.test") as client:
            response = await client.get(route)
        assert response.status_code == 423
        assert upstream_calls == []
        assert ledger.snapshot()["charged_micro_cny"] == ledger.snapshot()["reserved_calls"] == 0
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_cancel_unforwarded_refunds_only_local_reservation_and_wakes_budget_queue(tmp_path):
    path = tmp_path / "budget.json"
    ledger = BudgetLedger(path, _policy())
    queued = None
    try:
        first = await ledger.reserve()
        queued = asyncio.create_task(ledger.reserve())
        await asyncio.sleep(0)
        assert not queued.done()
        await ledger.cancel_unforwarded(first)
        second = await asyncio.wait_for(queued, timeout=1)
        with pytest.raises(ValueError, match="not owned"):
            await ledger.cancel_unforwarded(first)
        await ledger.settle(second, Usage(12, 3))
        state = json.loads(path.read_text(encoding="utf-8"))
        assert state["charged_micro_cny"] == 48
        assert state["reserved_calls"] == state["settled_calls"] == 1
        assert state["unmetered_calls"] == 0
        assert state["active_reservations"] == {}
    finally:
        if queued is not None and not queued.done():
            queued.cancel()
            await asyncio.gather(queued, return_exceptions=True)
        ledger.close()


@pytest.mark.asyncio
async def test_cancel_unforwarded_cannot_refund_an_old_process_reservation(tmp_path):
    path = tmp_path / "budget.json"
    original = BudgetLedger(path, _policy())
    stale = await original.reserve()
    original.close()
    ledger = BudgetLedger(path, _policy())
    try:
        with pytest.raises(ValueError, match="not owned"):
            await ledger.cancel_unforwarded(stale)
        assert ledger.snapshot()["charged_micro_cny"] == 2_262_144
        assert ledger.snapshot()["active_reservations"] == {stale.reservation_id: 2_262_144}
    finally:
        ledger.close()


def test_proxy_cli_policy_flag_reaches_request_admission(tmp_path, monkeypatch):
    policy = proxy.BudgetPolicy(
        model="deepseek-flash", budget_cny=Decimal("2.40"),
        input_cny_per_million=Decimal("2"), output_cny_per_million=Decimal("8"),
        max_input_tokens=1_000_000, max_output_tokens=32_768,
    )
    ledger_path = tmp_path / "budget.json"
    initial = proxy.BudgetLedger(ledger_path, policy)

    async def seed():
        reservation = await initial.reserve()
        await initial.cancel_unforwarded(reservation)

    asyncio.run(seed())
    initial.close()
    path = tmp_path / "request-policy.json"
    _write_policy(path, paused=True)
    env_file = tmp_path / "test.env"
    env_file.write_text("DEEPSEEK_API_BASE=https://api.test.invalid\nDEEPSEEK_API_KEY=unused-local-test\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["deepseek_budget_proxy", "--env-file", str(env_file), "--ledger", str(ledger_path), "--request-policy", str(path), "--budget-cny", "2.40"])
    observed = []
    real_create_app = proxy.create_app

    def local_app(*args, **kwargs):
        def upstream(request):
            observed.append("upstream")
            return httpx.Response(200)
        return real_create_app(*args, **kwargs, upstream_transport=httpx.MockTransport(upstream))

    def run(app, **kwargs):
        async def post():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy.test") as client:
                return await client.post("/chat/completions", json={"model": "deepseek-flash", "messages": []})
        response = asyncio.run(post())
        observed.append(response.status_code)

    monkeypatch.setattr(proxy, "create_app", local_app)
    monkeypatch.setattr(proxy.uvicorn, "run", run)
    proxy.main()
    assert observed == [423]
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["charged_micro_cny"] == 0
