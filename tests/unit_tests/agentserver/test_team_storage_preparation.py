# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team storage is prepared once per owner without caching partial migrations."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from openjiuwen.agent_teams import paths
    from jiuwenswarm.agents.harness.team import bootstrap
    from jiuwenswarm.common import utils

    state = {"home": tmp_path / "home", "library": tmp_path / "skills"}
    previous_home = paths.get_openjiuwen_home()
    previous_library = paths.global_skills_dir()
    monkeypatch.setattr(bootstrap, "get_user_workspace_dir", lambda: state["home"])
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: state["library"])
    try:
        yield state, bootstrap, paths
    finally:
        paths.configure_openjiuwen_home(previous_home)
        paths.configure_global_skills_dir(previous_library)


def test_concurrent_first_consumers_share_one_completed_migration(storage, monkeypatch):
    state, bootstrap, paths = storage
    preparation = bootstrap.TeamStoragePreparation()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def migrate(**kwargs):
        assert kwargs == {"library_dir": state["library"].resolve(), "strict": True}
        entered.set()
        assert release.wait(5)
        finished.set()

    migration = Mock(side_effect=migrate)
    monkeypatch.setattr(bootstrap, "migrate_team_skill_views", migration)

    def consume():
        with preparation.activate():
            assert finished.is_set()
            assert paths.get_openjiuwen_home() == state["home"].resolve()
            assert paths.global_skills_dir() == state["library"].resolve()

    with ThreadPoolExecutor(max_workers=8) as pool:
        first = pool.submit(consume)
        try:
            assert entered.wait(5)
            waiters = [pool.submit(consume) for _ in range(7)]
            assert not any(waiter.done() for waiter in waiters)
        finally:
            release.set()
        first.result(timeout=5)
        for waiter in waiters:
            waiter.result(timeout=5)
    preparation.prepare()
    assert migration.call_count == 1


def test_failed_preparation_propagates_and_retries(storage, monkeypatch):
    _, bootstrap, _ = storage
    migration = Mock(side_effect=[OSError("migration unavailable"), None])
    monkeypatch.setattr(bootstrap, "migrate_team_skill_views", migration)
    preparation = bootstrap.TeamStoragePreparation()

    with pytest.raises(OSError, match="migration unavailable"):
        preparation.prepare()
    preparation.prepare()
    preparation.prepare()
    assert migration.call_count == 2


def test_waiting_consumer_can_retry_after_failed_migration(storage, monkeypatch):
    _, bootstrap, _ = storage
    entered = threading.Event()
    release = threading.Event()
    attempts = []

    def migrate(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5)
            raise OSError("first attempt failed")

    monkeypatch.setattr(bootstrap, "migrate_team_skill_views", migrate)
    preparation = bootstrap.TeamStoragePreparation()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(preparation.prepare)
        try:
            assert entered.wait(5)
            waiting = pool.submit(preparation.prepare)
            assert not waiting.done()
        finally:
            release.set()
        with pytest.raises(OSError, match="first attempt failed"):
            first.result(timeout=5)
        waiting.result(timeout=5)
    preparation.prepare()
    assert len(attempts) == 2


def test_owner_and_directory_identity_do_not_reuse_another_success(storage, monkeypatch):
    state, bootstrap, paths = storage
    migration = Mock()
    monkeypatch.setattr(bootstrap, "migrate_team_skill_views", migration)
    first = bootstrap.TeamStoragePreparation()
    second = bootstrap.TeamStoragePreparation()
    first.prepare()
    second.prepare()
    first.prepare()
    assert migration.call_count == 2

    original_home = state["home"]
    state["home"] = original_home / "named-instance"
    first.prepare()
    state["library"] = state["library"] / "new-config"
    first.prepare()
    first.prepare()
    assert migration.call_count == 4
    assert paths.global_skills_dir() == state["library"].resolve()
    state["home"] = original_home
    first.prepare()
    assert migration.call_count == 5


def test_task_local_team_home_invalidates_prepared_identity(storage, monkeypatch):
    state, bootstrap, paths = storage
    homes = []
    monkeypatch.setattr(
        bootstrap, "migrate_team_skill_views",
        lambda **kwargs: homes.append(paths.get_openjiuwen_home()),
    )
    preparation = bootstrap.TeamStoragePreparation()
    preparation.prepare()
    task_home = state["home"] / "task"
    token = paths.set_task_openjiuwen_home(task_home)
    try:
        preparation.prepare()
        preparation.prepare()
    finally:
        paths.reset_task_openjiuwen_home(token)
    preparation.prepare()
    assert homes == [state["home"], task_home, state["home"]]


def test_cache_hit_restores_sdk_paths_without_rescanning(storage, monkeypatch):
    state, bootstrap, paths = storage
    migration = Mock()
    monkeypatch.setattr(bootstrap, "migrate_team_skill_views", migration)
    preparation = bootstrap.TeamStoragePreparation()
    preparation.prepare()
    paths.configure_openjiuwen_home(state["home"] / "other-owner")
    paths.configure_global_skills_dir(state["library"] / "other-owner")
    preparation.prepare()
    assert migration.call_count == 1
    assert paths.get_openjiuwen_home() == state["home"].resolve()
    assert paths.global_skills_dir() == state["library"].resolve()


def test_parallel_owners_keep_directory_bindings_during_consumption(storage, monkeypatch):
    state, bootstrap, paths = storage
    from jiuwenswarm.common import utils

    local = threading.local()
    monkeypatch.setattr(bootstrap, "get_user_workspace_dir", lambda: local.home)
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: local.home / "skills")
    migration = Mock()
    monkeypatch.setattr(bootstrap, "migrate_team_skill_views", migration)

    def consume(name):
        local.home = state["home"] / name
        preparation = bootstrap.TeamStoragePreparation()
        for _ in range(10):
            with preparation.activate():
                assert paths.get_openjiuwen_home() == local.home.resolve()
                assert paths.global_skills_dir() == (local.home / "skills").resolve()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(consume, str(index)) for index in range(4)]
        for future in futures:
            future.result(timeout=5)
    assert migration.call_count == 4


@pytest.mark.asyncio
async def test_repeated_visibility_requests_use_one_owner_preparation(storage, monkeypatch):
    state, bootstrap, _ = storage
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    migration = Mock()
    monkeypatch.setattr(bootstrap, "migrate_team_skill_views", migration)
    manager = SkillManager(workspace_dir=str(state["library"].parent))
    params = {"scope": "member", "team_name": "demo", "member_name": "reader"}
    for operation in ("get", "set", "update", "get"):
        response = await getattr(manager, f"handle_skills_visibility_{operation}")(params)
        assert response["success"]
        assert Path(response["path"]).is_relative_to(state["home"])
    assert migration.call_count == 1
