"""Persistence failures must never admit an unreserved or undercharged call."""
import asyncio
import ctypes
import json
import os
from decimal import Decimal

import httpx
import pytest

from scripts import deepseek_budget_proxy as proxy


def _policy(budget="19.90"):
    return proxy.BudgetPolicy("deepseek-flash", Decimal(budget), Decimal("2"),
                              Decimal("8"), 1_000_000, 32_768)


def _permission(winerror):
    error = PermissionError("test-only atomic replacement refusal")
    if winerror is not None:
        error.winerror = winerror
    return error


async def _seed(ledger):
    reservation = await ledger.reserve()
    await ledger.cancel_unforwarded(reservation)


def _app(ledger, upstream):
    return proxy.create_app(proxy.ProxySettings("https://test.invalid", "unused-local-test"),
                            ledger, upstream_transport=httpx.MockTransport(upstream))


@pytest.mark.asyncio
@pytest.mark.parametrize("winerror", [5, 32])
async def test_temporary_windows_replace_failure_retries_storage_not_model(tmp_path, monkeypatch, winerror):
    path = tmp_path / "budget.json"
    ledger = proxy.BudgetLedger(path, _policy())
    await _seed(ledger)
    original_replace = proxy.os.replace
    replacements = []
    upstream_calls = []

    def replace(source, target):
        replacements.append((source, target))
        if len(replacements) <= 2:
            raise _permission(winerror)
        return original_replace(source, target)

    def upstream(request):
        upstream_calls.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    monkeypatch.setattr(proxy.os, "replace", replace)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(_app(ledger, upstream), raise_app_exceptions=False),
                                     base_url="http://proxy.test") as client:
            response = await client.post("/chat/completions", json={"model": "deepseek-flash"})
        assert response.status_code == 200
        assert len(upstream_calls) == 1
        assert ledger.snapshot() == json.loads(path.read_text(encoding="utf-8"))
        assert ledger.snapshot()["charged_micro_cny"] == 26
        assert ledger.snapshot()["reserved_calls"] == ledger.snapshot()["settled_calls"] == 1
        assert ledger.snapshot()["active_reservations"] == {}
    finally:
        ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("winerror", [5, 32, 1, None])
async def test_permanent_reserve_failure_restores_snapshot_and_can_recover(tmp_path, monkeypatch, winerror):
    path = tmp_path / "budget.json"
    ledger = proxy.BudgetLedger(path, _policy())
    unknown = await ledger.reserve()
    await ledger.settle(unknown, None)
    before = ledger.snapshot()
    disk_before = path.read_bytes()
    original_replace = proxy.os.replace
    attempts = []
    upstream_calls = []

    def refuse(source, target):
        attempts.append(1)
        raise _permission(winerror)

    def upstream(request):
        upstream_calls.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    monkeypatch.setattr(proxy.os, "replace", refuse)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(_app(ledger, upstream), raise_app_exceptions=False),
                                     base_url="http://proxy.test") as client:
            response = await client.post("/chat/completions", json={"model": "deepseek-flash"})
            assert ledger.snapshot() == before
            assert path.read_bytes() == disk_before
            assert ledger._owned_reservations == set()
            assert upstream_calls == []
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "RSI_LEDGER_PERSIST_FAILED"
            assert 1 <= len(attempts) <= 5
            if winerror not in (5, 32):
                assert len(attempts) == 1
            monkeypatch.setattr(proxy.os, "replace", original_replace)
            recovered = await client.post("/chat/completions", json={"model": "deepseek-flash"})
        assert recovered.status_code == 200
        assert len(upstream_calls) == 1
        assert ledger.snapshot()["unmetered_calls"] == 1
        assert ledger.snapshot()["charged_micro_cny"] == 2_262_170
    finally:
        ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["settle", "cancel"])
async def test_temporary_settle_or_cancel_failure_still_commits_correct_state(tmp_path, monkeypatch, operation):
    path = tmp_path / "budget.json"
    ledger = proxy.BudgetLedger(path, _policy())
    reservation = await ledger.reserve()
    original_replace = proxy.os.replace
    attempts = []

    def replace(source, target):
        attempts.append(1)
        if len(attempts) == 1:
            raise _permission(32)
        return original_replace(source, target)

    monkeypatch.setattr(proxy.os, "replace", replace)
    try:
        if operation == "settle":
            await ledger.settle(reservation, proxy.Usage(5, 2))
            assert ledger.snapshot()["charged_micro_cny"] == 26
            assert ledger.snapshot()["reserved_calls"] == ledger.snapshot()["settled_calls"] == 1
        else:
            await ledger.cancel_unforwarded(reservation)
            assert ledger.snapshot()["charged_micro_cny"] == 0
            assert ledger.snapshot()["reserved_calls"] == ledger.snapshot()["settled_calls"] == 0
        assert ledger.snapshot() == json.loads(path.read_text(encoding="utf-8"))
        assert ledger.snapshot()["active_reservations"] == {}
    finally:
        ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [proxy.Usage(5, 2), None])
async def test_permanent_settle_failure_retains_worst_and_wakes_queue_to_stop(tmp_path, monkeypatch, usage):
    path = tmp_path / "budget.json"
    ledger = proxy.BudgetLedger(path, _policy("2.30"))
    reservation = await ledger.reserve()
    before = ledger.snapshot()
    disk_before = path.read_bytes()
    queued = asyncio.create_task(ledger.reserve())
    await asyncio.sleep(0)
    assert not queued.done()

    def refuse(source, target):
        raise _permission(5)

    monkeypatch.setattr(proxy.os, "replace", refuse)
    try:
        with pytest.raises(Exception):
            await ledger.settle(reservation, usage)
        assert ledger.snapshot() == before
        assert path.read_bytes() == disk_before
        assert ledger._owned_reservations == {reservation.reservation_id}
        with pytest.raises(Exception) as error:
            await asyncio.wait_for(queued, timeout=1)
        assert type(error.value).__name__ == "LedgerPersistenceError"
        with pytest.raises(Exception) as error:
            await ledger.reserve()
        assert type(error.value).__name__ == "LedgerPersistenceError"
        assert ledger.snapshot()["charged_micro_cny"] == 2_262_144
        assert ledger.snapshot()["unmetered_calls"] == 0
    finally:
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        ledger.close()


@pytest.mark.asyncio
async def test_permanent_cancel_failure_retains_reservation_and_stops_new_calls(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    ledger = proxy.BudgetLedger(path, _policy())
    reservation = await ledger.reserve()
    before = ledger.snapshot()
    disk_before = path.read_bytes()

    def refuse(source, target):
        raise _permission(5)

    monkeypatch.setattr(proxy.os, "replace", refuse)
    try:
        with pytest.raises(Exception):
            await ledger.cancel_unforwarded(reservation)
        assert ledger.snapshot() == before
        assert path.read_bytes() == disk_before
        assert ledger._owned_reservations == {reservation.reservation_id}
        with pytest.raises(Exception) as error:
            await ledger.reserve()
        assert type(error.value).__name__ == "LedgerPersistenceError"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_failed_post_send_settlement_degrades_health_and_never_forwards_next_call(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    ledger = proxy.BudgetLedger(path, _policy())
    calls = []

    def refuse(source, target):
        raise _permission(5)

    def upstream(request):
        calls.append(request)
        monkeypatch.setattr(proxy.os, "replace", refuse)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(_app(ledger, upstream), raise_app_exceptions=False),
                                     base_url="http://proxy.test") as client:
            first = await client.post("/chat/completions", json={"model": "deepseek-flash"})
            second = await client.post("/chat/completions", json={"model": "deepseek-flash"})
            health = await client.get("/health")
        assert first.status_code == second.status_code == 503
        assert first.json()["detail"]["code"] == second.json()["detail"]["code"] == "RSI_LEDGER_PERSIST_FAILED"
        assert len(calls) == 1
        assert health.json()["status"] == "degraded"
        assert ledger.snapshot() == json.loads(path.read_text(encoding="utf-8"))
        assert ledger.snapshot()["charged_micro_cny"] == 2_262_144
        assert ledger.snapshot()["reserved_calls"] == 1
        assert ledger.snapshot()["settled_calls"] == ledger.snapshot()["unmetered_calls"] == 0
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_pause_cancellation_persist_failure_closes_client_without_forwarding(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    request_policy = tmp_path / "request-policy.json"
    request_policy.write_text(json.dumps({"paused": False, "stop_at_utc": None}), encoding="utf-8")
    ledger = proxy.BudgetLedger(path, _policy())
    original_client = httpx.AsyncClient
    created = []
    calls = []

    def refuse(source, target):
        raise _permission(5)

    class LocalClient(original_client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def build_request(self, *args, **kwargs):
            request = super().build_request(*args, **kwargs)
            request_policy.write_text(json.dumps({"paused": True, "stop_at_utc": None}), encoding="utf-8")
            monkeypatch.setattr(proxy.os, "replace", refuse)
            return request

    def upstream(request):
        calls.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(proxy.httpx, "AsyncClient", LocalClient)
    try:
        app = proxy.create_app(proxy.ProxySettings("https://test.invalid", "unused-local-test"), ledger,
                               request_policy=request_policy, upstream_transport=httpx.MockTransport(upstream))
        async with original_client(transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                                    base_url="http://proxy.test") as client:
            response = await client.post("/chat/completions", json={"model": "deepseek-flash"})
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "RSI_LEDGER_PERSIST_FAILED"
        assert calls == []
        assert len(created) == 1 and created[0].is_closed
        assert ledger.snapshot() == json.loads(path.read_text(encoding="utf-8"))
        assert ledger.snapshot()["charged_micro_cny"] == 2_262_144
        assert ledger.snapshot()["unmetered_calls"] == 0
    finally:
        for client in created:
            await client.aclose()
        ledger.close()


def _windows_read_handle(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows native sharing semantics")
@pytest.mark.parametrize("release_after_failure", [True, False])
async def test_native_windows_reader_blocks_atomic_replace_without_delete_share(tmp_path, monkeypatch, release_after_failure):
    path = tmp_path / "budget.json"
    ledger = proxy.BudgetLedger(path, _policy())
    await _seed(ledger)
    before = ledger.snapshot()
    disk_before = path.read_bytes()
    kernel, handle = _windows_read_handle(path)
    original_replace = proxy.os.replace
    actual_refusals = []

    def replace(source, target):
        nonlocal handle
        try:
            return original_replace(source, target)
        except PermissionError as error:
            actual_refusals.append(error.winerror)
            if release_after_failure:
                assert kernel.CloseHandle(handle)
                handle = None
            raise

    monkeypatch.setattr(proxy.os, "replace", replace)
    try:
        if release_after_failure:
            reservation = await ledger.reserve()
            assert reservation.reservation_id in json.loads(path.read_text(encoding="utf-8"))["active_reservations"]
            await ledger.cancel_unforwarded(reservation)
        else:
            with pytest.raises(Exception):
                await ledger.reserve()
            assert ledger.snapshot() == before
            assert path.read_bytes() == disk_before
        assert actual_refusals and all(error in (5, 32) for error in actual_refusals)
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
        ledger.close()
