# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.agents.harness.code.prompt.code_prompt_builder import (
    build_code_system_prompt,
)


def test_code_prompt_contains_cc_task_convergence_wording_verbatim():
    text = build_code_system_prompt()

    assert (
        "The user will primarily request you to perform software engineering tasks. "
        "These may include solving bugs, adding new functionality, refactoring code, "
        "explaining code, and more. When given an unclear or generic instruction, "
        "consider it in the context of these software engineering tasks and the current "
        'working directory. For example, if the user asks you to change "methodName" '
        'to snake case, do not reply with just "method_name", instead find the method '
        "in the code and modify the code."
    ) in text
    assert (
        "If an approach fails, diagnose why before switching tactics—read the error, "
        "check your assumptions, try a focused fix. Don't retry the identical action "
        "blindly, but don't abandon a viable approach after a single failure either. "
        "Escalate to the user with ask_user only when you're genuinely stuck "
        "after investigation, not as a first response to friction."
    ) in text


def test_code_prompt_contains_cc_harness_wording_verbatim():
    text = build_code_system_prompt()

    assert (
        "Prefer the dedicated file/search tools over shell commands when one "
        "fits. Independent tool calls can run in parallel in one response."
    ) in text
    assert text.count(
        "Reference code as `file_path:line_number` — it's clickable."
    ) == 1
    assert "include the pattern file_path:line_number" not in text
    assert (
        "Write code that reads like the surrounding code: match its comment "
        "density, naming, and idiom."
    ) in text


def test_code_prompt_contains_single_cc_style_and_reporting_rules():
    text = build_code_system_prompt()

    assert text.count(
        "Write code that reads like the surrounding code: match its comment "
        "density, naming, and idiom."
    ) == 1
    assert text.count("Report outcomes faithfully: if tests fail") == 1


def test_code_prompt_contains_cc_actions_with_care_wording_verbatim():
    text = build_code_system_prompt()

    assert (
        "For actions that are hard to reverse or outward-facing, confirm first "
        "unless durably authorized or explicitly told to proceed without asking; "
        "approval in one context doesn't extend to the next. Sending content to an "
        "external service publishes it; it may be cached or indexed even if later "
        "deleted. Before deleting or overwriting, look at the target. If what you "
        "find contradicts how it was described, or you didn't create it, surface "
        "that instead of proceeding. Report outcomes faithfully: if tests fail, "
        "say so with the output; if a step was skipped, say that; when something is "
        "done and verified, state it plainly without hedging."
    ) in text


def test_code_prompt_does_not_discourage_compatibility():
    text = build_code_system_prompt()

    assert "backwards-compatibility shims" not in text
    assert "Avoid backwards-compatibility hacks" not in text


def test_code_prompt_contains_cc_search_routing_wording_verbatim():
    text = build_code_system_prompt()

    assert (
        "For narrow, targeted lookups in the codebase "
        "(say, a particular file, class, or function), "
        "call grep or glob directly."
    ) in text
    assert (
        "For wider exploration or deep research across the codebase, "
        'use subagent_spawn with subagent_type="explore_agent", '
        "then subagent_wait in the same turn. It is slower than calling grep/glob "
        "yourself, so reserve it for when a narrow, targeted search turns out to be "
        "insufficient or when the task will plainly need more than three queries."
    ) in text


def test_code_prompt_contains_cc_output_efficiency_wording_verbatim():
    text = build_code_system_prompt()

    assert (
        "IMPORTANT: Go straight to the point. Try the simplest approach first without "
        "going in circles. Do not overdo it. Be extra concise."
    ) in text


def test_code_prompt_does_not_cap_final_response_length():
    text = build_code_system_prompt()

    assert "End-of-turn summary: one or two sentences." not in text
    assert "What changed and what's next. Nothing else." not in text
    assert (
        "End with a concise, self-contained response containing everything "
        "the user needs for the task."
    ) in text
    assert "Match responses to the task" in text


def test_code_prompt_bounds_pre_change_exploration_and_verification():
    text = build_code_system_prompt()

    assert (
        "- Before making changes, inspect the relevant implementation and, when "
        "applicable, its interfaces, configuration, and nearby tests. Prefer "
        "repository evidence over assumptions, and verify the behavior and "
        "signatures of any APIs the change relies on. Avoid unrelated exploration "
        "or external research unless local evidence is insufficient.\n"
        "Stop exploring once you know what must change, where and why it must "
        "change, and how the result will be verified. Then make the surgical "
        "change that fully satisfies the request.\n"
        "Run focused verification first, followed by broader relevant checks when "
        "the risk justifies them. If verification fails or contradicts an "
        "assumption, investigate using that evidence and revise the change."
    ) in text


def test_code_prompt_excludes_non_cc_convergence_rules():
    text = build_code_system_prompt()

    assert 'subagent_type="browser_agent"' not in text
    assert "Do not write Playwright scripts" not in text
    assert "Extra exploration rounds before coding" not in text
    assert '"very thorough" for comprehensive analysis' not in text
    assert "Don't implement until the user agrees" not in text
    assert (
        "it does NOT limit your tool call count or codebase exploration depth"
        not in text
    )
    assert "keep working until you have made the requested change" not in text
    assert "stop broad exploration and make the smallest focused edit" not in text
    assert "This does not apply when the user asks you to fix" not in text
