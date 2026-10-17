"""Local handoff for SkillHub-hosted OAuth; no provider secret is stored here."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

_PROVIDERS = frozenset({"gitcode", "github"})
_ATTEMPT_TTL = 10 * 60
_lock = threading.Lock()
_attempts: dict[str, dict] = {}


def _hub_base() -> str:
    base = (
        os.getenv("SKILLHUB_OAUTH_BASE_URL")
        or os.getenv("TEAM_SKILLS_HUB_BASE_URL")
        or "https://swarmskills.openjiuwen.com"
    ).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme == "https" and parsed.netloc and not parsed.path:
        return base
    # A loopback mock is useful for local integration tests only.
    if parsed.scheme == "http" and not parsed.path:
        if parsed.hostname == "127.0.0.1" and parsed.port:
            return base
    # The published SkillHub test deployment uses HTTP; keep this exception exact.
    if base == "http://119.8.233.112:8080":
        return base
    raise ValueError("invalid SkillHub OAuth base URL")


def _prune() -> None:
    now = time.monotonic()
    for flow, attempt in list(_attempts.items()):
        if now - attempt["created"] > _ATTEMPT_TTL:
            del _attempts[flow]


def start(provider: str, host: str) -> tuple[int, dict]:
    if provider not in _PROVIDERS:
        return 400, {"error": "invalid_provider"}
    try:
        address = urlsplit(f"http://{host}")
        if address.hostname not in {"127.0.0.1", "localhost"} or not address.port or address.path:
            raise ValueError("invalid host")
        base = _hub_base()
    except ValueError:
        return 400, {"error": "invalid_configuration"}
    flow, claim = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    callback_host = f"127.0.0.1:{address.port}"
    redirect_to = f"http://{callback_host}/oauth/hub/callback?{urlencode({'flow': flow})}"
    with _lock:
        _prune()
        _attempts[flow] = {"provider": provider, "claim": claim, "created": time.monotonic(), "status": "pending"}
    authorize_url = f"{base}/api/v1/auth/oauth/{provider}/start?{urlencode({'redirect_to': redirect_to})}"
    return 200, {"authorize_url": authorize_url, "flow": flow, "claim": claim}


def complete(query: dict[str, str]) -> tuple[int, dict]:
    flow = query.get("flow", "")
    provider = query.get("oauth_provider", "")
    with _lock:
        _prune()
        attempt = _attempts.get(flow)
        if not attempt:
            return 404, {"error": "unknown_flow"}
        if attempt["status"] != "pending" or (provider and provider != attempt["provider"]):
            return 400, {"error": "invalid_callback"}
        provider = attempt["provider"]
        attempt["status"] = "processing"

    error = query.get("oauth_error") or query.get("oauth_error_code")
    if error:
        outcome = {"status": "failed", "error": str(error)[:300]}
    else:
        session = query.get("oauth_session", "")
        if not 8 <= len(session) <= 256:
            outcome = {"status": "failed", "error": "missing_session"}
        else:
            try:
                hub_base = _hub_base()
                request = Request(
                    f"{hub_base}/api/v1/auth/oauth/{provider}/session",
                    data=json.dumps({"session": session}).encode(),
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                # This one HTTP test deployment is directly reachable even when a global proxy is configured.
                open_request = (
                    build_opener(ProxyHandler({})).open
                    if hub_base == "http://119.8.233.112:8080"
                    else urlopen
                )
                with open_request(request, timeout=15) as response:
                    body = json.loads(response.read(65536))
                data = body.get("data") if isinstance(body, dict) else None
                token = data.get("access_token") if isinstance(data, dict) else None
                if not isinstance(token, str) or not token:
                    raise ValueError("invalid session response")
                outcome = {
                    "status": "complete", "provider": provider,
                    "access_token": token, "user": data.get("user"),
                }
            except (OSError, ValueError):
                outcome = {"status": "failed", "error": "session_exchange_failed"}
    with _lock:
        attempt.update(outcome)
    return 200, {"status": outcome["status"]}


def result(flow: str, claim: str) -> tuple[int, dict]:
    with _lock:
        _prune()
        attempt = _attempts.get(flow)
        if not attempt or not claim or not hmac.compare_digest(attempt["claim"], claim):
            return 404, {"error": "unknown_flow"}
        if attempt["status"] in {"pending", "processing"}:
            return 200, {"status": "pending"}
        del _attempts[flow]
        return 200, {
            key: attempt[key]
            for key in ("status", "provider", "access_token", "user", "error")
            if key in attempt
        }
