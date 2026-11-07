"""Spend guard for one paper run, priced from the task's own usage ledger.

``model_calls.jsonl`` records input / cache-hit / output tokens for every pipeline model call. The
guard prices them with the provider's published per-million-token rates and, before each manager
round (the manager agent is rebuilt every round, so its system prompt is re-read):

* above the **soft** limit, appends a host notice telling the manager to stop experimenting and
  write the paper with the results it has;
* above the **hard** limit, aborts the round (the run can be resumed later with a higher limit).

Experiment subprocesses are not in the ledger (see ``experiment_model``); keep headroom for them.
Peak / off-peak follows DeepSeek's rule (peak = Mon-Fri 9-12 and 14-18 Beijing time); public
holidays are priced as peak, so the estimate errs high.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# yuan per million tokens: (cache hit, cache miss, output), peak prices; off-peak is half.
# Source: api-docs.deepseek.com/zh-cn/quick_start/pricing, checked 2026-10-01.
PRICES = {
    "deepseek-flash": (0.04, 2.0, 8.0),
    "deepseek-v4-pro": (0.30, 9.0, 27.0),
}
_BEIJING = timezone(timedelta(hours=8))


class BudgetExceeded(RuntimeError):
    pass


def is_peak(ts: datetime) -> bool:
    local = ts.astimezone(_BEIJING)
    return local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18)


def ledger_cost(path: Path, prices: dict[str, tuple[float, float, float]] | None = None) -> tuple[float, dict]:
    """Priced spend of the ledger and its token totals (``prices`` defaults to ``PRICES``).

    ``unknown_calls``: calls (usually failed ones) for which the provider returned no usage — their
    cost is unknown, not zero, and is *not* in the total. ``unpriced_models``: models without a
    published price here; their calls are priced as the dearest known model (an estimate).
    """
    prices = PRICES if prices is None else prices
    total = 0.0
    tokens: dict = {"input": 0, "cache_hit": 0, "output": 0, "calls": 0, "unpriced_calls": 0, "unknown_calls": 0,
                    "failed_calls_with_usage": 0, "unpriced_models": [], "estimated_yuan": 0.0}
    if not path.is_file():
        return 0.0, tokens
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        call = event.get("model_call") or {}
        counts = call.get("tokens") or {}
        tokens["calls"] += 1
        if all(counts.get(k) is None for k in ("input", "output")):
            tokens["unknown_calls"] += 1
            continue
        if call.get("status") not in (None, "succeeded"):
            tokens["failed_calls_with_usage"] += 1
        inp, hit, out = int(counts.get("input") or 0), int(counts.get("cache_hit") or 0), int(counts.get("output") or 0)
        tokens["input"] += inp
        tokens["cache_hit"] += hit
        tokens["output"] += out
        model = str(call.get("model") or "")
        rate = prices.get(model)
        if rate is None:
            tokens["unpriced_calls"] += 1
            if model not in tokens["unpriced_models"]:
                tokens["unpriced_models"].append(model)
            rate = max(prices.values(), key=lambda r: r[1])  # unknown model: price as the dearest
        try:
            factor = 1.0 if is_peak(datetime.fromisoformat(event["ts"])) else 0.5
        except (KeyError, ValueError):
            factor = 1.0
        cost = factor * (hit * rate[0] + max(0, inp - hit) * rate[1] + out * rate[2]) / 1e6
        total += cost
        if model not in prices:
            tokens["estimated_yuan"] += cost
    return total, tokens


def deepseek_balance(api_key: str, api_base: str = "https://api.deepseek.com", timeout: float = 20.0) -> float | None:
    """Account balance in CNY from DeepSeek's ``/user/balance``; None when it cannot be read."""
    import urllib.request

    request = urllib.request.Request(f"{api_base.rstrip('/')}/user/balance",
                                     headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for info in payload.get("balance_infos") or []:
            if info.get("currency") == "CNY":
                return float(info["total_balance"])
    except Exception:
        return None
    return None


_STOP_TEXT = (
    "Do not start new designs, code or experiments. If a process-completed execution exists, go to "
    "`reporting` now and write the paper with the results you have (report the budget stop as a "
    "limitation); if reporting already succeeded, emit DONE.\n"
)


@dataclass
class BudgetGuard:
    """Ledger limits (``soft_yuan`` / ``hard_yuan``) and/or account-balance floors.

    The ledger misses calls that failed after the provider had already billed them (e.g. a
    completion that streamed for 15 minutes and then errored), so when the provider exposes a
    balance endpoint the balance floors are the reliable guard.
    """

    ledger: Path
    soft_yuan: float = float("inf")
    hard_yuan: float = float("inf")
    balance_reader: object = None  # () -> float | None
    soft_floor: float | None = None
    hard_floor: float | None = None

    def check_hard(self) -> tuple[float, float | None]:
        """Raise ``BudgetExceeded`` past a hard limit; returns (ledger spend, balance or None)."""
        spent, _ = ledger_cost(self.ledger)
        if spent >= self.hard_yuan:
            raise BudgetExceeded(f"pipeline spend {spent:.2f} yuan >= hard limit {self.hard_yuan:.2f}")
        balance = self.balance_reader() if callable(self.balance_reader) else None
        if balance is not None and self.hard_floor is not None and balance <= self.hard_floor:
            raise BudgetExceeded(f"account balance {balance:.2f} yuan <= hard floor {self.hard_floor:.2f}")
        return spent, balance

    def notice(self) -> str:
        spent, balance = self.check_hard()
        if balance is not None and self.soft_floor is not None and balance <= self.soft_floor:
            return ("\n\n## HOST BUDGET NOTICE\n\n"
                    f"The model account balance is down to {balance:.2f} yuan (floor {self.soft_floor:.0f}). "
                    + _STOP_TEXT)
        if spent < self.soft_yuan:
            return ""
        _, tokens = ledger_cost(self.ledger)
        caveat = ""
        if tokens["unpriced_models"] or tokens["unknown_calls"]:
            caveat = (f" (estimate: {tokens['unknown_calls']} calls without usage are not included; "
                      f"unpriced models {tokens['unpriced_models']} are priced as the dearest known model)")
        return (
            "\n\n## HOST BUDGET NOTICE\n\n"
            f"The run has spent about {spent:.1f} yuan of a {self.soft_yuan:.0f} yuan budget{caveat}. " + _STOP_TEXT
        )


def install_budget_guard(guard: BudgetGuard) -> None:
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.manager import agent as manager

    # agent-core exposes no public hook for the manager prompt; the private loader is wrapped by name
    current = getattr(manager, "_load_system_prompt")
    base = getattr(current, "__budget_base__", current)

    def _load_system_prompt():
        return base() + guard.notice()

    _load_system_prompt.__budget_base__ = base  # type: ignore[attr-defined]
    _load_system_prompt.__wrapped__ = getattr(base, "__wrapped__", base)  # type: ignore[attr-defined]
    setattr(manager, "_load_system_prompt", _load_system_prompt)


def install_execution_budget_check(guard: BudgetGuard) -> None:
    """Check the hard limits before every experiment variant, not only between manager rounds.

    An execution of 40 variants can run for hours; the manager-round check alone would only notice
    the overspend afterwards. ``BudgetExceeded`` aborts the execution (resumable, as for a round).
    """
    from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.experiment_execution import agent as module

    cls = module.ExperimentExecutionAgent
    current = cls.__dict__["_run_variant"]
    base = getattr(current, "__budget_base__", current)
    func = base.__func__ if isinstance(base, classmethod) else base

    def _run_variant(klass, variant, **kwargs):
        guard.check_hard()
        return func(klass, variant, **kwargs)

    wrapped = classmethod(_run_variant)
    wrapped.__budget_base__ = base  # type: ignore[attr-defined]
    setattr(cls, "_run_variant", wrapped)


# --------------------------------------------------------------------------- spend report
def _review_usage(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {"file": str(path), "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "unknown_calls": 0,
           "models": sorted({r.get("model") for r in data.get("reviews", []) if r.get("model")})}
    for review in data.get("reviews", []) + ([data["evidence_check"]] if data.get("evidence_check") else []):
        out["calls"] += 1
        if review.get("prompt_tokens") is None:
            out["unknown_calls"] += 1  # failed call without usage: unknown, not zero
        else:
            out["prompt_tokens"] += int(review["prompt_tokens"])
            out["completion_tokens"] += int(review.get("completion_tokens") or 0)
    return out


def _experiment_usage(results_dir: Path) -> dict:
    out: dict = {"variants": 0, "items": 0, "prompt_tokens": 0, "completion_tokens": 0,
                 "note_prompt_tokens": 0, "note_completion_tokens": 0, "variants_without_token_fields": [],
                 "models": []}
    for path in sorted(results_dir.glob("*.metrics.json")) if results_dir.is_dir() else []:
        try:
            metrics = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        records = metrics.get("per_question") or []
        if not isinstance(records, list) or not records:
            continue
        out["variants"] += 1
        if not any("prompt_tokens" in r for r in records if isinstance(r, dict)):
            out["variants_without_token_fields"].append(path.name[: -len(".metrics.json")])
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            out["items"] += 1
            for key in ("prompt_tokens", "completion_tokens", "note_prompt_tokens", "note_completion_tokens"):
                out[key] += int(record.get(key) or 0)
            model = record.get("model")
            if model and model not in out["models"]:
                out["models"].append(model)
    return out


def _execution_history(results_dir: Path) -> dict:
    """Executions per cell from the evidence history: runs, failed attempts (each may have cost
    tokens that no metrics file records) and referenced (reused, not re-run) cells."""
    from jiuwenswarm.agents.harness.common.paper_pipeline import evidence

    folder = evidence.evidence_dir(results_dir)
    out: dict = {"cells": {}, "failed_attempts": 0, "reruns": 0, "reused_cells": []}
    for history in sorted(folder.glob("cells/*/history.jsonl")):
        runs = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines() if line.strip()]
        failed = sum(1 for r in runs if r.get("status") != "completed")
        out["cells"][history.parent.name] = {"executions": len(runs), "failed": failed}
        out["failed_attempts"] += failed
        out["reruns"] += max(0, len(runs) - 1)
    manifest = evidence.load_manifest(results_dir) or {}
    cells = manifest.get("cells") or {}
    out["reused_cells"] = sorted(n for n, c in cells.items() if c.get("role") in ("frozen", "reused"))
    if out["failed_attempts"]:
        out["note"] = "failed attempts consumed experiment calls whose usage is unknown (no metrics were written)"
    return out


def spend_report(run_dir: Path, *, balance_before: float | None = None, balance_after: float | None = None) -> dict:
    """Every model call of the run by source, each labelled by how well it is known.

    * ``recorded_priced``: pipeline ledger calls with usage and a published price;
    * ``estimated``: ledger calls with usage but no published price (priced as the dearest model);
    * ``unknown``: calls without usage (typically failures); experiment / review calls are listed as
      recorded tokens with **unknown price** — no price table exists here for those endpoints;
    * ``account_balance_delta``: a shared account's balance change, which is *not* this task's cost.
    Billing data, when the operator has it, is the only authoritative number.
    """
    run_dir = Path(run_dir)
    priced_all, tokens = ledger_cost(run_dir / "model_calls.jsonl")
    estimated = tokens["estimated_yuan"]
    report = {
        "pipeline_ledger": {"tokens": tokens, "recorded_priced_yuan": round(priced_all - estimated, 4),
                            "estimated_yuan_unpriced_models": round(estimated, 4),
                            "unknown_cost_calls": tokens["unknown_calls"],
                            "pricing": "DeepSeek published rates, peak/off-peak by timestamp (see PRICES)"},
        "experiments": {"results_dirs": {}, "price": "unknown (experiment endpoint not in the price table)"},
        "review_panels": [],
        "account_balance": {"before": balance_before, "after": balance_after,
                            "delta": (round(balance_before - balance_after, 4)
                                      if balance_before is not None and balance_after is not None else None),
                            "note": "shared account: other tasks draw on it; not this task's exact cost"},
    }
    for results in sorted(run_dir.glob("experiments/*/results")):
        usage = _experiment_usage(results)
        usage["executions"] = _execution_history(results)
        report["experiments"]["results_dirs"][str(results)] = usage
    report["enforcement"] = (
        "hard limits are checked before every manager round and before every experiment variant starts; "
        "a variant already running is not interrupted, and experiment / review calls are not in the "
        "pipeline ledger, so neither is a strict limit inside one long experiment")
    for panel in sorted(run_dir.rglob("review_panel.json")):
        try:
            report["review_panels"].append(_review_usage(panel))
        except (OSError, ValueError):
            report["review_panels"].append({"file": str(panel), "error": "unreadable"})
    report["totals"] = {
        "known_priced_yuan": report["pipeline_ledger"]["recorded_priced_yuan"],
        "estimated_yuan": report["pipeline_ledger"]["estimated_yuan_unpriced_models"],
        "unknown": [f"{tokens['unknown_calls']} pipeline calls without usage",
                    "experiment calls: tokens recorded, price unknown",
                    f"{sum(u['executions']['failed_attempts'] for u in report['experiments']['results_dirs'].values())}"
                    " failed experiment attempts: usage unknown",
                    f"{sum(p.get('unknown_calls', 0) for p in report['review_panels'])} review calls without usage"],
        "usage_quality": {"pipeline": "recorded per call (ledger)", "experiments": "recorded per item when the code "
                          "writes token fields, else unknown", "reviews": "recorded per call when the API returns it"},
        "price_quality": {"pipeline": "known for priced models, estimated for unpriced ones",
                          "experiments": "unknown", "reviews": "unknown"},
    }
    (run_dir / "spend_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
