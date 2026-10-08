#!/usr/bin/env python3
"""Generate designer_node_catalog.json and .txt under designer_catalog_skills_reports_trajectory/."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "designer_catalog_skills_reports_trajectory"
OUT_JSON = OUT_DIR / "designer_node_catalog.json"
OUT_TXT = OUT_DIR / "designer_node_catalog.txt"


def node(
    node_id: str,
    *,
    scenario: str,
    label: str,
    modality: str,
    role: str,
    description: str,
    inputs: list[str],
    outputs: list[str],
    params: list[dict],
    models_cost: list[str],
    models_quality: list[str],
    tags: list[str] | None = None,
) -> dict:
    return {
        "id": node_id,
        "scenario": scenario,
        "label": label,
        "modality": modality,  # text|table|image|video|audio|mesh
        "role": role,  # intent|asset|control|transform|assemble|review
        "description": description,
        "typical_inputs": inputs,
        "typical_outputs": outputs,
        "parameters": params,
        "model_hints": {"cost": models_cost, "quality": models_quality},
        "tags": tags or [],
        "rerunnable": True,
        "preview": {
            "kind": modality,
            "interactive_3d": modality == "mesh",
        },
    }


def p(name: str, typ: str, default, description: str, **extra) -> dict:
    d = {"name": name, "type": typ, "default": default, "description": description}
    d.update(extra)
    return d


def build_catalog() -> dict:
    nodes: list[dict] = []

    # ── Shared / intent ─────────────────────────────────────────────────────
    shared = [
        ("intent.brief", "Project Brief", "text", "User intent, constraints, style bible"),
        ("intent.optimize_mode", "Optimize Mode", "text", "cost vs quality routing policy"),
        ("intent.style_guide", "Style Guide", "text", "Visual/audio style tokens"),
        ("intent.safety", "Safety Filter", "text", "Content policy / brand safety gates"),
        ("intent.references", "Reference Pack", "table", "URLs/files the pipeline may use"),
    ]
    for nid, label, mod, desc in shared:
        nodes.append(
            node(
                nid,
                scenario="shared",
                label=label,
                modality=mod,
                role="intent",
                description=desc,
                inputs=["user_prompt"],
                outputs=[mod],
                params=[
                    p("prompt", "string", "", "Primary instruction"),
                    p("language", "string", "en", "Output language"),
                ],
                models_cost=["deepseek-v4-flash"],
                models_quality=["deepseek-v4-pro"],
                tags=["shared", "entrypoint"],
            )
        )

    # ── Video generation ────────────────────────────────────────────────────
    video_defs = [
        ("video.script", "Script / Screenplay", "text", "Full spoken/narration script", ["intent.brief"], ["text"]),
        ("video.beat_sheet", "Beat Sheet", "table", "Narrative beats with timing", ["video.script"], ["table"]),
        ("video.storyboard", "Storyboard Table", "table", "Shot list: framing, action, dialogue", ["video.beat_sheet"], ["table"]),
        ("video.character_bible", "Character Bible", "text", "Character descriptions & continuity", ["intent.brief"], ["text"]),
        ("video.character_portrait", "Character Portrait", "image", "Hero/supporting character still", ["video.character_bible", "intent.style_guide"], ["image"]),
        ("video.character_sheet", "Character Turnaround", "image", "Multi-view character sheet", ["video.character_portrait"], ["image"]),
        ("video.character_expression", "Expression Sheet", "image", "Emotion variants", ["video.character_sheet"], ["image"]),
        ("video.scene_desc", "Scene Description", "text", "Location, time, weather, mood", ["video.storyboard"], ["text"]),
        ("video.environment_key", "Environment Key Art", "image", "Location establishing shot", ["video.scene_desc", "intent.style_guide"], ["image"]),
        ("video.prop_asset", "Prop Asset", "image", "Important props", ["video.scene_desc"], ["image"]),
        ("video.costume", "Costume Design", "image", "Wardrobe reference", ["video.character_bible"], ["image"]),
        ("video.lighting_plan", "Lighting Plan", "text", "Key/fill/rim notes", ["video.scene_desc"], ["text"]),
        ("video.camera_plan", "Camera Plan", "table", "Lens, move, FOV per shot", ["video.storyboard"], ["table"]),
        ("video.action_beats", "Action Beats", "text", "Physical actions / blocking", ["video.storyboard"], ["text"]),
        ("video.transcript", "Dialogue Transcript", "text", "Line-by-line dialogue + timing", ["video.script"], ["text"]),
        ("video.shot_frame", "Shot Keyframe", "image", "Per-shot first frame", ["video.storyboard", "video.character_portrait", "video.environment_key"], ["image"]),
        ("video.shot_end_frame", "Shot End Frame", "image", "Per-shot last frame", ["video.shot_frame"], ["image"]),
        ("video.clip", "Shot Clip", "video", "R2V shot from solos + scene", ["video.shot_frame", "video.shot_end_frame", "video.action_beats"], ["video"]),
        ("video.broll", "B-Roll Clip", "video", "Atmospheric cutaways", ["video.environment_key"], ["video"]),
        ("video.transition", "Transition FX", "video", "Wipe/dissolve/match-cut helper", ["video.clip"], ["video"]),
        ("video.voice_cast", "Voice Casting", "text", "Voice persona choice", ["video.character_bible"], ["text"]),
        ("video.tts", "Dialogue TTS", "audio", "Spoken lines", ["video.transcript", "video.voice_cast"], ["audio"]),
        ("video.sfx", "SFX Bed", "audio", "Foley / effects", ["video.action_beats"], ["audio"]),
        ("video.ambience", "Ambience", "audio", "Room tone / world bed", ["video.scene_desc"], ["audio"]),
        ("video.bgm", "Background Music", "audio", "Score / underscore", ["intent.style_guide", "video.beat_sheet"], ["audio"]),
        ("video.caption", "Captions / Subtitles", "table", "Timed captions", ["video.transcript"], ["table"]),
        ("video.color_grade", "Color Grade Ref", "image", "Look LUT / grade still", ["video.shot_frame"], ["image"]),
        ("video.assembly", "Timeline Assembly", "video", "Ordered clips + audio mix", ["video.clip", "video.tts", "video.bgm", "video.sfx"], ["video"]),
        ("video.final", "Final Master", "video", "Export deliverable", ["video.assembly", "video.caption"], ["video"]),
        ("video.thumbnail", "Thumbnail", "image", "Marketing still", ["video.final"], ["image"]),
        ("video.trailer", "Trailer Cut", "video", "Short teaser", ["video.final"], ["video"]),
    ]
    for nid, label, mod, desc, inputs, outputs in video_defs:
        nodes.append(
            node(
                nid,
                scenario="video",
                label=label,
                modality=mod,
                role="assemble" if "final" in nid or "assembly" in nid else "asset",
                description=desc,
                inputs=inputs,
                outputs=outputs,
                params=_default_params_for(mod),
                models_cost=_cost_models(mod),
                models_quality=_quality_models(mod),
                tags=["video", "generation"],
            )
        )

    # ── 3D generation ───────────────────────────────────────────────────────
    mesh3d = [
        ("mesh3d.scene_brief", "3D Scene Brief", "text", "Scene intent & scale", ["intent.brief"], ["text"]),
        ("mesh3d.asset_list", "Asset Inventory", "table", "Objects to generate", ["mesh3d.scene_brief"], ["table"]),
        ("mesh3d.concept", "Concept Sketch", "image", "2D concept for mesh", ["mesh3d.scene_brief"], ["image"]),
        ("mesh3d.blockout", "Blockout Mesh", "mesh", "Low-res massing", ["mesh3d.concept"], ["mesh"]),
        ("mesh3d.hero_mesh", "Hero Mesh", "mesh", "Main object geometry", ["mesh3d.blockout"], ["mesh"]),
        ("mesh3d.prop_mesh", "Prop Mesh", "mesh", "Secondary props", ["mesh3d.asset_list"], ["mesh"]),
        ("mesh3d.character_mesh", "Character Mesh", "mesh", "Character geometry", ["video.character_sheet"], ["mesh"]),
        ("mesh3d.retopo", "Retopology", "mesh", "Clean topology pass", ["mesh3d.hero_mesh"], ["mesh"]),
        ("mesh3d.uv", "UV Layout", "image", "UV islands preview", ["mesh3d.retopo"], ["image"]),
        ("mesh3d.albedo", "Albedo Texture", "image", "Base color map", ["mesh3d.uv", "mesh3d.concept"], ["image"]),
        ("mesh3d.normal", "Normal Map", "image", "Surface detail", ["mesh3d.albedo"], ["image"]),
        ("mesh3d.roughness", "Roughness Map", "image", "PBR roughness", ["mesh3d.albedo"], ["image"]),
        ("mesh3d.metalness", "Metalness Map", "image", "PBR metalness", ["mesh3d.albedo"], ["image"]),
        ("mesh3d.ao", "AO Map", "image", "Ambient occlusion", ["mesh3d.retopo"], ["image"]),
        ("mesh3d.emissive", "Emissive Map", "image", "Glow regions", ["mesh3d.albedo"], ["image"]),
        ("mesh3d.material", "Material Bundle", "table", "PBR material package refs", ["mesh3d.albedo", "mesh3d.normal", "mesh3d.roughness", "mesh3d.metalness"], ["table"]),
        ("mesh3d.rig", "Rig / Skeleton", "mesh", "Bones + bind", ["mesh3d.character_mesh"], ["mesh"]),
        ("mesh3d.skin", "Skin Weights", "mesh", "Deformation weights", ["mesh3d.rig"], ["mesh"]),
        ("mesh3d.anim_clip", "Animation Clip", "video", "Preview animation render", ["mesh3d.skin", "mesh3d.action"], ["video"]),
        ("mesh3d.action", "Action Graph", "text", "Idle/walk/attack clips list", ["mesh3d.scene_brief"], ["text"]),
        ("mesh3d.environment", "Environment Mesh", "mesh", "Terrain / room shell", ["mesh3d.scene_brief"], ["mesh"]),
        ("mesh3d.skybox", "Skybox / HDRI", "image", "Environment lighting", ["mesh3d.scene_brief"], ["image"]),
        ("mesh3d.lighting", "Light Rig", "text", "Light placement params", ["mesh3d.skybox"], ["text"]),
        ("mesh3d.camera", "Camera Rig", "text", "Cameras for renders", ["mesh3d.scene_brief"], ["text"]),
        ("mesh3d.collision", "Collision Mesh", "mesh", "Physics proxy", ["mesh3d.hero_mesh"], ["mesh"]),
        ("mesh3d.lod", "LOD Set", "mesh", "LOD0..N", ["mesh3d.retopo"], ["mesh"]),
        ("mesh3d.scene_compose", "Scene Compose", "mesh", "Place meshes in scene", ["mesh3d.hero_mesh", "mesh3d.prop_mesh", "mesh3d.environment"], ["mesh"]),
        ("mesh3d.render_still", "Beauty Still", "image", "Offline still render", ["mesh3d.scene_compose", "mesh3d.material", "mesh3d.lighting"], ["image"]),
        ("mesh3d.render_turntable", "Turntable", "video", "360 preview", ["mesh3d.scene_compose"], ["video"]),
        ("mesh3d.export_glb", "Export GLB", "mesh", "Final packed asset", ["mesh3d.scene_compose", "mesh3d.material"], ["mesh"]),
        ("mesh3d.final", "Final 3D Package", "table", "Deliverable manifest", ["mesh3d.export_glb", "mesh3d.render_still"], ["table"]),
    ]
    for nid, label, mod, desc, inputs, outputs in mesh3d:
        nodes.append(
            node(
                nid,
                scenario="3d",
                label=label,
                modality=mod,
                role="assemble" if nid.endswith(".final") or "compose" in nid else "asset",
                description=desc,
                inputs=inputs,
                outputs=outputs,
                params=_default_params_for(mod),
                models_cost=_cost_models(mod),
                models_quality=_quality_models(mod),
                tags=["3d", "mesh", "pbr"],
            )
        )

    # ── Music / audio ───────────────────────────────────────────────────────
    music = [
        ("music.brief", "Music Brief", "text", "Genre, mood, BPM target", ["intent.brief"], ["text"]),
        ("music.structure", "Song Structure", "table", "Intro/verse/chorus timings", ["music.brief"], ["table"]),
        ("music.melody", "Melody Line", "audio", "Lead melody sketch", ["music.structure"], ["audio"]),
        ("music.harmony", "Harmony / Chords", "audio", "Chord progression bed", ["music.structure"], ["audio"]),
        ("music.bass", "Bass Line", "audio", "Bass stem", ["music.harmony"], ["audio"]),
        ("music.drums", "Drums / Rhythm", "audio", "Percussion stem", ["music.structure"], ["audio"]),
        ("music.pads", "Pads / Atmosphere", "audio", "Texture layers", ["music.brief"], ["audio"]),
        ("music.vocals", "Vocals", "audio", "Lead vocal or choir", ["music.melody", "video.transcript"], ["audio"]),
        ("music.fx", "Production FX", "audio", "Risers, impacts", ["music.structure"], ["audio"]),
        ("music.mix", "Mix Bus", "audio", "Balanced stereo mix", ["music.melody", "music.harmony", "music.bass", "music.drums", "music.vocals"], ["audio"]),
        ("music.master", "Master", "audio", "Loudness-normalized master", ["music.mix"], ["audio"]),
        ("music.stems_pack", "Stems Pack", "table", "Exportable stems index", ["music.mix"], ["table"]),
        ("music.cover_art", "Cover Art", "image", "Album/single art", ["music.brief"], ["image"]),
        ("music.final", "Final Track", "audio", "Release audio", ["music.master", "music.cover_art"], ["audio"]),
    ]
    for nid, label, mod, desc, inputs, outputs in music:
        nodes.append(
            node(
                nid,
                scenario="music",
                label=label,
                modality=mod,
                role="assemble" if "final" in nid or nid.endswith(".master") else "asset",
                description=desc,
                inputs=inputs,
                outputs=outputs,
                params=_default_params_for(mod),
                models_cost=_cost_models(mod),
                models_quality=_quality_models(mod),
                tags=["music", "audio"],
            )
        )

    # ── Image / illustration ────────────────────────────────────────────────
    image = [
        ("image.brief", "Image Brief", "text", "Subject, composition, style", ["intent.brief"], ["text"]),
        ("image.moodboard", "Moodboard", "image", "Collage of refs", ["image.brief"], ["image"]),
        ("image.layout", "Layout Sketch", "image", "Rough composition", ["image.brief"], ["image"]),
        ("image.subject", "Subject Render", "image", "Main subject isolation", ["image.layout"], ["image"]),
        ("image.background", "Background", "image", "Scene backdrop", ["image.brief"], ["image"]),
        ("image.lighting", "Relight Pass", "image", "Lighting consistency", ["image.subject", "image.background"], ["image"]),
        ("image.composite", "Composite", "image", "Subject + BG merge", ["image.subject", "image.background", "image.lighting"], ["image"]),
        ("image.upscale", "Upscale", "image", "Resolution enhance", ["image.composite"], ["image"]),
        ("image.inpaint", "Inpaint / Edit", "image", "Local edits", ["image.composite"], ["image"]),
        ("image.outpaint", "Outpaint", "image", "Canvas expand", ["image.composite"], ["image"]),
        ("image.variant_a", "Variant A", "image", "Alt creative", ["image.brief"], ["image"]),
        ("image.variant_b", "Variant B", "image", "Alt creative", ["image.brief"], ["image"]),
        ("image.final", "Final Still", "image", "Delivery still", ["image.upscale"], ["image"]),
    ]
    for nid, label, mod, desc, inputs, outputs in image:
        nodes.append(
            node(
                nid,
                scenario="image",
                label=label,
                modality=mod,
                role="assemble" if "final" in nid else "asset",
                description=desc,
                inputs=inputs,
                outputs=outputs,
                params=_default_params_for(mod),
                models_cost=_cost_models(mod),
                models_quality=_quality_models(mod),
                tags=["image"],
            )
        )

    # ── Speech / podcast / voice ────────────────────────────────────────────
    speech = [
        ("speech.script", "Speech Script", "text", "Spoken content", ["intent.brief"], ["text"]),
        ("speech.ssml", "SSML Markup", "text", "Prosody markup", ["speech.script"], ["text"]),
        ("speech.voice_select", "Voice Select", "text", "Speaker identity", ["intent.brief"], ["text"]),
        ("speech.tts", "TTS Render", "audio", "Synthesized speech", ["speech.ssml", "speech.voice_select"], ["audio"]),
        ("speech.emotion", "Emotion Pass", "audio", "Affective re-synthesis", ["speech.tts"], ["audio"]),
        ("speech.denoise", "Denoise / Clean", "audio", "Cleanup", ["speech.tts"], ["audio"]),
        ("speech.bed", "Underscore Bed", "audio", "Soft music under speech", ["speech.script"], ["audio"]),
        ("speech.mix", "Speech Mix", "audio", "Voice + bed", ["speech.denoise", "speech.bed"], ["audio"]),
        ("speech.chapters", "Chapters", "table", "Timestamped chapters", ["speech.script"], ["table"]),
        ("speech.final", "Final Speech Audio", "audio", "Deliverable", ["speech.mix", "speech.chapters"], ["audio"]),
    ]
    for nid, label, mod, desc, inputs, outputs in speech:
        nodes.append(
            node(
                nid,
                scenario="speech",
                label=label,
                modality=mod,
                role="assemble" if "final" in nid else "asset",
                description=desc,
                inputs=inputs,
                outputs=outputs,
                params=_default_params_for(mod),
                models_cost=_cost_models(mod),
                models_quality=_quality_models(mod),
                tags=["speech", "tts"],
            )
        )

    # ── Multimodal / general agentic ────────────────────────────────────────
    multi = [
        ("multi.research", "Research Notes", "text", "Gather facts for the ask", ["intent.brief"], ["text"]),
        ("multi.plan", "Execution Plan", "table", "Ordered work packages", ["multi.research"], ["table"]),
        ("multi.branch_video", "Video Branch Gate", "text", "Whether to spawn video subgraph", ["multi.plan"], ["text"]),
        ("multi.branch_3d", "3D Branch Gate", "text", "Whether to spawn 3D subgraph", ["multi.plan"], ["text"]),
        ("multi.branch_music", "Music Branch Gate", "text", "Whether to spawn music subgraph", ["multi.plan"], ["text"]),
        ("multi.qa", "QA Checklist", "table", "Acceptance criteria checks", ["multi.plan"], ["table"]),
        ("multi.review", "Human Review Gate", "text", "Pause for user approval", ["multi.qa"], ["text"]),
        ("multi.package", "Delivery Package", "table", "All artifacts index", ["multi.review"], ["table"]),
    ]
    for nid, label, mod, desc, inputs, outputs in multi:
        nodes.append(
            node(
                nid,
                scenario="multimodal",
                label=label,
                modality=mod,
                role="control" if "branch" in nid or "review" in nid else "intent",
                description=desc,
                inputs=inputs,
                outputs=outputs,
                params=_default_params_for(mod),
                models_cost=["deepseek-v4-flash"],
                models_quality=["deepseek-v4-pro"],
                tags=["multimodal", "agentic"],
            )
        )

    scenarios = sorted({n["scenario"] for n in nodes})
    return {
        "schema_version": "designer-node-catalog.v1",
        "description": (
            "Catalog of agentic ComfyUI-style nodes for JiuwenSwarm Design. "
            "Agents select nodes by scenario and wire outputs→inputs "
            "using model_hints."
        ),
        "modalities": ["text", "table", "image", "video", "audio", "mesh"],
        "scenarios": scenarios,
        "node_count": len(nodes),
        "nodes": nodes,
        "scenario_templates": {
            # Must match original static video graph node ids / names.
            "video": [
                "n_brief",
                "n_character",
                "n_storyboard",
                "n_frame_1",
                "n_frame_2",
                "n_frame_3",
                "n_clip_1",
                "n_clip_2",
                "n_clip_3",
                "n_final",
            ],
            "3d": [
                "intent.brief",
                "mesh3d.scene_brief",
                "mesh3d.concept",
                "mesh3d.hero_mesh",
                "mesh3d.albedo",
                "mesh3d.normal",
                "mesh3d.material",
                "mesh3d.environment",
                "mesh3d.scene_compose",
                "mesh3d.render_still",
                "mesh3d.export_glb",
                "mesh3d.final",
            ],
            "music": [
                "intent.brief",
                "music.brief",
                "music.structure",
                "music.melody",
                "music.harmony",
                "music.drums",
                "music.bass",
                "music.mix",
                "music.master",
                "music.final",
            ],
            "image": [
                "intent.brief",
                "image.brief",
                "image.layout",
                "image.subject",
                "image.background",
                "image.composite",
                "image.upscale",
                "image.final",
            ],
            "speech": [
                "intent.brief",
                "speech.script",
                "speech.voice_select",
                "speech.tts",
                "speech.bed",
                "speech.mix",
                "speech.final",
            ],
            "multimodal": [
                "intent.brief",
                "multi.research",
                "multi.plan",
                "multi.branch_video",
                "multi.branch_3d",
                "multi.branch_music",
                "multi.package",
            ],
        },
    }


def _default_params_for(mod: str) -> list[dict]:
    base = [
        p("model", "string", "", "Selected model id for this node"),
        p("seed", "int", -1, "RNG seed (-1 = random)"),
    ]
    if mod == "text":
        return base + [
            p("temperature", "float", 0.7, "Sampling temperature", min=0, max=2),
            p("max_tokens", "int", 2048, "Max tokens"),
        ]
    if mod == "image":
        return base + [
            p("width", "int", 1024, "Width px"),
            p("height", "int", 1024, "Height px"),
            p("steps", "int", 28, "Diffusion steps"),
            p("cfg", "float", 5.5, "Guidance scale"),
            p("negative_prompt", "string", "", "Negative prompt"),
        ]
    if mod == "video":
        return base + [
            p("duration_s", "float", 4.0, "Clip length seconds"),
            p("fps", "int", 24, "Frames per second"),
            p("width", "int", 1280, "Width"),
            p("height", "int", 720, "Height"),
            p("motion", "float", 0.5, "Motion strength 0-1"),
        ]
    if mod == "audio":
        return base + [
            p("duration_s", "float", 30.0, "Duration seconds"),
            p("sample_rate", "int", 44100, "Sample rate"),
            p("bpm", "int", 120, "Tempo if musical"),
            p("voice_id", "string", "", "TTS voice id"),
        ]
    if mod == "mesh":
        return base + [
            p("polycount_target", "int", 20000, "Target triangles"),
            p("style", "string", "realistic", "stylized|realistic|lowpoly"),
            p("texture_res", "int", 2048, "Texture resolution"),
            p("format", "enum", "glb", "Export format", enum=["glb", "obj", "fbx"]),
        ]
    if mod == "table":
        return base + [p("rows_hint", "int", 8, "Suggested row count")]
    return base


def _cost_models(mod: str) -> list[str]:
    return {
        "text": ["deepseek-v4-flash"],
        "table": ["deepseek-v4-flash"],
        "image": ["flux-schnell", "sdxl-turbo", "user-image-api"],
        "video": ["ltx-video", "user-video-api"],
        "audio": ["user-tts-api", "user-music-api"],
        "mesh": ["user-3d-api", "tripo-fast"],
    }.get(mod, ["user-api"])


def _quality_models(mod: str) -> list[str]:
    return {
        "text": ["deepseek-v4-pro"],
        "table": ["deepseek-v4-pro"],
        "image": ["flux-pro", "sd3", "user-image-api"],
        "video": ["kling-pro", "runway-gen3", "user-video-api"],
        "audio": ["user-tts-hq", "user-music-hq"],
        "mesh": ["user-3d-hq", "meshy-pro"],
    }.get(mod, ["user-api"])


def to_txt(catalog: dict) -> str:
    lines = [
        "JiuwenSwarm Designer Node Catalog",
        f"schema: {catalog['schema_version']}",
        f"nodes: {catalog['node_count']}",
        f"scenarios: {', '.join(catalog['scenarios'])}",
        "",
        "This catalog drives agentic ComfyUI-style graph composition.",
        "Agents pick a scenario template, expand nodes from user intent,",
        "choose cost/quality models, wire outputs→inputs, and expose params + rerun.",
        "",
    ]
    by_scenario: dict[str, list[dict]] = {}
    for n in catalog["nodes"]:
        by_scenario.setdefault(n["scenario"], []).append(n)

    for scenario in catalog["scenarios"]:
        lines.append("=" * 72)
        lines.append(f"SCENARIO: {scenario}")
        tmpl = catalog["scenario_templates"].get(scenario) or []
        if tmpl:
            lines.append("Default template chain:")
            lines.append("  " + " -> ".join(tmpl))
        lines.append("")
        for n in by_scenario[scenario]:
            lines.append(f"- [{n['id']}] {n['label']}  ({n['modality']}/{n['role']})")
            lines.append(f"    {n['description']}")
            lines.append(f"    inputs: {', '.join(n['typical_inputs']) or '-'}")
            lines.append(f"    outputs: {', '.join(n['typical_outputs'])}")
            cost = ", ".join(n["model_hints"]["cost"])
            qual = ", ".join(n["model_hints"]["quality"])
            lines.append(f"    models cost=[{cost}] quality=[{qual}]")
            param_names = ", ".join(p["name"] for p in n["parameters"])
            lines.append(f"    params: {param_names}")
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    catalog = build_catalog()
    OUT_JSON.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    OUT_TXT.write_text(to_txt(catalog), encoding="utf-8")
    print(f"Wrote {OUT_JSON} ({catalog['node_count']} nodes)")
    print(f"Wrote {OUT_TXT}")


if __name__ == "__main__":
    main()
