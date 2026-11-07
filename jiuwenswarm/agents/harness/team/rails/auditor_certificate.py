# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Certify an auditor before its silence counts as evidence.

A review stage that finds nothing in a draft is evidence only if the reviewer
could have found something. This module measures that with no calibration data
and no model call of its own, by running the audit as a randomized trial. It is
the construction of the SciRigorBench paper, applied to the auditors this
harness runs.

1. Declare. The claim universe is fixed before anything is planted: every
   printed value the experiment stage registered, as slot -> printed literal.
   Slots whose numbers collide with another slot's, before or after
   perturbation, are set aside, so an accused number names exactly one slot
   (the collision-free universe U').
2. Plant. A seeded draw picks K slots of U' and moves each printed value by a
   relative delta, keeping its printed precision. The draw is the benchmark's
   own coin. The ledger of planted slots is committed with SHA-256 before the
   audit runs and never enters the auditor's view.
3. Audit. The auditor reads only the planted draft and names what it accuses.
4. Test. Under the sharp null that the auditor cannot tell planted values from
   genuine ones, the number of hits among A accusations is exactly
   hypergeometric (N, K, A). The round p-value is the upper tail. An auditor
   that accuses everything gets p = 1: volume earns nothing.
5. Accumulate. Round p-values are turned into e-values (e = 1 / (2 sqrt p)) and
   multiplied. The product is an e-process: by Ville's inequality the chance it
   ever reaches 1/alpha under the null is at most alpha, at whatever round one
   stops. Reaching 1/alpha certifies the auditor.
6. False accusations. Accusations on preserved slots, and every accusation on an
   unplanted round, feed an anytime-valid upper bound u0 on the probability
   that the auditor accuses a claim that carries its genuine value.

Pure standard library: it loads by path, with no framework installed, on Python 3.9 and later.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
from fractions import Fraction
from typing import Callable, Iterable


logger = logging.getLogger(__name__)
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
FA_LAMBDAS = tuple(2.0 ** k for k in range(-2, 7))   # fixed before any data is seen


def perturb_literal(literal: str, delta: float) -> str:
    """Move every number in a printed literal by the relative amount delta.

    Counts and effects are inflated; a p-value below 1e-3 is shrunk further.
    Precision is kept. When delta falls below the printed precision the value
    moves one unit in its last place instead, the smallest edit a reader could
    see. Every number in a composite literal ("0.81 [0.74, 0.88]") moves by the
    same factor, so an estimate and its interval stay consistent.
    """
    def repl(m: re.Match) -> str:
        tok = m.group(0)
        dp = len(tok.split(".")[1]) if "." in tok else 0
        v = float(tok)
        if v == 0:
            return tok
        nv = v * (1 - delta) if 0 < abs(v) < 1e-3 else v * (1 + delta)
        out = f"{nv:.{dp}f}" if dp else str(int(round(nv)))
        if out == tok:
            nv = v + (10 ** -dp if dp else 1) * (1 if v >= 0 else -1)
            out = f"{nv:.{dp}f}" if dp else str(int(round(nv)))
        return out

    return NUMBER.sub(repl, literal)


def collision_free(universe: dict[str, str], delta: float) -> list[str]:
    """Slots of U' (sorted): no number they print, genuine or planted, is printed by another slot.

    A collision makes an accused number ambiguous: if "12" is both a planted
    value and some other slot's genuine value, an accusation of "12" cannot be
    resolved to one slot, and the test would no longer be exact. A slot whose
    planting changes nothing ("0", "0.00") cannot carry a plant and is left out.
    """
    toks = {s: set(NUMBER.findall(v)) | set(NUMBER.findall(perturb_literal(v, delta)))
            for s, v in universe.items() if perturb_literal(v, delta) != v}
    return sorted(s for s, t in toks.items()
                  if t and not any(t & other_toks for other, other_toks in toks.items() if other != s))


def resolve_accusations(accused_numbers: Iterable[str], facts: dict[str, str],
                        slots: Iterable[str]) -> set[str]:
    """Map the numbers an auditor accuses to the slots of U' that print them."""
    accused = set(accused_numbers)
    return {s for s in slots if accused & set(NUMBER.findall(facts[s]))}


def commit(ledger: dict, salt: str) -> str:
    """SHA-256 commitment to the planting ledger, published before the audit."""
    blob = json.dumps(ledger, sort_keys=True, ensure_ascii=False) + salt
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def hypergeom_sf(x: int, n: int, k: int, a: int) -> Fraction:
    """Exact P(X >= x) for X ~ Hypergeometric(population n, k planted, a draws)."""
    if 0 in (a, k) or k >= n or x <= 0:
        return Fraction(1)
    total = math.comb(n, a)
    tail = sum(math.comb(k, i) * math.comb(n - k, a - i) for i in range(x, min(k, a) + 1))
    return Fraction(tail, total)


def round_p(n: int, planted: set[str], accused: set[str]) -> float:
    """Exact conditional p-value of one round: hits on planted slots given the accusation count."""
    return float(hypergeom_sf(len(planted & accused), n, len(planted), len(accused)))


def p_to_e(p: float) -> float:
    """Calibrator e = 1 / (2 sqrt p); E[e] <= 1 for any valid p-value."""
    return 0.5 * max(p, 1e-300) ** -0.5


def _log_mix(zsum: float, rounds: int, m: float) -> float:
    terms = []
    for lam in FA_LAMBDAS:
        c = 1 - math.exp(-lam)
        if m * c >= 1:
            return math.inf
        terms.append(-lam * zsum - rounds * math.log1p(-m * c))
    top = max(terms)
    return top + math.log(sum(math.exp(t - top) for t in terms) / len(terms))


def false_accusation_upper(rates: list[float], alpha: float = 0.05) -> float:
    """Anytime-valid upper confidence bound on the mean per-claim false-accusation probability.

    `rates[r]` is the share of round r's preserved slots that were accused. For
    each lambda, exp(-lambda * sum Z) * (1 - m c)^-R with c = 1 - exp(-lambda)
    is dominated by a nonnegative supermartingale when m is the mean accusation
    probability, under any dependence between rounds; the bound mixes a fixed
    lambda grid and inverts by bisection.
    """
    if not rates:
        return 1.0
    zsum, rounds = sum(rates), len(rates)
    threshold = math.log(1 / alpha)
    if _log_mix(zsum, rounds, 1.0) < threshold:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _log_mix(zsum, rounds, mid) < threshold else (lo, mid)
    return lo


class AuditRound:
    """One planted (or unplanted) round: what was planted, what was accused, the test result."""

    def __init__(self, index: int, planted: set[str], accused: set[str], n: int,
                 commitment: str, p: float) -> None:
        self.index = index
        self.planted = planted
        self.accused = accused
        self.n = n
        self.commitment = commitment
        self.p = p

    def to_record(self) -> dict:
        return {
            "round": self.index,
            "n": self.n,
            "k": len(self.planted),
            "accused": len(self.accused),
            "hits": len(self.planted & self.accused),
            "p": self.p,
            "ledger_sha256": self.commitment,
        }


class Certificate:
    """Outcome of certify(): the e-process path, rejections and the false-accusation bound."""

    def __init__(self, auditor: str, alpha: float, rounds: list[AuditRound],
                 null_rounds: list[AuditRound], universe: int, declared: int) -> None:
        self.auditor = auditor
        self.alpha = alpha
        self.rounds = rounds
        self.null_rounds = null_rounds
        self.universe = universe
        self.declared = declared
        path, e = [], 1.0
        for r in rounds:
            e *= p_to_e(r.p)
            path.append(e)
        self.e_path = path
        self.certified_at = next((i + 1 for i, v in enumerate(path) if v >= 1 / alpha), None)
        rates = [len(r.accused - r.planted) / (r.n - len(r.planted))
                 for r in rounds + null_rounds if r.n > len(r.planted)]
        self.u0 = false_accusation_upper(rates, alpha)

    @property
    def certified(self) -> bool:
        return self.certified_at is not None

    def to_record(self) -> dict:
        planted_hits = sum(len(r.planted & r.accused) for r in self.rounds)
        planted_total = sum(len(r.planted) for r in self.rounds)
        return {
            "auditor": self.auditor,
            "alpha": self.alpha,
            "declared_slots": self.declared,
            "collision_free_slots": self.universe,
            "rounds": len(self.rounds),
            "null_rounds": len(self.null_rounds),
            "rejections": sum(r.p <= self.alpha for r in self.rounds),
            "recall": planted_hits / planted_total if planted_total else None,
            "e_final": self.e_path[-1] if self.e_path else 1.0,
            "certified": self.certified,
            "certified_at_round": self.certified_at,
            "false_accusation_upper": self.u0,
            "round_log": [r.to_record() for r in self.rounds + self.null_rounds],
        }


def certify(
    universe: dict[str, str],
    render: Callable[[dict[str, str]], str],
    auditor: Callable[[str], Iterable[str]],
    *,
    name: str,
    rounds: int = 10,
    k: int = 2,
    delta: float = 0.3,
    null_rounds: int = 0,
    seed: int = 0,
    alpha: float = 0.05,
    on_round: Callable[[AuditRound], None] | None = None,
) -> Certificate:
    """Run `rounds` planted rounds and `null_rounds` unplanted ones against `auditor`.

    `universe` is slot -> printed literal, as registered by the experiment stage.
    `render(facts)` writes the draft the auditor will read from those values.
    `auditor(draft)` returns the numbers (printed tokens) it accuses; they are
    resolved to slots of U'. `on_round` receives each round after its test, so
    the caller can append it to the run log as it happens.

    The rounds are not independent of each other (same draft, same auditor);
    the e-process does not need them to be. Each round's p is exact given the
    past because the coin is fresh each round.
    """
    slots = collision_free(universe, delta)
    n = len(slots)
    if n <= k:
        raise ValueError(f"U' has {n} slots, too few to plant {k}")
    rng = random.Random(seed)
    salt = hashlib.sha256(f"{name}:{seed}".encode()).hexdigest()
    done: list[AuditRound] = []
    null: list[AuditRound] = []
    plan = [k] * rounds + [0] * null_rounds
    for i, kk in enumerate(plan):
        planted = set(rng.sample(slots, kk))
        facts = {s: (perturb_literal(v, delta) if s in planted else v) for s, v in universe.items()}
        commitment = commit({"round": i, "planted": sorted(planted)}, salt)
        accused = resolve_accusations(auditor(render(facts)), facts, slots)
        rnd = AuditRound(i, planted, accused, n, commitment, round_p(n, planted, accused))
        (done if kk else null).append(rnd)
        if on_round is not None:
            on_round(rnd)
    return Certificate(name, alpha, done, null, n, len(universe))


if __name__ == "__main__":
    # Self-check; exits non-zero if any property fails. Exactness: an auditor
    # that accuses at random is rejected at most alpha of the time. Volume:
    # accusing everything gives p = 1.
    U = {f"s{i}": f"{10 + 7 * i}.{i}" for i in range(20)}
    rng = random.Random(1)

    def render(facts):
        return " ".join(facts.values())

    def blind(text):
        return rng.sample(NUMBER.findall(text), 3)

    rejected = sum(r.p <= 0.05 for r in certify(U, render, blind, name="blind", rounds=2000, seed=2).rounds)
    everything = certify(U, render, NUMBER.findall, name="all", rounds=5)
    genuine = set(NUMBER.findall(render(U)))
    oracle = certify(U, render, lambda t: set(NUMBER.findall(t)) - genuine, name="oracle", rounds=3, null_rounds=20)
    checks = {
        "a random auditor is rejected at most alpha of the time":
            rejected / 2000 <= 0.05 + 3 * math.sqrt(0.05 * 0.95 / 2000),
        "accusing everything gives p = 1 and no certificate":
            all(r.p == 1.0 for r in everything.rounds) and not everything.certified,
        "an oracle auditor is certified with a small bound": oracle.certified and oracle.u0 < 0.15,
        "the hypergeometric tail is exact": hypergeom_sf(2, 20, 2, 2) == Fraction(1, 190),
    }
    failed = [name for name, held in checks.items() if not held]
    if failed:
        raise SystemExit(f"self-check failed: {failed}")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("self-check OK")
