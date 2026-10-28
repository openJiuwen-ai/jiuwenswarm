"""Jev's runtime helper retains normal host browser permission routing."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import deterministic_domain_route
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import build_tool_decision_facts


def facts(name, args, root):
    return build_tool_decision_facts(name, args, workspace_root=root, original_args_were_valid_object=True)


@pytest.mark.parametrize("url", ["https://example.test/", "https://127.0.0.1/", "http://example.test/"])
def test_jev_navigation_has_the_same_network_and_review_boundaries_as_native_navigation(tmp_path, url):
    profile = SimpleNamespace(network_guard_enforced=True)
    native = deterministic_domain_route(facts("browser_navigate", {"url": url}, tmp_path),
                                        original_user_intent=None, browser_runtime_security_profile=profile)
    helper = deterministic_domain_route(facts("browser_page_action", {
        "generation_id": "g1", "op": "navigate", "url": url,
    }, tmp_path), original_user_intent=None, browser_runtime_security_profile=profile)
    assert helper == native
    assert not helper.is_deterministic_allow


def test_bounded_scroll_still_requires_normal_interaction_review(tmp_path):
    route = deterministic_domain_route(facts("browser_page_action", {
        "generation_id": "g1", "op": "scroll", "direction": "down",
    }, tmp_path), original_user_intent=None)
    assert route.reason == "domain_policy_browser_interactive"
    assert route.requires_reviewer and not route.is_deterministic_allow


def test_phase_planning_and_verification_are_fixed_runtime_observations(tmp_path):
    from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import classify_tool

    capability = classify_tool("browser_phase")
    probe = classify_tool("browser_probe_interactives")
    assert capability.category == probe.category
    assert capability.static_side_effects == probe.static_side_effects


@pytest.mark.parametrize("op,extra", [
    ("read_text", {}), ("find", {"query": "observed value"}), ("snapshot", {}),
    ("tabs", {}), ("wait", {"ms": 500}),
])
def test_closed_local_readers_retain_native_snapshot_permission_review(tmp_path, op, extra):
    native = deterministic_domain_route(facts("browser_snapshot", {}, tmp_path), original_user_intent=None)
    helper = deterministic_domain_route(facts("browser_page_action", {
        "generation_id": "g1", "op": op, **extra,
    }, tmp_path), original_user_intent=None)
    assert helper == native
    assert helper.reason == "domain_policy_browser_readonly"
    assert helper.requires_reviewer and not helper.is_deterministic_allow


def test_new_helper_operations_do_not_make_arbitrary_evaluate_a_fixed_read(tmp_path):
    route = deterministic_domain_route(facts("browser_page_action", {
        "generation_id": "g1", "op": "evaluate", "script": "untrusted code",
    }, tmp_path), original_user_intent=None)
    assert route.reason == "domain_policy_browser_unknown_payload" and not route.is_deterministic_allow
