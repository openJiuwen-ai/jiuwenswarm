# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
    build_code_system_prompt,
)
from jiuwenswarm.server.runtime.agent_adapter.code_graph_flags import (
    AGENT_CODE,
    AGENT_ROOT,
    PROFILE_GRAPH,
    PROFILE_OFF,
    CodeGraphFlags,
)


def test_default_code_prompt_still_teaches_grep():
    text = build_code_system_prompt()
    assert "call grep or glob directly" in text
    assert "use grep instead of the bash grep command" in text
    assert "resolve_symbol" not in text
    assert "search_source_text" not in text


def test_off_profile_matches_default():
    assert build_code_system_prompt(profile="off") == build_code_system_prompt()
    assert build_code_system_prompt(profile="unknown") == build_code_system_prompt()


def test_graph_profile_teaches_code_graph_not_grep():
    text = build_code_system_prompt(profile="graph")
    assert "call grep or glob directly" not in text
    assert "use grep instead of the bash grep command" not in text
    assert "resolve_symbol" in text
    assert "find_code_symbols" in text
    assert "search_source_text" in text
    assert "grep and glob are not available" in text
    assert "before bash or read_file" in text
    assert "explore_agent" in text
    assert "## Task planning (todos)" in text


def test_root_prompt_profile_follows_mount_point():
    assert (
        CodeGraphFlags(profile=PROFILE_GRAPH, agent=AGENT_ROOT).root_prompt_profile
        == PROFILE_GRAPH
    )
    assert (
        CodeGraphFlags(profile=PROFILE_GRAPH, agent=AGENT_CODE).root_prompt_profile
        == PROFILE_OFF
    )
    assert (
        CodeGraphFlags(profile=PROFILE_OFF, agent=AGENT_ROOT).root_prompt_profile
        == PROFILE_OFF
    )
