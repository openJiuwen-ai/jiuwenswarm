# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cold discovery, external edits, lease isolation and mutable-session safety."""

import asyncio
import os
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.runtime import kernel
from jiuwenswarm.runtime.agent_kernel import agent_recipe
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


@pytest.fixture
def isolated_kernel(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils

    owner = kernel.ResourceKernel()
    monkeypatch.setattr(kernel, "_PROCESS_KERNEL", owner)
    monkeypatch.setattr(utils, "_workspace_base_dir", tmp_path / "data")
    return owner


def test_skill_discovery_reads_header_and_persistent_index_keeps_full_detail(
    monkeypatch, tmp_path, isolated_kernel,
):
    skill = tmp_path / "SKILL.md"
    body = ("full body " * 100000).strip()
    skill.write_text("---\nname: alpha\ndescription: first\n---\n" + body, encoding="utf-8")
    parsed = SkillManager._read_skill_metadata(skill)
    assert parsed["name"] == "alpha" and "body" not in parsed
    assert SkillManager._parse_skill_md(skill)["body"] == body
    # A new owner/process reuses the on-disk index without reading the body.
    monkeypatch.setattr(kernel, "_PROCESS_KERNEL", kernel.ResourceKernel())
    original = SkillManager._parse_skill_md_uncached
    monkeypatch.setattr(SkillManager, "_parse_skill_md_uncached", lambda *a, **k: pytest.fail("valid persistent hit"))
    assert SkillManager._read_skill_metadata(skill) == parsed
    monkeypatch.setattr(SkillManager, "_parse_skill_md_uncached", original)
    skill.write_text("---\nname: beta\ndescription: external edit\n---\nnew body", encoding="utf-8")
    assert SkillManager._read_skill_metadata(skill)["name"] == "beta"
    skill.unlink()
    assert SkillManager._read_skill_metadata(skill) is None
    skill.write_text("a plain skill without frontmatter", encoding="utf-8")
    assert SkillManager._read_skill_metadata(skill)["name"] == "SKILL"


def test_corrupt_skill_index_rebuilds_and_results_are_independent(monkeypatch, tmp_path, isolated_kernel):
    from jiuwenswarm.common.utils import get_user_workspace_dir

    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: alpha\ntags: [one]\n---\nbody", encoding="utf-8")
    first = SkillManager._read_skill_metadata(skill)
    first["tags"].append("caller mutation")
    for index in (get_user_workspace_dir() / "cache/skill-metadata").glob("*.json"):
        index.write_text("broken", encoding="utf-8")
    monkeypatch.setattr(kernel, "_PROCESS_KERNEL", kernel.ResourceKernel())
    assert SkillManager._read_skill_metadata(skill)["tags"] == ["one"]
    assert SkillManager._parse_skill_md(skill)["body"] == "body"


def test_external_same_size_edit_with_restored_mtime_invalidates_index(tmp_path, isolated_kernel):
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: alpha\n---\nbody", encoding="utf-8")
    stamp = skill.stat()
    assert SkillManager._read_skill_metadata(skill)["name"] == "alpha"
    skill.write_text("---\nname: omega\n---\nbody", encoding="utf-8")
    os.utime(skill, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert SkillManager._read_skill_metadata(skill)["name"] == "omega"


def test_unreadable_utf8_skill_is_skipped_without_failing_startup(tmp_path, isolated_kernel):
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(b"---\nname: \xff\n---\n")
    assert SkillManager._read_skill_metadata(skill) is None


def test_agent_recipe_is_immutable_but_each_session_gets_fresh_selection(isolated_kernel):
    identity = kernel.KernelIdentity.for_agent(project_dir="", work_mode="agent", definition="alpha",
                                               capabilities={}, configuration={})
    calls = []

    def prompt(language):
        calls.append(language)
        return "stable identity"

    definition = {"tools": ["read"], "skills": ["one"], "system_prompt": "alpha"}
    rails = {"context_engine_config": {"processors": ["compressor"]}}
    a = agent_recipe(identity, definition=definition, language="zh", prompt_factory=prompt, rail_config=rails)
    b = agent_recipe(identity, definition=definition, language="zh", prompt_factory=prompt, rail_config=rails)
    assert a is b and calls == ["zh"]
    with pytest.raises(FrozenInstanceError):
        a.identity_prompt = "mutated"
    a.definition_snapshot()["tools"].append("write")
    assert b.definition_snapshot()["tools"] == ["read"]
    a.rail_configuration()["context_engine_config"]["processors"].append("session mutation")
    assert b.rail_configuration()["context_engine_config"]["processors"] == ["compressor"]
    assert not hasattr(a, "session") and not hasattr(a, "permission_context")


@pytest.mark.asyncio
async def test_lazy_a2x_first_use_singleflight_and_scoped_leases(monkeypatch, isolated_kernel):
    from jiuwenswarm.agents.harness.team.a2x.lazy_client import lease_a2x_client
    from jiuwenswarm.agents.harness.team.a2x import client as client_module

    clients = []

    class Client:
        def __init__(self, **config):
            self.config = config
            self.closed = False
            clients.append(self)

        async def list_agents(self, dataset):
            await asyncio.sleep(0)
            return self.config["api_key"], dataset

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(client_module, "AsyncA2XRegistryClient", Client)

    async def make(key, **options):
        return (await lease_a2x_client({"react": {"a2x_registry": {"api_key": key, **options}}}))[0]

    a, b, c = await make("credential-a"), await make("credential-a"), await make("credential-b")
    assert clients == []
    result = await asyncio.gather(*[client.list_agents("dataset") for client in [a, b, c] * 8])
    assert len(clients) == 2
    assert result[0] == ("credential-a", "dataset") and result[2][0] == "credential-b"
    a._jiuwen_blank_agent_registration["service_id"] = "session-a"
    assert b._jiuwen_blank_agent_registration == {}
    await a.aclose()
    assert not any(client.closed for client in clients)
    assert await b.list_agents("still-open") == ("credential-a", "still-open")
    await b.aclose()
    await c.aclose()
    await isolated_kernel.close()
    assert all(client.closed for client in clients)


@pytest.mark.asyncio
async def test_teammate_registration_retains_startup_order_and_credential_isolation(monkeypatch, isolated_kernel):
    from jiuwenswarm.agents.harness.team.a2x import a2x_registry_runtime as registry

    registry.clear_blank_registration_cache_for_tests()
    calls = AsyncMock(return_value=SimpleNamespace(service_id="service"))
    config = {"distributed_mode": True, "role": "teammate", "dataset": "d", "endpoint": "tcp://127.0.0.1:42",
              "base_url": "http://127.0.0.1:8000", "api_key": "a", "ownership_file": False}
    a, b = SimpleNamespace(register_blank_agent=calls), SimpleNamespace(register_blank_agent=calls)
    await asyncio.gather(registry.register_blank_agent_if_teammate(a, config, source="startup"),
                         registry.register_blank_agent_if_teammate(b, config, source="startup"))
    assert calls.await_count == 1
    assert a._jiuwen_blank_agent_registration is not b._jiuwen_blank_agent_registration
    await registry.register_blank_agent_if_teammate(b, {**config, "api_key": "b"}, source="startup")
    assert calls.await_count == 2
