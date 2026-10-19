---
name: symphony-assistant
description: Must use first when skill capabilities may help complete a task.
allowed_tools:
  - symphony_compose_score
  - symphony_read_score
  - symphony_refresh_score
  - skill_branch_peek
  - skill_branch_explore
  - skill_index_build
---

# Symphony Assistant

Use this skill when a task may benefit from selecting, combining, ordering, or discovering skill capabilities.

## Workflow

1. If the user says to use skill(s) or 技能, or if skill capabilities, skill chaining, skill ordering, or a specialized toolchain could help, always call `symphony_compose_score` with the original user task as `query`.
2. When you identify, inspect, or recommend installed Skills that are relevant to the task, pass their exact identifiers or names as `symphony_compose_score.candidate_skill_ids`. Do not omit this field after selecting candidate Skills.
3. Select the planning mode from task complexity: use `fast` for simple tasks, short execution chains, or clear candidate skills; use `beam` for complex multi-step tasks, multiple possible skill paths, or tasks that require discovering prerequisite skills through bidirectional search. Omit `mode` to use the configured default.
4. Do not manually inspect skill folders or choose the execution chain yourself; Symphony owns ordering and graph composition.
5. Treat `symphony_compose_score` as the planning entrypoint: it reads the Symphony score, refreshes missing or stale scores, and returns the user-facing plan Markdown and Mermaid execution graph.
6. The `symphony_compose_score` result may already be displayed directly to the user; otherwise, present its returned `content` directly.
7. Treat the returned plan, Mermaid graph, structured missing inputs, and caveats as the source of truth.
8. Do not call individual skill tools just to manually recreate or verify the Symphony plan.
9. If Symphony reports missing inputs, ask the user for those inputs instead of inventing them.
10. If Symphony reports no suitable candidates, a missing capability, or caveats that point to a skill gap, use `search_skill` to discover external skills. When installing a discovered skill is appropriate, call `install_skill`; after a successful install, call `symphony_refresh_score` and then call `symphony_compose_score` again with the original user task.
11. For clearly ordinary tasks that do not benefit from skill capabilities, do not use Symphony.

## Notes

- `symphony_compose_score` reads the current Symphony score, refreshes it when missing or stale, and then plans the skill execution graph from provided `candidate_skill_ids` or the default score subgraph.
- External skills must first be discovered and installed through the skill tools, then added to the score with `symphony_refresh_score`, before they can participate in Symphony planning.
- Use `symphony_read_score` and `symphony_refresh_score` directly only when the user explicitly asks to read or refresh the Symphony score.
