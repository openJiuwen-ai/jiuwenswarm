# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An auditor is certified only when it finds planted values beyond chance."""

import math
import random
from fractions import Fraction

from jiuwenswarm.agents.harness.team.rails.auditor_certificate import (
    NUMBER,
    certify,
    collision_free,
    commit,
    false_accusation_upper,
    hypergeom_sf,
    perturb_literal,
    round_p,
)

UNIVERSE = {f"s{i}": f"{10 + 7 * i}.{i}" for i in range(20)}


def render(facts):
    return " ".join(facts.values())


GENUINE = set(NUMBER.findall(render(UNIVERSE)))


def oracle(text):
    return set(NUMBER.findall(text)) - GENUINE


def test_hypergeometric_tail_is_exact():
    assert hypergeom_sf(2, 20, 2, 2) == Fraction(1, 190)
    assert hypergeom_sf(1, 20, 2, 1) == Fraction(2, 20)
    assert hypergeom_sf(0, 20, 2, 5) == 1


def test_accusing_everything_earns_nothing():
    planted = {"s1", "s2"}
    assert round_p(20, planted, set(UNIVERSE)) == 1.0
    cert = certify(UNIVERSE, render, NUMBER.findall, name="all", rounds=10)
    assert not cert.certified and cert.to_record()["rejections"] == 0


def test_a_blind_auditor_is_rejected_at_most_alpha_of_the_time():
    rng = random.Random(7)
    blind = certify(UNIVERSE, render, lambda t: rng.sample(NUMBER.findall(t), 3),
                    name="blind", rounds=3000, seed=11)
    rate = sum(r.p <= 0.05 for r in blind.rounds) / 3000
    assert rate <= 0.05 + 3 * math.sqrt(0.05 * 0.95 / 3000)


def test_a_detecting_auditor_is_certified_and_its_bound_is_small():
    cert = certify(UNIVERSE, render, oracle, name="oracle", rounds=4, null_rounds=30)
    rec = cert.to_record()
    assert cert.certified and rec["certified_at_round"] <= 2
    assert rec["recall"] == 1.0 and rec["false_accusation_upper"] < 0.1


def test_an_auditor_that_also_accuses_genuine_values_has_a_large_bound():
    def noisy(text):
        return oracle(text) | set(NUMBER.findall(text)[:5])

    rec = certify(UNIVERSE, render, noisy, name="noisy", rounds=4, null_rounds=30).to_record()
    assert rec["false_accusation_upper"] > 0.2


def test_colliding_slots_are_left_out_of_the_tested_universe():
    universe = {"a": "12", "b": "12", "c": "0.40", "d": "7.5"}
    assert collision_free(universe, 0.3) == ["c", "d"]
    # 10 * 1.3 = 13 collides with "13" printed elsewhere
    assert "x" not in collision_free({"x": "10", "y": "13", "z": "2.25"}, 0.3)


def test_perturbation_keeps_precision_and_moves_below_precision_edits_one_unit():
    assert perturb_literal("0.81 [0.74, 0.88]", 0.1) == "0.89 [0.81, 0.97]"
    assert perturb_literal("100", 0.001) == "101"
    assert perturb_literal("0.0004", 0.3) == "0.0003"


def test_rounds_are_seeded_and_committed_before_the_audit():
    a = certify(UNIVERSE, render, oracle, name="o", rounds=3, seed=5).to_record()
    b = certify(UNIVERSE, render, oracle, name="o", rounds=3, seed=5).to_record()
    assert a["round_log"] == b["round_log"]
    assert len({r["ledger_sha256"] for r in a["round_log"]}) == 3
    assert commit({"round": 0, "planted": ["s1"]}, "salt") != commit({"round": 0, "planted": ["s2"]}, "salt")


def test_false_accusation_bound_is_anytime_valid_in_shape():
    assert false_accusation_upper([]) == 1.0
    assert false_accusation_upper([0.0] * 200) < 0.05
    rates = [0.1] * 100
    assert false_accusation_upper(rates) >= 0.1
