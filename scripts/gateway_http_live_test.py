#!/usr/bin/env python3
"""Catalog-driven live test for Gateway Web HTTP enterprise browser APIs."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)
BASE = "http://localhost:19002"
GET_TIMEOUT = 45
WRITE_SKIP = True  # avoid mutating permissions/skills/harness on live env


def join_url(path: str) -> str:
    return urllib.parse.urljoin(BASE.rstrip("/") + "/", path.lstrip("/"))


def join_api_path(*segments: str) -> str:
    """Join HTTP API path segments (urllib.parse, not filesystem paths)."""
    if not segments:
        return "/"
    path = segments[0]
    for segment in segments[1:]:
        path = urllib.parse.urljoin(path.rstrip("/") + "/", segment.lstrip("/"))
    return path


@dataclass
class ClassifyRequest:
    group: str
    method: str
    path: str
    rpc: str | None
    code: int
    text: str
    ms: int
    allow_biz: bool = True


@dataclass
class Row:
    group: str
    method: str
    path: str
    rpc: str | None
    status: str
    http_code: int | None = None
    ms: int = 0
    note: str = ""


rows: list[Row] = []
session_id: str | None = None
cron_job_id: str | None = None
skill_name: str | None = None


def http(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = GET_TIMEOUT,
) -> tuple[int, dict[str, str], str, int]:
    url = join_url(path)
    hdrs = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Request-Id": uuid.uuid4().hex,
    }
    if session_id:
        hdrs["X-Session-Id"] = session_id
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            ms = int((time.perf_counter() - t0) * 1000)
            return resp.status, dict(resp.headers), text, ms
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        ms = int((time.perf_counter() - t0) * 1000)
        return e.code, dict(e.headers), text, ms


def add(row: Row) -> None:
    rows.append(row)


def parse_json(text: str) -> dict[str, Any] | None:
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


def classify(req: ClassifyRequest) -> None:
    payload = parse_json(req.text)
    rpc_hdr = None
    if payload and isinstance(payload.get("metadata"), dict):
        rpc_hdr = payload["metadata"].get("rpc_method")
    if req.code in (200, 201) and payload and payload.get("ok") is True:
        add(Row(req.group, req.method, req.path, req.rpc or rpc_hdr, "PASS", req.code, req.ms))
        return
    if req.allow_biz and req.code in (400, 404, 409) and payload:
        msg = ""
        err = payload.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("code") or "")
        add(
            Row(
                req.group,
                req.method,
                req.path,
                req.rpc or rpc_hdr,
                "BIZ",
                req.code,
                req.ms,
                msg or "business response",
            ),
        )
        return
    if req.code == 403 and req.path.startswith("/file-api"):
        add(
            Row(
                req.group,
                req.method,
                req.path,
                req.rpc,
                "BIZ",
                req.code,
                req.ms,
                "forbidden path/dir (expected for bad dir)",
            ),
        )
        return
    if req.code == 200 and req.path.startswith(("/api/sessions", "/file-api/", "/share-api/")):
        add(
            Row(
                req.group,
                req.method,
                req.path,
                req.rpc,
                "PASS",
                req.code,
                req.ms,
                "legacy compat JSON (no ok envelope)",
            ),
        )
        return
    add(Row(req.group, req.method, req.path, req.rpc or rpc_hdr, "FAIL", req.code, req.ms, req.text[:180]))


def fill_path(path: str) -> str | None:
    global cron_job_id, skill_name
    if "{session_id}" in path:
        if not session_id:
            return None
        path = path.replace("{session_id}", session_id)
    if "{id}" in path:
        if "/cron/jobs/" in path:
            if not cron_job_id:
                return None
            path = path.replace("{id}", cron_job_id)
        else:
            path = path.replace("{id}", "test-rule-id")
    if "{tool}" in path:
        path = path.replace("{tool}", "bash")
    if "{name}" in path:
        if not skill_name:
            return None
        path = path.replace("{name}", urllib.parse.quote(skill_name, safe=""))
    if "{slug}" in path:
        path = path.replace("{slug}", "demo")
    if "{package_id}" in path:
        path = path.replace("{package_id}", "native")
    return path


def query_for(path: str, rpc: str | None) -> str:
    q: dict[str, str] = {}
    if "/cron/" in path:
        q["project_id"] = "default"
    if rpc and "clawhub.search" in rpc:
        q.update({"q": "demo", "limit": "1"})
    if rpc and "enterprise.list" in (rpc or ""):
        q.update({"group_id": "g1", "bot_id": "b1", "user_id": "u1"})
    if "evolution" in path or (rpc and "evolution" in rpc):
        if session_id:
            q["session_id"] = session_id
    if "skillnet.install_status" in (rpc or ""):
        q["install_id"] = "test-install-id"
    if "evolution" in (rpc or ""):
        q["name"] = skill_name or "demo-skill"
    if not q:
        return ""
    return "?" + urllib.parse.urlencode(q)


def bootstrap() -> None:
    global session_id, cron_job_id, skill_name
    code, _, text, ms = http("GET", "/api/v1/health")
    classify(ClassifyRequest("infra", "GET", "/api/v1/health", None, code, text, ms))
    code, _, text, ms = http("GET", "/api/v1/connection/status", timeout=15)
    classify(
        ClassifyRequest(
            "infra", "GET", "/api/v1/connection/status", "connection.status", code, text, ms,
        ),
    )

    code, _, text, ms = http("POST", "/api/v1/sessions", body={})
    payload = parse_json(text)
    if payload and payload.get("ok"):
        sid = payload.get("data", {}).get("session_id")
        if sid:
            session_id = str(sid)
    classify(ClassifyRequest("core", "POST", "/api/v1/sessions", "session.create", code, text, ms))

    code, _, text, ms = http("GET", "/api/v1/sessions")
    classify(ClassifyRequest("core", "GET", "/api/v1/sessions", "session.list", code, text, ms))

    cron_path = join_api_path("/api/v1/cron/jobs") + query_for("/cron/jobs", "cron.job.list")
    code, _, text, ms = http("GET", cron_path)
    payload = parse_json(text)
    if payload and isinstance(payload.get("data"), dict):
        jobs = payload["data"].get("jobs") or []
        if jobs and isinstance(jobs[0], dict):
            cron_job_id = str(jobs[0].get("id") or "")
    classify(ClassifyRequest("settings", "GET", "/api/v1/cron/jobs", "cron.job.list", code, text, ms))

    code, _, text, ms = http("GET", "/api/v1/skills/installed")
    payload = parse_json(text)
    if payload and isinstance(payload.get("data"), dict):
        plugins = payload["data"].get("plugins") or payload["data"].get("skills") or []
        if plugins and isinstance(plugins[0], dict):
            skill_name = str(plugins[0].get("name") or plugins[0].get("skill_name") or "")
    classify(
        ClassifyRequest("workspace", "GET", "/api/v1/skills/installed", "skills.installed", code, text, ms),
    )


def test_catalog_gets() -> None:
    _, _, text, _ = http("GET", "/api/v1/catalog", timeout=15)
    cat = parse_json(text)
    if not cat or not cat.get("ok"):
        add(Row("infra", "GET", "/api/v1/catalog", None, "FAIL", None, 0, "catalog unavailable"))
        return
    routes = cat.get("data", {}).get("routes", [])
    add(Row("infra", "GET", "/api/v1/catalog", None, "PASS", 200, 0, f"{len(routes)} routes"))

    for r in routes:
        method = str(r.get("http_method") or "").upper()
        if method != "GET":
            continue
        raw_path = str(r.get("path") or "")
        rpc = r.get("rpc_method")
        group = str(r.get("group") or r.get("phase") or "other")
        if raw_path in {"/api/v1/health", "/api/v1/catalog"}:
            continue
        if "/history" in raw_path and "stream" in raw_path:
            add(Row(group, method, raw_path, rpc, "SKIP", None, 0, "SSE history stream — manual"))
            continue
        rel = raw_path.replace("/api/v1", "")
        filled = fill_path(rel)
        if filled is None:
            add(Row(group, method, raw_path, rpc, "SKIP", None, 0, "missing fixture id"))
            continue
        full = join_api_path("/api/v1", filled.lstrip("/")) + query_for(
            filled, str(rpc) if rpc else None,
        )
        try:
            code, _, text, ms = http(method, full)
            classify(
                ClassifyRequest(group, method, full, str(rpc) if rpc else None, code, text, ms),
            )
        except Exception as exc:  # noqa: BLE001
            add(Row(group, method, full, str(rpc) if rpc else None, "FAIL", None, 0, str(exc)))


def test_core_writes() -> None:
    if not session_id:
        return
    sid = session_id
    code, _, text, ms = http(
        "PATCH",
        f"/api/v1/sessions/{sid}",
        body={"title": "enterprise-http-smoke"},
    )
    classify(ClassifyRequest("core", "PATCH", f"/api/v1/sessions/{sid}", "session.rename", code, text, ms))

    # SSE chat
    url = join_url("/api/v1/chat/completions")
    body = json.dumps(
        {"session_id": sid, "query": "reply with exactly: pong", "enable_streaming": True},
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "X-Request-Id": uuid.uuid4().hex,
            "X-Session-Id": sid,
        },
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            chunk = resp.read(8192).decode("utf-8", errors="replace")
            ms = int((time.perf_counter() - t0) * 1000)
            ok = resp.status == 200 and ("event:" in chunk or "data:" in chunk)
            add(
                Row(
                    "core",
                    "POST",
                    "/api/v1/chat/completions",
                    "chat.send",
                    "PASS" if ok else "WARN",
                    resp.status,
                    ms,
                    chunk[:120],
                ),
            )
    except Exception as exc:  # noqa: BLE001
        add(Row("core", "POST", "/api/v1/chat/completions", "chat.send", "FAIL", None, 0, str(exc)))

    code, _, text, ms = http("GET", f"/api/v1/sessions/{sid}/history")
    classify(ClassifyRequest("core", "GET", f"/api/v1/sessions/{sid}/history", "history.get", code, text, ms))

    code, _, text, ms = http(
        "POST",
        f"/api/v1/chat/{sid}/actions/interrupt",
        body={},
    )
    classify(
        ClassifyRequest(
            "core",
            "POST",
            f"/api/v1/chat/{sid}/actions/interrupt",
            "chat.interrupt",
            code,
            text,
            ms,
            allow_biz=True,
        ),
    )


def test_file_compat() -> None:
    cases = [
        ("GET", "/file-api/ws-debug-config", None),
        ("GET", "/file-api/list-files?dir=", None),
        ("GET", "/file-api/list-markdown?dir=", None),
        ("GET", "/file-api/list-files?dir=..%2F", "path traversal"),
        (
            "GET",
            join_api_path("/share-api/snapshot")
            + "?"
            + urllib.parse.urlencode({"session_id": session_id or "missing"}),
            None,
        ),
    ]
    for method, path, note in cases:
        p = path
        if p.endswith("=") and session_id:
            p = f"{p}{session_id}"
        try:
            code, _, text, ms = http(method, p, timeout=20)
            if note == "path traversal":
                payload = parse_json(text)
                verdict = "PASS" if code == 403 or (payload and payload.get("error")) else "FAIL"
                add(Row("file", method, p, None, verdict, code, ms, note))
            else:
                classify(ClassifyRequest("file", method, p, None, code, text, ms, allow_biz=True))
        except Exception as exc:  # noqa: BLE001
            add(Row("file", method, p, None, "FAIL", None, 0, str(exc)))


def test_enterprise_sessions() -> None:
    code, _, text, ms = http("GET", "/api/sessions?limit=10")
    classify(ClassifyRequest("enterprise", "GET", "/api/sessions", None, code, text, ms))
    if session_id:
        code, _, text, ms = http("GET", f"/api/sessions/{session_id}")
        classify(ClassifyRequest("enterprise", "GET", f"/api/sessions/{session_id}", None, code, text, ms))


def test_openapi() -> None:
    for p in ("/doc", "/openapi.json"):
        try:
            req = urllib.request.Request(join_url(p), headers={"Accept": "*/*"}, method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                add(Row("infra", "GET", p, None, "PASS", resp.status, 0))
        except Exception as exc:  # noqa: BLE001
            add(Row("infra", "GET", p, None, "FAIL", None, 0, str(exc)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    test_openapi()
    bootstrap()
    test_catalog_gets()
    test_core_writes()
    test_enterprise_sessions()
    test_file_compat()

    summary: dict[str, int] = {}
    for r in rows:
        summary[r.status] = summary.get(r.status, 0) + 1

    out = {
        "base": BASE,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "cron_job_id": cron_job_id,
        "skill_name": skill_name,
        "summary": summary,
        "results": [r.__dict__ for r in rows],
    }
    out_path = os.path.join("scripts", "gateway_http_live_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    logger.info("%s", json.dumps(summary, ensure_ascii=False))
    for r in rows:
        if r.status in ("FAIL", "WARN"):
            logger.info("[%s] %s %s %s", r.status, r.method, r.path, r.note)


if __name__ == "__main__":
    main()
