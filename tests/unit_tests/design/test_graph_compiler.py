# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the designer graph → swarmflow script compiler.

Covers the three compile-time behaviours that the system tests exercise only
end-to-end:

* wave scheduling (F15 §12) — serial chains stay serial, fan-out compiles to
  one engine ``parallel([...])`` block with per-branch gates, and the compiled
  fan-out script actually runs through the engine with the old-executor
  failure semantics (branch failure → None → downstream gated off);
* ``options.agent_template`` (F15 §13) — every agent() call carries the
  node's AgentGroup member template reference, the engine option whitelist
  accepts it against ``TeamWorkerBackend.KNOWN_OPTIONS`` and rejects it
  against a bare backend;
* version-in-phase regression — the node version still rides ``phase="v{n}"``
  and frozen nodes contribute literals (no agent call).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from openjiuwen.agent_teams.workflow.backends.team_worker_backend import (
    TeamWorkerBackend,
)

from jiuwenswarm.server.runtime.design.graph_compiler import GraphScriptCompiler


def _chain_graph():
    return {
        "graph_id": "g_chain",
        "nodes": [
            {"id": "n1", "type": "text", "label": "A", "config": {"prompt": "a", "pipeline": "brief"}},
            {"id": "n2", "type": "text", "label": "B", "config": {"prompt": "b", "pipeline": "brief"}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
    }


def _fanout_graph():
    return {
        "graph_id": "g_fan",
        "nodes": [
            {"id": "n0", "type": "text", "label": "outline", "config": {"prompt": "o", "pipeline": "brief"}},
            {"id": "n1", "type": "text", "label": "B1", "config": {"prompt": "b1", "pipeline": "brief"}},
            {"id": "n2", "type": "text", "label": "B2", "config": {"prompt": "b2", "pipeline": "brief"}},
            {"id": "n3", "type": "text", "label": "C", "config": {"prompt": "c", "pipeline": "brief"}},
        ],
        "edges": [
            {"id": "e1", "source": "n0", "target": "n1"},
            {"id": "e2", "source": "n0", "target": "n2"},
            {"id": "e3", "source": "n1", "target": "n3"},
            {"id": "e4", "source": "n2", "target": "n3"},
        ],
    }


# ── wave scheduling: source form ──────────────────────────────────


def test_chain_stays_serial():
    src = GraphScriptCompiler().compile(_chain_graph(), {})
    assert "parallel([" not in src
    assert "await agent(" in src


def test_fanout_compiles_to_parallel():
    src = GraphScriptCompiler().compile(_fanout_graph(), {})
    assert "await parallel([" in src
    # wave 1 (single node) stays serial, before the barrier
    assert src.index("r_n0 = await agent(") < src.index("await parallel(")
    # wave 3 (join) runs after the barrier
    assert src.index("r_n3 = await agent(") > src.index("])")
    # wave results destructured
    assert "_wave[0]" in src and "_wave[1]" in src


def test_frozen_root_first_wave_is_parallel():
    src = GraphScriptCompiler().compile(
        _fanout_graph(), {"n0": {"text": "frozen outline"}}
    )
    assert '"frozen outline"' in src
    assert "await parallel([" in src


def test_explicit_agent_template_wins():
    g = {
        "graph_id": "g_explicit",
        "nodes": [
            {
                "id": "n1",
                "type": "text",
                "label": "角色",
                "config": {
                    "prompt": "x",
                    "agent_template": "designer/character",
                },
            }
        ],
        "edges": [],
    }
    src = GraphScriptCompiler().compile(g, {})
    # SDD-0030: the compiler strips the legacy "designer/" prefix AND emits
    # swarmflow's uniform double-quoted style.
    assert '"agent_template": "character"' in src


def test_default_template_per_type():
    src = GraphScriptCompiler().compile(_chain_graph(), {})
    # pipeline nodes default to their designer member (bare name, SDD-0030)
    normalized = src.replace('"', "'")
    assert "'agent_template': 'brief'" in normalized


def test_member_calls_carry_ticket_and_output_schema():
    src = GraphScriptCompiler().compile(_chain_graph(), {}, run_id="run_1")
    assert "NODE_OUTPUT_SCHEMA = {" in src
    assert "shot_ticket: design:g_chain:run_1:n1" in src
    agent_lines = [ln for ln in src.splitlines() if "await agent(" in ln]
    assert agent_lines and all("schema=NODE_OUTPUT_SCHEMA" in ln for ln in agent_lines)


def test_generic_text_node_is_rejected():
    g = {
        "graph_id": "g_plain",
        "nodes": [{"id": "n1", "type": "text", "label": "A", "config": {"prompt": "a"}}],
        "edges": [],
    }
    with pytest.raises(ValueError, match="no pipeline"):
        GraphScriptCompiler().compile(g, {})


def test_version_in_phase_and_frozen_literal():
    g = _fanout_graph()
    # n1 frozen (literal), n2 carries version 3 → its live call encodes v3.
    src = GraphScriptCompiler().compile(
        g, {"n1": {"text": "frozen"}, "n2": {"version": 3}}
    )
    assert 'phase="v3"' in src
    agent_lines = [ln for ln in src.splitlines() if "await agent(" in ln]
    assert 'label="n1"' not in " ".join(agent_lines)  # frozen → no call
    assert '"frozen"' in src  # n1 contributes its text as a literal


def test_frozen_literal_reads_edited_output_file(tmp_path: Path):
    output = tmp_path / "n1.md"
    output.write_text("edited by user", encoding="utf-8")
    ref = {"kind": "text", "uri": output.as_uri(), "mime_type": "text/markdown"}
    src = GraphScriptCompiler().compile(
        _chain_graph(), {"n1": {"text": "stale", "output_ref": ref}}
    )
    assert '"edited by user"' in src
    assert '"stale"' not in src


def test_frozen_literal_falls_back_to_stored_text(tmp_path: Path):
    missing = {"kind": "text", "uri": (tmp_path / "gone.md").as_uri()}
    image = tmp_path / "n1.png"
    image.write_bytes(b"\x89PNG")
    media = {"kind": "image", "uri": image.as_uri(), "mime_type": "image/png"}
    for ref in (missing, media):
        src = GraphScriptCompiler().compile(
            _chain_graph(), {"n1": {"text": "stored", "output_ref": ref}}
        )
        assert '"stored"' in src


# ── engine option whitelist ───────────────────────────────────────


class _RT:
    backend = TeamWorkerBackend.__new__(TeamWorkerBackend)


class _BareRT:
    class backend:
        KNOWN_OPTIONS = frozenset()


def test_whitelist_accepts_agent_template():
    from openjiuwen.agent_teams.workflow.engine.primitives import _build_opts

    opts = _build_opts(
        _RT(), {"label": "n2"}, {"agent_template": "storyboard"}
    )
    assert opts.get("agent_template") == "storyboard"


def test_bare_backend_rejects_agent_template():
    from openjiuwen.agent_teams.workflow.engine.errors import EngineError
    from openjiuwen.agent_teams.workflow.engine.primitives import _build_opts

    with pytest.raises(EngineError):
        _build_opts(_BareRT(), {"label": "x"}, {"agent_template": "y"})


# ── compiled fan-out executes through the engine ─────────────────


def test_handler_reference_nodes_compile_to_literals():
    """Uploaded reference nodes (n_ref_*) materialize as literals, never agent() calls."""
    g = {
        "graph_id": "g_refs",
        "nodes": [
            {
                "id": "n_ref_01", "type": "image", "label": "hero.png",
                "config": {
                    "prompt": "Reference 1: hero.png", "role": "image",
                    "delegate": "handler", "skip_llm": True, "read_only": True,
                    "user_reference_path": "C:/refs/hero.png",
                    "upload": {"uri": "file:///C:/refs/hero.png"},
                },
            },
            {"id": "n_char", "type": "text", "label": "角色",
             "config": {"prompt": "参考 hero.png 设计角色", "pipeline": "character"}},
        ],
        "edges": [{"id": "e1", "source": "n_ref_01", "target": "n_char"}],
    }
    src = GraphScriptCompiler().compile(g, {})
    agent_lines = [ln for ln in src.splitlines() if "await agent(" in ln]
    assert 'label="n_ref_01"' not in " ".join(agent_lines)
    assert "user-uploaded reference media" in src
    assert 'label="n_char"' in " ".join(agent_lines)
    # the literal rides into the downstream prompt inline
    assert "user-uploaded reference media" in src


def test_handler_reference_node_script_runs():
    """The compiled script binds r_<ref> before returning it (no NameError)."""
    from openjiuwen.agent_teams.workflow.engine import run_workflow
    from openjiuwen.agent_teams.workflow.engine.backends.base import (
        AgentBackend,
        AgentResult,
    )

    class FakeBackend(AgentBackend):
        KNOWN_OPTIONS = frozenset({"agent_template"})

        async def run(self, prompt, opts, schema_json, *, call_key=None):
            label = str(opts.get("label"))
            ref = {"kind": "text", "uri": f"file:///tmp/{label}.md"}
            return AgentResult(text=label, structured={"output_ref": ref})

    g = {
        "graph_id": "g_refs_run",
        "nodes": [
            {
                "id": "n_ref_01", "type": "image", "label": "girl.png",
                "config": {
                    "delegate": "handler", "force_handler": True,
                    "skip_llm": True, "read_only": True,
                    "user_reference_id": "ref_01",
                    "user_reference_path": "/tmp/refs/girl.png",
                },
            },
            {"id": "n_char", "type": "text", "label": "角色",
             "config": {"prompt": "参考 girl.png 设计角色", "pipeline": "character"}},
        ],
        "edges": [{"id": "e1", "source": "n_ref_01", "target": "n_char"}],
    }
    src = GraphScriptCompiler().compile(g, {}, run_id="run_refs")
    path = Path(tempfile.gettempdir()) / "_ut_handler_ref_run.py"
    path.write_text(src, encoding="utf-8")
    result = asyncio.run(run_workflow(str(path), args=None, backend=FakeBackend()))

    assert "user-uploaded reference media (ref_01)" in result["n_ref_01"]
    assert result["n_char"]["output_ref"]["uri"] == "file:///tmp/n_char.md"


def test_wave_concurrency_cap_batches_large_waves():
    """A 5-branch wave splits into batches of at most _MAX_WAVE_CONCURRENCY."""
    from jiuwenswarm.server.runtime.design.graph_compiler import (
        _MAX_WAVE_CONCURRENCY,
    )

    g = {
        "graph_id": "g_wide",
        "nodes": [
            {"id": "root", "type": "text", "label": "R", "config": {"prompt": "r", "pipeline": "brief"}},
            *[{
                "id": f"b{i}", "type": "text", "label": f"B{i}",
                "config": {"prompt": f"b{i}", "pipeline": "brief"},
            } for i in range(5)],
        ],
        "edges": [
            *[{"id": f"e{i}", "source": "root", "target": f"b{i}"} for i in range(5)],
        ],
    }
    src = GraphScriptCompiler().compile(g, {})
    blocks = [ln for ln in src.splitlines() if ln.startswith("    _wave = await parallel([")]
    expected_blocks = -(-5 // _MAX_WAVE_CONCURRENCY)  # ceil(5 / cap)
    assert len(blocks) == expected_blocks, f"{len(blocks)} blocks vs expected {expected_blocks}"
    # Each block carries at most cap thunks
    for ln in blocks:
        assert ln.count("_thunk_") <= _MAX_WAVE_CONCURRENCY


def test_fanout_engine_run_gates_downstream():
    from openjiuwen.agent_teams.workflow.engine import run_workflow
    from openjiuwen.agent_teams.workflow.engine.backends.base import (
        AgentBackend,
        AgentResult,
    )

    class FakeBackend(AgentBackend):
        KNOWN_OPTIONS = frozenset({"agent_template"})

        def __init__(self):
            super().__init__()
            self.calls: list[str] = []
            self.concurrent = 0
            self.max_concurrent = 0

        async def run(self, prompt, opts, schema_json, *, call_key=None):
            self.calls.append(str(opts.get("label")))
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            try:
                await asyncio.sleep(0.15)  # force the overlap window
                label = str(opts.get("label"))
                if label == "n1":
                    raise RuntimeError("injected n1 failure")
                ref = {"kind": "text", "uri": f"file:///tmp/{label}.md"}
                return AgentResult(text=f"<<{label}>>", structured={"output_ref": ref})
            finally:
                self.concurrent -= 1

    backend = FakeBackend()
    src = GraphScriptCompiler().compile(_fanout_graph(), {})
    path = Path(tempfile.gettempdir()) / "_ut_wave_fanout.py"
    path.write_text(src, encoding="utf-8")
    # Force agents_per_run >= 2: CI hosts with cpu_count<=3 resolve the default
    # cap to 1 and would serialize the fan-out, defeating the overlap check.
    result = asyncio.run(
        run_workflow(str(path), args=None, backend=backend, cap=4)
    )

    # All three pre-gate nodes attempted; the join node gated off at runtime
    # (old-executor semantics: a failed predecessor blocks its dependents).
    assert "n0" in backend.calls and "n1" in backend.calls and "n2" in backend.calls
    assert "n3" not in backend.calls
    assert backend.max_concurrent >= 2  # branches overlapped
    assert result.get("n0")["output_ref"]["uri"] == "file:///tmp/n0.md"
    assert result.get("n1") is None
    assert result.get("n2")["output_ref"]["uri"] == "file:///tmp/n2.md"
    assert result.get("n3") is None
