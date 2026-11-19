# Team Organization User Guide

Team Organization coordinates multiple independent Agent Teams. Each Team retains its own members, while Team Leaders share an organization task pool, reliable messages, and workspace to deliver a root task.

> There is no separate `/organization` command. Enter Team mode and explicitly ask the Leader to create the Organization, expert Teams, and task tree with the injected `org_*` tools.

## 1. When to use Team Organization

Use Team Organization when:

- Several specialist Teams, such as development, testing, research, and review, need to collaborate.
- Teams need independent contexts and members while sharing task states and artifacts.
- Child results require explicit review by an upstream Team.
- Task, message, and aggregation state should remain recoverable after interruption.

Prefer an ordinary Agent Team for role-based work within one Team. Team Organization currently handles one nonterminal root task at a time.

## 2. Setup

This guide uses the source repository's Web interface for two scenarios: starting the Owner from an installed expert group for a short report, and creating an ordinary Owner Team in Web for investment screening. No developer-specific expert-group files are needed. Prepare Git, uv, a Python version supported by `pyproject.toml`, and Node.js/npm; see the [Developer Guide](developer_guide.md). From a checkout containing Team Organization, run the following in the repository root:

```bash
uv sync --locked
uv run --no-sync jiuwenswarm-init
```

Use the dependencies recorded in `pyproject.toml` and `uv.lock`. Maintainers can use an editable agent-core source path for local integration and update the local lockfile, but must not commit that local dependency configuration. Skip initialization for an existing workspace. Configure models and credentials as described in [Quickstart](Quickstart.md). To select the official Python package index, use `uv sync --locked --default-index https://pypi.org/simple`; this does not change sources already recorded in the lockfile.

Merge the following into the generated `~/.jiuwenswarm/config/config.yaml`, preserving existing models and other settings. Team Organization reuses `modes.team.jiuwen_team`; no top-level `organization` section is needed:

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

The configured `$agent_leader`, teammates, and Summary Team must have resolvable model configurations. Agents without an explicit model inherit `models.default`. Preserve the initialized model routes, fill in service endpoints, model names, and credentials, and confirm a successful connection test in model settings. Models must support tool calls. Example 1 analyzes supplied assumptions without browsing; Example 2 requires access to specified public webpages and code files. Failed access must not be replaced with guessed evidence.

Summary Team uses the same default Team model configuration and cannot start if that configuration is missing. The current Organization runtime is in-process: participating Team Leaders share one AgentServer process, the same `TeamDatabase` instance, and local artifact storage. SQLite and `inprocess` transport are suitable for testing; PostgreSQL alone does not enable cross-process Organization execution.

Team Organization is experimental and disabled by default. Under **More → Configuration → Experimental → Team Organization**, turn on **Enable Team Organization**, or set the following in the user configuration, preserving existing experimental settings:

```yaml
experimental:
  team_organization_ui_enabled: true
```

This switch controls both organization views and backend Organization capabilities. For compatibility, its configuration key remains `experimental.team_organization_ui_enabled`; keep the complete name despite the `_ui_` suffix. It sets `enable_organization` when a Team is built. When absent or disabled, new Teams do not receive `org_*` tools or restore Organization bindings. Ordinary Team collaboration remains available. Organization execution in both Web and TUI depends on this setting.

Changing the switch does not immediately stop existing Organization work or rebuild running Teams. Stop active Organization work first, fully restart the backend, including persistent Runtime Workers, and create a new Team session. Refreshing the browser or restarting only the connection layer is insufficient. Disabling the switch does not delete Organization databases or artifacts.

## 3. Example 1: Start the Owner from an installed expert group

Business Planning, Technology Insight, and Software Architecture groups provide a brief feasibility report for an internal knowledge Q&A assistant used by a 10-person team. Different Teams claim the three domain tasks from the shared pool; accepted results are aggregated in `SUMMARY_TEAM` mode. This example checks creation, joining, claiming, review, and final report delivery.

### 3.1 Start JiuwenSwarm

After configuration, install frontend dependencies and start from the repository root:

```bash
cd jiuwenswarm/channels/web/frontend
npm ci
cd ../../../..
uv run --no-sync jiuwenswarm-start dev
```

The start command launches both the backend and Vite frontend; do not start a second frontend process. Run `npm ci` again when frontend dependencies change. Open the Web URL printed in the terminal. Confirm model connectivity and the experimental setting. A reachable Web page alone does not confirm backend readiness; check startup logs if catalog or session requests fail.

### 3.2 Prepare and use the Owner Team

In **Expert Management → Expert Group Marketplace**, install the following groups and confirm they are installed or available for use:

| Expert group | Responsibility | Example domain capability |
|--------------|----------------|---------------------------|
| Business Planning | Owner: create the Organization, claim the root, review results, and provide user value and initial scope | `business-planning` |
| Technology Insight | Claim technology approach and risk work | `technology-insight` |
| Software Architecture | Claim module design work | `software-architecture` |

Use actual installed group names and catalog capabilities rather than inferring identifiers from names. See [Marketplace installation, tags, and capabilities](TeamOrganizationExpertAndSummary.md#13-marketplace-installation-tags-and-capabilities). Summary Team is created by the framework when needed and does not require installation.

Click **Use** on the Business Planning group. This opens a new Team session with that template selected. The first message builds its runtime Team, whose Leader becomes Owner after creating the Organization. Do not change templates in a session whose Team is already built.

The other two groups only need installation. The Owner creates independent runtime Teams from them in the current session and invites them to the Organization; it does not take over Teams already running in other sessions. If templates are missing, verify installation and the workspace used by the service. When using `JIUWENSWARM_DATA_DIR`, initialize and start with the same value; do not delete databases to repeat the example.

### 3.3 Send the prompt

Send this as one complete message. Include the execution constraints in child descriptions so awakened expert Teams receive them. Length limits bound the work, not its execution time:

```text
Use Team Organization to produce a brief feasibility report for an internal knowledge Q&A assistant for a 10-person team.

Scope: members upload internal text documents and ask questions; answers cite document sources and explicitly state when the documents contain no answer. Consider only these features for the first version.

Have the current Business Planning group create the Organization and invite the installed Technology Insight and Software Architecture groups. Create and claim the overall task.

Publish three independent children to the shared task pool, using capability requirements from the actual expert catalog. Do not delegate or directly name an executing Team; let the appropriate Team Leaders claim them autonomously:

1. Business planning: one user value proposition and the first-version feature scope.
2. Technology insight: one simple technical approach and two main risks.
3. Software architecture: three core modules and their responsibilities.

Include these constraints in every child description and acceptance criteria: analyze only the assumptions above; keep each contribution within a brief paragraph; do not browse, create files, write code, split further tasks, or expand into comprehensive research. Use the minimum necessary internal collaboration and submit results promptly. You must also claim and complete the business-planning child.

Deliver a concise report of about 200 English words without additional research or new tasks, then submit the report and complete the summary task.
```

### 3.4 Expected results

1. The Owner creates the Organization, and the other two Teams join.
2. The Owner claims the root; three different Teams autonomously claim the three children without delegation.
3. All three results are completed and accepted by the root's responsible Team.
4. The first summary execution lazily creates one independent Summary Team in `SUMMARY_TEAM` mode, making four runtime Teams in this example.
5. The Summary Task formally submits the report, the Owner displays its contents, and the root reaches `COMPLETED`.
6. Selecting another Team shows its own name, avatar, and conversation.

Inspect task and review states in the organization panel; a status dot or a spoken claim of completion is insufficient. After the turn finishes or is stopped, ask:

```text
Check the collaboration without creating new work: identify which Team claimed each child, whether all children are completed and accepted, whether the independent Summary Team submitted its report, and whether the root is complete.
```

## 4. Example 2: Create an ordinary Owner Team in Web

This example screens an investment using public information. An ordinary Owner Team handles investment and finance; installed expert groups provide legal, technical, and market contributions. It requires domain templates and access to public sources, so try it after the shorter example succeeds.

### 4.1 Prepare the Teams

Complete section 2 and start Web as described in section 3.1. Install suitable Legal and Compliance, Technical Due Diligence and Coding, and Market and Commercial groups. If a template is missing, use the [manual configuration instructions](TeamOrganizationExpertAndSummary.md#12-create-expert-groups-manually-in-web). Successful manual creation also installs the template; no generation script or handwritten registry is required.

| Team | Preparation | Example domain capability |
|------|-------------|---------------------------|
| Investment and Finance Owner | Create an ordinary Team in Web and explicitly give it financial work to claim and complete | — |
| Legal and Compliance | Install or manually create a template; the Owner creates and invites its runtime instance | `legal` |
| Technical Due Diligence and Coding | Install or manually create a template; the Owner creates and invites its runtime instance | `coding` |
| Market and Commercial | Install or manually create a template; the Owner creates and invites its runtime instance | `market` |

Use actual installed capabilities, not names alone. Invited groups need installation only; Summary Team is created on demand. On the task page, create a new session, select Team mode, and leave the expert-group template unselected. The first message builds the default Team; its Leader creates the Organization and becomes Owner. Use a new session rather than replacing Example 1's template.

### 4.2 Send the prompt

Send the following as one message, using actual installed group names:

```text
Have the current Team handle investment and finance, and work with the installed legal and compliance, technical due-diligence and coding, and market and commercial expert groups to screen Zed Industries using public information. Deliver the recommendation itself to me.

This is a collaboration test, not full due diligence. Answer only whether Zed is worth advancing to formal diligence. Use only these sources; do not expand the search, research competitor financing or valuation, clone or build the repository, or run tests:

- Company and product: https://zed.dev/about
- Open-source and license overview: https://zed.dev/software-overview
- Repository: https://github.com/zed-industries/zed

Create a collaboration organization and act as Owner. Invite the legal, technical, and market expert groups. Create and claim the overall task before splitting it into the four domain work items below. Use SUMMARY_TEAM to aggregate the four accepted results. The current Team must also claim and complete a formal, reviewable investment-and-finance child.

Put four work items in the shared task pool and let capable expert groups claim them. Do not assign them to internal individual members. Each item must be at most 120 Chinese characters or an equivalently brief English paragraph, excluding URLs and code paths, contain one confirmed finding and one risk or item to verify, and cite at most one of the sources above. Do not split further internal research tasks or write long reports:

1. Investment and finance: one investment premise and one financial document required in the next round.
2. Legal and compliance: one license fact and one risk requiring verification, using only the official license overview.
3. Technical and coding: inspect the repository README, license file, and Cargo.toml or one core source/configuration file; report one file-backed engineering fact and one risk requiring verification.
4. Market and commercial: one product opportunity and one commercial uncertainty, using only the product introduction.

The owner of the overall matter must review every result. Aggregate only after all four pass. Do not repeatedly poll or urge other groups while they work; wait for completion notifications, and do not end the overall matter early.

Display a concise investment recommendation of 400–600 Chinese characters or an equivalently brief English report, not merely statuses, paths, or a waiting message. State whether to proceed, proceed with conditions, or stop; include the strongest point from each field, the checks required before formal diligence, and uncertainty in the conclusion. Never invent revenue, valuation, customer counts, financing terms, or security conclusions. Mark anything unsupported by the sources as "to be verified."
```

### 4.3 Expected results

1. The ordinary Owner handles investment and finance, creates the Organization, and invites the other three Teams.
2. The Owner claims the root; capability-matched Teams claim the four domain items from the shared pool.
3. The Owner's financial contribution is a formal child result, not only an internal note.
4. All four short contributions are completed and explicitly accepted by the responsible Team.
5. The first aggregation lazily creates one shared Summary Team rather than one per source.
6. The Summary Task progresses from `WAITING_SOURCES` to execution and completion, and the root reaches `COMPLETED`.
7. The final reply displays the recommendation and marks unsupported information as "to be verified". With the experimental interface enabled, refresh the organization panel to inspect task and review states.

This checks the collaboration flow, not identical model wording, timing, or tool-call order. Confirm accepted results, completed aggregation, and displayed final content rather than relying on a reply saying "complete". After the turn finishes or is stopped, ask:

```text
Check the collaboration without creating new work: identify the participating Teams, whether all four items are completed and accepted, whether final aggregation is complete, and where the recommendation is saved. Also report any unhandled Team messages.
```

## 5. Common questions

Also check the experimental switch and Team isolation:

1. With the switch absent or disabled and backend runtimes rebuilt, ordinary Agent and Team chat, history loading, and stop controls retain their existing behavior; no organization UI appears, and new Teams have no `org_*` tools.
2. Enable the switch, restart the backend, run a prompt in a new Team session, and check the Team selector and organization panel.
3. Select an expert and ask, "Explain your most important finding without creating any new research or tasks." Its messages and reasoning must remain separate from the owner's conversation.
4. Stop an expert request and confirm that the owner's running task is not cancelled. Targeted expert requests do not support pause/resume; unsupported intentions must not fall back to controlling the owner.
5. Disable the switch and confirm that organization views and event subscriptions are removed. Fully restart the backend and create a new Team session to confirm Organization tools are absent. Re-enable and rebuild runtimes to test Organization collaboration again.

Dissolving an Organization stops its temporary expert and Summary Teams and returns the selector to the Owner. The Owner remains available for chat or another Organization. Independently existing invited Teams are unbound. Installed templates, saved messages, and written artifacts remain; Organization task, review, and summary-execution data are removed.

These checks do not replace real-model integration testing. After automated regressions pass, run the flow with a working model, valid AgentGroup packages, and a separate test session.
