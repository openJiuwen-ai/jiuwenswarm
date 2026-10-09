# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Review ensemble: independent reviews, a meta-review, at most five rewrite-only tasks. Offline."""

import json

from jiuwenswarm.agents.harness.common.research import review_ensemble as rev


class _Gateway:
    def __init__(self):
        self.calls = []

    def chat(self, step, system, user):
        self.calls.append((step, user))
        if step == "review_meta":
            return json.dumps({"meta_review": "Sound but terse.", "tasks": [f"task {i}" for i in range(7)],
                               "out_of_scope": ["run a new survey"]})
        return "Here is my review: " + json.dumps({"rating": {"review_methods": 6, "review_field": 5}.get(step, 4)})


def test_each_reviewer_reads_only_the_paper_and_the_chair_reads_all_reviews():
    gateway = _Gateway()
    out = rev.review("PAPER TEXT", gateway)
    assert [step for step, _ in gateway.calls] == ["review_methods", "review_field", "review_presentation",
                                                   "review_meta"]
    assert all(user == "PAPER TEXT" for step, user in gateway.calls[:3])
    assert gateway.calls[3][1].count("Review by the") == 3
    assert out["tasks"] == [f"task {i}" for i in range(5)] and out["mean_rating"] == 5.0


def test_a_re_review_asks_the_same_reviewers_and_no_chair():
    gateway = _Gateway()
    out = rev.review("REVISED TEXT", gateway, chair=False)
    assert [step for step, _ in gateway.calls] == ["review_methods", "review_field", "review_presentation"]
    assert out == {"reviews": out["reviews"], "mean_rating": 5.0}
