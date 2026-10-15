"""Render structured writing sections; no legacy Part1/Part2 input."""
from __future__ import annotations

import json
import re
import hashlib
from pathlib import Path

from scripts._utils import _escape_latex
from scripts.template_registry import render_document, resolve_template

_CITE = re.compile(r"\[cite:([\w./:-]+)\]")
_NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?%?")
_RAW_NUMBERED_ASSET_REFERENCE = re.compile(r"\b(?:Figure|Fig\.|Table)\s+\d+\b", re.IGNORECASE)
_RUN_LEVEL_ABSENCE_PATTERNS = (
    re.compile(r"\b(?:no|without|lack(?:s|ing)?|absence of)\b.{0,60}\brun[- ]level (?:record|detail|outcome)", re.IGNORECASE),
    re.compile(r"\brun[- ]level (?:record|detail|outcome)s?\b.{0,60}\b(?:not available|unavailable|missing|absent|not provided)", re.IGNORECASE),
    re.compile(r"\b(?:record|data)s? do not include\b.{0,60}\brun[- ]level (?:detail|outcome)", re.IGNORECASE),
)
_EMPIRICAL_ASSET_TEMPLATES = {
    "metric_dot_interval", "metric_strip", "metric_bar", "metric_boxplot", "metric_violin",
    "seed_trajectory", "pareto_scatter", "main_metrics_table", "ablation_table", "per_seed_results_table",
    "learning_curve", "line_comparison", "roc_curve", "precision_recall_curve", "calibration_curve",
    "confusion_matrix", "feature_importance", "attention_heatmap", "correlation_heatmap", "qualitative_grid",
}


def _is_empirical_asset(asset: dict) -> bool:
    """Whether an asset makes an empirical result claim rather than records protocol.

    An experiment ID alone only scopes a protocol table; it is not evidence of
    an innovation claim. Result templates must retain a linked claim, while
    legacy manifests without a template continue to be treated as empirical
    when they explicitly carry claim IDs.
    """
    return (
        str(asset.get("template") or "") in _EMPIRICAL_ASSET_TEMPLATES
        or bool(asset.get("claim_ids"))
    )


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_file_path(asset: dict, manifest_path: Path, field: str) -> Path | None:
    """Resolve a declared artifact or source snapshot without trusting cwd."""
    value = str(asset.get(field) or "").strip()
    if not value:
        return None
    declared = Path(value)
    output_root = manifest_path.parent.parent.resolve()
    if declared.is_absolute():
        return declared.resolve()
    if "assets" in declared.parts:
        return output_root.joinpath(*declared.parts[declared.parts.index("assets"):]).resolve()
    return (manifest_path.parent / declared).resolve()


def _measurement_index(path: str | Path | None) -> dict[tuple[str, str, str], dict]:
    if not path:
        return {}
    aggregates = json.loads(Path(path).read_text(encoding="utf-8")).get("aggregates", [])
    return {
        (str(item.get("experiment_id")), str(item.get("method")), str(item.get("metric"))): item
        for item in aggregates if isinstance(item, dict)
    }


def _has_successful_run_level_records(path: str | Path | None) -> bool:
    if not path:
        return False
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return any(
        isinstance(item, dict)
        and isinstance(item.get("record"), dict)
        and item["record"].get("success") is True
        for item in payload.get("measurements") or []
    )


def _claims_current_run_records_are_absent(text: str) -> bool:
    """Reject absence claims about this execution, while allowing literature limits."""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if not any(pattern.search(sentence) for pattern in _RUN_LEVEL_ABSENCE_PATTERNS):
            continue
        lowered = sentence.casefold()
        if any(marker in lowered for marker in ("[cite:", "reference ", "prior ", "paper-", "exp-prior")):
            continue
        # The old proximity pattern treated "records are available and not
        # absent" as an absence claim merely because it saw the word
        # "absent".  Respect explicit negation/contrast around the absence
        # term before raising a factual contradiction.
        if re.search(
            r"\b(?:available|present|provided)\b.{0,40}\b(?:not|rather than|instead of)\s+(?:being\s+)?(?:absent|missing|unavailable)\b",
            lowered,
        ):
            continue
        if re.search(r"\brun[- ]level (?:record|detail|outcome)s?\b.{0,40}\bnot\s+(?:absent|missing|unavailable)\b", lowered):
            continue
        return True
    return False


_NON_EMPIRICAL_NUMBER_CONTEXT = re.compile(
    r"\b(?:doi|isbn|version|seed|gpu|cpu|gb|mb|hour|hours|day|days|epoch|epochs|"
    r"threshold|alpha|p[- ]?value|sentinel|bert|gpt|top[- ]?k)\b",
    re.IGNORECASE,
)


def _has_unbound_empirical_number(text: str, measurement_metrics: set[str]) -> bool:
    """Detect only result-like numeric prose, not every technical numeral.

    IDs (Sentinel-2, a DOI), resources (6 GB), seeds, time budgets and
    declared thresholds are factual protocol metadata, not observed metric
    values.  Requiring a metric assertion for all of them creates false audit
    failures and encourages writers to fabricate irrelevant assertions.
    """
    for sentence in re.split(r"(?<=[.!?])\s+", str(text or "")):
        lowered = sentence.casefold()
        number_matches = list(_NUMBER.finditer(sentence))
        if not number_matches:
            continue
        if "[cite:" in lowered or "doi" in lowered:
            continue
        metric_mentioned = any(
            metric and re.search(r"(?<![a-z0-9_])" + re.escape(metric.casefold()) + r"(?![a-z0-9_])", lowered)
            for metric in measurement_metrics
        )
        result_language = bool(re.search(
            r"\b(?:observed|measured|mean|median|score|performance|result|achieved|improved|decreased|higher|lower|increase|decrease)\b",
            lowered,
        ))
        # Evaluate metadata context around each numeral rather than skipping a
        # whole sentence just because it also names Sentinel-2 or a seed.
        empirical_number_present = False
        for match in number_matches:
            context = sentence[max(0, match.start() - 28): min(len(sentence), match.end() + 28)]
            if _NON_EMPIRICAL_NUMBER_CONTEXT.search(context):
                continue
            empirical_number_present = True
        if empirical_number_present and (metric_mentioned or result_language):
            return True
    return False


_OVERSTATED_CONCLUSION_PATTERNS = (
    re.compile(r"\b(?:confirm|confirms|confirmed)\s+(?:the\s+)?(?:primary\s+)?hypothesis\b", re.IGNORECASE),
    re.compile(r"\b(?:prove|proves|proved|proven)\b", re.IGNORECASE),
    re.compile(r"\bdemonstrat(?:e|es|ed|ing)\s+(?:the\s+)?(?:overall\s+)?effectiveness\b", re.IGNORECASE),
)


def _overstates_conclusion(text: str) -> bool:
    """Catch categorical scientific conclusions that exceed bounded evidence.

    A supported threshold or comparison claim can support or provide evidence
    for a hypothesis.  It does not prove/confirm the hypothesis in general or
    demonstrate overall effectiveness.  This deterministic boundary prevents
    a fluent writer and an equally fluent reviewer from agreeing on the same
    overstatement.
    """
    return any(pattern.search(text) for pattern in _OVERSTATED_CONCLUSION_PATTERNS)


def _measurement_candidates(metric: str) -> list[str]:
    """Map a planned mean name to the traceable per-run metric name.

    Planning may legitimately register ``accuracy_mean`` while the execution
    ledger stores repeated raw ``accuracy`` values and the audit recomputes
    their mean.  Do not use loose substring matching: only the explicit mean
    suffix (and the established ``*_time`` → ``*_time_seconds`` spelling) is
    an alias.
    """
    candidates = [metric]
    if metric.endswith("_mean"):
        raw_metric = metric[:-len("_mean")]
        candidates.append(raw_metric)
        if raw_metric.endswith("_time"):
            candidates.append(f"{raw_metric}_seconds")
    return candidates


def _assertion_error(assertion: dict, measurements: dict[tuple[str, str, str], dict]) -> str | None:
    """Verify one writer-declared numeric or directional statement deterministically."""
    assertion_type = str(assertion.get("type") or "")
    experiment_id = str(assertion.get("experiment_id") or "")
    method = str(assertion.get("method") or "")
    metric = str(assertion.get("metric") or "")
    if assertion_type not in {"metric_value", "metric_comparison"}:
        return f"unsupported evidence assertion type {assertion_type or '<missing>'}"
    observed_metric = next(
        (candidate for candidate in _measurement_candidates(metric) if (experiment_id, method, candidate) in measurements),
        metric,
    )
    observed = measurements.get((experiment_id, method, observed_metric))
    if not observed:
        return f"assertion has no measurement for {experiment_id}/{method}/{metric}"
    if assertion_type == "metric_value":
        try:
            stated = float(assertion["value"])
        except (KeyError, TypeError, ValueError):
            return "metric_value assertion has no numeric value"
        actual = float(observed.get("mean"))
        tolerance = max(0.0005, abs(actual) * 0.005)
        if abs(stated - actual) > tolerance:
            return f"metric_value {stated} disagrees with measured mean {actual} for {experiment_id}/{method}/{metric}"
        return None
    comparison_method = str(assertion.get("comparison_method") or "")
    compared = measurements.get((experiment_id, comparison_method, observed_metric))
    if not compared:
        return f"comparison assertion has no measurement for {experiment_id}/{comparison_method}/{metric}"
    operator = str(assertion.get("operator") or "")
    lhs, rhs = float(observed.get("mean")), float(compared.get("mean"))
    holds = {">": lhs > rhs, ">=": lhs >= rhs, "<": lhs < rhs, "<=": lhs <= rhs}.get(operator)
    if holds is None:
        return f"comparison assertion has unsupported operator {operator or '<missing>'}"
    if not holds:
        return f"comparison {method} {operator} {comparison_method} contradicts measured means {lhs} and {rhs} for {experiment_id}/{metric}"
    return None


def _escape_text_preserving_citations(raw_text: object, asset_labels: dict[str, str] | None = None) -> str:
    """Escape prose while converting citations and registered asset IDs to TeX."""
    keys: list[str] = []
    references: list[str] = []

    def protect(match: re.Match[str]) -> str:
        keys.append(match.group(1))
        return f"ZZCITATIONMARK{len(keys) - 1}ZZ"

    def protect_reference(match: re.Match[str]) -> str:
        asset_id = match.group(2)
        label = (asset_labels or {}).get(asset_id)
        if not label:
            return match.group(0)
        references.append(label)
        return f"ZZASSETREFERENCE{len(references) - 1}ZZ"

    raw = re.sub(r"\b(Figure|Table)\s+\[([A-Za-z0-9_-]+)\]", protect_reference, str(raw_text or ""))
    escaped = _escape_latex(_CITE.sub(protect, raw))
    for index, key in enumerate(keys):
        escaped = escaped.replace(f"ZZCITATIONMARK{index}ZZ", rf"\citep{{{key}}}")
    for index, label in enumerate(references):
        escaped = escaped.replace(f"ZZASSETREFERENCE{index}ZZ", rf"\autoref{{{label}}}")
    return escaped

def audit_sections(
    sections: dict,
    bibliography_path: str | Path,
    claims_path: str | Path | None = None,
    asset_manifest_path: str | Path | None = None,
    measurements_path: str | Path | None = None,
    front: dict | None = None,
) -> list[str]:
    entries = json.loads(Path(bibliography_path).read_text(encoding="utf-8")).get("entries", [])
    keys = {str(x.get("id")) for x in entries if isinstance(x, dict) and (x.get("bibtex") or all(x.get(k) for k in ("authors", "year", "venue")))}
    errors = []
    citation_count = 0
    claims = json.loads(Path(claims_path).read_text(encoding="utf-8")).get("claims", []) if claims_path else []
    claim_by_id = {str(item.get("id")): item for item in claims if isinstance(item, dict) and item.get("id")}
    manifest_path = Path(asset_manifest_path) if asset_manifest_path else None
    assets = json.loads(manifest_path.read_text(encoding="utf-8")).get("assets", []) if manifest_path else []
    asset_by_id = {str(item.get("id")): item for item in assets if isinstance(item, dict) and item.get("id")}
    measurements = _measurement_index(measurements_path)
    measurement_metrics = {metric for _, _, metric in measurements}
    has_run_level_records = _has_successful_run_level_records(measurements_path)
    if manifest_path:
        for asset in asset_by_id.values():
            if asset.get("status") != "available":
                continue
            asset_path = _manifest_file_path(asset, manifest_path, "path")
            if asset_path is not None:
                if not asset_path.is_file():
                    errors.append(f"available asset {asset.get('id')} file is missing")
                elif asset.get("sha256") and _file_digest(asset_path).lower() != str(asset["sha256"]).lower():
                    errors.append(f"available asset {asset.get('id')} sha256 does not match manifest")
            source_path = _manifest_file_path(asset, manifest_path, "source")
            if asset.get("source_sha256"):
                if source_path is None or not source_path.is_file():
                    errors.append(f"asset {asset.get('id')} source snapshot is missing")
                elif _file_digest(source_path).lower() != str(asset["source_sha256"]).lower():
                    errors.append(f"asset {asset.get('id')} source snapshot sha256 does not match manifest")
    paragraph_claim_ids: set[str] = set()
    paragraph_asset_ids: set[str] = set()
    for section_id, section in sections.items():
        if not isinstance(section, dict):
            errors.append(f"section {section_id} must be an object")
            continue
        paragraphs = section.get("paragraphs", [])
        if not isinstance(paragraphs, list):
            errors.append(f"section {section_id} paragraphs must be a list")
            continue
        if not paragraphs:
            errors.append(f"empty section: {section_id}")
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict):
                errors.append(f"section {section_id} contains a non-object paragraph")
                continue
            paragraph_id = str(paragraph.get("paragraph_id") or "<missing-paragraph-id>")
            claim_ids = paragraph.get("claim_ids") if isinstance(paragraph.get("claim_ids"), list) else []
            asset_ids = paragraph.get("asset_ids") if isinstance(paragraph.get("asset_ids"), list) else []
            assertions = paragraph.get("evidence_assertions") if isinstance(paragraph.get("evidence_assertions"), list) else []
            for claim_id in claim_ids:
                claim_id = str(claim_id)
                paragraph_claim_ids.add(claim_id)
                if claims_path and claim_id not in claim_by_id:
                    errors.append(f"unknown claim {claim_id} in {section_id}/{paragraph_id}")
                elif claim_by_id.get(claim_id, {}).get("status") == "untested":
                    errors.append(f"untested claim {claim_id} is cited in {section_id}/{paragraph_id}")
            for asset_id in asset_ids:
                asset_id = str(asset_id)
                paragraph_asset_ids.add(asset_id)
                if asset_manifest_path and asset_id not in asset_by_id:
                    errors.append(f"unknown asset {asset_id} in {section_id}/{paragraph_id}")
                asset = asset_by_id.get(asset_id)
                if asset and asset.get("kind") in {"figure", "table"}:
                    prefix = "Figure" if asset.get("kind") == "figure" else "Table"
                    if f"{prefix} [{asset_id}]" not in str(paragraph.get("text") or ""):
                        errors.append(
                            f"asset {asset_id} in {section_id}/{paragraph_id} is declared but has no explicit "
                            f"{prefix} [{asset_id}] reference"
                        )
                if asset and _is_empirical_asset(asset):
                    linked_claims = {str(value) for value in asset.get("claim_ids") or []}
                    if linked_claims and not linked_claims.intersection(str(value) for value in claim_ids):
                        errors.append(f"asset {asset_id} in {section_id}/{paragraph_id} has no shared claim_id with its paragraph")
            if measurements_path and section_id in {"experiments", "conclusion"} and _has_unbound_empirical_number(str(paragraph.get("text") or ""), measurement_metrics) and not assertions:
                errors.append(f"numeric prose in {section_id}/{paragraph_id} has no evidence_assertions")
            for assertion in assertions:
                if not isinstance(assertion, dict):
                    errors.append(f"non-object evidence assertion in {section_id}/{paragraph_id}")
                    continue
                error = _assertion_error(assertion, measurements)
                if error:
                    errors.append(f"{error} in {section_id}/{paragraph_id}")
            raw_text = str(paragraph.get("text", ""))
            if asset_manifest_path and _RAW_NUMBERED_ASSET_REFERENCE.search(raw_text):
                errors.append(
                    f"raw numbered Figure/Table reference in {section_id}/{paragraph_id}; "
                    "use Figure [asset_id] or Table [asset_id] so final numbering is resolved from the manifest"
                )
            if has_run_level_records and _claims_current_run_records_are_absent(raw_text):
                errors.append(
                    f"{section_id}/{paragraph_id} says run-level records are unavailable, "
                    "but successful run records exist in evidence/measurements.json"
                )
            if section_id == "conclusion" and _overstates_conclusion(raw_text):
                errors.append(
                    f"{section_id}/{paragraph_id} uses categorical proof/confirmation language; "
                    "state that the bounded experiment supports the linked claim instead"
                )
            for key in _CITE.findall(raw_text):
                citation_count += 1
                if key not in keys: errors.append(f"unknown citation {key} in {section_id}")
            for key in keys:
                if f"[{key}]" in raw_text:
                    errors.append(f"bare citation marker [{key}] in {section_id}/{paragraph_id}; use [cite:{key}]")
    abstract_text = str((front or {}).get("abstract") or "")
    if asset_manifest_path and _RAW_NUMBERED_ASSET_REFERENCE.search(abstract_text):
        errors.append("raw numbered Figure/Table reference in abstract; use a manifest asset ID or remove the reference")
    if has_run_level_records and _claims_current_run_records_are_absent(abstract_text):
        errors.append(
            "abstract says run-level records are unavailable, but successful run records exist in evidence/measurements.json"
        )
    if _overstates_conclusion(abstract_text):
        errors.append(
            "abstract uses categorical proof/confirmation language; state that the bounded experiment supports the linked claim instead"
        )
    if keys and citation_count == 0:
        errors.append("bibliography contains entries but no in-text citation markers")
    if asset_manifest_path:
        for asset_id, asset in asset_by_id.items():
            if asset.get("status") != "available" or not _is_empirical_asset(asset):
                continue
            linked_claims = [str(value) for value in asset.get("claim_ids") or []]
            if not linked_claims:
                errors.append(f"empirical asset {asset_id} has no linked claim")
                continue
            for claim_id in linked_claims:
                claim = claim_by_id.get(claim_id)
                if claims_path and not claim:
                    errors.append(f"asset {asset_id} links unknown claim {claim_id}")
                elif claim and claim.get("experiment_id") and claim.get("experiment_id") != asset.get("experiment_id"):
                    errors.append(f"asset {asset_id} experiment {asset.get('experiment_id')} conflicts with claim {claim_id} experiment {claim.get('experiment_id')}")
                elif claim and claim.get("status") in {"not_supported", "inconclusive"}:
                    visual_text = f"{asset.get('purpose') or ''} {asset.get('caption') or ''}".casefold()
                    directional = any(word in visual_text for word in ("outperform", "improve", "supports", "supported"))
                    qualified = "not supported" in visual_text or "inconclusive" in visual_text
                    if directional and not qualified:
                        errors.append(f"asset {asset_id} overstates {claim_id} despite its {claim.get('status')} evidence status")
        empirical_assets = [asset_id for asset_id, asset in asset_by_id.items() if asset.get("status") == "available" and _is_empirical_asset(asset)]
        if empirical_assets and not (paragraph_claim_ids or paragraph_asset_ids):
            errors.append("experiments section has empirical assets but no paragraph-level claim_ids or asset_ids")
    if claims_path:
        for claim_id, claim in claim_by_id.items():
            if claim.get("kind") != "innovation" or claim.get("status") not in {"not_supported", "inconclusive"}:
                continue
            if claim_id not in paragraph_claim_ids:
                errors.append(f"claim {claim_id} is {claim.get('status')} but is not discussed with a paragraph-level claim_id")
    return errors


def _resolve_asset_relative_path(asset: dict, manifest_path: Path) -> str | None:
    """Return a TeX-safe path relative to this run's output directory."""
    output_root = manifest_path.parent.parent.resolve()
    path = _manifest_file_path(asset, manifest_path, "path")
    if path is None:
        return None
    try:
        return path.relative_to(output_root).as_posix()
    except ValueError:
        return None


def _render_asset(asset: dict, manifest_path: Path) -> str | None:
    relative = _resolve_asset_relative_path(asset, manifest_path)
    if not relative:
        return None
    kind = asset.get("kind")
    label = str(asset.get("label") or ("fig:" if kind == "figure" else "tab:") + str(asset.get("id") or "asset"))
    if kind == "figure":
        caption = _escape_latex(str(asset.get("caption") or ""))
        # Keep workflow-owned figures at their declared prose anchor.  Even
        # `!ht` remains a float and TeX may defer it to a later page bottom,
        # leaving a conspicuous blank band above it.  The `float` package's H
        # placement is intentionally used for these generated single-column
        # assets; it preserves figure--claim locality and makes pagination
        # deterministic.  The height cap remains content-agnostic and prevents
        # one tall asset plus its caption from overrunning a page.
        # A very wide and short figure should use its natural, trimmed height.
        # A global 0.62-textheight cap does not remove source padding and can
        # make a small schematic occupy a visually empty page.  The renderer
        # therefore caps only genuinely tall figures; figures produced by the
        # deterministic visual backend are exported with tight bounds.
        return (
            "\\begin{figure}[H]\n\\centering\n"
            "\\includegraphics[width=\\linewidth,height=0.54\\textheight,keepaspectratio]{" + relative + "}\n"
            "\\caption{" + caption + "}\n\\label{" + label + "}\n\\end{figure}"
        )
    if kind == "table":
        return "\\input{" + relative + "}"
    return None

def render(sections: dict, title: str, bibliography_path: str | Path, conference: str, abstract: str = "", asset_manifest_path: str | Path | None = None) -> str:
    entries = json.loads(Path(bibliography_path).read_text(encoding="utf-8")).get("entries", [])
    body = ["\\begin{abstract}\n" + _escape_latex(abstract) + "\n\\end{abstract}"] if abstract else []
    manifest_path = Path(asset_manifest_path) if asset_manifest_path else None
    assets = json.loads(manifest_path.read_text(encoding="utf-8")).get("assets", []) if manifest_path else []
    inserted: set[str] = set()
    asset_labels = {str(asset.get("id")): str(asset.get("label") or ("fig:" if asset.get("kind") == "figure" else "tab:") + str(asset.get("id"))) for asset in assets if isinstance(asset, dict)}
    for section_id in ("introduction", "related_work", "method", "experiments", "limitations", "conclusion"):
        section = sections.get(section_id)
        if not section: continue
        title_text = _escape_latex(str(section.get("title") or section_id.title()))
        paragraphs: list[str] = []
        for paragraph in section.get("paragraphs", []):
            text = _escape_text_preserving_citations(paragraph.get("text"), asset_labels)
            paragraphs.append(text)
            paragraph_id = str(paragraph.get("paragraph_id") or "")
            referenced_asset_ids = [str(value) for value in paragraph.get("asset_ids") or []]
            ordered_assets = [
                asset for asset_id in referenced_asset_ids for asset in assets
                if isinstance(asset, dict) and str(asset.get("id")) == asset_id
            ]
            ordered_assets.extend(asset for asset in assets if asset not in ordered_assets)
            for asset in ordered_assets:
                if not isinstance(asset, dict) or asset.get("status") != "available" or asset.get("section") != section_id:
                    continue
                asset_id = str(asset.get("id"))
                explicitly_referenced = asset_id in referenced_asset_ids
                anchored_here = str(asset.get("placement_after_paragraph_id") or "") == paragraph_id
                if not explicitly_referenced and not anchored_here:
                    continue
                rendered = _render_asset(asset, manifest_path) if manifest_path else None
                if rendered:
                    paragraphs.append(rendered); inserted.add(asset_id)
        body.append(f"\\section{{{title_text}}}\n" + "\n\n".join(paragraphs))
        # If a writer did not preserve the suggested paragraph ID, retain the
        # asset in its intended section instead of silently losing evidence.
        tail = []
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("status") != "available" or asset.get("section") != section_id or str(asset.get("id")) in inserted:
                continue
            rendered = _render_asset(asset, manifest_path) if manifest_path else None
            if rendered: tail.append(rendered); inserted.add(str(asset.get("id")))
        if tail:
            body[-1] += "\n\n" + "\n\n".join(tail)
    if any(isinstance(entry, dict) and (entry.get("bibtex") or all(entry.get(k) for k in ("id", "title", "authors", "year", "venue"))) for entry in entries):
        body.append("\\bibliographystyle{iclr2024_conference}\n\\bibliography{refs}")
    return render_document(resolve_template(conference), title=_escape_latex(title), body="\n\n".join(body))
