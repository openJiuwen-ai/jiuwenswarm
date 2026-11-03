#!/usr/bin/env python3
"""Promote and verify project-maintainer symbol audit integrity metadata."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from typing import Any

from symbol_fingerprints import normalize_hash, symbol_fingerprint


ALLOWED_AUDIT_STATUSES = (
    "unaudited",
    "script_assessed",
    "agent_audited",
    "human_audited",
    "audit_expired",
    "out_of_scope",
)
AGENT_TRUST_RESULTS = (
    "provisional_agent_audit",
    "trusted_agent_audit",
    "suspicious_agent_audit",
    "invalid_agent_audit",
)
DEFAULT_SCOPE = "default_health_audit"
DEFAULT_SIGNING_KEY_ENV = "PROJECT_MAINTAINER_AUDIT_SIGNING_KEY"
DEFAULT_KEY_ID = "project-maintainer-local-v1"
GENERATED_BY = "audit_integrity.py"
CANONICALIZATION = "sorted-json-excluding-signature-payload-hash-and-observation-fields"
INTEGRITY_SCHEMA = "project-maintainer.audit-integrity.v1"
SIGNING_KEY_SCHEMA = "project-maintainer.audit-signing-key.v1"
ARTIFACT_SIGNING_KEY_PATH = Path(".doc_project_maintainer") / "project" / "audit-signing-key.json"


class AuditIntegrityError(Exception):
    """Expected user-facing command error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise AuditIntegrityError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AuditIntegrityError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AuditIntegrityError(f"Expected JSON object in {path}")
    return data


def write_json(path: Path, data: dict[str, Any], pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, indent=2 if pretty else None, sort_keys=True, ensure_ascii=False)
    path.write_text(encoded + "\n", encoding="utf-8")


def run_git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def git_head(root: Path) -> str | None:
    output = run_git(root, "rev-parse", "HEAD")
    return output.strip() if output else None


def git_status_short(root: Path) -> list[str]:
    output = run_git(root, "status", "--short", "--untracked-files=all")
    if output is None:
        return []
    return [line for line in output.splitlines() if line.strip()]


def artifact_signing_key_path(root: Path) -> Path:
    return root / ARTIFACT_SIGNING_KEY_PATH


def generated_signing_key(env_name: str, key_id: str) -> dict[str, Any]:
    return {
        "schema": SIGNING_KEY_SCHEMA,
        "algorithm": "hmac-sha256",
        "created_at": utc_now(),
        "environment_variable": env_name,
        "generated_by": GENERATED_BY,
        "key_id": key_id,
        "purpose": "artifact-local agent workflow integrity",
        "secret": secrets.token_urlsafe(32),
    }


def artifact_signing_secret(root: Path, env_name: str, key_id: str) -> bytes:
    path = artifact_signing_key_path(root)
    if not path.exists():
        write_json(path, generated_signing_key(env_name, key_id))
    record = read_json(path)
    if record.get("schema") != SIGNING_KEY_SCHEMA:
        raise AuditIntegrityError(f"Artifact signing key has unsupported schema: {path}")
    if record.get("algorithm") != "hmac-sha256":
        raise AuditIntegrityError(f"Artifact signing key has unsupported algorithm: {path}")
    secret = record.get("secret")
    if not isinstance(secret, str) or not secret:
        raise AuditIntegrityError(f"Artifact signing key is missing a non-empty secret: {path}")
    return secret.encode("utf-8")


def signing_secret(env_name: str, root: Path, key_id: str) -> bytes:
    secret = os.environ.get(env_name)
    if secret:
        return secret.encode("utf-8")
    return artifact_signing_secret(root, env_name, key_id)


def ensure_key(args: argparse.Namespace) -> int:
    root = args.repo_root.resolve()
    path = artifact_signing_key_path(root)
    created = not path.exists()
    artifact_signing_secret(root, args.signing_key_env, args.key_id)
    record = read_json(path)
    output = {
        "created": created,
        "environment_variable": record.get("environment_variable"),
        "key_id": record.get("key_id"),
        "key_path": str(path),
        "purpose": record.get("purpose"),
        "schema": record.get("schema"),
    }
    print(canonical_json(output))
    return 0


def hmac_signature(secret: bytes, payload_hash: str) -> str:
    return "hmac-sha256:" + hmac.new(secret, payload_hash.encode("utf-8"), hashlib.sha256).hexdigest()


def script_hash() -> str:
    return file_hash(Path(__file__).resolve())


def resolve_repo_path(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def relative_to_repo(root: Path, path: Path) -> str:
    resolved = resolve_repo_path(root, path).resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def entry_doc_relative(root: Path, entry_doc: Path) -> str:
    resolved = resolve_repo_path(root, entry_doc).resolve()
    docs_root = (root / ".doc_project_maintainer").resolve()
    try:
        return resolved.relative_to(docs_root).as_posix()
    except ValueError:
        return relative_to_repo(root, entry_doc)


def find_symbol(audit_map: dict[str, Any], symbol_id: str) -> dict[str, Any]:
    symbols = audit_map.get("symbols")
    if not isinstance(symbols, list):
        raise AuditIntegrityError("Audit map does not contain a symbols list")
    for item in symbols:
        if isinstance(item, dict) and item.get("id") == symbol_id:
            return item
    raise AuditIntegrityError(f"Symbol id not found in audit map: {symbol_id}")


def heading_has_content(text: str, heading: str) -> bool:
    lines = text.splitlines()
    target = heading.strip().lower()
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            marker = stripped.lstrip("#").strip().lower()
            if in_section:
                return False
            in_section = marker == target
            continue
        if in_section and stripped and not stripped.startswith("---"):
            return True
    return False


def has_health(text: str) -> bool:
    return "health:" in text and "overall:" in text


def validate_entry_doc(entry_doc: Path) -> None:
    if not entry_doc.exists():
        raise AuditIntegrityError(f"Entry doc not found: {entry_doc}")
    text = entry_doc.read_text(encoding="utf-8")
    if not heading_has_content(text, "Actual Role"):
        raise AuditIntegrityError(f"Entry doc is missing Actual Role content: {entry_doc}")
    if not has_health(text):
        raise AuditIntegrityError(f"Entry doc is missing health fields: {entry_doc}")


def normalize_signature_batch(path: Path) -> dict[str, Any]:
    raw = read_json(path)
    calls = raw.get("calls")
    if not isinstance(calls, list) or not calls:
        raise AuditIntegrityError("--agent-call-signature-json must contain a non-empty calls list")

    normalized_calls: list[dict[str, str]] = []
    comparable_calls: list[dict[str, str]] = []
    for call in calls:
        if not isinstance(call, dict):
            raise AuditIntegrityError("Each agent call signature entry must be an object")
        tool_name = str(call.get("tool_name") or "")
        if not tool_name:
            raise AuditIntegrityError("Each agent call signature entry needs tool_name")
        raw_input = call.get("raw_input")
        input_hash = sha256_text(canonical_json(raw_input))
        normalized = {"tool_name": tool_name, "input_hash": input_hash}
        normalized_calls.append(normalized)
        comparable_calls.append(normalized)

    batch_hash = sha256_text(canonical_json({"calls": sorted(comparable_calls, key=canonical_json)}))
    return {
        "status": "provided",
        "submitted_at": utc_now(),
        "batch_hash": batch_hash,
        "call_count": len(normalized_calls),
        "calls": normalized_calls,
    }


def strip_integrity_self_fields(value: Any) -> Any:
    cloned = deepcopy(value)
    if isinstance(cloned, dict):
        integrity = cloned.get("integrity")
        if isinstance(integrity, dict):
            integrity.pop("signature", None)
            integrity.pop("payload_hash", None)
            observation = integrity.get("observation")
            if isinstance(observation, dict):
                observation.pop("audit_map_hash_after", None)
                if not observation:
                    integrity.pop("observation", None)
        return {key: strip_integrity_self_fields(item) for key, item in cloned.items()}
    if isinstance(cloned, list):
        return [strip_integrity_self_fields(item) for item in cloned]
    return cloned


def payload_hash_for_record(record: dict[str, Any]) -> str:
    return sha256_text(canonical_json(strip_integrity_self_fields(record)))


def audit_map_observation_hash(audit_map: dict[str, Any]) -> str:
    return sha256_text(canonical_json(strip_integrity_self_fields(audit_map)))


def summarize_symbols(symbols: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts = {status: 0 for status in ALLOWED_AUDIT_STATUSES}
    summary = {
        "symbols": len(symbols),
        "classes": 0,
        "top_level_functions": 0,
        "class_methods": 0,
        **status_counts,
        "open_issues": 0,
    }
    for item in symbols:
        kind = item.get("kind")
        if kind == "class":
            summary["classes"] += 1
        elif kind == "function":
            summary["top_level_functions"] += 1
        elif kind == "method":
            summary["class_methods"] += 1
        status = str((item.get("audit") or {}).get("status") or "unaudited")
        if status not in ALLOWED_AUDIT_STATUSES:
            status = "unaudited"
        summary[status] += 1
        summary["open_issues"] += sum(1 for issue in item.get("issues", []) if issue.get("status") != "closed")
    return summary


def refresh_audit_map_summaries(audit_map: dict[str, Any]) -> None:
    symbols = [item for item in audit_map.get("symbols", []) if isinstance(item, dict)]
    health_symbols = [item for item in symbols if item.get("audit_scope") == DEFAULT_SCOPE]

    audit_map["audit_statuses"] = list(ALLOWED_AUDIT_STATUSES)
    audit_map["summary"] = summarize_symbols(symbols)
    audit_map["health_audit_summary"] = summarize_symbols(health_symbols)
    audit_map["health_audit_symbols"] = health_symbols
    audit_map.pop("machine_assessment_statuses", None)
    audit_map.pop("machine_assessment_summary", None)
    audit_map.pop("health_audit_machine_assessment_summary", None)


def promote(args: argparse.Namespace) -> int:
    root = args.repo_root.resolve()
    audit_map_path = resolve_repo_path(root, args.audit_map)
    entry_doc = resolve_repo_path(root, args.entry_doc)
    source_file = resolve_repo_path(root, args.source_file)
    validate_entry_doc(entry_doc)
    if not source_file.exists():
        raise AuditIntegrityError(f"Source file not found: {source_file}")

    secret = signing_secret(args.signing_key_env, root, args.key_id)
    audit_map_hash_before = file_hash(audit_map_path)
    audit_map = read_json(audit_map_path)
    record = find_symbol(audit_map, args.symbol_id)

    signed_at = utc_now()
    agent_batch = normalize_signature_batch(resolve_repo_path(root, args.agent_call_signature_json)) if args.agent_call_signature_json else None
    source_hash = file_hash(source_file)
    current_symbol_hash, symbol_hash_mode = symbol_fingerprint(source_file, record)
    entry_hash = file_hash(entry_doc)
    current_script_hash = script_hash()
    current_head = git_head(root)
    status_short = git_status_short(root)

    audit = record.get("audit") if isinstance(record.get("audit"), dict) else {}
    audit = dict(audit)
    audit["status"] = "agent_audited" if agent_batch else "script_assessed"
    audit["audited_at"] = signed_at
    audit["audited_commit"] = current_head
    audit["audited_source_hash"] = source_hash
    audit["audited_symbol_hash"] = current_symbol_hash
    audit["confidence"] = audit.get("confidence") or "unknown"
    audit["expired_reason"] = None
    if agent_batch:
        audit["auditor"] = audit.get("auditor") or "codex"
        audit.pop("downgrade_reason", None)
        record["agent_call_signature_batch"] = agent_batch
        result_codes = ["valid_controlled_entrypoint", "provisional_agent_audit"]
    else:
        audit["auditor"] = audit.get("auditor") or GENERATED_BY
        audit["downgrade_reason"] = "missing_agent_call_signature"
        record.pop("agent_call_signature_batch", None)
        result_codes = ["missing_agent_call_signature", "script_assessed_only"]

    record["audit"] = audit
    record["integrity"] = {
        "schema": INTEGRITY_SCHEMA,
        "generated_by": GENERATED_BY,
        "script_hash": current_script_hash,
        "canonicalization": CANONICALIZATION,
        "source_hash": source_hash,
        "symbol_hash": current_symbol_hash,
        "symbol_hash_mode": symbol_hash_mode,
        "entry_doc_hash": entry_hash,
        "entry_doc": entry_doc_relative(root, entry_doc),
        "audit_map_hash_before": audit_map_hash_before,
        "git_head": current_head,
        "git_status_short": status_short,
        "signed_at": signed_at,
        "signature_algorithm": "hmac-sha256",
        "key_id": args.key_id,
        "signer": "project-maintainer-local-hmac",
    }
    payload_hash = payload_hash_for_record(record)
    record["integrity"]["payload_hash"] = payload_hash
    record["integrity"]["signature"] = hmac_signature(secret, payload_hash)

    refresh_audit_map_summaries(audit_map)
    record["integrity"]["observation"] = {"audit_map_hash_after": audit_map_observation_hash(audit_map)}
    write_json(audit_map_path, audit_map)

    output = {
        "symbol_id": args.symbol_id,
        "audit_status": audit["status"],
        "result_codes": result_codes,
    }
    if not agent_batch:
        output["warning"] = (
            "No agent call signature batch provided. Recorded as audit.status=script_assessed only. "
            "To promote to agent_audited, rerun with --agent-call-signature-json containing the recent tool-call batch."
        )
    print(canonical_json(output))
    return 1 if args.strict and not agent_batch else 0


def batch_hash_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        batch = record.get("agent_call_signature_batch")
        if isinstance(batch, dict) and batch.get("batch_hash"):
            batch_hash = str(batch["batch_hash"])
            counts[batch_hash] = counts.get(batch_hash, 0) + 1
    return counts


def entry_doc_for_record(root: Path, record: dict[str, Any]) -> Path | None:
    integrity = record.get("integrity")
    if isinstance(integrity, dict) and integrity.get("entry_doc"):
        return root / ".doc_project_maintainer" / str(integrity["entry_doc"])
    docs = record.get("docs")
    if isinstance(docs, dict) and docs.get("entry_doc"):
        return root / ".doc_project_maintainer" / str(docs["entry_doc"])
    return None


def verify_agent_record(
    *,
    root: Path,
    record: dict[str, Any],
    secret: bytes,
    batch_counts: dict[str, int],
    batch_reuse_threshold: int,
) -> tuple[str, list[str], bool]:
    codes: list[str] = []
    integrity = record.get("integrity")
    if not isinstance(integrity, dict) or not integrity.get("signature"):
        return "invalid_agent_audit", ["unsigned_agent_audit", "invalid_agent_audit"], False
    batch = record.get("agent_call_signature_batch")
    if not isinstance(batch, dict) or not batch.get("batch_hash"):
        return "invalid_agent_audit", ["missing_agent_call_signature", "invalid_agent_audit"], False

    invalid = False
    stale = False
    suspicious = False
    if integrity.get("generated_by") != GENERATED_BY:
        codes.append("integrity_mismatch")
        invalid = True
    if integrity.get("script_hash") != script_hash():
        codes.append("untrusted_script_hash")
        invalid = True
    source_path = root / str(record.get("source") or "")
    if source_path.exists():
        expected_symbol_hash = normalize_hash(integrity.get("symbol_hash"))
        if expected_symbol_hash:
            current_symbol_hash, _ = symbol_fingerprint(source_path, record)
            if normalize_hash(current_symbol_hash) != expected_symbol_hash:
                codes.append("symbol_hash_changed")
                stale = True
        elif normalize_hash(file_hash(source_path)) != normalize_hash(integrity.get("source_hash")):
            codes.append("source_hash_changed")
            stale = True
    entry_doc = entry_doc_for_record(root, record)
    if entry_doc and entry_doc.exists() and file_hash(entry_doc) != integrity.get("entry_doc_hash"):
        codes.append("entry_doc_hash_changed")
        stale = True
    if payload_hash_for_record(record) != integrity.get("payload_hash"):
        codes.append("integrity_mismatch")
        invalid = True
    expected_signature = hmac_signature(secret, str(integrity.get("payload_hash") or ""))
    if not hmac.compare_digest(expected_signature, str(integrity.get("signature") or "")):
        codes.append("integrity_mismatch")
        invalid = True
    batch_hash = str(batch.get("batch_hash"))
    if batch_counts.get(batch_hash, 0) > batch_reuse_threshold:
        codes.append("suspicious_batch_signature_reuse")
        suspicious = True

    if invalid or stale:
        codes.append("invalid_agent_audit")
        return "invalid_agent_audit", unique(codes), False
    if suspicious:
        codes.append("suspicious_agent_audit")
        return "suspicious_agent_audit", unique(codes), False
    codes.extend(["valid_controlled_entrypoint", "trusted_agent_audit"])
    return "trusted_agent_audit", unique(codes), True


def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def increment(mapping: dict[str, int], key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


def verify_or_report(args: argparse.Namespace, *, verify_mode: bool) -> int:
    root = args.repo_root.resolve()
    audit_map = read_json(resolve_repo_path(root, args.audit_map))
    secret = signing_secret(args.signing_key_env, root, args.key_id)
    all_records = [item for item in audit_map.get("symbols", []) if isinstance(item, dict)]
    scoped_records = [item for item in all_records if (args.scope == "all" or item.get("audit_scope") == args.scope)]
    counts = batch_hash_counts(scoped_records)
    status_counts = {status: 0 for status in ALLOWED_AUDIT_STATUSES}
    trust_counts = {status: 0 for status in AGENT_TRUST_RESULTS}
    result_counts: dict[str, int] = {}
    record_reports: list[dict[str, Any]] = []
    eligible = 0
    pending = 0
    invalid_seen = False
    stale_seen = False
    suspicious_seen = False

    for record in scoped_records:
        audit = record.get("audit") if isinstance(record.get("audit"), dict) else {}
        status = str(audit.get("status") or "unaudited")
        codes: list[str] = []
        trust_result: str | None = None
        closure_eligible = False
        if status not in ALLOWED_AUDIT_STATUSES:
            status = "invalid"
            codes.append("invalid_audit_status")
            invalid_seen = True
        elif status == "agent_audited":
            trust_result, codes, closure_eligible = verify_agent_record(
                root=root,
                record=record,
                secret=secret,
                batch_counts=counts,
                batch_reuse_threshold=args.batch_reuse_threshold,
            )
            trust_counts[trust_result] += 1
            invalid_seen = invalid_seen or "integrity_mismatch" in codes or "unsigned_agent_audit" in codes
            stale_seen = stale_seen or any(
                code in codes for code in ("symbol_hash_changed", "source_hash_changed", "entry_doc_hash_changed")
            )
            suspicious_seen = suspicious_seen or "suspicious_batch_signature_reuse" in codes
        elif status == "script_assessed":
            codes.append("script_assessed_only")
        elif status == "human_audited":
            closure_eligible = True
        elif status == "out_of_scope":
            closure_eligible = True
        elif status == "audit_expired":
            codes.append("audit_expired")
        else:
            codes.append("pending_audit")

        if status in status_counts:
            status_counts[status] += 1
        else:
            increment(result_counts, "invalid_audit_status")
        for code in unique(codes):
            increment(result_counts, code)
        if closure_eligible:
            eligible += 1
        else:
            pending += 1
        record_reports.append(
            {
                "id": record.get("id"),
                "audit_status": status,
                "trust_result": trust_result,
                "closure_eligible": closure_eligible,
                "result_codes": unique(codes),
            }
        )

    require_closure_passed = pending == 0
    report = {
        "records": len(scoped_records),
        "status_counts": status_counts,
        "agent_audit_trust_counts": trust_counts,
        "result_counts": result_counts,
        "closure": {
            "scope": args.scope,
            "eligible": eligible,
            "pending": pending,
            "require_closure_passed": require_closure_passed,
        },
        "batch_reuse": {
            "threshold": args.batch_reuse_threshold,
            "unique_agent_call_signature_batch_hashes": len(counts),
            "reused_batch_hashes": {
                batch_hash: [
                    str(record.get("id"))
                    for record in scoped_records
                    if isinstance(record.get("agent_call_signature_batch"), dict)
                    and record["agent_call_signature_batch"].get("batch_hash") == batch_hash
                ]
                for batch_hash, count in counts.items()
                if count > args.batch_reuse_threshold
            },
        },
        "records_detail": record_reports,
        "recommended_action": recommended_action(result_counts, pending),
    }
    if args.report_output:
        write_json(resolve_repo_path(root, args.report_output), report)
    print(json.dumps(report, indent=2, sort_keys=True))

    if not verify_mode:
        return 0
    if invalid_seen or result_counts.get("invalid_audit_status") or result_counts.get("untrusted_script_hash"):
        return 1
    if stale_seen:
        return 2
    if suspicious_seen:
        return 3
    if args.require_closure and not require_closure_passed:
        return 4
    return 0


def recommended_action(result_counts: dict[str, int], pending: int) -> str:
    if result_counts.get("integrity_mismatch") or result_counts.get("unsigned_agent_audit"):
        return "Rerun affected agent audits through promote with the correct signing key and controlled entrypoint."
    if (
        result_counts.get("symbol_hash_changed")
        or result_counts.get("source_hash_changed")
        or result_counts.get("entry_doc_hash_changed")
    ):
        return "Expire stale audits through the controlled entrypoint and rerun review for changed records."
    if result_counts.get("suspicious_batch_signature_reuse"):
        return "Review reused batch signatures and rerun per-action promotions before treating agent audits as trusted."
    if pending:
        return "Promote pending records through the controlled entrypoint or mark them out of scope after review."
    return "No action required for the requested scope."


def add_repo_key_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--signing-key-env", default=DEFAULT_SIGNING_KEY_ENV)
    parser.add_argument("--key-id", default=DEFAULT_KEY_ID)
    parser.add_argument("--strict", action="store_true")


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    add_repo_key_arguments(parser)
    parser.add_argument("--audit-map", type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure_key_parser = subparsers.add_parser("ensure-key", help="Ensure the artifact-local audit signing key exists")
    add_repo_key_arguments(ensure_key_parser)

    promote_parser = subparsers.add_parser("promote", help="Promote a symbol audit record through the controlled entrypoint")
    add_common_arguments(promote_parser)
    promote_parser.add_argument("--entry-doc", type=Path, required=True)
    promote_parser.add_argument("--symbol-id", required=True)
    promote_parser.add_argument("--source-file", type=Path, required=True)
    promote_parser.add_argument("--scope", default=DEFAULT_SCOPE)
    promote_parser.add_argument("--agent-call-signature-json", type=Path)

    verify_parser = subparsers.add_parser("verify", help="Verify audit integrity and return non-zero for invalid states")
    add_common_arguments(verify_parser)
    verify_parser.add_argument("--scope", default=DEFAULT_SCOPE)
    verify_parser.add_argument("--report-output", type=Path)
    verify_parser.add_argument("--require-closure", action="store_true")
    verify_parser.add_argument("--batch-reuse-threshold", type=int, default=1)

    report_parser = subparsers.add_parser("report", help="Print audit integrity status without failing")
    add_common_arguments(report_parser)
    report_parser.add_argument("--scope", default=DEFAULT_SCOPE)
    report_parser.add_argument("--report-output", type=Path)
    report_parser.add_argument("--require-closure", action="store_true")
    report_parser.add_argument("--batch-reuse-threshold", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "ensure-key":
            return ensure_key(args)
        if args.command == "promote":
            return promote(args)
        if args.command == "verify":
            return verify_or_report(args, verify_mode=True)
        if args.command == "report":
            return verify_or_report(args, verify_mode=False)
    except AuditIntegrityError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
