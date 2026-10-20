---
name: workswarm-single-html-delivery
description: Consolidate reviewed team results into one self-contained, static-first HTML artifact and validate it before the leader sends it. Use when an AgentGroup promises a single runnable page and must exclude temporary scripts, test files, local paths, or separate intermediate attachments.
---

# WorkSwarm Single HTML Delivery

Consume only reviewed upstream results. Do not silently redo research, invent missing
prices or facts, or replace a failed domain task with model guesses.

1. Write the exact promised primary filename with `write_file` inside the task workspace.
2. Put headings, conclusions, schedules, tables, checklists, source notes, and uncertainty
   markers in the initial HTML DOM. JavaScript may enhance interaction but must not create
   the core content.
3. Use inline CSS and JavaScript. Remove remote dependencies and machine-local paths.
   Embed required validated raster assets as `data:image/png|jpeg|webp|gif;base64,...`.
4. Re-open the file once and perform one bounded static inspection: verify the expected
   sections, closing tags, static content, absence of machine-local paths, and required
   embedded assets. Do not create ad-hoc test scripts, browser harnesses, screenshots, or
   launch a browser directly. When visual QA is explicitly requested and a dedicated
   web-page test Skill is assigned, replace the static inspection with one bounded call
   to that Skill; do not repeat tests through other tools.
5. Keep source scripts, screenshots, JSON, CSV, temporary checks, and test files internal.
   The successful user-visible deliverable is the one primary HTML file.

Non-leader members return the validated absolute path to the leader as an internal
artifact and must not call `send_file_to_user`. The leader alone performs the final send.
