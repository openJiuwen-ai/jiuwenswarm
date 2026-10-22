---
name: workswarm-video-generation
description: Generate optional videos with WorkSwarm native asynchronous video tools after confirmation and return the downloaded local artifact.
---

# WorkSwarm Video Generation

Generate video only after the existing user-confirmation gate has been satisfied.

1. Submit exactly one `generate_video(...)` call with the reviewed prompt, aspect
   ratio, resolution, duration, audio setting, workspace `save_dir`, and any supported
   first-frame or reference-image parameters.
2. If the result contains a running `job_id`, retain it and call
   `check_video_status(job_id, save_dir)` until the job reaches a terminal state.
3. Never resubmit the generation request merely because the job is still running.
4. Return the downloaded local file to the leader as an internal artifact. A
   non-leader member must not send it directly to the user.

Reference modes, duration limits, and audio support depend on the configured backend.
Report an unsupported mode or failed job truthfully instead of silently changing the
request.
