# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for Code Graph profile config and eval trace summaries (no LLM)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[3] / "scripts" / "eval"
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(REPO_ROOT))

from local_openjiuwen import prepend_local_agent_core  # noqa: E402

prepend_local_agent_core(verbose=False)

from jiuwenswarm.server.runtime.agent_adapter.code_graph_flags import (  # noqa: E402
    PROFILE_GRAPH,
    PROFILE_OFF,
    apply_code_graph_profile,
    resolve_code_graph_flags,
)
from jiuwenswarm.server.runtime.agent_adapter.code_graph_setup import (  # noqa: E402
    preload_code_graph_grammars,
)
from coding_agent import config_dir_name  # noqa: E402
from trace import summarize_tool_payload  # noqa: E402


def test_missing_code_graph_section_is_the_original_agent() -> None:
    flags = resolve_code_graph_flags({})
    assert flags.profile == PROFILE_OFF
    assert flags.enabled is False


def test_product_template_defaults_code_graph_off() -> None:
    import yaml

    path = REPO_ROOT / "jiuwenswarm" / "resources" / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["code_graph"]["profile"] == PROFILE_OFF
    flags = resolve_code_graph_flags(data)
    assert flags.enabled is False
    assert flags.on_root is False
    assert flags.on_code_agent is False


def test_profile_is_read_from_config() -> None:
    flags = resolve_code_graph_flags({"code_graph": {"profile": "graph"}})
    assert flags.profile == PROFILE_GRAPH
    assert flags.enabled is True


def test_bool_profile_is_off() -> None:
    assert resolve_code_graph_flags({"code_graph": {"profile": True}}).profile == PROFILE_OFF
    assert resolve_code_graph_flags({"code_graph": {"profile": False}}).profile == PROFILE_OFF


def test_unknown_profile_including_retropus_falls_back_to_off() -> None:
    for spelling in ("retropus", "on", "default", "query"):
        assert (
            resolve_code_graph_flags({"code_graph": {"profile": spelling}}).profile
            == PROFILE_OFF
        )


def test_legacy_commit_guard_yaml_is_ignored() -> None:
    flags = resolve_code_graph_flags(
        {"code_graph": {"profile": "graph", "require_context_commit_before_edit": True}}
    )
    assert flags.profile == PROFILE_GRAPH
    assert not hasattr(flags, "require_context_commit_before_edit")


def test_unknown_profile_falls_back_to_off() -> None:
    assert (
        resolve_code_graph_flags({"code_graph": {"profile": "nonsense"}}).profile
        == PROFILE_OFF
    )


def test_missing_agent_hangs_on_code_agent() -> None:
    flags = resolve_code_graph_flags({"code_graph": {"profile": "graph"}})
    assert flags.profile == PROFILE_GRAPH
    assert flags.agent == "code_agent"
    assert flags.on_code_agent is True
    assert flags.on_root is False


def test_yaml_agent_root_hangs_on_root() -> None:
    flags = resolve_code_graph_flags(
        {"code_graph": {"profile": "graph", "agent": "root"}}
    )
    assert flags.agent == "root"
    assert flags.on_root is True
    assert flags.on_code_agent is False


def test_yaml_agent_code_agent_is_explicit() -> None:
    flags = resolve_code_graph_flags(
        {"code_graph": {"profile": "graph", "agent": "code_agent"}}
    )
    assert flags.agent == "code_agent"
    assert flags.on_code_agent is True


def test_unknown_or_bool_agent_falls_back_to_code_agent() -> None:
    for spelling in (True, False, "graph_agent", "main"):
        flags = resolve_code_graph_flags(
            {"code_graph": {"profile": "graph", "agent": spelling}}
        )
        assert flags.agent == "code_agent"


def test_off_profile_does_not_hang_even_with_agent_root() -> None:
    flags = resolve_code_graph_flags(
        {"code_graph": {"profile": "off", "agent": "root"}}
    )
    assert flags.on_root is False
    assert flags.on_code_agent is False
    assert flags.agent == "root"


def test_overlay_keeps_yaml_agent() -> None:
    cfg = apply_code_graph_profile(
        {"code_graph": {"agent": "root"}, "react": {"subagents": {}}},
        PROFILE_GRAPH,
    )
    flags = resolve_code_graph_flags(cfg)
    assert flags.profile == PROFILE_GRAPH
    assert flags.agent == "root"
    assert cfg["react"]["subagents"]["code_agent"]["enabled"] is True


def test_profile_overlay_turns_graph_off_and_keeps_code_agent_on() -> None:
    cfg = apply_code_graph_profile(
        {
            "code_graph": {"profile": "graph"},
            "react": {"subagents": {"code_agent": {"enabled": False}}},
        },
        PROFILE_OFF,
    )
    assert resolve_code_graph_flags(cfg).profile == PROFILE_OFF
    assert cfg["react"]["subagents"]["code_agent"]["enabled"] is True


def test_profile_overlay_sets_graph_and_enables_code_agent() -> None:
    cfg = apply_code_graph_profile({"code_graph": {}, "react": {"subagents": {}}}, PROFILE_GRAPH)
    assert resolve_code_graph_flags(cfg).profile == PROFILE_GRAPH
    assert cfg["react"]["subagents"]["code_agent"]["enabled"] is True


def test_graph_agent_defaults_and_protocol_labels() -> None:
    from run_contextbench import describe_protocol, resolve_graph_agent

    assert resolve_graph_agent("off", None) == "root"
    assert resolve_graph_agent("off", "code_agent") == "code_agent"
    assert resolve_graph_agent("graph", None) == "code_agent"
    assert resolve_graph_agent("graph", "root") == "root"
    assert describe_protocol("graph", "code_agent") == (
        "find-root-delegates-code-agent"
    )
    assert describe_protocol("graph", "root") == "find-on-root"
    assert describe_protocol("off", "root") == "find-product-baseline"


def test_overlay_ignores_unknown_subagent_keys() -> None:
    cfg = apply_code_graph_profile(
        {
            "code_graph": {"profile": "graph"},
            "react": {"subagents": {"code_graph_agent": {"enabled": True}}},
        },
        PROFILE_OFF,
    )
    assert resolve_code_graph_flags(cfg).profile == PROFILE_OFF
    assert cfg["react"]["subagents"]["code_agent"]["enabled"] is True


def test_code_repair_yaml_is_not_a_product_knob() -> None:
    flags = resolve_code_graph_flags(
        {
            "code_graph": {"profile": "off"},
            "code_repair": {"enabled": True, "strict": True},
        }
    )
    assert flags.profile == PROFILE_OFF
    assert not hasattr(flags, "code_repair")


def test_overlay_only_sets_profile_and_enables_code_agent() -> None:
    cfg = apply_code_graph_profile(
        {
            "code_graph": {
                "profile": "off",
                "max_files": 12,
                "enabled": True,
                "require_context_commit_before_edit": True,
            },
            "code_repair": {"enabled": True},
            "react": {"subagents": {}},
        },
        PROFILE_GRAPH,
    )
    flags = resolve_code_graph_flags(cfg)
    assert flags.profile == PROFILE_GRAPH
    assert cfg["code_graph"]["profile"] == PROFILE_GRAPH
    assert cfg["code_graph"]["max_files"] == 12
    assert cfg["react"]["subagents"]["code_agent"]["enabled"] is True
    assert not hasattr(flags, "require_context_commit_before_edit")
    assert not hasattr(flags, "code_repair")


def test_legacy_surface_flags_do_not_enable_graph() -> None:
    cfg = apply_code_graph_profile(
        {"code_graph": {"enabled": True, "tools": True, "agent": True, "prompt": True}},
        PROFILE_GRAPH,
    )
    assert resolve_code_graph_flags(cfg).profile == PROFILE_GRAPH
    assert cfg["code_graph"]["profile"] == PROFILE_GRAPH


def test_summarize_resolve_symbol_keeps_the_name() -> None:
    summary = summarize_tool_payload(
        "resolve_symbol",
        {"name": "io.fits.FITSDiff", "kind": "class"},
        {"status": "NO_MATCH", "matches": [], "message": "no exact symbol match"},
    )
    assert summary["name"] == "io.fits.FITSDiff"
    assert summary["status"] == "NO_MATCH"
    summary = summarize_tool_payload(
        "find_code_symbols",
        {"query": "create_user"},
        {
            "status": "COMPLETE",
            "matches": [
                {"file": "a.py", "name": "create_user", "start_line": 1, "end_line": 3}
            ],
        },
    )
    assert summary["matches_count"] == 1
    assert summary["query"] == "create_user"
    assert json.dumps(summary)


def test_summarize_search_source_text_keeps_chunks() -> None:
    summary = summarize_tool_payload(
        "search_source_text",
        {"query": "clear_select_clause"},
        {
            "status": "COMPLETE",
            "chunks": [
                {
                    "file": "sql/query.py",
                    "symbol_id": "sql/query.py::Query.clear_select_clause",
                    "start_line": 2092,
                    "end_line": 2098,
                }
            ],
        },
    )
    assert summary["chunks_count"] == 1
    assert summary["chunks"][0]["symbol_id"] == "sql/query.py::Query.clear_select_clause"


def test_summarize_keeps_next_action_symbol_and_file() -> None:
    summary = summarize_tool_payload(
        "find_code_symbols",
        {"query": "TimeSeries"},
        {
            "status": "COMPLETE",
            "matches": [{"file": "sampled.py", "name": "TimeSeries"}],
            "next_actions": [
                {
                    "tool": "find_callers",
                    "symbol_id": "sampled.py:TimeSeries",
                    "file": "sampled.py",
                    "must_before": "edit",
                }
            ],
        },
    )
    assert summary["next_actions"] == [
        {
            "tool": "find_callers",
            "symbol_id": "sampled.py:TimeSeries",
            "file": "sampled.py",
            "must_before": "edit",
        }
    ]


def test_summarize_submit_keeps_the_packet_shape_not_its_body() -> None:
    summary = summarize_tool_payload(
        "submit_code_context",
        {},
        {
            "status": "COMPLETE",
            "phase": "committed",
            "context_packet": {
                "artifact_id": "loc-1234",
                "file_count": 2,
                "span_count": 3,
                "files": [{"file": "src/user.py", "spans": [{"symbol_id": "x"}]}],
            },
        },
    )
    assert summary["phase"] == "committed"
    assert summary["context_packet"] == {
        "artifact_id": "loc-1234",
        "file_count": 2,
        "span_count": 3,
    }
    assert "files" not in summary["context_packet"]


def test_trace_totals_count_find_tools_not_grep() -> None:
    from trace import EvalTrace

    trace = EvalTrace(repo_root="/tmp/repo")
    for name in ("find_callers", "submit_code_context", "grep"):
        trace.tool_events.append({"tool": name, "duration_ms": 1.0})
    totals = trace.finish()["totals"]
    assert totals["find_callers_calls"] == 1
    assert totals["submit_code_context_calls"] == 1
    assert totals["grep_calls"] == 1
    assert totals["graph_tool_calls"] == 2


def test_trace_totals_carry_the_process_metrics() -> None:
    from trace import EvalTrace

    trace = EvalTrace(repo_root="/tmp/repo")
    trace.tool_events.extend(
        [
            {
                "tool": "find_code_symbols",
                "matches_count": 3,
                "next_actions": ["read_file", "read_symbol"],
            },
            {"tool": "find_code_symbols", "matches_count": 3, "duplicate_query": True},
            {"tool": "read_file", "file_path": "src/user.py"},
            {"tool": "edit_file", "file_path": "src/user.py"},
        ]
    )
    totals = trace.finish()["totals"]
    assert totals["duplicate_search_calls"] == 1
    assert totals["next_actions_offered"] == 1
    assert totals["next_actions_adopted"] == 1
    assert totals["max_search_streak"] == 2
    assert totals["first_hit_to_first_evidence"] == 2
    assert totals["edit_calls"] == 1


def test_trace_adopts_one_next_action_without_dropping_the_rest() -> None:
    from trace import process_metrics

    metrics = process_metrics(
        [
            {
                "tool": "find_code_symbols",
                "next_actions": [
                    {"tool": "read_file", "file": "src/user.py"},
                    {"tool": "read_symbol", "symbol_id": "src/user.py:UserService"},
                ],
            },
            {"tool": "read_file", "file_path": "src/user.py"},
            {"tool": "find_code_symbols"},
        ]
    )
    assert metrics["next_actions_offered"] == 1
    assert metrics["next_actions_adopted"] == 1


def test_summarize_reads_json_string_arguments() -> None:
    summary = summarize_tool_payload(
        "bash",
        json.dumps(
            {
                "command": "rg create_user src",
                "timeout": 300,
            }
        ),
        {"content": "src/user.py:1"},
    )
    assert summary["command"] == "rg create_user src"

    edit = summarize_tool_payload(
        "edit_file", json.dumps({"file_path": "src/user.py"}), {}
    )
    assert edit["file_path"] == "src/user.py"


def test_preload_skips_when_language_pack_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.code_graph_setup._language_pack_importable",
        lambda: False,
    )

    def fail_run(*_args, **_kwargs):
        raise AssertionError("must not download grammars when the pack is missing")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.code_graph_setup.subprocess.run",
        fail_run,
    )
    assert preload_code_graph_grammars() is False


def test_preload_skips_when_cache_is_already_warm(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.code_graph_setup._language_pack_importable",
        lambda: True,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.code_graph_setup._parser_already_ready",
        lambda: True,
    )

    def fail_run(*_args, **_kwargs):
        raise AssertionError("must not download grammars when the cache is warm")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.code_graph_setup.subprocess.run",
        fail_run,
    )
    assert preload_code_graph_grammars() is True


def test_config_dir_name_labels_the_profile() -> None:
    assert config_dir_name(profile=PROFILE_OFF) == "cfg_b__graph-off"
    assert config_dir_name(profile=PROFILE_GRAPH) == "cfg_b__graph"
    assert (
        config_dir_name(profile=PROFILE_GRAPH, prefix="pre-ab")
        == "cfg_pre-ab__graph"
    )


def _graph_yaml(**overrides: object) -> dict:
    raw = {
        "profile": "graph",
        "agent": "root",
        "max_files": 2000,
        "max_source_bytes": 16777216,
        "max_build_rss_mb": 4096,
        "max_cache_size_mb": 2048,
    }
    raw.update(overrides)
    return {"code_graph": raw}


def test_code_graph_reload_skips_when_knobs_unchanged() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    cfg = _graph_yaml()
    adapter._remember_code_graph_reload_fingerprint(cfg)
    assert adapter._sync_code_graph_rail_for_reload(cfg) is None


def test_code_graph_reload_rebuilds_when_max_files_changes() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    adapter._remember_code_graph_reload_fingerprint(_graph_yaml())
    rail = adapter._sync_code_graph_rail_for_reload(_graph_yaml(max_files=4000))
    assert rail is not None
    assert rail.config.max_files == 4000
    assert adapter._code_graph_needs_warmup is True


def test_code_graph_reload_rebuilds_when_rss_cap_changes() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    adapter._remember_code_graph_reload_fingerprint(_graph_yaml())
    rail = adapter._sync_code_graph_rail_for_reload(_graph_yaml(max_build_rss_mb=2048))
    assert rail is not None
    assert rail.config.max_build_rss_mb == 2048


def test_code_graph_reload_turns_graph_off() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    adapter._remember_code_graph_reload_fingerprint(_graph_yaml())
    rail = adapter._sync_code_graph_rail_for_reload(_graph_yaml(profile="off"))
    assert rail is not None
    assert rail.profile.value == PROFILE_OFF
    assert adapter._code_graph_needs_warmup is False


def test_code_graph_cache_dir_is_absolute_under_agent_workspace(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    workspace = tmp_path / "agent-ws"
    project = tmp_path / "user-project"
    adapter._agent_workspace_dir = str(workspace)
    adapter._project_dir = str(project)
    cfg = adapter._build_code_graph_config(_graph_yaml())
    assert Path(cfg.cache_dir).is_absolute()
    assert Path(cfg.cache_dir) == (workspace / ".code_graph_cache").resolve()
    assert str(project) not in cfg.cache_dir


def test_code_graph_relative_cache_dir_follows_workspace(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    workspace = tmp_path / "agent-ws"
    adapter._agent_workspace_dir = str(workspace)
    cfg = adapter._build_code_graph_config(_graph_yaml(cache_dir="graphs"))
    assert Path(cfg.cache_dir) == (workspace / "graphs").resolve()
