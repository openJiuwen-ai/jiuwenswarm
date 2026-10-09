# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bridge transport: ledger, cache, replay and the capability-pack funnels. Offline."""

import asyncio
import base64
import json

import pytest

from jiuwenswarm.agents.harness.common.research_bridge import model_transport as mt


@pytest.fixture
def fake_agent(monkeypatch):
    calls = []

    async def agent_call(self, model, requested, system, prompt, temperature, images):
        if prompt.startswith("{"):
            calls.append({"model": model, "requested": requested, "system": system, "prompt": prompt})
            return {"text": prompt}, [{"model": model, "input_tokens": 1, "output_tokens": 1}]
        calls.append({"model": model, "requested": requested, "system": system, "prompt": prompt})
        row = {"model": model, "input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
        return {"text": f"echo:{prompt}", "rigor_findings": ["F1"]}, [dict(row, kind="chat", requested_model=requested)]

    monkeypatch.setattr(mt.Transport, "_agent_call", agent_call)
    monkeypatch.setenv("RESEARCH_CHAT_MODEL", "served-model")
    return calls


def _ledger(run):
    return [json.loads(line) for line in (run / "usage.jsonl").read_text().splitlines()]


def test_cache_ledger_and_replay(tmp_path, fake_agent):
    t = mt.Transport(tmp_path)
    with t.step("polish/review"):
        assert t.chat("a", requested_model="openai/gpt-x") == "echo:a"
        assert t.chat("a", requested_model="openai/gpt-x") == "echo:a"
    assert len(fake_agent) == 1 and fake_agent[0]["model"] == "served-model"
    rows = _ledger(tmp_path)
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[0]["step"] == "polish/review" and rows[0]["origin"] == "jiuwenswarm"
    assert rows[0]["requested_model"] == "openai/gpt-x" and rows[0]["latency_s"] >= 0
    assert t.rigor_findings == ["F1", "F1"]

    replay = mt.Transport(tmp_path, replay=True)
    assert replay.chat("a", requested_model="openai/gpt-x") == "echo:a"
    assert len(fake_agent) == 1
    assert _ledger(tmp_path)[-1]["seq"] == 3 and _ledger(tmp_path)[-1]["replayed"] is True
    with pytest.raises(SystemExit):
        replay.chat("never asked")


def test_research_harness_funnel_books_usage(tmp_path, fake_agent):
    pytest.importorskip("research_harness")
    t = mt.Transport(tmp_path)
    t.install()
    from research_harness.primitives import exemplar_impls
    before = len(exemplar_impls.usage_report()["calls"])
    assert exemplar_impls._direct_chat("gpt-x", "hello") == "echo:hello"
    calls = exemplar_impls.usage_report()["calls"][before:]
    assert [(c["prompt_tokens"], c["completion_tokens"]) for c in calls] == [(10, 2)]


def test_review_panel_runs_through_transport(tmp_path, fake_agent, monkeypatch):
    pytest.importorskip("research_harness")
    from jiuwenswarm.agents.harness.common.research_bridge import discriminator, tools
    reply = '```json\n{"findings": [{"category": "c", "severity": "fatal", "evidence": "e"}]}\n```'
    monkeypatch.setattr(mt.Transport, "chat", lambda self, prompt, **kw: fake_agent.append(kw) or reply)
    tex = tmp_path / "m.tex"
    tex.write_text("\\begin{abstract}Our ledger achieves 0.9.\\end{abstract}")
    out = tools.arena_panel(mt.Transport(tmp_path), str(tex), str(tmp_path / "panel.json"))
    assert out["terms_renamed"] == 1 and all(out[a] == 1 for a in discriminator.AXES)
    assert [kw["system"].split("COVERAGE AXIS: ")[1].split(".")[0] for kw in fake_agent] == list(discriminator.AXES)
    assert all(kw["temperature"] == 0.0 for kw in fake_agent)
    finding = json.loads((tmp_path / "panel.json").read_text())["findings"]["statistics"][0]
    assert finding["severity"] == "major" and finding["finding_id"] == "P-statistics-001"
    assert discriminator.review_axis("x", "statistics", lambda s, u: "no json")[0]["category"] == "panel_llm_error"


def test_genai_config_and_images():
    png = b"\x89PNG fake"
    contents = [{"type": "text", "text": "x"},
                {"type": "image", "image_base64": base64.b64encode(png).decode()},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(png).decode()}}]
    assert mt._content_images(contents) == [(png, "image/jpeg"), (png, "image/jpeg")]
    assert mt._genai_config({"system_instruction": "sys", "temperature": 0.5, "candidate_count": 2}) == ("sys", 0.5, 2)


def test_read_findings_ranks_both_review_outputs(tmp_path):
    from jiuwenswarm.agents.harness.common.research_bridge.tools import build_tools, read_findings

    polish = tmp_path / "polish.json"
    polish.write_text(json.dumps({"findings": [
        {"angle": "clarity", "severity": "low", "location": "intro", "issue": "wordy", "fix": "cut"},
        {"angle": "methods_stats", "severity": "high", "location": "results", "issue": "overclaim", "fix": "hedge"}]}))
    panel = tmp_path / "panel.json"
    panel.write_text(json.dumps({"findings": {"statistics": [
        {"severity": "major", "location": "table 1", "evidence": "CI misreported", "recommended_check": "recompute"}],
        "domain_realism": []}}))
    assert [f["source"] for f in read_findings(str(polish))] == ["polish/methods_stats", "polish/clarity"]
    assert read_findings(str(panel)) == [{"source": "arena/statistics", "severity": "major", "location": "table 1",
                                          "issue": "CI misreported", "fix": "recompute"}]
    tool = next(t for t in build_tools(mt.Transport(tmp_path)) if t.card.name == "read_findings")
    out = asyncio.run(tool.invoke({"path": str(polish), "limit": 1}))
    assert json.loads(out)[0]["issue"] == "overclaim"
