from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DB_NAME = "memory.sqlite3"


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


def tokens(value: Any) -> list[str]:
    return re.findall(r"\w+", normalize(value), flags=re.UNICODE)


def content_hash(record: dict[str, Any]) -> str:
    semantic: dict[str, Any] = {}
    semantic_keys = (
        "id", "memory_id", "priority", "content", "queries", "must_include",
        "evidence_refs", "source", "tags", "status",
    )
    for key in semantic_keys:
        semantic[key] = record.get(key)
    payload = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def root_path(value: str | Path | None = None) -> Path:
    return Path(value or Path.cwd()).expanduser().resolve()


def connect(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / DB_NAME, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_project(root: Path) -> dict[str, Any]:
    with connect(root) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS uts(
              id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, priority INTEGER NOT NULL,
              status TEXT NOT NULL, build_state TEXT NOT NULL, content TEXT NOT NULL,
              queries_json TEXT NOT NULL, must_include_json TEXT NOT NULL,
              evidence_refs_json TEXT NOT NULL, source TEXT NOT NULL, tags_json TEXT NOT NULL,
              content_hash TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS built_docs(
              ut_id TEXT PRIMARY KEY REFERENCES uts(id) ON DELETE CASCADE,
              content_hash TEXT NOT NULL, document TEXT NOT NULL, built_at TEXT NOT NULL
            );
            """
        )
        defaults = {
            "schema_version": SCHEMA_VERSION,
            "memory_revision": 0,
            "snapshot_revision": 0,
            "covered_through": 0,
            "snapshot": {},
        }
        for key, value in defaults.items():
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", (key, json.dumps(value)))
    return {"status": "initialized", "root": str(root), "database": str(root / DB_NAME)}


def require_project(root: Path) -> None:
    if not (root / DB_NAME).exists():
        raise ValueError(f"dynamic memory project not found at {root}")


def get_meta(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        raise ValueError(f"missing metadata: {key}")
    return json.loads(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def state(conn: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    metadata_keys = (
        "schema_version", "memory_revision", "snapshot_revision", "covered_through", "snapshot"
    )
    for key in metadata_keys:
        result[key] = get_meta(conn, key)
    return result


def decode_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for field in ("queries", "must_include", "evidence_refs", "tags"):
        value[field] = json.loads(value.pop(f"{field}_json"))
    return value


def validate_ut(value: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    required = ("id", "memory_id", "priority", "content", "queries", "must_include", "evidence_refs", "source")
    missing = [key for key in required if key not in value]
    if missing:
        errors.append({"code": "missing_fields", "fields": missing})
    for key in ("id", "memory_id", "content", "source"):
        if not isinstance(value.get(key), str) or not value.get(key, "").strip():
            errors.append({"code": "invalid_string", "field": key})
    priority = value.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
        errors.append({"code": "invalid_priority"})
    for key in ("queries", "must_include", "evidence_refs"):
        items = value.get(key)
        if not isinstance(items, list) or not items or not all(isinstance(x, str) and x.strip() for x in items):
            errors.append({"code": "invalid_string_list", "field": key})
    content = normalize(value.get("content", ""))
    for phrase in value.get("must_include") or []:
        if normalize(phrase) not in content:
            errors.append({"code": "must_include_missing", "phrase": phrase})
    if value.get("status", "active") not in {"active", "retired"}:
        errors.append({"code": "invalid_status"})
    return errors


def upsert_ut(conn: sqlite3.Connection, value: dict[str, Any], *, force_pending: bool = True) -> None:
    errors = validate_ut(value)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    record = {
        **value,
        "status": value.get("status", "active"),
        "tags": value.get("tags", []),
        "build_state": "pending" if force_pending else value.get("build_state", "pending"),
    }
    digest = content_hash(record)
    conn.execute(
        """INSERT INTO uts(id,memory_id,priority,status,build_state,content,queries_json,
        must_include_json,evidence_refs_json,source,tags_json,content_hash,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET memory_id=excluded.memory_id,priority=excluded.priority,
        status=excluded.status,build_state=excluded.build_state,content=excluded.content,
        queries_json=excluded.queries_json,must_include_json=excluded.must_include_json,
        evidence_refs_json=excluded.evidence_refs_json,source=excluded.source,tags_json=excluded.tags_json,
        content_hash=excluded.content_hash,updated_at=excluded.updated_at""",
        (
            record.get("id"),
            record.get("memory_id"),
            record.get("priority"),
            record.get("status"),
            record.get("build_state"),
            record.get("content"),
            json.dumps(record.get("queries"), ensure_ascii=False),
            json.dumps(record.get("must_include"), ensure_ascii=False),
            json.dumps(record.get("evidence_refs"), ensure_ascii=False),
            record.get("source"),
            json.dumps(record.get("tags"), ensure_ascii=False),
            digest,
            now(),
        ),
    )


def score(query: str, document: str) -> int:
    q = normalize(query).strip()
    d = normalize(document)
    if not q:
        return 0
    value = 20 if q in d else 0
    q_tokens = tokens(q)
    d_tokens = set(tokens(d))
    value += sum(3 for token in q_tokens if token in d_tokens)
    return value


def search_one(
    conn: sqlite3.Connection,
    query: str,
    *,
    built_only: bool = False,
    candidate_ids: set[str] | None = None,
) -> dict[str, Any]:
    matches: dict[str, dict[str, Any]] = {}
    candidate_ids = candidate_ids or set()
    rows = conn.execute("SELECT * FROM uts WHERE status='active'").fetchall()
    for row in rows:
        ut = decode_row(row)
        if not built_only and ut["build_state"] == "pending":
            document = "\n".join(
                [ut["content"], *ut["queries"], *ut["must_include"], *ut["tags"], ut["source"]]
            )
        else:
            built = conn.execute("SELECT * FROM built_docs WHERE ut_id=?", (ut["id"],)).fetchone()
            eligible = ut["build_state"] == "built" or ut["id"] in candidate_ids
            if built is None or not eligible or built["content_hash"] != ut["content_hash"]:
                continue
            document = built["document"]
        rank = score(query, document)
        if rank <= 0:
            continue
        matches[ut["id"]] = {
            "id": ut["id"], "memory_id": ut["memory_id"], "priority": ut["priority"],
            "score": rank, "content": ut["content"], "tags": ut["tags"],
            "source": ut["source"], "evidence_refs": ut["evidence_refs"],
            "build_state": "built" if built_only or ut["build_state"] == "built" else "pending",
        }
    ordered = sorted(
        matches.values(),
        key=lambda item: (-item["priority"], -item["score"], item["id"]),
    )
    return {"query": query, "matches": ordered}


def search(root: Path, queries: list[str], *, built_only: bool = False) -> dict[str, Any]:
    with connect(root) as conn:
        groups = [search_one(conn, query, built_only=built_only) for query in queries]
    return groups[0] if len(groups) == 1 else {"queries": groups}


def list_uts(root: Path, *, full: bool = False) -> dict[str, Any]:
    with connect(root) as conn:
        values = [decode_row(row) for row in conn.execute("SELECT * FROM uts ORDER BY priority DESC,id")]
    if not full:
        summary_keys = ("id", "memory_id", "priority", "status", "build_state", "source")
        values = [{key: value[key] for key in summary_keys} for value in values]
    return {"memories": values}


def show(root: Path, ut_id: str) -> dict[str, Any]:
    with connect(root) as conn:
        row = conn.execute("SELECT * FROM uts WHERE id=?", (ut_id,)).fetchone()
    return {"error": "not_found", "id": ut_id} if row is None else {"memory": decode_row(row)}


def publish_pending(root: Path, proposal: dict[str, Any]) -> dict[str, Any]:
    required_keys = (
        "base_memory_revision",
        "base_snapshot_revision",
        "from_cursor",
        "to_cursor",
        "snapshot",
        "changed_uts",
        "evidence_refs",
        "semantic_statement",
    )
    for key in required_keys:
        if key not in proposal:
            raise ValueError(f"proposal missing {key}")
    if not isinstance(proposal["snapshot"], dict) or not proposal["snapshot"]:
        raise ValueError("snapshot must be a non-empty object")
    if not proposal["evidence_refs"] or not str(proposal["semantic_statement"]).strip():
        raise ValueError("proposal requires evidence_refs and semantic_statement")
    with connect(root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = state(conn)
        stale_memory = proposal["base_memory_revision"] != current["memory_revision"]
        stale_snapshot = proposal["base_snapshot_revision"] != current["snapshot_revision"]
        if stale_memory or stale_snapshot:
            raise ValueError("stale proposal revision")
        if int(proposal["from_cursor"]) != int(current["covered_through"]) + 1:
            raise ValueError("proposal range is not continuous")
        if int(proposal["to_cursor"]) < int(proposal["from_cursor"]):
            raise ValueError("proposal range is empty or reversed")
        changed = list(proposal["changed_uts"])
        for change in changed:
            action = change.get("action", "upsert")
            if action == "upsert":
                upsert_ut(conn, change)
            elif action == "retire":
                cursor = conn.execute(
                    "UPDATE uts SET status='retired',updated_at=? WHERE id=?",
                    (now(), change.get("id")),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"cannot retire missing UT {change.get('id')}")
            else:
                raise ValueError(f"unsupported UT action: {action}")
        if changed:
            set_meta(conn, "memory_revision", int(current["memory_revision"]) + 1)
        set_meta(conn, "snapshot_revision", int(current["snapshot_revision"]) + 1)
        set_meta(conn, "covered_through", int(proposal["to_cursor"]))
        set_meta(conn, "snapshot", proposal["snapshot"])
        result = state(conn)
        conn.commit()
    return {"status": "published", "changed_ut_ids": [item.get("id") for item in changed], **result}


def freeze_pending(root: Path, output: Path) -> dict[str, Any]:
    with connect(root) as conn:
        rows = conn.execute(
            "SELECT * FROM uts WHERE status='active' AND build_state='pending' ORDER BY id"
        )
        items = [decode_row(row) for row in rows]
    # This timestamp belongs to the immutable build batch, not to any UT.
    # Calling it ``created_at`` is structurally ambiguous to the Builder: a
    # perfectly valid UT.updated_at necessarily precedes the time at which the
    # batch is frozen.  Keep that ordering explicit in the wire format.
    batch = {"frozen_at": now(), "items": items}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(batch, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return {"status": "frozen", "count": len(items), "output": str(output)}


def build_pending(root: Path, batch: dict[str, Any]) -> dict[str, Any]:
    items = list(batch.get("items") or [])
    with connect(root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        ids: set[str] = set()
        skipped: list[str] = []
        for frozen in items:
            row = conn.execute(
                "SELECT * FROM uts WHERE id=? AND status='active'",
                (frozen.get("id"),),
            ).fetchone()
            if row is None:
                skipped.append(str(frozen.get("id")))
                continue
            current = decode_row(row)
            if (
                current["build_state"] != "pending"
                or current["content_hash"] != frozen.get("content_hash")
            ):
                skipped.append(current["id"])
                continue
            document = "\n".join(
                [
                    current["content"],
                    *current["queries"],
                    *current["must_include"],
                    *current["tags"],
                    current["source"],
                ]
            )
            conn.execute(
                """INSERT INTO built_docs(ut_id,content_hash,document,built_at)
                VALUES(?,?,?,?) ON CONFLICT(ut_id) DO UPDATE SET
                content_hash=excluded.content_hash,document=excluded.document,
                built_at=excluded.built_at""",
                (current["id"], current["content_hash"], document, now()),
            )
            ids.add(current["id"])
        failures: list[dict[str, Any]] = []
        for ut_id in ids:
            row = conn.execute("SELECT * FROM uts WHERE id=?", (ut_id,)).fetchone()
            ut = decode_row(row)
            for query in ut["queries"]:
                result = search_one(conn, query, built_only=True, candidate_ids=ids)
                match = next((item for item in result["matches"] if item["id"] == ut_id), None)
                missing_phrase = match is not None and any(
                    normalize(phrase) not in normalize(match["content"])
                    for phrase in ut["must_include"]
                )
                if match is None or missing_phrase:
                    failures.append({"id": ut_id, "query": query})
        if failures:
            conn.rollback()
            return {"status": "failed", "failures": failures, "built": [], "skipped": skipped}
        for ut_id in ids:
            conn.execute(
                "UPDATE uts SET build_state='built',updated_at=? WHERE id=?",
                (now(), ut_id),
            )
        conn.commit()
    return {"status": "built", "built": sorted(ids), "skipped": skipped, "failures": []}


def run_tests(root: Path, *, built_only: bool = False) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    total = 0
    with connect(root) as conn:
        rows = conn.execute("SELECT * FROM uts WHERE status='active'").fetchall()
        for row in rows:
            ut = decode_row(row)
            if built_only and ut["build_state"] != "built":
                continue
            for query in ut["queries"]:
                total += 1
                result = search_one(conn, query, built_only=built_only)
                match = next((item for item in result["matches"] if item["id"] == ut["id"]), None)
                missing_phrase = match is not None and any(
                    normalize(phrase) not in normalize(match["content"])
                    for phrase in ut["must_include"]
                )
                if match is None or missing_phrase:
                    failures.append({"id": ut["id"], "query": query})
    return {"total": total, "failed": len(failures), "failures": failures, "built_only": built_only}


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def manual_add(root: Path, value: dict[str, Any], *, force: bool) -> dict[str, Any]:
    with connect(root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT 1 FROM uts WHERE id=?", (value.get("id"),)).fetchone()
        if existing and not force:
            return {"status": "exists", "id": value.get("id")}
        upsert_ut(conn, value)
        set_meta(conn, "memory_revision", int(get_meta(conn, "memory_revision")) + 1)
        conn.commit()
    return {"status": "added", "id": value["id"], "build_state": "pending"}


def update(root: Path, ut_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    shown = show(root, ut_id)
    if "memory" not in shown:
        return shown
    current = shown["memory"]
    for key in ("updated_at", "content_hash", "build_state"):
        current.pop(key, None)
    current.update(updates)
    current["id"] = ut_id
    return manual_add(root, current, force=True) | {"status": "updated"}


def retire(root: Path, ut_id: str, reason: str | None) -> dict[str, Any]:
    with connect(root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE uts SET status='retired',updated_at=? WHERE id=?",
            (now(), ut_id),
        )
        if cursor.rowcount != 1:
            return {"status": "not_found", "id": ut_id}
        set_meta(conn, "memory_revision", int(get_meta(conn, "memory_revision")) + 1)
        conn.commit()
    return {"status": "retired", "id": ut_id, "reason": reason}


def bench(root: Path) -> dict[str, Any]:
    with connect(root) as conn:
        queries: list[str] = []
        rows = conn.execute("SELECT queries_json FROM uts WHERE status='active'")
        for row in rows:
            queries.extend(json.loads(row["queries_json"]))
    samples: list[float] = []
    for query in queries:
        started = time.perf_counter()
        search(root, [query])
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))] if samples else 0.0
    return {
        "queries": len(samples),
        "p95_search_ms": round(p95, 3),
        "mean_search_ms": round(statistics.mean(samples), 3) if samples else 0.0,
    }


def emit_json(value: dict[str, Any]) -> None:
    """Write one JSON document through logging without decorating the CLI protocol."""
    logger = logging.getLogger("dynamic-memory-cli.output")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info(json.dumps(value, ensure_ascii=False, indent=2))
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dynamic-memory-cli")
    parser.add_argument("--root")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--path", required=True)
    search_p = sub.add_parser("search")
    search_p.add_argument("query", nargs="+")
    list_p = sub.add_parser("list")
    list_p.add_argument("--full", action="store_true")
    show_p = sub.add_parser("show")
    show_p.add_argument("id")
    check = sub.add_parser("check-conflicts")
    check.add_argument("--file", required=True)
    add = sub.add_parser("add")
    add.add_argument("--file", required=True)
    add.add_argument("--force", action="store_true")
    update_p = sub.add_parser("update")
    update_p.add_argument("id")
    update_p.add_argument("--file", required=True)
    retire_p = sub.add_parser("retire")
    retire_p.add_argument("id")
    retire_p.add_argument("--reason")
    sub.add_parser("get-state")
    publish = sub.add_parser("publish-pending")
    publish.add_argument("--file", required=True)
    freeze = sub.add_parser("freeze-pending")
    freeze.add_argument("--output", required=True)
    build = sub.add_parser("build-pending")
    build.add_argument("--file", required=True)
    test_p = sub.add_parser("test")
    test_p.add_argument("--built-only", action="store_true")
    sub.add_parser("bench")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            result = init_project(root_path(args.path))
        else:
            root = root_path(args.root)
            require_project(root)
            if args.command == "search":
                result = search(root, args.query)
            elif args.command == "list":
                result = list_uts(root, full=args.full)
            elif args.command == "show":
                result = show(root, args.id)
            elif args.command == "check-conflicts":
                candidate = read_json(args.file)
                errors = validate_ut(candidate)
                matches = [] if errors else [search(root, [query]) for query in candidate["queries"]]
                result = {"valid": not errors, "errors": errors, "matches": matches}
            elif args.command == "add":
                result = manual_add(root, read_json(args.file), force=args.force)
            elif args.command == "update":
                result = update(root, args.id, read_json(args.file))
            elif args.command == "retire":
                result = retire(root, args.id, args.reason)
            elif args.command == "get-state":
                with connect(root) as conn:
                    result = state(conn)
            elif args.command == "publish-pending":
                result = publish_pending(root, read_json(args.file))
            elif args.command == "freeze-pending":
                result = freeze_pending(root, Path(args.output).resolve())
            elif args.command == "build-pending":
                result = build_pending(root, read_json(args.file))
            elif args.command == "test":
                result = run_tests(root, built_only=args.built_only)
            elif args.command == "bench":
                result = bench(root)
            else:
                raise ValueError(f"unsupported command: {args.command}")
        emit_json(result)
        return 1 if result.get("status") in {"failed", "exists", "not_found"} or result.get("failed", 0) else 0
    except Exception as exc:
        emit_json({"error": type(exc).__name__, "message": str(exc)})
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
