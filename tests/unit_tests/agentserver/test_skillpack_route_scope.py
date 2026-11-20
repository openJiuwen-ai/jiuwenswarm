# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Authorization follows the selected Pack, not unrelated inventory changes."""

from __future__ import annotations

import shutil

import pytest

from jiuwenswarm.agents.harness.common.rails.symphony.skillpack_route import PackRoute
from tests.unit_tests.agentserver.test_skillpack_backend import _write_skill
from tests.unit_tests.agentserver.test_skillpack_inventory_guard_concurrency import installed_route


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["confirmation", "execution"])
@pytest.mark.parametrize("change", ["install", "uninstall", "disable", "edit", "reinstall"])
async def test_unrelated_skill_change_preserves_selected_pack(installed_route, phase, change):
    context = installed_route
    unrelated = context.skills / "unrelated"
    if change != "install":
        _write_skill(unrelated, "unrelated")
    before = await context.router.snapshot()
    route = PackRoute(
        candidates=list(context.router.eligible(before).values()),
        inventory_revision=before["inventory_revision"],
    )
    if phase == "execution":
        assert await context.router.confirm(route, "release-pack", source="confirmed_query_match")

    if change == "install":
        _write_skill(unrelated, "unrelated")
    elif change == "disable":
        context.manager.set_skill_enabled("unrelated", False)
    elif change == "edit":
        (unrelated / "new-script.py").write_text("print('new unrelated task')\n", encoding="utf-8")
    else:
        shutil.rmtree(unrelated)
        if phase == "execution":
            assert await context.router.is_current(route)
        if change == "reinstall":
            _write_skill(unrelated, "unrelated")
            (unrelated / "new-script.py").write_text("print('reinstalled')\n", encoding="utf-8")

    after = await context.router.snapshot()
    assert before["inventory_revision"] != after["inventory_revision"]
    if phase == "confirmation":
        assert await context.router.confirm(route, "release-pack", source="confirmed_query_match")
    assert await context.router.is_current(route)
    assert route.token.allowed_member_ids == ("summary", "risk")
    assert not route.changed and not route.blocked


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["confirmation", "execution"])
@pytest.mark.parametrize("target", ["release-pack", "summary"])
@pytest.mark.parametrize("change", ["edit", "disable", "remove"])
async def test_selected_pack_or_member_change_still_revokes_authorization(installed_route, phase, target, change):
    context = installed_route
    before = await context.router.snapshot()
    route = PackRoute(
        candidates=list(context.router.eligible(before).values()),
        inventory_revision=before["inventory_revision"],
    )
    if phase == "execution":
        assert await context.router.confirm(route, "release-pack", source="confirmed_query_match")
    directory = context.skills / target
    if change == "edit":
        (directory / "new-script.py").write_text("print('changed authorized code')\n", encoding="utf-8")
    elif change == "disable":
        context.manager.set_skill_enabled(target, False)
    else:
        shutil.rmtree(directory)
    if phase == "confirmation":
        assert not await context.router.confirm(route, "release-pack", source="confirmed_query_match")
        assert route.token is None and route.changed
    else:
        assert not await context.router.is_current(route)
        assert route.blocked
