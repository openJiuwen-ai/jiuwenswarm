import asyncio
from unittest.mock import AsyncMock

import pytest

from openjiuwen.harness import rails as core_rails

from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder


agent_memory_provider = pytest.importorskip("jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider")


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("scene", ["agent", "code"])
@pytest.mark.parametrize("supports_experience", [False, True])
def test_sdk_experience_mounts_existing_core_rail(monkeypatch, enabled, scene, supports_experience):
    sync_experience = AsyncMock()
    provider_options = {}

    class Provider:
        def __init__(self, **kwargs):
            provider_options.update(kwargs)
            self.sync_experience = sync_experience

        async def sync_experience(self, *args):
            await sync_experience(*args)

        def is_available(self):
            return True

    class LegacyRail:
        def __init__(self, provider, **options):
            self.received_options = options

    class ExperienceRail(LegacyRail):
        def __init__(self, provider, *, sync_experience=None, **options):
            super().__init__(provider, **options)
            self.received_options["sync_experience"] = sync_experience

    rail_type = ExperienceRail if supports_experience else LegacyRail
    rail_name = "JiuwenMemoryCodingRail" if scene == "code" else "JiuwenMemoryRail"
    monkeypatch.setattr(core_rails, rail_name, rail_type, raising=False)
    monkeypatch.setattr(agent_memory_provider, "AgentMemoryMemoryProvider", Provider)

    config = {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": {
        "mode": "sdk",
        "space": "project-a",
        "experience_space": "own" if enabled else "",
    }}}}

    rail = jiuwen_rail_builder.build_jiuwen_memory_rail(config, scene=scene)
    assert isinstance(rail, rail_type)
    if scene == "code":
        assert type(rail) is rail_type
    else:
        assert type(rail).__bases__ == (rail_type,)
    assert rail.received_options.get("sync_experience") is (
        sync_experience if enabled and supports_experience else None
    )
    if enabled:
        assert provider_options["experience_space"] == "project-a"
        assert provider_options["register_experience_jobs"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("experience_space", ["own", "shared-experience"])
async def test_sdk_experience_write_uses_the_configured_space(monkeypatch, tmp_path, experience_space):
    from jiuwen_memory.common.errors import PermissionDeniedError
    from jiuwen_memory.common.type_def import MemoryUnit, Scope, Segment
    appraiser = pytest.importorskip("jiuwen_memory.construction.appraiser_impl.experience_appraiser")
    extractor = pytest.importorskip("jiuwen_memory.construction.extractor_impl.experience_extractor")

    providers = []

    class RecordingProvider(agent_memory_provider.AgentMemoryMemoryProvider):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            providers.append(self)

    monkeypatch.setattr(agent_memory_provider, "AgentMemoryMemoryProvider", RecordingProvider)
    monkeypatch.setattr(appraiser.ExperienceAppraiser, "appraise", lambda *_args, **_kwargs: None)
    source = Scope(org="local-test", space="project-a", user="tester", agent="jiuwenswarm")
    derived = MemoryUnit(
        id="own-experience-test",
        scope=Scope(org=source.org, space=source.space),
        segments=[Segment(content="Read UTF-8 CSV with BOM before grouping records.")],
        system_metadata={"memory_type": "experience", "category": "other"},
    )
    extracted = asyncio.Event()
    loop = asyncio.get_running_loop()

    def extract(*_args, **_kwargs):
        loop.call_soon_threadsafe(extracted.set)
        return [derived]

    monkeypatch.setattr(extractor.ExperienceExtractor, "extract", extract)
    config = {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": {
        "mode": "sdk",
        "scope_id": source.org,
        "space": source.space,
        "user_id": source.user,
        "experience_space": experience_space,
        "sdk": {
            "embedder": {"default": {"target": "hashing"}},
            "markdown_store": {"default": {"target": "local", "params": {"root": str(tmp_path)}}},
        },
    }}}}

    rail = jiuwen_rail_builder.build_jiuwen_memory_rail(config, shared_document_root=True)
    assert rail is not None
    provider, = providers
    await provider.initialize(
        user_id=source.user, scope_id=source.org, agent_id=source.agent, session_id="csv",
        register_experience_jobs=False,
    )
    target = tmp_path / "memory" / "experience" / "EXPERIENCE.md"
    try:
        if experience_space == "own":
            await provider.add_trajectory("CSV task completed with tool evidence.", {}, "csv")
            async with asyncio.timeout(5):
                await extracted.wait()
                while not target.is_file():
                    await asyncio.sleep(0.01)
            assert derived.content in target.read_text(encoding="utf-8")
        else:
            with pytest.raises(PermissionDeniedError):
                await provider.add_trajectory("CSV task completed with tool evidence.", {}, "csv")
            assert not extracted.is_set()
            assert not target.exists()
    finally:
        await provider.shutdown()
