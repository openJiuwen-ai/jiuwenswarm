---
name: workswarm-image-generation
description: Generate concept visuals with WorkSwarm native image generation from a reviewed visual brief and return a local internal artifact.
---

# WorkSwarm Image Generation

First compile a concrete visual brief covering subject, composition, scene, style,
lighting, palette, aspect ratio, required details, and exclusions. Then call
`generate_image(prompt, size, quality, save_dir)` with a writable `save_dir` inside
the task workspace.

- Treat the returned local path as an internal team artifact. The leader decides
  whether to embed it in the final HTML or send it as a final deliverable.
- The native tool is text-to-image only. If the user supplied a reference photo,
  inspect it separately, derive a text brief, preserve only observable constraints,
  and label the output as a concept image rather than a faithful photo edit.
- Never claim exact preservation of a face, room geometry, garment fit, product
  detail, or construction dimension that the generator cannot guarantee.
- Do not copy platform-specific model routing, point accounting, credentials, or
  provider invocation instructions into the result.
