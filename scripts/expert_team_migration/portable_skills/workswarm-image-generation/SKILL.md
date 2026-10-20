---
name: workswarm-image-generation
description: Generate real raster visuals through WorkSwarm's configured generate_visual tool, validate the returned image artifact, and hand it to the team for embedding or delivery. Use for covers, invitation cards, food images, styling concepts, room concepts, and any task that explicitly asks for an AI-generated image rather than an SVG diagram.
---

# WorkSwarm Image Generation

Compile a concrete visual brief covering subject, composition, scene, style,
lighting, palette, aspect ratio, required details, and exclusions. Then call
`generate_visual(prompt, aspect_ratio, resolution, save_dir,
reference_image_paths)` with a writable `save_dir` inside the task workspace.

- For photo-guided room/storage, renovation, styling, product, or portrait tasks,
  pass the actual uploaded local path (inside the current task workspace), local
  `file://` URI, or strict image data URI in `reference_image_paths`. Remote URLs
  are intentionally rejected; first bring an authorized user image into the task
  workspace. Do not omit the reference and silently downgrade the task to
  text-to-image.
- Use a configured backend that supports reference images: ModelArk/Seedream,
  DashScope/Qwen-Image, vLLM-Omni, or an OpenRouter-compatible multimodal image
  model. MiniMax image generation does not support reference images; treat the
  returned `[ERROR]` as blocked and report the provider requirement to the leader.

- Reject results beginning with `[ERROR]`, empty results, remote-only URLs that were
  not downloaded, missing files, zero-byte files, and extensions other than PNG,
  JPEG, WebP, or GIF. A successful call must yield a readable raster file in the
  current task workspace.
- Treat the validated local path as an internal team artifact. Embed the actual
  raster bytes in the final self-contained HTML when the delivery contract requires
  it; do not leave a machine-local path in the HTML.
- Never claim exact preservation of a face, room geometry, garment fit, product
  detail, or construction dimension that the generator cannot guarantee.
- If a requested photorealistic visual cannot be generated, return a blocked or
  partial result to the leader. Do not draw an SVG/HTML/PIL substitute, screenshot a
  vector mockup, or claim that such a fallback is the requested generated image.
- Do not read or execute an unmounted global Skill directory to bypass Skill
  visibility. Do not copy platform-specific model routing, point accounting,
  credentials, or provider invocation instructions into the result.
