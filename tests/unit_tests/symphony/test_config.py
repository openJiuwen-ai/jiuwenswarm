"""Swarm product defaults, explicit YAML and strict Core configuration contract."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from openjiuwen.symphony import OrchestrationConfig, SymphonyConfig
from jiuwenswarm.common import symphony_config


def test_product_defaults_are_explicit_and_preserve_core_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(symphony_config, "get_agent_workspace_dir", lambda: tmp_path)
    cfg = symphony_config.symphony_config_from_dict({})
    assert isinstance(cfg, SymphonyConfig)
    assert [source.model_dump() for source in cfg.capability.sources] == [
        {"type": "skill", "root": str(tmp_path / "skills")},
    ]
    assert cfg.storage.artifact_root == str(tmp_path / "symphony" / "runtime")
    assert not cfg.retrieval.enabled and not cfg.orchestration.enabled and not cfg.orchestration.experience.enabled
    assert cfg.orchestration.min_edge_confidence == 0.5
    assert not cfg.orchestration.build.matching.require_consensus
    assert cfg.orchestration.build.matching.batch_size == 12
    assert cfg.capability.fingerprint.extraction.max_concurrency == 1
    assert cfg.capability.fingerprint.extraction.batch_size == 1
    assert cfg.capability.fingerprint.extraction.max_output_tokens is None
    assert cfg.retrieval.index_build.request.max_concurrency == 2
    assert cfg.retrieval.index_build.structure.branching_factor == 128
    assert cfg.retrieval.index_build.request.timeout_seconds == 420
    core = SymphonyConfig(capability={"sources": [{"type": "skill", "root": "x"}]}, storage={"artifact_root": "y"})
    assert core.orchestration.min_edge_confidence == 0.7
    assert cfg.orchestration.experience == core.orchestration.experience
    assert cfg.orchestration.experience.model_dump() == {
        "enabled": False,
        "acceptance": {"min_successes": 3, "min_pack_success_rate": 0.8},
        "narrative": {"max_examples": 5},
    }


def test_explicit_yaml_sources_replace_default_and_resolve_each_path(monkeypatch, tmp_path):
    monkeypatch.setattr(symphony_config, "get_agent_workspace_dir", lambda: tmp_path / "workspace")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYMPHONY_SKILL_SOURCE", str(tmp_path / "skills"))
    raw = yaml.safe_load("""
symphony:
  capability:
    sources:
      - type: skill
        root: "  ${SYMPHONY_SKILL_SOURCE}  "
      - type: skill
        root: ./shared-skills
      - type: expert
        root: ./experts
""")
    cfg = symphony_config.load_symphony_config(raw)
    assert [source.model_dump() for source in cfg.capability.sources] == [
        {"type": "skill", "root": str(tmp_path / "skills")},
        {"type": "skill", "root": str(tmp_path / "shared-skills")},
        {"type": "expert", "root": str(tmp_path / "experts")},
    ]


@pytest.mark.parametrize("sources", [None, []])
def test_explicit_empty_sources_are_rejected(sources):
    with pytest.raises(ValidationError) as exc:
        symphony_config.symphony_config_from_dict({"capability": {"sources": sources}})
    assert exc.value.errors()[0]["loc"] == ("symphony", "capability", "sources")


@pytest.mark.parametrize("source, field", [
    ({"type": "expert", "root": "  "}, "root"),
    ({"type": "expert", "root": "${SYMPHONY_EMPTY_SOURCE}"}, "root"),
    ({"type": "expert", "root": None}, "root"),
    ({"type": "unknown", "root": "./unknown"}, "type"),
])
def test_invalid_source_reports_its_index(monkeypatch, source, field):
    monkeypatch.delenv("SYMPHONY_EMPTY_SOURCE", raising=False)
    sources = [{"type": "skill", "root": "./skills"}, source]
    with pytest.raises(ValidationError) as exc:
        symphony_config.symphony_config_from_dict({"capability": {"sources": sources}})
    assert exc.value.errors()[0]["loc"] == ("symphony", "capability", "sources", 1, field)


def test_legacy_capability_fields_are_rejected():
    with pytest.raises(ValidationError) as exc:
        symphony_config.symphony_config_from_dict({"capability": {"root": "./skills", "type": "skill"}})
    assert {(error["loc"], error["type"]) for error in exc.value.errors()} == {
        (("symphony", "capability", "root"), "extra_forbidden"),
        (("symphony", "capability", "type"), "extra_forbidden"),
    }


@pytest.mark.parametrize("group", ["retrieval", "orchestration"])
@pytest.mark.parametrize("value", [None, True, False])
def test_switches_resolve_null_in_host(group, value):
    cfg = symphony_config.symphony_config_from_dict({group: {"enabled": value}})
    assert getattr(cfg, group).enabled is (False if value is None else value)


@pytest.mark.parametrize("value", [None, True, False])
def test_experience_switch_inherits_core_default_or_uses_explicit_value(value):
    cfg = symphony_config.symphony_config_from_dict({"orchestration": {"experience": {"enabled": value}}})
    assert cfg.orchestration.experience.enabled is (False if value is None else value)


@pytest.mark.parametrize("field", ["enabled", "paths", "fingerprint", "build", "evolution", "skill_retrieval"])
def test_retired_yaml_fields_are_rejected_with_full_path(field):
    with pytest.raises(ValidationError) as exc:
        symphony_config.symphony_config_from_dict({field: {}})
    assert ("symphony", field) in [error["loc"] for error in exc.value.errors()]


def test_unknown_nested_fields_and_strict_values_fail_at_exact_path():
    with pytest.raises(ValidationError) as exc:
        symphony_config.symphony_config_from_dict({"retrieval": {"index_build": {"request": {"max_concurrency": "4"}}}})
    assert exc.value.errors()[0]["loc"] == ("symphony", "retrieval", "index_build", "request", "max_concurrency")
    for field in ("llm", "build", "discovery"):
        with pytest.raises(ValidationError) as exc:
            symphony_config.symphony_config_from_dict({"retrieval": {field: {}}})
        assert exc.value.errors()[0]["loc"] == ("symphony", "retrieval", field)


def test_explicit_values_override_product_and_context_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("SYMPHONY_TEST_ROOT", str(tmp_path / "artifacts"))
    cfg = symphony_config.symphony_config_from_dict({
        "storage": {"artifact_root": "${SYMPHONY_TEST_ROOT}"},
        "orchestration": {"min_edge_confidence": 0.9, "plan": {"mode": "beam", "dynamic_graph_enabled": True}},
        "retrieval": {"prompt": {"context_window_tokens": 12345, "candidate_budget_ratio": 0.02}},
    }, context_window_tokens=4000)
    assert cfg.storage.artifact_root == str(tmp_path / "artifacts")
    assert cfg.retrieval.prompt.context_window_tokens == 12345
    assert cfg.retrieval.prompt.candidate_budget_tokens == 246
    assert cfg.orchestration.plan.dynamic_graph_enabled
    assert cfg.orchestration.min_edge_confidence == 0.9
    assert symphony_config.symphony_config_from_dict({}, context_window_tokens=4000).retrieval.prompt.context_window_tokens == 4000


@pytest.mark.parametrize("path", ["jiuwenswarm/resources/config.yaml", "deploy/yuanrong/conf/gateway-config-yuanrong.template.yaml"])
def test_real_templates_validate_and_keep_explicit_build_limits(path):
    data = yaml.safe_load(Path(path).read_text())
    cfg = symphony_config.load_symphony_config(data)
    assert cfg.orchestration.build.max_concurrency == 4
    assert cfg.orchestration.build.matching.batch_size == 16
    assert cfg.capability.fingerprint.extraction.max_concurrency == 4
    assert cfg.capability.fingerprint.extraction.batch_size == 2
    assert cfg.capability.fingerprint.extraction.max_output_tokens is None
    assert cfg.storage.artifact_root.endswith("/symphony/runtime")
    assert not cfg.retrieval.enabled and not cfg.orchestration.enabled
    assert cfg.orchestration.experience == OrchestrationConfig().experience
    assert not cfg.orchestration.plan.dynamic_graph_enabled


@pytest.mark.parametrize("path", [
    "docs/en/Symphony.md", "docs/zh/Symphony-技能编排与分发.md",
    "docs/en/Configuration.md", "docs/zh/配置信息.md",
])
def test_documented_symphony_yaml_examples_use_current_contract(path):
    import re
    root = Path(__file__).resolve().parents[3]
    examples = [yaml.safe_load(block) for block in re.findall(r"```yaml\n(.*?)```", (root / path).read_text(), re.DOTALL)]
    configured = [example for example in examples if isinstance(example, dict) and "symphony" in example]
    assert configured
    for example in configured:
        config = symphony_config.load_symphony_config(example)
        assert config.retrieval.enabled and config.orchestration.enabled
        assert config.storage.artifact_root.endswith("symphony/runtime")


def test_dependency_and_container_defaults_pin_the_same_core_commit():
    import tomllib
    root = Path(__file__).resolve().parents[3]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    lock = tomllib.loads((root / "uv.lock").read_text())
    pin = project["tool"]["uv"]["sources"]["openjiuwen"]
    assert len(pin["rev"]) == 40
    requirements = [*project["project"]["dependencies"],
                    *project["project"]["optional-dependencies"]["harmony"],
                    *project["tool"]["uv"]["override-dependencies"]]
    direct_core = [item for item in requirements if item.startswith(("openjiuwen @", "openjiuwen[")) and " @ " in item]
    assert len(direct_core) == 6
    assert all(f'{pin["git"]}@{pin["rev"]}' in item for item in direct_core)
    package = next(package for package in lock["package"] if package["name"] == "openjiuwen")
    assert package["source"]["git"] == f'{pin["git"]}?rev={pin["rev"]}#{pin["rev"]}'
    for dockerfile in ("docker/Dockerfile.claw", "docker/openshift/Dockerfile.claw"):
        source = (root / dockerfile).read_text()
        assert f'ARG AGENT_CORE_REPOS={pin["git"]}' in source
        assert f'ARG AGENT_CORE_COMMIT={pin["rev"]}' in source
