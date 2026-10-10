"""Deterministic paired statistics over the per-question records of an executed experiment.

agent-core's experiment execution hands reflection / reporting one point estimate per variant
(``answer_accuracy: 0.62``). The paper-writing agent then has no interval to report and nothing that
says whether two variants actually differ, so papers end up with "single seed, no CI" claims (the
Stanford Agentic Reviewer's Experimental_Soundness -1 on our bl1 paper).

This module computes, from the ``per_question`` records every variant already writes:

* for each top-level metric backed by a per-question field of the *same name* (or an explicit
  ``metric_fields`` mapping) whose mean reproduces it: a percentile bootstrap 95% CI over questions;
* for each comparison inside one experimental condition (same setting prefix, model, dataset and,
  for budgeted arms, the same *recorded* budget) over an identical, uniquely identified item set: the
  paired mean difference, its paired bootstrap 95% CI, and a two-sided p-value (exact McNemar for
  0/1 fields, paired sign-flip permutation test otherwise). Cross-condition comparisons happen only
  when declared explicitly. A pair or metric that cannot be verified is refused with a note (or an
  error in ``ExperimentStats.errors``), never approximated.

Everything is seeded and dependency-free, so the numbers are reproducible and can be re-derived by
anyone from the metrics files. Results are written back into each variant's metrics as flat scalar
keys (``<metric>_ci95_low`` ...), which is where reporting's numeric-traceability check and the
reflection prompt already look.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

ID_KEYS = ("qid", "question_id", "id", "item_id", "task_id", "example_id")
RECORD_KEYS = ("per_question", "task_records", "records", "item_records")
# fields holding the agent's final answer; an empty value means the item ended without an answer
PREDICTION_KEYS = ("predicted", "predicted_answer", "prediction", "pred", "model_answer", "final_answer")
UNANSWERED_STATUSES = ("no_answer", "unanswered", "no_submission", "turn_cap")
DEFAULT_RESAMPLES = 2000
DEFAULT_SEED = 20261001
EQUIVALENCE_MARGIN = 0.05  # a null whose 95% CI lies inside +/- this is reported as bounded
_MATCH_TOL = 5e-4
SETTING_SEP = "__"  # replication settings prefix the variant name: ``m2__proposed_T1``
SIGN_FLIP_TEST = "paired_sign_flip_monte_carlo"
# Recorded in statistics.json and shown to reflection / reporting so the procedure is described exactly.
METHOD = {
    "ci": "percentile bootstrap over items",
    "bootstrap_resamples": DEFAULT_RESAMPLES,
    "bootstrap_seed": DEFAULT_SEED,
    "paired_test_binary": "exact McNemar (two-sided, binomial on discordant pairs)",
    "paired_test_continuous": (f"Monte Carlo paired sign-flip permutation test, {DEFAULT_RESAMPLES} random sign "
                               f"assignments, seed {DEFAULT_SEED + 1}, p = (hits+1)/(resamples+1); an "
                               "approximation, not an exact test"),
    "multiplicity": "Holm step-down within each family (metric x role); confirmatory = pre-registered arms of "
                    "the original setting, exploratory = post hoc variants, replication settings and declared "
                    "cross-condition pairs",
    "bounded_verdict": (f"95% CI inside +/-{EQUIVALENCE_MARGIN:g}: a descriptive bound on the effect, not a "
                        "formal equivalence (TOST) test"),
    "pairing": "within one condition (setting, model, dataset, recorded budget) over identical item-id sets; "
               "cross-condition pairs only when declared",
    "unanswered": "counted from answer fields; 'unknown' when the records carry none (never assumed 0)",
    "primary_coverage": "the primary metric covers every item of each variant; a missing value is scored 0 only "
                        "when the frozen protocol says so and the item is unanswered / an API failure / a parse "
                        "failure, otherwise the comparison is unverified (never computed on the answered subset)",
}
# per-item reasons a primary value can be missing; only these may be scored by a protocol rule
MISSING_REASONS = ("unanswered", "api_error", "parse_error")
MISSING_RULES = ("refuse", "score_zero")
_ERROR_KEYS = ("api_error", "error", "exception")
_PARSE_KEYS = ("parse_error", "parse_failed")
_NOT_METRICS = ("n_questions", "model_call_count", "revision", "budget_tokens", "design_revision",
                "implementation_revision", "n_items_requested")


@dataclass
class MetricStats:
    metric: str  # top-level metric name, e.g. answer_accuracy
    field: str  # per-question field it is the mean of, e.g. correct
    binary: bool
    n: int
    mean: float
    ci_low: float
    ci_high: float


@dataclass
class PairStats:
    metric: str
    a: str
    b: str
    n_paired: int
    mean_diff: float  # mean(a - b)
    ci_low: float
    ci_high: float
    p_value: float
    test: str
    wins: int = 0  # a better than b (binary: a=1, b=0)
    losses: int = 0
    # finishing vs answering: wins of a on items b left unanswered, losses of a on items a left unanswered
    wins_vs_unanswered: int = 0
    losses_vs_unanswered: int = 0
    p_holm: float = 1.0  # Holm-adjusted within its family (same metric and role)
    scope: str = "within_condition"  # or "declared_cross_condition"
    # confirmatory = pre-registered arms of the original setting; anything post hoc, in a replication
    # setting or declared across conditions is exploratory. Holm runs per (metric, role) family.
    role: str = "confirmatory"

    @property
    def family(self) -> str:
        return f"{self.metric}/{self.role}"

    @property
    def verdict(self) -> str:
        if self.ci_low > 0 or self.ci_high < 0:
            return "detectable"
        if -EQUIVALENCE_MARGIN <= self.ci_low and self.ci_high <= EQUIVALENCE_MARGIN:
            return f"bounded within +/-{EQUIVALENCE_MARGIN:g}"
        return "inconclusive"


@dataclass
class ExperimentStats:
    metrics: dict[str, list[MetricStats]] = field(default_factory=dict)  # variant -> stats
    pairs: list[PairStats] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    primary: list[str] = field(default_factory=list)  # metrics that got intervals / paired tests
    # variant -> items ended without an answer; None = unknown (the records carry no answer field)
    unanswered: dict[str, int | None] = field(default_factory=dict)
    # analysis that had to stop: unresolved conditions, an unmappable primary metric (stable prefixes)
    errors: list[str] = field(default_factory=list)
    conditions: dict[str, "Condition"] = field(default_factory=dict)
    # every candidate comparison, computed or not: {metric, a, b, role, scope, status, reason, ...};
    # status is "verified" (computed over the full declared item set) or "unverified" (with why)
    comparisons: list[dict[str, Any]] = field(default_factory=list)
    # variant -> metric -> {n_items, n_scored, scored_zero, missing: {reason: count}, complete}
    coverage: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    missing_rule: str = "refuse"

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": METHOD,
            "missing_primary_rule": self.missing_rule,
            "families": dict(sorted(Counter(p.family for p in self.pairs).items())),
            "conditions": {n: c.to_dict() for n, c in self.conditions.items()},
            "variants": {v: [s.__dict__ for s in stats] for v, stats in self.metrics.items()},
            "coverage": self.coverage,
            "comparisons": self.comparisons,
            "pairs": [{**p.__dict__, "family": p.family, "verdict": p.verdict} for p in self.pairs],
            "unanswered": {k: ("unknown" if v is None else v) for k, v in self.unanswered.items()},
            "errors": self.errors,
            "notes": self.notes,
        }


@dataclass
class Condition:
    """What a variant actually ran under. ``budget``: None = unbudgeted, ``"unknown"`` = not recorded."""

    name: str
    setting: str  # replication prefix, "" for the original setting
    method: str
    tier: str
    budget: float | str | None = "unknown"
    model: str | None = None
    dataset: str | None = None
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def records_of(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    for key in RECORD_KEYS:
        value = metrics.get(key)
        if isinstance(value, list) and value and all(isinstance(r, dict) for r in value):
            return value
    return []


def record_key(record: dict[str, Any], index: int) -> str:
    for key in ID_KEYS:
        if record.get(key) not in (None, ""):
            return str(record[key])
    return f"#{index}"


def item_ids(records: list[dict[str, Any]]) -> tuple[list[str] | None, str]:
    """Stable item ids in record order, or (None, why) when an id is missing or repeated.

    Pairing by row position is never a substitute: two variants can write the same items in a
    different order, and a dropped item silently shifts every later row.
    """
    ids, missing = [], 0
    for record in records:
        rid = next((str(record[k]) for k in ID_KEYS if record.get(k) not in (None, "")), None)
        if rid is None:
            missing += 1
        ids.append(rid)
    if missing:
        return None, f"{missing} of {len(records)} records carry no item id ({'/'.join(ID_KEYS)})"
    seen: set[str] = set()
    duplicates = sorted({i for i in ids if i in seen or seen.add(i)})  # type: ignore[func-returns-value]
    if duplicates:
        return None, f"duplicate item ids {duplicates[:5]}{' ...' if len(duplicates) > 5 else ''}"
    return ids, ""  # type: ignore[return-value]


def unanswered_ids(records: list[dict[str, Any]]) -> set[str] | None:
    """Ids of items that ended without an answer; None when the records carry no answer field."""
    known = False
    out: set[str] = set()
    for index, record in enumerate(records):
        status = str(record.get("status") or "").lower()
        keys = [k for k in PREDICTION_KEYS if k in record]
        if keys or status:
            known = True
        empty = bool(keys) and all(str(record.get(k) if record.get(k) is not None else "").strip() == "" for k in keys)
        if empty or status in UNANSWERED_STATUSES:
            out.add(record_key(record, index))
    return out if known else None


def holm(p_values: list[float]) -> list[float]:
    """Holm step-down adjusted p-values, in input order."""
    order = sorted(range(len(p_values)), key=lambda i: p_values[i])
    adjusted = [1.0] * len(p_values)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(p_values) - rank) * p_values[i]))
        adjusted[i] = running
    return adjusted


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def per_question_fields(records: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """field -> {question id -> value}, for scalar fields present on at least 80% of records.

    Rows are keyed by position (``#i``) when ids are missing or repeated, so a duplicate id cannot
    silently drop a row from an interval; such columns are never paired (see ``item_ids``).
    """
    ids, _ = item_ids(records)
    columns: dict[str, dict[str, float]] = {}
    for index, record in enumerate(records):
        rid = ids[index] if ids is not None else f"#{index}"
        for key, raw in record.items():
            value = as_number(raw)
            if value is not None and key not in ID_KEYS:
                columns.setdefault(key, {})[rid] = value
    floor = 0.8 * len(records)
    return {k: v for k, v in columns.items() if len(v) >= floor}


def missing_reason(record: dict[str, Any]) -> str:
    """Why an item has no value for a metric: unanswered / api_error / parse_error / missing."""
    status = str(record.get("status") or "").lower()
    if any(str(record.get(k) or "").strip() for k in _PARSE_KEYS) or status in ("parse_error", "parse_failed"):
        return "parse_error"
    if any(str(record.get(k) or "").strip() for k in _ERROR_KEYS) or status in ("api_error", "error", "failed"):
        return "api_error"
    keys = [k for k in PREDICTION_KEYS if k in record]
    if status in UNANSWERED_STATUSES or (keys and all(str(record.get(k) or "").strip() == "" for k in keys)):
        return "unanswered"
    return "missing"


def complete_column(records: list[dict[str, Any]], col: str, rule: str = "refuse"
                    ) -> tuple[dict[str, float], dict[str, Any]]:
    """The metric column over *every* record, and its coverage.

    A record without a value is scored 0 only under ``rule == "score_zero"`` and only when its
    reason is one of ``MISSING_REASONS``; any other gap leaves the column incomplete (the caller
    must then refuse to treat it as a full-sample analysis). No 80% floor: one missing item counts.
    """
    ids, _ = item_ids(records)
    column: dict[str, float] = {}
    missing: dict[str, int] = {}
    scored_zero = 0
    for index, record in enumerate(records):
        rid = ids[index] if ids is not None else f"#{index}"
        value = as_number(record.get(col))
        if value is not None:
            column[rid] = value
            continue
        reason = missing_reason(record)
        missing[reason] = missing.get(reason, 0) + 1
        if rule == "score_zero" and reason in MISSING_REASONS:
            column[rid] = 0.0
            scored_zero += 1
    coverage = {"field": col, "n_items": len(records), "n_scored": len(column) - scored_zero,
                "scored_zero": scored_zero, "missing": missing, "complete": len(column) == len(records)}
    return column, coverage


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def bootstrap_ci(values: list[float], *, resamples: int = DEFAULT_RESAMPLES,
                 seed: int = DEFAULT_SEED) -> tuple[float, float]:
    if len(values) < 2:
        return values[0], values[0]
    rng = random.Random(seed)
    n = len(values)
    means = sorted(_mean([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
    return means[int(0.025 * resamples)], means[min(resamples - 1, int(0.975 * resamples))]


def mcnemar_exact(wins: int, losses: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def sign_flip_p(diffs: list[float], *, resamples: int = DEFAULT_RESAMPLES, seed: int = DEFAULT_SEED) -> float:
    observed = abs(_mean(diffs))
    if observed == 0:
        return 1.0
    rng = random.Random(seed + 1)
    hits = sum(
        abs(_mean([d if rng.random() < 0.5 else -d for d in diffs])) >= observed - 1e-12 for _ in range(resamples)
    )
    return (hits + 1) / (resamples + 1)


def match_metrics(
    top_level: dict[str, Any],
    columns: dict[str, dict[str, float]],
    explicit: dict[str, str] | None = None,
    notes: list[str] | None = None,
) -> dict[str, str]:
    """top-level metric name -> per-question field it is the mean of.

    Only a field of the same name, or one named in an explicit mapping (the ``explicit`` argument,
    else a ``metric_fields`` dict in the metrics payload), qualifies; the mapping is then verified
    (the field's mean must reproduce the top-level value). Matching by equal means alone is not
    done: two unrelated metrics can share a mean (``n_api_errors = 0`` and an all-false flag).
    """
    declared = explicit if explicit is not None else top_level.get("metric_fields")
    declared = declared if isinstance(declared, dict) else {}
    matched: dict[str, str] = {}
    for name, raw in top_level.items():
        value = as_number(raw)
        if value is None or isinstance(raw, bool) or name in _NOT_METRICS:
            continue
        col = declared.get(name, name)
        if col not in columns:
            if name in declared and notes is not None:
                notes.append(f"{name}: declared per-item field `{col}` is not in the records")
            continue
        mean = _mean(list(columns[col].values()))
        if abs(mean - value) <= _MATCH_TOL * max(1.0, abs(value)):
            matched[name] = col
        elif notes is not None:
            notes.append(f"{name} = {value:.4f} but per-item `{col}` averages {mean:.4f}; not analysed")
    return matched


_CONDITION_SUFFIX = re.compile(r"^(.*?)[_-]((?:t|b|tier|budget)\d+)$", re.IGNORECASE)


def split_condition(name: str) -> tuple[str, str]:
    """``proposed_T2`` -> (``proposed``, ``t2``); names without a tier suffix -> (name, "")."""
    match = _CONDITION_SUFFIX.match(name)
    return (match.group(1), match.group(2).lower()) if match else (name, "")


def split_setting(name: str) -> tuple[str, str]:
    """``m2__proposed_T1`` -> (``m2``, ``proposed_T1``); the original setting is ""."""
    if SETTING_SEP in name:
        setting, rest = name.split(SETTING_SEP, 1)
        if setting and rest:
            return setting, rest
    return "", name


def _distinct(records: list[dict[str, Any]], key: str) -> list[Any]:
    out: list[Any] = []
    for record in records:
        if key in record and record[key] not in out:
            out.append(record[key])
    return out


def _budget(value: Any) -> float | None:
    if value in (None, "", "none", "None", 0):
        return None
    number = as_number(value)
    return number if number is not None else None


def parse_condition(name: str, metrics: dict[str, Any] | None = None) -> Condition:
    """The condition a variant actually ran under, from its name *and* what it recorded.

    The name gives setting / method / tier; the budget, model and dataset come from the payload
    (top level, ``config``, ``replication``) and are cross-checked against the per-item records.
    Disagreements become ``problems``: such a variant is not paired with anything.
    """
    setting, rest = split_setting(name)
    method, tier = split_condition(rest)
    cond = Condition(name, setting, method, tier)
    if metrics is None:
        return cond
    records = records_of(metrics)
    config = metrics.get("config") if isinstance(metrics.get("config"), dict) else {}
    replication = metrics.get("replication") if isinstance(metrics.get("replication"), dict) else {}

    rec_budgets = [_budget(b) for b in _distinct(records, "budget_tokens")]
    rec_budgets = list(dict.fromkeys(rec_budgets))
    if len(rec_budgets) > 1:
        cond.problems.append(f"records disagree on budget_tokens {rec_budgets}")
    if "budget_tokens" in metrics:
        cond.budget = _budget(metrics["budget_tokens"])
        if rec_budgets and cond.budget not in rec_budgets:
            cond.problems.append(f"budget_tokens {cond.budget} but records say {rec_budgets}")
    elif len(rec_budgets) == 1:
        cond.budget = rec_budgets[0]

    model = config.get("model") or replication.get("requested_model") or metrics.get("model")
    rec_models = [m for m in _distinct(records, "model") if m not in (None, "")]
    if len(rec_models) > 1:
        cond.problems.append(f"records answered by several models {rec_models}")
    elif rec_models and model and rec_models[0] != model:
        cond.problems.append(f"configured model {model} but records answered by {rec_models[0]}")
    cond.model = str(model) if model else (str(rec_models[0]) if len(rec_models) == 1 else None)

    dataset = (config.get("item_list_hash") or replication.get("dataset_hash") or metrics.get("item_list_hash")
               or config.get("dataset") or metrics.get("dataset"))
    if isinstance(dataset, (dict, list)):
        dataset = json.dumps(dataset, sort_keys=True, ensure_ascii=False)
    cond.dataset = str(dataset) if dataset else None
    return cond


def choose_pairs(names: list[str], anchor: str = "proposed",
                 conditions: dict[str, Condition] | None = None) -> list[tuple[str, str]]:
    """Candidate comparisons, kept linear in the number of variants and never across settings:

    * inside each setting, the anchor method (``proposed`` / ``proposed_<tier>``) against every other
      method *at the same tier*, and against untiered variants (e.g. an unconstrained reference) at
      each tier;
    * without an anchor, every pair within a tier; all pairs only when nothing is tiered.

    ``compute`` then checks each candidate against what the variants recorded (``validate_pair``).
    """
    conds = conditions or {n: parse_condition(n) for n in names}
    pairs: list[tuple[str, str]] = []
    for setting in sorted({conds[n].setting for n in names}):
        group_names = [n for n in names if conds[n].setting == setting]
        tiers = sorted({conds[n].tier for n in group_names if conds[n].tier})
        anchors = [n for n in group_names if conds[n].method == anchor]
        if not tiers:
            if anchors:
                pairs += [(anchors[0], b) for b in group_names if b != anchors[0]]
            else:
                pairs += [(a, b) for i, a in enumerate(group_names) for b in group_names[i + 1:]]
            continue
        untiered = [n for n in group_names if not conds[n].tier]
        for tier in tiers:
            group = [n for n in group_names if conds[n].tier == tier]
            heads = [n for n in group if conds[n].method == anchor]
            if heads:
                pairs += [(heads[0], b) for b in group + untiered if b != heads[0]]
            else:
                pairs += [(a, b) for i, a in enumerate(group) for b in group[i + 1:]]
    return pairs


def validate_pair(a: Condition, b: Condition, *, declared: bool = False) -> str:
    """Why ``a`` and ``b`` may not be compared ("" when they may)."""
    for cond in (a, b):
        if cond.problems:
            return f"{cond.name} condition unresolved ({'; '.join(cond.problems)})"
    if declared:
        return ""
    if a.setting != b.setting:
        return f"different settings ({a.setting or 'original'} vs {b.setting or 'original'})"
    for attr in ("model", "dataset"):
        va, vb = getattr(a, attr), getattr(b, attr)
        if (va is None) != (vb is None):
            return f"{attr} recorded for only one side ({va} vs {vb})"
        if va != vb:
            return f"different {attr} ({va} vs {vb})"
    if a.tier and b.tier:
        if a.budget == "unknown" or b.budget == "unknown":
            return "budget not recorded; a shared tier name does not show equal budgets"
        if a.budget != b.budget:
            return f"same tier name but recorded budgets differ ({a.budget} vs {b.budget})"
    return ""


def compute(
    variants: dict[str, dict[str, Any]],
    *,
    primary: Iterable[str] = (),
    anchor: str = "proposed",
    metric_fields: dict[str, str] | None = None,
    cross_pairs: Iterable[tuple[str, str]] = (),
    complete: Iterable[str] | None = None,
    missing_rule: str = "refuse",
    extra_pairs: Iterable[tuple[str, str]] = (),
) -> ExperimentStats:
    """``variants``: variant name -> its metrics.json payload.

    ``metric_fields`` maps a top-level metric to its per-item field when the names differ;
    ``cross_pairs`` are comparisons across conditions (another model / dataset / setting) the
    design explicitly asks for — they are never generated automatically.

    ``complete`` (default: the first of ``primary``) are metrics that must cover every item: a gap
    is filled only by ``missing_rule`` (``refuse`` | ``score_zero``, from the frozen protocol), and
    a still-incomplete column makes each of its comparisons ``unverified`` instead of shrinking the
    sample to the scored items. Other metrics may be partial; their denominators are reported.

    ``extra_pairs`` (e.g. the comparisons review items require) are added to the anchor candidates
    and validated like them (same condition, same items).
    """
    if missing_rule not in MISSING_RULES:
        raise ValueError(f"missing_rule {missing_rule!r} not in {MISSING_RULES}")
    out = ExperimentStats(missing_rule=missing_rule)
    wanted = list(dict.fromkeys(primary))
    must_cover = set(wanted[:1] if complete is None else complete)
    records = {name: records_of(m) for name, m in variants.items()}
    columns = {name: per_question_fields(records[name]) for name in variants}
    unanswered = {name: unanswered_ids(records[name]) for name in variants}
    ids = {name: item_ids(records[name]) for name in variants}
    out.unanswered = {name: (len(u) if u is not None else None) for name, u in unanswered.items()}
    out.conditions = {name: parse_condition(name, m) for name, m in variants.items()}
    for name, cond in out.conditions.items():
        if cond.problems:
            out.errors.append(f"CONDITION_UNRESOLVED {name}: {'; '.join(cond.problems)}")
    for name, metrics in variants.items():  # full-coverage columns replace the 80%-floor ones
        declared_fields = metric_fields if metric_fields is not None else metrics.get("metric_fields")
        declared_fields = declared_fields if isinstance(declared_fields, dict) else {}
        for metric in must_cover:
            col = declared_fields.get(metric, metric)
            if not records[name] or not any(col in r for r in records[name]):
                continue
            column, coverage = complete_column(records[name], col, missing_rule)
            columns[name][col] = column
            out.coverage.setdefault(name, {})[metric] = coverage
    matched: dict[str, dict[str, str]] = {}
    for name, metrics in variants.items():
        if not columns[name]:
            out.notes.append(f"{name}: no per-question records, no interval computed")
            continue
        notes: list[str] = []
        matched[name] = match_metrics(metrics, columns[name], metric_fields, notes)
        out.notes += [f"{name}: {n}" for n in notes]
        stats = []
        for metric, col in matched[name].items():
            values = list(columns[name][col].values())
            low, high = bootstrap_ci(values)
            binary = all(v in (0.0, 1.0) for v in values)
            stats.append(MetricStats(metric, col, binary, len(values), _mean(values), low, high))
            if metric not in must_cover:
                out.coverage.setdefault(name, {})[metric] = {
                    "field": col, "n_items": len(records[name]), "n_scored": len(values), "scored_zero": 0,
                    "missing": {"not_recorded": len(records[name]) - len(values)} if len(values) < len(records[name])
                    else {}, "complete": len(values) == len(records[name])}
        out.metrics[name] = stats

    # Paired tests only for the declared decision metrics; one that cannot be mapped in a variant
    # stops for that variant (an error), it is never replaced by another metric.
    if not wanted:
        out.notes.append("no decision metric declared: intervals only, no paired tests")
    out.primary = wanted
    candidates = [(a, b, False) for a, b in choose_pairs(sorted(variants), anchor, out.conditions)]
    seen = {frozenset((a, b)) for a, b, _ in candidates}
    for a, b in extra_pairs:
        if a == b or not {a, b} <= variants.keys() or frozenset((a, b)) in seen:
            continue
        seen.add(frozenset((a, b)))
        candidates.append((a, b, False))
    candidates += [(a, b, True) for a, b in cross_pairs if a in variants and b in variants]

    def record(metric: str, a: str, b: str, declared: bool, why: str, **extra: Any) -> None:
        out.comparisons.append({
            "metric": metric, "a": a, "b": b, "role": _role(variants, out.conditions, a, b, declared),
            "scope": "declared_cross_condition" if declared else "within_condition",
            "complete_coverage_required": metric in must_cover,
            "status": "unverified" if why else "verified", "reason": why, **extra})

    def gap(side: str, metric: str) -> str:
        coverage = out.coverage.get(side, {}).get(metric)
        if metric not in must_cover or not coverage or coverage["complete"]:
            return ""
        lacking = sum(coverage["missing"].values()) - coverage["scored_zero"]
        return (f"{side}: {lacking} of {coverage['n_items']} items lack `{metric}` ({coverage['missing']}, "
                f"rule {missing_rule})")

    for metric in wanted:
        unmapped = sorted(n for n in variants if n not in matched or metric not in matched[n])
        if unmapped:
            out.errors.append(f"PRIMARY_METRIC_UNMAPPED {metric}: no verified per-item field in {unmapped}")
        for a, b, declared in candidates:
            gaps = [g for g in (gap(a, metric), gap(b, metric)) if g]
            if gaps:  # the coverage gap is the cause, also when it made the mean unverifiable
                why = "primary metric incomplete — " + "; ".join(gaps)
                out.notes.append(f"{metric}: {a} vs {b} not compared: {why}")
                record(metric, a, b, declared, why)
                continue
            if a in unmapped or b in unmapped:
                record(metric, a, b, declared, "metric has no verified per-item field in "
                       + ", ".join(s for s in (a, b) if s in unmapped))
                continue
            why = validate_pair(out.conditions[a], out.conditions[b], declared=declared)
            if not why:
                for side in (a, b):
                    if ids[side][0] is None:
                        why = f"{side}: {ids[side][1]}"
                        break
            if not why and set(ids[a][0]) != set(ids[b][0]):  # type: ignore[arg-type]
                items_a, items_b = set(ids[a][0]), set(ids[b][0])  # type: ignore[arg-type]
                only_a, only_b = items_a - items_b, items_b - items_a
                why = f"item sets differ ({len(only_a)} only in {a}, {len(only_b)} only in {b})"
            if why:
                out.notes.append(f"{metric}: {a} vs {b} not compared: {why}")
                record(metric, a, b, declared, why)
                continue
            ca, cb = columns[a][matched[a][metric]], columns[b][matched[b][metric]]
            common = sorted(set(ca) & set(cb))
            if len(common) < 2:
                out.notes.append(f"{metric}: {a} vs {b} share {len(common)} scored questions, not compared")
                record(metric, a, b, declared, f"only {len(common)} items scored on both sides")
                continue
            diffs = [ca[q] - cb[q] for q in common]
            low, high = bootstrap_ci(diffs)
            wins = sum(1 for d in diffs if d > 0)
            losses = sum(1 for d in diffs if d < 0)
            if all(ca[q] in (0.0, 1.0) and cb[q] in (0.0, 1.0) for q in common):
                p, test = mcnemar_exact(wins, losses), "exact_mcnemar"
            else:
                p, test = sign_flip_p(diffs), SIGN_FLIP_TEST
            ua, ub = unanswered.get(a) or set(), unanswered.get(b) or set()
            wins_u = sum(1 for q in common if ca[q] > cb[q] and q in ub)
            losses_u = sum(1 for q in common if ca[q] < cb[q] and q in ua)
            pair = PairStats(metric, a, b, len(common), _mean(diffs), low, high, p, test, wins, losses,
                             wins_u, losses_u, scope="declared_cross_condition" if declared else "within_condition",
                             role=_role(variants, out.conditions, a, b, declared))
            out.pairs.append(pair)
            n_items = len(ids[a][0] or [])  # type: ignore[arg-type]
            record(metric, a, b, declared, "", n_items=n_items, n_paired=len(common), _pair=pair)
    for family in {p.family for p in out.pairs}:
        group = [p for p in out.pairs if p.family == family]
        for pair, adjusted in zip(group, holm([p.p_value for p in group])):
            pair.p_holm = adjusted
    for entry in out.comparisons:  # outcome after Holm, from the pair it summarises
        pair = entry.pop("_pair", None)
        if pair is not None:
            entry.update(mean_diff=_r(pair.mean_diff), ci95=[_r(pair.ci_low), _r(pair.ci_high)],
                         p=_r(pair.p_value), p_holm=_r(pair.p_holm), verdict=pair.verdict, outcome=outcome_of(pair))
    return out


def outcome_of(pair: PairStats) -> str:
    """What a verified comparison says about ``a`` vs ``b`` — a null or negative is still an outcome."""
    if pair.ci_low > 0:
        return "a_better"
    if pair.ci_high < 0:
        return "a_worse"
    return "bounded_null" if pair.verdict.startswith("bounded") else "inconclusive"


def _post_hoc(metrics: dict[str, Any]) -> bool:
    flag = metrics.get("post_hoc")
    return flag is True or str(flag).lower() in ("true", "1", "yes")


def _role(variants: dict[str, dict[str, Any]], conditions: dict[str, Condition], a: str, b: str,
          declared: bool) -> str:
    """confirmatory only for pre-registered arms of the original setting compared within condition."""
    if declared:
        return "exploratory"
    if any(conditions[v].setting or _post_hoc(variants[v]) for v in (a, b)):
        return "exploratory"
    return "confirmatory"


def _r(value: float) -> float:
    return round(value, 4)


def annotate(variants: dict[str, dict[str, Any]], stats: ExperimentStats) -> None:
    """Write the statistics into each variant's metrics as flat scalars, in place.

    Keys go to the *front* of the dict: agent-core's ``compact_metrics`` (what the manager and
    reflection see) keeps only the first 40 scalars, and reporting's ``known_numbers`` accepts any
    top-level scalar, so this is what makes the intervals both visible and citable.
    """
    added: dict[str, dict[str, Any]] = {name: {} for name in variants}
    for name, items in stats.metrics.items():
        for s in items:
            if s.metric in stats.primary:
                added[name][f"{s.metric}_ci95_low"] = _r(s.ci_low)
                added[name][f"{s.metric}_ci95_high"] = _r(s.ci_high)
                added[name][f"{s.metric}_n"] = s.n
                coverage = stats.coverage.get(name, {}).get(s.metric)
                if coverage:
                    added[name][f"{s.metric}_n_items"] = coverage["n_items"]
                    if coverage["scored_zero"]:
                        added[name][f"{s.metric}_scored_zero_by_protocol"] = coverage["scored_zero"]
    for p in stats.pairs:
        for owner, other, sign in ((p.a, p.b, 1.0), (p.b, p.a, -1.0)):
            low, high = (p.ci_low, p.ci_high) if sign > 0 else (-p.ci_high, -p.ci_low)
            prefix = f"{p.metric}_diff_vs_{other}"
            added[owner][prefix] = _r(sign * p.mean_diff)
            added[owner][f"{prefix}_ci95_low"] = _r(low)
            added[owner][f"{prefix}_ci95_high"] = _r(high)
            added[owner][f"{prefix}_p"] = _r(p.p_value)
            added[owner][f"{prefix}_p_holm"] = _r(p.p_holm)
    for name, count in stats.unanswered.items():
        if count is not None:  # unknown stays absent rather than becoming 0
            added[name]["unanswered_items"] = count
    for name, extra in added.items():
        if extra:
            rest = {k: v for k, v in variants[name].items() if k not in extra}
            variants[name].clear()
            variants[name].update(extra)
            variants[name].update(rest)


def markdown(stats: ExperimentStats) -> str:
    lines = ["# Paired statistics (host-computed, deterministic)", ""]
    for name, items in sorted(stats.metrics.items()):
        for s in items:
            lines.append(f"- `{name}` {s.metric} = {s.mean:.4f} (95% CI [{s.ci_low:.4f}, {s.ci_high:.4f}], n={s.n})")
    if stats.unanswered:
        lines += ["", "Items that ended without an answer (scored by the primary metric as given): "
                  + ", ".join(f"`{k}` {'unknown' if v is None else v}" for k, v in sorted(stats.unanswered.items()))]
    if stats.pairs:
        lines += ["", "| metric | role | A | B | n paired | mean(A-B) | 95% CI | p | p (Holm, family) | verdict "
                  "| wins / losses | of which vs unanswered | test |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in sorted(stats.pairs, key=lambda q: (q.metric, q.role != "confirmatory")):
            family_size = sum(1 for q in stats.pairs if q.family == p.family)
            lines.append(
                f"| {p.metric} | {p.role} | {p.a} | {p.b} | {p.n_paired} | {p.mean_diff:+.4f} | "
                f"[{p.ci_low:+.4f}, {p.ci_high:+.4f}] | {p.p_value:.4f} | {p.p_holm:.4f} (of {family_size}) | "
                f"{p.verdict} | {p.wins} / {p.losses} | {p.wins_vs_unanswered} / {p.losses_vs_unanswered} | {p.test} |"
            )
        lines += ["", "How to read this table (binding for reflection and the paper):",
                  "- Only `confirmatory` rows test the pre-registered hypotheses. `exploratory` rows (post hoc "
                  "variants, replication settings, declared cross-condition pairs) generate hypotheses; label them "
                  "so and never promote one to a headline result.",
                  "- Significance claims use `p (Holm)` of the row's family (metric x role), not the raw p; do not "
                  "pick significant rows out of the table without that correction.",
                  f"- `detectable` = CI excludes 0; `bounded within +/-{EQUIVALENCE_MARGIN:g}` = the CI lies inside "
                  "the margin — a descriptive bound, not a formal equivalence (TOST) test; otherwise `inconclusive`.",
                  f"- `{SIGN_FLIP_TEST}` is a Monte Carlo approximation ({DEFAULT_RESAMPLES} random sign flips, "
                  "seeded); call it a permutation test, not an exact test. `exact_mcnemar` is exact.",
                  f"- Bootstrap: {DEFAULT_RESAMPLES} resamples, seed {DEFAULT_SEED}.",
                  "- `of which vs unanswered`: wins of A on items B left unanswered / losses of A on items A left "
                  "unanswered — the part of a difference that is about finishing rather than answering."]
        cross = [p for p in stats.pairs if p.scope != "within_condition"]
        if cross:
            lines.append("Declared cross-condition comparisons (different model / dataset / setting): "
                         + ", ".join(f"{p.a} vs {p.b}" for p in cross) + ".")
    unverified = [c for c in stats.comparisons if c["status"] != "verified"]
    if unverified:
        lines += ["", "Comparisons NOT verified (no claim may rest on them; missing evidence is not a null result):"]
        lines += [f"- [{c['role']}] {c['metric']} {c['a']} vs {c['b']}: {c['reason']}" for c in unverified]
    partial = []
    for v, per in sorted(stats.coverage.items()):
        partial += [(v, m, c) for m, c in per.items() if not c["complete"] or c["scored_zero"]]
    if partial:
        lines += ["", "Coverage (denominators; missing values by reason):"]
        for v, m, c in partial:
            zero = (f", {c['scored_zero']} scored 0 by the protocol rule ({stats.missing_rule})"
                    if c["scored_zero"] else "")
            missing = f", missing {c['missing']}" if c["missing"] else ""
            lines.append(f"- `{v}` {m}: {c['n_scored']} scored of {c['n_items']} items{zero}{missing}")
    if stats.errors:
        lines += ["", "Analysis stopped (not substituted):"] + [f"- {e}" for e in stats.errors]
    if stats.notes:
        lines += ["", "Notes:"] + [f"- {n}" for n in stats.notes]
    return "\n".join(lines) + "\n"
