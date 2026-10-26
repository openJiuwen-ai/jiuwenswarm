#!/usr/bin/env python3
"""Live smoke test against Gateway Web HTTP (enterprise browser APIs)."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

BASE = "http://localhost:19002"
TIMEOUT = 30


def join_url(path: str) -> str:
    return urllib.parse.urljoin(BASE.rstrip("/") + "/", path.lstrip("/"))


@dataclass
class Result:
    group: str
    name: str
    method: str
    path: str
    status: str  # PASS | FAIL | SKIP | WARN
    http_code: int | None = None
    rpc_method: str | None = None
    note: str = ""
    detail: str = ""


results: list[Result] = []
session_id: str | None = None
cron_job_id: str | None = None


def req(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, str], str]:
    url = join_url(path)
    hdrs = {"Accept": "application/json", "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = None
    if raw_body is not None:
        data = raw_body
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, dict(resp.headers), text
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        return e.code, dict(e.headers), text


def record(result: Result) -> None:
    results.append(result)


def expect_ok_json(
    group: str,
    name: str,
    method: str,
    path: str,
    *,
    rpc: str | None = None,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    allow_codes: set[int] | None = None,
) -> dict[str, Any] | None:
    allow = allow_codes or {200, 201}
    try:
        code, hdrs, text = req(method, path, body=body, headers=headers)
        rpc_hdr = hdrs.get("X-Web-RPC-Method") or hdrs.get("x-web-rpc-method")
        if code not in allow:
            record(
                Result(
                    group, name, method, path, "FAIL", code, rpc or rpc_hdr,
                    f"HTTP {code}", text[:500],
                ),
            )
            return None
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            record(Result(group, name, method, path, "FAIL", code, rpc, "invalid json", text[:500]))
            return None
        if isinstance(payload, dict) and payload.get("ok") is False:
            err = payload.get("error", {})
            msg = err.get("message") if isinstance(err, dict) else str(err)
            # Some endpoints legitimately return business errors in dev
            record(
                Result(
                    group, name, method, path, "WARN", code, rpc or rpc_hdr,
                    msg or "ok=false", text[:500],
                ),
            )
            return payload
        record(Result(group, name, method, path, "PASS", code, rpc or rpc_hdr))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:  # noqa: BLE001
        record(Result(group, name, method, path, "FAIL", None, rpc, str(exc)))
        return None


def test_doc_and_catalog() -> None:
    for path, name in [("/doc", "Swagger UI"), ("/openapi.json", "OpenAPI")]:
        try:
            code, _, _ = req("GET", path, headers={"Accept": "text/html"})
            record(Result("infra", name, "GET", path, "PASS" if code == 200 else "FAIL", code))
        except Exception as exc:  # noqa: BLE001
            record(Result("infra", name, "GET", path, "FAIL", note=str(exc)))

    code, _, text = req("GET", "/api/v1/catalog")
    try:
        cat = json.loads(text)
        n = len(cat.get("data", {}).get("routes", []))
        record(
            Result(
                "infra",
                "catalog",
                "GET",
                "/api/v1/catalog",
                "PASS" if cat.get("ok") and n > 50 else "WARN",
                code,
                note=f"{n} routes",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        record(Result("infra", "catalog", "GET", "/api/v1/catalog", "FAIL", code, note=str(exc)))


def test_core() -> None:
    global session_id
    expect_ok_json("core", "health", "GET", "/api/v1/health")
    expect_ok_json(
        "core",
        "connection.status",
        "GET",
        "/api/v1/connection/status",
        rpc="connection.status",
    )
    expect_ok_json("core", "session.list", "GET", "/api/v1/sessions", rpc="session.list")
    created = expect_ok_json(
        "core",
        "session.create",
        "POST",
        "/api/v1/sessions",
        rpc="session.create",
        body={},
        allow_codes={200, 201},
    )
    if created and isinstance(created.get("data"), dict):
        session_id = str(created["data"].get("session_id") or "")
    if not session_id:
        record(Result("core", "session.create", "POST", "/api/v1/sessions", "FAIL", note="no session_id"))
        return

    sid = session_id
    expect_ok_json(
        "core",
        "session.get_metadata",
        "GET",
        f"/api/v1/sessions/{sid}",
        rpc="session.get_metadata",
    )
    expect_ok_json(
        "core",
        "session.rename",
        "PATCH",
        f"/api/v1/sessions/{sid}",
        rpc="session.rename",
        body={"title": "http-smoke-test"},
    )

    # SSE chat - read first chunk only (before history: empty session returns 404)
    try:
        url = join_url("/api/v1/chat/completions")
        body = json.dumps(
            {"session_id": sid, "query": "reply with exactly: pong", "enable_streaming": True},
        ).encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as resp:
            chunk = resp.read(4096).decode("utf-8", errors="replace")
            ok = resp.status == 200 and ("event:" in chunk or "data:" in chunk)
            record(
                Result(
                    "core",
                    "chat.completions (SSE)",
                    "POST",
                    "/api/v1/chat/completions",
                    "PASS" if ok else "WARN",
                    resp.status,
                    "chat.send",
                    detail=chunk[:200],
                ),
            )
    except Exception as exc:  # noqa: BLE001
        record(
            Result(
                "core",
                "chat.completions (SSE)",
                "POST",
                "/api/v1/chat/completions",
                "FAIL",
                rpc_method="chat.send",
                note=str(exc),
            ),
        )

    expect_ok_json(
        "core",
        "history.get (json)",
        "GET",
        f"/api/v1/sessions/{sid}/history",
        rpc="history.get",
        allow_codes={200, 404},
    )

    expect_ok_json(
        "core",
        "chat.interrupt",
        "POST",
        f"/api/v1/chat/{sid}/actions/interrupt",
        rpc="chat.interrupt",
        body={},
        allow_codes={200, 400, 409},
    )


def test_settings() -> None:
    expect_ok_json("settings", "config.get", "GET", "/api/v1/config", rpc="config.get")
    expect_ok_json("settings", "models.list", "GET", "/api/v1/models", rpc="models.list")
    loc = expect_ok_json("settings", "locale.get", "GET", "/api/v1/locale", rpc="locale.get_conf")
    lang = "en"
    if loc and isinstance(loc.get("data"), dict):
        cur = str(loc["data"].get("preferred_language") or "zh")
        lang = "en" if cur == "zh" else "zh"
    expect_ok_json(
        "settings",
        "locale.set",
        "PUT",
        "/api/v1/locale",
        rpc="locale.set_conf",
        body={"preferred_language": lang},
    )
    jobs = expect_ok_json(
        "settings",
        "cron.job.list",
        "GET",
        "/api/v1/cron/jobs?project_id=default",
        rpc="cron.job.list",
    )
    global cron_job_id
    if jobs and isinstance(jobs.get("data"), dict):
        items = jobs["data"].get("jobs") or []
        if items and isinstance(items[0], dict):
            cron_job_id = str(items[0].get("id") or "")
    if cron_job_id:
        jid = cron_job_id
        expect_ok_json(
            "settings",
            "cron.job.get",
            "GET",
            f"/api/v1/cron/jobs/{jid}?project_id=default",
            rpc="cron.job.get",
        )
        expect_ok_json(
            "settings",
            "cron.job.preview",
            "POST",
            f"/api/v1/cron/jobs/{jid}/actions/preview?project_id=default",
            rpc="cron.job.preview",
            body={"count": 1},
        )


def test_workspace_sample() -> None:
    expect_ok_json(
        "workspace",
        "permissions.tools.get",
        "GET",
        "/api/v1/permissions/tools",
        rpc="permissions.tools.get",
    )
    expect_ok_json(
        "workspace",
        "permissions.owner_scopes.get",
        "GET",
        "/api/v1/permissions/owner-scopes",
        rpc="permissions.owner_scopes.get",
    )
    expect_ok_json(
        "workspace",
        "skills.list",
        "GET",
        "/api/v1/skills",
        rpc="skills.list",
    )
    expect_ok_json(
        "workspace",
        "skills.installed",
        "GET",
        "/api/v1/skills/installed",
        rpc="skills.installed",
    )
    expect_ok_json(
        "workspace",
        "harness.packages",
        "GET",
        "/api/v1/harness/packages",
        rpc="harness.packages",
    )


def test_enterprise_compat() -> None:
    expect_ok_json(
        "enterprise",
        "GET /api/sessions (history store)",
        "GET",
        "/api/sessions?limit=5",
        allow_codes={200},
    )
    if session_id:
        expect_ok_json(
            "enterprise",
            "GET /api/sessions/{id}",
            "GET",
            f"/api/sessions/{session_id}",
            allow_codes={200, 404},
        )


def test_file_api() -> None:
    expect_ok_json(
        "file",
        "list-files (workspace)",
        "GET",
        "/file-api/list-files?dir=agent%2Fworkspace",
        allow_codes={200},
    )
    expect_ok_json(
        "file",
        "ws-debug-config",
        "GET",
        "/file-api/ws-debug-config",
        allow_codes={200},
    )
    # path traversal should be blocked
    code, _, text = req("GET", "/file-api/list-files?dir=..%2F")
    try:
        payload = json.loads(text)
        blocked = code == 403 or payload.get("error")
        record(
            Result(
                "file",
                "path traversal blocked",
                "GET",
                "/file-api/list-files?dir=../",
                "PASS" if blocked else "FAIL",
                code,
                detail=text[:200],
            ),
        )
    except Exception:  # noqa: BLE001
        record(
            Result(
                "file",
                "path traversal blocked",
                "GET",
                "/file-api/list-files?dir=../",
                "PASS" if code == 403 else "WARN",
                code,
            ),
        )


def test_mapped_routes_spot_check() -> None:
    """Spot-check a few table-driven routes from catalog."""
    samples = [
        ("GET", "/api/v1/permissions/rules", "permissions.rules.get"),
        ("GET", "/api/v1/skills/retrieval/status", "skills.retrieval.status"),
        ("GET", "/api/v1/skills/evolution/status", "skills.evolution.status"),
    ]
    for method, path, rpc in samples:
        expect_ok_json("workspace", path, method, path, rpc=rpc)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("Gateway Web HTTP smoke test @ %s", BASE)
    logger.info("%s", "=" * 60)
    test_doc_and_catalog()
    test_core()
    test_settings()
    test_workspace_sample()
    test_mapped_routes_spot_check()
    test_enterprise_compat()
    test_file_api()

    counts = {"PASS": 0, "FAIL": 0, "WARN": 0, "SKIP": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    logger.info(
        "\nSummary: PASS=%s WARN=%s FAIL=%s",
        counts["PASS"],
        counts["WARN"],
        counts["FAIL"],
    )
    logger.info("%s", "-" * 60)
    for r in results:
        code = r.http_code if r.http_code is not None else "-"
        rpc = f" [{r.rpc_method}]" if r.rpc_method else ""
        extra = f" — {r.note}" if r.note else ""
        logger.info("[%s] %s %s %s%s (%s)%s", r.status, r.group, r.method, r.path, rpc, code, extra)

    # JSON for report generation
    out_path = os.path.join("scripts", "gateway_http_smoke_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "base": BASE,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "session_id": session_id,
                "summary": counts,
                "results": [r.__dict__ for r in results],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    logger.info("\nWrote %s", out_path)
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
