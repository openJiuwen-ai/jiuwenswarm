"""D19 tier 3a: the predicate rail -- an explicit instruction's mechanizable
projection is checked before the write, over data already in hand."""

from __future__ import annotations

from jiuwenswarm.extensions.co_scribe.backend.toolkit.rails.result_predicates import (
    check_result_predicates,
    implied_predicates,
)


def test_shorten_refuses_a_longer_rewrite():
    """The founding example: 「剪短」 answered with a longer sentence used to land as
    applied, and only a person reading the diff could catch it."""
    v = check_result_predicates("把这句剪短", [("短句子。", "这是一个反而变得更长的句子。")])
    assert v and "不短于原文" in v


def test_shorten_passes_a_shorter_rewrite():
    assert check_result_predicates("把这句剪短", [("一个相当长的句子。", "短句。")]) is None


def test_shorten_skips_the_region_form():
    """The region form carries no before-image at the rail; a predicate that needs
    old must skip, never guess."""
    assert check_result_predicates("剪短", [(None, "任意长的内容也不判")]) is None


def test_emptied_refuses_leftover_content():
    v = check_result_predicates("把这格清空", [("原值", "还有字")])
    assert v and "非空" in v
    assert check_result_predicates("把这格清空", [("原值", "")]) is None


def test_removed_target_must_be_absent():
    v = check_result_predicates("删掉「草稿」这个词", [("初版草稿说明", "初版草稿说明 v2")])
    assert v and "草稿" in v
    assert check_result_predicates("删掉「草稿」这个词", [("初版草稿说明", "初版说明")]) is None


def test_became_target_must_be_present():
    v = check_result_predicates("改成「Q3 预算」", [("我的预算", "我的开销")])
    assert v and "Q3 预算" in v
    assert check_result_predicates("改成「Q3 预算」", [("我的预算", "Q3 预算")]) is None


def test_became_works_on_the_region_form():
    """Presence needs no before-image, so the region form is checkable."""
    v = check_result_predicates("改成「Q3 预算」", [(None, "别的东西")])
    assert v is not None
    assert check_result_predicates("改成「Q3 预算」", [(None, "Q3 预算")]) is None


def test_an_unrecognised_instruction_implies_nothing():
    """Conservatism is the rail's licence to exist: a verb outside the lexicon must
    yield no predicate, or the rail manufactures refusals from its own misreadings."""
    assert implied_predicates("这段话优化一下，语气正式些") == []
    assert check_result_predicates("润色", [("a", "bbbb")]) is None


def test_compound_instructions_are_held_to_every_predicate():
    v = check_result_predicates(
        "删掉「废话」，其余剪短", [("一句带废话的长长长长句", "废话没删净")]
    )
    assert v and "废话" in v, v
