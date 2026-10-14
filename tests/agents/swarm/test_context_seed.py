# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Seed recovery and member derivation must not share request-owned state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.swarm.context import (
    SwarmBuildContext,
    set_heartbeat_job_service,
)
from jiuwenswarm.agents.swarm.context_seed import snapshot_swarm_seed


def _restore(seed, **handles):
    return SwarmBuildContext.from_seed(
        seed,
        config=handles.get("config"),
        trajectory_span_processor=handles.get("processor"),
    )


def _context():
    return SwarmBuildContext(
        session_id="session-1",
        request_id="request-1",
        user_id="owner-1",
        channel_id="web",
        channel="web",
        mode="code.team",
        project_dir="/project",
        team_id="team-1",
        team_ws_root="/team",
        task_workspace_root="/team/task",
        team_outputs_dir="/team/task/output",
        team_skill_visibility_path="/team/skills-visibility.json",
        global_skills_dir="/skills",
        trusted_dirs=["/project", "/shared"],
        request_metadata={"cron": True, "routing": {"tags": ["original"]}},
    )


def test_seed_round_trip_preserves_all_serializable_fields():
    context = _context()
    seed = json.loads(json.dumps(context.to_seed()))

    restored = _restore(seed)

    assert restored.to_seed() == seed
    assert restored.extras["is_cron"] is True
    assert restored.request_metadata is not seed["request_metadata"]
    assert restored.trusted_dirs is not seed["trusted_dirs"]


def test_exported_seed_is_an_independent_snapshot():
    context = _context()
    seed = context.to_seed()
    seed["request_metadata"]["routing"]["tags"].append("changed")
    seed["trusted_dirs"].append("/other")

    assert context.request_metadata["routing"]["tags"] == ["original"]
    assert context.trusted_dirs == ["/project", "/shared"]


def test_context_updates_do_not_change_an_already_exported_seed():
    context = _context()
    seed = context.to_seed()
    context.request_metadata["routing"]["tags"].clear()
    context.trusted_dirs.clear()

    assert seed["request_metadata"]["routing"]["tags"] == ["original"]
    assert seed["trusted_dirs"] == ["/project", "/shared"]


def test_recovered_contexts_do_not_mutate_each_other_or_the_seed():
    seed = _context().to_seed()
    first = _restore(seed)
    second = _restore(seed)
    first.request_metadata["routing"]["tags"].append("first-only")
    first.trusted_dirs.append("/first-only")

    assert second.request_metadata["routing"]["tags"] == ["original"]
    assert seed["request_metadata"]["routing"]["tags"] == ["original"]
    assert second.trusted_dirs == ["/project", "/shared"]
    assert seed["trusted_dirs"] == ["/project", "/shared"]


def test_constructor_takes_ownership_of_request_containers():
    metadata = {"nested": {"items": ["original"]}}
    trusted_dirs = ["/project"]
    extras = {"handle": object()}
    context = SwarmBuildContext(
        request_metadata=metadata,
        trusted_dirs=trusted_dirs,
        extras=extras,
    )
    metadata["nested"]["items"].append("caller")
    trusted_dirs.append("/caller")
    extras["other"] = "caller"

    assert context.request_metadata == {"nested": {"items": ["original"]}}
    assert context.trusted_dirs == ["/project"]
    assert set(context.extras) == {"handle"}
    assert context.extras["handle"] is extras["handle"]


def test_dataclass_member_views_isolate_slots_but_preserve_live_handles():
    handle = object()
    parent = _context()
    parent.extras["model"] = handle
    first = parent.derive(member_name="first")
    second = parent.derive(member_name="second")
    first.extras["model"] = object()
    first.extras["member-rail"] = object()
    first.request_metadata["routing"]["tags"].append("first")
    first.trusted_dirs.clear()

    assert parent.extras["model"] is handle
    assert second.extras["model"] is handle
    assert "member-rail" not in parent.extras
    assert "member-rail" not in second.extras
    assert second.request_metadata["routing"]["tags"] == ["original"]
    assert parent.trusted_dirs == second.trusted_dirs == ["/project", "/shared"]


def test_explicit_non_cron_metadata_clears_inherited_cron_signal():
    parent = SwarmBuildContext(request_metadata={"cron": True})

    child = parent.derive(request_metadata={"cron": False})

    assert child.extras["is_cron"] is False
    assert parent.extras["is_cron"] is True


def test_runtime_handles_are_sourced_locally_and_never_serialized():
    config, processor, heartbeat = {"local": True}, object(), object()
    context = _context()
    context.config = config
    context.trajectory_span_processor = processor
    context.heartbeat_job_service = heartbeat
    context.skill_retrieval_toolkit = object()
    context.extras["model"] = object()
    seed = context.to_seed()
    for key in (
        "config",
        "trajectory_span_processor",
        "heartbeat_job_service",
        "skill_retrieval_toolkit",
        "extras",
    ):
        assert key not in seed
    seed.update(config={"injected": True}, trajectory_span_processor="not-local")
    set_heartbeat_job_service(heartbeat)
    try:
        restored = _restore(seed, config=config, processor=processor)
        assert restored.config is config
        assert restored.trajectory_span_processor is processor
        assert restored.heartbeat_job_service is heartbeat
        assert restored.skill_retrieval_toolkit is None
    finally:
        set_heartbeat_job_service(None)


def test_legacy_empty_seed_uses_context_defaults():
    restored = _restore({})

    assert restored.session_id == ""
    assert restored.team_id == ""
    assert restored.channel == "default"
    assert restored.mode == "team"
    assert restored.request_metadata is None
    assert restored.trusted_dirs is None
    assert restored.disable_teammate_worktree is False


def test_unknown_seed_fields_are_ignored_for_forward_compatibility():
    seed = {"session_id": "s1", "future_setting": object(), "team_skills_dir": "/old"}

    restored = _restore(seed)

    assert restored.session_id == "s1"
    assert "future_setting" not in restored.to_seed()
    assert "team_skills_dir" not in restored.to_seed()


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_worktree_flag_does_not_coerce_non_booleans(value):
    with pytest.raises(ValueError, match="disable_teammate_worktree must be a boolean"):
        _restore({"disable_teammate_worktree": value})


@pytest.mark.parametrize("value", [True, False])
def test_worktree_flag_preserves_real_booleans(value):
    assert (
        _restore({"disable_teammate_worktree": value}).disable_teammate_worktree
        is value
    )


@pytest.mark.parametrize(
    "field",
    [
        "session_id",
        "request_id",
        "user_id",
        "channel_id",
        "channel",
        "mode",
        "project_dir",
        "team_id",
        "team_ws_root",
        "task_workspace_root",
        "team_outputs_dir",
        "team_skill_visibility_path",
        "global_skills_dir",
    ],
)
@pytest.mark.parametrize("value", [1, False, [], {}, Path("relative")])
def test_known_text_fields_reject_wrong_types(field, value):
    with pytest.raises(ValueError, match=field):
        _restore({field: value})


@pytest.mark.parametrize("field", ["session_id", "team_id"])
def test_explicit_null_identity_is_not_a_valid_legacy_default(field):
    with pytest.raises(ValueError, match=field):
        _restore({field: None})


@pytest.mark.parametrize("value", ["/project", [1], [None], ("/project",), {}])
def test_trusted_directories_require_a_string_list(value):
    with pytest.raises(ValueError, match="trusted_dirs"):
        _restore({"trusted_dirs": value})


@pytest.mark.parametrize("value", [[], "cron", False, 1])
def test_request_metadata_requires_an_object(value):
    with pytest.raises(ValueError, match="request_metadata must be an object"):
        _restore({"request_metadata": value})


@pytest.mark.parametrize(
    "value", [object(), Path("path"), {"set"}, (1, 2), float("inf"), float("nan")]
)
def test_nested_runtime_values_are_rejected_at_seed_boundary(value):
    with pytest.raises(
        ValueError, match="request_metadata must contain only JSON values"
    ):
        snapshot_swarm_seed({"request_metadata": {"nested": [value]}})


def test_metadata_object_keys_are_not_silently_coerced():
    with pytest.raises(ValueError, match="string object keys"):
        snapshot_swarm_seed({"request_metadata": {"nested": {1: "value"}}})


def test_cyclic_metadata_has_a_clear_validation_error():
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="cyclic or too deeply nested"):
        snapshot_swarm_seed({"request_metadata": cyclic})


def test_all_json_value_types_round_trip_without_coercion():
    metadata = {"items": [None, True, False, 0, -1, 1.25, "中文", {}, []]}

    seed = snapshot_swarm_seed({"request_metadata": metadata})

    assert seed == {"request_metadata": metadata}
    assert seed["request_metadata"] is not metadata
    assert seed["request_metadata"]["items"] is not metadata["items"]
    assert json.loads(json.dumps(seed)) == seed


@pytest.mark.parametrize("value", [None, [], "seed", 1])
def test_seed_itself_must_be_an_object(value):
    with pytest.raises(ValueError, match="swarm seed must be an object"):
        _restore(value)
