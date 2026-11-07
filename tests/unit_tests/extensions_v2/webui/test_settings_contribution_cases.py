# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentServer settings declarations against the cases shared with the Gateway (S_18)."""

import json
from pathlib import Path

import pytest

from jiuwenswarm.extensions_v2.webui.settings import parse_settings

CASES = json.loads((Path(__file__).parents[4] / "tests/fixtures/settings_contribution_cases.json").read_text("utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_agentserver_declarations_follow_the_shared_cases(case):
    if case["agentserver"]:
        parse_settings(case["declaration"])
    else:
        with pytest.raises(ValueError):
            parse_settings(case["declaration"])
