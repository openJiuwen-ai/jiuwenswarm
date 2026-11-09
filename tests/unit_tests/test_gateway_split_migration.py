"""Cross-package identities and paths remain shared after removing gateway/."""
from pathlib import Path

from agent_gateway.common import expert_packages as gateway_packages
from agent_gateway.common import paths as gateway_paths
from agent_gateway.enterprise.team import route_binding_store as gateway_routes
from agent_gateway.enterprise.team.quota_policy import OwnerIdentity, TeamQuotaPolicyStore
from jiuwenswarm.common import utils
from jiuwenswarm.common.schema import expert_packages
from jiuwenswarm.enterprise.team import route_binding_store
from jiuwenswarm.enterprise.team.control_store import OwnerIdentity as ExecutorIdentity
from jiuwenswarm.enterprise.team.quota import TeamQuotaService


def test_execution_side_reuses_gateway_contract_objects():
    assert ExecutorIdentity is OwnerIdentity
    assert issubclass(TeamQuotaService, TeamQuotaPolicyStore)
    assert route_binding_store.TeamRouteBinding is gateway_routes.TeamRouteBinding
    assert route_binding_store.get_team_route_binding_store is gateway_routes.get_team_route_binding_store
    assert expert_packages.encode_expert_packages_header is gateway_packages.encode_expert_packages_header


def test_mother_and_gateway_agree_on_partition_paths(tmp_path, monkeypatch):
    for module in (utils, gateway_paths):
        monkeypatch.setattr(module, "get_user_workspace_dir", lambda: tmp_path)
        monkeypatch.setattr(module, "_initialized", False)
        for name in ("_config_dir", "_workspace_dir", "_root_dir"):
            monkeypatch.setattr(module, name, None)
    gateway_paths.prepare_runtime_workspace(cleanup_stale_descs=False)
    assert utils.get_config_dir() == gateway_paths.get_config_dir() == tmp_path / "infra/config"
    assert utils.get_agent_sessions_dir() == gateway_paths.get_agent_sessions_dir() == tmp_path / "domain/agent/sessions"
    assert utils.get_logs_dir() == gateway_paths.get_logs_dir() == tmp_path / "infra/logs"


def test_gateway_is_loaded_from_external_package():
    import agent_gateway

    root = Path(__file__).resolve().parents[2]
    assert not (root / "gateway").exists()
    assert Path(agent_gateway.__file__).resolve() != root / "agent_gateway" / "__init__.py"


def test_new_upstream_business_directories_migrate_into_domain(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    for name in (".organizations", "jiuwenmate"):
        old = tmp_path / name
        old.mkdir()
        (old / "state.json").write_text(name)
    utils._do_migrate_partitioned_data_root_layout()
    for name in (".organizations", "jiuwenmate"):
        assert (tmp_path / "domain" / name / "state.json").read_text() == name
        assert not (tmp_path / name).exists()
