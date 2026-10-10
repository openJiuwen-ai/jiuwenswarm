"""Smart location evidence is distinguishable and private at the model boundary."""

from dataclasses import replace
import json

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
    IsolatedModelReviewerClient,
    build_reviewer_action_view,
)
from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_redaction import (
    redact_reviewer_intent,
    redact_text,
    reviewer_path_location,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    OriginalUserIntentEvidence,
    UserIntentSource,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    build_tool_decision_facts,
)


def _view(root, paths, *, platform="", external=(), intent="", malformed_access=False):
    facts = build_tool_decision_facts(
        "write_file",
        {"file_path": str(paths[0])},
        workspace_root=root,
        platform_trusted_root=platform,
        original_args_were_valid_object=True,
        external_paths=external,
    )
    facts = replace(facts, write_paths=tuple(str(path) for path in paths))
    if malformed_access:
        # Exercise defensive projection even when a caller supplies malformed
        # access evidence that the current Core extractor normally rejects.
        facts = replace(facts, accesses_known=True)
    return build_reviewer_action_view(
        facts,
        policy_level="ask",
        policy_reason="policy_ask",
        allowed_outcomes=("allow_once", "manual", "deny"),
        no_auto_allow_reason="",
        domain_route=None,
        original_user_intent=OriginalUserIntentEvidence(
            source=UserIntentSource.HOST_USER_MESSAGE,
            text=intent,
        ),
    )


class _Transport:
    def __init__(self):
        self.messages = []

    async def ainvoke(self, messages, **kwargs):
        self.messages.append(messages)
        return "{}"


async def test_complete_model_requests_distinguish_three_same_name_targets(tmp_path):
    model = _Transport()
    client = IsolatedModelReviewerClient(model=model)
    for directory in ("work/presentation", ".", "other"):
        await client.assess(
            _view(
                tmp_path,
                [tmp_path / directory / "report.py"],
                intent=f'Only change files in "{tmp_path}/work/presentation".',
            )
        )
    wire = [messages[1]["content"] for messages in model.messages]
    assert len(set(wire)) == 3
    for payload in wire:
        assert str(tmp_path) not in payload
        evidence = json.loads(payload)["request"]["review_evidence"]
        assert evidence["user_intent"]["trusted_user_turns"][0]["text"] == (
            "Only change files in [workspace]/work/presentation."
        )
        assert evidence["path_targets"][0]["location_status"] == "complete"
        assert evidence["path_targets_incomplete"] is False


def test_identity_deduplication_precedes_bounded_projection(tmp_path):
    same = tmp_path / "work" / "report.py"
    paths = [same, same, same.parent / "." / "report.py"]
    paths += [tmp_path / f"dir{index}" / "report.py" for index in range(8)]
    evidence = _view(tmp_path, paths).to_json_dict()["review_evidence"]
    assert len(evidence["path_targets"]) == 8
    assert evidence["path_targets_incomplete"] is True
    assert (
        len({json.dumps(target["location"]) for target in evidence["path_targets"]})
        == 8
    )


@pytest.mark.parametrize(
    "relative",
    [
        ".ssh/public.txt",
        "nested/.env.local/a.txt",
        "private_key_archive/a.txt",
        "nested/api_token/a.txt",
        "nested/credential-cache/a.txt",
        "keys/server.pem",
        "nested/sk-FAKEONLY123456789/a.txt",
        "control\nsegment/a.txt",
        "control\u202esegment/a.txt",
    ],
)
async def test_sensitive_ancestors_never_reach_model(tmp_path, relative):
    # TEST ONLY: synthetic secret-shaped fixture, never usable credentials.
    model = _Transport()
    path = tmp_path / relative
    await IsolatedModelReviewerClient(model=model).assess(
        _view(tmp_path, [path], intent=f'Read "{path}".')
    )
    wire = model.messages[0][1]["content"]
    target = json.loads(wire)["request"]["review_evidence"]["path_targets"][0]
    assert "location" not in target
    assert target["location_status"] == "redacted"
    assert str(tmp_path) not in wire
    assert relative not in wire
    assert "sk-FAKEONLY123456789" not in wire


def test_redacted_distinct_targets_are_not_deduplicated(tmp_path):
    paths = [tmp_path / "first-secret" / "a.txt", tmp_path / "second-secret" / "a.txt"]
    targets = _view(tmp_path, paths).to_json_dict()["review_evidence"]["path_targets"]
    assert len(targets) == 2
    assert targets[0] == targets[1]
    assert targets[0]["location_status"] == "redacted"


@pytest.mark.parametrize(
    "basename",
    [
        "report\u202eexe.py",
        "report\x00.py",
        "report\x85.py",
        "report\u200b.py",
        "sk-FAKEONLY123456789.py",
    ],
)
async def test_controls_in_basename_never_reach_model(tmp_path, basename):
    model = _Transport()
    await IsolatedModelReviewerClient(model=model).assess(
        _view(tmp_path, [tmp_path / "work" / basename], malformed_access=True)
    )
    wire = model.messages[0][1]["content"]
    target = json.loads(wire)["request"]["review_evidence"]["path_targets"][0]
    assert target["location_status"] == "redacted"
    assert target["target"] == "[redacted_target]"
    assert "location" not in target
    assert basename not in wire
    assert "\\u0000" not in wire


def test_root_overlap_restriction_and_long_evidence(tmp_path):
    root = tmp_path / "project"
    target = root / "nested" / "report.py"
    item = _view(
        root, [target], platform=tmp_path, external=(str(target),)
    ).to_json_dict()["review_evidence"]["path_targets"][0]
    assert item["scope"] == "engine_restricted"
    assert item["location"] == {
        "base": "workspace",
        "relative_path": "nested/report.py",
    }
    location, status = reviewer_path_location(
        str(root), workspace_root=str(root), platform_trusted_root=""
    )
    assert (location, status) == (
        {"base": "workspace", "relative_path": "."},
        "complete",
    )
    long_target = root / ("a" * 121) / ("b" * 121) / "report.py"
    location, status = reviewer_path_location(
        str(long_target), workspace_root=str(root), platform_trusted_root=""
    )
    assert (location, status) == (None, "omitted")


def test_intent_boundaries_unicode_spaces_and_platform(tmp_path):
    root = tmp_path / "project"
    platform = tmp_path / "data"
    raw = f'只改 "{root}/work/项目 A"，读取 {platform}/skills/demo/SKILL.md。'
    text = redact_reviewer_intent(
        raw, workspace_root=str(root), platform_trusted_root=str(platform)
    )
    assert (
        text
        == "只改 [workspace]/work/项目 A，读取 [platform_trusted_root]/skills/demo/SKILL.md。"
    )
    for outside in (
        f"{root}-other/report.py",
        f"file://{root}/report.py",
        f"{root}/../outside/report.py",
    ):
        text = redact_reviewer_intent(
            outside, workspace_root=str(root), platform_trusted_root=""
        )
        assert "[workspace]" not in text
        assert str(tmp_path) not in text
    # The shared redactor retains its pre-repair behavior.
    assert redact_text(f"Read {root}/report.py") == "Read [path]"
    assert (
        redact_reviewer_intent(
            'Inspect /custom-location/a and "/custom-location/目录 A".',
            workspace_root=str(root),
            platform_trusted_root="",
        )
        == "Inspect [path] and [path]."
    )


def test_symlink_location_uses_host_canonical_target(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    directory = root / "actual"
    directory.mkdir()
    alias = root / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    location, status = reviewer_path_location(
        str(alias / "report.py"), workspace_root=str(root), platform_trusted_root=""
    )
    assert status == "complete"
    assert location["relative_path"] == "actual/report.py"
