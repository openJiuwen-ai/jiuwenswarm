"""Local fail-closed budget proxy for a controlled DeepSeek RSI run."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Mapping

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

_MICRO_CNY_PER_CNY = Decimal("1000000")


class BudgetExceeded(RuntimeError):
    """Raised before forwarding when the worst-case reservation cannot fit."""


class LedgerLocked(RuntimeError):
    """Raised when another proxy process already owns the budget ledger."""


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    model: str
    budget_cny: Decimal
    input_cny_per_million: Decimal
    output_cny_per_million: Decimal
    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model is required")
        if self.budget_cny <= 0:
            raise ValueError("budget_cny must be positive")
        if self.input_cny_per_million < 0 or self.output_cny_per_million < 0:
            raise ValueError("prices must be non-negative")
        if self.max_input_tokens < 1 or self.max_output_tokens < 1:
            raise ValueError("token limits must be positive")

    @property
    def budget_micro_cny(self) -> int:
        return _ceil_decimal(self.budget_cny * _MICRO_CNY_PER_CNY)

    @property
    def worst_call_micro_cny(self) -> int:
        return self.cost_micro_cny(
            Usage(
                input_tokens=self.max_input_tokens,
                output_tokens=self.max_output_tokens,
            )
        )

    def cost_micro_cny(self, usage: Usage) -> int:
        if usage.input_tokens < 0 or usage.output_tokens < 0:
            raise ValueError("usage tokens must be non-negative")
        return _ceil_decimal(
            Decimal(usage.input_tokens) * self.input_cny_per_million
            + Decimal(usage.output_tokens) * self.output_cny_per_million
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "budget_cny": str(self.budget_cny),
            "input_cny_per_million": str(self.input_cny_per_million),
            "output_cny_per_million": str(self.output_cny_per_million),
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    worst_micro_cny: int


class BudgetLedger:
    """Persist worst-case reservations before any billable request is sent."""

    def __init__(self, path: str | Path, policy: BudgetPolicy) -> None:
        self.path = Path(path).expanduser().resolve()
        self.policy = policy
        self._lock = asyncio.Lock()
        self._settled = asyncio.Condition(self._lock)
        self._owned_reservations: set[str] = set()
        self._process_lock = _ProcessFileLock(self.path.with_suffix(f"{self.path.suffix}.lock"))
        try:
            self._state = self._load()
        except Exception:
            self._process_lock.close()
            raise

    def close(self) -> None:
        self._process_lock.close()

    def __del__(self) -> None:
        process_lock = getattr(self, "_process_lock", None)
        if process_lock is not None:
            process_lock.close()

    async def reserve(self) -> Reservation:
        async with self._settled:
            worst = self.policy.worst_call_micro_cny
            charged = int(self._state["charged_micro_cny"])
            while charged + worst > self.policy.budget_micro_cny:
                in_flight = sum(self._state["active_reservations"][key] for key in self._owned_reservations)
                if charged - in_flight + worst > self.policy.budget_micro_cny:
                    raise BudgetExceeded("RSI model budget cannot cover another worst-case call")
                await self._settled.wait()
                charged = int(self._state["charged_micro_cny"])
            reservation = Reservation(uuid.uuid4().hex, worst)
            self._state["charged_micro_cny"] = charged + worst
            self._state["reserved_calls"] = int(self._state["reserved_calls"]) + 1
            self._state["active_reservations"][reservation.reservation_id] = worst
            self._persist()
            self._owned_reservations.add(reservation.reservation_id)
            return reservation

    async def settle(self, reservation: Reservation, usage: Usage | None) -> None:
        async with self._settled:
            active = self._state["active_reservations"]
            persisted_worst = active.get(reservation.reservation_id)
            if persisted_worst is None:
                raise ValueError("reservation is not active")
            if int(persisted_worst) != reservation.worst_micro_cny:
                raise ValueError("reservation does not match persisted state")
            actual = None if usage is None else self.policy.cost_micro_cny(usage)
            if actual is not None and actual > reservation.worst_micro_cny:
                raise ValueError("reported usage exceeds the reserved model contract")
            active.pop(reservation.reservation_id)
            self._owned_reservations.discard(reservation.reservation_id)
            if usage is None:
                self._state["unmetered_calls"] = int(self._state["unmetered_calls"]) + 1
            else:
                assert actual is not None
                refund = reservation.worst_micro_cny - actual
                self._state["charged_micro_cny"] = int(self._state["charged_micro_cny"]) - refund
            self._state["settled_calls"] = int(self._state["settled_calls"]) + 1
            self._persist()
            self._settled.notify_all()

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._state))

    def public_snapshot(self) -> dict[str, Any]:
        state = self.snapshot()
        charged = int(state["charged_micro_cny"])
        return {
            "schema_version": state["schema_version"],
            "model": self.policy.model,
            "budget_cny": _micro_cny_string(self.policy.budget_micro_cny),
            "charged_cny": _micro_cny_string(charged),
            "remaining_cny": _micro_cny_string(self.policy.budget_micro_cny - charged),
            "active_reservations": len(state["active_reservations"]),
            "reserved_calls": state["reserved_calls"],
            "settled_calls": state["settled_calls"],
            "unmetered_calls": state["unmetered_calls"],
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {
                "schema_version": "1.0",
                "policy": self.policy.as_dict(),
                "charged_micro_cny": 0,
                "reserved_calls": 0,
                "settled_calls": 0,
                "unmetered_calls": 0,
                "active_reservations": {},
                "updated_at": None,
            }
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if state.get("schema_version") != "1.0" or state.get("policy") != self.policy.as_dict():
            raise ValueError("persisted budget policy does not match the requested policy")
        if not isinstance(state.get("active_reservations"), dict):
            raise ValueError("persisted budget ledger is invalid")
        numeric_fields = (
            "charged_micro_cny",
            "reserved_calls",
            "settled_calls",
            "unmetered_calls",
        )
        if any(
            not isinstance(state.get(field), int)
            or isinstance(state.get(field), bool)
            or state[field] < 0
            for field in numeric_fields
        ):
            raise ValueError("persisted budget ledger has invalid counters")
        reservations = state["active_reservations"]
        if any(
            not isinstance(key, str)
            or not key
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value != self.policy.worst_call_micro_cny
            for key, value in reservations.items()
        ):
            raise ValueError("persisted budget ledger has invalid reservations")
        charged = state["charged_micro_cny"]
        if charged > self.policy.budget_micro_cny or charged < sum(reservations.values()):
            raise ValueError("persisted budget ledger violates its budget invariant")
        return state

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state["updated_at"] = datetime.now(UTC).isoformat()
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


@dataclass(frozen=True, slots=True)
class ProxySettings:
    upstream_base_url: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 900.0

    def __post_init__(self) -> None:
        if not self.upstream_base_url.startswith("https://"):
            raise ValueError("upstream_base_url must use HTTPS")
        if not self.api_key:
            raise ValueError("api_key is required")


def bounded_chat_body(body: Mapping[str, Any], policy: BudgetPolicy) -> dict[str, Any]:
    result = dict(body)
    if str(result.get("model") or "").strip() != policy.model:
        raise ValueError("request model does not match the budget policy")
    if "max_completion_tokens" in result:
        raise ValueError("max_completion_tokens is not allowed; use max_tokens")
    if "best_of" in result:
        raise ValueError("best_of is not allowed")
    if "n" in result and result["n"] != 1:
        raise ValueError("n must be 1")
    raw_max = result.get("max_tokens")
    if raw_max is None:
        capped = policy.max_output_tokens
    elif isinstance(raw_max, int) and not isinstance(raw_max, bool) and raw_max > 0:
        capped = min(raw_max, policy.max_output_tokens)
    else:
        raise ValueError("max_tokens must be a positive integer")
    result["max_tokens"] = capped
    if result.get("stream") is True:
        options = dict(result.get("stream_options") or {})
        options["include_usage"] = True
        result["stream_options"] = options
    return result


def extract_usage(payload: bytes, content_type: str) -> Usage | None:
    if "text/event-stream" in content_type.lower():
        parser = SseUsageParser()
        parser.feed(payload)
        parser.finish()
        return parser.usage
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _usage_from_mapping(value.get("usage")) if isinstance(value, dict) else None


class SseUsageParser:
    """Incrementally retain only an incomplete SSE line and the last usage."""

    def __init__(self) -> None:
        self._buffer = b""
        self.usage: Usage | None = None
        self.done_seen = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        lines = self._buffer.split(b"\n")
        self._buffer = lines.pop()
        for line in lines:
            self._consume_line(line.rstrip(b"\r"))

    def finish(self) -> None:
        if self._buffer:
            self._consume_line(self._buffer.rstrip(b"\r"))
        self._buffer = b""

    def _consume_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        value = line[5:].strip()
        if value == b"[DONE]":
            self.done_seen = True
            return
        if not value:
            return
        try:
            candidate = json.loads(value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(candidate, dict):
            self.usage = _usage_from_mapping(candidate.get("usage")) or self.usage


def create_app(
    settings: ProxySettings,
    ledger: BudgetLedger,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", **ledger.public_snapshot()}

    async def forward_models() -> Response:
        return await _forward_unmetered(settings, "/models", upstream_transport)

    app.add_api_route("/models", forward_models, methods=["GET"])
    app.add_api_route("/v1/models", forward_models, methods=["GET"])

    async def forward_chat(request: Request) -> Response:
        try:
            raw_body = await request.json()
            if not isinstance(raw_body, dict):
                raise ValueError("request body must be an object")
            body = bounded_chat_body(raw_body, ledger.policy)
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            reservation = await ledger.reserve()
        except BudgetExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail={"code": "RSI_MODEL_BUDGET_EXCEEDED", "message": str(exc)},
            ) from exc

        client = httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            transport=upstream_transport,
        )
        try:
            upstream_request = client.build_request(
                "POST",
                f"{settings.upstream_base_url.rstrip('/')}/chat/completions",
                headers=_upstream_headers(settings.api_key),
                json=body,
            )
            response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            await ledger.settle(reservation, None)
            raise HTTPException(status_code=502, detail="DeepSeek upstream request failed") from exc

        headers = _response_headers(response)
        if body.get("stream") is True:
            return StreamingResponse(
                _stream_upstream(response, client, ledger, reservation),
                status_code=response.status_code,
                headers=headers,
            )

        usage = None
        try:
            content = await response.aread()
            usage = extract_usage(content, response.headers.get("content-type", ""))
        except BaseException:
            await asyncio.shield(ledger.settle(reservation, None))
            raise
        finally:
            await response.aclose()
            await client.aclose()
        await ledger.settle(reservation, usage)
        return Response(content=content, status_code=response.status_code, headers=headers)

    app.add_api_route("/chat/completions", forward_chat, methods=["POST"])
    app.add_api_route("/v1/chat/completions", forward_chat, methods=["POST"])
    return app


async def _stream_upstream(
    response: httpx.Response,
    client: httpx.AsyncClient,
    ledger: BudgetLedger,
    reservation: Reservation,
):
    parser = SseUsageParser()
    completed = False
    try:
        async for chunk in response.aiter_bytes():
            parser.feed(chunk)
            yield chunk
        parser.finish()
        completed = True
    finally:
        await response.aclose()
        await client.aclose()
        await asyncio.shield(
            ledger.settle(reservation, parser.usage if completed or parser.done_seen else None)
        )


async def _forward_unmetered(
    settings: ProxySettings,
    path: str,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> Response:
    try:
        async with httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            transport=upstream_transport,
        ) as client:
            response = await client.get(
                f"{settings.upstream_base_url.rstrip('/')}{path}",
                headers=_upstream_headers(settings.api_key),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="DeepSeek upstream request failed") from exc
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=_response_headers(response),
    )


def _usage_from_mapping(value: Any) -> Usage | None:
    if not isinstance(value, Mapping):
        return None
    input_tokens = value.get("input_tokens", value.get("prompt_tokens"))
    output_tokens = value.get("output_tokens", value.get("completion_tokens"))
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0:
        return None
    if not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or output_tokens < 0:
        return None
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens)


def _upstream_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _response_headers(response: httpx.Response) -> dict[str, str]:
    allowed = {"content-type", "x-request-id", "request-id"}
    return {key: value for key, value in response.headers.items() if key.lower() in allowed}


def _ceil_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _micro_cny_string(value: int) -> str:
    return format(Decimal(value) / _MICRO_CNY_PER_CNY, ".6f")


class _ProcessFileLock:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a+b")
        if self._stream.seek(0, os.SEEK_END) == 0:
            self._stream.write(b"0")
            self._stream.flush()
        self._stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._stream.close()
            raise LedgerLocked("another budget proxy already owns this ledger") from exc

    def close(self) -> None:
        if self._stream.closed:
            return
        try:
            self._stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()


def validate_loopback_host(host: str) -> str:
    normalized = str(host or "").strip()
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(normalized, None)}
    except socket.gaierror as exc:
        raise ValueError("proxy host must resolve to loopback") from exc
    if not addresses or any(address not in {"127.0.0.1", "::1"} for address in addresses):
        raise ValueError("proxy host must resolve only to loopback")
    return normalized


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--budget-cny", default="19.90")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19080)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    env = _read_env_file(args.env_file.expanduser().resolve())
    policy = BudgetPolicy(
        model=env.get("DEEPSEEK_MODEL", "deepseek-flash"),
        budget_cny=Decimal(args.budget_cny),
        input_cny_per_million=Decimal("2"),
        output_cny_per_million=Decimal("8"),
        max_input_tokens=1_000_000,
        max_output_tokens=32_768,
    )
    settings = ProxySettings(
        upstream_base_url=env.get("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
        api_key=env.get("DEEPSEEK_API_KEY", ""),
    )
    ledger = BudgetLedger(args.ledger, policy)
    host = validate_loopback_host(args.host)
    uvicorn.run(create_app(settings, ledger), host=host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
