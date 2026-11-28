"""Swarm product defaults, explicit YAML and strict Core configuration contract."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from openjiuwen.symphony import OrchestrationConfig, SymphonyConfig
from jiuwenswarm.common import symphony_config


def test_product_defaults_are_explicit_and_preserve_core_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(symphony_config, "get_agent_workspace_dir", lambda: tmp_path)
    # 默认 artifact_root 经 paths.get_symphony_dir() 派生（= agent workspace
    # 下 symphony/），函数内局部 import，patch 需覆盖 paths 层
    monkeypatch.setattr("jiuwenswarm.common.paths.get_agent_workspace_dir", lambda: tmp_path)
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
      - type: connector
        root: ./mcp_hub
""")
    cfg = symphony_config.load_symphony_config(raw)
    assert [source.model_dump() for source in cfg.capability.sources] == [
        {"type": "skill", "root": str(tmp_path / "skills")},
        {"type": "skill", "root": str(tmp_path / "shared-skills")},
        {"type": "expert", "root": str(tmp_path / "experts")},
        {"type": "mcp", "root": str(tmp_path / "mcp_hub")},
    ]
    assert raw["symphony"]["capability"]["sources"][-1]["type"] == "connector"


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


def test_legacy_empty_root_categories_migrates_to_unset():
    cfg = symphony_config.symphony_config_from_dict({
        "skill_retrieval": {"build": {"root_categories": ""}},
    })

    assert cfg.retrieval.index_build.structure.root_categories is None


def test_legacy_symphony_values_migrate_without_mutating_input():
    raw = {
        "enabled": True,
        "fingerprint": {"scan": {"max_depth": 2}, "extraction": {
            "workers": 3, "batch_size": 5, "body_limit": 8000,
        }},
        "build": {"workers": 6, "batch_size": 9, "max_candidates_per_skill_relation": 17,
                  "require_consensus": True, "min_edge_confidence": 0.6},
        "evolution": {"flow": {"enabled": True, "min_successes": 5, "min_pack_success_rate": 0.9}},
        "orchestration": {"mode": "beam", "top_k": 7, "max_depth": 8},
        "skill_retrieval": {
            "enabled": True,
            "discovery": {"use_existing_index": False, "candidate_budget_ratio": 0.02,
                          "context_window_tokens": 12000, "prompt_preferred_skills": ["custom-skill"],
                          "max_output_chars": 3000, "incremental_notice_max_chars": 500},
            "build": {"max_workers": 3, "max_depth": 8, "branching_factor": 32, "max_retries": 0,
                      "request_timeout_seconds": 150.0, "classification_batch_limit": 10,
                      "root_categories": ["custom"], "discovery_seed": 7, "strict_failure": True,
                      "postprocess_enabled": False, "postprocess_min_skills": 4,
                      "skill_profiles_enabled": True, "skill_profile_batch_size": 8,
                      "equivalence_enabled": True, "equivalence_allow_singleton_groups": False,
                      "incremental_max_change_ratio": 0.1, "generate_tree_html": True},
        },
    }
    original = deepcopy(raw)
    converted = symphony_config.migrate_legacy_symphony_config(raw)
    cfg = symphony_config.symphony_config_from_dict(converted)

    assert raw == original
    assert symphony_config.migrate_legacy_symphony_config(converted) == converted
    assert set(converted) == {"capability", "retrieval", "orchestration"}
    assert cfg.capability.scan.max_depth == 2
    assert cfg.capability.fingerprint.extraction.max_concurrency == 3
    assert cfg.capability.fingerprint.extraction.batch_size == 5
    assert cfg.capability.fingerprint.extraction.max_body_chars == 8000
    assert cfg.orchestration.enabled is True
    assert cfg.orchestration.build.max_concurrency == 6
    assert cfg.orchestration.build.matching.batch_size == 9
    assert cfg.orchestration.build.matching.max_candidates_per_capability_relation == 17
    assert cfg.orchestration.build.matching.require_consensus is True
    assert cfg.orchestration.min_edge_confidence == 0.6
    assert cfg.orchestration.plan.mode == "beam"
    assert cfg.orchestration.plan.top_k == 7
    assert cfg.orchestration.plan.max_depth == 8
    assert cfg.orchestration.experience.enabled is True
    assert cfg.orchestration.experience.acceptance.min_successes == 5
    assert cfg.orchestration.experience.acceptance.min_pack_success_rate == 0.9
    assert cfg.retrieval.enabled is True
    assert cfg.retrieval.use_existing_index is False
    assert cfg.retrieval.prompt.preferred_capability_ids == ["custom-skill"]
    assert cfg.retrieval.prompt.candidate_budget_tokens == 240
    assert cfg.retrieval.output.max_chars == 3000
    assert cfg.retrieval.output.incremental_notice_max_chars == 500
    index = cfg.retrieval.index_build
    assert index.structure.max_depth == 8
    assert index.structure.branching_factor == 32
    assert index.structure.root_categories == ["custom"]
    assert index.request.max_concurrency == 3
    assert index.request.max_retries == 0
    assert index.request.timeout_seconds == 150.0
    assert index.request.classification_batch_limit == 10
    assert index.prompt.discovery_seed == 7
    assert index.task.allow_fallback_tree is False
    assert index.postprocess.enabled is False
    assert index.postprocess.min_capabilities == 4
    assert index.profiles.enabled is True
    assert index.profiles.batch_size == 8
    assert index.equivalence.enabled is True
    assert index.equivalence.allow_singleton_groups is False
    assert index.incremental.max_change_ratio == 0.1
    assert index.output.generate_tree_html is True


@pytest.mark.parametrize("enabled", [None, False, True])
def test_new_fields_win_over_legacy_values_including_null_and_false(enabled):
    raw = {
        "enabled": True,
        "fingerprint": {"extraction": {"workers": 5}},
        "paths": {"skills_root": "./old-skills", "graph_dir": "./old-graph"},
        "skill_retrieval": {"enabled": True, "build": {"max_workers": 8}},
        "evolution": {"flow": {"enabled": True}},
        "capability": {"sources": [{"type": "skill", "root": "./new-skills"}]},
        "storage": {"artifact_root": ""},
        "retrieval": {"enabled": enabled, "index_build": {"request": {"max_concurrency": 2}}},
        "orchestration": {"enabled": enabled, "experience": {"enabled": False},
                          "mode": "beam", "top_k": 9, "plan": {"mode": "fast"}},
    }
    converted = symphony_config.migrate_legacy_symphony_config(raw)
    assert converted["storage"]["artifact_root"] == ""
    assert converted["retrieval"]["enabled"] is enabled
    assert converted["orchestration"]["enabled"] is enabled
    cfg = symphony_config.symphony_config_from_dict(raw)
    assert cfg.retrieval.enabled is (enabled is True)
    assert cfg.orchestration.enabled is (enabled is True)
    assert cfg.orchestration.experience.enabled is False
    assert cfg.capability.require_single_source().root == str(Path("./new-skills").resolve())
    assert cfg.capability.fingerprint.extraction.max_concurrency == 5
    assert cfg.retrieval.index_build.request.max_concurrency == 2
    assert cfg.orchestration.plan.mode == "fast"
    assert cfg.orchestration.plan.top_k == 9


def test_legacy_flow_null_inherits_old_evolution_switch():
    cfg = symphony_config.symphony_config_from_dict({
        "enabled": False, "evolution": {"enabled": True, "flow": {"enabled": None}},
    })
    assert cfg.orchestration.experience.enabled is True
    assert cfg.orchestration.enabled is True


@pytest.mark.parametrize("section, field", [("paths", "graph_dir"), ("skill_retrieval", "artifact_root")])
def test_legacy_artifacts_stay_separate_from_runtime(tmp_path, monkeypatch, section, field):
    old_root = tmp_path / "old-artifacts"
    old_root.mkdir()
    old_artifact = old_root / "legacy.json"
    old_artifact.write_text('{"legacy": true}', encoding="utf-8")
    monkeypatch.setenv("SYMPHONY_LEGACY_ROOT", str(old_root))
    monkeypatch.setenv("SYMPHONY_LEGACY_SKILLS", str(tmp_path / "custom-skills"))
    raw = {"paths": {"skills_root": "${SYMPHONY_LEGACY_SKILLS}"}}
    raw.setdefault(section, {})[field] = "${SYMPHONY_LEGACY_ROOT:-}"
    cfg = symphony_config.symphony_config_from_dict(raw)
    assert cfg.storage.artifact_root == str(old_root / "runtime")
    assert cfg.capability.require_single_source().root == str(tmp_path / "custom-skills")
    assert old_artifact.read_text(encoding="utf-8") == '{"legacy": true}'
    assert not (old_root / "runtime").exists()


def test_legacy_empty_paths_and_body_limit_use_current_defaults(monkeypatch):
    monkeypatch.delenv("SYMPHONY_LEGACY_ROOT", raising=False)
    cfg = symphony_config.symphony_config_from_dict({
        "paths": {"skills_root": "", "graph_dir": ""},
        "skill_retrieval": {"artifact_root": "${SYMPHONY_LEGACY_ROOT:-}", "llm": None},
        "fingerprint": {"extraction": {"body_limit": None}},
    })
    assert cfg.storage.artifact_root.endswith("/symphony/runtime")
    assert cfg.capability.require_single_source().root.endswith("/skills")
    assert cfg.capability.fingerprint.extraction.max_body_chars == 16000


def test_migration_preserves_unknown_current_fields_for_strict_validation():
    with pytest.raises(ValidationError) as exc:
        symphony_config.symphony_config_from_dict({"enabled": True, "retrieval": {"unknown": True}})
    assert exc.value.errors()[0]["loc"] == ("symphony", "retrieval", "unknown")


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


def test_dependency_defaults_pin_the_same_core_commit():
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
