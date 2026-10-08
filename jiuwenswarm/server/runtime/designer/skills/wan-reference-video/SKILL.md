---
name: wan-reference-video
description: >-
  Best practices for Alibaba Wan reference-to-video (R2V) prompts and
  reference_images attach order. Use when authoring Design clip prompts,
  binding character1… / Scene N refs, or debugging identity/placement drift.
---

# Wan reference-to-video (R2V) skill

Domain-agnostic. Applies to **every** scene and posture — not dinner/church/etc. hardcodes.

## Official prompt formula (Alibaba Model Studio)

```
Character (character1 / character2 …)
+ Action
+ Lines (optional, exact speech_line only)
+ Scene (environment / Scene N)
```

- Label subjects **`character1`…** in the **same order** as attached solo reference images.
- Environment = last set reference (**Scene N** plate) — architecture/light/props only, **no cast**.
- Prefer English `Image 1` / `character1` style identifiers matching attach order.
- Each **character** reference should contain **one** subject (solo sheet).
- Total refs are limited (typically ≤5) — prefer: optional continuity endings + on-screen solos + one scene card.
- Keep the full motion prompt **≤4000 characters** (self-limit; pipeline sends full text).

## Attach order (Design clip-as-shot)

1. Optional same-scene **ending stills** (newest first) — blocking / localization only.
2. On-screen **solo identity sheets** → `character1`, `character2`, …
3. **Scene N** environment card **LAST** — empty of people.

Authority:
- **Identity / wardrobe** → solos
- **Geography / light** → scene card
- **Start blocking** → newest prior ending (if any); else storyboard contact pose

## Hard placement rules (all scenes)

When placing people from studio solos into an empty environment:

- **CONTACT / ANTI-PENETRATION:** bodies never intersect solid furniture, walls, props, or each other.
- Seated / kneeling / reclining / leaning → visible weight on the support surface — **on**, not **through**, not floating.
- Solo sheets are often standing portraits — **repose** for the shot; do not paste standing legs into chairs/desks/floors as if fused.
- Hands grasp prop exteriors; limbs are not buried inside objects.
- Depth order matches storyboard (in front of / behind props) — never merged into prop volume.

Infer posture from action language (sit, lean, kneel, walk, hold, …) — generic verbs, any landmark.

## Style

- Copy the visual medium from the brief/storyboard exactly.
- If the brief cannot infer a medium from the user, it uses the cartoonish default
  (flat shapes, soft rendering, rounded forms) for the whole film.

## Authoring checklist for leaf clip agents

1. STYLE LOCK copied from the brief/storyboard
2. Wan REFERENCE MODE binding (labels match attach order)
3. CONTACT / ANTI-PENETRATION lock
4. THIS shot only: action, camera, speech (no prior-line restart)
5. One primary motion; explicit camera or `static`
6. No unlabeled extras; no clones; no landmark teleport

## Sources

- [Wan text/image-to-video prompt guide](https://www.alibabacloud.com/help/en/model-studio/text-to-video-prompt)
- [Wan reference-to-video API](https://www.alibabacloud.com/help/en/model-studio/legacy-wan-reference-to-video-api-reference)
- [Using Wan reference-to-video](https://help.aliyun.com/en/model-studio/video-to-video-guide)
