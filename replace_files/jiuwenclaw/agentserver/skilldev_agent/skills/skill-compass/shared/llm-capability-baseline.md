# LLM Capability Baseline

> Single source of truth for what a capable LLM can do WITHOUT any skill installed.
> Referenced by: D5 (comparative baseline).
> Calibrated against: Claude Sonnet 4 / Opus 4 (2026-03 snapshot).

## Value Categories and Baseline Quality

When evaluating a skill, first classify it into one of these categories.
The baseline quality is the expected output quality (0-10) when a competent user
writes a 2-3 minute prompt WITHOUT the skill installed.

| Category | Baseline Quality | Description |
|----------|-----------------|-------------|
| tool_access | 3 | Skill provides access to CLI tools, APIs, or external services the LLM cannot invoke alone |
| domain_expertise | 5 | Skill encodes narrow expert knowledge in a specific field (not general CS/dev knowledge) |
| workflow_automation | 5 | Skill orchestrates multi-step processes that a user would otherwise do manually |
| quality_enforcement | 6 | Skill enforces standards/checks the user might forget to specify |
| general_knowledge | 7 | Skill teaches information already well-covered in LLM training data |

## LLM Built-in Capabilities (General Knowledge Areas)

The following capabilities are well-handled by a capable LLM with a thoughtful prompt.
Skills whose PRIMARY value falls in these areas should be scrutinized for genuine
added value beyond what direct prompting achieves.

| Area | LLM Proficiency | Notes |
|------|-----------------|-------|
| Code generation (most languages) | High | Functions, classes, modules, boilerplate |
| Code review & refactoring | High | Bug detection, naming, SRP, DRY |
| Explaining code | High | Walk-through, summarization, documentation |
| Writing tests (unit, integration) | High | pytest, Jest, JUnit, Go testing |
| Git operations & commit messages | High | Branch strategy, conflict concepts |
| Documentation (README, API docs) | High | JSDoc, docstrings, changelogs |
| Debugging (stack trace analysis) | High | Hypothesis generation, root cause |
| SQL & database design | High | Queries, normalization, indexing basics |
| REST API design | High | Endpoints, status codes, pagination |
| Security basics (OWASP top 10) | High | Input validation, auth patterns, XSS/SQLi |
| CSS / HTML / Tailwind | High | Responsive layout, component styling |
| DevOps concepts (Docker, CI/CD) | High | Dockerfile, pipeline YAML, deploy patterns |
| Clean code principles | High | SOLID, naming, anti-patterns |
| Design patterns | High | Factory, Observer, Strategy, etc. |
| Regex & text processing | Moderate-High | Complex patterns, multi-step transforms |
| Performance optimization | Moderate | Profiling advice, algorithmic complexity |
| Cloud platform specifics | Moderate | AWS/GCP/Azure services, not CLI flags |
| Niche framework internals | Low | Specific CLI tools, undocumented APIs |
| Real-time external data | None | Current prices, live status, API calls |
| File system / tool execution | None | Cannot run commands without tool access |

## Usage in D5 (Comparative)

When scoring D5, use the **Baseline Quality** from the value category table as
the default WITHOUT-SKILL score for each scenario. Adjust ±2 based on scenario
specifics, but document why you deviated from the default.
