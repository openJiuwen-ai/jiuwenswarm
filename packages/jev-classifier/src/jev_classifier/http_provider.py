"""JEV typed-choice adapter using the existing repository's wire contract."""

import json
import math
from dataclasses import dataclass, field
from typing import Any, cast

import httpx

from .classifier import Decision
from .errors import InvalidResponseError, JevTimeoutError, ProviderError
from .prompts import CRITERIA, INSTRUCTIONS


@dataclass(frozen=True, slots=True)
class JevHttpConfig:
    endpoint: str
    model: str
    api_key: str = field(repr=False)
    max_response_bytes: int = 65536

    def __post_init__(self) -> None:
        for name in ("endpoint", "model", "api_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty text")
        try:
            url = httpx.URL(self.endpoint)
        except (httpx.InvalidURL, ValueError):
            raise ValueError("invalid JEV endpoint") from None
        if (url.scheme not in ("http", "https") or not url.host
                or url.userinfo or url.query or url.fragment):
            raise ValueError("endpoint must be HTTP(S), without credentials, query or fragment")
        if type(self.max_response_bytes) is not int or self.max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")


def _probability(value: Any) -> float:
    # The range test also rejects NaN/infinity without converting huge integers.
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= 1):
        raise InvalidResponseError("probabilities and confidence must be numbers in [0, 1]")
    return float(value)


def parse_response(body: Any) -> Decision:
    """Validate provider choice and probabilities without changing the choice."""
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        raise InvalidResponseError("missing answers object")
    answer = body["answers"].get("action")
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise InvalidResponseError("missing action choice")
    choice = answer.get("choice")
    if type(choice) is not str or choice not in ("INTERRUPT", "APPEND"):
        raise InvalidResponseError("invalid action choice")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != {"INTERRUPT", "APPEND"}:
        raise InvalidResponseError("expected exactly INTERRUPT and APPEND probabilities")
    interrupt = _probability(probabilities["INTERRUPT"])
    append = _probability(probabilities["APPEND"])
    _probability(answer.get("confidence"))
    if not math.isclose(interrupt + append, 1.0, rel_tol=0, abs_tol=1e-5):
        raise InvalidResponseError("probabilities must sum to one")
    if probabilities[choice] < max(interrupt, append):
        raise InvalidResponseError("choice disagrees with probabilities")
    return cast(Decision, choice)


class JevHttpProvider:
    """One HTTP request, no redirects/retries; injected client stays caller-owned.

    Use through JevClassifier, which supplies the total deadline including body
    streaming. If no client is injected, a short-lived client is created per call.
    """

    def __init__(self, config: JevHttpConfig, *, client: httpx.AsyncClient | None = None):
        self.config = config
        self.client = client

    async def decide(self, *, context: str, messages: tuple[str, ...]) -> Decision:
        payload = {"model": self.config.model,
                   "state": {"context": context, "messages": list(messages)},
                   "questions": {"action": {
                       "type": "choice", "instructions": INSTRUCTIONS,
                       "criteria": dict(CRITERIA),
                   }}}
        client = self.client if self.client is not None else httpx.AsyncClient()
        try:
            async with client.stream(
                "POST", self.config.endpoint,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=payload, follow_redirects=False, timeout=None,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise ProviderError(f"JEV HTTP status {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self.config.max_response_bytes:
                        raise InvalidResponseError("JEV response exceeds max_response_bytes")
                    body.extend(chunk)
            try:
                parsed = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise InvalidResponseError("JEV response is not valid JSON") from None
            return parse_response(parsed)
        except httpx.TimeoutException:
            raise JevTimeoutError("JEV HTTP request timed out") from None
        except httpx.HTTPError:
            raise ProviderError("JEV HTTP request failed") from None
        finally:
            if self.client is None:
                await client.aclose()

