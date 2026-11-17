# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fixtures for tests simulating a process-lifetime Server Worker."""

import json
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


@pytest.fixture
async def worker_callback_framework(monkeypatch):
    """Give each simulated Worker its own SDK registry and cleanup boundary."""
    from jiuwenswarm.runtime import kernel as kernel_module
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.callback import AsyncCallbackFramework

    kernel = kernel_module.ResourceKernel()
    monkeypatch.setattr(kernel_module, "_PROCESS_KERNEL", kernel)
    framework = AsyncCallbackFramework(enable_logging=False)
    monkeypatch.setattr(Runner, "callback_framework", framework)
    yield framework
    owner = getattr(framework.register, "_worker_callback_registry_owner", None)
    if owner is not None:
        await owner.aclose()
    await kernel.close()


@pytest.fixture
def clean_extension_registry():
    """Run real worker-bootstrap registry creation without leaking the crypto bridge.

    ``create_instance`` installs ``_RegistryCryptoBridge`` into the global crypto
    provider; restoring only the ``_instance`` class attribute afterwards (as
    ``monkeypatch.setattr`` does) leaves the bridge orphaned — provider set,
    instance gone — and later ``crypto.encrypt`` callers fail with
    "ExtensionRegistry 尚未初始化". ``reset_instance`` clears both sides, so the
    process ends exactly as clean as it started.
    """
    from agent_gateway.extensions.registry import ExtensionRegistry

    ExtensionRegistry.reset_instance()
    yield
    ExtensionRegistry.reset_instance()


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillManager:
    """Isolated SkillManager over a tmp skills dir with a clean state file.

    Shared by the skill_manager / skill_sync / skills_import test families;
    extracted verbatim from their formerly duplicated local fixtures.
    """
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    state_file = skills_dir / "skills_state.json"
    state_file.write_text(
        json.dumps(
            {
                "marketplaces": [],
                "installed_plugins": [],
                "local_skills": [],
                "skill_configs": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: tmp_path / "builtin_missing",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager._get_agent_root_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager._get_marketplace_dir",
        lambda: skills_dir / "_marketplace",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager._get_state_file",
        lambda: state_file,
    )
    # skilldev state_utils 也走同一 state 文件
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_state_file",
        lambda: state_file,
    )
    return SkillManager()
