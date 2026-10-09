# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Independent reviews and an area chair's meta-review of a frozen draft, turned into revision tasks.

The review topology is AgentReview's (Jin et al., EMNLP 2024; Apache-2.0):
reviewers review the same draft independently, then an area chair reads their
reviews and writes the meta-review. Here the meta-review ends in at most five
revision tasks the writer can carry out from the evidence it already has; a
task that would need new data, new experiments or new numbers is out of
scope, because every number the paper prints must already be in the verified
registry.

The reviewers' roles follow the research harness polish engine: methods and
statistics, the field, and presentation. Each scores the draft on the ICLR
form. The scores are recorded as the reviewers gave them; they are model
judgments of a draft, not a prediction of any venue's decision.
"""

from __future__ import annotations

import json
import re

ROLES = {
    "methods": "a methods and statistics reviewer: is the design able to answer the question, are the "
               "statistics right and reported with uncertainty, are the limitations the ones the design has",
    "field": "a reviewer from the paper's field: is the question framed the way the field frames it, is the "
             "evidence read correctly, are the claims proportionate to it",
    "presentation": "a presentation reviewer: is the argument easy to follow, does each section do one job, "
                    "are the figures referred to and explained, is anything repeated or missing",
}

REVIEW_SYSTEM = """You are {role}. Review the paper below for ICLR. Answer with one JSON object:
{{"summary": "two sentences", "strengths": ["..."], "weaknesses": ["specific, with where in the paper"],
"questions": ["..."], "soundness": 1-4, "presentation": 1-4, "contribution": 1-4, "rating": 1-10,
"confidence": 1-5}}"""

META_SYSTEM = """You are the area chair. Read the reviews of the paper and write the meta-review. Then list
at most five revision tasks, most important first, that the authors can carry out by rewriting alone: no new
data, no new experiments, no new numbers, no new citations. Drop any reviewer request that would need one.
The methods section already states everything the study did, so a request to add a definition, criterion,
rule or check to the methods as if the study had applied it needs a new study: drop it too.
Answer with one JSON object: {"meta_review": "one paragraph", "tasks": ["imperative, specific, where in the paper"],
"out_of_scope": ["requests dropped because they need new evidence"]}"""


def _json(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        obj = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        obj = {}
    return obj if isinstance(obj, dict) else {}


def review(paper: str, gateway, chair: bool = True) -> dict:
    """Three independent reviews and, with the chair, the meta-review with its revision tasks.

    Without the chair it is a re-review: the same reviewers rate a revision, so a
    revision can be held to not reading worse than the draft it replaces.
    """
    reviews = {name: _json(gateway.chat(f"review_{name}", REVIEW_SYSTEM.format(role=role), paper))
               for name, role in ROLES.items()}
    ratings = [r.get("rating") for r in reviews.values() if isinstance(r.get("rating"), (int, float))]
    mean = round(sum(ratings) / len(ratings), 2) if ratings else None
    if not chair:
        return {"reviews": reviews, "mean_rating": mean}
    listing = "\n\n".join(f"Review by the {name} reviewer:\n{json.dumps(r, ensure_ascii=False)}"
                          for name, r in reviews.items())
    meta = _json(gateway.chat("review_meta", META_SYSTEM, f"Paper:\n{paper}\n\n{listing}"))
    return {"reviews": reviews, "meta": meta, "tasks": [str(t) for t in meta.get("tasks") or []][:5],
            "mean_rating": mean}
