# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the Skill visibility RPC handlers.

Three properties are pinned here:

* The three wire methods are declared by ``ReqMethod`` and routed to a handler
  that actually exists. A missing enum member silently unroutes the whole RPC,
  which is exactly the kind of failure no other test would catch.
* ``skills.visibility.update`` applies its deltas inside one lock acquisition,
  so two clients authorizing at the same time keep each other's change. The
  full-replacement ``skills.visibility.set`` cannot offer that, because its
  read-modify-write happens in the caller.
* Names that could never be a Skill directory are dropped with a warning
  instead of being persisted into the metadata forever.
"""

# pylint: disable=protected-access

import asyncio
import logging
import time
from contextlib import contextmanager

import pytest
from openjiuwen.agent_teams.paths import (
    configure_openjiuwen_home,
    member_skill_visibility_path,
    reset_openjiuwen_home,
    team_skill_visibility_path,
)
from openjiuwen.agent_teams.skill import visibility as visibility_module
from openjiuwen.agent_teams.skill.visibility import (
    SCOPE_MEMBER,
    SCOPE_TEAM,
    read_skill_visibility,
    set_skill_visibility,
)

from jiuwenswarm.server.runtime.skill.skill_manager import (
    SkillManager,
    _coerce_skill_name_list,
    _validate_skill_name,
)

test_logger = logging.getLogger("tests.skill_visibility_rpc")

TEAM_NAME = "demo_team"
MEMBER_NAME = "reviewer"

VISIBILITY_METHODS: tuple[tuple[str, str], ...] = (
    ("skills.visibility.get", "handle_skills_visibility_get"),
    ("skills.visibility.set", "handle_skills_visibility_set"),
    ("skills.visibility.update", "handle_skills_visibility_update"),
)


@pytest.mark.parametrize("wire_name,handler_name", VISIBILITY_METHODS)
def test_visibility_method_is_declared_and_routed(wire_name, handler_name):
    """Every visibility method parses to a ReqMethod and reaches a real handler.

    The route table is keyed by ``ReqMethod``, so a wire name the enum does not
    declare cannot be routed at all — and nothing raises: the request would just
    fall through as "not a Skills request". This test is the only thing standing
    between that and a silently dead RPC.
    """
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.agent_adapter.interface import _SKILL_ROUTES

    method = ReqMethod(wire_name)
    test_logger.info("routing %s -> %s", method, _SKILL_ROUTES.get(method))
    assert _SKILL_ROUTES[method] == handler_name
    assert callable(getattr(SkillManager, handler_name))


def test_visibility_writes_are_marked_for_a_rail_refresh():
    """A write must be listed as such, otherwise a revocation never takes hold."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import _SKILL_VISIBILITY_WRITE_HANDLERS

    assert _SKILL_VISIBILITY_WRITE_HANDLERS == {
        "handle_skills_visibility_set",
        "handle_skills_visibility_update",
    }


@pytest.fixture(name="visibility_home")
def fixture_visibility_home(tmp_path, monkeypatch):
    """Redirect both the team layout and the global Skill library into tmp_path."""
    skills_dir = tmp_path / "agent" / "workspace" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_agent_skills_dir",
        lambda: skills_dir,
    )
    configure_openjiuwen_home(tmp_path / "openjiuwen_home")
    yield tmp_path
    reset_openjiuwen_home()


def _member_params(**extra) -> dict:
    """Build member-scoped RPC params, merged with the given overrides."""
    params = {"scope": SCOPE_MEMBER, "team_name": TEAM_NAME, "member_name": MEMBER_NAME}
    params.update(extra)
    return params


@contextmanager
def _capture_manager_warnings():
    """Collect warnings emitted by the skill manager module.

    ``caplog`` is not usable here: the runtime detaches the ``jiuwenswarm``
    logger from the root logger, so records never reach pytest's root handler.

    Yields:
        A list that receives every captured warning message.
    """
    messages: list[str] = []

    class _Collector(logging.Handler):
        """Minimal handler appending formatted messages to ``messages``."""

        def emit(self, record: logging.LogRecord) -> None:
            """Append one formatted record."""
            messages.append(record.getMessage())

    manager_logger = logging.getLogger("jiuwenswarm.server.runtime.skill.skill_manager")
    handler = _Collector(level=logging.WARNING)
    previous_level = manager_logger.level
    manager_logger.addHandler(handler)
    manager_logger.setLevel(logging.WARNING)
    try:
        yield messages
    finally:
        manager_logger.removeHandler(handler)
        manager_logger.setLevel(previous_level)


def _slow_down_visibility_writes(monkeypatch, delay: float) -> None:
    """Widen the critical section so a lost update would be observable.

    ``update_skill_visibility`` reads, merges and writes inside one lock
    acquisition. Sleeping right before the write keeps that section open long
    enough that a competing writer certainly tries to enter it.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        delay: Seconds to sleep inside the critical section.
    """
    original_write = visibility_module._write_atomic

    def _slow_write(path, visibility):
        time.sleep(delay)
        original_write(path, visibility)

    monkeypatch.setattr(visibility_module, "_write_atomic", _slow_write)


@pytest.mark.asyncio
async def test_visibility_update_keeps_concurrent_grants(visibility_home, monkeypatch):
    """Concurrent incremental grants must not overwrite each other."""
    manager = SkillManager()
    _slow_down_visibility_writes(monkeypatch, 0.05)
    granted = [f"skill-{index}" for index in range(6)]

    results = await asyncio.gather(
        *(manager.handle_skills_visibility_update(_member_params(add_allow=[name])) for name in granted)
    )

    assert all(result["success"] for result in results)
    path = member_skill_visibility_path(TEAM_NAME, MEMBER_NAME)
    visibility = read_skill_visibility(path, scope=SCOPE_MEMBER, entity_id=MEMBER_NAME)
    test_logger.info("allow after %d concurrent grants: %s", len(granted), visibility.allow)
    assert visibility.allow == sorted(granted)


@pytest.mark.asyncio
async def test_visibility_update_applies_add_and_remove_deltas(visibility_home):
    """One call may grant and revoke at once, leaving untouched entries alone."""
    manager = SkillManager()
    path = member_skill_visibility_path(TEAM_NAME, MEMBER_NAME)
    set_skill_visibility(
        path,
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
        allow=["skill-a", "skill-b"],
        deny=["skill-x"],
    )

    payload = await manager.handle_skills_visibility_update(
        _member_params(
            add_allow=["skill-c"],
            remove_allow=["skill-a"],
            add_deny=["skill-y"],
            remove_deny=["skill-x"],
        )
    )

    test_logger.info("visibility after delta update: %s", payload)
    assert payload["success"] is True
    assert payload["allow"] == ["skill-b", "skill-c"]
    assert payload["deny"] == ["skill-y"]
    assert payload["enabled_skills"] == ["skill-b", "skill-c"]
    assert payload["disabled_skills"] == ["skill-y"]


@pytest.mark.asyncio
async def test_visibility_update_without_deltas_is_a_no_op(visibility_home):
    """An empty delta must not clear the document the way a full set would."""
    manager = SkillManager()
    path = team_skill_visibility_path(TEAM_NAME)
    set_skill_visibility(
        path,
        scope=SCOPE_TEAM,
        entity_id=TEAM_NAME,
        allow=["skill-a"],
        deny=["skill-x"],
    )

    payload = await manager.handle_skills_visibility_update({"scope": SCOPE_TEAM, "team_name": TEAM_NAME})

    assert payload["allow"] == ["skill-a"]
    assert payload["deny"] == ["skill-x"]


@pytest.mark.asyncio
async def test_visibility_update_rejects_invalid_scope_and_names(visibility_home):
    """Malformed addressing is answered as an error payload, never as a 500."""
    manager = SkillManager()

    bad_scope = await manager.handle_skills_visibility_update(_member_params(scope="galaxy"))
    bad_team = await manager.handle_skills_visibility_update(_member_params(team_name="../escape"))
    bad_member = await manager.handle_skills_visibility_update(_member_params(member_name="a/b"))

    test_logger.info("rejected update payloads: %s | %s | %s", bad_scope, bad_team, bad_member)
    assert bad_scope["success"] is False
    assert bad_team["success"] is False
    assert bad_member["success"] is False


@pytest.mark.asyncio
async def test_visibility_update_reports_lock_timeout_as_error_payload(visibility_home, monkeypatch):
    """A busy metadata file yields a failure payload instead of an exception."""
    from openjiuwen.agent_teams.skill.file_lock import FileLockTimeout

    manager = SkillManager()

    def _raise_timeout(*args, **kwargs):
        raise FileLockTimeout("busy")

    monkeypatch.setattr(visibility_module, "update_skill_visibility", _raise_timeout)

    payload = await manager.handle_skills_visibility_update(_member_params(add_allow=["skill-a"]))

    assert payload["success"] is False
    assert "detail" in payload


@pytest.mark.asyncio
async def test_visibility_update_drops_illegal_skill_names(visibility_home):
    """A dirty name is warned about and discarded; the legal ones still land."""
    manager = SkillManager()
    dirty = ["../../etc/passwd", "..", ".hidden", "bad:name", "with\nnewline", ""]

    with _capture_manager_warnings() as warnings:
        payload = await manager.handle_skills_visibility_update(
            _member_params(add_allow=[*dirty, "skill-ok"], add_deny=["nested/evil", "skill-blocked"])
        )

    test_logger.info("visibility after dirty input: %s", payload)
    assert payload["allow"] == ["skill-ok"]
    assert payload["deny"] == ["skill-blocked"]
    stored = read_skill_visibility(
        member_skill_visibility_path(TEAM_NAME, MEMBER_NAME),
        scope=SCOPE_MEMBER,
        entity_id=MEMBER_NAME,
    )
    assert stored.allow == ["skill-ok"]
    assert stored.deny == ["skill-blocked"]
    test_logger.info("rejection warnings: %s", warnings)
    assert any("rejected invalid skill name" in message for message in warnings)


@pytest.mark.asyncio
async def test_visibility_set_drops_illegal_skill_names(visibility_home):
    """Full replacement filters the same way the incremental path does."""
    manager = SkillManager()

    payload = await manager.handle_skills_visibility_set(
        _member_params(allow=["skill-ok", "../../x"], deny=["skill-blocked", "C:\\tmp\\x"])
    )

    assert payload["allow"] == ["skill-ok"]
    assert payload["deny"] == ["skill-blocked"]


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "nested/skill",
        "back\\slash",
        r"C:\tmp\skill",
        ".",
        "..",
        "...",
        ".hidden",
        "trailing.",
        "bad:name",
        'quote"name',
        "pipe|name",
        "star*name",
        "question?name",
        "less<name",
        "greater>name",
        "control\x00name",
        "tab\tname",
        "CON",
        "com1.md",
        "x" * 129,
    ],
)
def test_validate_skill_name_rejects_unusable_names(name):
    """Nothing that could not be a library directory may reach the metadata."""
    with pytest.raises(ValueError):
        _validate_skill_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "skill-creator",
        "swarmskill-creator",
        "openJiuwen-DeepSearch",
        "skill_omni_creation",
        "skill-gen-4-enterprise-doc",
        "ascend-moe-optimizer-auto-trace",
        "skill.v2",
        "报表生成",
        "skill with space",
    ],
)
def test_validate_skill_name_accepts_real_world_names(name):
    """The rule must not be stricter than the names the library really carries."""
    assert _validate_skill_name(name) == name


def test_coerce_skill_name_list_normalizes_shapes():
    """A single name, a collection and junk all normalize predictably."""
    assert _coerce_skill_name_list("skill-a", "allow") == ["skill-a"]
    assert _coerce_skill_name_list(["skill-a", " skill-b ", 7, None], "allow") == ["skill-a", "skill-b"]
    assert _coerce_skill_name_list(None, "deny") == []
    assert _coerce_skill_name_list(42, "deny") == []
