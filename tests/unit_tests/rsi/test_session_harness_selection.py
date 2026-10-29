"""Session RSI selection replaces the default using real plugin bindings."""

import asyncio
import errno
import json
import shutil
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rsi.plugin_catalog import register_harness_plugin
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime import extension_package_manager as catalog
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from tests.unit_tests.rsi.test_plugin_roundtrip import _agent, _make_plugin_package


@pytest.fixture
def harness_selection(tmp_path, rsi_catalog_workspace, monkeypatch):
    monkeypatch.setattr("jiuwenswarm.common.utils._workspace_base_dir", tmp_path / "user")
    packages = {}
    for version in ("aabbcc", "ddeeff", "112233"):
        package = tmp_path / version
        package.mkdir()
        (package / "manifest.json").write_text(json.dumps({
            "package_type": "plugin", "id": "native-baseline", "name": "Baseline",
            "prompt_sections": [
                {"name": "kernel_optimization_remedy_derivation", "content": {"en": version}},
                {"name": f"only_{version}", "content": {"en": version}},
            ],
        }), encoding="utf-8")
        name = f"rsi-harness-{version}"
        register_harness_plugin(package, name)
        packages[name] = package
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = _agent()
    yield adapter, packages
    snapshot = adapter._rsi_selected_package_snapshot
    if snapshot is not None:
        snapshot.cleanup()


def _content(adapter):
    return adapter._instance.system_prompt_builder.get_section(
        "kernel_optimization_remedy_derivation",
    ).content["en"]


async def _default(adapter, packages, name="rsi-harness-aabbcc"):
    await adapter.apply_rsi_harness_install_local(
        "activate", config_path=str(packages[name]), installation_id=name,
    )


async def _select(adapter, names):
    await adapter._unload_plugins_for_request({"plugin_names": names})
    await adapter._load_plugins_for_request({"plugin_names": names})


@pytest.mark.asyncio
async def test_selected_rsi_replaces_default_and_clearing_restores_it(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    await _select(adapter, ["rsi-harness-ddeeff"])
    assert _content(adapter) == "ddeeff"
    assert adapter._instance.system_prompt_builder.get_section("only_aabbcc") is None
    await _select(adapter, ["rsi-harness-112233"])
    assert _content(adapter) == "112233"
    assert adapter._instance.system_prompt_builder.get_section("only_ddeeff") is None
    await _select(adapter, [])
    assert _content(adapter) == "aabbcc"
    assert adapter._instance.system_prompt_builder.get_section("only_112233") is None


@pytest.mark.asyncio
async def test_same_selected_version_is_idempotent_and_counts_as_current_equipment(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    original = adapter._rsi_harness_load_record
    await _select(adapter, ["rsi-harness-aabbcc"])
    await _select(adapter, ["rsi-harness-aabbcc"])
    assert adapter._rsi_harness_load_record is original
    assert adapter._equipment_would_change({
        "agent_template_name": "", "plugin_names": ["rsi-harness-aabbcc"],
    }) == (False, "")


@pytest.mark.asyncio
async def test_global_change_preserves_selection_and_clear_uses_latest_default(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    await _select(adapter, ["rsi-harness-ddeeff"])
    await _default(adapter, packages, "rsi-harness-112233")
    assert _content(adapter) == "ddeeff"
    await _select(adapter, [])
    assert _content(adapter) == "112233"


@pytest.mark.asyncio
async def test_failed_selection_restores_previous_harness(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    await _select(adapter, ["rsi-harness-ddeeff"])
    broken = catalog.resolve_plugin_dir("rsi-harness-112233") / "manifest.json"
    payload = json.loads(broken.read_text(encoding="utf-8"))
    payload["prompt_sections"].append({"name": "invalid", "file": "missing.md"})
    broken.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception, match="missing.md"):
        await _select(adapter, ["rsi-harness-112233"])
    assert _content(adapter) == "ddeeff"
    await _select(adapter, [])
    assert _content(adapter) == "aabbcc"


@pytest.mark.asyncio
async def test_multiple_rsi_selections_rejected_without_changing_harness(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    original = adapter._rsi_harness_load_record
    with pytest.raises(ValueError, match="只能选择一个"):
        await _select(adapter, ["rsi-harness-ddeeff", "rsi-harness-112233"])
    assert adapter._rsi_harness_load_record is original
    assert _content(adapter) == "aabbcc"


@pytest.mark.asyncio
async def test_selection_without_global_default_can_be_cleared(harness_selection):
    adapter, _ = harness_selection
    await _select(adapter, ["rsi-harness-ddeeff"])
    await _select(adapter, [])
    assert adapter._instance.system_prompt_builder.get_section("kernel_optimization_remedy_derivation") is None


@pytest.mark.asyncio
async def test_uninstall_selected_rsi_restores_default(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    await _select(adapter, ["rsi-harness-ddeeff"])
    await adapter.unload_equipment_if_loaded("plugin_packages", "rsi-harness-ddeeff")
    assert _content(adapter) == "aabbcc"


@pytest.mark.asyncio
async def test_omitted_selection_is_preserved_and_saved_for_session_restore(harness_selection):
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_equipment

    adapter, packages = harness_selection
    await _default(adapter, packages)
    request = AgentRequest(request_id="selection", channel_id="web", session_id="rsi-selection", params={
        "plugin_names": ["rsi-harness-ddeeff"],
    })
    assert await adapter._ensure_chat_extensions(request) is None
    request.params = {"query": "next turn"}
    assert await adapter._ensure_chat_extensions(request) is None
    assert _content(adapter) == "ddeeff"
    assert get_session_equipment("rsi-selection")["plugin_names"] == ["rsi-harness-ddeeff"]
    request.params = {"plugin_names": []}
    assert await adapter._ensure_chat_extensions(request) is None
    assert _content(adapter) == "aabbcc"


@pytest.mark.asyncio
async def test_failed_first_selection_keeps_default_and_returns_chat_error(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    broken = catalog.resolve_plugin_dir("rsi-harness-ddeeff") / "manifest.json"
    payload = json.loads(broken.read_text(encoding="utf-8"))
    payload["prompt_sections"].append({"name": "invalid", "file": "missing.md"})
    broken.write_text(json.dumps(payload), encoding="utf-8")
    response = await adapter._ensure_chat_extensions(AgentRequest(
        request_id="broken-selection", channel_id="web", session_id="rsi-broken-selection",
        params={"plugin_names": ["rsi-harness-ddeeff"]},
    ))
    assert response.ok is False
    assert response.payload["event_type"] == "chat.error"
    assert _content(adapter) == "aabbcc"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,error", [
    ("TemporaryDirectory", PermissionError("temporary directory is not writable")),
    ("TemporaryDirectory", OSError(errno.ENOSPC, "no space left")),
    ("copytree", FileNotFoundError("plugin directory disappeared")),
    ("copytree", shutil.Error("package copy failed")),
])
async def test_snapshot_io_failure_returns_chat_error_without_unloading_default(
    harness_selection, monkeypatch, operation, error,
):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    original = adapter._rsi_harness_load_record

    def fail_io(*args, **kwargs):
        raise error

    monkeypatch.setattr(interface_deep, operation, fail_io)
    response = await adapter._ensure_chat_extensions(AgentRequest(
        request_id="snapshot-failure", channel_id="web", session_id="snapshot-failure",
        params={"plugin_names": ["rsi-harness-ddeeff"]},
    ))
    assert response.ok is False
    assert response.payload == {"event_type": "chat.error", "error": str(error)}
    assert adapter._rsi_harness_load_record is original
    assert _content(adapter) == "aabbcc"
    assert adapter._rsi_selected_plugin is None


@pytest.mark.asyncio
async def test_snapshot_manifest_read_failure_returns_chat_error(harness_selection, monkeypatch):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    original = adapter._rsi_harness_load_record
    read_text = Path.read_text

    def read_manifest(path, *args, **kwargs):
        if path.name == "manifest.json" and any(part.startswith("rsi_session_") for part in path.parts):
            raise PermissionError("snapshot manifest is not readable")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_manifest)
    response = await adapter._ensure_chat_extensions(AgentRequest(
        request_id="manifest-failure", channel_id="web", session_id="manifest-failure",
        params={"plugin_names": ["rsi-harness-ddeeff"]},
    ))
    assert response.ok is False
    assert response.payload == {"event_type": "chat.error", "error": "snapshot manifest is not readable"}
    assert adapter._rsi_harness_load_record is original
    assert _content(adapter) == "aabbcc"


@pytest.mark.asyncio
async def test_failed_replacement_and_rollback_return_chat_error(harness_selection, monkeypatch):
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_equipment

    adapter, packages = harness_selection
    await _default(adapter, packages)

    async def fail_load(path):
        raise RuntimeError("package unavailable")

    monkeypatch.setattr(adapter._instance, "load_plugin", fail_load)
    response = await adapter._ensure_chat_extensions(AgentRequest(
        request_id="rollback-failure", channel_id="web", session_id="rollback-failure",
        params={"plugin_names": ["rsi-harness-ddeeff"]},
    ))
    assert response.ok is False
    assert response.payload["event_type"] == "chat.error"
    assert "旧版本恢复失败" in response.payload["error"]
    assert adapter._rsi_harness_load_record is None
    assert adapter._rsi_selected_plugin is None
    assert get_session_equipment("rollback-failure") == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), TypeError("unexpected implementation error")])
async def test_snapshot_cancellation_and_unexpected_errors_propagate(harness_selection, monkeypatch, error):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    original = adapter._rsi_harness_load_record

    def fail_copy(*args, **kwargs):
        raise error

    monkeypatch.setattr(interface_deep, "copytree", fail_copy)
    with pytest.raises(type(error)):
        await adapter._ensure_chat_extensions(AgentRequest(
            request_id="cancelled-selection", channel_id="web", session_id="cancelled-selection",
            params={"plugin_names": ["rsi-harness-ddeeff"]},
        ))
    assert adapter._rsi_harness_load_record is original
    assert _content(adapter) == "aabbcc"


@pytest.mark.asyncio
async def test_selected_package_version_change_reloads_resources(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    await _select(adapter, ["rsi-harness-ddeeff"])
    manifest = catalog.resolve_plugin_dir("rsi-harness-ddeeff") / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version"] = "2"
    payload["prompt_sections"][0]["content"]["en"] = "edited"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    await _select(adapter, ["rsi-harness-ddeeff"])
    assert _content(adapter) == "edited"


@pytest.mark.asyncio
async def test_first_selection_of_edited_active_catalog_copy_loads_edited_version(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    manifest = catalog.resolve_plugin_dir("rsi-harness-aabbcc") / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version"] = "2"
    payload["prompt_sections"][0]["content"]["en"] = "edited"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    await _select(adapter, ["rsi-harness-aabbcc"])
    assert _content(adapter) == "edited"
    await _select(adapter, [])
    assert _content(adapter) == "aabbcc"


@pytest.mark.asyncio
async def test_failed_in_place_edit_preserves_selected_resources(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    await _select(adapter, ["rsi-harness-ddeeff"])
    manifest = catalog.resolve_plugin_dir("rsi-harness-ddeeff") / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version"] = "broken"
    payload["prompt_sections"].append({"name": "invalid", "file": "missing.md"})
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception, match="missing.md"):
        await _select(adapter, ["rsi-harness-ddeeff"])
    assert _content(adapter) == "ddeeff"
    assert adapter._rsi_harness_load_record is not None
    await _select(adapter, [])
    assert _content(adapter) == "aabbcc"


@pytest.mark.asyncio
async def test_adapter_cleanup_removes_selected_package_snapshot(harness_selection):
    from pathlib import Path

    adapter, packages = harness_selection
    await _default(adapter, packages)
    await _select(adapter, ["rsi-harness-ddeeff"])
    snapshot = Path(adapter._rsi_harness_config_path)
    assert snapshot.is_dir()
    await adapter.cleanup()
    assert not snapshot.exists()


@pytest.mark.asyncio
async def test_global_fanout_failure_restores_default_without_replacing_selection(harness_selection):
    adapter, packages = harness_selection
    await _default(adapter, packages)
    await _select(adapter, ["rsi-harness-ddeeff"])
    child = JiuWenSwarmDeepAdapter()
    child._instance = _agent()
    child._is_session_scoped_adapter = True
    await _default(child, packages)
    # A native resource conflict makes the second target fail after the first
    # has accepted a new default; rollback must restore defaults, not selections.
    from openjiuwen.core.single_agent.prompts.builder import PromptSection

    child._instance.system_prompt_builder.add_section(PromptSection(name="only_112233", content={"en": "host"}))
    adapter._session_adapters = {"child": child}
    with pytest.raises(Exception, match="only_112233"):
        await adapter.apply_rsi_harness_install(
            "activate", config_path=str(packages["rsi-harness-112233"]), installation_id="rsi-harness-112233",
        )
    assert _content(adapter) == "ddeeff"
    assert _content(child) == "aabbcc"
    await _select(adapter, [])
    assert _content(adapter) == "aabbcc"


@pytest.mark.asyncio
async def test_selection_keeps_original_plugin_displaced_until_default_is_restored(
    tmp_path, rsi_catalog_workspace, monkeypatch,
):
    monkeypatch.setattr("jiuwenswarm.common.utils._workspace_base_dir", tmp_path / "user")
    source = _make_plugin_package(tmp_path, "coding-guard")
    register_harness_plugin(source, "rsi-harness-aabbcc")
    register_harness_plugin(source, "rsi-harness-ddeeff")
    catalog.import_plugin_package({"path": str(source)})
    catalog.install_plugin_package({"id": "coding-guard"})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = _agent()
    await _select(adapter, ["coding-guard"])
    await adapter.apply_rsi_harness_install_local(
        "activate", config_path=str(source), installation_id="rsi-harness-aabbcc",
    )
    await _select(adapter, ["coding-guard", "rsi-harness-ddeeff"])
    assert adapter._instance.ability_manager.get("security_review") is not None
    assert "coding-guard" not in adapter._loaded_plugins
    assert await adapter._ensure_chat_extensions(AgentRequest(
        request_id="keep-original", channel_id="web", session_id="rsi-original-selection", params={},
    )) is None
    await _select(adapter, ["coding-guard"])
    assert adapter._rsi_harness_install_id == "rsi-harness-aabbcc"
    await adapter.apply_rsi_harness_install_local("deactivate", config_path="", installation_id="")
    assert "coding-guard" in adapter._loaded_plugins
    await adapter._instance.unload_extension(adapter._loaded_plugins["coding-guard"][0])


@pytest.mark.asyncio
@pytest.mark.parametrize("notify_runtime", [True, False])
@pytest.mark.parametrize("explicit_snapshot", [True, False])
async def test_uninstalled_displaced_plugin_does_not_block_chat_or_restore(
    tmp_path, rsi_catalog_workspace, monkeypatch, notify_runtime, explicit_snapshot,
):
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_equipment

    monkeypatch.setattr("jiuwenswarm.common.utils._workspace_base_dir", tmp_path / "user")
    source = _make_plugin_package(tmp_path, "coding-guard")
    catalog.import_plugin_package({"path": str(source)})
    catalog.install_plugin_package({"id": "coding-guard"})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = _agent()
    await _select(adapter, ["coding-guard"])
    await adapter.apply_rsi_harness_install_local(
        "activate", config_path=str(source), installation_id="rsi-harness-aabbcc",
    )
    request = AgentRequest(request_id="uninstall-displaced", channel_id="web", session_id="displaced", params={})
    assert await adapter._ensure_chat_extensions(request) is None
    saved = get_session_equipment("displaced")
    assert saved["plugin_names"] == ["coding-guard"]
    if notify_runtime:
        child = JiuWenSwarmDeepAdapter()
        child._is_session_scoped_adapter = True
        child._rsi_displaced_plugins = dict(adapter._rsi_displaced_plugins)
        adapter._session_adapters = {"child": child}
        await adapter.unload_equipment_if_loaded("plugin_packages", "coding-guard")
        assert "coding-guard" not in adapter._rsi_displaced_plugins
        assert "coding-guard" not in child._rsi_displaced_plugins
    catalog.uninstall_plugin_package({"id": "coding-guard"})
    assert adapter._current_plugin_names() == []
    if explicit_snapshot:
        request.params = saved
    assert await adapter._ensure_chat_extensions(request) is None
    assert get_session_equipment("displaced")["plugin_names"] == []
    await adapter.apply_rsi_harness_install_local("deactivate", config_path="", installation_id="")
    assert adapter._loaded_plugins == {}
    assert adapter._instance.ability_manager.get("security_review") is None


@pytest.mark.asyncio
async def test_external_uninstall_does_not_restore_displaced_package_without_chat(
    tmp_path, rsi_catalog_workspace,
):
    source = _make_plugin_package(tmp_path, "coding-guard")
    catalog.import_plugin_package({"path": str(source)})
    catalog.install_plugin_package({"id": "coding-guard"})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = _agent()
    await _select(adapter, ["coding-guard"])
    await adapter.apply_rsi_harness_install_local(
        "activate", config_path=str(source), installation_id="rsi-harness-aabbcc",
    )
    catalog.uninstall_plugin_package({"id": "coding-guard"})
    await adapter.apply_rsi_harness_install_local("deactivate", config_path="", installation_id="")
    assert adapter._loaded_plugins == {}
    assert adapter._rsi_displaced_plugins == {}
