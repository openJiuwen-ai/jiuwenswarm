# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Paper-guard rails: research-paper quality constraints for swarm members.

Provides two config-driven rails for automated research paper generation
scenarios (e.g. the openJiuwen paper-agent pipeline):

- ``HallucinationCheckRail`` — quantitative claims in model output must be
  traceable to a ground-truth results file; unmatched numbers trigger an
  injected warning so downstream stages drop or fix them.
- ``AcademicFormatRail`` — enforces complete paper sections (abstract /
  introduction / method / experiment / conclusion / references) on outputs
  that look like a paper draft.

Mounted per-member via the ``swarm.paper_guard`` provider
(``agents/swarm/providers/member_rails.py``), gated on
``paper_guard.enabled`` in ``config.yaml``.
"""

from jiuwenswarm.agents.harness.common.rails.paper_guard.academic_format_rail import (
    AcademicFormatRail,
)
from jiuwenswarm.agents.harness.common.rails.paper_guard.hallucination_check_rail import (
    HallucinationCheckRail,
)

__all__ = ["HallucinationCheckRail", "AcademicFormatRail"]