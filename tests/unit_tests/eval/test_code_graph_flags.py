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

from jiuwenswarm.server.runtime.agent_adapter.code_graph_flags import (  # noqa: E402
    PROFILE_GRAPH,
    PROFILE_OFF,
    admit_code_graph_workspace,
    apply_code_graph_profile,
    enable_code_agent_subagent,
    format_source_volume_for_yaml,
    parse_source_volume_to_bytes,
    product_code_graph_config,
    resolve_code_graph_flags,
    rewrite_code_graph_limit_message,
)
from jiuwenswarm.server.runtime.agent_adapter.code_graph_setup import (  # noqa: E402
    preload_code_graph_grammars,
)
from coding_agent import config_dir_name  # noqa: E402
from trajectory import summarize_tool_payload  # noqa: E402


def test_product_code_graph_config_reads_live_caps() -> None:
    cfg = product_code_graph_config({"code_graph": {"profile": "graph", "max_files": 50}})
    assert cfg.max_files == 50


def test_product_code_graph_config_keeps_zero_cache_cap() -> None:
    cfg = product_code_graph_config({"code_graph": {"max_cache_size_mb": 0}})
    assert cfg.max_cache_size_mb == 0


def test_eval_graph_config_uses_product_fields(tmp_path: Path) -> None:
    from coding_agent import _graph_config

    cfg = _graph_config(
        {
            "code_graph": {
                "max_files": 12,
                "max_source_bytes": "8MB",
                "max_build_rss_mb": 512,
                "max_cache_size_mb": 256,
            }
        },
        tmp_path,
        tmp_path / "cache",
    )
    assert cfg.max_files == 12
    assert cfg.max_source_bytes == 8 * 1024 * 1024
    assert cfg.max_build_rss_mb == 512
    assert cfg.max_cache_size_mb == 256
    assert Path(cfg.cache_dir) == (tmp_path / "cache").resolve()


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
    from trajectory import EvalTrace

    trace = EvalTrace(repo_root="/tmp/repo")
    for name in ("find_callers", "submit_code_context", "grep"):
        trace.tool_events.append({"tool": name, "duration_ms": 1.0})
    totals = trace.finish()["totals"]
    assert totals["find_callers_calls"] == 1
    assert totals["submit_code_context_calls"] == 1
    assert totals["grep_calls"] == 1
    assert totals["graph_tool_calls"] == 2


def test_trace_totals_carry_the_process_metrics() -> None:
    from trajectory import EvalTrace

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
    from trajectory import process_metrics

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


def test_code_graph_reload_fingerprint_match_on_root_still_needs_apply() -> None:
    """create already remembered the fingerprint; configure still restores grep."""
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    cfg = _graph_yaml()
    adapter._remember_code_graph_reload_fingerprint(cfg)
    assert adapter._sync_code_graph_rail_for_reload(cfg) is None
    assert adapter._code_graph_rail_needs_apply is True
    assert adapter._code_graph_profile_rail is not None


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
    assert adapter._code_graph_rail_needs_apply is True


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
    assert adapter._code_graph_rail_needs_apply is True


def test_root_rail_stays_off_when_graph_hangs_on_code_agent() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    rail = adapter._build_code_graph_profile_rail(
        {"code_graph": {"profile": "graph", "agent": "code_agent"}}
    )
    assert rail.profile.value == PROFILE_OFF


def test_root_rail_is_graph_when_hang_is_root() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    rail = adapter._build_code_graph_profile_rail(_graph_yaml())
    assert rail.profile.value == PROFILE_GRAPH


def test_off_create_does_not_hang_code_graph_rail() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    specs = adapter._build_profile_rail_specs(
        {},
        {"code_graph": {"profile": "off", "agent": "root"}},
        mode="code",
    )
    names = [spec.attr_name for spec in specs.after_permission]
    assert "_code_graph_profile_rail" not in names


def test_graph_on_root_create_hangs_code_graph_rail() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    specs = adapter._build_profile_rail_specs({}, _graph_yaml(), mode="code")
    names = [spec.attr_name for spec in specs.after_permission]
    assert "_code_graph_profile_rail" in names


def test_apply_code_graph_rail_now_inits_the_swapped_rail() -> None:
    import asyncio

    from openjiuwen.harness.rails.code_graph_profile_rail import CodeGraphProfileRail

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    old = CodeGraphProfileRail(PROFILE_OFF)
    new = CodeGraphProfileRail(PROFILE_GRAPH)
    adapter = JiuwenSwarmCodeAdapter()
    adapter._code_graph_profile_rail = new

    class _FakeDeep:
        def __init__(self) -> None:
            self.registered = [old]
            self.pending = [new]
            self.unregistered: list[object] = []
            self.register_calls: list[object] = []

        def find_rails_by_type(self, types: tuple[type, ...]) -> list[object]:
            return [rail for rail in (*self.pending, *self.registered) if isinstance(rail, types)]

        async def unregister_rail(self, rail: object) -> None:
            self.unregistered.append(rail)
            self.registered = [item for item in self.registered if item is not rail]

        def remove_pending_rail(self, rail: object) -> None:
            self.pending = [item for item in self.pending if item is not rail]

        def is_registered_rail(self, rail: object) -> bool:
            return rail in self.registered

        async def register_rail(self, rail: object) -> None:
            self.register_calls.append(rail)
            self.registered.append(rail)

    fake = _FakeDeep()
    adapter._instance = fake
    asyncio.run(adapter._apply_code_graph_rail_now())
    assert old in fake.unregistered
    assert fake.pending == []
    assert fake.register_calls == [new]
    assert new in fake.registered


def test_apply_code_graph_rail_now_rehangs_when_already_registered() -> None:
    """Pending-init during ensure_initialized must not skip the final graph hang."""
    import asyncio

    from openjiuwen.harness.rails.code_graph_profile_rail import CodeGraphProfileRail

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    rail = CodeGraphProfileRail(PROFILE_GRAPH)
    adapter = JiuwenSwarmCodeAdapter()
    adapter._code_graph_profile_rail = rail

    class _FakeDeep:
        def __init__(self) -> None:
            self.registered = [rail]
            self.pending: list[object] = []
            self.unregistered: list[object] = []
            self.register_calls: list[object] = []

        def find_rails_by_type(self, types: tuple[type, ...]) -> list[object]:
            return [item for item in self.registered if isinstance(item, types)]

        async def unregister_rail(self, item: object) -> None:
            self.unregistered.append(item)
            self.registered = [r for r in self.registered if r is not item]

        def remove_pending_rail(self, item: object) -> None:
            self.pending = [r for r in self.pending if r is not item]

        def is_registered_rail(self, item: object) -> bool:
            return item in self.registered

        async def register_rail(self, item: object) -> None:
            self.register_calls.append(item)
            self.registered.append(item)

    fake = _FakeDeep()
    adapter._instance = fake
    asyncio.run(adapter._apply_code_graph_rail_now())
    assert fake.unregistered == [rail]
    assert fake.register_calls == [rail]
    assert rail in fake.registered


def test_apply_code_graph_rail_abandons_when_project_already_over_limit(monkeypatch) -> None:
    import asyncio

    from openjiuwen.harness.rails.code_graph_profile_rail import CodeGraphProfileRail

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    abandoned: list[str] = []

    class _Rail(CodeGraphProfileRail):
        def abandon_graph(self, agent=None, *, reason: str = "") -> None:
            abandoned.append(reason)

    rail = _Rail(PROFILE_GRAPH)
    adapter = JiuwenSwarmCodeAdapter()
    adapter._code_graph_profile_rail = rail
    adapter._project_dir = "/tmp/over-limit-repo"

    class _FakeDeep:
        def __init__(self) -> None:
            self.registered: list[object] = []
            self.pending: list[object] = []

        def find_rails_by_type(self, types: tuple[type, ...]) -> list[object]:
            return []

        async def unregister_rail(self, item: object) -> None:
            return None

        def remove_pending_rail(self, item: object) -> None:
            return None

        async def register_rail(self, item: object) -> None:
            self.registered.append(item)

    class _Mgr:
        def stats(self, workspace, config=None):
            return {
                "state": "stale",
                "limit_exceeded": True,
                "message": "max_files is 4, cap is 3",
            }

    adapter._instance = _FakeDeep()
    monkeypatch.setattr(
        "openjiuwen.core.retrieval.code_graph.manager.get_code_graph_manager",
        lambda _cfg=None: _Mgr(),
    )
    asyncio.run(adapter._apply_code_graph_rail_now())
    assert abandoned
    assert "max_files" in abandoned[0]


def test_code_graph_reload_fingerprint_match_off_does_not_reapply() -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    cfg = _graph_yaml(profile="off")
    adapter._remember_code_graph_reload_fingerprint(cfg)
    assert adapter._sync_code_graph_rail_for_reload(cfg) is None
    assert adapter._code_graph_rail_needs_apply is False


def test_apply_builds_missing_rail_when_off_session_switches_on() -> None:
    import asyncio

    from openjiuwen.harness.rails.code_graph_profile_rail import CodeGraphProfileRail

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    adapter._code_graph_profile_rail = None
    adapter._config_base_cache = _graph_yaml()

    class _FakeDeep:
        def __init__(self) -> None:
            self.registered: list[object] = []
            self.pending: list[object] = []
            self.register_calls: list[object] = []

        def find_rails_by_type(self, types: tuple[type, ...]) -> list[object]:
            return [item for item in self.registered if isinstance(item, types)]

        async def unregister_rail(self, item: object) -> None:
            self.registered = [rail for rail in self.registered if rail is not item]

        def remove_pending_rail(self, item: object) -> None:
            self.pending = [rail for rail in self.pending if rail is not item]

        async def register_rail(self, item: object) -> None:
            self.register_calls.append(item)
            self.registered.append(item)

    fake = _FakeDeep()
    adapter._instance = fake
    asyncio.run(adapter._apply_code_graph_rail_now())
    assert adapter._code_graph_profile_rail is not None
    assert isinstance(adapter._code_graph_profile_rail, CodeGraphProfileRail)
    assert fake.register_calls == [adapter._code_graph_profile_rail]


def test_conceal_hides_grep_when_find_already_registered() -> None:
    from openjiuwen.harness.rails.code_graph_profile_rail import CodeGraphProfileRail

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    class _Card:
        def __init__(self, name: str) -> None:
            self.name = name
            self.id = name

    class _Mgr:
        def __init__(self) -> None:
            self.tools = {
                "resolve_symbol": _Card("resolve_symbol"),
                "grep": _Card("grep"),
                "glob": _Card("glob"),
            }

        def get(self, name: str):
            return self.tools.get(name)

        def remove_ability(self, name: str) -> None:
            self.tools.pop(name, None)

        def list(self):
            return list(self.tools.values())

    rail = CodeGraphProfileRail(PROFILE_GRAPH)
    adapter = JiuwenSwarmCodeAdapter()
    adapter._code_graph_profile_rail = rail
    adapter._config_base_cache = _graph_yaml()

    class _FakeDeep:
        def __init__(self) -> None:
            self.ability_manager = _Mgr()

        def find_rails_by_type(self, types: tuple[type, ...]) -> list[object]:
            return [rail]

    fake = _FakeDeep()
    adapter._conceal_grep_if_graph_live(fake, rail)
    assert fake.ability_manager.get("grep") is None
    assert fake.ability_manager.get("glob") is None
    assert fake.ability_manager.get("resolve_symbol") is not None


def test_reload_applies_graph_rail_after_ensure_initialized() -> None:
    import asyncio
    from unittest.mock import AsyncMock, patch

    from openjiuwen.harness.rails.code_graph_profile_rail import CodeGraphProfileRail

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    rail = CodeGraphProfileRail(PROFILE_GRAPH)
    adapter = JiuwenSwarmCodeAdapter()
    adapter._code_graph_profile_rail = rail
    adapter._code_graph_rail_needs_apply = True
    adapter._code_graph_needs_warmup = False
    order: list[str] = []

    class _FakeDeep:
        def __init__(self) -> None:
            self.registered = [rail]
            self.pending: list[object] = []

        async def ensure_initialized(self) -> None:
            order.append("ensure")

        def find_rails_by_type(self, types: tuple[type, ...]) -> list[object]:
            return [item for item in self.registered if isinstance(item, types)]

        async def unregister_rail(self, item: object) -> None:
            order.append("unregister")
            self.registered = [r for r in self.registered if r is not item]

        def remove_pending_rail(self, item: object) -> None:
            return None

        async def register_rail(self, item: object) -> None:
            order.append("register")
            self.registered.append(item)

    adapter._instance = _FakeDeep()

    async def _super_reload(*_args: object, **_kwargs: object) -> None:
        order.append("super")

    with patch.object(JiuWenSwarmDeepAdapter, "reload_agent_config", new=AsyncMock(side_effect=_super_reload)):
        asyncio.run(adapter.reload_agent_config({"code_graph": {"profile": "graph"}}))
    assert order == ["super", "ensure", "unregister", "register"]
    assert adapter._code_graph_rail_needs_apply is False


def test_enable_code_agent_subagent_writes_enabled_true() -> None:
    cfg: dict = {}
    enable_code_agent_subagent(cfg)
    assert cfg["react"]["subagents"]["code_agent"]["enabled"] is True


def test_graph_on_code_agent_builds_subagent_even_when_yaml_disabled(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    adapter._workspace_dir = str(tmp_path)
    adapter._project_dir = str(tmp_path)
    adapter._coding_memory_rail = None
    adapter._sys_operation = MagicMock()
    with patch.object(adapter, "_browser_runtime_enabled", return_value=False):
        subagents, _ = adapter._build_configured_subagents(
            MagicMock(),
            {"subagents": {"code_agent": {"enabled": False}}, "max_iterations": 15},
            {"code_graph": {"profile": "graph", "agent": "code_agent"}},
        )
    names = [spec.agent_card.name for spec in subagents]
    assert "code_agent" in names


def test_graph_on_root_does_not_force_code_agent_subagent(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    adapter = JiuwenSwarmCodeAdapter()
    adapter._workspace_dir = str(tmp_path)
    adapter._project_dir = str(tmp_path)
    adapter._coding_memory_rail = None
    adapter._sys_operation = MagicMock()
    with patch.object(adapter, "_browser_runtime_enabled", return_value=False):
        subagents, _ = adapter._build_configured_subagents(
            MagicMock(),
            {"subagents": {"code_agent": {"enabled": False}}, "max_iterations": 15},
            {"code_graph": {"profile": "graph", "agent": "root"}},
        )
    names = [spec.agent_card.name for spec in subagents]
    assert "code_agent" not in names


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


def test_rewrite_rss_limit_message_uses_mb_not_bytes() -> None:
    raw = (
        "Code Graph limit exceeded: max_build_rss_mb is 151748608, cap is 1048576. "
        "This repository is too large to index."
    )
    text = rewrite_code_graph_limit_message(raw)
    assert "151748608" not in text
    assert "1048576" not in text
    assert "max_build_rss_mb is 144.7, cap is 1" in text


def test_rewrite_disk_limit_message_uses_mb_not_bytes() -> None:
    raw = "max_cache_size_mb is 2097152, cap is 1048576"
    assert rewrite_code_graph_limit_message(raw) == "max_cache_size_mb is 2, cap is 1"


def test_rewrite_max_files_message_is_unchanged() -> None:
    raw = "max_files is 4, cap is 3"
    assert rewrite_code_graph_limit_message(raw) == raw


def test_parse_source_volume_accepts_mb_and_legacy_bytes() -> None:
    assert parse_source_volume_to_bytes("40MB") == 41943040
    assert parse_source_volume_to_bytes("40") == 41943040
    assert parse_source_volume_to_bytes(40) == 41943040
    assert parse_source_volume_to_bytes(41943040) == 41943040
    assert parse_source_volume_to_bytes("1GB") == 1073741824
    assert format_source_volume_for_yaml(41943040) == "40MB"
    assert format_source_volume_for_yaml(1073741824) == "1GB"


def test_product_config_reads_40mb_yaml() -> None:
    cfg = product_code_graph_config({"code_graph": {"max_source_bytes": "40MB"}})
    assert cfg.max_source_bytes == 41943040


def test_admit_code_graph_workspace_refuses_over_max_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8")
    cfg = product_code_graph_config({"code_graph": {"profile": "graph", "max_files": 1}})
    over = admit_code_graph_workspace(str(tmp_path), cfg)
    assert over is not None
    assert getattr(over, "limit", "") == "max_files"


def test_code_adapter_seeds_agent_workspace_not_project(tmp_path: Path, monkeypatch) -> None:
    from jiuwenswarm.server.runtime.agent_adapter.interface_code import (
        JiuwenSwarmCodeAdapter,
    )

    project = tmp_path / "user-project"
    agent_ws = tmp_path / "agent-ws"
    project.mkdir()
    agent_ws.mkdir()
    seen: dict[str, str | None] = {}

    def _fake_init_cwd(cwd, project_root=None, *, workspace=None, team_workspace=None):
        seen["cwd"] = cwd
        seen["project_root"] = project_root
        seen["workspace"] = workspace

    monkeypatch.setattr("openjiuwen.core.sys_operation.cwd.init_cwd", _fake_init_cwd)
    adapter = JiuwenSwarmCodeAdapter()
    adapter._project_dir = str(project)
    adapter._workspace_dir = str(project)
    adapter._agent_workspace_dir = str(agent_ws)
    adapter._seed_runtime_cwd(str(project), workspace=str(project))
    assert seen["cwd"] == str(project)
    assert seen["project_root"] == str(project)
    assert seen["workspace"] == str(agent_ws)
