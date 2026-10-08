#!/usr/bin/env python3
"""Run the real Designer Play pipeline (not competition) on the church Bible prompt.

Uses the Director plus leaf agents when a chat model is configured. Saves the final
compose mp4 (if any) under pipeline_test_out/ and a JSON report beside it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROMPT = (
    "I want the video of a man standing in a church explaining something from the Bible "
    "and the crowd listened while another man gets up and leaves the church while the man "
    "at the pulpit is still speaking, this man is sitting in the front, and then the camera "
    "pans to a woman whose tears begin to flow as she nods gently agreeing with what the "
    "man is saying and a child next to her listening profusely"
)

OUT_DIR = ROOT / "pipeline_test_out"
DEFAULT_TIMEOUT_SEC = 45 * 60


def _uri_to_path(uri: str) -> Path | None:
    raw = str(uri or "").strip()
    if not raw:
        return None
    if raw.startswith("file:"):
        parsed = urlparse(raw)
        path = unquote(parsed.path or "")
        if path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return Path(path)
    p = Path(raw)
    return p if p.exists() else None


def _collect_compose_mp4(run: dict[str, Any]) -> list[Path]:
    found: list[Path] = []
    states = run.get("node_states") or {}
    for nid, st in states.items():
        if "compose" not in str(nid).lower():
            continue
        if not isinstance(st, dict):
            continue
        refs: list[dict[str, Any]] = []
        if isinstance(st.get("output_ref"), dict):
            refs.append(st["output_ref"])
        for r in st.get("output_refs") or []:
            if isinstance(r, dict):
                refs.append(r)
        for ref in refs:
            uri = str(ref.get("uri") or "")
            if not uri.lower().endswith(".mp4"):
                continue
            if "still" in uri.lower():
                continue
            path = _uri_to_path(uri)
            if path and path.is_file() and path.stat().st_size > 512:
                found.append(path)
    return found


async def _wait_run(
    store: Any,
    executor: Any,
    run_id: str,
    *,
    timeout_sec: float,
    poll_sec: float = 8.0,
) -> dict[str, Any]:
    terminal = {"completed", "failed", "cancelled"}
    deadline = time.monotonic() + timeout_sec
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        task = getattr(executor, "_tasks", {}).get(run_id)
        if task is not None and task.done():
            exc = task.exception()
            if exc is not None:
                raise RuntimeError(f"executor task failed: {exc}") from exc
        last = store.get_run(run_id)
        if last and str(last.get("status") or "") in terminal:
            return last
        if last:
            states = last.get("node_states") or {}
            done = sum(1 for s in states.values() if (s or {}).get("status") == "completed")
            running = [
                nid for nid, s in states.items() if (s or {}).get("status") == "running"
            ]
            print(
                f"[church-play] status={last.get('status')} "
                f"completed={done}/{len(states)} running={running}",
                flush=True,
            )
        await asyncio.sleep(poll_sec)
    raise TimeoutError(f"run {run_id} did not finish within {timeout_sec:.0f}s")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-sec", type=int, default=DEFAULT_TIMEOUT_SEC)
    args = parser.parse_args()

    try:
        from jiuwenswarm.common.utils import get_env_file
        from jiuwenswarm.dotenv_early import load_dotenv_runtime

        load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
    except Exception:
        traceback.print_exc()

    from jiuwenswarm.server.runtime.designer.executor import GraphExecutor
    from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
    from jiuwenswarm.server.runtime.designer.model_tools import llm_available
    from jiuwenswarm.server.runtime.designer.script_analysis import analyze_creative_brief
    from jiuwenswarm.server.runtime.designer.skills_loader import attach_skills_metadata
    from jiuwenswarm.server.runtime.designer.smart_graph import (
        apply_runtime_delegate,
        build_smart_video_graph,
        prune_non_contributing_nodes,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    report: dict[str, Any] = {
        "prompt": PROMPT,
        "llm_available": bool(llm_available()),
        "started_at": t0,
        "notes": [],
    }
    print(f"[church-play] llm_available={report['llm_available']}", flush=True)
    if not llm_available():
        report["error"] = "llm_available() is False — configure Settings chat model"
        (OUT_DIR / "church_play_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(report["error"], file=sys.stderr)
        return 2

    print("[church-play] analyze_creative_brief()...", flush=True)
    analysis = await analyze_creative_brief(PROMPT, timeout_sec=60.0)
    # Keep load within image/video quotas: 3 cinematic beats cover the prompt.
    shots = list(analysis.get("shots") or [])
    if len(shots) > 3:
        analysis["shots"] = shots[:3]
        for i, shot in enumerate(analysis["shots"], start=1):
            if isinstance(shot, dict):
                shot["shot_index"] = i
    analysis["target_shot_count"] = len(analysis.get("shots") or [])
    # Always audible soundtrack for this narrative (preaching + crowd).
    audio = dict(analysis.get("audio") or {}) if isinstance(analysis.get("audio"), dict) else {}
    audio["include_speech"] = True
    audio["include_music"] = True
    audio["policy"] = "speech_and_music"
    analysis["audio"] = audio
    # Force shot casts so the preacher stays at the pulpit while another man leaves
    # (prevents cloning one face onto both bodies).
    chars = [c for c in (analysis.get("characters") or []) if isinstance(c, dict)]
    by_role: dict[str, str] = {}
    for c in chars:
        cid = str(c.get("id") or "")
        name = str(c.get("name") or "").lower()
        if not cid:
            continue
        if any(k in name for k in ("preach", "pastor", "pulpit", "minister")):
            by_role["preacher"] = cid
        elif any(k in name for k in ("leav", "depart", "walk", "front")):
            by_role["leaver"] = cid
        elif any(k in name for k in ("woman", "weep", "tear", "mother")):
            by_role["woman"] = cid
        elif any(k in name for k in ("child", "kid", "boy", "girl")):
            by_role["child"] = cid
    if not by_role.get("preacher") and chars:
        by_role["preacher"] = str(chars[0].get("id"))
    if len(chars) > 1 and not by_role.get("leaver"):
        by_role["leaver"] = str(chars[1].get("id"))
    for shot in analysis.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        idx = int(shot.get("shot_index") or 0)
        if idx == 1 and by_role.get("preacher"):
            shot["character_ids"] = [by_role["preacher"]]
            shot["action"] = (
                str(shot.get("action") or "")
                + " Preacher alone at pulpit; congregation listening in pews; "
                "ONE preacher only — never duplicate him."
            )[:500]
        elif idx == 2 and by_role.get("preacher") and by_role.get("leaver"):
            shot["character_ids"] = [by_role["preacher"], by_role["leaver"]]
            shot["action"] = (
                "TWO DIFFERENT men: Preacher remains speaking at the pulpit; "
                "Man Leaving (different face/clothes, from front pew) stands and walks out the aisle. "
                "Never clone the preacher as the walker. Same congregation as shot 1."
            )[:500]
        elif idx == 3:
            ids = [by_role[k] for k in ("woman", "child") if by_role.get(k)]
            if ids:
                shot["character_ids"] = ids
            shot["action"] = (
                str(shot.get("action") or "")
                + " Woman weeping and nodding with child beside her; same church crowd behind. "
                "Do not show a duplicate preacher walking."
            )[:500]
    report["script_analysis_source"] = analysis.get("source")
    report["shots"] = len(analysis.get("shots") or [])
    report["characters"] = [
        str(c.get("name") or c.get("id")) for c in (analysis.get("characters") or [])
    ]

    graph = build_smart_video_graph(
        project_id="pipeline_test_church",
        prompt=PROMPT,
        analysis=analysis,
        title="Church Bible scene (pipeline test)",
    )
    graph = apply_runtime_delegate(graph)
    graph = attach_skills_metadata(graph, PROMPT)
    # AI leaf agents (DeepSeek / Settings chat) with tools; handlers only materialize media.
    # Keep force_handler on speech/music beds + compose ffmpeg path when present.
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        role = str((node.get("config") or {}).get("role") or "")
        cfg = node.setdefault("config", {})
        cfg["max_image_calls"] = max(4, int(cfg.get("max_image_calls") or 1))
        if role in {"speech", "music", "compose", "scene"}:
            cfg["force_handler"] = True
            cfg["delegate"] = "handler"
        else:
            cfg.pop("force_handler", None)
            cfg["delegate"] = "agent"
            cfg["kind"] = "agent"
    meta = dict(graph.get("metadata") or {})
    meta["script_analysis"] = analysis
    meta["pipeline_test"] = "church_bible_play_spatial_v3_audio"
    meta["audio_intent"] = {
        "include_speech": True,
        "include_music": True,
        "policy": "speech_and_music",
    }
    graph["metadata"] = meta

    from jiuwenswarm.server.runtime.designer.orchestration import (
        _ensure_audio_nodes_for_intent,
        _director_prune_and_cohere,
    )

    _ensure_audio_nodes_for_intent(graph)
    _director_prune_and_cohere(graph)

    pruned = prune_non_contributing_nodes(graph)
    report["pruned_before_run"] = pruned
    report["bootstrap"] = meta.get("bootstrap")
    report["node_ids"] = [str(n.get("id")) for n in (graph.get("nodes") or [])]
    report["edge_count"] = len(graph.get("edges") or [])

    store = DesignerGraphStore()
    graph = store.save_graph(graph)
    report["graph_id"] = graph.get("graph_id")

    executor = GraphExecutor(store)
    run = executor.create_run(graph)
    run_id = str(run["run_id"])
    report["run_id"] = run_id
    (OUT_DIR / "church_play_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[church-play] start_run graph={graph.get('graph_id')} run={run_id}", flush=True)

    await executor.start_run(run_id)
    timed_out = False
    try:
        finished = await _wait_run(
            store, executor, run_id, timeout_sec=float(args.timeout_sec)
        )
    except TimeoutError as exc:
        timed_out = True
        finished = store.get_run(run_id) or run
        report["error"] = str(exc)
        report["notes"].append(str(exc))

    report["run_status"] = finished.get("status")
    report["elapsed_sec"] = round(time.time() - t0, 1)
    report["timed_out"] = timed_out

    g2 = store.get_graph(str(graph["graph_id"])) or graph
    meta2 = dict(g2.get("metadata") or {})
    report["storyboard_reviewed"] = bool(meta2.get("storyboard_reviewed"))
    report["dual_rater_overall"] = (meta2.get("dual_rater_aggregate") or {}).get(
        "aggregated_overall"
    )
    report["last_feedback_path"] = meta2.get("last_feedback_path")

    saved_videos: list[str] = []
    for path in _collect_compose_mp4(finished):
        dest = OUT_DIR / path.name
        shutil.copy2(path, dest)
        saved_videos.append(str(dest))
    report["compose_videos"] = saved_videos

    clip_dir = OUT_DIR / "clips"
    clip_dir.mkdir(exist_ok=True)
    clip_copies: list[str] = []
    for nid, st in (finished.get("node_states") or {}).items():
        if not str(nid).startswith("n_clip"):
            continue
        if not isinstance(st, dict):
            continue
        ref = st.get("output_ref") if isinstance(st.get("output_ref"), dict) else None
        if not ref:
            continue
        uri = str(ref.get("uri") or "")
        if not uri.lower().endswith(".mp4") or "still" in uri.lower():
            continue
        p = _uri_to_path(uri)
        if p and p.is_file():
            dest = clip_dir / f"{nid}_{p.name}"
            shutil.copy2(p, dest)
            clip_copies.append(str(dest))
    report["clip_videos"] = clip_copies

    node_statuses = {
        nid: {
            "status": (st or {}).get("status"),
            "message": str((st or {}).get("message") or "")[:200],
            "uri": str(((st or {}).get("output_ref") or {}).get("uri") or "")[:200],
        }
        for nid, st in (finished.get("node_states") or {}).items()
    }
    report["node_statuses"] = node_statuses

    report_path = OUT_DIR / "church_play_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    ok = bool(saved_videos) and str(finished.get("status") or "") == "completed"
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
