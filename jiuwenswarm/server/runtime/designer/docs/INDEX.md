# Designer pipeline docs

One **director** plans the film and gates prompts. Leaf nodes generate the brief, storyboard, character sheets, scene specs, shots, and the composed video.

Continuity mode: **`scene_card_plus_clip_shots`**.  
Images use image generation. Shots use reference-to-video.  
The storyboard row is the authority for each shot: `start_state`, action, camera, speech, `end_state`.

| Doc | Covers |
|-----|--------|
| [PIPELINE.md](./PIPELINE.md) | Whole path from the user prompt to the composed film |
| [DIRECTOR.md](./DIRECTOR.md) | The single overseeing agent |
| [BRIEF.md](./BRIEF.md) | `n_brief` |
| [STORYBOARD.md](./STORYBOARD.md) | `n_storyboard` |
| [CHARACTER.md](./CHARACTER.md) | `n_character_*` |
| [SCENE.md](./SCENE.md) | `n_scene_*` |
| [CLIP.md](./CLIP.md) | `n_clip_*` |
| [COMPOSE.md](./COMPOSE.md) | `n_compose` |
| [REFERENCE_LED.md](./REFERENCE_LED.md) | Reference-image roles, graph branch, and test report |

```
n_brief
   └── n_storyboard
          ├── n_character_*
          ├── n_scene_*
          └── n_clip_*
                 └── n_compose
```

The director is not a canvas node. It runs around this graph: plan, leaf prompt gate, then ratings after compose.
