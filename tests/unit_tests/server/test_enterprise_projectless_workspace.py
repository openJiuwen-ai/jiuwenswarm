"""Enterprise task artifacts reuse tenant paths instead of Pod-local Documents."""
from jiuwenswarm.common import projectless_workspace, utils
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext


def test_enterprise_task_roots_are_tenant_scoped_and_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.delenv("JIUWENSWARM_TASKS_DIR", raising=False)
    monkeypatch.delenv("JIUWENSWARM_TASK_REGISTRY_DIR", raising=False)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    with TenantContext.scoped("group::bot::admin"):
        admin = projectless_workspace.get_projectless_task_workspace("same-session")
        assert admin.root_dir.is_relative_to(utils.get_default_project_workspace_dir())
    with TenantContext.scoped("group::bot::user1"):
        user = projectless_workspace.get_projectless_task_workspace("same-session")
        assert user.root_dir.is_relative_to(utils.get_default_project_workspace_dir())
        assert user == projectless_workspace.get_projectless_task_workspace("same-session")
    with TenantContext.scoped("group::bot::admin"):
        assert admin == projectless_workspace.get_projectless_task_workspace("same-session")
    assert admin.root_dir != user.root_dir


def test_personal_documents_and_explicit_override_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.delenv("JIUWENSWARM_TASKS_DIR", raising=False)
    monkeypatch.setattr(projectless_workspace, "_get_documents_dir", lambda: tmp_path)
    assert projectless_workspace.get_projectless_tasks_dir() == tmp_path / "JiuwenSwarm"
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENSWARM_TASKS_DIR", str(tmp_path / "explicit"))
    assert projectless_workspace.get_projectless_tasks_dir() == tmp_path / "explicit"
