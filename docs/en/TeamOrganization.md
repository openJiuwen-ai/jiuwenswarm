# Team Organization

Team Organization coordinates multiple independent Agent Teams. Each Team retains its own members, while Team Leaders share an organization task pool, reliable inbox, and workspace.

> There is no separate `/organization` command. Enter Team mode and explicitly ask the Leader to create the Organization, expert Teams, and task tree with the injected `org_*` tools.

## Configuration

Team Organization reuses `modes.team.jiuwen_team`:

```yaml
modes:
  team:
    jiuwen_team:
      team_name: jiuwen_team
      lifecycle: persistent
      teammate_mode: build_mode
      spawn_mode: inprocess
      leader:
        member_name: team-leader
        display_name: Team Leader
        persona: "Coordinates cross-team planning, review, and risk"
      agents:
        leader: $agent_leader
      workspace:
        enabled: true
      transport:
        type: inprocess
      storage:
        type: sqlite
```

The default Team model must resolve because the Summary Team uses it. The current Organization runtime is in-process: participating Team leaders share one AgentServer runtime pool, the same `TeamDatabase` instance, and a local workspace. SQLite and in-process transport are suitable for local testing; PostgreSQL alone does not enable distributed Organization execution.

The Web organization UI is experimental and disabled by default. Enable it under **More → Configuration → Experimental**, or set the following in `~/.jiuwenswarm/config/config.yaml`, preserving existing experimental settings:

```yaml
experimental:
  team_organization_ui_enabled: true
```

This UI-only switch controls the Team selector, organization panel, and separate Team conversations. Backend Organization capabilities and TUI execution do not depend on it.

## End-to-end test

For paired source-branch testing before the agent-core dependency is updated, first run `uv sync` in JiuwenSwarm, then install the local agent-core integration checkout and its declared dependencies with `uv pip install -e "<agent-core-checkout>"`. This changes only the local virtual environment, not the dependency files. Use `uv run --no-sync` so the lockfile does not restore the old pinned SDK. Skip initialization if your workspace is already initialized. Do not copy local-only `pyproject.toml` or `uv.lock` edits into the PR.

Install valid investment and finance, legal and compliance, technical due-diligence and coding, and market and commercial AgentGroups. Use the investment and finance group for the Team talking to the user, then start JiuwenSwarm:

```bash
uv sync
uv pip install -e "<agent-core-source-directory>"
uv run --no-sync jiuwenswarm-init
uv run --no-sync jiuwenswarm-start dev
```

Create a session, enter Team mode, and send the test prompt:

```text
/mode team
```

```text
Organize the existing investment and finance, legal and compliance, technical due-diligence and coding, and market and commercial expert groups to perform a quick public-information investment screen of Zed Industries. Deliver the recommendation itself to me.

This is a collaboration test, not full due diligence. Answer only whether Zed is worth advancing to formal diligence. Use only these sources; do not expand the search, research competitor financing or valuation, clone or build the repository, or run tests:

- Company and product: https://zed.dev/about
- Open-source and license overview: https://zed.dev/software-overview
- Repository: https://github.com/zed-industries/zed

Create a collaboration organization and bring in the legal, technical, and market expert groups to work with the current investment and finance group. Please create and claim the overall task before splitting it into the four domain work items below. The person responsible for the overall matter should choose an aggregation approach appropriate to the work, preferably using an independent summary team for accepted results. The current Team's investment view must also be a formal, reviewable deliverable.

Put four work items in the shared task pool and let capable expert groups claim them. Do not assign them to internal individual members. Each item must be at most 120 Chinese characters or an equivalently brief English paragraph, contain one confirmed finding and one risk or item to verify, and cite at most one of the sources above:

1. Investment and finance: one investment premise and one financial document required in the next round.
2. Legal and compliance: one license fact and one risk requiring verification, using only the official license overview.
3. Technical and coding: inspect the repository README, license file, and Cargo.toml or one core source/configuration file; report one file-backed engineering fact and one risk requiring verification.
4. Market and commercial: one product opportunity and one commercial uncertainty, using only the product introduction.

The owner of the overall matter must review every result. Aggregate only after all four pass. Do not repeatedly poll or urge other groups while they work, and do not end the overall matter early.

Return a concise investment recommendation, not merely statuses, paths, or a waiting message. State whether to proceed, proceed with conditions, or stop; include the strongest point from each field and the items required before formal diligence. Never invent revenue, valuation, customer counts, financing terms, or security conclusions. Mark anything unsupported by the sources as "to be verified."
```

A successful run shows the owner and three participating expert Teams, four accepted source tasks, one lazily created Summary Team, a completed Summary Task and root task, and the final recommendation. With the experimental UI enabled, refresh the organization panel to inspect task and review status.

Also verify the UI switch and conversation isolation:

1. With the switch absent or disabled, ordinary Agent and Team chat, history loading, and stop controls retain their existing behavior; no organization UI appears.
2. Enable the switch, run the prompt in Team mode, and check the team selector and organization panel.
3. Select an expert and ask, "Explain your most important finding without creating any new research or tasks." Its messages and reasoning must remain separate from the owner's conversation.
4. Stop an expert request and confirm that the owner's running task is not cancelled. Targeted expert requests do not support pause/resume; unsupported intentions must not fall back to controlling the owner.
5. Disable the switch and confirm that organization UI and event subscriptions are removed. Reload to verify persistence, then enable it again to check restored conversation history.

Expert message history is isolated; restoring expert tool execution history is not covered by this UI iteration. Automated regression tests do not replace this real-model acceptance run.

See [Expert and Summary Team Configuration](TeamOrganizationExpertAndSummary.md) for package validation and lifecycle details.
