"""Stable workspace contracts without constructing an agent or permission rail."""

from dataclasses import FrozenInstanceError

import pytest

from jiuwenswarm.common import runtime_workspace as workspace


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("JIUWENSWARM_TASK_REGISTRY_DIR", str(tmp_path / "registry"))
    return tmp_path


@pytest.mark.parametrize("explicit", [False, True])
def test_binding_and_request_resolution_have_distinct_owners(isolated, monkeypatch, explicit):
    project = isolated / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    binding = workspace.bind_session_runtime_workspace(
        internal_workspace_dir=isolated / "internal",
        project_dir=str(project) if explicit else None,
        session_id="workspace-binding",
    )
    assert binding.internal_workspace_dir == isolated / "internal"
    assert binding.is_projectless is not explicit
    if explicit:
        assert binding.runtime_workspace_root == project
        assert binding.cwd == project
    else:
        assert binding.cwd == binding.runtime_workspace_root / "work"
        assert binding.outputs_dir == binding.runtime_workspace_root / "outputs"
        assert binding.cwd.is_dir() and binding.outputs_dir.is_dir()

    def unexpected(*_args, **_kwargs):
        pytest.fail("request validation attempted to allocate a workspace")

    monkeypatch.setattr(workspace, "get_projectless_task_workspace", unexpected)
    result = workspace.resolve_bound_runtime_workspace_paths(
        binding, project_dir=None, workspace_dir=None, cwd=str(nested),
    )
    assert result.runtime_workspace_root == binding.runtime_workspace_root
    assert result.cwd == (nested if explicit else binding.cwd)
    assert binding.cwd == (project if explicit else binding.runtime_workspace_root / "work")
    assert workspace.resolve_bound_runtime_workspace_paths(
        binding, project_dir=None, workspace_dir=str(binding.runtime_workspace_root), cwd=None,
    ) is binding
    with pytest.raises(ValueError, match="runtime_workspace_changed"):
        workspace.resolve_bound_runtime_workspace_paths(
            binding, project_dir=str(isolated / "different"), workspace_dir=None, cwd=None,
        )
    with pytest.raises(FrozenInstanceError):
        binding.cwd = isolated


def test_projectless_rebinding_reuses_registry_identity(isolated):
    arguments = dict(internal_workspace_dir=isolated / "internal", project_dir=None,
                     session_id="restored-workspace")
    first = workspace.bind_session_runtime_workspace(**arguments)
    marker = first.cwd / "retained.txt"
    marker.write_text("retained")
    restored = workspace.bind_session_runtime_workspace(**arguments)
    assert restored == first
    assert marker.read_text() == "retained"


@pytest.mark.parametrize("session_id", [None, "", "  "])
def test_explicit_binding_requires_identity_before_allocator(isolated, monkeypatch, session_id):
    def unexpected(*_args, **_kwargs):
        pytest.fail("invalid identity reached the allocator")
    monkeypatch.setattr(workspace, "get_projectless_task_workspace", unexpected)
    with pytest.raises(ValueError, match="runtime_workspace_session_missing"):
        workspace.bind_session_runtime_workspace(
            internal_workspace_dir=isolated, project_dir=None, session_id=session_id,
        )


def test_unbound_resolver_preserves_existing_defaults(isolated, monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("unbound resolver allocated a workspace")
    monkeypatch.setattr(workspace, "get_projectless_task_workspace", unexpected)
    paths = workspace.resolve_runtime_workspace_paths(
        internal_workspace_dir=isolated, project_dir=None, workspace_dir=None,
        cwd=str(isolated / "arbitrary-cwd"), session_id=None, task_name=None,
        bind_request=False,
    )
    assert paths.runtime_workspace_root == paths.cwd == isolated
    assert not paths.is_projectless


@pytest.mark.parametrize("cwd", ["missing", "outside"])
def test_bound_explicit_cwd_keeps_existing_fallback(isolated, cwd):
    project = isolated / "project"
    project.mkdir()
    outside = isolated / "outside"
    outside.mkdir()
    binding = workspace.bind_session_runtime_workspace(
        internal_workspace_dir=isolated, project_dir=str(project), session_id="cwd",
    )
    resolved = workspace.resolve_bound_runtime_workspace_paths(
        binding, project_dir=str(project), workspace_dir=None, cwd=str(isolated / cwd),
    )
    assert resolved.cwd == project
