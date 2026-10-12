# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verification loop for Code mode: test discovery, focused test runs, gate evidence.

* :mod:`.discovery` — which frameworks a workspace uses (cached, file probes only);
* :mod:`.mapping`   — changed source files → likely test targets;
* :mod:`.runner`    — run one framework with a timeout, parse, spill, summarise;
* :mod:`.tool`      — the model-facing ``run_tests`` tool;
* :mod:`.evidence`  — turn a ``run_tests`` result into gate evidence for
  ``ImplementCompletenessRail``.

Design: docs/en/VerificationLoop.md · docs/zh/验证闭环.md.
"""

from jiuwenswarm.agents.harness.code.verification.discovery import (
    Discovery,
    Framework,
    clear_discovery_cache,
    discover_tests,
)
from jiuwenswarm.agents.harness.code.verification.evidence import (
    RunTestsEvidence,
    parse_run_tests_evidence,
)
from jiuwenswarm.agents.harness.code.verification.mapping import map_files_to_tests
from jiuwenswarm.agents.harness.code.verification.tool import (
    TOOL_NAME,
    RunTestsTool,
    build_run_tests_card,
)

__all__ = [
    "TOOL_NAME",
    "Discovery",
    "Framework",
    "RunTestsEvidence",
    "RunTestsTool",
    "build_run_tests_card",
    "clear_discovery_cache",
    "discover_tests",
    "map_files_to_tests",
    "parse_run_tests_evidence",
]
