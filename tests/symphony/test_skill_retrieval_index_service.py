from __future__ import annotations

from dataclasses import replace
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from jiuwenswarm.symphony.skill_retrieval.build_coordinator import (
    cancel_skill_index_build,
    start_skill_index_build,
)
from jiuwenswarm.symphony.skill_retrieval import api as skill_retrieval_api
from jiuwenswarm.symphony.skill_retrieval.api import build_skill_index
from jiuwenswarm.symphony.skill_retrieval.config import (
    BuildSettings,
    LLMSettings,
    RetrieveSettings,
    SkillRetrievalSettings,
)
from jiuwenswarm.symphony.skill_retrieval.dispatch_imports import dispatch_import_path
from jiuwenswarm.symphony.skill_retrieval.index_service import SkillIndexService, expected_index_fingerprint
from jiuwenswarm.symphony.skill_retrieval.inventory import scan_skill_inventory


def _write_skill(root: Path, dirname: str, *, name: str | None = None, description: str = "desc") -> None:
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True)
    skill_name = name or dirname
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: {description}\n---\n\nBody\n",
        encoding="utf-8",
    )


class _InventoryManager:
    def __init__(self, skills_dir: Path) -> None:
        self._skills_dir = skills_dir

    @staticmethod
    def get_local_skills() -> list[dict]:
        return []

    @staticmethod
    def get_installed_plugins() -> list[dict]:
        return [
            {"name": "disabled-plugin", "enabled": False, "skills": ["disabled-plugin"]},
            {"name": "enabled-plugin", "enabled": True, "skills": ["enabled-plugin"]},
        ]

    @staticmethod
    def get_skill_enabled(name: str) -> bool:
        return name != "disabled-skill"


def test_scan_skill_inventory_includes_all_installed_skills(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "disabled-plugin")
    _write_skill(skills_dir, "disabled-skill")
    _write_skill(skills_dir, "enabled-plugin")

    inventory = scan_skill_inventory(_InventoryManager(skills_dir))

    assert [item.name for item in inventory.items] == [
        "disabled-plugin",
        "disabled-skill",
        "enabled-plugin",
    ]


def test_index_fingerprint_tracks_only_skill_inventory(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "enabled-skill")
    inventory = scan_skill_inventory(SimpleNamespace(_skills_dir=skills_dir))
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=tmp_path / "artifact",
        llm=LLMSettings(model="model-a", api_key="key-a", base_url="https://api-a.example"),
        build=BuildSettings(max_depth=4),
        retrieve=RetrieveSettings(),
    )

    changed_llm = replace(
        settings,
        llm=LLMSettings(model="model-b", api_key="key-b", base_url="https://api-b.example", seed=123),
    )
    changed_build = replace(settings, build=BuildSettings(max_depth=5))
    _write_skill(skills_dir, "another-skill")
    changed_inventory = scan_skill_inventory(SimpleNamespace(_skills_dir=skills_dir))

    assert expected_index_fingerprint(inventory, changed_llm) == expected_index_fingerprint(inventory, settings)
    assert expected_index_fingerprint(inventory, changed_build) == expected_index_fingerprint(inventory, settings)
    assert expected_index_fingerprint(changed_inventory, settings) != expected_index_fingerprint(inventory, settings)


def test_status_and_tree_keep_index_available_when_build_llm_changes(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "enabled-skill")
    manager = SimpleNamespace(_skills_dir=skills_dir)
    inventory = scan_skill_inventory(manager)
    artifact_root = tmp_path / "artifact"
    index_dir = artifact_root / "index"
    index_dir.mkdir(parents=True)
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model-a", api_key="key-a", base_url="https://api-a.example"),
        build=BuildSettings(max_depth=4),
        retrieve=RetrieveSettings(),
    )
    changed_llm = replace(
        settings,
        llm=LLMSettings(model="model-b", api_key="key-b", base_url="https://api-b.example"),
    )
    (index_dir / "tree_index.yaml").write_text("nodes: []\n", encoding="utf-8")
    (index_dir / "catalog.jsonl").write_text("", encoding="utf-8")
    (index_dir / "manifest.json").write_text(
        json.dumps({"item_paths": inventory.item_paths}),
        encoding="utf-8",
    )
    (artifact_root / "state.json").write_text(
        json.dumps(
            {
                "fingerprint": expected_index_fingerprint(inventory, settings),
                "indexed_count": inventory.count,
                "build": {"status": "success", "stage": "success", "progress": 1.0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: changed_llm,
    )

    status = SkillIndexService(manager).status()
    tree = SkillIndexService(manager).tree(language="zh")

    assert status["index_exists"] is True
    assert status["fresh"] is True
    assert status["build_status"] == "success"
    assert tree["success"] is True
    assert tree["index_dir"] == str(index_dir)


def test_tree_disabled_message_uses_language_without_markdown_heading(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    settings = SkillRetrievalSettings(
        enabled=False,
        artifact_root=tmp_path / "artifact",
        llm=LLMSettings(model="", api_key="", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )

    zh = SkillIndexService(SimpleNamespace(_skills_dir=skills_dir)).tree(language="zh")
    en = SkillIndexService(SimpleNamespace(_skills_dir=skills_dir)).tree(language="en")

    assert zh["success"] is False
    assert "技能检索当前已关闭" in zh["result"]
    assert not zh["result"].lstrip().startswith("#")
    assert "Skill retrieval is currently disabled" in en["result"]
    assert not en["result"].lstrip().startswith("#")


def test_build_index_with_no_skills_clears_stale_index_and_records_failure(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    artifact_root = tmp_path / "artifact"
    index_dir = artifact_root / "index"
    index_dir.mkdir(parents=True)
    (index_dir / "tree_index.yaml").write_text("nodes: []\n", encoding="utf-8")
    (index_dir / "catalog.jsonl").write_text("", encoding="utf-8")
    (index_dir / "manifest.json").write_text(json.dumps({"item_paths": ["/old/skill"]}), encoding="utf-8")
    (artifact_root / "state.json").write_text(
        json.dumps({"fingerprint": "old", "indexed_count": 1}),
        encoding="utf-8",
    )
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="", api_key="", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )

    result = SkillIndexService(SimpleNamespace(_skills_dir=skills_dir)).build_index(force=True)

    assert result["success"] is False
    assert not index_dir.exists()
    state = json.loads((artifact_root / "state.json").read_text(encoding="utf-8"))
    assert state["build"]["status"] == "failed"
    assert "No installed skills" in state["build"]["error"]


def test_cancel_without_running_build_does_not_write_cancel_state(monkeypatch, tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifact"
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model", api_key="key", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.build_coordinator.load_settings",
        lambda: settings,
    )

    result = cancel_skill_index_build(SimpleNamespace(_skills_dir=tmp_path / "skills"))

    assert result["success"] is False
    assert result["build_status"] == "idle"
    assert not (artifact_root / "state.json").exists()


def test_background_build_marks_shared_state(monkeypatch, tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifact"
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model", api_key="key", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.build_coordinator.load_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )
    release = threading.Event()
    started = threading.Event()

    def fake_build_index(self, *, force=False, cancel_check=None, source="manual"):
        started.set()
        release.wait(timeout=1)
        return {"success": True, "result": "# ok"}

    monkeypatch.setattr(SkillIndexService, "build_index", fake_build_index)
    manager = SimpleNamespace(_skills_dir=tmp_path / "skills")

    result = start_skill_index_build(manager, force=True, source="web")
    assert started.wait(timeout=1)

    assert result["success"] is True
    assert result["background"] is True
    state = json.loads((artifact_root / "state.json").read_text(encoding="utf-8"))
    assert state["build"]["status"] == "running"
    assert state["build"]["stage"] == "queued"

    cancel_result = cancel_skill_index_build(manager)
    release.set()
    assert cancel_result["success"] is True
    assert cancel_result["build_status"] == "cancelled"
    state = json.loads((artifact_root / "state.json").read_text(encoding="utf-8"))
    assert state["build"]["status"] == "cancelled"
    assert state["build"]["stage"] == "cancelled"
    assert state["build"]["cancel_requested"] is False


def test_force_build_bypasses_fresh_index_reuse(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "enabled-skill")
    manager = SimpleNamespace(_skills_dir=skills_dir)
    artifact_root = tmp_path / "artifact"
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model", api_key="key", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    inventory = scan_skill_inventory(manager)
    expected = expected_index_fingerprint(inventory, settings)
    index_dir = artifact_root / "index"
    index_dir.mkdir(parents=True)
    (index_dir / "tree_index.yaml").write_text("nodes: []\n", encoding="utf-8")
    (index_dir / "catalog.jsonl").write_text("", encoding="utf-8")
    (index_dir / "manifest.json").write_text(
        json.dumps({"item_paths": inventory.item_paths}),
        encoding="utf-8",
    )
    (artifact_root / "state.json").write_text(
        json.dumps({"fingerprint": expected, "indexed_count": inventory.count}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )
    monkeypatch.setattr(SkillIndexService, "_check_build_llm_access", staticmethod(lambda settings: None))
    calls: list[str] = []

    def fake_run_dispatch_build(*, settings, inventory, output_dir):
        calls.append("build")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "tree_index.yaml").write_text("nodes: []\n", encoding="utf-8")
        (output_dir / "catalog.jsonl").write_text("", encoding="utf-8")
        (output_dir / "manifest.json").write_text(
            json.dumps({"item_paths": inventory.item_paths}),
            encoding="utf-8",
        )

    monkeypatch.setattr(SkillIndexService, "_run_dispatch_build", staticmethod(fake_run_dispatch_build))

    result = SkillIndexService(manager).build_index(force=True)

    assert result["success"] is True
    assert calls == ["build"]
    state = json.loads((artifact_root / "state.json").read_text(encoding="utf-8"))
    assert state["build"]["status"] == "success"


def test_missing_llm_config_records_failure(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "enabled-skill")
    artifact_root = tmp_path / "artifact"
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="", api_key="", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )

    result = SkillIndexService(SimpleNamespace(_skills_dir=skills_dir)).build_index(force=True)

    assert result["success"] is False
    assert "requires a model and API key" in result["result"]
    state = json.loads((artifact_root / "state.json").read_text(encoding="utf-8"))
    assert state["build"]["status"] == "failed"
    assert state["build"]["stage"] == "llm_config"


def test_build_fails_when_llm_access_check_fails(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "enabled-skill")
    artifact_root = tmp_path / "artifact"
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model", api_key="bad-key", base_url="https://example.invalid"),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )

    def fail_llm_access(settings):
        raise RuntimeError("Skill index build model is not reachable or rejected the request: unauthorized")

    monkeypatch.setattr(SkillIndexService, "_check_build_llm_access", staticmethod(fail_llm_access))

    result = SkillIndexService(SimpleNamespace(_skills_dir=skills_dir)).build_index(force=True)

    assert result["success"] is False
    assert "not reachable" in result["result"]
    assert not (artifact_root / "index").exists()
    state = json.loads((artifact_root / "state.json").read_text(encoding="utf-8"))
    assert state["build"]["status"] == "failed"
    assert state["build"]["stage"] == "llm_check"
    assert "not reachable" in state["build"]["error"]


def test_llm_access_check_uses_tree_builder_runtime_imports(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "enabled-skill")
    manager = SimpleNamespace(_skills_dir=skills_dir)
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=tmp_path / "artifact",
        llm=LLMSettings(model="model", api_key="key", base_url="https://example.invalid"),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )

    with dispatch_import_path():
        from indexing.tree.llm_runtime import TreeLLMRuntime
        from indexing.workflows.index_builder import IndexBuilder

        def fake_build(*, item_paths, output_dir, item_type, config):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "tree_index.yaml").write_text("nodes: []\n", encoding="utf-8")
            (output_dir / "catalog.jsonl").write_text("", encoding="utf-8")
            (output_dir / "manifest.json").write_text(
                json.dumps({"item_paths": list(item_paths)}),
                encoding="utf-8",
            )

        monkeypatch.setattr(TreeLLMRuntime, "call_llm_json", lambda self, prompt, max_retries=3: {"ok": True})
        monkeypatch.setattr(IndexBuilder, "build", staticmethod(fake_build))

    result = SkillIndexService(manager).build_index(force=True)

    assert result["success"] is True


def test_status_ignores_success_state_when_index_artifacts_are_deleted(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "enabled-skill")
    artifact_root = tmp_path / "artifact"
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model", api_key="key", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    inventory = scan_skill_inventory(SimpleNamespace(_skills_dir=skills_dir))
    artifact_root.mkdir(parents=True)
    (artifact_root / "state.json").write_text(
        json.dumps(
            {
                "fingerprint": expected_index_fingerprint(inventory, settings),
                "indexed_count": inventory.count,
                "build": {"status": "success", "stage": "success", "progress": 1.0},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )

    status = SkillIndexService(SimpleNamespace(_skills_dir=skills_dir)).status()
    tree = SkillIndexService(SimpleNamespace(_skills_dir=skills_dir)).tree(language="zh")

    assert status["index_exists"] is False
    assert status["fresh"] is False
    assert status["build_status"] == "idle"
    assert status["build_logs"] == []
    assert "No usable skill index" in status["build_message"]
    assert tree["success"] is False
    assert tree["nodes"] == []


def test_api_status_repairs_interrupted_running_state(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    artifact_root = tmp_path / "artifact"
    artifact_root.mkdir()
    (artifact_root / "state.json").write_text(
        json.dumps({"build": {"status": "running", "stage": "build", "progress": 0.5}}),
        encoding="utf-8",
    )
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model", api_key="key", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(skill_retrieval_api, "_STARTUP_REPAIR_DONE", False)
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.build_coordinator.load_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )

    status = skill_retrieval_api.get_skill_retrieval_status(SimpleNamespace(_skills_dir=skills_dir))

    assert status["build_status"] == "failed"
    assert status["build_stage"] == "interrupted"
    assert "interrupted" in status["build_error"]


def test_api_status_keeps_active_build_running(monkeypatch, tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifact"
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model", api_key="key", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(skill_retrieval_api, "_STARTUP_REPAIR_DONE", False)
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.build_coordinator.load_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )
    release = threading.Event()
    started = threading.Event()
    manager = SimpleNamespace(_skills_dir=tmp_path / "skills")

    def fake_build_index(self, *, force=False, cancel_check=None, source="manual"):
        started.set()
        release.wait(timeout=1)
        return {"success": True, "result": "# ok"}

    monkeypatch.setattr(SkillIndexService, "build_index", fake_build_index)

    start_skill_index_build(manager, force=True, source="web")
    assert started.wait(timeout=1)
    status = skill_retrieval_api.get_skill_retrieval_status(manager)
    release.set()

    assert status["build_status"] == "running"
    assert status["build_stage"] == "queued"


def test_build_skill_index_api_waits_for_shared_background_build(monkeypatch, tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifact"
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model", api_key="key", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.build_coordinator.load_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )
    calls: list[tuple[bool, str]] = []
    started = threading.Event()
    release = threading.Event()
    manager = SimpleNamespace(_skills_dir=tmp_path / "skills")

    def fake_build_index(self, *, force=False, cancel_check=None, source="tool"):
        calls.append((force, source))
        started.set()
        release.wait(timeout=1)
        return {"success": True, "result": "# Skill Retrieval Index\n\nDone."}

    monkeypatch.setattr(SkillIndexService, "build_index", fake_build_index)
    monkeypatch.setattr(
        SkillIndexService,
        "status",
        lambda self: {"build_status": "success", "index_exists": True, "fresh": True},
    )

    web_result = start_skill_index_build(manager, force=True, source="web")
    assert started.wait(timeout=1)
    state = json.loads((artifact_root / "state.json").read_text(encoding="utf-8"))
    assert web_result["build_status"] == "running"
    assert state["build"]["status"] == "running"

    result_box: list[dict] = []
    tool_thread = threading.Thread(
        target=lambda: result_box.append(build_skill_index(manager, force=True, source="tool"))
    )
    tool_thread.start()
    release.set()
    tool_thread.join(timeout=1)

    assert result_box == [
        {
            "success": True,
            "result": (
                "# Skill Index Build\n\n"
                "Skill index build completed. You can now call `skill_branch_explore` "
                "or `skill_branch_peek` to inspect installed skills."
            ),
        }
    ]
    assert calls == [(True, "web")]


def test_tree_rejects_stale_manifest_and_uses_requested_language(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "current-skill")
    artifact_root = tmp_path / "artifact"
    index_dir = artifact_root / "index"
    index_dir.mkdir(parents=True)
    (index_dir / "tree_index.yaml").write_text(
        "nodes:\n"
        "  - cid: old\n"
        "    type: leaf\n"
        "    worker_id: old-skill\n",
        encoding="utf-8",
    )
    (index_dir / "catalog.jsonl").write_text("", encoding="utf-8")
    (index_dir / "manifest.json").write_text(json.dumps({"item_paths": ["/old/skill"]}), encoding="utf-8")
    (artifact_root / "state.json").write_text(json.dumps({"fingerprint": "old"}), encoding="utf-8")
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model", api_key="key", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )

    zh = SkillIndexService(SimpleNamespace(_skills_dir=skills_dir)).tree(language="zh")
    en = SkillIndexService(SimpleNamespace(_skills_dir=skills_dir)).tree(language="en")

    assert zh["success"] is False
    assert zh["nodes"] == []
    assert "# 技能索引树" not in zh["result"]
    assert "当前没有可用" in zh["result"]
    assert en["success"] is False
    assert en["nodes"] == []
    assert "# Skill Index Tree" not in en["result"]
    assert "No usable" in en["result"]


def test_build_error_normalizes_non_streaming_remote_model_error(monkeypatch, tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "enabled-skill")
    artifact_root = tmp_path / "artifact"
    settings = SkillRetrievalSettings(
        enabled=True,
        artifact_root=artifact_root,
        llm=LLMSettings(model="model", api_key="key", base_url=""),
        build=BuildSettings(),
        retrieve=RetrieveSettings(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.skill_retrieval.index_service.load_settings",
        lambda: settings,
    )
    monkeypatch.setattr(SkillIndexService, "_check_build_llm_access", staticmethod(lambda settings: None))

    def raise_remote_error(*, settings, inventory, output_dir):
        raise RuntimeError("set to false for non-streaming calls")

    monkeypatch.setattr(SkillIndexService, "_run_dispatch_build", staticmethod(raise_remote_error))

    result = SkillIndexService(SimpleNamespace(_skills_dir=skills_dir)).build_index(force=True)

    assert result["success"] is False
    assert "non-streaming LLM calls" in result["result"]
    state = json.loads((artifact_root / "state.json").read_text(encoding="utf-8"))
    assert "non-streaming LLM calls" in state["build"]["error"]
