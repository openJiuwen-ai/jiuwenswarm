#!/usr/bin/env python3
"""Render a self-contained Project Maintainer audit visualization report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


DEFAULT_ARTIFACT_ROOT = ".doc_project_maintainer"
DEFAULT_SCOPE = "default_health_audit"
ALL_SCOPE = "all"
SEVERITY_SCORE = {"critical": 900, "high": 800, "medium": 500, "low": 200}
HEALTH_RISK_VALUES = {"risky", "weak", "high", "poor", "unsafe", "missing"}


class AuditReportError(Exception):
    """Expected user-facing report generation error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise AuditReportError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AuditReportError(f"Invalid JSON in {label}: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AuditReportError(f"Expected JSON object in {label}: {path}")
    return data


def resolve_paths(args: argparse.Namespace) -> dict[str, Path]:
    repo_root = args.repo_root.resolve()
    artifact_root = (args.artifact_root or (repo_root / DEFAULT_ARTIFACT_ROOT)).resolve()
    project_root = artifact_root / "project"
    return {
        "repo_root": repo_root,
        "artifact_root": artifact_root,
        "coverage_map": (args.coverage_map or (project_root / "coverage-map.json")).resolve(),
        "audit_map": (args.audit_map or (project_root / "symbol-audit-map.json")).resolve(),
        "integrity_report": (args.integrity_report_output or (project_root / "audit-integrity-report.json")).resolve(),
        "output": (args.output or (project_root / "audit-report.html")).resolve(),
    }


def symbol_scope(symbol: dict[str, Any]) -> str:
    return str(symbol.get("audit_scope") or "repository_coverage_only")


def audit_status(symbol: dict[str, Any]) -> str:
    audit = symbol.get("audit") if isinstance(symbol.get("audit"), dict) else {}
    return str(audit.get("status") or "unaudited")


def open_issues(symbol: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        issue
        for issue in symbol.get("issues", [])
        if isinstance(issue, dict) and str(issue.get("status") or "open") != "closed"
    ]


def issue_severity(issue: dict[str, Any]) -> str:
    return str(issue.get("severity") or "unknown").lower()


def health_overall(item: dict[str, Any]) -> str:
    health = item.get("health") if isinstance(item.get("health"), dict) else {}
    return str(health.get("overall") or "unknown").lower()


def priority_score(item: dict[str, Any]) -> int:
    codes = {str(code) for code in item.get("resultCodes", [])}
    trust = str(item.get("trustResult") or "")
    status = str(item.get("auditStatus") or "")
    score = 0
    if "integrity_mismatch" in codes or "unsigned_agent_audit" in codes or trust == "invalid_agent_audit":
        score = max(score, 1000)
    if "suspicious_batch_signature_reuse" in codes or trust == "suspicious_agent_audit":
        score = max(score, 900)
    for issue in item.get("issues", []):
        if isinstance(issue, dict):
            score = max(score, SEVERITY_SCORE.get(issue_severity(issue), 0))
    if status == "audit_expired":
        score = max(score, 700)
    if status == "unaudited":
        score = max(score, 650)
    if status == "script_assessed" or trust == "provisional_agent_audit":
        score = max(score, 600)
    if health_overall(item) in HEALTH_RISK_VALUES:
        score = max(score, 450)
    return score


def risk_level(score: int) -> str:
    if score >= 700:
        return "high"
    if score >= 450:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def danger_reason(item: dict[str, Any]) -> str:
    if item.get("resultCodes"):
        return "Trust verification reported: " + ", ".join(str(code) for code in item["resultCodes"])
    if item.get("issues"):
        first = item["issues"][0]
        return str(first.get("evidence") or first.get("summary") or "Open issue recorded")
    if item.get("auditStatus") == "audit_expired":
        return "Audit source hash changed after review"
    if item.get("auditStatus") == "unaudited":
        return "Symbol has not been audited"
    if item.get("auditStatus") == "script_assessed":
        return "Only script processing exists; no trusted agent or human audit"
    return "No open risk reason recorded"


def suggested_action(item: dict[str, Any]) -> str:
    if item.get("issues"):
        first = item["issues"][0]
        action = first.get("suggested_action")
        if action:
            return str(action)
    if item.get("trustResult") in {"invalid_agent_audit", "suspicious_agent_audit"}:
        return "Rerun or review the affected audit through the controlled integrity workflow"
    if item.get("auditStatus") == "audit_expired":
        return "Refresh symbol docs and rerun the audit for the changed source"
    if item.get("auditStatus") == "unaudited":
        return "Assign a symbol audit or mark the symbol out of scope with a reason"
    if item.get("auditStatus") == "script_assessed":
        return "Perform a real agent or human audit before treating this as closure"
    return "No immediate action recorded"


def normalize_symbol(symbol: dict[str, Any], integrity_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    symbol_id = str(symbol.get("id") or "")
    location = symbol.get("location") if isinstance(symbol.get("location"), dict) else {}
    docs = symbol.get("docs") if isinstance(symbol.get("docs"), dict) else {}
    trust = integrity_by_id.get(symbol_id, {})
    item = {
        "id": symbol_id,
        "name": str(symbol.get("name") or symbol.get("symbol") or symbol_id),
        "symbol": str(symbol.get("symbol") or symbol.get("name") or symbol_id),
        "kind": str(symbol.get("kind") or "unknown"),
        "className": symbol.get("class"),
        "source": str(symbol.get("source") or "unknown"),
        "line": location.get("line"),
        "endLine": location.get("end_line"),
        "sourceRole": str(symbol.get("source_role") or "unknown"),
        "auditScope": symbol_scope(symbol),
        "auditStatus": audit_status(symbol),
        "health": symbol.get("health") if isinstance(symbol.get("health"), dict) else {"overall": "unknown"},
        "issues": open_issues(symbol),
        "entryDoc": docs.get("entry_doc"),
        "trustResult": trust.get("trust_result") or "unverified",
        "closureEligible": bool(trust.get("closure_eligible")) if trust else False,
        "resultCodes": trust.get("result_codes") if isinstance(trust.get("result_codes"), list) else [],
    }
    score = priority_score(item)
    item["priorityScore"] = score
    item["riskLevel"] = risk_level(score)
    item["dangerReason"] = danger_reason(item)
    item["suggestedAction"] = suggested_action(item)
    return item


def build_integrity_lookup(integrity_report: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not integrity_report:
        return {}
    records = integrity_report.get("records_detail")
    if not isinstance(records, list):
        return {}
    return {str(item.get("id")): item for item in records if isinstance(item, dict) and item.get("id")}


def run_integrity_report(args: argparse.Namespace, paths: dict[str, Path]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if args.skip_integrity_refresh:
        return None, {
            "fresh": False,
            "status": "skipped",
            "generatedAt": None,
            "message": "Integrity refresh skipped",
            "command": None,
            "reportPath": str(paths["integrity_report"]),
        }

    script = Path(__file__).resolve().with_name("audit_integrity.py")
    command = [
        sys.executable,
        str(script),
        "report",
        "--repo-root",
        str(paths["repo_root"]),
        "--audit-map",
        str(paths["audit_map"]),
        "--scope",
        ALL_SCOPE,
        "--report-output",
        str(paths["integrity_report"]),
        "--signing-key-env",
        args.signing_key_env,
        "--batch-reuse-threshold",
        str(args.batch_reuse_threshold),
    ]
    result = subprocess.run(
        command,
        cwd=paths["repo_root"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        return None, {
            "fresh": False,
            "status": "failed",
            "generatedAt": utc_now(),
            "message": "Integrity refresh failed",
            "command": command,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "reportPath": str(paths["integrity_report"]),
        }
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, {
            "fresh": False,
            "status": "failed",
            "generatedAt": utc_now(),
            "message": f"Integrity refresh failed: invalid JSON output: {exc}",
            "command": command,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "reportPath": str(paths["integrity_report"]),
        }
    return report, {
        "fresh": True,
        "status": "fresh",
        "generatedAt": utc_now(),
        "message": "Integrity refresh succeeded",
        "command": command,
        "reportPath": str(paths["integrity_report"]),
        "summary": {
            "records": report.get("records"),
            "closure": report.get("closure"),
            "agentAuditTrustCounts": report.get("agent_audit_trust_counts"),
            "resultCounts": report.get("result_counts"),
        },
    }


def in_scope(symbol: dict[str, Any], scope: str) -> bool:
    return scope == ALL_SCOPE or symbol.get("auditScope") == scope


def overview_for_scope(symbols: list[dict[str, Any]], scope: str) -> dict[str, Any]:
    scoped = [item for item in symbols if in_scope(item, scope)]
    audited = [
        item
        for item in scoped
        if item.get("auditStatus") in {"agent_audited", "human_audited", "out_of_scope"}
    ]
    pending = [item for item in scoped if not item.get("closureEligible")]
    risk_counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
    health_counts: dict[str, int] = {}
    issue_dimensions: dict[str, int] = {}
    for item in scoped:
        risk_counts[str(item.get("riskLevel") or "none")] += 1
        overall = health_overall(item)
        health_counts[overall] = health_counts.get(overall, 0) + 1
        for issue in item.get("issues", []):
            dimension = str(issue.get("dimension") or "unknown")
            issue_dimensions[dimension] = issue_dimensions.get(dimension, 0) + 1
    coverage = round((len(audited) / len(scoped)) * 100, 1) if scoped else 0.0
    return {
        "scope": scope,
        "symbols": len(scoped),
        "audited": len(audited),
        "unaudited": sum(1 for item in scoped if item.get("auditStatus") == "unaudited"),
        "scriptAssessed": sum(1 for item in scoped if item.get("auditStatus") == "script_assessed"),
        "auditExpired": sum(1 for item in scoped if item.get("auditStatus") == "audit_expired"),
        "closureEligible": sum(1 for item in scoped if item.get("closureEligible")),
        "pending": len(pending),
        "highRisk": risk_counts["high"],
        "mediumRisk": risk_counts["medium"],
        "lowRisk": risk_counts["low"],
        "riskCounts": risk_counts,
        "healthCounts": health_counts,
        "topIssueDimensions": sorted(issue_dimensions.items(), key=lambda pair: (-pair[1], pair[0]))[:5],
        "coveragePercent": coverage,
    }


def build_scope_tree(symbols: list[dict[str, Any]]) -> dict[str, Any]:
    root: dict[str, Any] = {"name": ".", "kind": "root", "children": {}, "symbols": []}
    for item in symbols:
        path_parts = str(item.get("source") or "unknown").split("/")
        node = root
        for part in path_parts:
            children = node.setdefault("children", {})
            node = children.setdefault(part, {"name": part, "kind": "path", "children": {}, "symbols": []})
        node.setdefault("symbols", []).append(item["id"])
    return root


def build_report_model(
    *,
    coverage_map: dict[str, Any],
    audit_map: dict[str, Any],
    integrity_report: dict[str, Any] | None,
    integrity_state: dict[str, Any],
    default_scope: str,
    generated_at: str,
) -> dict[str, Any]:
    integrity_by_id = build_integrity_lookup(integrity_report)
    symbols = [
        normalize_symbol(symbol, integrity_by_id)
        for symbol in audit_map.get("symbols", [])
        if isinstance(symbol, dict)
    ]
    priority_items = sorted(
        [item for item in symbols if item["priorityScore"] > 0],
        key=lambda item: (-item["priorityScore"], item["source"], item["name"]),
    )
    return {
        "schema": "project-maintainer.audit-visual-report.v1",
        "generatedAt": generated_at,
        "defaultScope": default_scope,
        "scopes": [{"scope": DEFAULT_SCOPE}, {"scope": ALL_SCOPE}],
        "overview": {
            DEFAULT_SCOPE: overview_for_scope(symbols, DEFAULT_SCOPE),
            ALL_SCOPE: overview_for_scope(symbols, ALL_SCOPE),
        },
        "priorityItems": priority_items[:50],
        "scopeTree": build_scope_tree(symbols),
        "status": {
            "coverageGeneratedAt": coverage_map.get("generated_at"),
            "auditGeneratedAt": audit_map.get("generated_at"),
            "coverageGit": coverage_map.get("git") if isinstance(coverage_map.get("git"), dict) else {},
            "auditGit": audit_map.get("git") if isinstance(audit_map.get("git"), dict) else {},
            "integrity": integrity_state,
        },
        "coverageSummary": coverage_map.get("summary") if isinstance(coverage_map.get("summary"), dict) else {},
        "auditSummary": audit_map.get("summary") if isinstance(audit_map.get("summary"), dict) else {},
        "healthAuditSummary": audit_map.get("health_audit_summary")
        if isinstance(audit_map.get("health_audit_summary"), dict)
        else {},
        "coverageFiles": coverage_map.get("files") if isinstance(coverage_map.get("files"), list) else [],
        "symbols": symbols,
    }


def safe_json_for_script(value: Any) -> str:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_html(model: dict[str, Any]) -> str:
    data = safe_json_for_script(model)
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "  <title>Project Maintainer Audit Report</title>\n"
        "  <style>\n"
        "    :root { color-scheme: light; --ink: #172026; --muted: #65717a; --line: #d9e0e6; --bg: #f7f9fb; --panel: #ffffff; --accent: #0f766e; }\n"
        "    * { box-sizing: border-box; }\n"
        "    body { margin: 0; font-family: Georgia, 'Times New Roman', serif; color: var(--ink); background: var(--bg); }\n"
        "    header { padding: 32px clamp(20px, 5vw, 56px) 24px; border-bottom: 1px solid var(--line); background: var(--panel); }\n"
        "    h1 { margin: 0 0 8px; font-size: clamp(30px, 5vw, 54px); line-height: 1; letter-spacing: 0; }\n"
        "    main { width: min(1200px, calc(100% - 32px)); margin: 24px auto 48px; }\n"
        "    .toolbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 18px; }\n"
        "    input, select { min-height: 40px; border: 1px solid var(--line); background: white; padding: 0 12px; }\n"
        "    input { min-width: min(320px, 100%); }\n"
        "    .dashboard { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }\n"
        "    .metric, section { border: 1px solid var(--line); background: var(--panel); padding: 16px; }\n"
        "    .metric span { display: block; color: var(--muted); font-size: 13px; }\n"
        "    .metric strong { display: block; margin-top: 6px; font-size: 28px; }\n"
        "    table { width: 100%; border-collapse: collapse; background: var(--panel); }\n"
        "    th, td { border-bottom: 1px solid var(--line); padding: 10px; text-align: left; vertical-align: top; }\n"
        "    code { font-family: Consolas, 'Courier New', monospace; font-size: 13px; }\n"
        "    .status.warning { color: #991b1b; font-weight: 700; }\n"
        "    .priority-list { display: grid; gap: 10px; }\n"
        "    .priority-list article { border-left: 4px solid var(--line); padding: 10px 12px; background: #fbfcfd; }\n"
        "    .risk-high { color: #991b1b; }\n"
        "    .risk-medium { color: #9a5b00; }\n"
        "    .risk-low { color: #2f5d50; }\n"
        "    #scopeTree ul { margin: 6px 0 6px 18px; padding: 0; }\n"
        "    #scopeTree li { margin: 4px 0; }\n"
        "    th[onclick] { cursor: pointer; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <header>\n"
        "    <h1>Project Maintainer Audit Report</h1>\n"
        "    <p id=\"statusLine\" class=\"status\"></p>\n"
        "  </header>\n"
        "  <main>\n"
        "    <div class=\"toolbar\"><label>Scope <select id=\"scopeSelect\"><option value=\"default_health_audit\">Default health audit</option><option value=\"all\">All symbols</option></select></label><input id=\"searchInput\" type=\"search\" placeholder=\"Search symbol, class, file\"><select id=\"riskFilter\"><option value=\"\">All risks</option><option value=\"high\">High risk</option><option value=\"medium\">Medium risk</option><option value=\"low\">Low risk</option><option value=\"none\">No risk</option></select><select id=\"statusFilter\"></select></div>\n"
        "    <section><h2>Overview</h2><div id=\"dashboard\" class=\"dashboard\"></div></section>\n"
        "    <section><h2>Priority View</h2><div id=\"priority\" class=\"priority-list\"></div></section>\n"
        "    <section><h2>Scope View</h2><div id=\"scopeTree\"></div></section>\n"
        "    <section><h2>Audit Items</h2><table id=\"detailsTable\"></table></section>\n"
        "  </main>\n"
        "  <script>\n"
        f"    const report = {data};\n"
        "    let currentSort = 'priorityScore';\n"
        "    let sortDescending = true;\n"
        "    function escapeHtml(value) {\n"
        "      return String(value ?? '').replace(/[&<>\"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch]));\n"
        "    }\n"
        "    function activeScope() {\n"
        "      return document.getElementById('scopeSelect').value;\n"
        "    }\n"
        "    function activeSymbols() {\n"
        "      const scope = activeScope();\n"
        "      return report.symbols.filter(item => scope === 'all' || item.auditScope === scope);\n"
        "    }\n"
        "    function populateStatusFilter() {\n"
        "      const statuses = Array.from(new Set(report.symbols.map(item => item.auditStatus))).sort();\n"
        "      document.getElementById('statusFilter').innerHTML = '<option value=\"\">All statuses</option>' + statuses.map(status => '<option value=\"' + escapeHtml(status) + '\">' + escapeHtml(status) + '</option>').join('');\n"
        "    }\n"
        "    function renderDashboard() {\n"
        "      const scope = activeScope();\n"
        "      const overview = report.overview[scope];\n"
        "      document.getElementById('dashboard').innerHTML = [\n"
        "        ['Audited', overview.audited],\n"
        "        ['Unaudited', overview.unaudited],\n"
        "        ['Pending', overview.pending],\n"
        "        ['Closure eligible', overview.closureEligible],\n"
        "        ['High risk', overview.highRisk],\n"
        "        ['Coverage', overview.coveragePercent + '%']\n"
        "      ].map(([label, value]) => '<div class=\"metric\"><span>' + label + '</span><strong>' + value + '</strong></div>').join('');\n"
        "    }\n"
        "    function renderPriority() {\n"
        "      const scope = activeScope();\n"
        "      const items = report.priorityItems.filter(item => scope === 'all' || item.auditScope === scope).slice(0, 10);\n"
        "      document.getElementById('priority').innerHTML = items.map(item =>\n"
        "        '<article><strong class=\"risk-' + escapeHtml(item.riskLevel) + '\">' + escapeHtml(item.riskLevel.toUpperCase()) + '</strong> ' +\n"
        "        escapeHtml(item.name) + ' <code>' + escapeHtml(item.source) + ':' + escapeHtml(item.line || '') + '</code><br>' +\n"
        "        '<span>' + escapeHtml(item.dangerReason) + '</span><br><em>' + escapeHtml(item.suggestedAction) + '</em></article>'\n"
        "      ).join('') || '<p>No prioritized risks recorded for this scope.</p>';\n"
        "    }\n"
        "    function applyFilters() {\n"
        "      const query = document.getElementById('searchInput').value.toLowerCase();\n"
        "      const risk = document.getElementById('riskFilter').value;\n"
        "      const status = document.getElementById('statusFilter').value;\n"
        "      let rows = activeSymbols().filter(item => {\n"
        "        const haystack = [item.name, item.symbol, item.className, item.source, item.kind].join(' ').toLowerCase();\n"
        "        return (!query || haystack.includes(query)) && (!risk || item.riskLevel === risk) && (!status || item.auditStatus === status);\n"
        "      });\n"
        "      rows.sort((a, b) => {\n"
        "        const left = a[currentSort] ?? '';\n"
        "        const right = b[currentSort] ?? '';\n"
        "        if (left < right) return sortDescending ? 1 : -1;\n"
        "        if (left > right) return sortDescending ? -1 : 1;\n"
        "        return a.name.localeCompare(b.name);\n"
        "      });\n"
        "      renderTable(rows);\n"
        "      renderDashboard();\n"
        "      renderPriority();\n"
        "    }\n"
        "    function sortBy(field) {\n"
        "      if (currentSort === field) sortDescending = !sortDescending;\n"
        "      else { currentSort = field; sortDescending = field === 'priorityScore'; }\n"
        "      applyFilters();\n"
        "    }\n"
        "    function renderTable(rows) {\n"
        "      document.getElementById('detailsTable').innerHTML = '<tr><th onclick=\"sortBy(\\'name\\')\">Name</th><th onclick=\"sortBy(\\'source\\')\">Source</th><th onclick=\"sortBy(\\'auditStatus\\')\">Status</th><th onclick=\"sortBy(\\'riskLevel\\')\">Risk</th><th>Action</th></tr>' + rows.map(item => '<tr><td>' + escapeHtml(item.name) + '<br><small>' + escapeHtml(item.kind) + '</small></td><td><code>' + escapeHtml(item.source) + ':' + escapeHtml(item.line || '') + '</code></td><td>' + escapeHtml(item.auditStatus) + '<br><small>' + escapeHtml(item.trustResult) + '</small></td><td class=\"risk-' + escapeHtml(item.riskLevel) + '\">' + escapeHtml(item.riskLevel) + '</td><td>' + escapeHtml(item.suggestedAction) + '</td></tr>').join('');\n"
        "    }\n"
        "    function renderScopeTreeNode(node) {\n"
        "      const children = Object.values(node.children || {});\n"
        "      const symbolCount = (node.symbols || []).length;\n"
        "      return '<li>' + escapeHtml(node.name) + (symbolCount ? ' <small>(' + symbolCount + ' symbols)</small>' : '') + (children.length ? '<ul>' + children.map(renderScopeTreeNode).join('') + '</ul>' : '') + '</li>';\n"
        "    }\n"
        "    function renderScopeTree() {\n"
        "      document.getElementById('scopeTree').innerHTML = '<ul>' + renderScopeTreeNode(report.scopeTree) + '</ul>';\n"
        "    }\n"
        "    function render() { applyFilters(); }\n"
        "    populateStatusFilter();\n"
        "    renderScopeTree();\n"
        "    document.getElementById('scopeSelect').value = report.defaultScope;\n"
        "    ['scopeSelect','riskFilter','statusFilter','searchInput'].forEach(id => document.getElementById(id).addEventListener('input', applyFilters));\n"
        "    const statusEl = document.getElementById('statusLine');\n"
        "    statusEl.className = 'status' + (report.status.integrity.status === 'failed' ? ' warning' : '');\n"
        "    statusEl.textContent = 'Generated ' + report.generatedAt + ' | Integrity: ' + report.status.integrity.message;\n"
        "    applyFilters();\n"
        "  </script>\n"
        "</body>\n"
        "</html>\n"
    )


def generate_report(args: argparse.Namespace) -> Path:
    paths = resolve_paths(args)
    coverage_map = read_json(paths["coverage_map"], "coverage-map.json")
    audit_map = read_json(paths["audit_map"], "symbol-audit-map.json")
    integrity_report, integrity_state = run_integrity_report(args, paths)
    model = build_report_model(
        coverage_map=coverage_map,
        audit_map=audit_map,
        integrity_report=integrity_report,
        integrity_state=integrity_state,
        default_scope=args.scope,
        generated_at=utc_now(),
    )
    html = render_html(model)
    paths["output"].parent.mkdir(parents=True, exist_ok=True)
    paths["output"].write_text(html, encoding="utf-8")
    return paths["output"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--coverage-map", type=Path)
    parser.add_argument("--audit-map", type=Path)
    parser.add_argument("--integrity-report-output", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scope", choices=(DEFAULT_SCOPE, ALL_SCOPE), default=DEFAULT_SCOPE)
    parser.add_argument("--skip-integrity-refresh", action="store_true")
    parser.add_argument("--signing-key-env", default="PROJECT_MAINTAINER_AUDIT_SIGNING_KEY")
    parser.add_argument("--batch-reuse-threshold", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output = generate_report(args)
    except AuditReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"output": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
