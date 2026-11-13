# Clip Agent Skill

Generate **clip** video via **R2V** (on-screen **character_specs** + **scene_specs**). Keep duration short. Honor silent policy (no implied dialogue) or leave room for later speech mix.

## Consistency
- **character_consistency**: bind `character1`… in attach order; clothing/face from specs only.
- **scene_consistency**: Scene N **scene_specs** last; empty of cast.
<<<<<<< HEAD
- **shot_consistency**: when continuing a setting, respect prior-clip last-frame handoff and storyboard end states.
- **content_safety**: the video model's moderation rejects graphic violence and gore; show such beats indirectly (aftermath, reactions, silhouettes, symbolic props), never explicitly.
||||||| e487e9a30
- **shot_consistency**: when continuing a setting, respect prior-clip last-frame handoff and storyboard end states.
=======
- **shot_consistency**: when continuing a setting, respect prior-clip speech anti-repeat and storyboard end states.
- **content_safety**: the video model's moderation rejects graphic violence and gore; show such events indirectly (aftermath, reactions, silhouettes, symbolic props), never explicitly.
>>>>>>> origin/develop

When `metadata.video_style=final_frame_reverse`: this **clip** sits on an arc that ENDS on the user reference / classic image — decisive motion early, settle late, motif-motivated continuity; never turntable a finished pose.
