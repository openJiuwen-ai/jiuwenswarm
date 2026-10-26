# Team Organization Expert and Summary Team Configuration

This document describes AgentGroup integration with Team Organization and final delivery in `HIERARCHICAL` and `SUMMARY_TEAM` modes. Dependency installation, the experimental switch, and runtime configuration are documented in the [User Guide](TeamOrganization.md#2-setup).

For a first test, use Example 1 in the User Guide: install the Business Planning, Technology Insight, and Software Architecture groups, then enter the Owner Team from Business Planning. No custom templates are needed. Sections 1.1 and 1.2 below provide manual setup materials when suitable templates are unavailable; section 1.3 explains Owner entry paths and capability matching. Startup commands, prompts, and acceptance checks remain in the User Guide.

Direct messages to a running expert Team use the same `interact` channel as ordinary Teams. Acceptance means the input was delivered to the Leader runtime, not that its task has completed. Replies continue through the existing Team stream and are saved in that Team's conversation history. A paused expert still resumes through the existing execution path. Organization-internal task and Leader-message scheduling is unchanged.

## Summary Team

The fixed Summary Team contains:

| Member | Responsibility |
|--------|----------------|
| `summary-leader` | Validates sources. Integrates and submits short text directly; coordinates an outline and draft only for a complex deliverable |
| `source-integrator` | For a complex deliverable, turns the bound sources into the outline the draft needs |
| `delivery-drafter` | For a complex deliverable, produces the user-facing draft from the sources and outline |

It uses the configured default Team models but does not copy user Team members, tools, or template prompts. Transport and spawning use `inprocess`; members use `build_mode`. Intermediate work stays in its Team workspace; final artifacts belong under the Organization `summary/` directory. One persistent Summary Team is lazily created per Organization and reused across root tasks; finishing one summary does not stop the shared Team.

Summary inputs include `root_task` (ID, title, description and output specification) alongside the existing bound sources. Root and Summary Task requirements determine format and acceptance; factual conclusions still come only from the bound sources of this execution. When a source body already covers the request, use that text. Read a cited file only when the summary needs its content, and do not scan files from earlier runs. An outline keeps only the structure the draft needs, source attribution, and disagreements that would change the conclusion. It does not add research, risk lists, or verification items the user did not request. A final file is written only in the absolute Organization `summary/` directory supplied for this turn, and `result_uri` uses that same path. With no final file, the full text goes in `output_context.description`. To publish an existing artifact, copy it unchanged into that directory before setting `result_uri` and the actual `result_type`.

On binding failure, the launcher rolls an expert Team back. Summary Team lifecycle and Summary Execution state are persisted separately so restart recovery can restore the shared Team and re-evaluate source readiness.

Task completion and Web execution status have separate lifecycles. When a background Organization turn ends, the Web client closes that Team's current reasoning and streaming output without ending other Teams' work. A passive `attach_only` subscription does not keep the session busy; real execution and requests waiting for user input still do. Check Summary Execution and task states to confirm completion, rather than treating an intermediate `chat.final` segment as the end of the turn.

## Importing packages

Imports validate skills before saving or installing the package. String entries in `skills` must be skill names, such as `"docx-pro"`; mappings use package-relative directories, such as `{"dir": "skills/docx-pro", "mode": "all"}`. Absolute POSIX and Windows paths are rejected, including Windows paths uploaded to a non-Windows server. Referenced skill directories must remain inside the package and contain `SKILL.md`.
