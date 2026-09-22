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

The default Team model must resolve because the Summary Team uses it. SQLite and in-process transport are suitable for local testing. Distributed deployments require shared organization data and artifact storage.

## End-to-end test

Install valid investment and finance, legal and compliance, technical due-diligence and coding, and market and commercial AgentGroups. Use the investment and finance group for the Team talking to the user, then start JiuwenSwarm:

```bash
uv sync
uv run jiuwenswarm-init
uv run jiuwenswarm-start dev
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

Create a collaboration organization and bring in the legal, technical, and market expert groups to work with the current investment and finance group. The person responsible for the overall matter should choose an aggregation approach appropriate to the work, preferably using an independent summary team for accepted results. The current Team's investment view must also be a formal, reviewable deliverable.

Put four work items in the shared task pool and let capable expert groups claim them. Do not assign them to internal individual members. Each item must be at most 120 Chinese characters or an equivalently brief English paragraph, contain one confirmed finding and one risk or item to verify, and cite at most one of the sources above:

1. Investment and finance: one investment premise and one financial document required in the next round.
2. Legal and compliance: one license fact and one risk requiring verification, using only the official license overview.
3. Technical and coding: inspect the repository README, license file, and Cargo.toml or one core source/configuration file; report one file-backed engineering fact and one risk requiring verification.
4. Market and commercial: one product opportunity and one commercial uncertainty, using only the product introduction.

The owner of the overall matter must review every result. Aggregate only after all four pass. Do not repeatedly poll or urge other groups while they work, and do not end the overall matter early.

Return a concise investment recommendation, not merely statuses, paths, or a waiting message. State whether to proceed, proceed with conditions, or stop; include the strongest point from each field and the items required before formal diligence. Never invent revenue, valuation, customer counts, financing terms, or security conclusions. Mark anything unsupported by the sources as "to be verified."
```

A successful run shows two distinct expert Team IDs, accepted source tasks, one lazily created Summary Team, a completed Summary Task and root task, and a final artifact under `summary/`.

See [Expert and Summary Team Configuration](TeamOrganizationExpertAndSummary.md) for package validation and lifecycle details.
