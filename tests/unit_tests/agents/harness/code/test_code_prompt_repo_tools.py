# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The Code prompt routes navigation to the repo-aware tools and forbids answer-hunting."""

from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
    _code_session_guidance_prompt,
    _code_using_your_tools_prompt,
    build_code_system_prompt,
)

# Upper bound for what this change may add to the system prompt (chars ≈ tokens × 4).
_MAX_ADDED_CHARS = 1000


def test_prompt_routes_to_repo_tools_and_keeps_fallback():
    text = build_code_system_prompt()
    assert "When code_search, find_symbol, or symbol_outline are registered, start with them" in text
    assert "symbol_outline lists a file's symbols with line numbers" in text
    assert "Then read_file only the range you need (offset/limit)." in text
    assert "Fall back to grep/glob when those tools are not available." in text
    assert "prefer code_search over grep/rg pipelines" in text
    # The pre-existing grep/glob routing stays verbatim (covered by convergence tests too).
    assert "call grep or glob directly." in text


def test_prompt_forbids_history_and_network_answer_hunting():
    text = build_code_system_prompt()
    assert "Do not mine git history (git log, git show, git blame, other branches)" in text
    assert "or the network (GitHub, package indexes, docs sites)" in text
    # Scoped to answer-hunting: legitimate git use and explicit user requests stay allowed.
    assert "git status/diff are fine" in text
    assert "when the user explicitly asks for history" in text


def test_added_guidance_is_bounded():
    guidance = _code_session_guidance_prompt().content["en"]
    tools = _code_using_your_tools_prompt().content["en"]
    added = [
        line
        for line in (guidance + tools).splitlines()
        if "code_search" in line or "git history" in line or "own reasoning" in line
    ]
    assert added, "guidance lines missing"
    assert sum(len(line) for line in added) <= _MAX_ADDED_CHARS


def test_session_guidance_keeps_the_no_answer_hunting_rule() -> None:
    """Every prompt profile carries the rule against mining history/network for a solution."""
    from jiuwenswarm.agents.harness.code.prompt import code_prompt_builder as builder

    rule = builder._NO_ANSWER_HUNTING_RULE.strip()
    profiles = ["default"]
    if "profile" in builder.build_code_system_prompt.__code__.co_varnames:
        profiles.append("lean")
    for profile in profiles:
        kwargs = {"profile": profile} if profile != "default" else {}
        assert rule in builder.build_code_system_prompt(**kwargs), profile
