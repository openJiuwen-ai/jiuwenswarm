"""Unconfirmed Pack reads use the real session Skill target, not global inventory health."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.skills.skill_manager import Skill
from openjiuwen.core.sys_operation.config import LocalWorkConfig
from openjiuwen.core.sys_operation.sys_operation import SysOperation, SysOperationCard
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.tools.skills.skill_tool import SkillTool

from jiuwenswarm.agents.harness.common.rails.symphony import SymphonyOrchestrationRail
from jiuwenswarm.agents.harness.common.rails.symphony.skillpack_route import SkillPackRouter
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.skill.skillpack_install import inventory_mutex
from tests.unit_tests.agentserver.test_skillpack_backend import (
    _install_demo_pack, _write_pack, _write_skill, manager,
)
from tests.unit_tests.agentserver.test_skillpack_direct_route import _config, _model_ctx, _tool_ctx
from tests.unit_tests.agentserver.test_symphony_orchestration_needs_input import _outer_ctx


async def _context(manager, tmp_path: Path, *, extra_roots: tuple[Path, ...] = ()):
    operation = SysOperation(SysOperationCard(
        id=f"pack-read-{tmp_path.name}", work_config=LocalWorkConfig(sandbox_root=[str(tmp_path)]),
    ))
    skill_rail = SkillUseRail(
        [str(manager._skills_dir), *(str(root) for root in extra_roots)], include_tools=False,
    )
    skill_rail.set_sys_operation(operation)
    await skill_rail.reload_skills()
    router = SkillPackRouter(
        manager=lambda: manager, toolkit=lambda: None, model=lambda: None,
        skills_for_session=skill_rail.get_skills_for_session,
    )
    rail = SymphonyOrchestrationRail(
        config_base=_config(False), skillpack_router=router, automatic_skillpack_enabled=lambda: False,
    )
    outer = _outer_ctx("Read the ordinary Skill")
    outer.session = Session(session_id="pack-read-session")
    await rail.before_invoke(outer)
    await rail.before_model_call(_model_ctx(outer))
    assert rail._outer_states[id(outer)].pack_route is None
    skill_tool = SkillTool(operation=operation, get_skills=skill_rail.get_skills_for_session)
    return SimpleNamespace(
        manager=manager, router=router, rail=rail, skill_rail=skill_rail,
        skill_tool=skill_tool, outer=outer,
    )


async def _read(context, name: str, *, relative_file_path: str = "SKILL.md"):
    args = {"skill_name": name, "relative_file_path": relative_file_path}
    call = _tool_ctx(context.outer, "skill_tool", args)
    await context.rail.before_tool_call(call)
    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_provider", "provider_error", "invalid_directory"])
async def test_read_guard_logs_target_failure_and_keeps_denial(manager, tmp_path, monkeypatch, caplog, failure):
    _write_skill(manager._skills_dir / "ordinary", "ordinary", description="ordinary content")
    context = await _context(manager, tmp_path)
    provider = context.router.skills_for_session
    if failure == "missing_provider":
        broken_provider = None
        expected_exception = RuntimeError
    elif failure == "provider_error":
        broken_provider = Mock(side_effect=TypeError("invalid provider contract"))
        expected_exception = TypeError
    else:
        broken_provider = Mock(return_value=[SimpleNamespace(name="ordinary", directory=None)])
        expected_exception = TypeError
    monkeypatch.setattr(context.router, "skills_for_session", broken_provider)

    logger_name = "jiuwenswarm.agents.harness.common.rails.symphony.orchestration_rail"
    with caplog.at_level("WARNING", logger=logger_name):
        blocked = await _read(context, "ordinary")

    assert blocked.inputs.tool_result["success"] is False
    assert blocked.inputs.tool_result["reason"] == "skillpack_inventory_unavailable"
    assert "ordinary content" not in json.dumps(blocked.inputs.tool_result)
    records = [record for record in caplog.records if "skillpack.read_guard summary_unavailable" in record.message]
    assert len(records) == 1
    assert "skill=ordinary" in records[0].message
    # The production masking filter renders then clears exc_info.
    assert "Traceback (most recent call last)" in caplog.text
    assert expected_exception.__name__ in caplog.text
    assert "unconfirmed_pack_summary" in caplog.text

    monkeypatch.setattr(context.router, "skills_for_session", provider)
    allowed = await _read(context, "ordinary")
    assert allowed.inputs.tool_result is None
    result = await context.skill_tool.invoke(allowed.inputs.tool_args, session=context.outer.session)
    assert result.success and "ordinary content" in result.data["skill_content"]


@pytest.mark.asyncio
async def test_ordinary_skill_executes_with_discovery_off_and_real_inventory_lock(manager, tmp_path, monkeypatch):
    _write_skill(manager._skills_dir / "ordinary", "ordinary", description="ordinary content")
    context = await _context(manager, tmp_path)
    snapshot = Mock(wraps=manager.skillpack_inventory_snapshot)
    monkeypatch.setattr(manager, "skillpack_inventory_snapshot", snapshot)
    acquired, release = threading.Event(), threading.Event()

    def hold_inventory():
        with inventory_mutex(manager._skills_dir):
            acquired.set()
            release.wait(timeout=20)

    holder = threading.Thread(target=hold_inventory)
    holder.start()
    try:
        assert await asyncio.to_thread(acquired.wait, 3)
        for _ in range(4):
            call = await _read(context, "ordinary")
            assert call.inputs.tool_result is None
            result = await context.skill_tool.invoke(call.inputs.tool_args, session=context.outer.session)
            assert result.success
            assert "ordinary content" in result.data["skill_content"]
        snapshot.assert_not_called()
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 3)
    assert not holder.is_alive()


@pytest.mark.asyncio
async def test_unrelated_inventory_failure_does_not_block_ordinary_or_expose_pack(manager, tmp_path, monkeypatch):
    _install_demo_pack(manager)
    context = await _context(manager, tmp_path)
    snapshot = Mock(side_effect=OSError("unrelated unreadable archive"))
    monkeypatch.setattr(manager, "skillpack_inventory_snapshot", snapshot)
    ordinary = await _read(context, "first-skill")
    assert ordinary.inputs.tool_result is None
    result = await context.skill_tool.invoke(ordinary.inputs.tool_args, session=context.outer.session)
    assert result.success and "member" in result.data["skill_content"]
    pack = await _read(context, "demo-pack")
    assert pack.inputs.tool_result["reason"] == "skillpack_confirmation_required"
    assert pack.inputs.tool_result["read_only"]
    snapshot.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "invalid_yaml", "permission", "invalid_utf8"])
async def test_target_failure_is_local_and_does_not_allow_pack_body(manager, tmp_path, monkeypatch, failure):
    _write_skill(manager._skills_dir / "ordinary", "ordinary")
    _write_pack(manager._skills_dir / "demo-pack")
    context = await _context(manager, tmp_path)
    target = manager._skills_dir / "demo-pack" / "SKILL.md"
    if failure == "missing":
        target.unlink()
    elif failure == "invalid_yaml":
        target.write_text("---\nkind: skillpack\nskills: [\n---\nSECRET BODY", encoding="utf-8")
    elif failure == "invalid_utf8":
        target.write_bytes(b"\xff\xfe")
    else:
        read_text = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == target:
                raise PermissionError("target unreadable")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable)
    pack = await _read(context, "demo-pack")
    assert pack.inputs.tool_result["success"] is False
    assert "SECRET BODY" not in json.dumps(pack.inputs.tool_result)
    ordinary = await _read(context, "ordinary")
    assert ordinary.inputs.tool_result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("is_pack", [False, True])
async def test_external_skills_use_core_registered_target(manager, tmp_path, is_pack):
    external = tmp_path / "external-mcp"
    target = external / "external-skill"
    if is_pack:
        _write_pack(target, name="external-skill")
    else:
        _write_skill(target, "frontmatter-alias", description="external ordinary content")
    context = await _context(manager, tmp_path, extra_roots=(external,))
    selected = context.skill_tool._get_skill_by_name("external-skill", context.outer.session)
    assert selected.directory == target
    assert context.skill_tool._get_skill_by_name("frontmatter-alias", context.outer.session) is None
    call = await _read(context, "external-skill")
    if is_pack:
        assert call.inputs.tool_result["reason"] == "skillpack_confirmation_required"
    else:
        assert call.inputs.tool_result is None
        result = await context.skill_tool.invoke(call.inputs.tool_args, session=context.outer.session)
        assert result.success and "external ordinary content" in result.data["skill_content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline_is_pack", [False, True])
async def test_restored_session_baseline_directory_matches_actual_skill_tool(manager, tmp_path, baseline_is_pack):
    current = manager._skills_dir / "same-name"
    saved = tmp_path / "saved-location" / "same-name"
    if baseline_is_pack:
        _write_skill(current, "same-name")
        _write_pack(saved, name="same-name")
    else:
        _write_pack(current, name="same-name")
        _write_skill(saved, "same-name", description="saved ordinary content")
    context = await _context(manager, tmp_path)
    context.skill_rail._save_session_baseline(context.outer.session, [Skill(
        name="same-name", directory=saved, description="saved session target",
    )])
    assert context.skill_tool._get_skill_by_name("same-name", context.outer.session).directory == saved
    call = await _read(context, "same-name")
    if baseline_is_pack:
        assert call.inputs.tool_result["reason"] == "skillpack_confirmation_required"
    else:
        assert call.inputs.tool_result is None
        result = await context.skill_tool.invoke(call.inputs.tool_args, session=context.outer.session)
        assert result.success and "saved ordinary content" in result.data["skill_content"]


@pytest.mark.asyncio
async def test_ordinary_changed_to_pack_is_reclassified_on_next_read(manager, tmp_path):
    target = manager._skills_dir / "changing-skill"
    _write_skill(target, "changing-skill")
    context = await _context(manager, tmp_path)
    assert (await _read(context, "changing-skill")).inputs.tool_result is None
    _write_pack(target, name="changing-skill")
    result = (await _read(context, "changing-skill")).inputs.tool_result
    assert result["reason"] == "skillpack_confirmation_required" and result["read_only"]


@pytest.mark.asyncio
@pytest.mark.parametrize("last_is_pack", [False, True])
async def test_duplicate_session_names_use_last_target_like_core_skill_tool(manager, tmp_path, last_is_pack):
    first = tmp_path / "first" / "duplicate"
    last = tmp_path / "last" / "duplicate"
    if last_is_pack:
        _write_skill(first, "duplicate")
        _write_pack(last, name="duplicate")
    else:
        _write_pack(first, name="duplicate")
        _write_skill(last, "duplicate", description="last ordinary content")
    context = await _context(manager, tmp_path)
    context.skill_rail._save_session_baseline(context.outer.session, [
        Skill(name="duplicate", directory=root, description="saved duplicate") for root in (first, last)
    ])
    assert context.skill_tool._get_skill_by_name("duplicate", context.outer.session).directory == last
    call = await _read(context, "duplicate")
    if last_is_pack:
        assert call.inputs.tool_result["reason"] == "skillpack_confirmation_required"
    else:
        assert call.inputs.tool_result is None
        result = await context.skill_tool.invoke(call.inputs.tool_args, session=context.outer.session)
        assert result.success and "last ordinary content" in result.data["skill_content"]


@pytest.mark.asyncio
async def test_new_pack_after_session_baseline_cannot_use_stale_allowlist(manager, tmp_path):
    _write_skill(manager._skills_dir / "ordinary", "ordinary")
    context = await _context(manager, tmp_path)
    context.skill_rail._ensure_session_baseline(context.outer)
    assert (await _read(context, "ordinary")).inputs.tool_result is None
    _write_pack(manager._skills_dir / "new-pack", name="new-pack")
    await context.skill_rail.reload_skills()
    assert context.skill_tool._get_skill_by_name("new-pack", context.outer.session) is not None
    result = (await _read(context, "new-pack")).inputs.tool_result
    assert result["reason"] == "skillpack_confirmation_required" and result["read_only"]


@pytest.mark.asyncio
@pytest.mark.parametrize("relative_file_path", ["SKILL.md", "scripts/run.py", "references/detail.md"])
async def test_pack_requested_files_never_return_execution_body(manager, tmp_path, relative_file_path):
    root = manager._skills_dir / "demo-pack"
    _write_pack(root)
    for relative in ("scripts/run.py", "references/detail.md"):
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text("SECRET EXECUTION BODY", encoding="utf-8")
    with (root / "SKILL.md").open("a", encoding="utf-8") as stream:
        stream.write("\nSECRET EXECUTION BODY\n")
    context = await _context(manager, tmp_path)
    result = (await _read(context, "demo-pack", relative_file_path=relative_file_path)).inputs.tool_result
    assert result["success"] and result["read_only"]
    assert result["members"] == ["first-skill", "second-skill"]
    assert "SECRET EXECUTION BODY" not in json.dumps(result)


@pytest.mark.asyncio
async def test_confirmed_pack_still_checks_live_member_inventory(manager, tmp_path, monkeypatch):
    _install_demo_pack(manager)
    context = await _context(manager, tmp_path)
    route = await context.router.choose("Run this Pack", ["demo-pack"], mode="off")
    assert route.token is not None
    snapshot = Mock(wraps=manager.skillpack_inventory_snapshot)
    monkeypatch.setattr(manager, "skillpack_inventory_snapshot", snapshot)
    assert await context.router.is_current(route)
    _write_skill(manager._skills_dir / "first-skill", "first-skill", description="changed member")
    assert not await context.router.is_current(route)
    assert route.blocked
    assert snapshot.call_count == 2


@pytest.mark.asyncio
async def test_adapter_guard_uses_current_skill_rail_and_session_baseline(manager, tmp_path):
    _write_skill(manager._skills_dir / "ordinary", "ordinary")
    context = await _context(manager, tmp_path)
    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._config_base_cache = _config(False)
    adapter._skill_manager = manager
    adapter._skill_rail = context.skill_rail
    adapter._model = None
    mounted = adapter._build_symphony_orchestration_rail()
    assert mounted is not None
    router = mounted._skillpack_router
    assert await router.unconfirmed_pack_summary("ordinary", session=context.outer.session) is None
    replacement = SkillUseRail(str(tmp_path / "replacement"), include_tools=False)
    target = tmp_path / "replacement" / "ordinary"
    _write_pack(target, name="ordinary")
    replacement.skills = [Skill(name="ordinary", directory=target, description="replacement Pack")]
    adapter._skill_rail = replacement
    result = await router.unconfirmed_pack_summary("ordinary", session=context.outer.session)
    assert result["reason"] == "skillpack_confirmation_required"
