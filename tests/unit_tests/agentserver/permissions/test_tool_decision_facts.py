"""Current contracts for the thin tool decision facts carrier."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    build_tool_decision_facts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
    install_permission_file_semantics,
)


def _facts(
    tool_name: str,
    args: dict[str, object],
    root: Path | None,
    **kwargs: object,
):
    return build_tool_decision_facts(
        tool_name,
        args,
        workspace_root=root,
        original_args_were_valid_object=True,
        **kwargs,
    )


def test_core_access_extraction_owns_read_and_write_paths(tmp_path: Path) -> None:
    read = _facts("read_file", {"path": "README.md"}, tmp_path)
    write = _facts("write_file", {"path": "output.txt", "content": "ok"}, tmp_path)

    assert read.read_paths == ((tmp_path / "README.md").as_posix(),)
    assert read.write_paths == ()
    assert write.write_paths == ((tmp_path / "output.txt").as_posix(),)
    assert read.accesses_known is True
    assert write.accesses_known is True


def test_read_pdf_uses_core_file_access_extraction(tmp_path: Path) -> None:
    install_permission_file_semantics()
    pdf_path = tmp_path / "report.pdf"
    facts = _facts("read_pdf", {"pdf_path": str(pdf_path)}, tmp_path)

    assert facts.accesses_known is True
    assert facts.read_paths == (pdf_path.as_posix(),)
    assert facts.write_paths == ()

    missing = _facts("read_pdf", {"pdf_path": ""}, tmp_path)
    assert missing.accesses_known is False
    assert missing.read_paths == ()


def test_engine_external_paths_are_consumed_not_recomputed(tmp_path: Path) -> None:
    external = (tmp_path.parent / "outside.txt").as_posix()
    facts = _facts(
        "read_file",
        {"path": "README.md"},
        tmp_path,
        external_paths=(external,),
    )

    assert facts.external_paths == (external,)
    assert external not in facts.paths


def test_platform_root_is_host_fact_not_an_action_descriptor(tmp_path: Path) -> None:
    platform = tmp_path / "agent-workspace"
    facts = _facts(
        "read_file",
        {"path": str(platform / "skills" / "SKILL.md")},
        tmp_path / "project",
        platform_trusted_root=platform,
    )

    assert facts.platform_trusted_root == platform.as_posix()
    assert not hasattr(facts, "trusted_path_rules")


def test_missing_workspace_or_unsupported_path_never_looks_empty_and_safe() -> None:
    missing_root = _facts("read_file", {"path": "README.md"}, None)
    unsupported = _facts("apply_patch", {"patch": "*** Begin Patch"}, Path("."))

    assert missing_root.accesses_known is False
    assert unsupported.accesses_known is False


def test_shell_command_is_carried_but_not_locally_parsed(tmp_path: Path) -> None:
    facts = _facts("mcp_exec_command", {"command": "printf ok | tee out"}, tmp_path)

    assert facts.command == "printf ok | tee out"
    assert facts.accesses_known is True
    assert not hasattr(facts, "command_operator_kinds")
    assert not hasattr(facts, "deterministic_findings")


def test_send_paths_come_only_from_send_file_guard(tmp_path: Path) -> None:
    path = (tmp_path / "report.md").as_posix()
    facts = _facts(
        "send_file_to_user",
        {"abs_file_path_list": [path]},
        tmp_path,
        send_paths=(path,),
    )

    assert facts.read_paths == (path,)
    assert facts.accesses_known is True


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        ("todo_modify", {"future": {"nested": True}}),
        ("memory_search", {"query": object(), "maxResults": -999}),
        ("skill_tool", {"skill_name": "daily-report", "future": {"nested": True}}),
    ],
)
def test_runtime_owned_arguments_remain_opaque(
    tmp_path: Path,
    tool_name: str,
    tool_args: dict[str, object],
) -> None:
    facts = _facts(tool_name, tool_args, tmp_path)

    assert dict(facts.untrusted_args) == tool_args


def test_arguments_are_immutable_and_no_descriptor_fields_remain(tmp_path: Path) -> None:
    facts = _facts("todo_get", {"id": "todo-1"}, tmp_path)

    with pytest.raises(TypeError):
        facts.untrusted_args["id"] = "other"  # type: ignore[index]
    for retired in ("schema_valid", "risk_tier", "domain_operation", "absolute_uris"):
        assert not hasattr(facts, retired)
