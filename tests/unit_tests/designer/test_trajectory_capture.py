# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer trajectory files: OTLP spans, design records, and the global toggle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jiuwenswarm.observability.config import TrajectoryStoreSettings
from jiuwenswarm.server.runtime.designer import feedback, paths
from jiuwenswarm.server.runtime.designer.handlers.clip import generate_clip_video
from jiuwenswarm.server.runtime.designer.media_generation import DesignerVideoRequest
from jiuwenswarm.server.runtime.designer.trajectory import (
    TRAJECTORY_SCHEMA,
    begin_trajectory,
    current_trajectory_span,
    end_trajectory,
    get_trajectory,
)


def _settings(tmp_path: Path, *, enabled: bool) -> TrajectoryStoreSettings:
    return TrajectoryStoreSettings(
        enabled=enabled,
        database_path=tmp_path / ".trace" / "sessions",
        retention_days=7,
        queue_size=16,
        batch_size=4,
        flush_interval_ms=10,
    )


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "get_user_workspace_dir", lambda: tmp_path)
    return tmp_path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _spans(path: Path) -> list[dict[str, Any]]:
    return [
        line["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        for line in _read_jsonl(path)
    ]


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in span["attributes"]:
        value = item["value"]
        if "stringValue" in value:
            result[item["key"]] = value["stringValue"]
        elif "intValue" in value:
            result[item["key"]] = int(value["intValue"])
        elif "arrayValue" in value:
            result[item["key"]] = [v["stringValue"] for v in value["arrayValue"]["values"]]
        else:
            result[item["key"]] = next(iter(value.values()))
    return result


def test_files_are_keyed_by_project_under_data_dir(data_dir: Path) -> None:
    rec = begin_trajectory(
        "graph-1",
        "run-1",
        project_id="proj_abc",
        meta={"scenario": "video"},
        settings=_settings(data_dir, enabled=True),
    )
    with rec.span(agent_id="director", action="plan", phase="orchestration", role="director"):
        pass
    design_path = end_trajectory("run-1")

    directory = data_dir / ".trace" / "designer"
    assert design_path == str(directory / "proj_abc.design.jsonl")
    assert sorted(p.name for p in directory.iterdir()) == [
        "proj_abc.design.jsonl",
        "proj_abc.otlp.jsonl",
    ]
    records = _read_jsonl(directory / "proj_abc.design.jsonl")
    assert [r["kind"] for r in records] == ["run_started", "run_ended"]
    assert records[0]["schema_version"] == TRAJECTORY_SCHEMA
    assert records[0]["meta"] == {"scenario": "video"}
    assert records[1]["agents"]["director"]["role"] == "director"


def test_second_run_appends_to_same_project_files(data_dir: Path) -> None:
    for run_id in ("run-a", "run-b"):
        rec = begin_trajectory(
            "graph-1", run_id, project_id="proj_abc", settings=_settings(data_dir, enabled=True)
        )
        with rec.span(agent_id="director", action="plan"):
            pass
        end_trajectory(run_id)

    directory = data_dir / ".trace" / "designer"
    spans = _spans(directory / "proj_abc.otlp.jsonl")
    assert [_attrs(s)["openjiuwen.run.id"] for s in spans] == ["run-a", "run-b"]
    assert spans[0]["traceId"] != spans[1]["traceId"]


def test_trajectory_key_uses_project_id_only() -> None:
    assert paths.design_trajectory_key("proj_1") == "proj_1"
    assert paths.design_trajectory_key("default") == "default"
    unsafe = paths.design_trajectory_key("../escape")
    assert "/" not in unsafe and len(unsafe) == 64


def test_disabled_toggle_writes_nothing(data_dir: Path) -> None:
    rec = begin_trajectory(
        "graph-1", "run-off", project_id="proj_abc", settings=_settings(data_dir, enabled=False)
    )
    assert not rec.enabled
    assert get_trajectory("run-off") is None
    with rec.span(agent_id="director", action="plan") as payload:
        with current_trajectory_span(action="agent_call", detail={"prompt": "p"}):
            pass
        payload["note"] = "ignored"
    rec.record(agent_id="director", action="plan_result")
    rec.set_feedback({"final": {}})
    assert end_trajectory("run-off") is None
    assert not (data_dir / ".trace").exists()


def test_nested_calls_keep_complete_prompts_and_inputs(data_dir: Path) -> None:
    rec = begin_trajectory(
        "graph-exact", "run-exact", project_id="proj_exact", settings=_settings(data_dir, enabled=True)
    )
    prompt = "keep this prompt verbatim\nincluding the second line"
    tool_input = {
        "prompt": prompt,
        "nested": {"values": [1, 2, {"name": "unchanged"}]},
    }

    with rec.span(
        agent_id="director",
        action="plan",
        phase="orchestration",
        role="director",
    ):
        with current_trajectory_span(
            action="agent_call",
            phase="inference",
            detail={
                "agent_type": "chat_model",
                "prompt": prompt,
                "system_prompt": "exact system prompt",
                "input": {
                    "model": "qwen-plus",
                    "max_tokens": 128,
                    "messages": [
                        {"role": "system", "content": "exact system prompt"},
                        {"role": "user", "content": prompt},
                    ],
                },
            },
        ) as payload:
            payload["output"] = "model answer"
            payload["finish_reason"] = "stop"
            payload["usage"] = {"input_tokens": 11, "output_tokens": 3}
        with current_trajectory_span(
            action="tool_call",
            phase="tool",
            tool="example_tool",
            detail={"input": tool_input},
        ) as payload:
            payload["output"] = {"ok": True}
        rec.record(
            agent_id="director",
            action="plan_result",
            phase="orchestration",
            role="director",
            detail={"notes": "handoff"},
        )

    tool_input["nested"]["values"].append("mutated later")
    rec.set_feedback({"final": {"improvement_plan": "more light"}})
    end_trajectory("run-exact")

    directory = data_dir / ".trace" / "designer"
    spans = {s["name"]: s for s in _spans(directory / "proj_exact.otlp.jsonl")}
    parent = spans["director.plan"]
    chat = spans["chat qwen-plus"]
    tool = spans["execute_tool example_tool"]
    assert chat["parentSpanId"] == parent["spanId"] == tool["parentSpanId"]
    assert int(chat["endTimeUnixNano"]) >= int(chat["startTimeUnixNano"])

    chat_attrs = _attrs(chat)
    assert chat_attrs["gen_ai.operation.name"] == "chat"
    assert json.loads(chat_attrs["gen_ai.system_instructions"]) == [
        {"type": "text", "content": "exact system prompt"}
    ]
    assert json.loads(chat_attrs["gen_ai.input.messages"]) == [
        {"role": "user", "parts": [{"type": "text", "content": prompt}]}
    ]
    assert json.loads(chat_attrs["gen_ai.output.messages"])[0]["parts"][0]["content"] == "model answer"
    assert chat_attrs["gen_ai.usage.input_tokens"] == 11
    assert chat_attrs["gen_ai.request.max_tokens"] == 128
    assert chat_attrs["session.id"] == "proj_exact"

    tool_attrs = _attrs(tool)
    assert json.loads(tool_attrs["gen_ai.tool.call.arguments"]) == {
        "prompt": prompt,
        "nested": {"values": [1, 2, {"name": "unchanged"}]},
    }
    assert json.loads(tool_attrs["gen_ai.tool.call.result"]) == {"ok": True}

    records = _read_jsonl(directory / "proj_exact.design.jsonl")
    event = next(r for r in records if r["kind"] == "event")
    assert event["action"] == "plan_result"
    assert event["span_id"] == parent["spanId"]
    assert event["detail"] == {"notes": "handoff"}
    fb = next(r for r in records if r["kind"] == "feedback")
    assert fb["feedback"]["final"]["improvement_plan"] == "more light"
    summary = records[-1]
    assert summary["kind"] == "run_ended"
    assert summary["agents"]["director"]["agent_calls"] == 1
    assert summary["agents"]["director"]["tool_calls"] == 1


def test_span_error_sets_otlp_status(data_dir: Path) -> None:
    rec = begin_trajectory(
        "graph-err", "run-err", project_id="proj_err", settings=_settings(data_dir, enabled=True)
    )
    with pytest.raises(ValueError), rec.span(agent_id="n_1", action="execute", phase="node"):
        raise ValueError("boom")
    end_trajectory("run-err")
    span = _spans(data_dir / ".trace" / "designer" / "proj_err.otlp.jsonl")[0]
    assert span["status"] == {"code": 2, "message": "ValueError: boom"}
    assert _attrs(span)["error.type"] == "ValueError"


@pytest.mark.asyncio
async def test_video_backend_records_effective_tool_input(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = data_dir / "generated.mp4"
    output.write_bytes(b"video")
    received: list[DesignerVideoRequest] = []

    async def fake_generate(request: DesignerVideoRequest, *, save_dir: str | None = None) -> dict[str, str]:
        received.append(request)
        return {"video_path": str(output)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.media_generation.generate_video",
        fake_generate,
    )

    rec = begin_trajectory(
        "graph-video", "run-video", project_id="proj_video", settings=_settings(data_dir, enabled=True)
    )
    with rec.span(
        agent_id="n_clip_1",
        action="execute",
        phase="node",
        role="clip",
    ):
        result = await generate_clip_video(
            "exact video prompt",
            reference_images=["character.png", "scene.png"],
            duration=7,
            audio=True,
            force_reference_mode=True,
        )
    end_trajectory("run-video")

    assert result["video_path"] == str(output)
    span = next(
        s
        for s in _spans(data_dir / ".trace" / "designer" / "proj_video.otlp.jsonl")
        if _attrs(s).get("gen_ai.tool.name") == "video_generation"
    )
    [request] = received
    recorded = json.loads(_attrs(span)["gen_ai.tool.call.arguments"])
    assert recorded["prompt"] == request.prompt == "exact video prompt"
    assert recorded["reference_images"] == list(request.reference_images) == ["character.png", "scene.png"]
    assert recorded["duration"] == request.duration == 7
    assert recorded["audio"] is request.audio is True
    assert recorded["force_reference_mode"] is request.reference_mode is True
    assert recorded["size"] == request.size
    assert recorded["resolution"] == request.resolution


def test_feedback_store_has_no_latest_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feedback, "get_agent_root_dir", lambda: tmp_path)
    directory = tmp_path / "designer" / "feedback" / "graph-1"
    directory.mkdir(parents=True)
    feedback.save_feedback("graph-1", "run-1", {"final": {"improvement_plan": "x"}})
    # A pointer left by older versions must never be read as feedback.
    (directory / "latest.json").write_text('{"path": "stale", "run_id": "run-0"}', encoding="utf-8")

    assert sorted(p.name for p in directory.iterdir()) == ["latest.json", "run-1.json"]
    latest = feedback.load_latest_feedback("graph-1")
    assert latest is not None
    assert latest["run_id"] == "run-1"
    assert latest["final"]["improvement_plan"] == "x"
