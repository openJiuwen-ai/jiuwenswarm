# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for skills.evolution.archives / rollback / rebuild host adapters."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter import evolution_version as evolution_version_ctl
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_manager import _DISK_ONLY_EVOLUTION_METHODS


def _write_skill(tmp_path: Path, name: str) -> Path:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text(
        f"---\nname: {name}\nversion: 1.0.0\n---\n# {name}\n",
        encoding="utf-8",
    )
    skill_dir.joinpath("evolutions.json").write_text(
        json.dumps(
            {
                "skill_id": name,
                "version": "v1.0.0",
                "updated_at": "live",
                "entries": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return skills_dir


def _write_pair(tmp_path: Path, name: str, version: str, body: str) -> None:
    archive = tmp_path / "skills" / name / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    archive.joinpath(f"SKILL.{version}.md").write_text(body, encoding="utf-8")
    archive.joinpath(f"evolutions.{version}.json").write_text(
        json.dumps(
            {
                "skill_id": name,
                "version": version,
                "updated_at": "archived",
                "entries": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_disk_only_methods_exclude_rebuild():
    assert "skills.evolution.archives" in _DISK_ONLY_EVOLUTION_METHODS
    assert "skills.evolution.rollback" in _DISK_ONLY_EVOLUTION_METHODS
    assert "skills.evolution.rebuild" not in _DISK_ONLY_EVOLUTION_METHODS


def test_safe_path_name_rejects_traversal():
    with pytest.raises(ValueError):
        evolution_version_ctl.safe_path_name("../evil", "skill")


def test_skill_md_fingerprint_changes_with_content(tmp_path: Path):
    path = tmp_path / "SKILL.md"
    path.write_text("a", encoding="utf-8")
    first = evolution_version_ctl.skill_md_fingerprint(str(path))
    path.write_text("b", encoding="utf-8")
    second = evolution_version_ctl.skill_md_fingerprint(str(path))
    assert first is not None and second is not None
    assert first != second


@pytest.mark.anyio
async def test_handle_skills_evolution_archives_lists_versions(tmp_path, monkeypatch):
    skills_dir = _write_skill(tmp_path, "demo-skill")
    _write_pair(tmp_path, "demo-skill", "v1.0.0", "# old\n")
    adapter = JiuWenSwarmDeepAdapter()
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [str(skills_dir)])

    result = await adapter.handle_skills_evolution_archives({"name": "demo-skill"})
    assert result["name"] == "demo-skill"
    assert "SKILL.v1.0.0.md" in result["versions"]


@pytest.mark.anyio
async def test_handle_skills_evolution_rollback_latest(tmp_path, monkeypatch):
    skills_dir = _write_skill(tmp_path, "demo-skill")
    skill_md = tmp_path / "skills" / "demo-skill" / "SKILL.md"
    skill_md.write_text("# current\n", encoding="utf-8")
    _write_pair(tmp_path, "demo-skill", "v1.0.0", "# archived-body\n")

    adapter = JiuWenSwarmDeepAdapter()
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [str(skills_dir)])

    result = await adapter.handle_skills_evolution_rollback(
        {"name": "demo-skill", "version": "latest"}
    )
    assert result["success"] is True
    assert result["rolled_back"] is True
    assert skill_md.read_text(encoding="utf-8") == "# archived-body\n"
    live = json.loads(
        (tmp_path / "skills" / "demo-skill" / "evolutions.json").read_text(encoding="utf-8")
    )
    assert live["entries"] == []


@pytest.mark.anyio
async def test_handle_skills_evolution_rollback_lists_when_version_omitted(tmp_path, monkeypatch):
    skills_dir = _write_skill(tmp_path, "demo-skill")
    _write_pair(tmp_path, "demo-skill", "v1.2.0", "# v120\n")
    adapter = JiuWenSwarmDeepAdapter()
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [str(skills_dir)])

    result = await adapter.handle_skills_evolution_rollback({"name": "demo-skill"})
    assert result["success"] is True
    assert result["rolled_back"] is False
    assert result["versions"]


@pytest.mark.anyio
async def test_generate_evolution_merge_version_fingerprint_gate(tmp_path, monkeypatch):
    skills_dir = _write_skill(tmp_path, "demo-skill")
    skill_md = tmp_path / "skills" / "demo-skill" / "SKILL.md"
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = object()  # pylint: disable=protected-access
    adapter._model = object()  # pylint: disable=protected-access
    adapter._config_cache = {"evolution": {"auto_save": True}}  # pylint: disable=protected-access
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [str(skills_dir)])
    monkeypatch.setattr(adapter, "_bind_request_env_overlay", lambda: (None, None, None))
    monkeypatch.setattr(adapter, "_reset_request_env_bindings", lambda *_a, **_k: None)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "cn")
    monkeypatch.setattr(adapter, "_resolve_model_name", lambda: "test-model")
    monkeypatch.setattr(adapter, "ensure_instance", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_execute_merge_version_rewrite",
        AsyncMock(return_value=True),
    )

    # Rewrite succeeds but file unchanged → must fail before complete_rebuild
    with pytest.raises(ValueError, match="未更新"):
        await adapter.generate_evolution_merge_version({"name": "demo-skill"})

    assert skill_md.read_text(encoding="utf-8").startswith("---")


def test_extra_trusted_dirs_for_skill_md(tmp_path):
    skill_md = tmp_path / "office-claw" / "skills" / "beer" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("# beer\n", encoding="utf-8")
    extra = evolution_version_ctl.extra_trusted_dirs_for_skill_md(str(skill_md))
    assert str((tmp_path / "office-claw" / "skills").resolve()) in extra
    assert str(skill_md.parent.resolve()) in extra


@pytest.mark.anyio
async def test_generate_evolution_merge_version_does_not_seed_trusted_dirs(
    tmp_path, monkeypatch
):
    """Direct-LLM rebuild writes via Path.write_text; no permission trusted_dirs seed."""
    skills_dir = _write_skill(tmp_path, "demo-skill")
    skill_md = tmp_path / "skills" / "demo-skill" / "SKILL.md"
    captured: list[list[str]] = []

    class _Rail:
        def __init__(self) -> None:
            self._engine = SimpleNamespace(trusted_dirs=[])

        def set_trusted_dirs(self, dirs: list[str] | None) -> None:
            captured.append(list(dirs or []))
            self._engine.trusted_dirs = list(dirs or [])

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = object()  # pylint: disable=protected-access
    adapter._model = object()  # pylint: disable=protected-access
    adapter._permission_rail = _Rail()  # pylint: disable=protected-access
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [str(skills_dir)])
    monkeypatch.setattr(adapter, "_bind_request_env_overlay", lambda: (None, None, None))
    monkeypatch.setattr(adapter, "_reset_request_env_bindings", lambda *_a, **_k: None)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "cn")
    monkeypatch.setattr(adapter, "_resolve_model_name", lambda: "test-model")
    monkeypatch.setattr(adapter, "ensure_instance", AsyncMock())
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.evolution_version.prepare_rebuild_followup",
        AsyncMock(
            return_value={
                "ok": True,
                "followup_prompt": "rebuild",
                "rebuild_context": {
                    "skill_md_path": str(skill_md),
                    "archive_pair": True,
                    "subject_kind": "skill",
                },
            }
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.evolution_version.finalize_rebuild_followup",
        AsyncMock(
            return_value={"ok": True, "new_version": "1.0.1", "cleared": True}
        ),
    )

    async def _rewrite(**_kwargs: Any) -> bool:
        skill_md.write_text("# rebuilt\n", encoding="utf-8")
        return True

    monkeypatch.setattr(adapter, "_execute_merge_version_rewrite", _rewrite)

    result = await adapter.generate_evolution_merge_version(
        {"name": "demo-skill", "skill_path": str(skill_md)}
    )
    assert result["success"] is True
    assert captured == []


@pytest.mark.anyio
async def test_execute_merge_version_rewrite_direct_llm_writes_skill_md(
    tmp_path, monkeypatch
):
    skills_dir = _write_skill(tmp_path, "demo-skill")
    skill_md = skills_dir / "demo-skill" / "SKILL.md"
    rebuilt = (
        "---\nname: demo-skill\nversion: 1.0.0\n---\n# demo-skill\n\nrebuilt body\n"
    )

    class _FakeModel:
        async def invoke(self, _messages, **_kwargs):
            return SimpleNamespace(content=f"```markdown\n{rebuilt}\n```")

    # CI openjiuwen may lack build_rebuild_llm_direct_prompt; stub the module
    # attribute so the in-function ``from ... import`` succeeds.
    import openjiuwen.harness.rails.evolution.commands as evolution_commands

    def _stub_direct_prompt(
        *,
        subject: dict[str, Any],
        current_skill_md: str,
        user_intent: str | None = None,
        rebuild_context: dict[str, Any] | None = None,
        language: str = "cn",
    ) -> tuple[str, str]:
        _ = (subject, user_intent, rebuild_context, language)
        return (
            "You rebuild Skill definition files (SKILL.md).",
            f"Current SKILL.md:\n{current_skill_md}\n",
        )

    monkeypatch.setattr(
        evolution_commands,
        "build_rebuild_llm_direct_prompt",
        _stub_direct_prompt,
        raising=False,
    )

    adapter = JiuWenSwarmDeepAdapter()
    adapter._model = _FakeModel()  # pylint: disable=protected-access
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "cn")

    ok = await adapter._execute_merge_version_rewrite(  # pylint: disable=protected-access
        skill_md_path=str(skill_md),
        rebuild_context={
            "skill_md_path": str(skill_md),
            "records": [
                {
                    "record_id": "ev_1",
                    "summary": "add note",
                    "target": "skill",
                    "section": "Instructions",
                    "score": 0.9,
                    "content": "always mention UV",
                }
            ],
        },
        subject={"kind": "skill", "name": "demo-skill"},
    )
    assert ok is True
    text = skill_md.read_text(encoding="utf-8")
    assert "rebuilt body" in text
    assert text.startswith("---")
    assert "```" not in text


def test_build_rebuild_llm_direct_prompt_has_no_write_file_tools():
    import openjiuwen.harness.rails.evolution.commands as evolution_commands

    build_rebuild_llm_direct_prompt = getattr(
        evolution_commands, "build_rebuild_llm_direct_prompt", None
    )
    if build_rebuild_llm_direct_prompt is None:
        pytest.skip(
            "openjiuwen package lacks build_rebuild_llm_direct_prompt"
        )

    system, user = build_rebuild_llm_direct_prompt(
        subject={"kind": "skill", "name": "demo"},
        current_skill_md="---\nname: demo\n---\n# demo\n",
        rebuild_context={"skill_md_path": "/tmp/demo/SKILL.md", "records": []},
    )
    blob = f"{system}\n{user}".lower()
    assert "write_file" not in blob
    assert "skill-creator" not in blob
    assert "Current SKILL.md:" in user
    assert "---\nname: demo\n---" in user


def test_queue_auto_rebuild_respects_skill_evolution_action(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter()
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [])
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.resolve_skill_evolution_action",
        lambda skill_name, **_kwargs: "suggest",
    )
    adapter._queue_auto_rebuild_skill("demo-skill")  # pylint: disable=protected-access
    assert adapter._pending_auto_rebuild_skills == []  # pylint: disable=protected-access

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.resolve_skill_evolution_action",
        lambda skill_name, **_kwargs: "auto",
    )
    adapter._queue_auto_rebuild_skill("demo-skill")  # pylint: disable=protected-access
    assert adapter._pending_auto_rebuild_skills == ["demo-skill"]  # pylint: disable=protected-access

    adapter._pending_auto_rebuild_skills.clear()  # pylint: disable=protected-access
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.resolve_skill_evolution_action",
        lambda skill_name, **_kwargs: "off",
    )
    adapter._queue_auto_rebuild_skill("demo-skill")  # pylint: disable=protected-access
    assert adapter._pending_auto_rebuild_skills == []  # pylint: disable=protected-access


def _adapter_for_auto_rebuild(monkeypatch, *, entries: list[Any]) -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._pending_auto_rebuild_skills = ["demo-skill"]  # pylint: disable=protected-access
    store = SimpleNamespace(
        load_full_evolution_log=AsyncMock(return_value=SimpleNamespace(entries=entries)),
    )
    monkeypatch.setattr(adapter, "_get_disk_evolution_store", lambda: store)
    monkeypatch.setattr(adapter, "_should_auto_merge_evolved_skill", lambda _name: True)
    return adapter


@pytest.mark.anyio
async def test_run_auto_rebuild_skips_when_no_live_records(monkeypatch):
    adapter = _adapter_for_auto_rebuild(monkeypatch, entries=[])
    merge_calls: list[str] = []

    async def _fake_merge(*, skill_name: str | None = None, **_kwargs: Any) -> dict[str, Any]:
        merge_calls.append(str(skill_name))
        return {"success": True}

    monkeypatch.setattr(adapter, "generate_evolution_merge_version", _fake_merge)

    await adapter._run_auto_rebuild_skills_detached(request_id="rid")  # pylint: disable=protected-access

    assert merge_calls == []
    assert adapter._pending_auto_rebuild_skills == []  # pylint: disable=protected-access


@pytest.mark.anyio
async def test_run_auto_rebuild_proceeds_when_live_records_exist(monkeypatch):
    adapter = _adapter_for_auto_rebuild(monkeypatch, entries=[SimpleNamespace(id="e1")])
    merge_calls: list[str] = []

    async def _fake_merge(*, skill_name: str | None = None, **_kwargs: Any) -> dict[str, Any]:
        merge_calls.append(str(skill_name))
        return {"success": True}

    monkeypatch.setattr(adapter, "generate_evolution_merge_version", _fake_merge)

    await adapter._run_auto_rebuild_skills_detached(request_id="rid")  # pylint: disable=protected-access

    assert merge_calls == ["demo-skill"]
    assert adapter._pending_auto_rebuild_skills == []  # pylint: disable=protected-access


@pytest.mark.anyio
async def test_do_evolve_rollback_clears_live_evolutions(tmp_path):
    skills_dir = _write_skill(tmp_path, "demo-skill")
    skill_dir = tmp_path / "skills" / "demo-skill"
    skill_dir.joinpath("evolutions.json").write_text(
        json.dumps(
            {
                "skill_id": "demo-skill",
                "version": "v1.0.0",
                "updated_at": "dirty",
                "entries": [{"id": "e1"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_pair(tmp_path, "demo-skill", "v1.0.0", "# restored\n")
    store = evolution_version_ctl.get_disk_evolution_store([str(skills_dir)])
    result = await evolution_version_ctl.do_evolve_rollback(store, "demo-skill", "latest")
    assert result["ok"] is True
    assert result["rolled_back"] is True
    live = json.loads(skill_dir.joinpath("evolutions.json").read_text(encoding="utf-8"))
    assert live["entries"] == []


@pytest.mark.anyio
async def test_handle_skills_evolution_rollback_accepts_office_claw_skill_path(
    tmp_path, monkeypatch,
):
    """Control-plane .office-claw/skills path must pass even if adapter is workspace-only."""
    workspace_skills = tmp_path / "workspace" / "skills"
    workspace_skills.mkdir(parents=True)

    project_root = tmp_path / "relay-claw"
    skills_dir = project_root / ".office-claw" / "skills"
    skill_dir = skills_dir / "tianqi"
    archive = skill_dir / "archive"
    archive.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("# current\n", encoding="utf-8")
    skill_dir.joinpath("evolutions.json").write_text(
        json.dumps(
            {
                "skill_id": "tianqi",
                "version": "v2.0.0",
                "updated_at": "live",
                "entries": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    archive.joinpath("SKILL.v1.0.0.md").write_text("# archived-body\n", encoding="utf-8")
    archive.joinpath("evolutions.v1.0.0.json").write_text(
        json.dumps(
            {
                "skill_id": "tianqi",
                "version": "v1.0.0",
                "updated_at": "archived",
                "entries": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    adapter = JiuWenSwarmDeepAdapter()
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [str(workspace_skills)])
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.resolve_agent_registered_skill_dirs",
        lambda: [workspace_skills],
    )
    monkeypatch.setattr(
        evolution_version_ctl,
        "resolve_agent_registered_skill_dirs",
        lambda: [workspace_skills],
    )

    result = await adapter.handle_skills_evolution_rollback(
        {
            "name": "tianqi",
            "version": "latest",
            "skill_path": str(skill_md),
        }
    )
    assert result["success"] is True
    assert result["rolled_back"] is True
    assert skill_md.read_text(encoding="utf-8") == "# archived-body\n"


@pytest.mark.anyio
async def test_generate_evolution_merge_version_accepts_office_claw_skill_path(
    tmp_path, monkeypatch,
):
    """Rebuild with skill_path must find skill even when adapter dirs are empty."""
    workspace_skills = tmp_path / "workspace" / "skills"
    workspace_skills.mkdir(parents=True)

    skills_dir = tmp_path / ".office-claw" / "skills"
    skill_dir = skills_dir / "image-reader"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: image-reader\nversion: 1.0.0\n---\n# image-reader\n",
        encoding="utf-8",
    )
    skill_dir.joinpath("evolutions.json").write_text(
        json.dumps(
            {
                "skill_id": "image-reader",
                "version": "v1.0.0",
                "updated_at": "live",
                "entries": [
                    {
                        "id": "ev_1",
                        "score": 0.9,
                        "summary": "fix path handling",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = object()  # pylint: disable=protected-access
    adapter._model = object()  # pylint: disable=protected-access
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [str(workspace_skills)])
    monkeypatch.setattr(adapter, "_bind_request_env_overlay", lambda: (None, None, None))
    monkeypatch.setattr(adapter, "_reset_request_env_bindings", lambda *_a, **_k: None)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "cn")
    monkeypatch.setattr(adapter, "_resolve_model_name", lambda: "test-model")
    monkeypatch.setattr(adapter, "ensure_instance", AsyncMock())
    monkeypatch.setattr(adapter, "_apply_rebuild_permission_trusted_dirs", lambda *_a, **_k: None)

    captured_store_dirs: list[list[str]] = []
    real_get_store = evolution_version_ctl.get_disk_evolution_store

    def _capture_store(skills_dirs=None):
        dirs = [str(p) for p in (skills_dirs or [])]
        captured_store_dirs.append(dirs)
        return real_get_store(dirs)

    monkeypatch.setattr(
        evolution_version_ctl,
        "get_disk_evolution_store",
        _capture_store,
    )
    monkeypatch.setattr(
        evolution_version_ctl,
        "prepare_rebuild_followup",
        AsyncMock(
            return_value={
                "ok": True,
                "followup_prompt": "rebuild",
                "rebuild_context": {
                    "skill_md_path": str(skill_md),
                    "archive_pair": True,
                },
            }
        ),
    )
    monkeypatch.setattr(
        evolution_version_ctl,
        "finalize_rebuild_followup",
        AsyncMock(return_value={"ok": True, "new_version": "1.0.1", "cleared": True}),
    )

    async def _rewrite(**_kwargs: Any) -> bool:
        skill_md.write_text("# rebuilt\n", encoding="utf-8")
        return True

    monkeypatch.setattr(adapter, "_execute_merge_version_rewrite", _rewrite)

    result = await adapter.generate_evolution_merge_version(
        {
            "name": "image-reader",
            "skill_path": str(skill_md),
            "record_ids": ["ev_1"],
        }
    )
    assert result["success"] is True
    assert captured_store_dirs
    resolved_store = {str(Path(p).resolve()) for p in captured_store_dirs[0]}
    assert resolved_store == {str(skills_dir.resolve())}
    assert str(workspace_skills.resolve()) not in resolved_store


@pytest.mark.anyio
async def test_handle_skills_evolution_rollback_rejects_mismatched_skill_dir_name(
    tmp_path, monkeypatch,
):
    skills_dir = tmp_path / "skills"
    wrong = skills_dir / "wrong-name"
    wrong.mkdir(parents=True)
    skill_md = wrong / "SKILL.md"
    skill_md.write_text("# dummy\n", encoding="utf-8")

    adapter = JiuWenSwarmDeepAdapter()
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: [str(skills_dir)])

    with pytest.raises(ValueError, match="directory name must match skill name"):
        await adapter.handle_skills_evolution_rollback(
            {
                "name": "demo-skill",
                "version": "latest",
                "skill_path": str(skill_md),
            }
        )


@pytest.mark.anyio
async def test_ws_disk_only_evolution_uses_agent_manager_not_stateless_agent(monkeypatch):
    """archives/rollback must not skip AgentManager disk-only skill-root binding."""
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = object.__new__(AgentWebSocketServer)
    manager_calls: list[AgentRequest] = []
    stateless_calls: list[AgentRequest] = []

    class _Manager:
        async def process_message(self, request):
            manager_calls.append(request)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"name": "tianqi", "versions": ["SKILL.v1.0.0.md"]},
            )

    class _Stateless:
        async def process_message(self, request):
            stateless_calls.append(request)
            raise AssertionError("stateless agent must not handle disk-only evolution")

    server._agent_manager = _Manager()  # pylint: disable=protected-access
    from jiuwenswarm.server.handlers import _default as _default_handlers

    monkeypatch.setattr(
        _default_handlers,
        "_uses_tenant_pool",
        lambda _request: False,
    )
    monkeypatch.setattr(
        _default_handlers,
        "_get_stateless_agent",
        AsyncMock(return_value=_Stateless()),
    )

    sent: list[Any] = []

    async def _send_wire(_ws, wire):
        sent.append(wire)
        return True

    monkeypatch.setattr(
        "jiuwenswarm.server.agent_ws_server.send_wire_payload",
        _send_wire,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.agent_ws_server.encode_agent_response_for_wire",
        lambda resp, response_id=None: {"ok": resp.ok, "payload": resp.payload},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.ensure_persistent_checkpointer",
        AsyncMock(),
    )

    request = AgentRequest(
        request_id="req-disk",
        channel_id="web",
        req_method=ReqMethod.SKILLS_EVOLUTION_ROLLBACK,
        params={"name": "tianqi", "version": "latest", "skill_path": "X:/proj/.office-claw/skills/tianqi/SKILL.md"},
        is_stream=False,
    )
    from jiuwenswarm.server.context import AgentServerServices, RequestContext
    from jiuwenswarm.server.transports.sink import WSSink

    class _FakeWS:
        async def send(self, payload):
            sent.append(payload)

    ctx = RequestContext(
        request=request,
        sink=WSSink(_FakeWS(), asyncio.Lock()),
        services=AgentServerServices(server),
        connection_id="test",
    )
    await _default_handlers._handle_unary_impl(ctx, request)

    assert len(manager_calls) == 1
    assert not stateless_calls
    assert sent
